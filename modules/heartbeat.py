"""
Heartbeat — autonomous initiation loop.

Fires on a timer. On each tick, decides whether Lumi should say
something without being prompted. If yes, generates an in-character
message and pushes it through the normal pipeline. If no, stays silent.

Two tiers of valid reasons to speak:
  - REACTIVE: something specific happened (tool failed, it's very
    late, unresolved thread from last session)
  - SPONTANEOUS: Lumi just feels like talking (bored, had a thought,
    wants to check in, something reminded her of something)

The spontaneous tier is the important one. A companion who only
speaks when something is wrong is not good company.
"""

import asyncio
import ctypes
import time
import datetime
import requests

from modules.module import Module
from constants import (LLM_ENDPOINT, MODEL_NAME, AI_NAME, HOST, FAST_MODEL_NAME,
                       FAST_MODEL_ENABLED, FAST_MODEL_NUM_CTX, FAST_MODEL_KEEP_ALIVE,
                       get_chat_system_prompt)
from modules.log_hub import emit as hub_emit
from modules.llm_providers import chat_sync, LLMResponse

# ===============================================================
# TUNING KNOBS
# ---------------------------------------------------------------
# All times in seconds. Adjust after observing real behavior.
# ===============================================================
TICK_SECONDS = 20                  # How often the heartbeat checks in
MIN_IDLE_SECONDS = 90              # User must be quiet this long before we consider breaking silence
MIN_GAP_BETWEEN_UTTERANCES = 180   # At least 3 min between unprompted messages
LATE_NIGHT_START = 23              # 11 PM — the "it's late" window begins
LATE_NIGHT_END = 5                 # 5 AM — the window ends
LATE_NIGHT_IDLE_SECONDS = 1800     # During late night, require longer idle (30 min)

# How many of our own recent unprompted messages to show the model so
# it can avoid repeating itself.
RECENT_UTTERANCE_MEMORY = 3


DECISION_PROMPT = """You are {ai_name}, deciding whether to break silence with {host_name}.

Current context:
- Time: {time_str}
- It has been {idle_min} minutes since {host_name} last spoke.
- Last conversation topic: {last_topic}
- What {host_name} is currently doing: {active_window}
- Your current mood: {mood}
- Your recent unprompted messages (do NOT repeat these): {recent_utterances}

The user has NOT prompted you. You are deciding on your own whether to say something.

RULES:
- Most of the time, the correct answer is SILENCE. Silence is normal and expected.
- You have two kinds of valid reasons to speak:

  REACTIVE reasons (something specific happened):
    - It's very late at night and he should sleep.
    - An earlier tool failed and he might want to know.
    - The last conversation ended on something unresolved and you want to follow up.

  SPONTANEOUS reasons (you just feel like talking):
    - You're bored and want to start a conversation about something.
    - You had a thought, an observation, or a question you want to share.
    - It's been a very long time and you want to check in.
    - Something in the context (what he's doing, the time, your mood) reminded you of something specific.

- ALWAYS anchor to something concrete in the context. "How's the coding going?" is bad —
  it's generic. "Still stuck in prompter.py, or did you finally fix that regex?" is good —
  it shows you noticed what he's doing.
- Spontaneous reasons should be used MORE often than reactive ones. A companion
  who only speaks when something is wrong is not good company.
- Do NOT repeat a recent unprompted message. Vary the topic, phrasing, and reason.
- When in doubt, lean toward speaking. Silence is a valid default, but
  perpetual silence is worse than occasional noise.

EMOTION TAGS:
- Start your message with ONE emotion tag in square brackets.
- Approved tags: [sarcastic] [sighing] [chuckling] [laughing] [curious] [bored]
  [confident] [soft tone] [whispering] [worried] [happy] [indifferent]
- Example: "[sarcastic] Still on that bug, or did you finally give up?"

RESPONSE FORMAT:
- If you decide to speak: respond with your message only, starting with an emotion tag.
  One or two short sentences in your normal voice, like a person talking. Anchor to
  something specific.
- If you decide to stay silent: respond with exactly the word SILENCE

Your response:"""


class Heartbeat(Module):
    def __init__(
        self,
        signals,
        enabled=True,
        tick_seconds=TICK_SECONDS,
        min_idle_seconds=MIN_IDLE_SECONDS,
        min_gap_between_utterances=MIN_GAP_BETWEEN_UTTERANCES,
        decision_model=None,
        modules=None,
    ):
        super().__init__(signals, enabled)

        self.tick_seconds = tick_seconds
        self.min_idle_seconds = min_idle_seconds
        self.min_gap_between_utterances = min_gap_between_utterances
        # The fast model, like casual chat: the main model doesn't fit in
        # VRAM next to it and the game agent's model, so every 30 s check
        # pushed the fast model out and the next chat reply waited ~8 s
        # for it to reload.
        self.decision_model = decision_model or (FAST_MODEL_NAME if FAST_MODEL_ENABLED
                                                 else MODEL_NAME)

        # Optional reference to the module list so we can find TTS if we
        # need to push audio directly (fallback path when signals.tts_queue
        # isn't available). Normal path uses signals.tts_queue.
        self.modules = modules if modules is not None else []

        # Track the timestamp of our last self-initiated message so we don't
        # speak too often. Also reset on "silence" decisions so we don't
        # immediately re-ask every 30 seconds.
        self._last_utterance_time = 0.0

        # Ring buffer of our last N unprompted messages, so the model can
        # avoid repeating itself.
        self._recent_utterances = []

    # ---------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------
    def _now(self):
        return time.time()

    def _seconds_since_user_message(self):
        """Time since the user last typed or spoke."""
        last = getattr(self.signals, "last_message_time", 0) or 0
        if last == 0:
            return float("inf")
        return self._now() - last

    def _seconds_since_our_last_utterance(self):
        if self._last_utterance_time == 0:
            return float("inf")
        return self._now() - self._last_utterance_time

    def _is_user_active(self):
        """True if we should not interrupt right now.

        Two independent checks, either one is enough:

          1. The tail of history is a user message with no assistant
             reply yet — the prompter is mid-turn, even if no busy
             flag has been set.
          2. The user spoke less than `min_idle_seconds` ago, as
             measured by signals.last_message_time.

        If neither timestamp nor a pending user message exists, we
        default to *active* (do not speak) rather than idle. A fresh
        session should stay silent until the user has actually gone
        quiet, not fire on tick 1."""
        # Check 1: unanswered user message at the tail of history
        history = getattr(self.signals, "history", []) or []
        if history and history[-1].get("role") == "user":
            return True

        # Check 2: recent user timestamp
        last = getattr(self.signals, "last_message_time", 0) or 0
        if last == 0:
            # No timestamp at all. Be conservative.
            return True

        return (self._now() - last) < self.min_idle_seconds

    def _is_busy(self):
        """True if the prompter is currently thinking, speaking, or
        anywhere in an agent turn.

        Checks multiple flag names because the prompter sets different
        ones at different stages (LLM call, tool dispatch, TTS). Any one
        of them being True means we stay quiet."""
        for flag in (
            "AI_thinking",
            "AI_speaking",
            "agent_turn_active",
            "in_agent_turn",
            "agent_busy",
            "prompter_busy",
            "processing",
        ):
            if getattr(self.signals, flag, False):
                return True
        return False

    def _is_in_cooldown(self):
        """True if we spoke unprompted recently."""
        return self._seconds_since_our_last_utterance() < self.min_gap_between_utterances

    def _is_late_night(self):
        hour = datetime.datetime.now().hour
        return hour >= LATE_NIGHT_START or hour < LATE_NIGHT_END

    def _last_topic(self):
        """Grab a short hint at what the recent conversation was about."""
        history = getattr(self.signals, "history", []) or []
        for msg in reversed(history[-6:]):
            if msg.get("role") == "user":
                content = (msg.get("content") or "").strip()
                if content:
                    return content[:120]
        return "(no recent topic)"

    def _current_mood(self):
        mood_mgr = getattr(self.signals, "mood_manager", None)
        if mood_mgr:
            return getattr(mood_mgr, "current_mood", "default")
        return "default"

    def _get_active_window_title(self):
        """Read the current foreground window title via Win32 API.
        Returns an empty string on non-Windows or on any failure."""
        try:
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return ""
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return ""
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value.strip()
            # Truncate very long titles (some apps put whole doc contents in the title)
            return title[:120]
        except Exception:
            return ""

    def _format_active_window(self):
        """Return a readable description of what the user is doing.
        Uses the ambient module's cached VLM description when available,
        falls back to just the window title."""
        title = self._get_active_window_title() or "(unknown window)"

        ambient = getattr(self.signals, "ambient", None)
        if ambient:
            try:
                line = ambient.get_context_line()
                if line:
                    return line
            except Exception:
                pass

        return title

    def _format_recent_utterances(self):
        """Return a short string of our recent unprompted messages, or a
        hint that there haven't been any."""
        if not self._recent_utterances:
            return "(none yet — this will be your first unprompted message this session)"
        # Show them oldest-first so the model sees the progression
        return " | ".join(f'"{u}"' for u in self._recent_utterances)

    def _remember_utterance(self, text):
        """Add an unprompted message to the recent ring buffer."""
        self._recent_utterances.append(text)
        if len(self._recent_utterances) > RECENT_UTTERANCE_MEMORY:
            self._recent_utterances.pop(0)

    # ---------------------------------------------------------------
    # Decision logic
    # ---------------------------------------------------------------
    def _fmt_age(self, seconds):
        """Format a duration for log messages. Handles float('inf')
        (fresh session, no user message yet) and NaN without raising."""
        if seconds == float("inf") or seconds != seconds:   # inf or NaN
            return "never"
        try:
            return f"{int(seconds)}s"
        except (OverflowError, ValueError):
            return "∞"

    def _should_consider_acting(self):
        """Cheap gating before we even call the model. Returns
        (True, reason) or (False, reason)."""
        if self._is_busy():
            return False, "prompter busy"

        user_age = self._seconds_since_user_message()
        if self._is_user_active():
            return False, f"user active ({self._fmt_age(user_age)} ago)"

        our_age = self._seconds_since_our_last_utterance()
        if self._is_in_cooldown():
            return False, f"cooldown ({self._fmt_age(our_age)})"

        if self._is_late_night():
            if user_age < LATE_NIGHT_IDLE_SECONDS:
                return False, (
                    f"late night, not idle enough "
                    f"({self._fmt_age(user_age)} ago)"
                )

        return True, "eligible"

    def _ask_model(self):
        """Send the decision prompt. Returns the raw model response or None."""
        idle_seconds = self._seconds_since_user_message()
        idle_min = int(idle_seconds // 60) if idle_seconds != float("inf") else 9999

        now = datetime.datetime.now()
        time_str = now.strftime("%A %I:%M %p")

        prompt = DECISION_PROMPT.format(
            ai_name=AI_NAME,
            host_name=HOST.name,
            time_str=time_str,
            idle_min=idle_min,
            last_topic=self._last_topic(),
            active_window=self._format_active_window(),
            mood=self._current_mood(),
            recent_utterances=self._format_recent_utterances(),
        )

        try:
            fast = self.decision_model == FAST_MODEL_NAME
            # Her persona first, so the line sounds like her (and Ollama
            # reuses the prompt cache chat built).
            resp: LLMResponse = chat_sync(
                self.decision_model,
                [{"role": "system", "content": get_chat_system_prompt()},
                 {"role": "user", "content": prompt}],
                think=False,
                keep_alive=FAST_MODEL_KEEP_ALIVE if fast else None,
                options={"temperature": 0.7, "top_p": 0.9, "num_predict": 120,
                         **({"num_ctx": FAST_MODEL_NUM_CTX} if fast else {})},
            )
            return (resp.content or "").strip()
        except Exception as e:
            hub_emit(self.signals, "heartbeat",
                     f"decision call failed: {e}", level="warn")
            return None

    def _emit_utterance(self, text):
        """Push the message into the normal pipeline: history + TTS."""
        from modules.prompter import CASUAL_SENTENCES, _clean_reply, limit_sentences
        text = limit_sentences(_clean_reply(text) or text, CASUAL_SENTENCES)
        # Notify the self-model that she spoke unprompted.
        if hasattr(self.signals, "self_model"):
            try:
                self.signals.self_model.note_unprompted(text)
            except Exception:
                pass

        # Add to history as an assistant message so it appears in the log
        self.signals.history.append({"role": "assistant", "content": text})

        # Trim history like the prompter does
        if len(self.signals.history) > 20:
            self.signals.history = self.signals.history[-20:]

        # Push to TTS
        if hasattr(self.signals, "tts_queue"):
            self.signals.tts_queue.put(text)
        else:
            tts_module = next(
                (m for m in self.modules if m.__class__.__name__ == "TTS"),
                None,
            )
            if tts_module and hasattr(tts_module, "speak_stream"):
                tts_module.speak_stream(text)

        # Remember for repetition avoidance
        self._remember_utterance(text)

        # Save history
        if hasattr(self.signals, "save_history"):
            self.signals.save_history()

        self._last_utterance_time = self._now()

    # ---------------------------------------------------------------
    # Main loop
    # ---------------------------------------------------------------
    async def run(self):
        if not self.enabled:
            return

        hub_emit(
            self.signals, "heartbeat",
            f"loop started (tick={self.tick_seconds}s, "
            f"idle={self.min_idle_seconds}s, "
            f"cooldown={self.min_gap_between_utterances}s)"
        )

        loop = asyncio.get_running_loop()

        while not self.signals.terminate:
            await asyncio.sleep(self.tick_seconds)

            if self.signals.terminate:
                break

            should_act, reason = self._should_consider_acting()
            if not should_act:
                # Debug logging — comment out if too noisy
                # print(f"\033[90mHEARTBEAT: skip ({reason})\033[0m")
                continue

            # Conditions are met — ask the model for a decision
            hub_emit(self.signals, "heartbeat", "conditions met, asking model")
            response = await loop.run_in_executor(None, self._ask_model)

            if not response:
                continue

            # Treat the literal token SILENCE as "do not speak"
            upper = response.strip().upper()
            if "SILENCE" in upper and len(response.strip()) < 30:
                hub_emit(self.signals, "heartbeat", "model chose silence")
                # Reset the cooldown window even though we didn't speak —
                # otherwise we'd re-ask every single tick, which wastes
                # model calls and contributes nothing.
                self._last_utterance_time = self._now()
                continue

            # Otherwise, treat the response as a real utterance
            # Unprompted speech is the interesting event — keep in
            # terminal + hub so you can see it happen live.
            print(f"\033[95mHEARTBEAT: Lumi speaks unprompted:\033[0m {response}")
            hub_emit(self.signals, "heartbeat",
                     f"unprompted: {response[:200]}")
            self._emit_utterance(response)
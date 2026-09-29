"""
Self-model — Lumi's tracking of her own patterns over time.

Three jobs:
  1. Collect. Every 60s, snapshot emotional state. After each turn,
     count tags she used, which frame fired, response length. When
     heartbeat fires, note that she spoke unprompted.
  2. Reflect. Every ~10 min, ask the fast model to notice patterns
     in the collected data ("you've been more sarcastic than usual
     today"). Store observations as short strings.
  3. Inject. get_injection() returns a sparse self-awareness block,
     or "" if there's nothing interesting. Observations are framed
     as internal awareness, not something to announce.

Files:
  modules/self_model.py                — this file
  data/memories/self_model.json        — persisted observations
                                          across sessions
"""

import asyncio
import json
import os
import re
import time
import datetime
import threading
from collections import Counter, deque

import requests

from modules.module import Module
from constants import (
    LLM_ENDPOINT, FAST_MODEL_NAME, MODEL_NAME, AI_NAME, DATA_ROOT,
)
from modules.llm_providers import chat_sync, LLMResponse

# ===============================================================
# CONFIG
# ===============================================================
EMOTIONAL_SNAPSHOT_INTERVAL = 60         # seconds between snapshots
EMOTIONAL_WINDOW = 120                   # snapshots kept (~2h)
TURN_WINDOW = 100                        # turns kept
REFLECTION_INTERVAL = 600                # ~10 min
REFLECTION_MIN_TURNS = 5                 # don't reflect on nothing
OBSERVATION_STALE_AFTER = 3600           # drop observations after 1h
OBSERVATION_CAP = 4                      # max observations kept

# Persisted state lives under data/memories/ after the folder reorg.
SELF_MODEL_PATH = os.path.join(DATA_ROOT, "memories", "self_model.json")


# ===============================================================
# CANONICAL TAG REGEX
# ---------------------------------------------------------------
# Only count tags from Fish Audio's S2 documentation. Invented tags
# are ignored so the delivery stats stay meaningful.
# ===============================================================
_KNOWN_TAGS = {
    # Basic emotions
    "happy", "sad", "angry", "excited", "calm", "nervous", "confident",
    "surprised", "satisfied", "delighted", "scared", "worried", "upset",
    "frustrated", "depressed", "empathetic", "embarrassed", "disgusted",
    "moved", "proud", "relaxed", "grateful", "curious", "sarcastic",
    # Advanced emotions
    "disdainful", "unhappy", "anxious", "hysterical", "indifferent",
    "uncertain", "doubtful", "confused", "disappointed", "regretful",
    "guilty", "ashamed", "jealous", "envious", "hopeful", "optimistic",
    "pessimistic", "nostalgic", "lonely", "bored", "contemptuous",
    "sympathetic", "compassionate", "determined", "resigned",
    # Tone markers
    "in a hurry tone", "shouting", "screaming", "whispering",
    "soft tone", "emphasis",
    # Audio effects
    "laughing", "chuckling", "sobbing", "crying loudly", "sighing",
    "groaning", "panting", "gasping", "yawning", "snoring",
    "clear throat",
    # Pauses / crowd
    "break", "long-break", "audience laughing", "background laughter",
    "crowd laughing",
}

_TAG_REGEX = re.compile(
    r"\[(" + "|".join(re.escape(t) for t in _KNOWN_TAGS) + r")\]",
    re.IGNORECASE,
)


# ===============================================================
# REFLECTION PROMPT
# ===============================================================
_REFLECTION_PROMPT = """You are {ai_name}, an AI companion doing a quiet self-check. Look at your own recent behavior and notice 2 to 4 short patterns.

EMOTIONAL HISTORY (last ~{window_min} minutes):
{emotional_summary}

DELIVERY PATTERNS (this session):
{delivery_summary}

SESSION CONTEXT:
- Session length: {session_min} minutes
- Turns this session: {turn_count}
- Unprompted messages from you: {unprompted_count}

IMPORTANT: Only reference numbers and facts that appear in the data above.
Do not invent session length, unprompted counts, or emotional values.
If the data says 5 turns, do not say "an hour" — say "the last few minutes."

TASK:
Write 2 to 4 short observations about YOUR OWN recent patterns. Each under 20 words.
Focus on things that feel NOTABLE — changes, cycles, recurring moves.
Do NOT comment on unremarkable things.
Do NOT describe your personality in general — only what has shifted or repeated recently.
If nothing feels notable, return an empty list.

Good observations:
- "I've been more sarcastic than usual — leaning on it as a default."
- "Arousal has been climbing steadily for the last hour."
- "Three strategy-frame turns in a row — I've been focused longer than I usually sustain."
- "Only two unprompted messages today — quieter than my baseline."

Bad observations:
- "I am sarcastic and love my host." (describes personality, not a pattern)
- "Anthony is a tired developer." (about him, not me)

Output strict JSON: {{"observations": ["...", "..."]}}
"""


# ===============================================================
# MODULE
# ===============================================================
class SelfModel(Module):
    def __init__(self, signals, enabled=True):
        super().__init__(signals, enabled)
        self.signals = signals

        # Rolling in-session windows
        self.emotional_history = deque(maxlen=EMOTIONAL_WINDOW)
        self.turn_log = deque(maxlen=TURN_WINDOW)
        self.tag_counts = Counter()
        self.frame_counts = Counter()
        self.unprompted_count = 0
        self.session_start = time.time()

        # Cross-session persistence
        self.observations = []
        self.observations_ts = 0.0

        self._lock = threading.Lock()
        self._load()

        # Register on signals so prompter and heartbeat can find us.
        signals.self_model = self

    # ---------------------------------------------------------------
    # Persistence
    # ---------------------------------------------------------------
    def _load(self):
        if not os.path.exists(SELF_MODEL_PATH):
            return
        try:
            with open(SELF_MODEL_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.observations = data.get("observations", []) or []
            self.observations_ts = data.get("observations_ts", 0.0) or 0.0
        except Exception as e:
            print(f"\033[90mSELF-MODEL: load failed: {e}\033[0m")

    def _save(self):
        try:
            os.makedirs(os.path.dirname(SELF_MODEL_PATH) or ".", exist_ok=True)
            with open(SELF_MODEL_PATH, "w", encoding="utf-8") as f:
                json.dump({
                    "observations": self.observations,
                    "observations_ts": self.observations_ts,
                    "last_saved": datetime.datetime.now().isoformat(timespec="seconds"),
                }, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"\033[90mSELF-MODEL: save failed: {e}\033[0m")

    # ---------------------------------------------------------------
    # Collection hooks
    # ---------------------------------------------------------------
    def note_turn(self, user_text, assistant_text, frame_name):
        """Called after each assistant turn completes."""
        with self._lock:
            tags = _TAG_REGEX.findall(assistant_text or "")
            tags_lower = [t.lower() for t in tags]
            for t in tags_lower:
                self.tag_counts[t] += 1

            word_count = len((assistant_text or "").split())
            self.turn_log.append({
                "ts": time.time(),
                "word_count": word_count,
                "frame": (frame_name or "play").lower(),
                "tags": tags_lower,
            })

            if frame_name:
                self.frame_counts[frame_name.lower()] += 1

    def note_unprompted(self, text):
        """Called from heartbeat when Lumi speaks without being prompted."""
        with self._lock:
            self.unprompted_count += 1

    def _snapshot_emotional(self):
        """Record a snapshot of the current emotional state."""
        if not hasattr(self.signals, "emotional_state"):
            return
        es = self.signals.emotional_state
        try:
            with self._lock:
                self.emotional_history.append({
                    "ts": time.time(),
                    "valence": getattr(es, "valence", 0.0),
                    "arousal": getattr(es, "arousal", 0.4),
                    "frustration": getattr(es, "frustration", 0.0),
                    "curiosity": getattr(es, "curiosity", 0.5),
                    "boredom": getattr(es, "boredom", 0.2),
                })
        except Exception:
            pass

    # ---------------------------------------------------------------
    # Summaries for the reflection prompt
    # ---------------------------------------------------------------
    def _summarize_emotional(self):
        if not self.emotional_history:
            return "(no emotional data yet)"
        lines = []
        for key in ["valence", "arousal", "frustration", "curiosity", "boredom"]:
            vals = [h[key] for h in self.emotional_history]
            start_v = vals[0]
            end_v = vals[-1]
            min_v = min(vals)
            max_v = max(vals)
            avg = sum(vals) / len(vals)
            delta = end_v - start_v

            direction = ""
            if abs(delta) > 0.15:
                direction = " (climbed)" if delta > 0 else " (dropped)"

            lines.append(
                f"  {key}: start {start_v:.2f}, end {end_v:.2f}, "
                f"range {min_v:.2f}-{max_v:.2f}, avg {avg:.2f}{direction}"
            )
        return "\n".join(lines)

    def _summarize_delivery(self):
        if not self.turn_log:
            return "(no turns yet)"
        lines = []

        if self.tag_counts:
            top = self.tag_counts.most_common(5)
            tag_str = ", ".join(f"[{t}] x{c}" for t, c in top)
            lines.append(f"  Most-used tags: {tag_str}")

        if self.frame_counts:
            total = sum(self.frame_counts.values()) or 1
            frames = ", ".join(
                f"{name} ({100 * count // total}%)"
                for name, count in self.frame_counts.most_common()
            )
            lines.append(f"  Cognitive frames: {frames}")

        lengths = [t["word_count"] for t in self.turn_log]
        if lengths:
            avg = sum(lengths) / len(lengths)
            lines.append(f"  Average response length: {avg:.0f} words")

        return "\n".join(lines) if lines else "(no delivery data yet)"

    # ---------------------------------------------------------------
    # Reflection
    # ---------------------------------------------------------------
    def _reflect(self):
        """One reflection pass. Runs in a worker thread."""
        with self._lock:
            emotional_snapshot = list(self.emotional_history)
            turn_count = len(self.turn_log)
            unprompted = self.unprompted_count

        if turn_count < REFLECTION_MIN_TURNS:
            return

        window_min = 0
        if emotional_snapshot:
            window_min = max(1, int(
                (time.time() - emotional_snapshot[0]["ts"]) / 60
            ))

        session_min = int((time.time() - self.session_start) / 60)

        prompt = _REFLECTION_PROMPT.format(
            ai_name=AI_NAME,
            window_min=window_min,
            emotional_summary=self._summarize_emotional(),
            delivery_summary=self._summarize_delivery(),
            session_min=session_min,
            turn_count=turn_count,
            unprompted_count=unprompted,
        )

        try:
            resp: LLMResponse = chat_sync(
                MODEL_NAME,
                [{"role": "user", "content": prompt}],
                keep_alive="30s",
                options={
                    "temperature": 0.4,
                    "num_ctx": 8192,
                    "num_predict": 300,
                },
            )
            raw = (resp.content or "").strip()
        except Exception as e:
            print(f"\033[90mSELF-MODEL: reflection call failed: {e}\033[0m")
            return

        # Strip code fences
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        parsed = None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if m:
                try:
                    parsed = json.loads(m.group(0))
                except json.JSONDecodeError:
                    parsed = None

        if not parsed:
            return

        raw_obs = parsed.get("observations") or []
        if not isinstance(raw_obs, list):
            return

        observations = [
            str(o).strip()[:140]
            for o in raw_obs
            if isinstance(o, (str, int, float)) and str(o).strip()
        ][:OBSERVATION_CAP]

        with self._lock:
            self.observations = observations
            self.observations_ts = time.time()

        self._save()

        try:
            from modules.log_hub import emit as hub_emit
            hub_emit(
                self.signals, "system",
                f"self-model reflected ({len(observations)} observations)"
            )
        except Exception:
            pass

    # ---------------------------------------------------------------
    # Injection
    # ---------------------------------------------------------------
    def get_injection(self):
        """Return the self-awareness injection, or '' when nothing to say."""
        if not self.observations:
            return ""

        age = time.time() - self.observations_ts
        if age > OBSERVATION_STALE_AFTER:
            return ""

        bullets = "\n".join(f"- {o}" for o in self.observations)
        return (
            "[Self-observations — internal awareness only]\n"
            f"{bullets}\n"
            "Let these color your tone if they're relevant. "
            "Do NOT announce them, list them, or refer to them directly."
        )

    # ---------------------------------------------------------------
    # Run loop
    # ---------------------------------------------------------------
    async def run(self):
        if not self.enabled:
            return

        print(f"\033[90mSELF-MODEL: tracking started "
              f"(reflection every {REFLECTION_INTERVAL}s)\033[0m")

        last_reflection = 0.0
        loop = asyncio.get_running_loop()

        while not self.signals.terminate:
            await asyncio.sleep(EMOTIONAL_SNAPSHOT_INTERVAL)
            if self.signals.terminate:
                break

            try:
                self._snapshot_emotional()
            except Exception:
                pass

            now = time.time()
            if now - last_reflection >= REFLECTION_INTERVAL:
                with self._lock:
                    ready = len(self.turn_log) >= REFLECTION_MIN_TURNS
                if ready:
                    try:
                        await loop.run_in_executor(None, self._reflect)
                    except Exception as e:
                        print(f"\033[91mSELF-MODEL: reflect error: {e}\033[0m")
                    last_reflection = now
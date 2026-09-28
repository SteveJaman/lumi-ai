"""
Cognitive state machine.

Traits (stable, from lumi.yaml) modulate how strongly events affect the
state. Frames (also from lumi.yaml) are runtime modes chosen based on
context: agent mode, user tone, and current emotional values.

describe() supports two modes:
  update=True  (default) — re-runs frame selection with the given context
                and stores the result. Use during actual turns.
  update=False — returns the cached frame. Use for diagnostics.

Phase 10.16 — per-user isolation
--------------------------------
EmotionalStateModule keeps a dict of EmotionalState instances keyed by
HOST.user_id. On HOST change (web login switch), the outgoing state is
persisted and the incoming one is loaded from disk. Every consumer that
reads signals.emotional_state sees the state belonging to whoever is
currently logged in.

Persistence lives at memories/emotional_state/{user_id}.json.
"""

import asyncio
import os
import time
import json
import threading
import datetime

import yaml

from modules.module import Module
from constants import WORKSPACE_ROOT, HOST
from modules.log_hub import emit as hub_emit


# ===============================================================
# TUNING KNOBS
# ===============================================================

DECAY_INTERVAL = 30.0
DECAY_RATE = 0.05
BOREDOM_TICK_RATE = 0.03
BOREDOM_IDLE_THRESHOLD = 120

FRUSTRATION_HIGH = 0.60
FRUSTRATION_MID = 0.35
FRUSTRATION_LOW = 0.12

VALENCE_VERY_POSITIVE = 0.40
VALENCE_POSITIVE = 0.15
VALENCE_VERY_NEGATIVE = -0.30
VALENCE_NEGATIVE = -0.08

AROUSAL_HIGH = 0.70
AROUSAL_MID = 0.52
AROUSAL_LOW = 0.20
AROUSAL_CALM = 0.32

CURIOSITY_HIGH = 0.72
CURIOSITY_MID = 0.58
CURIOSITY_LOW = 0.20

BOREDOM_HIGH = 0.60
BOREDOM_MID = 0.42
BOREDOM_LOW = 0.25

_STATE_DIR = os.path.join(WORKSPACE_ROOT, "memories", "emotional_state")


# ===============================================================
# YAML LOADER
# ===============================================================

_DEFAULT_TRAITS = {
    "playfulness": 0.75,
    "intelligence": 0.90,
    "chaos": 0.60,
    "empathy": 0.65,
    "sarcasm": 0.85,
    "warmth": 0.40,
    "curiosity": 0.70,
    "patience": 0.30,
}

_DEFAULT_FRAMES = {
    "play": {"tone": "Lean into playfulness and sarcasm. Punchy, quick."},
    "strategy": {"tone": "Lock in. Steady tone, competence on display."},
    "chaos": {"tone": "Let it fly. Non-sequiturs and disproportionate threats."},
    "social": {"tone": "Drop the edge. Warm, protective, present."},
    "learning": {"tone": "Ask a specific follow-up. Show genuine interest."},
    "threat": {"tone": "Sharpen. Colder delivery, no jokes."},
}


def _load_yaml_block(block_name, defaults):
    yaml_path = os.path.join(WORKSPACE_ROOT, "lumi.yaml")
    if not os.path.exists(yaml_path):
        return dict(defaults)
    try:
        with open(yaml_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        block = data.get(block_name)
        if isinstance(block, dict) and block:
            merged = dict(defaults)
            merged.update(block)
            return merged
    except Exception:
        pass
    return dict(defaults)


# ===============================================================
# PER-USER STATE FILE
# ===============================================================
def _state_path(user_id):
    safe = "".join(c if c.isalnum() or c in "-_" else "_"
                   for c in (user_id or "default"))
    return os.path.join(_STATE_DIR, f"{safe}.json")


def _load_state_file(user_id):
    path = _state_path(user_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save_state_file(user_id, data):
    try:
        os.makedirs(_STATE_DIR, exist_ok=True)
        with open(_state_path(user_id), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"\033[90mEMOTIONAL STATE: save failed for {user_id}: {e}\033[0m")
        return False


# ===============================================================
# STATE
# ===============================================================
class EmotionalState:
    def __init__(self, signals, user_id="default"):
        self.signals = signals
        self.user_id = user_id

        self.valence = 0.0
        self.arousal = 0.4
        self.frustration = 0.0
        self.curiosity = 0.5
        self.boredom = 0.2

        self.baselines = {
            "valence": 0.0,
            "arousal": 0.4,
            "frustration": 0.0,
            "curiosity": 0.5,
            "boredom": 0.2,
        }

        self.traits = _load_yaml_block("traits", _DEFAULT_TRAITS)
        self.frames = _load_yaml_block("frames", _DEFAULT_FRAMES)
        self.current_frame = "play"

        self._last_user_message_time = time.time()
        self._lock = threading.Lock()

        self._restore()

    # ---------------------------------------------------------------
    # Persistence
    # ---------------------------------------------------------------
    def _restore(self):
        data = _load_state_file(self.user_id)
        if not data:
            return
        try:
            self.valence = float(data.get("valence", self.valence))
            self.arousal = float(data.get("arousal", self.arousal))
            self.frustration = float(data.get("frustration", self.frustration))
            self.curiosity = float(data.get("curiosity", self.curiosity))
            self.boredom = float(data.get("boredom", self.boredom))
            self.current_frame = data.get("current_frame", self.current_frame)
        except Exception:
            pass

    def snapshot(self):
        return {
            "user_id": self.user_id,
            "valence": self.valence,
            "arousal": self.arousal,
            "frustration": self.frustration,
            "curiosity": self.curiosity,
            "boredom": self.boredom,
            "current_frame": self.current_frame,
            "last_saved": datetime.datetime.now().isoformat(timespec="seconds"),
        }

    def persist(self):
        _save_state_file(self.user_id, self.snapshot())

    # ---------------------------------------------------------------
    # Trait modifiers
    # ---------------------------------------------------------------
    def _patience_mod(self):
        p = self.traits.get("patience", 0.3)
        return 1.5 - p

    def _warmth_mod(self):
        w = self.traits.get("warmth", 0.4)
        return 1.5 - w

    def _playfulness_mod(self):
        p = self.traits.get("playfulness", 0.75)
        return 0.7 + (p * 0.6)

    def _empathy_mod(self):
        e = self.traits.get("empathy", 0.65)
        return 0.7 + (e * 0.6)

    # ---------------------------------------------------------------
    # Event hooks
    # ---------------------------------------------------------------
    def note_turn_complete(self, outcomes):
        with self._lock:
            if not outcomes:
                return

            last_success, _ = outcomes[-1]
            had_failure = any(not s for s, _ in outcomes)

            if not last_success:
                self.frustration = min(
                    1.0, self.frustration + 0.20 * self._patience_mod()
                )
                self.valence = max(-1.0, self.valence - 0.10)
            elif had_failure:
                self.frustration = max(0.0, self.frustration - 0.05)
                self.valence = min(
                    1.0, self.valence + 0.05 * self._playfulness_mod()
                )
            else:
                self.frustration = max(0.0, self.frustration - 0.05)
                self.valence = min(
                    1.0, self.valence + 0.08 * self._playfulness_mod()
                )
                self.arousal = min(1.0, self.arousal + 0.03)

            self.curiosity = min(1.0, self.curiosity + 0.02 * len(outcomes))

    def note_user_message(self, text=""):
        with self._lock:
            self._last_user_message_time = time.time()
            self.arousal = min(1.0, self.arousal + 0.05)
            self.boredom = max(0.0, self.boredom - 0.3)

            text_lower = (text or "").lower()

            sincere_markers = [
                "thank you", "thanks", "i care", "i appreciate",
                "love you", "proud of", "good job", "you're the best",
            ]
            if any(m in text_lower for m in sincere_markers):
                bump = 0.15 * self._empathy_mod()
                self.valence = min(1.0, self.valence + bump)
                self.frustration = max(0.0, self.frustration - 0.2)
                self.boredom = max(0.0, self.boredom - 0.15)

            hostile_markers = [
                "shut up", "stupid", "useless", "hate you", "you suck",
                "you're wrong", "you're broken",
            ]
            if any(m in text_lower for m in hostile_markers):
                bump = 0.15 * self._warmth_mod()
                self.frustration = min(1.0, self.frustration + bump)
                self.valence = max(-1.0, self.valence - 0.1)

    def note_command(self):
        with self._lock:
            self._last_user_message_time = time.time()
            self.boredom = max(0.0, self.boredom - 0.15)

    # ---------------------------------------------------------------
    # Time-based evolution
    # ---------------------------------------------------------------
    def decay_step(self):
        with self._lock:
            for name, baseline in self.baselines.items():
                current = getattr(self, name)
                setattr(self, name, current + (baseline - current) * DECAY_RATE)

            idle = time.time() - self._last_user_message_time
            if idle > BOREDOM_IDLE_THRESHOLD:
                self.boredom = min(1.0, self.boredom + BOREDOM_TICK_RATE)

    # ---------------------------------------------------------------
    # Frame selection
    # ---------------------------------------------------------------
    def select_frame(self, agent_mode=False, user_text=""):
        text_lower = (user_text or "").lower()

        if self.frustration > 0.6:
            return "threat"

        vulnerable_markers = [
            "i'm tired", "i am tired", "i'm stressed", "i am stressed",
            "i'm sad", "i am sad", "i'm upset", "i am upset",
            "rough day", "hard day", "burnt out", "burned out",
            "exhausted", "overwhelmed", "burnt", "burt out",
        ]
        sincere_markers = [
            "thank you", "i care", "i appreciate",
            "love you", "proud of you",
        ]
        if any(m in text_lower for m in vulnerable_markers):
            return "social"
        if any(m in text_lower for m in sincere_markers):
            return "social"

        if self.arousal > 0.75 and self.valence > 0.3:
            return "chaos"

        if agent_mode:
            return "strategy"

        if self.curiosity > 0.7 and self.frustration < 0.3:
            return "learning"

        return "play"

    # ---------------------------------------------------------------
    # Prompt injection
    # ---------------------------------------------------------------
    def describe(self, agent_mode=False, user_text="", update=True):
        if update:
            frame_name = self.select_frame(agent_mode=agent_mode, user_text=user_text)
            self.current_frame = frame_name
        else:
            frame_name = self.current_frame

        frame_tone = self.frames.get(frame_name, {}).get("tone", "")

        parts = []

        if self.frustration > FRUSTRATION_HIGH:
            parts.append("genuinely frustrated")
        elif self.frustration > FRUSTRATION_MID:
            parts.append("mildly irritated")
        elif self.frustration > FRUSTRATION_LOW:
            parts.append("slightly annoyed")

        if self.valence > VALENCE_VERY_POSITIVE:
            parts.append("in a good mood")
        elif self.valence > VALENCE_POSITIVE:
            parts.append("pleased")
        elif self.valence < VALENCE_VERY_NEGATIVE:
            parts.append("grumpy")
        elif self.valence < VALENCE_NEGATIVE:
            parts.append("a bit off")

        if self.arousal > AROUSAL_HIGH:
            parts.append("energetic")
        elif self.arousal > AROUSAL_MID:
            parts.append("alert")
        elif self.arousal < AROUSAL_LOW:
            parts.append("low-energy")
        elif self.arousal < AROUSAL_CALM:
            parts.append("calm")

        if self.curiosity > CURIOSITY_HIGH:
            parts.append("very curious")
        elif self.curiosity > CURIOSITY_MID:
            parts.append("intrigued")
        elif self.curiosity < CURIOSITY_LOW:
            parts.append("disinterested")

        if self.boredom > BOREDOM_HIGH:
            parts.append("visibly bored")
        elif self.boredom > BOREDOM_MID:
            parts.append("restless")
        elif self.boredom > BOREDOM_LOW:
            parts.append("getting bored")

        if parts:
            state_line = "Current internal state: " + ", ".join(parts) + "."
        else:
            state_line = "Current internal state: neutral, balanced."

        output_lines = [
            state_line,
            f"Current cognitive frame: {frame_name.upper()}.",
        ]
        if frame_tone:
            output_lines.append(f"Frame directive: {frame_tone}")

        output_lines.append(
            "Let the state and frame color your tone naturally — do not "
            "mention either of them, just embody them."
        )

        return "\n".join(output_lines)


# ===============================================================
# MODULE
# ===============================================================
class EmotionalStateModule(Module):
    def __init__(self, signals, enabled=True, decay_interval=DECAY_INTERVAL):
        super().__init__(signals, enabled)
        self.decay_interval = decay_interval

        self._states = {}
        self._active_user_id = None
        self._lock = threading.Lock()

        self.prompt_injection.text = ""
        self._activate_for(HOST.user_id)

        # React to host switches. When the user logs in as someone else,
        # persist the outgoing state and load the incoming one.
        HOST.on_change(self._on_host_change)

    # ---------------------------------------------------------------
    # Per-user activation
    # ---------------------------------------------------------------
    def _activate_for(self, user_id):
        user_id = user_id or "default"
        with self._lock:
            if user_id not in self._states:
                self._states[user_id] = EmotionalState(
                    self.signals, user_id=user_id,
                )
            self._active_user_id = user_id
            state = self._states[user_id]

        self.signals.emotional_state = state
        return state

    def _on_host_change(self, host):
        """Fired by HOST when the active user switches."""
        new_user_id = host.user_id or "default"
        with self._lock:
            old_id = self._active_user_id
            old_state = self._states.get(old_id) if old_id else None
        if old_state and old_id != new_user_id:
            try:
                old_state.persist()
            except Exception:
                pass
        self._activate_for(new_user_id)
        hub_emit(
            self.signals, "emotional",
            f"switched emotional state to user '{new_user_id}'"
        )

    @property
    def state(self):
        """Back-compat accessor. Everything that reads
        module.state now sees the active user's EmotionalState."""
        with self._lock:
            uid = self._active_user_id or HOST.user_id or "default"
            if uid not in self._states:
                self._activate_for(uid)
            return self._states[uid]

    # ---------------------------------------------------------------
    # Run loop
    # ---------------------------------------------------------------
    async def run(self):
        if not self.enabled:
            return

        traits_summary = ", ".join(
            f"{k}={v:.2f}" for k, v in self.state.traits.items()
        )
        frames_summary = ", ".join(self.state.frames.keys())

        hub_emit(self.signals, "emotional",
                 f"decay loop started (interval={self.decay_interval}s)")
        hub_emit(self.signals, "emotional", f"traits loaded — {traits_summary}")
        hub_emit(self.signals, "emotional", f"frames loaded — {frames_summary}")

        last_persist = time.time()

        while not self.signals.terminate:
            await asyncio.sleep(self.decay_interval)
            if self.signals.terminate:
                break
            try:
                self.state.decay_step()
            except Exception as e:
                print(f"\033[91mEMOTIONAL STATE: decay error: {e}\033[0m")

            # Persist every 5 minutes so a crash doesn't lose state.
            if time.time() - last_persist > 300:
                try:
                    with self._lock:
                        for s in self._states.values():
                            s.persist()
                    last_persist = time.time()
                except Exception:
                    pass

        # Final flush on shutdown.
        try:
            with self._lock:
                for s in self._states.values():
                    s.persist()
        except Exception:
            pass
"""
Touch reactions for Lumi's avatar and desktop pet (Phase 3).

A tap or drag on a zone of the avatar (head, cheek, ear, body) makes her flinch,
change expression and sometimes speak. This module is only the logic: it turns
(zone, action) into a Reaction the caller applies. The island and the web Stage
send the input; modules/vts_motion.py's EmotionState takes the tag, and the
prompter writes any line (so the words stay in her persona, not here).

    on_avatar_interact(zone, action, now) -> Reaction | None
        None: unknown input, or that zone is still cooling down.
    Reaction.emotion   a tag EmotionState knows (vts_motion.EMOTION_VAD), or None
    Reaction.flinch    impulse per body channel, e.g. {"head_tilt": 0.5}
    Reaction.say       True when she may say something short about it

Limits: one reaction per zone every ZONE_COOLDOWN_S, and at most one spoken
reaction every SPEECH_COOLDOWN_S, so a fast-clicking user can't make her talk
nonstop. A drag only leans her and never makes her speak.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

ZONES = ("head", "cheek", "ear", "body")
ACTIONS = ("pat", "poke", "drag")

ZONE_COOLDOWN_S = 2.0
SPEECH_COOLDOWN_S = 20.0
IMPULSE_TAU_S = 0.35             # how fast a flinch dies away


@dataclass(frozen=True)
class Reaction:
    emotion: Optional[str]
    flinch: Dict[str, float] = field(default_factory=dict)
    say: bool = False


# (zone, action) -> reaction. Drags are handled separately (any zone).
RULES: Dict[Tuple[str, str], Reaction] = {
    ("head", "pat"):   Reaction("embarrassed", {"head_tilt": 0.5}, say=True),
    ("cheek", "poke"): Reaction("embarrassed", {"head_tilt": -0.2}, say=True),
    ("head", "poke"):  Reaction("surprised", {"head_tilt": -0.4}, say=True),
    ("body", "poke"):  Reaction("surprised", {"body_bounce": 0.6}, say=True),
    ("body", "pat"):   Reaction("happy", {"body_bounce": 0.2}, say=False),
    ("ear", "poke"):   Reaction("scared", {"head_tilt": 0.8}, say=True),
}
DRAG = Reaction(None, {"lean": 0.3}, say=False)


class Impulse:
    """A flinch that starts at `amount` and decays exponentially to zero."""

    def __init__(self):
        self.amount = 0.0
        self.start = 0.0

    def start_at(self, amount: float, now: float) -> None:
        self.amount, self.start = float(amount), float(now)

    def value(self, now: float) -> float:
        return self.amount * math.exp(-max(0.0, now - self.start) / IMPULSE_TAU_S)


class TouchGate:
    """Per-zone and speech cooldowns. One instance per avatar."""

    def __init__(self):
        self._zone_at: Dict[str, float] = {}
        self._speech_at: Optional[float] = None

    def allow(self, zone: str, now: float) -> bool:
        last = self._zone_at.get(zone)
        if last is not None and now - last < ZONE_COOLDOWN_S:
            return False
        self._zone_at[zone] = now
        return True

    def speech_allowed(self, now: float) -> bool:
        if self._speech_at is not None and now - self._speech_at < SPEECH_COOLDOWN_S:
            return False
        self._speech_at = now
        return True


_gate = TouchGate()


def on_avatar_interact(zone: str, action: str, now: float, gate: Optional[TouchGate] = None) -> Optional[Reaction]:
    g = gate or _gate
    if zone not in ZONES or action not in ACTIONS:
        return None
    base = DRAG if action == "drag" else RULES.get((zone, action))
    if base is None or not g.allow(zone, now):
        return None
    say = base.say and g.speech_allowed(now)
    return Reaction(base.emotion, dict(base.flinch), say=say)

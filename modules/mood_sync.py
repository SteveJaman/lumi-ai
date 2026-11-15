"""
Lumi's mood, live, to every open UI: the web app (any page, over socket.io) and every
Lumi Island (over the approval server's WebSocket), through ui_sync:

    mood_update  {"mood": {valence, arousal, frustration, curiosity, boredom, frame}}

The emotional state (modules/emotional_state.py) changes in many places (turns, game
events, decay), so instead of hooking each one this watches it: every POLL_S seconds it
compares the five numbers with what it last sent and broadcasts when any moved by
MIN_DELTA or the frame changed, and at least every KEEPALIVE_S so a UI that missed one
catches up. current() is the same snapshot for a UI that has just connected (GET
/api/ui/state, island "ui_state"). Cheap: five float reads every 2 s, no model, no I/O.

The Pipeline (web/pipeline.js) paces and colours its Lumis by it on every page.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional

from modules import ui_sync

KEYS = ("valence", "arousal", "frustration", "curiosity", "boredom")
POLL_S = 2.0
MIN_DELTA = 0.02
KEEPALIVE_S = 30.0

_signals = None


def current(signals=None) -> Optional[Dict[str, Any]]:
    """The active user's mood, rounded, or None when there's no emotional state yet."""
    st = getattr(signals or _signals, "emotional_state", None)
    if st is None:
        return None
    try:
        out: Dict[str, Any] = {k: round(float(getattr(st, k)), 3) for k in KEYS}
    except (TypeError, ValueError, AttributeError):
        return None
    out["frame"] = getattr(st, "current_frame", None)
    return out


def changed(old: Optional[Dict[str, Any]], new: Dict[str, Any]) -> bool:
    if old is None:
        return True
    if old.get("frame") != new.get("frame"):
        return True
    return any(abs((new.get(k) or 0) - (old.get(k) or 0)) >= MIN_DELTA for k in KEYS)


async def run(signals) -> None:
    """Watch the mood until Lumi shuts down. Started by the emotional state module."""
    global _signals
    _signals = signals
    last, last_t = None, 0.0
    while not getattr(signals, "terminate", False):
        await asyncio.sleep(POLL_S)
        snap = current(signals)
        if snap is None:
            continue
        now = time.time()
        if changed(last, snap) or now - last_t >= KEEPALIVE_S:
            ui_sync.broadcast("mood_update", {"mood": snap})
            last, last_t = snap, now

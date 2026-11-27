"""
When Lumi may speak without being spoken to (Phase 4).

    ProactiveGate.allowed(now, gaming, dnd)   True when an unprompted remark is OK
    ProactiveGate.fired(now)                  record that one happened
    EventThrottle.allowed(kind, now)          per-kind limit for event commentary

Rules: at most one idle remark every IDLE_INTERVAL_S (15 minutes by default), and
nothing at all while Gaming Mode or Do-Not-Disturb is on. Event commentary (a
game moment, a track change) has its own, shorter throttle per kind, so a busy
stream of events can't turn into a monologue.

Pure logic: the caller decides what to say and does the speaking.
"""

from __future__ import annotations

import time
from typing import Dict, Optional

IDLE_INTERVAL_S = 15 * 60
EVENT_INTERVAL_S = 90.0


class ProactiveGate:
    def __init__(self, interval_s: float = IDLE_INTERVAL_S):
        self.interval_s = float(interval_s)
        self._last: Optional[float] = None

    def allowed(self, now: Optional[float] = None, gaming: bool = False, dnd: bool = False) -> bool:
        if gaming or dnd:
            return False
        now = time.time() if now is None else now
        return self._last is None or now - self._last >= self.interval_s

    def fired(self, now: Optional[float] = None) -> None:
        self._last = time.time() if now is None else now


class EventThrottle:
    def __init__(self, interval_s: float = EVENT_INTERVAL_S):
        self.interval_s = float(interval_s)
        self._last: Dict[str, float] = {}

    def allowed(self, kind: str, now: Optional[float] = None, gaming: bool = False, dnd: bool = False) -> bool:
        if gaming or dnd:
            return False
        now = time.time() if now is None else now
        last = self._last.get(kind)
        if last is not None and now - last < self.interval_s:
            return False
        self._last[kind] = now
        return True

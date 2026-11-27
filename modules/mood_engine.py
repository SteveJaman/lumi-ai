"""
Lumi's mood: a few numbers that persist across restarts and bend how she talks (Phase 4).

    smugness, annoyance, curiosity, energy     0..100
    interruption_streak                         int, how many times you talked over her in a row

Stored in data/mood_state.json (atomic writes). Events move the numbers:

    barge_in        you talked over her: annoyance up, the streak goes up
    turn_finished   she finished a reply without being cut off: streak back to 0
    late_night      it's 1:00-4:59 and you're still here: energy down, smugness up
    touch_poke      you poked her avatar: annoyance up
    touch_pat       you patted her avatar: smugness up
    task_done       she finished a task: smugness and energy up

Between events the numbers drift back toward their resting values over time, so a
bad afternoon doesn't last until next week. Nothing here calls a model.
"""

from __future__ import annotations

import datetime
import json
import math
import os
import threading
import time
from typing import Dict, Optional

from constants import DATA_ROOT

PATH = os.path.join(DATA_ROOT, "mood_state.json")
RESTING = {"smugness": 40.0, "annoyance": 10.0, "curiosity": 50.0, "energy": 60.0}
DRIFT_TAU_H = 8.0            # hours for the gap to shrink to about 37% of what it was
EVENTS: Dict[str, Dict[str, float]] = {
    "barge_in":      {"annoyance": 6.0},
    "turn_finished": {},
    "late_night":    {"energy": -5.0, "smugness": 3.0},
    "touch_poke":    {"annoyance": 3.0},
    "touch_pat":     {"smugness": 2.0},
    "task_done":     {"smugness": 5.0, "energy": 2.0},
}
_lock = threading.Lock()


def _clamp(v: float) -> float:
    return max(0.0, min(100.0, float(v)))


class MoodEngine:
    def __init__(self, path: str = PATH, clock=time.time):
        self.path = path
        self.clock = clock
        self._lock = threading.Lock()
        self.state = {**RESTING, "interruption_streak": 0, "updated_at": clock()}
        self._load()

    # ---- persistence ----
    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        for k in RESTING:
            if k in data:
                self.state[k] = _clamp(data[k])
        self.state["interruption_streak"] = max(0, int(data.get("interruption_streak", 0) or 0))
        self.state["updated_at"] = float(data.get("updated_at", self.clock()))

    def save(self) -> None:
        with self._lock:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2)
            os.replace(tmp, self.path)

    # ---- behaviour ----
    def drift(self, now: Optional[float] = None) -> None:
        now = self.clock() if now is None else now
        hours = max(0.0, (now - self.state.get("updated_at", now)) / 3600.0)
        if hours <= 0:
            return
        pull = 1.0 - math.exp(-hours / DRIFT_TAU_H)
        for k, rest in RESTING.items():
            self.state[k] = _clamp(self.state[k] + (rest - self.state[k]) * pull)
        self.state["updated_at"] = now

    def event(self, name: str, now: Optional[float] = None) -> None:
        if name not in EVENTS:
            raise ValueError(f"unknown mood event {name!r}")
        now = self.clock() if now is None else now
        self.drift(now)
        if name == "turn_finished":
            self.state["interruption_streak"] = 0
        elif name == "barge_in":
            self.state["interruption_streak"] = int(self.state["interruption_streak"]) + 1
        for k, delta in EVENTS.get(name, {}).items():
            self.state[k] = _clamp(self.state[k] + delta)
        self.state["updated_at"] = now

    def late_night(self, now: Optional[float] = None) -> bool:
        """True (and records the event) when it's 1:00-4:59 local time."""
        now = self.clock() if now is None else now
        hour = datetime.datetime.fromtimestamp(now).hour
        if 1 <= hour <= 4:
            self.event("late_night", now)
            return True
        return False

    def snapshot(self) -> Dict[str, float]:
        return {k: (round(v, 1) if isinstance(v, float) else v) for k, v in self.state.items()}


_shared: Optional[MoodEngine] = None
_shared_lock = threading.Lock()


def shared() -> MoodEngine:
    """The one mood the whole app uses, loaded from data/mood_state.json on first use."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = MoodEngine()
        return _shared


def record(name: str) -> None:
    """Apply a mood event and save. Never raises: a mood write must not break a reply."""
    try:
        eng = shared()
        eng.event(name)
        eng.save()
    except Exception:
        pass

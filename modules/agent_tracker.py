"""
Agent tracker: what each of Lumi's agents is doing right now, for the Pipeline
view on Lumi Island.

    main       Lumi herself: chat and agent turns
    minecraft  her Minecraft "other half" (a separate process, via the SDK)
    office     desk work for you: calendar / email tools and Claude Code runs

Each agent has:
    status      idle | thinking | executing | waiting_approval | error
    task        what it's doing, in a few words
    progress    0-100
    model       the routed tier: haiku | sonnet | opus (or None)
    model_name  the model that actually runs it, e.g. "qwen3:8b" or
                "Claude Code (opus)", so the tier label never misleads

update(...) is thread-safe and calls every registered callback with a fresh
snapshot when something changed. The approval server registers one that pushes
{"type": "agent_update", "agents": {...}} to the island.
"""

from __future__ import annotations

import copy
import threading
import time
from typing import Any, Callable, Dict, List, Optional

AGENTS = ("main", "minecraft", "office")
STATUSES = ("idle", "thinking", "executing", "waiting_approval", "error")
MODELS = ("haiku", "sonnet", "opus")
MAX_TASK_CHARS = 140


class AgentTracker:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._callbacks: List[Callable[[dict], None]] = []
        self._agents: Dict[str, Dict[str, Any]] = {
            a: {"status": "idle", "task": "", "progress": 0, "model": None, "model_name": None,
                "updated": time.time()}
            for a in AGENTS
        }

    def register(self, fn: Callable[[dict], None]) -> None:
        """fn(snapshot) runs after every change, from whichever thread made it."""
        self._callbacks.append(fn)

    def unregister(self, fn: Callable[[dict], None]) -> None:
        try:
            self._callbacks.remove(fn)
        except ValueError:
            pass

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._agents)

    def get(self, agent_id: str) -> Dict[str, Any]:
        with self._lock:
            return dict(self._agents[agent_id])

    def update(self, agent_id: str, status: str, task: Optional[str] = None,
               progress: Optional[int] = None, model: Optional[str] = None,
               model_name: Optional[str] = None) -> bool:
        """Set an agent's state. Fields left as None keep their value, except
        that going idle clears the task and progress. Returns True if anything
        changed (and the callbacks ran)."""
        if agent_id not in AGENTS:
            raise ValueError(f"unknown agent {agent_id!r}; expected one of {AGENTS}")
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}; expected one of {STATUSES}")
        if model is not None and model not in MODELS:
            raise ValueError(f"unknown model {model!r}; expected one of {MODELS}")
        with self._lock:
            cur = self._agents[agent_id]
            new = dict(cur)
            new["status"] = status
            if status == "idle":
                new["task"] = "" if task is None else " ".join(str(task).split())[:MAX_TASK_CHARS]
                new["progress"] = 0 if progress is None else _clamp(progress)
            else:
                if task is not None:
                    new["task"] = " ".join(str(task).split())[:MAX_TASK_CHARS]
                if progress is not None:
                    new["progress"] = _clamp(progress)
            if model is not None:
                new["model"] = model
            if model_name is not None:
                new["model_name"] = str(model_name)[:60]
            if {k: v for k, v in new.items() if k != "updated"} == {k: v for k, v in cur.items() if k != "updated"}:
                return False
            new["updated"] = time.time()
            self._agents[agent_id] = new
            snap = copy.deepcopy(self._agents)
        for fn in list(self._callbacks):
            try:
                fn(snap)
            except Exception:
                pass
        return True

    def settle(self, agent_id: str) -> None:
        """End of a piece of work: back to idle, unless it ended in an error
        (that stays on show until the agent's next piece of work)."""
        with self._lock:
            status = self._agents[agent_id]["status"]
        if status != "error":
            self.update(agent_id, "idle")


def _clamp(value: Any) -> int:
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return 0


tracker = AgentTracker()


def update(agent_id, status, task=None, progress=None, model=None, model_name=None) -> bool:
    return tracker.update(agent_id, status, task, progress, model, model_name)


def snapshot():
    return tracker.snapshot()


def unregister(fn) -> None:
    tracker.unregister(fn)


def register(fn) -> None:
    tracker.register(fn)


def get(agent_id):
    return tracker.get(agent_id)


def settle(agent_id) -> None:
    tracker.settle(agent_id)

"""
Agent tracker: what each of Lumi's agents is doing right now, for the Pipeline
view on Lumi Island.

A modern agent team, one character each:

    lead       talks to you, plans and delegates: chat turns, agent-mode
               planning, the task list, subagents
    research   finds things out: web search, the browser, memory, the
               library, reading files, deals, product research, meetings
    build      makes and changes things: writing files, the terminal,
               Claude Code, MCP tools, workflows
    operate    uses your apps and PC: desktop windows, email & calendar,
               finance, Spotify, VTube Studio, and Minecraft (her game
               agent, a separate process via the SDK)

Older names still work (LEGACY): main -> lead, office -> build,
minecraft and system -> operate.

Each agent has:
    status      idle | thinking | executing | waiting_approval | error
    task        what it's doing, in a few words
    progress    0-100
    model       the routed tier: haiku | sonnet | opus (or None)
    model_name  the model that actually runs it, e.g. "qwen3:8b" or
                "Claude Code (opus)", so the tier label never misleads

For the pixel-art Pipeline (web/pipeline.js, island/src/pipeline.js) each
agent also reports where it works and how to draw it:
    station       desk | bookshelf | workbench | server_rack (fixed per agent)
    visual_state  derived from status, e.g. Lead thinking -> typing,
                  Research executing -> reading, Operate executing ->
                  blinking, any waiting_approval -> waving, any error -> alarm

update(...) is thread-safe and calls every registered callback with a fresh
snapshot when something changed. The approval server registers one that pushes
{"type": "agent_update", "agents": {...}} to the island.
"""

from __future__ import annotations

import copy
import threading
import time
from typing import Any, Callable, Dict, List, Optional

AGENTS = ("lead", "research", "build", "operate")
LEGACY = {"main": "lead", "office": "build", "minecraft": "operate", "system": "operate"}
STATUSES = ("idle", "thinking", "executing", "waiting_approval", "error")
MODELS = ("haiku", "sonnet", "opus")
MAX_TASK_CHARS = 140

STATIONS = {"lead": "desk", "research": "bookshelf", "build": "workbench", "operate": "server_rack"}

# status -> visual_state, per agent (what the pixel character is doing).
_VISUALS = {
    "lead":     {"idle": "sitting",  "thinking": "typing",   "executing": "typing"},
    "research": {"idle": "sitting",  "thinking": "reading",  "executing": "reading"},
    "build":    {"idle": "sitting",  "thinking": "typing",   "executing": "typing"},
    "operate":  {"idle": "standing", "thinking": "blinking", "executing": "blinking"},
}

# Built-in tools by who does them (everyday tools carry their own AGENT).
RESEARCH_TOOLS = frozenset({"web_search", "browse_url", "recall_memory", "search_library",
                            "read_file", "list_dir", "search_files"})
BUILD_TOOLS = frozenset({"write_file", "run_command", "run_in_terminal", "workflow_run"})
OPERATE_TOOLS = frozenset({"dispatch_game_command", "look_at_minecraft", "vtube_move", "vtube_set_expression"})


def agent_id_for(name: str) -> str:
    """An agent id, or an older name for one (main, office, ...), as today's id."""
    return LEGACY.get(name, name)


def agent_for_tool(name: str) -> str:
    """Which character shows a built-in tool running."""
    if name in RESEARCH_TOOLS:
        return "research"
    if name in BUILD_TOOLS or name.startswith("mcp__"):
        return "build"
    if name in OPERATE_TOOLS or name.startswith("sdk__"):
        return "operate"
    return "lead"


def visual_state(agent_id: str, status: str) -> str:
    agent_id = agent_id_for(agent_id)
    if status == "waiting_approval":
        return "waving"
    if status == "error":
        return "alarm"
    return _VISUALS.get(agent_id, {}).get(status, "standing")


class AgentTracker:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._callbacks: List[Callable[[dict], None]] = []
        self._agents: Dict[str, Dict[str, Any]] = {
            a: {"status": "idle", "task": "", "progress": 0, "model": None, "model_name": None,
                "station": STATIONS[a], "visual_state": visual_state(a, "idle"),
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
        agent_id = agent_id_for(agent_id)
        with self._lock:
            return dict(self._agents[agent_id])

    def update(self, agent_id: str, status: str, task: Optional[str] = None,
               progress: Optional[int] = None, model: Optional[str] = None,
               model_name: Optional[str] = None) -> bool:
        """Set an agent's state. Fields left as None keep their value, except
        that going idle clears the task and progress. Returns True if anything
        changed (and the callbacks ran)."""
        agent_id = agent_id_for(agent_id)
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
            new["visual_state"] = visual_state(agent_id, status)
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
        agent_id = agent_id_for(agent_id)
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

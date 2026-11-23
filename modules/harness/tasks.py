"""Persistent task DAG for the agent harness.

Tasks live in data/tasks/lumi_tasks.json so a run survives a crash or restart:
the loop always reads the current task from here, never from the conversation.
Every write validates the graph (no cycles, no unknown dependency ids) and is
atomic (temp file + os.replace) under an in-process lock.
"""

import json
import os
import threading
import time

from modules.harness import config

STATUSES = ("pending", "in_progress", "done", "failed", "blocked")
OPEN_STATUSES = ("pending", "in_progress")


class DAGError(ValueError):
    """A write that would leave the task graph invalid."""


def _as_id(value):
    if isinstance(value, bool):
        raise DAGError(f"invalid task id: {value!r}")
    if isinstance(value, int):
        return value
    s = str(value).strip().lstrip("#").lower()
    if s.startswith("t") and s[1:].isdigit():
        s = s[1:]
    if not s.isdigit():
        raise DAGError(f"invalid task id: {value!r}")
    return int(s)


def validate(tasks):
    """Raise DAGError if `tasks` has a bad status, unknown dependency or cycle."""
    by_id = {}
    for t in tasks:
        if t["id"] in by_id:
            raise DAGError(f"duplicate task id {t['id']}")
        if t.get("status") not in STATUSES:
            raise DAGError(f"task {t['id']} has invalid status {t.get('status')!r}")
        by_id[t["id"]] = t
    for t in tasks:
        for d in t.get("depends_on") or []:
            if d not in by_id:
                raise DAGError(f"task {t['id']} depends on unknown task {d}")
            if d == t["id"]:
                raise DAGError(f"task {t['id']} depends on itself")
    WHITE, GREY, BLACK = 0, 1, 2
    color = {i: WHITE for i in by_id}

    def visit(i, path):
        color[i] = GREY
        for d in by_id[i].get("depends_on") or []:
            if color[d] == GREY:
                raise DAGError("dependency cycle: " + " -> ".join(map(str, path + [d])))
            if color[d] == WHITE:
                visit(d, path + [d])
        color[i] = BLACK

    for i in by_id:
        if color[i] == WHITE:
            visit(i, [i])


class TaskStore:
    def __init__(self, path=None):
        self.path = path or os.path.join(config.TASKS_DIR, "lumi_tasks.json")
        self._lock = threading.RLock()

    # ---- persistence ----
    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return {"next_id": 1, "tasks": []}
        except (OSError, json.JSONDecodeError) as e:
            raise DAGError(f"task store unreadable: {e}")
        data.setdefault("tasks", [])
        data.setdefault("next_id", max([t["id"] for t in data["tasks"]] or [0]) + 1)
        return data

    def _save(self, data):
        validate(data["tasks"])
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    # ---- queries ----
    def list(self, status=None):
        with self._lock:
            tasks = self._load()["tasks"]
        return [t for t in tasks if status is None or t["status"] == status]

    def get(self, task_id):
        tid = _as_id(task_id)
        for t in self.list():
            if t["id"] == tid:
                return t
        return None

    def current(self):
        """The task in progress, if any."""
        for t in self.list():
            if t["status"] == "in_progress":
                return t
        return None

    def has_open(self):
        return any(t["status"] in OPEN_STATUSES for t in self.list())

    @staticmethod
    def _ready(t, by_id):
        return t["status"] == "pending" and all(
            by_id[d]["status"] == "done" for d in t.get("depends_on") or [])

    def peek_next(self):
        """The task task_next would return, without changing anything."""
        tasks = self.list()
        by_id = {t["id"]: t for t in tasks}
        for t in tasks:
            if t["status"] == "in_progress":
                return t
        for t in tasks:
            if self._ready(t, by_id):
                return t
        return None

    # ---- writes ----
    def add(self, description, depends_on=None, acceptance_criteria=""):
        description = str(description or "").strip()
        if not description:
            raise DAGError("a task needs a description")
        deps = [_as_id(d) for d in (depends_on or [])]
        with self._lock:
            data = self._load()
            task = {
                "id": data["next_id"], "description": description, "status": "pending",
                "depends_on": deps, "acceptance_criteria": str(acceptance_criteria or ""),
                "result": None, "attempts": 0, "last_error": None,
                "created_at": time.time(),
            }
            by_id = {t["id"]: t for t in data["tasks"]}
            if any(by_id.get(d, {}).get("status") in ("failed", "blocked") for d in deps):
                task["status"] = "blocked"
            data["tasks"].append(task)
            data["next_id"] += 1
            self._save(data)          # validates; raises before writing on a bad graph
            return task

    def next(self):
        """Mark the next unblocked pending task in_progress and return it (None when none)."""
        with self._lock:
            data = self._load()
            by_id = {t["id"]: t for t in data["tasks"]}
            for t in data["tasks"]:
                if t["status"] == "in_progress":
                    return t
            for t in data["tasks"]:
                if self._ready(t, by_id):
                    t["status"] = "in_progress"
                    t["attempts"] = int(t.get("attempts") or 0) + 1
                    t["started_at"] = time.time()
                    self._save(data)
                    return t
            return None

    def complete(self, task_id, result=""):
        tid = _as_id(task_id)
        with self._lock:
            data = self._load()
            t = next((x for x in data["tasks"] if x["id"] == tid), None)
            if t is None:
                raise DAGError(f"no task {tid}")
            if t["status"] in ("done", "failed"):
                raise DAGError(f"task {tid} is already {t['status']}")
            t["status"] = "done"
            t["result"] = str(result or "")
            t["finished_at"] = time.time()
            self._save(data)
            return t

    def fail(self, task_id, reason=""):
        tid = _as_id(task_id)
        with self._lock:
            data = self._load()
            t = next((x for x in data["tasks"] if x["id"] == tid), None)
            if t is None:
                raise DAGError(f"no task {tid}")
            if t["status"] in ("done", "failed"):
                raise DAGError(f"task {tid} is already {t['status']}")
            t["status"] = "failed"
            t["last_error"] = str(reason or "")
            t["finished_at"] = time.time()
            # Everything downstream of a failure can never run: block it.
            changed = True
            dead = {tid}
            while changed:
                changed = False
                for x in data["tasks"]:
                    if x["status"] == "pending" and dead & set(x.get("depends_on") or []):
                        x["status"] = "blocked"
                        x["last_error"] = f"dependency {sorted(dead & set(x['depends_on']))} failed"
                        dead.add(x["id"])
                        changed = True
            self._save(data)
            return t

    def clear(self):
        with self._lock:
            self._save({"next_id": 1, "tasks": []})


# ===============================================================
# Agent tools
# ===============================================================
def _schema(name, description, properties=None, required=None):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties or {},
                       "required": required or []}}}


TASK_TOOL_SCHEMAS = [
    _schema("task_list", "List every task in the persistent task store with its status.",
            {"status": {"type": "string", "enum": list(STATUSES),
                        "description": "Only show tasks with this status."}}),
    _schema("task_add", "Add a task to the persistent task DAG; it runs after the tasks it depends on are done.",
            {"description": {"type": "string", "description": "What to do, as one concrete action."},
             "depends_on": {"type": "array", "items": {"type": "integer"},
                            "description": "Ids of tasks that must be done first."},
             "acceptance_criteria": {"type": "string", "description": "How to tell the task is done."}},
            ["description"]),
    _schema("task_next", "Return the task to work on now (the one in progress, or the next unblocked one); call it before every decision."),
    _schema("task_complete", "Mark a task done and record its result as evidence.",
            {"id": {"type": "integer", "description": "Task id."},
             "result": {"type": "string", "description": "What was done and how you know it worked."}},
            ["id", "result"]),
    _schema("task_fail", "Mark a task failed with the reason; tasks depending on it become blocked.",
            {"id": {"type": "integer", "description": "Task id."},
             "reason": {"type": "string", "description": "The error or why it cannot be done."}},
            ["id", "reason"]),
]
TASK_TOOL_NAMES = frozenset(s["function"]["name"] for s in TASK_TOOL_SCHEMAS)
TASK_MUTATING_TOOLS = frozenset({"task_add", "task_complete", "task_fail"})


def _brief(t):
    keys = ("id", "description", "status", "depends_on", "acceptance_criteria",
            "result", "attempts", "last_error")
    return {k: t.get(k) for k in keys}


def execute_task_tool(store, name, args):
    """Run one task tool and return its JSON result text. Errors come back as
    'Error: ...' strings so the model can correct itself."""
    args = args or {}
    try:
        if name == "task_list":
            status = args.get("status") or None
            return json.dumps([_brief(t) for t in store.list(status)], ensure_ascii=False)
        if name == "task_add":
            deps = args.get("depends_on") or []
            if isinstance(deps, (str, int)):
                deps = [deps]
            t = store.add(args.get("description"), deps, args.get("acceptance_criteria", ""))
            return json.dumps({"added": _brief(t)}, ensure_ascii=False)
        if name == "task_next":
            t = store.next()
            if t is None:
                return json.dumps({"task": None, "note": "No unblocked task left. If the goal is met, you are done."})
            return json.dumps({"task": _brief(t)}, ensure_ascii=False)
        if name == "task_complete":
            t = store.complete(args.get("id"), args.get("result", ""))
            return json.dumps({"completed": _brief(t)}, ensure_ascii=False)
        if name == "task_fail":
            t = store.fail(args.get("id"), args.get("reason", ""))
            return json.dumps({"failed": _brief(t)}, ensure_ascii=False)
    except DAGError as e:
        return f"Error: {e}"
    return f"Error: unknown task tool {name}"

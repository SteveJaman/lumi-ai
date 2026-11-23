"""Hook registry for the agent harness: PreToolUse, PostToolUse, PostToolUseFailure.

Hooks are plain Python files in data/hooks/*.py, loaded by filename (files
starting with "_" are skipped). A hook file defines any of:

    def pre_tool_use(event): ...           # return {"deny": True, "reason": "..."} to block
    def post_tool_use(event): ...          # return {"message": "..."} to tell the model something
    def post_tool_use_failure(event): ...  # same

event = {"event", "tool", "args", "session_id", "result"?, "error"?}

Hooks run in the harness, outside the model's context, so they cost no tokens.
A denial blocks the call even in YOLO mode. A hook that raises is logged and
ignored (fail open for post hooks), except PreToolUse, which fails closed.
"""

import importlib.util
import json
import os
import sys
import traceback

from modules.harness import config

PRE_TOOL_USE = "PreToolUse"
POST_TOOL_USE = "PostToolUse"
POST_TOOL_USE_FAILURE = "PostToolUseFailure"
EVENTS = (PRE_TOOL_USE, POST_TOOL_USE, POST_TOOL_USE_FAILURE)
_FN_FOR_EVENT = {
    PRE_TOOL_USE: "pre_tool_use",
    POST_TOOL_USE: "post_tool_use",
    POST_TOOL_USE_FAILURE: "post_tool_use_failure",
}
FAILURE_LIMIT = 3


def args_key(tool, args):
    try:
        a = json.dumps(args or {}, sort_keys=True, default=str)
    except (TypeError, ValueError):
        a = str(args)
    return f"{tool}:{a}"


class HookRegistry:
    def __init__(self, hooks_dir=None, autoload=True):
        self.hooks_dir = hooks_dir or config.HOOKS_DIR
        self._hooks = {e: [] for e in EVENTS}       # event -> [(name, fn)]
        self.failures = {}                           # args_key -> count
        self.load_errors = []
        if autoload:
            self.load()

    # ---- registration ----
    def register(self, event, fn, name=None):
        if event not in EVENTS:
            raise ValueError(f"unknown hook event {event}")
        self._hooks[event].append((name or getattr(fn, "__name__", "hook"), fn))

    def load(self):
        """(Re)load every data/hooks/*.py file."""
        self._hooks = {e: [] for e in EVENTS}
        self.load_errors = []
        if not os.path.isdir(self.hooks_dir):
            return
        for fname in sorted(os.listdir(self.hooks_dir)):
            if not fname.endswith(".py") or fname.startswith("_"):
                continue
            path = os.path.join(self.hooks_dir, fname)
            modname = f"lumi_hook_{fname[:-3]}"
            try:
                spec = importlib.util.spec_from_file_location(modname, path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
            except Exception as e:  # a broken hook file must not stop the agent
                self.load_errors.append(f"{fname}: {e}")
                print(f"[harness] hook {fname} failed to load: {e}", file=sys.stderr)
                continue
            for event, fn_name in _FN_FOR_EVENT.items():
                fn = getattr(mod, fn_name, None)
                if callable(fn):
                    self.register(event, fn, name=f"{fname}:{fn_name}")

    def names(self, event):
        return [n for n, _ in self._hooks[event]]

    # ---- dispatch ----
    def pre_tool_use(self, tool, args, session_id=""):
        """Return (allowed, reason). The first denial wins."""
        event = {"event": PRE_TOOL_USE, "tool": tool, "args": args or {}, "session_id": session_id}
        for name, fn in self._hooks[PRE_TOOL_USE]:
            try:
                out = fn(dict(event))
            except Exception as e:
                traceback.print_exc()
                return False, f"PreToolUse hook {name} crashed ({e}); call blocked."
            if isinstance(out, dict) and out.get("deny"):
                return False, str(out.get("reason") or f"Denied by hook {name}.")
        return True, ""

    def _post(self, event_name, event):
        messages = []
        for name, fn in self._hooks[event_name]:
            try:
                out = fn(dict(event))
            except Exception:
                traceback.print_exc()
                continue
            if isinstance(out, dict) and out.get("message"):
                messages.append(str(out["message"]))
        return messages

    def post_tool_use(self, tool, args, result, session_id=""):
        """Run PostToolUse hooks; returns messages to show the model."""
        return self._post(POST_TOOL_USE, {"event": POST_TOOL_USE, "tool": tool, "args": args or {},
                                          "result": result, "session_id": session_id})

    def post_tool_use_failure(self, tool, args, error, session_id=""):
        """Count the failure per tool+args and run PostToolUseFailure hooks.
        Returns messages to show the model (the 3x-failure notice included)."""
        key = args_key(tool, args)
        self.failures[key] = self.failures.get(key, 0) + 1
        count = self.failures[key]
        messages = self._post(POST_TOOL_USE_FAILURE, {
            "event": POST_TOOL_USE_FAILURE, "tool": tool, "args": args or {},
            "error": error, "session_id": session_id, "failure_count": count})
        if count >= FAILURE_LIMIT:
            try:
                shown = json.dumps(args or {}, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                shown = str(args)
            if len(shown) > 300:
                shown = shown[:300] + "…"
            messages.append(f"Tool {tool} failed {count}× with these args: {shown}; "
                            f"try a different approach (or call task_fail with the error).")
        return messages

    def failure_count(self, tool, args):
        return self.failures.get(args_key(tool, args), 0)

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
# Loop breaker: a local model retries a refused call word for word. After the same
# tool+args (or, for a command off the allowlist, the same program) fails this many
# times, the model is told to stop, and any further identical call is refused
# before it runs.
FAILURE_LIMIT = 2
SHELL_TOOLS = ("run_command", "run_in_terminal")
_ALLOWLIST_REFUSAL = "not on the command allowlist"
STOP_MESSAGE = ("STOP TRYING THIS COMMAND. {tool} failed {count}x with exactly these arguments: {args}. "
                "It is blocked by system policy or it doesn't work here. You must use a different tool "
                "or tell the user you cannot do this (task_fail with the reason).")


def _shown(args):
    try:
        shown = json.dumps(args or {}, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        shown = str(args)
    return shown[:300] + "…" if len(shown) > 300 else shown


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
    def _program_key(self, tool, args):
        """For shell tools: one key per program, so 'ipconfig' and 'ipconfig /all'
        count as the same refused command."""
        if tool not in SHELL_TOOLS:
            return None
        cmd = str((args or {}).get("command") or "").strip()
        first = cmd.split()[0].lower() if cmd else ""
        return f"{tool}::program::{first}" if first else None

    def blocked_reason(self, tool, args):
        """The stop message when this exact call already failed FAILURE_LIMIT times."""
        count = self.failures.get(args_key(tool, args), 0)
        pkey = self._program_key(tool, args)
        count = max(count, self.failures.get(pkey, 0) if pkey else 0)
        if count < FAILURE_LIMIT:
            return None
        return STOP_MESSAGE.format(tool=tool, count=count, args=_shown(args))

    def pre_tool_use(self, tool, args, session_id=""):
        """Return (allowed, reason). The first denial wins. A call that already
        failed FAILURE_LIMIT times with the same args is refused without running."""
        repeat = self.blocked_reason(tool, args)
        if repeat:
            return False, repeat
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
        pkey = self._program_key(tool, args)
        if pkey and _ALLOWLIST_REFUSAL in str(error):         # the program itself is refused
            self.failures[pkey] = self.failures.get(pkey, 0) + 1
            count = max(count, self.failures[pkey])
        messages = self._post(POST_TOOL_USE_FAILURE, {
            "event": POST_TOOL_USE_FAILURE, "tool": tool, "args": args or {},
            "error": error, "session_id": session_id, "failure_count": count})
        if count >= FAILURE_LIMIT:
            messages.append(STOP_MESSAGE.format(tool=tool, count=count, args=_shown(args)))
        return messages

    def failure_count(self, tool, args):
        return self.failures.get(args_key(tool, args), 0)

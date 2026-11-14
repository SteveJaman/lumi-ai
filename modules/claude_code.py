"""
Claude Code as a tool for Lumi: she hands a coding task to your installed
Claude Code CLI (`claude -p`, headless) and gets the answer back.

  * It runs on YOUR Claude login (your subscription), not API billing. The
    child process has ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN removed, so a
    key sitting in Lumi's .env can never silently switch it to pay-per-token.
    Lumi never sees or copies your login; she just launches the official CLI.
  * Every call needs your approval (the prompter routes it through the HITL
    flow with the full task shown). Nothing here approves itself.
  * Two modes. "read_only" (default): Read, Grep, Glob. "edit": adds Edit and
    Write. There is no shell and no web access in either: `--tools` removes
    those tools entirely, it doesn't merely ask nicely.
  * Whatever Lumi's own file tools refuse (her code, .env, credentials, her
    memories) Claude Code is denied too, in both modes, so the protections
    can't be walked around by delegating. To change Lumi's own code, use
    Claude Code yourself in your editor, as you do today.
  * The task goes in on stdin and the executable is started directly (never
    through cmd.exe), so nothing in the text can be read as a shell command.
  * One run at a time, a time limit, a turn limit, an hourly cap, and a size
    cap on what comes back.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional

import constants
from constants import WORKSPACE_ROOT
from modules import agent_tools

TOOL_NAME = "ask_claude_code"
TIMEOUT_S = 600
MAX_TURNS = 15
MAX_RUNS_PER_HOUR = 8
MAX_TASK_CHARS = 3000      # the approval prompt shows at most ~4000 characters; the whole task must fit
MAX_OUTPUT_CHARS = 6000

READ_TOOLS = ["Read", "Grep", "Glob"]
EDIT_TOOLS = READ_TOOLS + ["Edit", "Write"]

# Never readable by Claude Code, in either mode.
_NO_READ = [".env", ".env.*", "data/credentials/**", "data/workspace/**", "data/memories/**", "data/API/**"]

_SYSTEM_NOTE = (
    "You are being run by Lumi, a local AI companion, on the user's behalf. Do the task and "
    "reply with a short, plain summary of what you found or changed (file names and the key "
    "points). You have no shell or web access. Some paths are off limits by design; if a "
    "task needs one, say so instead of working around it."
)

TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": (
            "Hand a coding task to Claude Code (a much stronger coding model than you) and get its "
            "answer. Use it for real programming work: writing or fixing a script, reviewing or "
            "explaining code, planning a change. Not for chat, and not for things your other tools "
            "already do. It runs inside the project folder with file tools only (no shell, no web). "
            "The user must approve every call, so write a clear, complete task."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "What to do, with all the context it needs (it can't see this chat)."},
                "mode": {"type": "string", "enum": ["read_only", "edit"],
                         "description": "read_only (default): look at files and answer. edit: may also create and change files."},
            },
            "required": ["task"],
        },
    },
}


class ClaudeCodeError(Exception):
    """Something worth showing to the user as-is."""


_lock = threading.Lock()
_runs: deque = deque()


def find_cli() -> Optional[str]:
    """Path to the real claude.exe, or None. A `.cmd` shim is never used: the
    shim hands arguments to cmd.exe, where special characters can be misread."""
    exe = shutil.which("claude")
    if not exe:
        return None
    low = exe.lower()
    if low.endswith(".exe"):
        return exe
    if low.endswith((".cmd", ".bat", ".ps1")) or os.path.splitext(exe)[1] == "":
        base = os.path.dirname(exe)
        for rel in (("node_modules", "@anthropic-ai", "claude-code", "bin", "claude.exe"),
                    ("node_modules", "@anthropic-ai", "claude-code", "bin", "claude")):
            cand = os.path.join(base, *rel)
            if os.path.isfile(cand):
                return cand
    return None


def available() -> bool:
    """Offered to the model only when enabled and installed."""
    return bool(getattr(constants, "CLAUDE_CODE_ENABLED", False)) and find_cli() is not None


def _deny_lists() -> Dict[str, List[str]]:
    """Path rules derived from Lumi's own protections, so the two never drift."""
    protected = [p.rstrip("/") + "/**" for p in agent_tools.PROTECTED_PREFIXES] + sorted(agent_tools.PROTECTED_FILES)
    edit = list(dict.fromkeys(_NO_READ + protected))
    return {"read": [f"Read(./{p})" for p in _NO_READ],
            "edit": [f"Edit(./{p})" for p in edit]}


def build_command(exe: str, mode: str) -> List[str]:
    tools = EDIT_TOOLS if mode == "edit" else READ_TOOLS
    deny = _deny_lists()["read"] + (_deny_lists()["edit"] if mode == "edit" else [])
    cmd = [exe, "-p", "--output-format", "json",
           "--tools", ",".join(tools),
           "--disallowedTools", *deny,
           "--max-turns", str(MAX_TURNS),
           "--no-session-persistence",
           "--append-system-prompt", _SYSTEM_NOTE]
    if mode == "edit":
        # Headless runs have nobody to ask, so edits would all be refused. Accepting
        # edits is safe here only because the deny rules above still win over it.
        cmd += ["--permission-mode", "acceptEdits"]
    return cmd


def child_env() -> Dict[str, str]:
    """The environment Claude Code runs with: your login, never a stray API
    key, and detached from any Claude Code session Lumi itself was started in."""
    env = dict(os.environ)
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                 "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT"):
        env.pop(name, None)
    return env


def preview(task: str, mode: str) -> Dict[str, Any]:
    """What the approval prompt shows: the whole task, not a summary."""
    return {"action": "Ask Claude Code" + (" (may edit files)" if mode == "edit" else " (read-only)"),
            "folder": WORKSPACE_ROOT, "mode": mode, "task": task}


def run(task: Any, mode: str = "read_only", timeout: int = TIMEOUT_S) -> str:
    """Blocking. Returns Claude Code's answer, or raises ClaudeCodeError."""
    task = str(task or "").strip()
    if not task:
        raise ClaudeCodeError("There's no task to hand over.")
    if len(task) > MAX_TASK_CHARS:
        raise ClaudeCodeError(f"The task is too long ({len(task)} characters; the limit is {MAX_TASK_CHARS}).")
    if mode not in ("read_only", "edit"):
        mode = "read_only"
    exe = find_cli()
    if exe is None:
        raise ClaudeCodeError("Claude Code isn't installed (npm install -g @anthropic-ai/claude-code, then sign in once).")

    now = time.time()
    with _lock:
        while _runs and now - _runs[0] > 3600:
            _runs.popleft()
        if len(_runs) >= MAX_RUNS_PER_HOUR:
            raise ClaudeCodeError(f"Claude Code has already been used {MAX_RUNS_PER_HOUR} times this hour "
                                  "(these runs count against your Claude plan). Try again later.")
        busy = not _running.acquire(blocking=False)
        if busy:
            raise ClaudeCodeError("Claude Code is already working on something. One task at a time.")
        _runs.append(now)
    try:
        return _execute(exe, task, mode, timeout)
    finally:
        _running.release()


_running = threading.Lock()


def _execute(exe: str, task: str, mode: str, timeout: int) -> str:
    kw: Dict[str, Any] = {}
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        proc = subprocess.Popen(build_command(exe, mode), cwd=WORKSPACE_ROOT, env=child_env(),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace", **kw)
    except OSError as e:
        raise ClaudeCodeError(f"Couldn't start Claude Code ({type(e).__name__}).")
    try:
        out, err = proc.communicate(task, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        raise ClaudeCodeError(f"Claude Code took longer than {timeout // 60} minutes, so I stopped it.")
    return _parse(proc.returncode, out, err)


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=10,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            proc.kill()
    except Exception:
        pass
    try:
        proc.communicate(timeout=5)
    except Exception:
        pass


def _parse(code: int, out: str, err: str) -> str:
    text = (out or "").strip()
    data: Optional[dict] = None
    try:
        data = json.loads(text) if text else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        tail = " ".join((err or text or "no output").split())[:240]
        raise ClaudeCodeError(f"Claude Code didn't return a usable answer (exit {code}): {tail}")
    result = str(data.get("result") or "").strip()
    sub = str(data.get("subtype") or "")
    if data.get("is_error") or sub.startswith("error"):
        if "max_turns" in sub:
            raise ClaudeCodeError(f"Claude Code ran out of steps ({MAX_TURNS}) before finishing. "
                                  "Try a smaller task." + (f" Partial: {result[:300]}" if result else ""))
        low = result.lower()
        if "login" in low or "authenticate" in low or "api key" in low:
            raise ClaudeCodeError("Claude Code isn't signed in. Run `claude` once in a terminal and log in with your account.")
        raise ClaudeCodeError(f"Claude Code reported a problem: {result[:300] or sub or 'unknown'}")
    if not result:
        raise ClaudeCodeError("Claude Code finished but said nothing.")
    if len(result) > MAX_OUTPUT_CHARS:
        result = result[:MAX_OUTPUT_CHARS].rstrip() + "\n… (cut off)"
    turns = data.get("num_turns")
    return result + (f"\n\n(Claude Code, {turns} step{'s' if turns != 1 else ''})" if turns else "")

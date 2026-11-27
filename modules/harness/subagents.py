"""Subagent delegation for the agent harness.

spawn_subagent(role, prompt, tools_allowed, timeout) runs a fresh, isolated
conversation (its own history and tool allowlist) and returns only the final
response, so the main agent's context grows by one tool message, not by the
subagent's whole transcript.

Roles:
    Explore  read_file, search_files, list_dir     -> file map + snippets
    Plan     read_file, search_files, task_add, task_list -> task DAG as JSON
    Verify   read_file, run_command (read-only)    -> {"pass": bool, "evidence": str}

Every invocation is logged to data/tasks/subagents.log.
"""

import asyncio
import hashlib
import inspect
import json
import os
import re
import time

from modules.harness import config
from modules.harness.context import estimate_tokens, truncate_text
from modules.harness.tasks import TASK_TOOL_NAMES, execute_task_tool

DEFAULT_TIMEOUT_S = 300
MAX_SUBAGENT_STEPS = 25

ROLES = {
    "Explore": {
        "tools": ["read_file", "search_files", "list_dir"],
        "system": (
            "You are an Explore subagent. Use read_file, search_files and list_dir to find the "
            "files and code relevant to the request. Do not change anything. When you know enough, "
            "reply WITHOUT a tool call with a file map: one line per relevant file "
            "(path: why it matters) and a few short code snippets with line hints."),
    },
    "Plan": {
        "tools": ["read_file", "search_files", "task_add", "task_list"],
        "system": (
            "You are a Plan subagent. Read what you need, then break the goal into small, concrete "
            "tasks with task_add (one action each, with depends_on and acceptance_criteria). "
            "When done, reply WITHOUT a tool call with the task DAG as JSON: "
            '{"tasks": [{"id": 1, "description": "...", "depends_on": []}]}.'),
    },
    "Verify": {
        "tools": ["read_file", "run_command"],
        "system": (
            "You are a Verify subagent. Check whether the stated goal or acceptance criteria are "
            "met, using read_file and read-only run_command (tests, git status/diff, listing files). "
            "Never modify anything. Reply WITHOUT a tool call with JSON only: "
            '{"pass": true or false, "evidence": "what you ran or read and what it showed"}.'),
    },
}

# Read-only commands Verify may run. Each segment of a compound command must match.
_READ_ONLY_CMD = re.compile(
    r"^\s*(git\s+(status|diff|log|show|branch|rev-parse|ls-files)\b|"
    r"(python|py|python3)(\.exe)?\s+-m\s+(pytest|pyflakes|py_compile|compileall)\b|pytest\b|"
    r"ls\b|dir\b|type\b|cat\b|head\b|tail\b|wc\b|findstr\b|grep\b|rg\b|find\s|where\b|which\b|"
    r"echo\b|pip\s+(list|show|freeze)\b|node\s+--version|npm\s+(ls|test)\b|"
    r"get-childitem\b|get-content\b|select-string\b|test-path\b)",
    re.I)


def command_is_read_only(command):
    """True when every segment of `command` is an allowlisted read-only command."""
    cmd = str(command or "")
    if re.search(r"(?<![<>=-])>(?!=)|\btee\b|`|\$\(", cmd):
        return False
    segments = [s for s in re.split(r"&&|\|\||;|\|", cmd) if s.strip()]
    return bool(segments) and all(_READ_ONLY_CMD.match(s) for s in segments)


SPAWN_SUBAGENT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "spawn_subagent",
        "description": ("Delegate a focused job to a fresh subagent (Explore = find files, Plan = add tasks, "
                        "Verify = check the work) and get back only its final answer."),
        "parameters": {
            "type": "object",
            "properties": {
                "role": {"type": "string", "enum": list(ROLES), "description": "Which subagent to run."},
                "prompt": {"type": "string", "description": "The job, with every detail the subagent needs."},
                "tools_allowed": {"type": "array", "items": {"type": "string"},
                                  "description": "Optional narrower tool list (subset of the role's tools)."},
                "timeout": {"type": "integer", "description": "Seconds before the subagent is stopped (default 300)."},
            },
            "required": ["role", "prompt"],
        },
    },
}


async def _maybe_await(v):
    return await v if inspect.isawaitable(v) else v


def _extract_json(text):
    text = (text or "").strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


class SubagentRunner:
    def __init__(self, chat_fn, dispatch_fn, tool_schemas, store, hooks=None, output_store=None,
                 tasks_dir=None, session_id="", max_steps=MAX_SUBAGENT_STEPS, failure_fn=None,
                 recover_fn=None, plan_chat_fn=None):
        self.chat_fn = chat_fn
        self.plan_chat_fn = plan_chat_fn      # Plan only: the deep tier when Ollama has it (model_tiers)
        self.dispatch_fn = dispatch_fn
        self.schemas = {s["function"]["name"]: s for s in tool_schemas}
        self.store = store
        self.hooks = hooks
        self.output_store = output_store
        self.tasks_dir = tasks_dir or config.TASKS_DIR
        self.session_id = session_id
        self.max_steps = max_steps
        self.failure_fn = failure_fn or (lambda r: str(r).lstrip().lower().startswith(("error", "denied", "refused")))
        self.recover_fn = recover_fn      # text-form tool calls -> tool-call dicts (local models)
        self.invocations = 0

    def _log(self, entry):
        os.makedirs(self.tasks_dir, exist_ok=True)
        with open(os.path.join(self.tasks_dir, "subagents.log"), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    async def _exec(self, role, name, args):
        if self.hooks is not None:
            allowed, reason = self.hooks.pre_tool_use(name, args, self.session_id)
            if not allowed:
                return f"Denied: {reason}"
        if role == "Verify" and name == "run_command" and not command_is_read_only(args.get("command")):
            return ("Denied: Verify may only run read-only commands (tests, git status/diff/log, "
                    "listing or reading files).")
        try:
            if name in TASK_TOOL_NAMES:
                result = execute_task_tool(self.store, name, args)
            else:
                result = await _maybe_await(self.dispatch_fn(name, args))
        except Exception as e:
            result = f"Error: {type(e).__name__}: {e}"
        if self.hooks is not None:
            if self.failure_fn(result):
                self.hooks.post_tool_use_failure(name, args, str(result), self.session_id)
            else:
                self.hooks.post_tool_use(name, args, result, self.session_id)
        if self.output_store is not None:
            text, _ = self.output_store.truncate(name, str(result))
            return text
        return truncate_text(result)

    async def _loop(self, role, prompt, tools):
        messages = [{"role": "system", "content": ROLES[role]["system"]},
                    {"role": "user", "content": prompt}]
        schemas = [self.schemas[n] for n in tools if n in self.schemas]
        tokens = 0
        text_turns = 0
        chat = self.plan_chat_fn if (role == "Plan" and self.plan_chat_fn) else self.chat_fn
        for _ in range(self.max_steps):
            tokens += estimate_tokens(messages)
            resp = await _maybe_await(chat(messages, schemas))
            calls = []
            for tc in getattr(resp, "tool_calls", None) or []:
                calls.append(tc.as_ollama() if hasattr(tc, "as_ollama") else tc)
            content = (getattr(resp, "content", "") or "").strip()
            tokens += len(content) // 4
            if not calls and content and self.recover_fn:
                try:
                    recovered = self.recover_fn(content) or []
                except Exception:
                    recovered = []
                if recovered:
                    calls, content = recovered, ""
            if not calls:
                if content or text_turns:
                    return content, tokens, messages
                text_turns += 1
                messages.append({"role": "assistant", "content": ""})
                messages.append({"role": "user", "content": "Reply with your final answer now."})
                continue
            messages.append({"role": "assistant", "content": content, "tool_calls": calls})
            for call in calls:
                fn = call.get("function", {}) or {}
                name = fn.get("name") or ""
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                if name not in tools:
                    result = f"Denied: {name} is not available to the {role} subagent (allowed: {', '.join(tools)})."
                else:
                    result = await self._exec(role, name, args)
                messages.append({"role": "tool", "content": str(result), "name": name})
        return "", tokens, messages

    def _shape(self, role, text):
        if role == "Verify":
            obj = _extract_json(text)
            if isinstance(obj, dict) and "pass" in obj:
                return json.dumps({"pass": bool(obj["pass"]), "evidence": str(obj.get("evidence", ""))})
            return json.dumps({"pass": False, "evidence": (text or "Verify gave no verdict.")[:2000]})
        if role == "Plan":
            obj = _extract_json(text)
            if isinstance(obj, dict) and "tasks" in obj:
                return json.dumps(obj, ensure_ascii=False)
            tasks = [{"id": t["id"], "description": t["description"], "depends_on": t.get("depends_on") or [],
                      "status": t["status"]} for t in self.store.list()]
            return json.dumps({"tasks": tasks, "note": (text or "")[:500]}, ensure_ascii=False)
        return text or "(the subagent returned nothing)"

    async def spawn(self, role, prompt, tools_allowed=None, timeout=DEFAULT_TIMEOUT_S):
        """Run one subagent and return only its final answer (a string)."""
        role = str(role or "").strip().capitalize()
        if role not in ROLES:
            return f"Error: unknown subagent role {role!r}; use one of {', '.join(ROLES)}."
        prompt = str(prompt or "").strip()
        if not prompt:
            return "Error: the subagent needs a prompt."
        tools = list(ROLES[role]["tools"])
        if tools_allowed:
            tools = [t for t in tools if t in set(tools_allowed)]
        try:
            timeout = float(timeout or DEFAULT_TIMEOUT_S)
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT_S
        self.invocations += 1
        started = time.monotonic()
        status, tokens, steps = "ok", 0, 0
        try:
            text, tokens, msgs = await asyncio.wait_for(self._loop(role, prompt, tools), timeout)
            steps = sum(1 for m in msgs if m.get("role") == "assistant")
            out = self._shape(role, text)
        except asyncio.TimeoutError:
            status = "timeout"
            out = (json.dumps({"pass": False, "evidence": f"Verify timed out after {timeout:.0f}s"})
                   if role == "Verify" else f"Error: the {role} subagent timed out after {timeout:.0f}s.")
        except Exception as e:
            status = "error"
            out = f"Error: the {role} subagent failed: {type(e).__name__}: {e}"
        self._log({"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "session_id": self.session_id,
                   "role": role, "prompt_hash": hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16],
                   "tools": tools, "duration_s": round(time.monotonic() - started, 2),
                   "tokens": tokens, "steps": steps, "status": status})
        return out

    async def tool(self, args):
        """spawn_subagent tool entry point."""
        return await self.spawn(args.get("role"), args.get("prompt"),
                                args.get("tools_allowed"), args.get("timeout") or DEFAULT_TIMEOUT_S)

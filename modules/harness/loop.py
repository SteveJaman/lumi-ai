"""Goal-driven agent loop for Lumi's agent mode.

Replaces the fixed 6-step loop. The loop runs until one of these, checked every cycle:

    task_complete  task_next is empty after a task_complete / task_fail
    task_failed    same, but the last resolution was a task_fail
    turn_complete  a text-only turn with no open task (a plain answer)
    model_stalled  two consecutive text-only turns while a task is open
    stagnant       no mutating tool call in STAGNATION_CYCLES consecutive cycles
    safety_cap     max_safety_steps model calls (default 200)
    time_budget    wall-clock budget exceeded (default 2 h)
    error          the model request itself failed

The current task is read from the TaskStore every cycle and shown to the model in
an ephemeral message that is never stored in history. Every tool call passes the
PreToolUse hooks first (a denial blocks it, even in YOLO mode); results run the
PostToolUse / PostToolUseFailure hooks, whose messages reach the next model turn.
Tool results are truncated before they enter history (full output saved to disk),
and history is compacted (archive first) at 80% of the context window.
"""

import asyncio
import inspect
import json
import os
import time
import uuid
from dataclasses import dataclass, field

from modules.harness import config, workflows
from modules.harness.context import Compactor, ToolOutputStore, rules_message
from modules.harness.hooks import HookRegistry
from modules.harness.subagents import SPAWN_SUBAGENT_SCHEMA
from modules.harness.tasks import (
    TaskStore, TASK_TOOL_SCHEMAS, TASK_TOOL_NAMES, TASK_MUTATING_TOOLS, execute_task_tool,
)

CONTINUE_PROMPT = ("Continue with the next concrete action, or call "
                   "`task_complete`/`task_fail`.")
RESUME_WORDS = ("continue", "resume", "keep going", "go on", "carry on")


@dataclass
class HarnessResult:
    session_id: str
    termination_reason: str
    steps: int
    duration_s: float
    final_text: str
    tool_calls: int = 0
    compactions: int = 0
    extra: dict = field(default_factory=dict)


def default_failure_check(result):
    s = str(result or "").lstrip()
    low = s[:200].lower()
    return (s.startswith(("Error", "ERROR", "Refused", "Denied"))
            or "the user declined" in low or low.startswith("traceback"))


def _parse_args(raw):
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            v = json.loads(raw)
            return v if isinstance(v, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _calls_from(resp):
    out = []
    for tc in getattr(resp, "tool_calls", None) or []:
        if hasattr(tc, "as_ollama"):
            out.append(tc.as_ollama())
        elif isinstance(tc, dict):
            out.append(tc)
    return out


async def _maybe_await(v):
    return await v if inspect.isawaitable(v) else v


class AgentHarness:
    """One agent run over a persistent task store.

    chat_fn(messages, tools) -> LLMResponse-like (sync or async)
    dispatch_fn(name, args) -> result (sync or async), for every non-harness tool
    recover_fn(content) -> [ollama tool-call dicts] for text-form tool calls (optional)
    """

    def __init__(self, chat_fn, dispatch_fn, tools=None, store=None, session_id=None,
                 hooks=None, recover_fn=None, failure_fn=None, mutating_tools=(),
                 max_safety_steps=None, time_budget_s=None,
                 stagnation_cycles=config.DEFAULT_STAGNATION_CYCLES,
                 tasks_dir=None, rules=None, num_ctx=None, clock=time.monotonic, on_step=None,
                 subagents=None, workflows_dir=None):
        self.chat_fn = chat_fn
        self.dispatch_fn = dispatch_fn
        self.store = store or TaskStore()
        self.hooks = hooks if hooks is not None else HookRegistry()
        self.session_id = session_id or time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.recover_fn = recover_fn
        self.failure_fn = failure_fn or default_failure_check
        self.max_safety_steps = max_safety_steps or config.max_safety_steps()
        self.time_budget_s = time_budget_s or config.time_budget_s()
        self.stagnation_cycles = stagnation_cycles
        self.tasks_dir = tasks_dir or config.TASKS_DIR
        self.rules = config.rules_text() if rules is None else rules
        self.clock = clock
        self.on_step = on_step
        self.output_store = ToolOutputStore(self.session_id, os.path.join(self.tasks_dir, "tool_outputs"))
        self.compactor = Compactor(self.session_id, num_ctx=num_ctx,
                                   transcripts_dir=os.path.join(self.tasks_dir, "transcripts"),
                                   rules=self.rules)
        self.native = {}                          # name -> callable(args) for harness-owned tools
        self.native_schemas = list(TASK_TOOL_SCHEMAS)
        self.mutating = set(mutating_tools) | set(TASK_MUTATING_TOOLS)
        names = {s["function"]["name"] for s in self.native_schemas}
        self.tools = list(self.native_schemas) + [
            t for t in (tools or []) if t.get("function", {}).get("name") not in names]
        self.messages = []
        self.tool_call_count = 0
        self.subagents = subagents
        if subagents is not None:
            # Subagents share the store, hooks and output truncation, never the history.
            if subagents.store is None:
                subagents.store = self.store
            if subagents.hooks is None:
                subagents.hooks = self.hooks
            if subagents.output_store is None:
                subagents.output_store = self.output_store
            if not subagents.session_id:
                subagents.session_id = self.session_id
            self.add_native_tool(SPAWN_SUBAGENT_SCHEMA, subagents.tool, mutating=True)
            # Declarative workflows (data/workflows/*.json) are sequences of subagent calls.
            self.workflows_dir = workflows_dir or config.WORKFLOWS_DIR
            self.add_native_tool(
                workflows.WORKFLOW_RUN_SCHEMA,
                lambda args: workflows.tool(args, subagents.spawn, self.workflows_dir),
                mutating=True)

    # ---- extension points ----
    def add_native_tool(self, schema, fn, mutating=False):
        name = schema["function"]["name"]
        self.native[name] = fn
        self.tools = [t for t in self.tools if t.get("function", {}).get("name") != name] + [schema]
        if mutating:
            self.mutating.add(name)

    # ---- helpers ----
    def _task_state_message(self):
        cur = self.store.current()
        nxt = cur or self.store.peek_next()
        open_count = sum(1 for t in self.store.list() if t["status"] in ("pending", "in_progress"))
        if cur:
            body = (f"Current task (in progress): #{cur['id']} {cur['description']}\n"
                    f"Acceptance criteria: {cur.get('acceptance_criteria') or 'n/a'}\n"
                    f"Attempts: {cur.get('attempts')}  Last error: {cur.get('last_error') or 'none'}")
        elif nxt:
            body = f"No task in progress. Next ready task: #{nxt['id']} {nxt['description']}. Call task_next."
        else:
            body = "No open tasks. If the goal is met, call task_complete on the last task or stop."
        return {"role": "system",
                "content": f"[TASK STORE — {open_count} open]\n{body}\n"
                           f"Every reply must be a tool call."}

    def _log_session(self, result, goal):
        os.makedirs(self.tasks_dir, exist_ok=True)
        line = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "session_id": result.session_id,
                "termination_reason": result.termination_reason, "steps": result.steps,
                "duration_s": round(result.duration_s, 2), "tool_calls": result.tool_calls,
                "compactions": result.compactions, "goal": (goal or "")[:160]}
        with open(os.path.join(self.tasks_dir, "sessions.log"), "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    def seed(self, goal):
        """Make sure the goal is in the task store. Open tasks from an earlier run
        are kept (resume); a plain 'continue' adds nothing."""
        goal = (goal or "").strip()
        if not goal:
            return None
        if goal.lower().rstrip(".!") in RESUME_WORDS and self.store.has_open():
            return None
        for t in self.store.list():
            if t["status"] in ("pending", "in_progress") and t["description"] == goal:
                return t
        return self.store.add(goal, acceptance_criteria="The user's request is fulfilled.")

    def _prepare_messages(self, base_messages):
        msgs = [dict(m) for m in (base_messages or [])]
        rules = rules_message(self.rules)
        insert_at = 1 if msgs and msgs[0].get("role") == "system" else 0
        msgs.insert(insert_at, rules)
        return msgs

    async def _call_model(self, messages):
        return await _maybe_await(self.chat_fn(messages, self.tools))

    async def _execute(self, name, args):
        if name in TASK_TOOL_NAMES:
            return execute_task_tool(self.store, name, args)
        if name in self.native:
            return await _maybe_await(self.native[name](args))
        return await _maybe_await(self.dispatch_fn(name, args))

    def _tool_message(self, name, result):
        """Truncate first (full output to tool_outputs/), then build the history message."""
        text, _ = self.output_store.truncate(name, str(result))
        return {"role": "tool", "content": text, "name": name}

    # ---- the loop ----
    async def run(self, goal, base_messages=None):
        start = self.clock()
        self.seed(goal)
        self.messages = self._prepare_messages(base_messages)
        steps = 0
        text_only_streak = 0
        cycles_since_mutation = 0
        reason = None
        final_text = ""
        last_resolution = None

        while True:
            if self.clock() - start >= self.time_budget_s:
                reason = "time_budget"
                break
            if steps >= self.max_safety_steps:
                reason = "safety_cap"
                break

            state = self._task_state_message()
            if self.compactor.needs_compaction(self.messages + [state]):
                self.messages = self.compactor.compact(self.messages, self.store.current())
                print(f"[harness] compacted context (archive: {self.compactor.last_archive})")
            call_messages = self.messages + [state]
            steps += 1
            if self.on_step:
                try:
                    self.on_step(steps, self.store.current())
                except Exception:
                    pass
            try:
                resp = await self._call_model(call_messages)
            except Exception as e:
                reason = "error"
                final_text = f"Something went wrong talking to my own model: {e}"
                break

            tool_calls = _calls_from(resp)
            content = (getattr(resp, "content", "") or "").strip()
            if not tool_calls and content and self.recover_fn:
                try:
                    recovered = self.recover_fn(content) or []
                except Exception:
                    recovered = []
                if recovered:
                    tool_calls, content = recovered, ""

            # ---- (b) text-only turn ----
            if not tool_calls:
                text_only_streak += 1
                cycles_since_mutation += 1
                self.messages.append({"role": "assistant", "content": content})
                if content:
                    final_text = content
                if not self.store.has_open():
                    reason = "turn_complete"
                    break
                if text_only_streak >= 2:
                    reason = "model_stalled"
                    break
                self.messages.append({"role": "user", "content": CONTINUE_PROMPT})
                if cycles_since_mutation >= self.stagnation_cycles:
                    reason = "stagnant"
                    break
                continue

            text_only_streak = 0
            self.messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})
            mutated = False
            resolved = False
            notices = []
            for call in tool_calls:
                fn = call.get("function", {}) or {}
                name = fn.get("name") or ""
                args = _parse_args(fn.get("arguments"))
                self.tool_call_count += 1
                allowed, deny_reason = self.hooks.pre_tool_use(name, args, self.session_id)
                if not allowed:
                    self.messages.append(self._tool_message(
                        name, f"Denied: {deny_reason} Do not resend this call."))
                    continue
                try:
                    result = await self._execute(name, args)
                except Exception as e:
                    result = f"Error: {type(e).__name__}: {e}"
                failed = self.failure_fn(result)
                if failed:
                    notices += self.hooks.post_tool_use_failure(name, args, str(result), self.session_id)
                else:
                    notices += self.hooks.post_tool_use(name, args, result, self.session_id)
                self.messages.append(self._tool_message(name, result))
                if name in self.mutating:
                    mutated = True
                if name in ("task_complete", "task_fail") and not failed:
                    resolved = True
                    last_resolution = name
                    if name == "task_complete":
                        final_text = str(args.get("result") or final_text)
                    else:
                        final_text = f"I couldn't finish that: {args.get('reason') or 'unknown error'}"

            if notices:
                self.messages.append({"role": "user", "content": "[Harness notice — not from the user]\n"
                                      + "\n".join(notices)})
            cycles_since_mutation = 0 if mutated else cycles_since_mutation + 1

            # ---- (a) nothing left after a resolution ----
            if resolved and self.store.peek_next() is None:
                reason = "task_failed" if last_resolution == "task_fail" else "task_complete"
                break
            # ---- (c) stagnation ----
            if cycles_since_mutation >= self.stagnation_cycles:
                reason = "stagnant"
                break

        result = HarnessResult(
            session_id=self.session_id, termination_reason=reason, steps=steps,
            duration_s=self.clock() - start, final_text=final_text or "...",
            tool_calls=self.tool_call_count, compactions=self.compactor.compactions)
        self._finish(result, goal)
        return result

    def _finish(self, result, goal):
        try:
            self._log_session(result, goal)
        except OSError:
            pass

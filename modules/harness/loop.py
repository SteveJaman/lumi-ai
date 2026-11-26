"""Goal-driven agent loop for Lumi's agent mode.

Replaces the fixed 6-step loop. The loop runs until one of these, checked every cycle:

    task_complete  task_next is empty after a task_complete / task_fail
    task_failed    same, but the last resolution was a task_fail
    turn_complete  a text-only turn with no open task (a plain answer)
    (b') a text reply after the task's real work succeeded finishes that task with
         the text as the reply (FINISHED_BY_REPLY), instead of stalling on it
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
import re
import time
import uuid
from dataclasses import dataclass, field

from modules.harness import config, workflows
from modules.harness.context import Compactor, ToolOutputStore, rules_message, archive
from modules.harness.hooks import HookRegistry
from modules.harness.subagents import SPAWN_SUBAGENT_SCHEMA
from modules.harness.tasks import (
    TaskStore, TASK_TOOL_SCHEMAS, TASK_TOOL_NAMES, TASK_MUTATING_TOOLS, execute_task_tool,
)

# Sent once after a text-only reply while a task is open. A small local model
# often "finishes" by chatting ("Alright, it's playing!"); this tells it where
# that sentence belongs.
CONTINUE_PROMPT = ("You replied with text, not a tool call. If the request is done, call "
                   "`task_complete` now and put your message to the user in `result` (it is shown "
                   "to them as your reply). Otherwise call the tool for the next concrete action, "
                   "or `task_fail` with the reason.")
RESUME_WORDS = ("continue", "resume", "keep going", "go on", "carry on")

# Stagnation counts cycles without progress. Progress is a mutating tool, or any
# other tool that succeeded and isn't just looking (spotify_play, click_button,
# send_email...). Reads, searches and "what's playing" don't count.
_LOOKING_TOOL = re.compile(
    r"(?:^|_)(?:read|list|get|search|find|look|now|playing|status|recall|browse|peek|show|tracks|info|"
    r"query|check|triage|summarize|extract|research)(?:_|$)")


def is_progress(name, failed, mutating):
    if name in mutating:
        return True
    return not failed and name not in TASK_TOOL_NAMES and not _LOOKING_TOOL.search(name or "")


# Sent (as a system note) when a text-only reply follows a tool call and isn't
# taken as the finish: the tool ran; finishing is task_complete, not prose.
TOOL_THEN_TEXT_PROMPT = ("Tool executed. If the task is done, you MUST call `task_complete` "
                         "(put your message to the user in `result`). Do not reply with text.")

# The harness's and prompter's own status lines. A small model copies them out of
# chat history; they are never her finishing reply.
_HARNESS_NOTES = ("(I stopped:", "I couldn't finish that:", "Something went wrong talking to my own model",
                  "[Harness notice", "[TASK STORE")

# A text reply that still promises work, or asks the user something, isn't a finish.
_UNFINISHED = re.compile(
    r"\b(i'?ll|i will|i'?m going to|i am going to|gonna|let me|about to|next,? i|first,? i|"
    r"one (sec|second|moment)|give me (a|one|just one)|hold on|bear with me|working on it|"
    r"looking into|still (need|have) to|i need to|i have to|trying to)\b", re.I)


def sounds_unfinished(text):
    """True when a reply reads as 'about to do something' or asks the user a question."""
    t = (text or "").strip()
    return (not t or t.endswith("?") or bool(_UNFINISHED.search(t))
            or any(note in t for note in _HARNESS_NOTES))


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
        self.finished_by_reply = 0          # tasks finished by a text reply after their work (b')
        self.task_switches = 0              # times the window was cleared for a new task
        self.parked_ids = []                # earlier runs' open tasks set aside by this request
        self.resumed_ids = []               # parked tasks "continue" brought back
        self._head_len = 0
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
                           f"Every reply must be a tool call. When the request is done, call "
                           f"task_complete; its result is your reply to the user, so never "
                           f"announce it in a text reply."}

    def _log_session(self, result, goal):
        os.makedirs(self.tasks_dir, exist_ok=True)
        line = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "session_id": result.session_id,
                "termination_reason": result.termination_reason, "steps": result.steps,
                "duration_s": round(result.duration_s, 2), "tool_calls": result.tool_calls,
                "compactions": result.compactions, "goal": (goal or "")[:160]}
        with open(os.path.join(self.tasks_dir, "sessions.log"), "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    def seed(self, goal):
        """Make sure the goal is in the task store, and that it's what this run works on.
        A new request parks open tasks left by earlier runs (a stalled "play my music"
        mustn't hijack "find a restaurant"); "continue" brings them back and adds nothing."""
        goal = (goal or "").strip()
        if not goal:
            return None
        if goal.lower().rstrip(".!") in RESUME_WORDS:
            back = self.store.unpark_all() if hasattr(self.store, "unpark_all") else []
            if back or self.store.has_open():
                self.parked_ids, self.resumed_ids = [], back
                return None
        task = next((t for t in self.store.list()
                     if t["status"] in ("pending", "in_progress") and t["description"] == goal), None)
        if task is None:
            task = self.store.add(goal, acceptance_criteria="The user's request is fulfilled.")
        if hasattr(self.store, "park_open"):
            self.parked_ids = self.store.park_open(keep=[task["id"]])
        return task

    def _prepare_messages(self, base_messages):
        msgs = [dict(m) for m in (base_messages or [])]
        rules = rules_message(self.rules)
        insert_at = 1 if msgs and msgs[0].get("role") == "system" else 0
        msgs.insert(insert_at, rules)
        self._head_len = insert_at + 1            # system prompt + rules: kept on a task switch
        return msgs

    def _isolate(self, task):
        """A different task was just picked (task_next): archive the transcript and
        drop everything after the system prompt and rules, so the old task's chatter
        (and the chat history it came with) can't steer this one."""
        try:
            archive(self.messages, self.session_id, getattr(self.compactor, "transcripts_dir", None),
                    reason=f"task switch to #{task['id']}")
        except OSError:
            pass
        head = self.messages[:self._head_len]
        brief = (f"[New task #{task['id']}] {task['description']}\n"
                 f"Acceptance criteria: {task.get('acceptance_criteria') or 'n/a'}\n"
                 "Earlier tasks' messages were cleared; work only on this one.")
        self.messages = head + [{"role": "user", "content": brief}]
        self.task_switches += 1

    def _open_task(self, goal_task):
        """The task being worked on: the one in progress, else this run's own
        request while it's still open (a model often skips task_next)."""
        cur = self.store.current()
        if cur is not None:
            return cur
        if goal_task is not None:
            t = self.store.get(goal_task["id"]) if hasattr(self.store, "get") else None
            if t is not None and t["status"] in ("pending", "in_progress"):
                return t
        return None

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
    async def run(self, goal, base_messages=None, seed=True):
        """seed=False: the caller already filled the task store (e.g. an
        approved plan from modules/autonomous_agent.py); `goal` is only logged."""
        start = self.clock()
        goal_task = self.seed(goal) if seed else None
        self.messages = self._prepare_messages(base_messages)
        steps = 0
        text_only_streak = 0
        acted_for = None              # task id whose real work (a non-task tool) succeeded
        last_step_had_tools = False   # the previous model step called a non-task tool
        resumed = self.store.current()
        worked_on = resumed["id"] if resumed else None   # the last task this run worked on
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
                # (b') She did the work and is telling the user about it ("Alright, your
                # Liked Songs are playing!"): that IS the finish. Finish the task with it
                # as the reply, now if it reads as done, else rather than stalling.
                target = self._open_task(goal_task)
                if (target is not None and content and acted_for == target["id"]
                        and (text_only_streak >= 2 or not sounds_unfinished(content))):
                    self.store.complete(target["id"], content)
                    self.finished_by_reply += 1
                    last_resolution, acted_for, text_only_streak = "task_complete", None, 0
                    final_text = content
                    if self.store.peek_next() is None:
                        reason = "task_complete"
                        break
                    continue
                if text_only_streak >= 2:
                    reason = "model_stalled"
                    break
                if last_step_had_tools:
                    self.messages.append({"role": "system", "content": TOOL_THEN_TEXT_PROMPT})
                else:
                    self.messages.append({"role": "user", "content": CONTINUE_PROMPT})
                last_step_had_tools = False
                if cycles_since_mutation >= self.stagnation_cycles:
                    reason = "stagnant"
                    break
                continue

            text_only_streak = 0
            # Only real work counts (spotify_play, write_file...), not task bookkeeping.
            last_step_had_tools = any(((c.get("function") or {}).get("name") or "") not in TASK_TOOL_NAMES
                                      for c in tool_calls)
            self.messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})
            mutated = False
            resolved = False
            notices = []
            switched_to = None
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
                if is_progress(name, failed, self.mutating):
                    mutated = True
                if not failed and name not in TASK_TOOL_NAMES:
                    work_on = self._open_task(goal_task)
                    acted_for = work_on["id"] if work_on else acted_for
                if name == "task_next" and not failed:
                    now = self.store.current()
                    if now is not None:
                        if worked_on is not None and now["id"] != worked_on:
                            switched_to = now
                        worked_on = now["id"]
                if name in ("task_complete", "task_fail") and not failed:
                    resolved = True
                    acted_for = None
                    last_resolution = name
                    if name == "task_complete":
                        final_text = str(args.get("result") or final_text)
                    else:
                        final_text = f"I couldn't finish that: {args.get('reason') or 'unknown error'}"

            if switched_to is not None:               # (Change 2) only this task in view
                self._isolate(switched_to)
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

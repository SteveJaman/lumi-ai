"""
Autonomous tasks (`!auto <goal>`): Lumi works through a goal unsupervised.

    plan ──► one approval ──► execute (harness loop) ──► verify in Python ──► learn
                                   ▲                          │ failed
                                   └──── feedback as new task ┘   (up to 3 rounds)

Built on the agent harness (modules/harness/), not beside it: the task DAG,
the goal loop, text-form tool-call recovery, PreToolUse hooks, output
truncation and compaction all apply. This module adds:

* **One approval for the plan.** The model drafts a plan; the user approves
  it once through the HITL gate. After that, read-only actions run without
  asking: reading files in data/ and sandbox/, read-only commands, window
  reading and focusing, and web price searches (find_local_deals skips its
  own prompt because the approved plan said searches would run). Writing a
  file and any command that isn't read-only still ask every time.
* **A sandbox.** File and terminal actions must stay inside data/ or
  sandbox/ (SandboxPolicy). data/credentials, cached mail, the browser
  profile, the user database, and Lumi's own hooks, workflows, task store
  and skills are off limits. Tools outside AUTONOMOUS_TOOLS (email, clicks,
  Claude Code, games, SDK/MCP) are not offered and are denied if called.
  Web actions are read-only by construction (see deal_finder.py).
* **Self-healing.** ToolGate catches every failure, retries transient ones
  (timeouts, dropped connections) once by itself, and appends a diagnosis so
  the model tries a different approach. After 3 retries on the same task
  (4 failures) the task is marked failed and the run moves on.
* **Independent verification.** ``verify_success(goal, evidence)`` never
  takes the model's word: it checks the task store, that promised files exist
  on disk and aren't empty, and that a price the goal asked for is a real
  currency amount that also appears in a tool's output (not invented).
  Failures go back to the model as a new task.
* **Procedural memory.** A verified success is written to data/skills/ by
  skills_manager, and the best-matching skill is shown to the planner and the
  executor next time.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from constants import DATA_ROOT, WORKSPACE_ROOT
from modules import skills_manager
from modules.harness.subagents import command_is_read_only

SANDBOX_ROOT = os.path.join(WORKSPACE_ROOT, "sandbox")
AUTONOMOUS_TASKS_DIR = os.path.join(DATA_ROOT, "tasks", "autonomous")

MAX_RETRIES = 3                  # retries per task after its first failure
MAX_VERIFY_ROUNDS = 3            # execute+verify rounds before giving up
TRANSIENT_BACKOFF_S = 2.0
MAX_EVIDENCE_CHARS = 4000
DEFAULT_TIME_BUDGET_S = 30 * 60
DEFAULT_MAX_STEPS = 80

# data/ folders the agent may never read (secrets, private mail, sign-ins).
READ_BLOCKED_DATA_DIRS = frozenset({"credentials", "workspace", "browser_profile", "memories", "profiles",
                                    "browser_search_profile"})
# ...and never write: its own safety hooks, workflows, task store and skills too.
WRITE_BLOCKED_DATA_DIRS = READ_BLOCKED_DATA_DIRS | {"hooks", "workflows", "tasks", "skills"}

READ_ONLY_TOOLS = frozenset({
    "read_file", "list_dir", "search_files", "search_library",
    "get_active_window_info", "read_window_text", "focus_window",
    "find_local_deals", "research_product",
})
WRITE_TOOLS = frozenset({"write_file"})
COMMAND_TOOLS = frozenset({"run_command"})
AUTONOMOUS_TOOLS = READ_ONLY_TOOLS | WRITE_TOOLS | COMMAND_TOOLS
# Tools that ask for approval on their own but are covered by the plan approval.
PLAN_APPROVED_TOOLS = frozenset({"find_local_deals"})

CURRENCY_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(\.\d{2})?(?![\d,])")
_PATH_TOKEN_RE = re.compile(r"[\w./\\-]*[\w-]\.(?:txt|md|csv|json|html|log|yaml|yml|py|tsv|xml)\b", re.I)
_WANTS_FILE_RE = re.compile(
    r"\b(save|write|store|export|record|log|put|create)\b.{0,60}\b(file|txt|csv|md|json|document|note)s?\b"
    r"|\b(save|write|export)\b.{0,40}\bto\b", re.I)
_WANTS_PRICE_RE = re.compile(r"\b(price|prices|pricing|cost|costs|cheapest|cheaper|deal|deals|how much)\b|\$", re.I)
_TRANSIENT_RE = re.compile(r"time(d)?[ -]?out|temporar|connection (reset|refused|aborted|error)"
                           r"|\b50[234]\b|try again later", re.I)
_DRIVE_RE = re.compile(r"(?<![\w])[A-Za-z]:[\\/]")


class PolicyError(Exception):
    """A tool call that would leave the sandbox or isn't allowed unsupervised."""


# ─── sandbox ─────────────────────────────────────────────────────────

def _real(p: str) -> str:
    return os.path.normcase(os.path.realpath(p))


def _inside(target: str, root: str) -> bool:
    t, r = _real(target), _real(root)
    return t == r or t.startswith(r + os.sep)


class SandboxPolicy:
    """What an autonomous run may touch. Paths are resolved the way
    agent_tools._safe_path does (relative to the repo root, symlinks followed)
    and must land in data/ or sandbox/."""

    def __init__(self, data_root: str = DATA_ROOT, sandbox_root: str = SANDBOX_ROOT,
                 workspace_root: str = WORKSPACE_ROOT):
        self.data_root = data_root
        self.sandbox_root = sandbox_root
        self.workspace_root = workspace_root
        self.plan_approved = False

    # ---- paths ----
    def resolve(self, path: Any, write: bool = False) -> str:
        raw = str(path or "").strip().strip("'\"")
        if not raw or raw in (".", "./"):
            raise PolicyError("give a path under sandbox/ or data/ (the repo root is off limits).")
        if "\x00" in raw:
            raise PolicyError("bad path.")
        if os.path.isabs(raw) or _DRIVE_RE.match(raw) or raw.startswith(("/", "\\")):
            raise PolicyError(f"'{raw}' is an absolute path; use a path under sandbox/ or data/.")
        target = os.path.realpath(os.path.join(self.workspace_root, raw))
        if _inside(target, self.sandbox_root):
            return target
        if _inside(target, self.data_root):
            rel = os.path.relpath(target, os.path.realpath(self.data_root))
            first = rel.split(os.sep)[0].lower() if rel != "." else ""
            blocked = WRITE_BLOCKED_DATA_DIRS if write else READ_BLOCKED_DATA_DIRS
            if first in blocked:
                raise PolicyError(f"data/{first}/ is off limits to autonomous tasks.")
            from modules.workspace import _is_private_name
            if any(_is_private_name(p) for p in rel.split(os.sep)):
                raise PolicyError("that file looks like a secret (key, token or database).")
            if write and rel == ".":
                raise PolicyError("write to a file, not to data/ itself.")
            return target
        raise PolicyError(f"'{raw}' is outside the sandbox; autonomous tasks may only use sandbox/ and data/.")

    def _check_command(self, cmd: str) -> None:
        if not cmd.strip():
            raise PolicyError("empty command.")
        if _DRIVE_RE.search(cmd) or ".." in cmd:
            raise PolicyError("commands may not use absolute paths or '..'; stay in sandbox/ or data/.")
        for tok in re.split(r"\s+", cmd.strip())[1:]:
            tok = tok.strip("'\"")
            if tok.startswith("-") or ("/" not in tok and "\\" not in tok and not _PATH_TOKEN_RE.fullmatch(tok)):
                continue                                      # a flag or a plain word
            # A path, or a bare file name (which would mean the repo root): must
            # resolve inside sandbox/ or data/.
            self.resolve(tok, write=False)

    # ---- the gate ----
    def check(self, name: str, args: Optional[Dict[str, Any]]) -> None:
        """Raise PolicyError when this call isn't allowed unsupervised."""
        args = args or {}
        if name in ("task_list", "task_add", "task_next", "task_complete", "task_fail"):
            return
        if name not in AUTONOMOUS_TOOLS:
            raise PolicyError(f"{name} isn't available in autonomous mode.")
        if name in ("read_file", "list_dir"):
            self.resolve(args.get("path"), write=False)
        elif name == "search_files":
            target = self.resolve(args.get("path"), write=False)
            if _real(target) == _real(self.data_root):
                raise PolicyError("search a folder inside data/ (e.g. data/tasks) or sandbox/, not data/ itself.")
        elif name == "write_file":
            self.resolve(args.get("path"), write=True)
        elif name == "run_command":
            self._check_command(str(args.get("command") or ""))

    def preapproved(self, name: str, args: Optional[Dict[str, Any]] = None) -> bool:
        """True when the approved plan already covers this call, so the
        prompter skips its per-call approval prompt."""
        if not self.plan_approved:
            return False
        if name in PLAN_APPROVED_TOOLS:
            return True
        if name == "run_command":
            cmd = str((args or {}).get("command") or "")
            try:
                self._check_command(cmd)
            except PolicyError:
                return False
            return command_is_read_only(cmd)
        return False


# ─── failures ────────────────────────────────────────────────────────

def default_failure(result: Any) -> bool:
    s = str(result or "").lstrip()
    low = s[:300].lower()
    return (low.startswith(("error", "refused", "denied", "traceback"))
            or (s.startswith("Exit code ") and not s.startswith("Exit code 0"))
            or "the user declined" in low or "no prices found" in low)


def diagnose(result: Any) -> str:
    """A one-line hint about what went wrong and what to try instead."""
    low = str(result or "").lower()
    if _TRANSIENT_RE.search(low):
        return "it timed out or the connection dropped. Try once more, then switch to another source or a smaller request."
    if "bot check" in low or "blocks automated" in low or "captcha" in low:
        return "that site blocks automated browsers. Don't retry it; try different search words or another category."
    if "no prices found" in low:
        return "the page had no prices. Try broader or simpler search words, or another category."
    if "declined" in low:
        return "the user declined. Don't repeat that action; find another way or call task_fail."
    if "outside the sandbox" in low or "off limits" in low or "absolute path" in low:
        return "that was outside the sandbox. Use paths under sandbox/ (or data/)."
    if "isn't available in autonomous mode" in low or "not on the command allowlist" in low:
        return "that tool or command isn't allowed here. Use one of the tools you were given."
    if "not found" in low or "no such file" in low or "does not exist" in low or "no matches" in low:
        return "something wasn't found. Check names with list_dir (sandbox/ or data/) before using them."
    if "metacharacter" in low:
        return "run one program with plain arguments; no pipes, redirects or chaining."
    return "read the error, then change the arguments or use a different tool."


# ─── evidence + verification ─────────────────────────────────────────

@dataclass
class Evidence:
    final_text: str = ""
    termination: str = ""
    tool_log: List[Dict[str, Any]] = field(default_factory=list)
    files_written: List[str] = field(default_factory=list)
    tasks: List[Dict[str, Any]] = field(default_factory=list)
    output_file: Optional[str] = None            # the plan's promised deliverable, if any

    def observed_text(self) -> str:
        """Everything tools returned (not what the model wrote)."""
        return "\n".join(str(e.get("result", "")) for e in self.tool_log
                         if not e.get("failed") and e.get("name") not in WRITE_TOOLS)


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


@dataclass
class VerificationResult:
    passed: bool
    checks: List[Check]

    def feedback(self) -> str:
        bad = [c for c in self.checks if not c.passed]
        return "\n".join(f"- {c.name}: {c.detail}" for c in bad) or "- (no failed checks)"


def _prices(text: str) -> List[float]:
    out = []
    for whole, cents in CURRENCY_RE.findall(text or ""):
        try:
            out.append(round(float(whole.replace(",", "") + (cents or "")), 2))
        except ValueError:
            pass
    return out


def wants_file(goal: str) -> bool:
    return bool(_WANTS_FILE_RE.search(goal or "") or _PATH_TOKEN_RE.search(goal or ""))


def wants_price(goal: str) -> bool:
    return bool(_WANTS_PRICE_RE.search(goal or ""))


# ─── the task ────────────────────────────────────────────────────────

ChatFn = Callable[[List[Dict[str, Any]], Optional[List[Dict[str, Any]]]], Any]
DispatchFn = Callable[[str, Dict[str, Any]], Awaitable[Any]]
ConfirmFn = Callable[[str, Any], Awaitable[bool]]
LLMFn = Callable[[str, str], Awaitable[str]]


@dataclass
class Plan:
    summary: str
    steps: List[Dict[str, str]]
    output_file: Optional[str] = None


@dataclass
class AutonomousResult:
    status: str                       # "verified" | "unverified" | "declined" | "error"
    reply: str
    session_id: str
    rounds: int = 0
    verification: Optional[VerificationResult] = None
    skill_path: Optional[str] = None
    evidence: Optional[Evidence] = None


_PLAN_SYSTEM = (
    "You plan work for Lumi's autonomous mode. Reply with JSON only, no prose:\n"
    "{\"summary\": \"one sentence\", \"steps\": [{\"description\": \"...\", \"acceptance\": \"how to tell it is done\"}], "
    "\"output_file\": \"sandbox/<name>.<ext>\" or null}\n"
    "Rules: 2-6 concrete steps, each doable with ONE or two tool calls. Files may only be written under "
    "sandbox/ (or data/). Never plan purchases, sign-ins, carts or checkouts. Set output_file when the goal "
    "asks to save something.\nTools you can use:\n"
)

_EXEC_RULES = (
    "[AUTONOMOUS MODE — rules from Lumi's harness, not from the user]\n"
    "You are working through an approved plan with no one watching. Work the task store: call task_next, "
    "do the task with tools, then task_complete with a short result (or task_fail with the reason).\n"
    "- Files: only paths under sandbox/ (or data/). Example: write_file(path=\"sandbox/eggs.md\", ...).\n"
    "- Web: read-only. Never buy, sign in, or open carts/checkouts.\n"
    "- Never claim something you didn't observe in a tool result. Your work is checked by code afterwards: "
    "promised files must exist, and every price you report must appear in a tool result.\n"
    "- When a tool fails, read the diagnosis and change your approach; don't repeat the same call."
)


class AutonomousTask:
    """One autonomous goal, from plan to verified result."""

    def __init__(self, goal: str, *, chat_fn: ChatFn, dispatch_fn: DispatchFn, confirm_fn: ConfirmFn,
                 tools: List[Dict[str, Any]], llm_fn: Optional[LLMFn] = None,
                 recover_fn=None, policy: Optional[SandboxPolicy] = None,
                 tasks_dir: Optional[str] = None, skills_dir: Optional[str] = None,
                 time_budget_s: Optional[float] = None, max_steps: Optional[int] = None,
                 on_step=None, sleep=asyncio.sleep, harness_factory=None, store=None, on_event=None):
        self.goal = " ".join(str(goal or "").split())
        self.chat_fn = chat_fn
        self.dispatch_fn = dispatch_fn
        self.confirm_fn = confirm_fn
        self.llm_fn = llm_fn
        self.recover_fn = recover_fn
        self.policy = policy or SandboxPolicy()
        self.tools = [t for t in tools if t.get("function", {}).get("name") in AUTONOMOUS_TOOLS]
        self.session_id = "auto-" + time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.tasks_dir = tasks_dir or AUTONOMOUS_TASKS_DIR
        self.skills_dir = skills_dir
        self.time_budget_s = time_budget_s or _env_float("LUMI_AUTONOMOUS_TIME_BUDGET_S", DEFAULT_TIME_BUDGET_S)
        self.max_steps = max_steps or int(_env_float("LUMI_AUTONOMOUS_MAX_STEPS", DEFAULT_MAX_STEPS))
        self.on_step = on_step
        self.on_event = on_event          # (phase, data) for the Model Pipeline view; never raises
        self.sleep = sleep
        self.harness_factory = harness_factory
        self._store = store
        self.evidence = Evidence()
        self.plan: Optional[Plan] = None
        self.skill: Optional[skills_manager.Skill] = None
        self._task_failures: Dict[Any, int] = {}

    # ---- the store ----
    @property
    def store(self):
        if self._store is None:
            from modules.harness.tasks import TaskStore
            self._store = TaskStore(os.path.join(self.tasks_dir, f"{self.session_id}.json"))
        return self._store

    # ---- plan ----
    async def _ask(self, system: str, user: str) -> str:
        if self.llm_fn is not None:
            return await self.llm_fn(system, user)
        resp = await _maybe_await(self.chat_fn([{"role": "system", "content": system},
                                                {"role": "user", "content": user}], None))
        return str(getattr(resp, "content", resp) or "")

    def _tool_menu(self) -> str:
        lines = []
        for t in self.tools:
            f = t.get("function", {})
            lines.append(f"- {f.get('name')}: {str(f.get('description', '')).split('. ')[0][:140]}")
        return "\n".join(lines)

    async def make_plan(self) -> Plan:
        skill_block = skills_manager.format_for_prompt(self.skill)
        user = f"Goal: {self.goal}"
        if skill_block:
            user += "\n\n" + skill_block
        try:
            text = await self._ask(_PLAN_SYSTEM + self._tool_menu(), user)
            data = skills_manager._parse_json(text) or {}
        except Exception:
            data = {}
        steps = []
        for s in data.get("steps") or []:
            if isinstance(s, dict) and str(s.get("description") or "").strip():
                steps.append({"description": " ".join(str(s["description"]).split())[:300],
                              "acceptance": " ".join(str(s.get("acceptance") or "").split())[:300]})
            elif isinstance(s, str) and s.strip():
                steps.append({"description": " ".join(s.split())[:300], "acceptance": ""})
        if not steps:
            steps = [{"description": self.goal, "acceptance": "The goal is met."}]
        out = data.get("output_file")
        out = str(out).strip() if isinstance(out, str) and out.strip() and out.lower() != "null" else None
        if out:
            try:
                self.policy.resolve(out, write=True)
            except PolicyError:
                out = "sandbox/" + os.path.basename(out.replace("\\", "/"))
        return Plan(summary=str(data.get("summary") or self.goal)[:300], steps=steps[:6], output_file=out)

    def plan_preview(self, plan: Plan) -> str:
        lines = [f"Autonomous task: {self.goal}", "", f"Plan: {plan.summary}"]
        lines += [f"  {i}. {s['description']}" for i, s in enumerate(plan.steps, 1)]
        if plan.output_file:
            lines.append(f"Output file: {plan.output_file}")
        if self.skill is not None:
            lines.append(f"Using learned workflow: {self.skill.name}")
        lines += ["",
                  "Runs WITHOUT asking again: reading files in sandbox/ and data/, read-only commands, "
                  "reading/focusing windows, read-only web price searches (find_local_deals).",
                  "Still ASKS each time: writing a file, any command that changes something.",
                  "Never: purchases, sign-ins, carts, files outside sandbox/ and data/."]
        return "\n".join(lines)

    # ---- progress events (Plan -> Execute -> Verify) ----
    def _event(self, phase: str, step: str, status: str = "running", **extra) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(phase, {"session_id": self.session_id, "phase": phase, "step": step,
                                  "status": status, **extra})
        except Exception:
            pass                          # a UI hiccup must never break the run

    # ---- the gate every tool call passes ----
    def _current_task_id(self):
        try:
            cur = self.store.current()
        except Exception:
            cur = None
        return cur["id"] if cur else None

    def _log(self, name, args, result, failed):
        self.evidence.tool_log.append({"name": name, "args": dict(args or {}),
                                       "result": str(result)[:MAX_EVIDENCE_CHARS], "failed": failed})

    async def gate(self, name: str, args: Dict[str, Any]) -> str:
        args = args if isinstance(args, dict) else {}
        try:
            self.policy.check(name, args)
        except PolicyError as e:
            result = f"Denied: {e}"
            self._log(name, args, result, True)
            self._event("execute", "Executing Tool", "denied", tool=name)
            return self._after_failure(name, result)

        self._event("execute", "Executing Tool", "running", tool=name)
        result = str(await self.dispatch_fn(name, args))
        failed = default_failure(result)
        if failed and _TRANSIENT_RE.search(result):
            await self.sleep(TRANSIENT_BACKOFF_S)             # one automatic retry for flaky networks
            self._log(name, args, result, True)
            result = str(await self.dispatch_fn(name, args))
            failed = default_failure(result)
        self._log(name, args, result, failed)
        self._event("execute", "Executing Tool", "failed" if failed else "success", tool=name)
        if not failed:
            if name == "write_file":
                try:
                    self.evidence.files_written.append(self.policy.resolve(args.get("path"), write=True))
                except PolicyError:
                    pass
            return result
        return self._after_failure(name, result)

    def _after_failure(self, name: str, result: str) -> str:
        tid = self._current_task_id()
        n = self._task_failures.get(tid, 0) + 1
        self._task_failures[tid] = n
        hint = diagnose(result)
        if tid is not None and n > MAX_RETRIES:
            try:
                self.store.fail(tid, f"gave up after {MAX_RETRIES} retries; last error: {str(result)[:200]}")
            except Exception:
                pass
            return (f"{result}\n[Autonomous — not from the user] Task #{tid} failed {n} times, so it was "
                    f"marked failed. Move on: call task_next.")
        left = MAX_RETRIES - n + 1
        return (f"{result}\n[Autonomous — not from the user] Failure {n} on this task "
                f"({max(left, 0)} retr{'y' if left == 1 else 'ies'} left). Diagnosis: {hint}")

    # ---- verification ----
    def verify_success(self, goal: str, evidence: Evidence) -> VerificationResult:
        """Check the real state of the system, not the model's claim."""
        checks: List[Check] = []

        tasks = evidence.tasks
        unfinished = [t for t in tasks if t.get("status") != "done"]
        checks.append(Check(
            "tasks", not unfinished and evidence.termination in ("task_complete", "turn_complete"),
            "all planned tasks are done" if not unfinished else
            "not done: " + "; ".join(f"#{t['id']} {t['description'][:60]} ({t['status']})" for t in unfinished[:4])))

        did_work = any(not e["failed"] and e["name"] in AUTONOMOUS_TOOLS for e in evidence.tool_log)
        checks.append(Check("work", did_work, "at least one tool call succeeded" if did_work else
                            "no tool call succeeded, so nothing was actually done"))

        # Files: every promised file exists inside the sandbox and isn't empty.
        expected: List[str] = []
        problems: List[str] = []
        for tok in _PATH_TOKEN_RE.findall(goal or ""):
            if "/" in tok or "\\" in tok:
                expected.append(tok)
            else:                                           # bare name: wherever it was written, else sandbox/
                hit = next((p for p in evidence.files_written if os.path.basename(p).lower() == tok.lower()), None)
                expected.append(hit or f"sandbox/{tok}")
        if evidence.output_file:
            expected.append(evidence.output_file)
        expected += evidence.files_written
        seen, resolved = set(), []
        for p in expected:
            try:
                ap = p if os.path.isabs(p) else self.policy.resolve(p, write=True)
            except PolicyError as e:
                problems.append(f"{p}: {e}")
                continue
            key = _real(ap)
            if key not in seen:
                seen.add(key)
                resolved.append(ap)
        if wants_file(goal) and not resolved and not problems:
            problems.append("the goal asks to save a file but no file was written")
        for ap in resolved:
            rel = _display(ap, self.policy.workspace_root)
            if not os.path.isfile(ap):
                problems.append(f"{rel} does not exist")
            elif os.path.getsize(ap) == 0:
                problems.append(f"{rel} is empty")
        if resolved or problems or wants_file(goal):
            checks.append(Check("files", not problems,
                                "; ".join(problems) if problems else
                                "exists and not empty: " + ", ".join(
                                    _display(p, self.policy.workspace_root) for p in resolved)))

        # Prices: a real currency amount, and one that a tool actually returned.
        if wants_price(goal):
            claimed_text = evidence.final_text
            file_text = ""
            for ap in resolved:
                try:
                    with open(ap, "r", encoding="utf-8", errors="replace") as f:
                        file_text += "\n" + f.read(200_000)
                except OSError:
                    pass
            claimed = _prices(claimed_text + file_text)
            observed = set(_prices(evidence.observed_text()))
            if not claimed:
                checks.append(Check("price", False, "no $ amount in the answer or the saved file"))
            else:
                grounded = [p for p in claimed if p in observed]
                checks.append(Check(
                    "price", bool(grounded),
                    f"${grounded[0]:.2f} appears in a tool result" if grounded else
                    "the price(s) " + ", ".join(f"${p:.2f}" for p in claimed[:3])
                    + " don't appear in any tool result; they may be invented"))
                if resolved and wants_file(goal):
                    in_file = bool(_prices(file_text))
                    checks.append(Check("price in file", in_file,
                                        "the saved file contains a price" if in_file else
                                        "the saved file has no $ amount in it"))

        return VerificationResult(passed=all(c.passed for c in checks), checks=checks)

    # ---- run ----
    def _messages(self, base_messages, extra=None):
        block = _EXEC_RULES
        skill_block = skills_manager.format_for_prompt(self.skill)
        if skill_block:
            block += "\n\n" + skill_block
        msgs = [dict(m) for m in (base_messages or [])]
        msgs.append({"role": "system", "content": block})
        msgs.append({"role": "user", "content": f"Goal: {self.goal}"})
        return msgs + list(extra or [])

    def _make_harness(self):
        if self.harness_factory is not None:
            return self.harness_factory(self)
        from modules.harness.loop import AgentHarness
        return AgentHarness(
            self.chat_fn, self.gate, tools=self.tools, store=self.store, session_id=self.session_id,
            recover_fn=self.recover_fn, failure_fn=default_failure,
            mutating_tools=set(WRITE_TOOLS | COMMAND_TOOLS), max_safety_steps=self.max_steps,
            time_budget_s=self.time_budget_s, on_step=self.on_step, tasks_dir=self.tasks_dir)

    async def run(self, base_messages=None) -> AutonomousResult:
        if not self.goal:
            return AutonomousResult("error", "Give me a goal: !auto <what you want done>.", self.session_id)
        self.skill = skills_manager.find_relevant(self.goal, self.skills_dir)
        self._event("plan", "Planning", "running", goal=self.goal[:200],
                    skill=self.skill.name if self.skill else None)
        self.plan = await self.make_plan()
        self.evidence.output_file = self.plan.output_file

        self._event("plan", "Waiting for plan approval", "waiting", steps=len(self.plan.steps))
        if not await self.confirm_fn("autonomous_plan", self.plan_preview(self.plan)):
            self._event("plan", "Plan declined", "declined")
            return AutonomousResult("declined", "Okay, I won't start that task.", self.session_id)
        self._event("plan", "Plan approved", "success", steps=len(self.plan.steps))
        self.policy.plan_approved = True
        os.makedirs(self.policy.sandbox_root, exist_ok=True)

        prev = None
        for s in self.plan.steps:
            t = self.store.add(s["description"], depends_on=[prev] if prev else None,
                               acceptance_criteria=s.get("acceptance") or "")
            prev = t["id"]

        harness = self._make_harness()
        extra: List[Dict[str, Any]] = []
        verification: Optional[VerificationResult] = None
        rounds = 0
        for rounds in range(1, MAX_VERIFY_ROUNDS + 1):
            self._event("execute", f"Executing plan (round {rounds})", "running", round=rounds)
            try:
                res = await harness.run(self.goal, self._messages(base_messages, extra), seed=False)
            except Exception as e:
                self._event("execute", "Run crashed", "failed", error=type(e).__name__)
                return AutonomousResult("error", f"The autonomous run crashed: {type(e).__name__}: {e}",
                                        self.session_id, rounds, evidence=self.evidence)
            self.evidence.final_text = res.final_text
            self.evidence.termination = res.termination_reason
            self.evidence.tasks = self.store.list()
            self._event("verify", "Verifying", "running", round=rounds)
            verification = self.verify_success(self.goal, self.evidence)
            self._event("verify", "Verification passed" if verification.passed else "Verification failed",
                        "success" if verification.passed else "failed", round=rounds,
                        checks=[{"name": c.name, "passed": c.passed} for c in verification.checks])
            _log_line(self.tasks_dir, {"session_id": self.session_id, "round": rounds,
                                       "termination": res.termination_reason, "passed": verification.passed,
                                       "checks": [c.__dict__ for c in verification.checks]})
            if verification.passed or rounds == MAX_VERIFY_ROUNDS:
                break
            if res.termination_reason in ("time_budget", "safety_cap", "error"):
                break
            feedback = verification.feedback()
            self.store.add("Fix what independent verification found:\n" + feedback,
                           acceptance_criteria="Every verification check passes.")
            extra = [{"role": "assistant", "content": res.final_text},
                     {"role": "user", "content": "[Verification — checked by code, not from the user] "
                                                 "Your work did not pass:\n" + feedback
                                                 + "\nFix it. A new task was added for this."}]

        skill_path = None
        if verification is not None and verification.passed:
            skill_path = await skills_manager.record_success(
                self.goal, [s["description"] for s in self.plan.steps], self.evidence.tool_log,
                llm=self.llm_fn or self._ask, skills_dir=self.skills_dir)
            status = "verified"
            reply = self.evidence.final_text
        else:
            status = "unverified"
            reply = (f"{self.evidence.final_text}\n\nI couldn't confirm this worked:\n"
                     f"{verification.feedback() if verification else '- the run did not finish'}")
        self._event("done", "Verified" if status == "verified" else "Finished unverified",
                    "success" if status == "verified" else "failed", rounds=rounds)
        return AutonomousResult(status, reply, self.session_id, rounds, verification, skill_path, self.evidence)


# ─── helpers ─────────────────────────────────────────────────────────

async def _maybe_await(v):
    return await v if asyncio.iscoroutine(v) or isinstance(v, asyncio.Future) else v


def _display(path: str, root: str) -> str:
    try:
        return os.path.relpath(path, root).replace("\\", "/")
    except ValueError:                                  # another drive
        return path


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, ""))
    except ValueError:
        return default


def _log_line(tasks_dir: str, entry: Dict[str, Any]) -> None:
    try:
        os.makedirs(tasks_dir, exist_ok=True)
        entry = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), **entry}
        with open(os.path.join(tasks_dir, "verification.log"), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass

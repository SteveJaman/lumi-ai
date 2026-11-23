"""Phase 4 acceptance criteria for the agent harness (see data/tasks/REFACTOR_REPORT.md).

AC1  20-step synthetic task completes; MAX_AGENT_STEPS no longer caps runs
AC2  kill the process mid-task; restart; the agent resumes from the persisted DAG
AC3  a PreToolUse hook denying run_command blocks the call in YOLO mode
AC4  90% context fill -> compaction fires and the archive file exists
AC5  Explore subagent: main context grows by ~1 message, not the subagent transcript
AC6  30-step synthetic task ends with termination_reason = task_complete
AC7  two consecutive text-only turns -> termination_reason = model_stalled
AC8  run_command emitting 50K chars -> 4K in context, full output in tool_outputs/
AC9  baseline suite still passes (the full pytest run; not a test here)
AC10 chat and plan (legacy one-shot) turns keep their message-history shapes
     (tests/test_mode_shapes.py)
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from modules.harness import config
from modules.harness.context import MAX_TOOL_CHARS, estimate_tokens
from modules.harness.hooks import HookRegistry, PRE_TOOL_USE
from modules.harness.loop import AgentHarness
from modules.harness.subagents import SubagentRunner
from modules.harness.tasks import TaskStore, TASK_TOOL_SCHEMAS
from modules.agent_tools import TOOL_SCHEMAS
from modules.llm_providers import LLMResponse, ToolCall

ROOT = Path(__file__).resolve().parent.parent


def tc(name, **a):
    return LLMResponse(tool_calls=[ToolCall(name=name, arguments=a)])


class Script:
    def __init__(self, items):
        self.items, self.seen = list(items), []

    def __call__(self, messages, tools):
        self.seen.append([dict(m) for m in messages])
        return self.items.pop(0) if self.items else LLMResponse(content="(exhausted)")


def _harness(tmp_path, chat, dispatch=lambda n, a: "ok", **kw):
    kw.setdefault("store", TaskStore(str(tmp_path / "lumi_tasks.json")))
    kw.setdefault("hooks", HookRegistry(str(tmp_path / "hooks")))
    kw.setdefault("mutating_tools", {"write_file", "run_command"})
    return AgentHarness(chat, dispatch, tasks_dir=str(tmp_path), rules="1. Always call task_next.", **kw)


def _run(h, goal="synthetic goal"):
    return asyncio.run(h.run(goal, [{"role": "system", "content": "SYS"}, {"role": "user", "content": goal}]))


def _synthetic(n_steps):
    """task_next, (n-2) file writes spread over 3 subtasks, then the last task_complete."""
    items = [tc("task_add", description="part 1", depends_on=[1]),
             tc("task_add", description="part 2", depends_on=[2]),
             tc("task_next"), tc("task_complete", id=1, result="planned"),
             tc("task_next")]
    writes = n_steps - len(items) - 3                 # 3 more bookkeeping calls follow
    items += [tc("write_file", path=f"p1_{i}.txt", content="x") for i in range(writes // 2)]
    items += [tc("task_complete", id=2, result="part 1 done"), tc("task_next")]
    items += [tc("write_file", path=f"p2_{i}.txt", content="x") for i in range(writes - writes // 2)]
    items += [tc("task_complete", id=3, result="synthetic task done")]
    assert len(items) == n_steps
    return items


def test_ac1_twenty_step_task_not_capped(tmp_path):
    from modules import prompter
    items = _synthetic(20)
    h = _harness(tmp_path, Script(items))
    res = _run(h)
    assert res.termination_reason == "task_complete"
    assert res.steps == len(items) == 20 > prompter.MAX_AGENT_STEPS


def test_ac2_kill_and_resume(tmp_path):
    child = str(ROOT / "tests" / "harness_child.py")
    ready = tmp_path / "ready"
    p = subprocess.Popen([sys.executable, child, str(tmp_path), "start", str(ready)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.time() + 60
    while not ready.exists() and time.time() < deadline and p.poll() is None:
        time.sleep(0.1)
    assert ready.exists(), p.stderr.read().decode(errors="replace") if p.poll() is not None else "child never got ready"
    p.kill()                                   # hard kill mid-task
    p.wait(timeout=20)
    store = TaskStore(str(tmp_path / "lumi_tasks.json"))
    before = {t["id"]: t["status"] for t in store.list()}
    assert before == {1: "done", 2: "in_progress", 3: "pending", 4: "pending"}

    out = subprocess.run([sys.executable, child, str(tmp_path), "resume", str(ready)],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout.strip().splitlines()[-1])
    assert res["termination_reason"] == "task_complete"
    assert "#2 sub A" in res["first_state"]          # resumed the interrupted task first
    assert set(res["statuses"].values()) == {"done"} and len(res["statuses"]) == 4


def test_ac3_pre_hook_blocks_run_command_in_yolo(tmp_path, monkeypatch):
    from tests.test_harness_hooks import _prompter_run, DENY_RUN_COMMAND
    items = [tc("task_next"), tc("run_command", command="echo hi"), tc("task_fail", id=1, reason="denied")]
    reply, executed, confirmed, seen = _prompter_run(tmp_path, monkeypatch, items, yolo=True,
                                                     hook_src=DENY_RUN_COMMAND)
    assert os.environ.get("LUMI_AGENT_YOLO") == "1"
    assert executed == [] and confirmed == []
    assert any("run_command is disabled by policy" in str(m.get("content")) for m in seen[2])


def test_ac4_ninety_percent_fill_compacts_and_archives(tmp_path):
    num_ctx = 8192
    filler = "f" * int(num_ctx * 0.9 * 4)               # ≈90% of the window in one message
    s = Script([tc("task_next"), tc("task_complete", id=1, result="ok")])
    h = _harness(tmp_path, s, num_ctx=num_ctx, session_id="ac4")
    res = asyncio.run(h.run("x", [{"role": "system", "content": "SYS"},
                                  {"role": "user", "content": "earlier"},
                                  {"role": "assistant", "content": filler},
                                  {"role": "user", "content": "x"}]))
    assert res.compactions >= 1
    archive = tmp_path / "transcripts" / "ac4.jsonl"
    assert archive.exists()
    archived = [json.loads(l) for l in archive.read_text(encoding="utf-8").splitlines()]
    assert any(m.get("content") == filler for m in archived)          # full content preserved
    assert estimate_tokens(s.seen[0]) < num_ctx * 0.8                  # the call itself fit


def test_ac5_explore_adds_one_message_to_main_context(tmp_path):
    from tests.test_harness_subagents import Router
    explore = [tc("list_dir", path="."), tc("search_files", query="loop"), tc("read_file", path="a.py"),
               tc("read_file", path="b.py"), LLMResponse(content="a.py: loop\nb.py: config")]
    r = Router(main=[tc("task_next"), tc("spawn_subagent", role="Explore", prompt="map it"),
                     tc("task_complete", id=1, result="ok")], sub={"Explore": explore})
    store = TaskStore(str(tmp_path / "lumi_tasks.json"))
    runner = SubagentRunner(r, lambda n, a: "y" * 3000, TOOL_SCHEMAS + TASK_TOOL_SCHEMAS, store,
                            tasks_dir=str(tmp_path))
    h = _harness(tmp_path, r, store=store, subagents=runner)
    assert _run(h).termination_reason == "task_complete"
    before, after = r.main_seen[1], r.main_seen[2]
    new = after[len(before) - 1:-1]                   # excluding the ephemeral task-state message
    assert [m["role"] for m in new] == ["assistant", "tool"]
    assert new[1]["content"] == "a.py: loop\nb.py: config"


def test_ac6_thirty_step_task_complete(tmp_path):
    items = _synthetic(30)
    res = _run(_harness(tmp_path, Script(items)))
    assert res.termination_reason == "task_complete" and res.steps == 30


def test_ac7_two_text_only_turns_model_stalled(tmp_path):
    s = Script([tc("task_next"), LLMResponse(content="I'll get right on it."),
                LLMResponse(content="Sure, here's what I'd do...")])
    res = _run(_harness(tmp_path, s))
    assert res.termination_reason == "model_stalled"
    log = (tmp_path / "sessions.log").read_text(encoding="utf-8").splitlines()
    assert json.loads(log[-1])["termination_reason"] == "model_stalled"


def test_ac8_run_command_50k_truncated(tmp_path):
    big = "\n".join(("line %05d " % i) + "z" * 489 for i in range(100))
    assert len(big) >= 50_000
    s = Script([tc("task_next"), tc("run_command", command="python noisy.py"),
                tc("task_complete", id=1, result="ok")])
    h = _harness(tmp_path, s, dispatch=lambda n, a: big, session_id="ac8")
    _run(h)
    tool_msg = [m for m in s.seen[2] if m.get("role") == "tool" and m.get("name") == "run_command"][0]
    body = tool_msg["content"].split("\n[truncated;")[0]
    assert len(body) <= MAX_TOOL_CHARS and "[truncated;" in tool_msg["content"]
    files = list((tmp_path / "tool_outputs" / "ac8").iterdir())
    assert len(files) == 1 and files[0].read_text(encoding="utf-8") == big


def test_ac10_mode_shapes(monkeypatch):
    from tests.test_mode_shapes import test_chat_and_legacy_agent_shapes_unchanged
    test_chat_and_legacy_agent_shapes_unchanged(monkeypatch)

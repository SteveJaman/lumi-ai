"""Goal-driven agent loop (modules/harness/loop.py), driven by scripted fake models."""

import asyncio
import json

import pytest

from modules.harness.loop import AgentHarness, CONTINUE_PROMPT
from modules.harness.tasks import TaskStore
from modules.llm_providers import LLMResponse, ToolCall


def call(name, **args):
    return LLMResponse(tool_calls=[ToolCall(name=name, arguments=args)])


def text(s):
    return LLMResponse(content=s)


class Script:
    """A fake model: each item is an LLMResponse or a callable(messages) -> LLMResponse."""

    def __init__(self, items):
        self.items = list(items)
        self.seen = []

    def __call__(self, messages, tools):
        self.seen.append([dict(m) for m in messages])
        item = self.items.pop(0) if self.items else text("(script exhausted)")
        return item(messages) if callable(item) else item


@pytest.fixture
def env(tmp_path):
    store = TaskStore(str(tmp_path / "lumi_tasks.json"))
    dispatched = []

    async def dispatch(name, args):
        dispatched.append((name, args))
        return f"ok:{name}"

    def make(script, **kw):
        kw.setdefault("tasks_dir", str(tmp_path))
        kw.setdefault("rules", "1. Always call task_next.")
        kw.setdefault("mutating_tools", {"write_file", "run_command"})
        return AgentHarness(script, dispatch, tools=[], store=store, **kw)

    return store, dispatched, make, tmp_path


def run(h, goal, base=None):
    return asyncio.run(h.run(goal, base or [{"role": "system", "content": "SYS"},
                                            {"role": "user", "content": goal}]))


def _sessions(tmp_path):
    return [json.loads(l) for l in (tmp_path / "sessions.log").read_text(encoding="utf-8").splitlines()]


def test_long_task_is_not_capped_at_six_steps(env):
    store, dispatched, make, tmp = env
    n = 20
    items = [call("task_next")]
    items += [call("write_file", path=f"f{i}.txt", content="x") for i in range(n - 2)]
    items += [call("task_complete", id=1, result="wrote everything")]
    h = make(Script(items))
    res = run(h, "write 18 files")
    assert res.termination_reason == "task_complete"
    assert res.steps == n
    assert len(dispatched) == n - 2
    assert res.final_text == "wrote everything"
    log = _sessions(tmp)[-1]
    assert log["termination_reason"] == "task_complete" and log["steps"] == n and "duration_s" in log


def test_rules_and_task_state_are_injected_but_state_is_ephemeral(env):
    store, _, make, _ = env
    s = Script([call("task_next"), call("task_complete", id=1, result="done")])
    h = make(s)
    run(h, "do it")
    first = s.seen[0]
    assert first[0]["content"] == "SYS"
    assert first[1]["content"].startswith("[LUMI.md RULES]")
    assert first[-1]["content"].startswith("[TASK STORE")
    # the task-state message is rebuilt each call, never stored
    assert not any(m.get("content", "").startswith("[TASK STORE") for m in h.messages)
    assert "in progress" in s.seen[1][-1]["content"]


def test_two_text_only_turns_stall(env):
    _, _, make, _ = env
    s = Script([call("task_next"), text("I will now do it."), text("Here is the answer in prose.")])
    res = run(make(s), "do a thing")
    assert res.termination_reason == "model_stalled"
    # exactly one re-prompt between the two text turns
    assert sum(1 for m in s.seen[2] if m.get("content") == CONTINUE_PROMPT) == 1


def test_single_text_turn_reprompts_then_recovers(env):
    _, _, make, _ = env
    s = Script([call("task_next"), text("thinking..."), call("task_complete", id=1, result="r")])
    res = run(make(s), "x")
    assert res.termination_reason == "task_complete"


def test_text_turn_with_no_open_task_is_turn_complete(env):
    store, _, make, _ = env
    s = Script([text("Hi! Nothing to do.")])
    h = make(s)
    store.clear()
    res = asyncio.run(h.run("", [{"role": "system", "content": "SYS"}]))
    assert res.termination_reason == "turn_complete"
    assert res.final_text == "Hi! Nothing to do."


def test_stagnation(env):
    _, _, make, _ = env
    s = Script([call("task_next")] + [call("read_file", path=f"r{i}") for i in range(10)])
    res = run(make(s), "look around forever")
    assert res.termination_reason == "stagnant"
    assert res.steps == 5


def test_safety_cap(env):
    _, _, make, _ = env
    s = Script([call("write_file", path=f"w{i}", content="x") for i in range(50)])
    res = run(make(s, max_safety_steps=12), "write forever")
    assert res.termination_reason == "safety_cap" and res.steps == 12


def test_time_budget(env):
    _, _, make, _ = env
    t = [0.0]

    def clock():
        t[0] += 10
        return t[0]

    s = Script([call("write_file", path=f"w{i}", content="x") for i in range(50)])
    res = run(make(s, time_budget_s=55, clock=clock), "slow")
    assert res.termination_reason == "time_budget"


def test_task_fail_ends_with_task_failed(env):
    _, _, make, _ = env
    s = Script([call("task_next"), call("task_fail", id=1, reason="disk is read-only")])
    res = run(make(s), "x")
    assert res.termination_reason == "task_failed"
    assert "disk is read-only" in res.final_text


def test_subtasks_are_worked_in_dependency_order(env):
    store, dispatched, make, _ = env
    s = Script([
        call("task_add", description="step a"),
        call("task_add", description="step b", depends_on=[2]),
        call("task_next"),                              # root #1 first
        call("task_complete", id=1, result="planned"),
        call("task_next"),                              # #2
        call("write_file", path="a", content="1"),
        call("task_complete", id=2, result="a done"),
        call("task_next"),                              # #3
        call("write_file", path="b", content="2"),
        call("task_complete", id=3, result="b done"),
    ])
    res = run(make(s), "two-step job")
    assert res.termination_reason == "task_complete"
    assert [t["status"] for t in store.list()] == ["done", "done", "done"]


def test_text_form_tool_calls_are_recovered(env):
    _, _, make, _ = env

    def recover(content):
        if content.startswith("{"):
            o = json.loads(content)
            return [{"function": {"name": o["name"], "arguments": o["arguments"]}}]
        return []

    s = Script([text('{"name": "task_next", "arguments": {}}'),
                text('{"name": "task_complete", "arguments": {"id": 1, "result": "ok"}}')])
    res = run(make(s, recover_fn=recover), "x")
    assert res.termination_reason == "task_complete"


def test_resume_after_restart_continues_in_progress_task(env):
    store, dispatched, make, _ = env
    # First "process": works on the task, then dies mid-way.
    s1 = Script([call("task_add", description="sub", depends_on=[1]),
                 call("task_next"), call("write_file", path="half", content="1")])

    class Killed(Exception):
        pass

    def dying(messages, tools):
        if not s1.items:
            raise Killed()
        return s1(messages, tools)

    h1 = make(dying)
    res1 = run(h1, "big job")
    assert res1.termination_reason == "error"
    assert store.current()["id"] == 1

    # Restart: a fresh harness over the same file resumes, adding no new task.
    store2 = TaskStore(store.path)
    s2 = Script([call("task_next"), call("task_complete", id=1, result="finished"),
                 call("task_next"), call("task_complete", id=2, result="sub done")])
    h2 = AgentHarness(s2, lambda n, a: "ok", store=store2, tasks_dir=make(Script([])).tasks_dir,
                      rules="r")
    res2 = asyncio.run(h2.run("continue", [{"role": "system", "content": "SYS"}]))
    assert res2.termination_reason == "task_complete"
    assert len(store2.list()) == 2
    assert "#1" in s2.seen[0][-1]["content"]


def test_model_error_terminates_with_error(env):
    _, _, make, _ = env

    def boom(messages, tools):
        raise RuntimeError("ollama down")

    res = run(make(boom), "x")
    assert res.termination_reason == "error" and "ollama down" in res.final_text

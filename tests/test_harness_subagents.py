"""Subagents (modules/harness/subagents.py)."""

import asyncio
import json

import pytest

from modules.harness.context import estimate_tokens
from modules.harness.loop import AgentHarness
from modules.harness.subagents import SubagentRunner, command_is_read_only, ROLES, SPAWN_SUBAGENT_SCHEMA
from modules.harness.tasks import TaskStore, TASK_TOOL_SCHEMAS
from modules.agent_tools import TOOL_SCHEMAS
from modules.llm_providers import LLMResponse, ToolCall


def tc(name, **a):
    return LLMResponse(tool_calls=[ToolCall(name=name, arguments=a)])


class Router:
    """Fake model shared by main agent and subagents; routes by system prompt."""

    def __init__(self, main, sub):
        self.main, self.sub = list(main), dict((k, list(v)) for k, v in sub.items())
        self.main_seen, self.sub_seen = [], []

    def __call__(self, messages, tools):
        sys0 = messages[0].get("content", "")
        for role in self.sub:
            if sys0.startswith(f"You are a{'n' if role[0] in 'AEIOU' else ''} {role} subagent"):
                self.sub_seen.append((role, [t["function"]["name"] for t in tools]))
                q = self.sub[role]
                return q.pop(0) if q else LLMResponse(content="(empty)")
        self.main_seen.append([dict(m) for m in messages])
        return self.main.pop(0) if self.main else LLMResponse(content="done")


@pytest.fixture
def store(tmp_path):
    return TaskStore(str(tmp_path / "t.json"))


def _runner(tmp_path, store, chat, dispatch=None):
    return SubagentRunner(chat, dispatch or (lambda n, a: f"{n} result"),
                          TOOL_SCHEMAS + TASK_TOOL_SCHEMAS, store, tasks_dir=str(tmp_path), session_id="s")


def test_explore_returns_only_final_answer_and_main_context_grows_by_one(tmp_path, store):
    explore_transcript = [tc("list_dir", path="."), tc("search_files", query="MAX_AGENT_STEPS"),
                          tc("read_file", path="modules/prompter.py"), tc("read_file", path="constants.py"),
                          LLMResponse(content="modules/prompter.py: agent loop\nconstants.py: models")]
    r = Router(main=[tc("task_next"), tc("spawn_subagent", role="Explore", prompt="find the agent loop"),
                     tc("task_complete", id=1, result="mapped")],
               sub={"Explore": explore_transcript})
    runner = _runner(tmp_path, store, r)
    h = AgentHarness(r, lambda n, a: "x" * 3000, store=store, tasks_dir=str(tmp_path), rules="r",
                     subagents=runner, mutating_tools=set())
    res = asyncio.run(h.run("map the code", [{"role": "system", "content": "S"}]))
    assert res.termination_reason == "task_complete"
    before, after = r.main_seen[1], r.main_seen[2]
    # +1 assistant tool-call message and +1 tool result, nothing from the subagent's 5 turns
    assert len(after) - len(before) == 2
    result_msg = after[-2]
    assert result_msg["role"] == "tool" and result_msg["name"] == "spawn_subagent"
    assert result_msg["content"].startswith("modules/prompter.py: agent loop")
    assert estimate_tokens(after) - estimate_tokens(before) < 100
    # subagent saw only its role's tools
    assert all(set(tools) == set(ROLES["Explore"]["tools"]) for role, tools in r.sub_seen)
    log = [json.loads(l) for l in (tmp_path / "subagents.log").read_text(encoding="utf-8").splitlines()]
    assert log[-1]["role"] == "Explore" and log[-1]["status"] == "ok"
    assert {"prompt_hash", "duration_s", "tokens"} <= set(log[-1])


def test_subagent_cannot_use_tools_outside_its_role(tmp_path, store):
    ran = []
    r = Router(main=[], sub={"Explore": [tc("write_file", path="x", content="y"), LLMResponse(content="ok")]})
    runner = _runner(tmp_path, store, r, lambda n, a: ran.append(n) or "ok")
    out = asyncio.run(runner.spawn("Explore", "look"))
    assert out == "ok" and ran == []


def test_plan_adds_tasks_and_returns_dag_json(tmp_path, store):
    r = Router(main=[], sub={"Plan": [tc("task_add", description="a"), tc("task_add", description="b", depends_on=[1]),
                                      LLMResponse(content="I added the tasks.")]})
    out = json.loads(asyncio.run(_runner(tmp_path, store, r).spawn("Plan", "plan it")))
    assert [t["description"] for t in out["tasks"]] == ["a", "b"]
    assert out["tasks"][1]["depends_on"] == [1]


def test_verify_returns_pass_evidence_and_blocks_writes(tmp_path, store):
    ran = []
    r = Router(main=[], sub={"Verify": [tc("run_command", command="echo x > out.txt"),
                                        tc("run_command", command="python -m pytest -q tests/test_x.py"),
                                        LLMResponse(content='Result: {"pass": true, "evidence": "3 passed"}')]})
    out = asyncio.run(_runner(tmp_path, store, r, lambda n, a: ran.append(a["command"]) or "3 passed").spawn(
        "Verify", "are tests green?"))
    assert json.loads(out) == {"pass": True, "evidence": "3 passed"}
    assert ran == ["python -m pytest -q tests/test_x.py"]


def test_verify_without_json_is_a_fail(tmp_path, store):
    r = Router(main=[], sub={"Verify": [LLMResponse(content="looks fine to me")]})
    out = json.loads(asyncio.run(_runner(tmp_path, store, r).spawn("Verify", "check")))
    assert out["pass"] is False and "looks fine" in out["evidence"]


def test_timeout(tmp_path, store):
    async def slow(messages, tools):
        await asyncio.sleep(5)

    out = asyncio.run(_runner(tmp_path, store, slow).spawn("Explore", "x", timeout=0.2))
    assert "timed out" in out


@pytest.mark.parametrize("cmd,ok", [
    ("git status", True), ("python -m pytest -q", True), ("git diff && git log -3", True),
    ("ls | grep foo", True), ("rm x", False), ("echo hi > f", False), ("pip install x", False),
    ("git push", False), ("python script.py", False), ("cat a; rm b", False),
])
def test_read_only_classifier(cmd, ok):
    assert command_is_read_only(cmd) is ok


def test_schema_is_short():
    d = SPAWN_SUBAGENT_SCHEMA["function"]["description"]
    assert d.count(". ") < 2 and set(SPAWN_SUBAGENT_SCHEMA["function"]["parameters"]["properties"]) == \
        {"role", "prompt", "tools_allowed", "timeout"}


def test_prompter_offers_spawn_subagent_and_runs_explore(monkeypatch, tmp_path):
    from modules import prompter as P
    from modules.harness import config
    from tests.test_mode_shapes import _make_prompter
    monkeypatch.setattr(config, "TASKS_DIR", str(tmp_path / "tasks"))
    monkeypatch.setattr(config, "HOOKS_DIR", str(tmp_path / "hooks"))
    monkeypatch.setenv("LUMI_HARNESS", "1")
    p, _ = _make_prompter(monkeypatch)
    p.agent_mode_enabled = True

    async def fake_dispatch(name, args):
        return f"{name} ok"

    p._dispatch_tool_call = fake_dispatch
    r = Router(main=[tc("task_next"), tc("spawn_subagent", role="Explore", prompt="where is the loop?"),
                     tc("task_complete", id=1, result="found it")],
               sub={"Explore": [tc("search_files", query="run_agent_turn"), LLMResponse(content="prompter.py")]})
    tools_seen = []
    monkeypatch.setattr(P, "chat_sync", lambda model, messages, tools=None, **kw:
                        tools_seen.append([t["function"]["name"] for t in tools or []]) or r(messages, tools or []))
    assert asyncio.run(p.run_agent_turn("find the loop")) == "found it"
    assert "spawn_subagent" in tools_seen[0]
    assert (tmp_path / "tasks" / "subagents.log").exists()


def test_subagent_recovers_text_form_tool_calls(tmp_path, store):
    """Local models (qwen2.5-coder) write tool calls as JSON text; found in the live run."""
    from modules.prompter import _recover_text_tool_calls
    ran = []
    r = Router(main=[], sub={"Explore": [LLMResponse(content='{"name": "search_files", "arguments": {"query": "stagnant"}}'),
                                         LLMResponse(content="modules/harness/loop.py: termination reasons")]})
    runner = SubagentRunner(r, lambda n, a: ran.append((n, a)) or "loop.py:12: stagnant",
                            TOOL_SCHEMAS + TASK_TOOL_SCHEMAS, store, tasks_dir=str(tmp_path),
                            recover_fn=_recover_text_tool_calls)
    out = asyncio.run(runner.spawn("Explore", "where are termination reasons?"))
    assert ran == [("search_files", {"query": "stagnant"})]
    assert out == "modules/harness/loop.py: termination reasons"

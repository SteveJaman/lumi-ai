"""Workflow scripts (modules/harness/workflows.py)."""

import asyncio
import json

from modules.harness import workflows
from modules.harness.loop import AgentHarness
from modules.harness.subagents import SubagentRunner
from modules.harness.tasks import TaskStore, TASK_TOOL_SCHEMAS
from modules.agent_tools import TOOL_SCHEMAS
from modules.llm_providers import LLMResponse, ToolCall
from tests.test_harness_subagents import Router, tc, store, _runner  # noqa: F401 (fixture)


def _wf(tmp_path, data, name="wf"):
    d = tmp_path / "workflows"
    d.mkdir(exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")
    return str(d)


def test_workflow_bindings_and_until_loop(tmp_path):
    calls = []
    verdicts = iter(['{"pass": false, "evidence": "1 failed"}', '{"pass": true, "evidence": "all green"}'])

    async def spawn(role, prompt, tools_allowed=None, timeout=300):
        calls.append((role, prompt))
        if role == "Explore":
            return "FILEMAP"
        if role == "Plan":
            return '{"tasks": [{"id": 1}]}'
        return next(verdicts)

    d = _wf(tmp_path, {"inputs": ["goal"], "steps": [
        {"id": "e", "role": "Explore", "prompt": "find {{inputs.goal}}"},
        {"id": "p", "role": "Plan", "prompt": "plan {{inputs.goal}} using {{steps.e}}"},
        {"id": "v", "role": "Verify", "prompt": "check {{steps.p.tasks}}",
         "until": {"field": "pass", "equals": True}, "max_iterations": 3}]})
    res = asyncio.run(workflows.run("wf", {"goal": "G"}, spawn, d))
    assert calls[0] == ("Explore", "find G")
    assert calls[1] == ("Plan", "plan G using FILEMAP")
    assert calls[2][1].startswith('check [{"id": 1}]')
    assert res["iterations"]["v"] == 2 and res["status"] == "ok"
    assert json.loads(res["outputs"]["v"])["pass"] is True


def test_workflow_validation(tmp_path):
    d = _wf(tmp_path, {"steps": [{"id": "a", "role": "Explore", "prompt": "{{steps.b}}"}]}, "bad")
    out = asyncio.run(workflows.tool({"name": "bad"}, None, d))
    assert out.startswith("Error:") and "before it runs" in out
    assert asyncio.run(workflows.tool({"name": "nope"}, None, d)).startswith("Error: no workflow")
    assert asyncio.run(workflows.tool({"name": "../x"}, None, d)).startswith("Error: invalid")


def test_shipped_workflow_is_valid():
    wf = workflows.load("explore_plan_verify")
    assert [s["role"] for s in wf["steps"]] == ["Explore", "Plan", "Verify"]


def test_workflow_run_tool_in_harness(tmp_path, store):
    d = _wf(tmp_path, {"inputs": ["goal"], "steps": [
        {"id": "e", "role": "Explore", "prompt": "find {{inputs.goal}}"}]}, "mini")
    r = Router(main=[tc("task_next"), LLMResponse(tool_calls=[ToolCall(name="workflow_run", arguments={"name": "mini", "inputs": {"goal": "loop"}})]),
                     tc("task_complete", id=1, result="ok")],
               sub={"Explore": [LLMResponse(content="MAP")]})
    runner = _runner(tmp_path, store, r)
    h = AgentHarness(r, lambda n, a: "ok", store=store, tasks_dir=str(tmp_path), rules="r",
                     subagents=runner, workflows_dir=d)
    res = asyncio.run(h.run("x", [{"role": "system", "content": "S"}]))
    assert res.termination_reason == "task_complete"
    tool_msg = [m for m in r.main_seen[2] if m.get("name") == "workflow_run"][0]
    assert json.loads(tool_msg["content"])["outputs"]["e"] == "MAP"

"""Prompter <-> harness wiring: session agent mode runs modules/harness/."""

import asyncio
import json

import pytest

from modules.harness import config
from modules.llm_providers import LLMResponse, ToolCall
from tests.test_mode_shapes import _make_prompter


@pytest.fixture
def harness_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TASKS_DIR", str(tmp_path / "tasks"))
    monkeypatch.setattr(config, "HOOKS_DIR", str(tmp_path / "hooks"))
    monkeypatch.setenv("LUMI_HARNESS", "1")
    monkeypatch.delenv("LUMI_AGENT_YOLO", raising=False)
    return tmp_path


def _scripted(monkeypatch, p, items):
    from modules import prompter as P
    seen = []

    def fake_chat_sync(model, messages, tools=None, **kw):
        seen.append({"model": model, "messages": [dict(m) for m in messages],
                     "tools": [t["function"]["name"] for t in (tools or [])], "kw": kw})
        return items.pop(0) if items else LLMResponse(content="(done)")

    monkeypatch.setattr(P, "chat_sync", fake_chat_sync)
    return seen


def tc(name, **args):
    return LLMResponse(tool_calls=[ToolCall(name=name, arguments=args)])


def test_agent_mode_runs_harness_past_six_steps(monkeypatch, harness_env):
    p, _ = _make_prompter(monkeypatch)
    p.agent_mode_enabled = True
    ran = []

    async def fake_dispatch(name, args):
        ran.append(name)
        return "ok"

    p._dispatch_tool_call = fake_dispatch
    items = [tc("task_next")] + [tc("list_dir", path=".") if i % 2 else tc("write_file", path=f"f{i}", content="x")
                                 for i in range(10)] + [tc("task_complete", id=1, result="all files written")]
    seen = _scripted(monkeypatch, p, items)
    reply = asyncio.run(p.run_agent_turn("write ten files"))
    assert reply == "all files written"
    assert len(seen) == 12                       # past the legacy cap of 6
    assert {"task_next", "task_complete", "task_add"} <= set(seen[0]["tools"])
    assert seen[0]["messages"][1]["content"].startswith("[LUMI.md RULES]")
    assert seen[0]["model"] == config.agent_model()
    assert seen[0]["kw"]["options"]["num_ctx"] == config.num_ctx()
    log = (harness_env / "tasks" / "sessions.log").read_text(encoding="utf-8").splitlines()
    assert json.loads(log[-1])["termination_reason"] == "task_complete"


def test_text_form_tool_calls_recovered_in_prompter(monkeypatch, harness_env):
    p, _ = _make_prompter(monkeypatch)
    p.agent_mode_enabled = True
    items = [LLMResponse(content='{"name": "task_next", "arguments": {}}'),
             LLMResponse(content='{"name": "task_complete", "arguments": {"id": 1, "result": "done via text"}}')]
    _scripted(monkeypatch, p, items)
    assert asyncio.run(p.run_agent_turn("x")) == "done via text"


def test_stall_reply_mentions_resume(monkeypatch, harness_env):
    p, _ = _make_prompter(monkeypatch)
    p.agent_mode_enabled = True
    _scripted(monkeypatch, p, [tc("task_next"), LLMResponse(content="sure"), LLMResponse(content="ok")])
    reply = asyncio.run(p.run_agent_turn("x"))
    assert "model stalled" in reply and "continue" in reply


def test_harness_off_uses_legacy_loop(monkeypatch, harness_env):
    monkeypatch.setenv("LUMI_HARNESS", "0")
    p, _ = _make_prompter(monkeypatch)
    p.agent_mode_enabled = True
    seen = _scripted(monkeypatch, p, [LLMResponse(content="Here you go.")])
    asyncio.run(p.run_agent_turn("hello"))
    assert "task_next" not in seen[0]["tools"]

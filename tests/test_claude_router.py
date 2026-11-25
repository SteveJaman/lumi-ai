"""Claude smart router (modules/claude_router.py) and the Model Pipeline events."""

import asyncio
import json

import pytest

from modules import claude_router as cr
from modules import workspace
from modules import llm_providers
from modules.llm_providers import LLMResponse, ToolCall

REAL_SWITCH = workspace.emit_model_switch
REAL_TOKENS = workspace.emit_token_usage

TOOLS = [{"type": "function", "function": {"name": "read_file", "description": "Read a file.",
                                            "parameters": {"type": "object", "properties": {}}}}]


class FakeClient:
    """Answers per model id; a value that is an Exception is raised."""

    def __init__(self, replies, configured=True):
        self.replies = replies
        self.calls = []
        self.configured = configured

    def is_configured(self):
        return self.configured

    def chat(self, messages, tools=None, think=None, options=None, keep_alive=None, model=""):
        self.calls.append({"model": model, "tools": tools, "options": options, "messages": messages})
        r = self.replies[model]
        if isinstance(r, list):
            r = r.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def reply(text="ok", calls=None, inp=10, out=5, stop="end_turn"):
    return LLMResponse(content=text, tool_calls=calls or [],
                       raw={"usage": {"input_tokens": inp, "output_tokens": out}, "stop_reason": stop},
                       provider="anthropic")


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "USAGE_PATH", str(tmp_path / "claude_usage.json"))
    events = []
    monkeypatch.setattr(workspace, "emit_model_switch",
                        lambda tier, mid, reason, cost: events.append(("switch", tier, mid, reason, cost)))
    monkeypatch.setattr(workspace, "emit_token_usage",
                        lambda tier, mid, i, o, totals=None, classifier=False:
                        events.append(("tokens", tier, i, o, totals, classifier)))
    return events


M = cr.models()


# ─── classification ─────────────────────────────────────────────

@pytest.mark.parametrize("prompt,tier", [
    ("hey lumi!", "haiku"),
    ("thanks", "haiku"),
    ("what time is it in Tokyo", "haiku"),
    ("write a python script that renames my photos by date", "sonnet"),
    ("refactor this function to use a dict", "sonnet"),
    ("Design the architecture for my app. I keep hitting a race condition and a memory leak "
     "when I debug the worker pool.", "opus"),
])
def test_heuristic_clear_cases(prompt, tier):
    assert cr.heuristic(prompt)[0] == tier


def test_code_block_and_length_push_up():
    big = "Here is my code:\n```python\n" + "def f(x):\n    return x\n" * 150 + "```\nWhy does it fail?"
    assert cr.heuristic(big)[0] in ("sonnet", "opus")
    assert "code block" in cr.heuristic(big)[1]


def test_ambiguous_prompt_uses_haiku_micro_call(monkeypatch):
    fake = FakeClient({M["haiku"]: reply("opus", inp=40, out=1)})
    monkeypatch.setattr(cr, "_client", lambda: fake)
    prompt = "x " * 100                            # mid-length, no keywords: score 0
    assert cr.heuristic(prompt)[0] is None
    assert cr.classify_complexity(prompt) == "opus"
    call = fake.calls[0]
    assert call["model"] == M["haiku"]
    assert call["options"]["num_predict"] == cr.CLASSIFIER_MAX_TOKENS
    assert call["options"]["anthropic_payload"] == {"thinking": {"type": "disabled"}}
    usage = cr.usage_summary()
    assert usage["classifier_calls"] == 1 and usage["tiers"]["haiku"]["calls"] == 1


def test_classifier_failure_falls_back_to_sonnet(monkeypatch):
    fake = FakeClient({M["haiku"]: RuntimeError("boom")})
    monkeypatch.setattr(cr, "_client", lambda: fake)
    assert cr.classify_complexity("x " * 100) == "sonnet"


def test_no_key_means_no_micro_call(monkeypatch):
    fake = FakeClient({}, configured=False)
    monkeypatch.setattr(cr, "_client", lambda: fake)
    assert cr.classify_complexity("x " * 100) == "sonnet"
    assert fake.calls == []


# ─── calling ────────────────────────────────────────────────────

def test_simple_chat_goes_to_haiku(isolate):
    fake = FakeClient({M["haiku"]: reply("hi!")})
    resp = asyncio.run(cr.call_claude([{"role": "user", "content": "hey lumi"}], "You are Lumi.", client=fake))
    assert resp.content == "hi!"
    assert fake.calls[0]["model"] == M["haiku"]
    assert fake.calls[0]["messages"][0] == {"role": "system", "content": "You are Lumi."}
    assert resp.raw["lumi_tier"] == "haiku" and resp.raw["lumi_escalated"] is False
    kinds = [e[0] for e in isolate]
    assert kinds == ["switch", "tokens"]
    assert isolate[0][1] == "haiku" and isolate[0][4] == "low"


def test_tool_turn_never_below_sonnet(isolate):
    fake = FakeClient({M["sonnet"]: reply("done")})
    asyncio.run(cr.call_claude([{"role": "user", "content": "hi"}], None, tools=TOOLS, client=fake))
    assert fake.calls[0]["model"] == M["sonnet"]
    assert "tool-calling turn" in isolate[0][3]


def test_error_escalates_once(isolate):
    fake = FakeClient({M["sonnet"]: RuntimeError("Anthropic API error (529): overloaded"),
                       M["opus"]: reply("fixed")})
    resp = asyncio.run(cr.call_claude([{"role": "user", "content": "fix the bug in my parser"}], None,
                                      tier="sonnet", client=fake))
    assert [c["model"] for c in fake.calls] == [M["sonnet"], M["opus"]]
    assert resp.raw["lumi_tier"] == "opus" and resp.raw["lumi_escalated"] is True
    switches = [e for e in isolate if e[0] == "switch"]
    assert switches[1][1] == "opus" and "auto-escalation from sonnet" in switches[1][3]


def test_escalates_only_once_then_raises():
    fake = FakeClient({M["haiku"]: RuntimeError("a"), M["sonnet"]: RuntimeError("b")})
    with pytest.raises(RuntimeError, match="b"):
        cr.call_claude_sync([{"role": "user", "content": "hey"}], None, tier="haiku", client=fake)
    assert len(fake.calls) == 2


def test_opus_error_is_not_retried():
    fake = FakeClient({M["opus"]: RuntimeError("down")})
    with pytest.raises(RuntimeError):
        cr.call_claude_sync([{"role": "user", "content": "x"}], None, tier="opus", client=fake)
    assert len(fake.calls) == 1


def test_malformed_tool_call_escalates():
    bad = reply(calls=[ToolCall(name="delete_everything", arguments={})])
    good = reply(calls=[ToolCall(name="read_file", arguments={"path": "a.txt"})])
    fake = FakeClient({M["sonnet"]: bad, M["opus"]: good})
    resp = cr.call_claude_sync([{"role": "user", "content": "read a.txt"}], None, tools=TOOLS, client=fake)
    assert resp.tool_calls[0].name == "read_file"
    assert [c["model"] for c in fake.calls] == [M["sonnet"], M["opus"]]


def test_malformed_detection():
    assert cr.malformed_tool_call(reply(calls=[ToolCall(name="", arguments={})]), TOOLS)
    assert cr.malformed_tool_call(reply(calls=[ToolCall(name="read_file", arguments="x")]), TOOLS)
    assert "cut off" in cr.malformed_tool_call(
        reply(calls=[ToolCall(name="read_file", arguments={})], stop="max_tokens"), TOOLS)
    assert cr.malformed_tool_call(reply(calls=[ToolCall(name="read_file", arguments={})]), TOOLS) is None
    assert cr.malformed_tool_call(reply("plain text"), TOOLS) is None


def test_no_api_key_raises_clearly():
    with pytest.raises(RuntimeError, match="No Anthropic API key"):
        cr.call_claude_sync([{"role": "user", "content": "hi"}], None, client=FakeClient({}, configured=False))


# ─── usage file ─────────────────────────────────────────────────

def test_usage_accumulates_and_computes_savings(isolate):
    fake = FakeClient({M["haiku"]: [reply(inp=1000, out=500), reply(inp=1000, out=500)]})
    for _ in range(2):
        cr.call_claude_sync([{"role": "user", "content": "hey"}], None, client=fake)
    with open(cr.USAGE_PATH, encoding="utf-8") as f:
        data = json.load(f)
    h = data["tiers"]["haiku"]
    assert h["calls"] == 2 and h["input_tokens"] == 2000 and h["output_tokens"] == 1000
    pi, po = cr.prices()["haiku"]
    oi, oo = cr.prices()["opus"]
    assert data["totals"]["cost_usd"] == pytest.approx((2000 * pi + 1000 * po) / 1e6)
    assert data["totals"]["opus_equivalent_usd"] == pytest.approx((2000 * oi + 1000 * oo) / 1e6)
    assert data["totals"]["saved_usd"] > 0
    totals = [e for e in isolate if e[0] == "tokens"][-1][4]
    assert totals["calls_by_tier"]["haiku"] == 2 and totals["tokens"] == 3000


def test_corrupt_usage_file_starts_fresh():
    with open(cr.USAGE_PATH, "w", encoding="utf-8") as f:
        f.write("{not json")
    cr._record("sonnet", M["sonnet"], {"input_tokens": 3, "output_tokens": 4})
    assert cr.usage_summary()["tiers"]["sonnet"]["calls"] == 1


# ─── anthropic:auto through chat_sync ───────────────────────────

def test_chat_sync_auto_spec_uses_router(monkeypatch):
    seen = {}

    def fake_call(messages, system_prompt, tools=None, **kw):
        seen["args"] = (messages, system_prompt, tools)
        return reply("routed")
    monkeypatch.setattr(cr, "call_claude_sync", fake_call)
    out = []
    resp = llm_providers.chat_sync("anthropic:auto", [{"role": "user", "content": "hi"}], tools=TOOLS,
                                   on_text=out.append)
    assert resp.content == "routed" and out == ["routed"]
    assert seen["args"][2] == TOOLS


# ─── workspace events (real emitters, no socket) ────────────────

def test_workspace_keeps_pipeline_snapshot(monkeypatch):
    monkeypatch.setattr(workspace, "_recent_steps", [])
    monkeypatch.setattr(workspace, "_last_switch", None)
    monkeypatch.setattr(workspace, "_last_tokens", None)
    REAL_SWITCH("opus", "claude-opus-x", "keywords: architecture", "high")
    REAL_TOKENS("opus", "claude-opus-x", 100, 20, {"tokens": 120})
    workspace.emit_autonomous_step("plan", {"step": "Planning", "status": "running", "session_id": "auto-1"})
    workspace.emit_autonomous_step("execute", {"step": "Executing Tool", "status": "success",
                                               "tool": "find_local_deals", "checks": [{"name": "x", "passed": True}]})
    assert workspace._last_switch["model"] == "opus" and workspace._last_switch["tier_cost"] == "high"
    assert workspace._last_tokens["tokens"] == 120
    assert [s["phase"] for s in workspace._recent_steps] == ["plan", "execute"]
    assert workspace._recent_steps[1]["tool"] == "find_local_deals"
    workspace.emit_autonomous_step("plan", {"step": "Planning"})      # a new run starts a fresh trail
    assert len(workspace._recent_steps) == 1

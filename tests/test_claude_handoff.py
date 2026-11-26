"""Local-first routing with a Claude Code handoff (modules/claude_handoff.py):
normal turns stay on Ollama; a hard coding/reasoning turn that would escalate
to Claude but can't (no key / the API refused the account) is offered to
Claude Code, and only runs if you approve."""

import asyncio
from types import SimpleNamespace

import pytest

from modules import claude_code, claude_handoff, claude_router as cr, hybrid_mode
from modules import prompter as pr
from modules.llm_providers import LLMResponse

HARD = "Refactor the websocket reconnect code in socketIO.py; it leaks memory after an hour."


class Key:
    def __init__(self, configured=True):
        self.configured = configured

    def is_configured(self):
        return self.configured


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    cr.clear_account_error()
    monkeypatch.setattr(cr, "USAGE_PATH", str(tmp_path / "claude_usage.json"))
    monkeypatch.setattr(hybrid_mode, "enabled", lambda: True)
    monkeypatch.setattr(hybrid_mode, "cloud_model", lambda: "anthropic:auto")
    monkeypatch.setattr(claude_code, "available", lambda: True)
    yield
    cr.clear_account_error()


def with_key(monkeypatch, configured=True):
    import modules.llm_providers as lp
    monkeypatch.setattr(lp, "get_client", lambda *_: Key(configured))
    monkeypatch.setattr(cr, "_client", lambda: Key(configured))


# ─── availability ───────────────────────────────────────────────

def test_api_status_follows_the_key_and_account_refusals(monkeypatch):
    with_key(monkeypatch, configured=False)
    assert cr.api_status() == (False, "No ANTHROPIC_API_KEY is set")
    with_key(monkeypatch)
    assert cr.api_status() == (True, "")
    err = RuntimeError("Anthropic API error (400): Your credit balance is too low to access the Anthropic API.")
    assert cr.is_account_error(err) and not cr.is_account_error(RuntimeError("(529) overloaded"))
    cr.mark_account_error(err)
    ok, why = cr.api_status()
    assert not ok and "no credit" in why


def test_a_refused_account_makes_later_calls_fail_fast_without_a_request(monkeypatch):
    calls = []

    class Refusing(Key):
        def chat(self, *a, **k):
            calls.append(k.get("model"))
            raise RuntimeError("Anthropic API error (400): Your credit balance is too low")
    with pytest.raises(RuntimeError, match="credit"):
        cr.call_claude_sync([{"role": "user", "content": "hey"}], None, tier="haiku", client=Refusing())
    assert len(calls) == 1                                   # an account error isn't escalated
    with pytest.raises(RuntimeError, match="unavailable"):
        cr.call_claude_sync([{"role": "user", "content": "hey"}], None, tier="haiku", client=Refusing())
    assert len(calls) == 1                                   # and the next call doesn't even try


def test_escalation_is_blocked_only_for_hard_turns_bound_for_claude(monkeypatch):
    with_key(monkeypatch)
    assert hybrid_mode.escalation_blocked("opus") is None    # key works: escalate as normal
    assert hybrid_mode.model_for("opus", "dolphin3:8b") == "anthropic:auto"
    cr.mark_account_error("credit balance is too low")
    why = hybrid_mode.escalation_blocked("opus")
    assert why and "no credit" in why and "Claude Code" in why
    assert hybrid_mode.model_for("opus", "dolphin3:8b") == "dolphin3:8b"     # stays local
    assert hybrid_mode.escalation_blocked("sonnet") is None
    monkeypatch.setattr(hybrid_mode, "enabled", lambda: False)
    assert hybrid_mode.escalation_blocked("opus") is None    # escalation off: nothing would escalate
    monkeypatch.setattr(hybrid_mode, "enabled", lambda: True)
    monkeypatch.setattr(hybrid_mode, "cloud_model", lambda: "openai:gpt-x")
    assert hybrid_mode.escalation_blocked("opus") is None    # not Claude: not ours to hand off


def test_no_key_blocks_too(monkeypatch):
    with_key(monkeypatch, configured=False)
    assert "No API key" in hybrid_mode.escalation_blocked("opus")


# ─── what gets offered ──────────────────────────────────────────

@pytest.mark.parametrize("text,task", [
    (HARD, True),
    ("Explain why this regex backtracks: ^(a+)+$", True),
    ("```python\nprint(1)\n```", False),        # too short to be worth Claude Code
    ("how was your day lumi, tell me something fun", False),
    ("fix", False),
])
def test_looks_like_task(text, task):
    assert claude_handoff.looks_like_task(text) is task


def test_offer_reason_needs_a_blocked_escalation_claude_code_and_a_task(monkeypatch):
    with_key(monkeypatch, configured=False)
    assert claude_handoff.offer_reason("opus", HARD)
    assert claude_handoff.offer_reason("sonnet", HARD) is None
    assert claude_handoff.offer_reason("opus", "tell me a story about a fox please") is None
    monkeypatch.setattr(claude_code, "available", lambda: False)
    assert claude_handoff.offer_reason("opus", HARD) is None


def test_build_task_fits_the_limit():
    t = claude_handoff.build_task("x" * (claude_code.MAX_TASK_CHARS + 500))
    assert len(t) <= claude_code.MAX_TASK_CHARS and t.endswith("[message shortened to fit]")


# ─── the chat turn ──────────────────────────────────────────────

def fake_prompter(monkeypatch, approve, handoff="No API key for anthropic"):
    p = object.__new__(pr.Prompter)
    p.signals = SimpleNamespace(history=[], speech_epoch=0)
    seen = {"confirm": [], "local": [], "cc": []}

    async def route_for(text):
        return "opus"

    def request(text, pending_user=None, tier=None, force_local=False):
        return {"model": "dolphin3:8b", "label": "full", "think": True, "keep_alive": "30s", "messages": [],
                "options": {}, "reply_cap": 100, "max_sentences": None, "tier": tier, "forced_local": force_local}

    async def confirm(name, preview, timeout=None, agent=None):
        seen["confirm"].append((name, preview, agent))
        return approve.pop(0) if isinstance(approve, list) else approve

    async def local_turn(req, text, tier, recover=True):
        seen["local"].append(req)
        return "local reply"

    def cc_run(task, mode, model):
        seen["cc"].append((task, mode, model))
        return "The leak is the unbounded retry list in reconnect()."

    p._route_for = route_for
    p.conversational_request = request
    p._note_route = lambda req: None
    p._confirm = confirm
    p._run_chat_request = local_turn
    monkeypatch.setattr(pr.speculative, "enabled", lambda: False)
    if handoff is not None:
        monkeypatch.setattr(claude_handoff, "offer_reason", lambda tier, text: handoff if tier == "opus" else None)
    monkeypatch.setattr(claude_code, "run", cc_run)
    monkeypatch.setattr(pr.agent_tracker, "update", lambda *a, **k: None)
    monkeypatch.setattr(pr.agent_tracker, "settle", lambda *a, **k: None)
    return p, seen


EXPLAIN = "Explain why this regex backtracks so badly on long input: ^(a+)+$"


def test_a_change_request_is_offered_with_edits_first(monkeypatch):
    p, seen = fake_prompter(monkeypatch, approve=True)
    reply = asyncio.run(p.run_conversational(HARD))                 # "Refactor …": asks for changes
    assert "Claude Code (with permission to edit files)" in reply and "unbounded retry list" in reply
    name, preview, agent = seen["confirm"][0]
    assert name == claude_code.TOOL_NAME and agent == "build" and len(seen["confirm"]) == 1
    assert preview["task"] == HARD and preview["mode"] == "edit" and "edit files" in preview["why"]
    assert "can't take it: no API key for anthropic." in preview["why"]
    assert seen["cc"] == [(HARD, "edit", "opus")] and seen["local"] == []
    assert p._spoken_override == claude_handoff.SPOKEN["edit"]      # the voice says one line


def test_declining_edits_then_read_only_is_offered(monkeypatch):
    p, seen = fake_prompter(monkeypatch, approve=[False, True])
    reply = asyncio.run(p.run_conversational(HARD))
    assert [c[1]["mode"] for c in seen["confirm"]] == ["edit", "read_only"]
    assert seen["confirm"][1][1]["why"].startswith("Read-only instead?")
    assert seen["cc"] == [(HARD, "read_only", "opus")] and "(read-only)" in reply
    assert p._spoken_override == claude_handoff.SPOKEN["read_only"]


def test_a_question_is_only_offered_read_only(monkeypatch):
    p, seen = fake_prompter(monkeypatch, approve=True)
    asyncio.run(p.run_conversational(EXPLAIN))
    assert [c[1]["mode"] for c in seen["confirm"]] == ["read_only"]
    assert seen["cc"][0][1] == "read_only"


def test_a_declined_handoff_answers_locally(monkeypatch):
    p, seen = fake_prompter(monkeypatch, approve=False)
    assert asyncio.run(p.run_conversational(HARD)) == "local reply"
    assert len(seen["confirm"]) == 2                                 # edits, then read-only
    assert seen["cc"] == [] and len(seen["local"]) == 1
    assert getattr(p, "_spoken_override", None) is None             # a local reply is spoken in full


@pytest.mark.parametrize("text,edits", [
    (HARD, True), ("please fix the bug in parser.py where dates break", True),
    (EXPLAIN, False), ("Analyze the time complexity of my merge sort", False),
])
def test_wants_edits(text, edits):
    assert claude_handoff.wants_edits(text) is edits
    assert claude_handoff.modes_to_offer(text) == (["edit", "read_only"] if edits else ["read_only"])


def test_the_reason_drops_the_redundant_tail(monkeypatch):
    assert claude_handoff.clean_reason(
        "Your Anthropic API account has no credit. Hard tasks are offered to Claude Code instead.") \
        == "Your Anthropic API account has no credit"
    with_key(monkeypatch)
    cr.mark_account_error("credit balance is too low")
    assert claude_handoff.offer_reason("opus", HARD) == "Your Anthropic API account has no credit"


def test_a_failed_claude_code_run_falls_back_to_local(monkeypatch):
    p, seen = fake_prompter(monkeypatch, approve=True)

    def boom(*a):
        raise claude_code.ClaudeCodeError("Claude Code has already been used 8 times this hour")
    monkeypatch.setattr(claude_code, "run", boom)
    assert asyncio.run(p.run_conversational(HARD)) == "local reply"


def test_no_handoff_means_a_normal_local_turn(monkeypatch):
    p, seen = fake_prompter(monkeypatch, approve=True, handoff=None)
    assert asyncio.run(p.run_conversational("hey lumi")) == "local reply"
    assert seen["confirm"] == []


def test_the_first_refusal_mid_turn_offers_the_handoff_then_goes_local(monkeypatch):
    """Escalation went to Claude (nothing known yet), the API refused the account:
    the turn offers Claude Code, and on decline answers locally, not with an error."""
    p, seen = fake_prompter(monkeypatch, approve=False, handoff=None)
    with_key(monkeypatch)
    real = pr.Prompter._run_chat_request

    async def run_req(req, text, tier, recover=True):
        if not recover:                              # the local retry
            seen["local"].append(req)
            return "local reply"
        return await real(p, dict(req, label="cloud", model="anthropic:auto"), text, tier, recover)
    p._run_chat_request = run_req

    def refused(*a, **k):
        cr.mark_account_error("credit balance is too low")    # what claude_router does
        raise RuntimeError("Claude API unavailable: Your Anthropic API account has no credit (credit balance).")
    monkeypatch.setattr(pr, "chat_sync", refused)
    assert asyncio.run(p.run_conversational(HARD)) == "local reply"
    assert len(seen["confirm"]) == 2 and "no credit" in seen["confirm"][0][1]["why"]
    assert "Hard tasks are offered" not in seen["confirm"][0][1]["why"]
    assert seen["local"][0]["forced_local"] is True             # the retry was forced local

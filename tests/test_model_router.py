"""Model router: obvious small talk skips the model, the model's word is
parsed, and anything going wrong lands on "sonnet". No Ollama needed."""

import pytest
import requests

import constants
from modules import model_router as mr


@pytest.fixture(autouse=True)
def no_gaming(monkeypatch):
    monkeypatch.setattr(constants, "GAMING_MODE", False, raising=False)


def asked(monkeypatch, answer=None, exc=None):
    calls = []

    def fake(prompt):
        calls.append(prompt)
        if exc:
            raise exc
        return answer
    monkeypatch.setattr(mr, "_ask_model", fake)
    return calls


@pytest.mark.parametrize("text", ["hi", "hey lumi"[:3], "Good morning!", "thanks :)", "lol", "how are you?", "", "   "])
def test_small_talk_is_haiku_without_asking_the_model(monkeypatch, text):
    calls = asked(monkeypatch, "opus")
    assert mr.route_model(text) == "haiku"
    assert calls == []


@pytest.mark.parametrize("answer, tier", [
    ("haiku", "haiku"), ("Sonnet", "sonnet"), ("opus.", "opus"), (" -> opus", "opus"),
    ("I think haiku", "haiku"), ("gpt-5", "sonnet"), ("", "sonnet"), (None, "sonnet"),
])
def test_the_models_word_is_parsed_and_nonsense_is_sonnet(monkeypatch, answer, tier):
    asked(monkeypatch, answer)
    assert mr.route_model("write a script that renames my photos by date") == tier


@pytest.mark.parametrize("exc", [requests.Timeout("slow"), requests.ConnectionError("down"),
                                 ValueError("bad json"), RuntimeError("anything")])
def test_any_failure_falls_back_to_sonnet_and_never_raises(monkeypatch, exc):
    asked(monkeypatch, exc=exc)
    assert mr.route_model("my reconnect loop leaks memory, find out why") == "sonnet"


def test_gaming_mode_never_loads_the_router_model(monkeypatch):
    monkeypatch.setattr(constants, "GAMING_MODE", True, raising=False)
    calls = asked(monkeypatch, "opus")
    assert mr.route_model("design the service boundaries for my app") == "sonnet"
    assert mr.route_model("hello") == "haiku"                       # small talk still recognised
    assert calls == []


def test_the_request_is_short_bounded_and_not_thinking(monkeypatch):
    seen = {}

    class R:
        def raise_for_status(self): pass
        def json(self): return {"message": {"content": "opus"}}

    def post(url, json=None, timeout=None):
        seen.update(url=url, json=json, timeout=timeout)
        return R()
    monkeypatch.setattr(mr.requests, "post", post)
    assert mr.route_model("x" * 5000) == "opus"
    assert seen["url"].endswith("/api/chat") and seen["timeout"] <= mr.TIMEOUT_S
    body = seen["json"]
    assert body["model"] == mr.ROUTER_MODEL and body["think"] is False and body["stream"] is False
    assert len(body["messages"][-1]["content"]) <= mr.MAX_PROMPT_CHARS


def test_tiers_map_to_claude_code_aliases_only():
    assert [mr.claude_code_model(t) for t in mr.TIERS] == ["haiku", "sonnet", "opus"]
    assert mr.claude_code_model(None) is None and mr.claude_code_model("gpt") is None

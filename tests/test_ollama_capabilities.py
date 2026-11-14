"""The Ollama client asks each model what it can do (/api/show), so a
chat-only model like dolphin3 never gets "think" (Ollama: 400 "does not
support thinking") and a tools request to it fails with a clear message
instead of a bare 400. No Ollama needed."""

import pytest

import constants
from modules import llm_providers as lp


class Resp:
    def __init__(self, data, status=200):
        self._d, self.status_code = data, status
    def json(self): return self._d
    def raise_for_status(self): pass


@pytest.fixture
def ollama(monkeypatch):
    lp.OllamaClient._caps_cache.clear()
    caps = {"dolphin3:8b": ["completion"], "qwen3:8b": ["completion", "tools", "thinking"]}
    sent, shows = [], []

    def post(url, json=None, timeout=None, **kw):
        if url.endswith("/api/show"):
            shows.append(json["model"])
            if json["model"] == "old":
                return Resp({})                                    # older Ollama: no capabilities field
            return Resp({"capabilities": caps[json["model"]]})
        sent.append(json)
        return Resp({"message": {"content": "ok"}, "done": True})
    monkeypatch.setattr(lp.requests, "post", post)
    yield lp.OllamaClient("http://127.0.0.1:11434"), sent, shows
    lp.OllamaClient._caps_cache.clear()


def test_think_is_dropped_for_a_model_that_cant_think(ollama):
    c, sent, _ = ollama
    c.chat([{"role": "user", "content": "hi"}], think=True, model="dolphin3:8b")
    assert "think" not in sent[-1]


def test_think_is_kept_for_a_thinking_model(ollama):
    c, sent, _ = ollama
    c.chat([{"role": "user", "content": "hi"}], think=True, model="qwen3:8b")
    assert sent[-1]["think"] is True


def test_tools_to_a_chat_only_model_fail_clearly_before_any_request(ollama):
    c, sent, _ = ollama
    with pytest.raises(RuntimeError, match="can't call tools.*AGENT_MODEL_NAME"):
        c.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function"}], think=True, model="dolphin3:8b")
    assert sent == []


def test_tools_go_through_to_a_tool_model(ollama):
    c, sent, _ = ollama
    c.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function"}], think=True, model="qwen3:8b")
    assert sent[-1]["tools"] and sent[-1]["think"] is True


def test_capabilities_are_asked_once_per_model(ollama):
    c, _, shows = ollama
    for _ in range(3):
        c.chat([{"role": "user", "content": "hi"}], think=True, model="qwen3:8b")
    assert shows == ["qwen3:8b"]


def test_plain_chat_doesnt_ask_at_all(ollama):
    c, sent, shows = ollama
    c.chat([{"role": "user", "content": "hi"}], think=False, model="dolphin3:8b")
    assert shows == [] and sent[-1]["think"] is False


def test_an_ollama_that_doesnt_report_capabilities_is_left_alone(ollama):
    c, sent, _ = ollama
    c.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function"}], think=True, model="old")
    assert sent[-1]["think"] is True and sent[-1]["tools"]


def test_the_agent_model_is_separate_from_the_chat_model():
    assert constants.AGENT_MODEL_NAME and constants.AGENT_MODEL_NAME != "dolphin3:8b"

"""Model router: obvious small talk skips the model, the model's word is
parsed, and anything going wrong lands on "sonnet". No Ollama needed."""

import pytest
import requests

import constants
from modules import model_router as mr


@pytest.fixture(autouse=True)
def no_gaming(monkeypatch):
    monkeypatch.setattr(constants, "GAMING_MODE", False, raising=False)
    mr._down_until["t"] = 0.0
    yield
    mr._down_until["t"] = 0.0


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


def test_after_a_failure_the_router_is_skipped_for_a_minute(monkeypatch):
    calls = asked(monkeypatch, exc=requests.Timeout("slow"))
    for _ in range(3):
        assert mr.route_model("find the memory leak in my reconnect loop") == "sonnet"
    assert len(calls) == 1                       # only the first message paid the wait
    mr._down_until["t"] = 0.0                    # cooldown over
    asked(monkeypatch, "opus")
    assert mr.route_model("find the memory leak in my reconnect loop") == "opus"


def test_a_tier_is_never_a_model_name():
    """Chat always runs on the configured Ollama names, never on 'sonnet' etc."""
    import inspect
    from modules import prompter
    assert "haiku" not in (constants.MODEL_NAME, constants.FAST_MODEL_NAME)
    assert not {constants.MODEL_NAME, constants.FAST_MODEL_NAME} & set(mr.TIERS)
    src = inspect.getsource(prompter.Prompter)
    assert "model = FAST_MODEL_NAME if use_fast else MODEL_NAME" in src
    flat = " ".join(src.split())
    # Agent turns: the tool-capable local model, unless Cloud Escalation sends an
    # opus-tier turn to the cloud model (modules/hybrid_mode.py).
    assert "agent_model = hybrid_mode.model_for(tier, AGENT_MODEL_NAME)" in flat
    assert "chat_sync( agent_model," in flat
    assert "tier" not in flat.split("chat_sync(")[1].split(")")[0]   # no tier variable reaches a chat call


# ─── task tiers: daily (web, media, light work) vs coding ───────────────
import asyncio

from modules.harness import config as hcfg

TIER_LUMI_MD = """# LUMI.md

## Rules
- be careful

## Model
- daily_models: qwen3.5:9b, qwen3:8b
- daily_num_ctx: 16384
- coding_models: qwen2.5-coder:14b, qwen3:8b
- coding_num_ctx: 4096
- game_model: qwen2.5:3b
- agent_model: qwen3:8b
- num_ctx: 8192
- vram_headroom_gb: 2.5
"""
INSTALLED = {"qwen3.5:9b": 6.6, "qwen3:8b": 5.2, "qwen2.5-coder:14b": 9.0, "qwen2.5:3b": 1.9}


@pytest.fixture
def tiers(tmp_path, monkeypatch):
    path = tmp_path / "LUMI.md"
    path.write_text(TIER_LUMI_MD, encoding="utf-8")
    monkeypatch.setattr(hcfg, "LUMI_MD_PATH", str(path))
    monkeypatch.setattr(mr, "installed_models", lambda: dict(INSTALLED))
    monkeypatch.setattr(mr, "gpu_vram_gb", lambda: 11.0)                # the RTX 2080 Ti
    monkeypatch.setattr(mr, "_caps_cache", {})
    return path


@pytest.mark.parametrize("prompt", [
    "search the web for good ramen near me", "browse to the KSU parking page", "look up the weather tomorrow",
    "play my liked songs on Spotify", "skip this song", "look up how to fix this python bug",
    "hey, how was your day?", "fix the typo in notes.txt", "what's on my class schedule today?"])
def test_daily_requests(prompt):
    assert mr.task_tier(prompt) == "daily"


@pytest.mark.parametrize("prompt", [
    "write a python script that renames my photos by date", "fix the bug in app.py",
    "debug this traceback: KeyError in main", "refactor the parser function", "add unit tests for utils.ts"])
def test_coding_requests(prompt):
    assert mr.task_tier(prompt) == "coding"


def test_a_write_code_prompt_runs_the_coder_with_4k(tiers):
    r = mr.TurnRouter("write a python script that renames my photos by date")
    assert (r.start, r.model, r.num_ctx) == ("coding", "qwen2.5-coder:14b", 4096)


def test_a_web_search_call_moves_the_turn_to_the_daily_model(tiers):
    r = mr.TurnRouter("fix the bug in app.py")
    assert r.route([{"role": "user", "content": "fix the bug in app.py"}]) == "coding"
    convo = [{"role": "user", "content": "fix the bug in app.py"},
             {"role": "assistant", "content": "", "tool_calls": [
                 {"function": {"name": "web_search", "arguments": {"query": "KeyError fix"}}}]},
             {"role": "tool", "name": "web_search", "content": "<html>" + "x" * 5000}]
    assert r.route(convo) == "daily"
    assert (r.model, r.num_ctx) == ("qwen3.5:9b", 16384)
    assert r.route(convo[:1]) == "daily"                              # never back to the 4K coder


@pytest.mark.parametrize("tool", ["web_search", "browse_url", "spotify_play", "media_seek",
                                  "media_playlist_tracks", "sdk__minecraft__mine", "recall_memory"])
def test_web_and_media_tools_are_daily(tool):
    assert mr.tier_for_tools([tool]) == "daily"
    assert mr.tier_for_tools(["read_file", "write_file"]) is None


def test_daily_must_fit_in_vram_but_coding_may_spill(tiers, monkeypatch):
    assert mr.model_for_task("daily") == "qwen3.5:9b"                 # 6.6 GB <= 11 - 2.5
    monkeypatch.setattr(mr, "installed_models", lambda: {"qwen3:8b": 5.2, "qwen2.5-coder:14b": 9.0})
    assert mr.model_for_task("daily") == "qwen3:8b"                   # 9B not pulled: the fallback
    assert mr.model_for_task("coding") == "qwen2.5-coder:14b"         # 9 GB spills, still chosen
    monkeypatch.setattr(mr, "installed_models", lambda: {"qwen3:8b": 5.2})
    assert mr.model_for_task("coding") == "qwen3:8b"
    monkeypatch.setattr(mr, "installed_models", lambda: {})
    assert mr.model_for_task("coding") == "qwen3:8b"                  # LUMI.md agent_model


def test_gaming_mode_keeps_every_turn_on_daily(tiers, monkeypatch):
    monkeypatch.setattr(constants, "GAMING_MODE", True, raising=False)
    r = mr.TurnRouter("write a python script")
    assert (r.start, r.model, r.num_ctx) == ("daily", "qwen3.5:9b", 16384)


def test_tiers_come_from_lumi_md_and_old_keys_still_work(tiers, tmp_path, monkeypatch):
    assert hcfg.daily_models() == ["qwen3.5:9b", "qwen3:8b"] and hcfg.coding_models()[0] == "qwen2.5-coder:14b"
    assert (hcfg.tier_num_ctx("daily"), hcfg.tier_num_ctx("coding")) == (16384, 4096)
    monkeypatch.setattr(constants, "MINECRAFT_AGENT_LOCAL_MODEL", None, raising=False)
    assert mr.local_agent_model() == "qwen2.5:3b"
    old = tmp_path / "OLD.md"
    old.write_text("## Model\n- num_ctx: 8192\n- fast_agent_models: qwen3:8b\n- primary_agent_models: qwen2.5-coder:14b\n",
                   encoding="utf-8")
    monkeypatch.setattr(hcfg, "LUMI_MD_PATH", str(old))
    assert hcfg.daily_models() == ["qwen3:8b"] and hcfg.coding_models() == ["qwen2.5-coder:14b"]
    assert hcfg.tier_num_ctx("coding") == 8192


def _ollama_payloads(monkeypatch):
    """Capture what Lumi sends to Ollama's /api/chat."""
    from modules import llm_providers as lp
    sent = []

    class Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"role": "assistant", "content": "ok"}, "done": True}

    def post(url, json=None, **kw):
        if url.endswith("/api/chat"):
            sent.append(json)
        return Resp()
    monkeypatch.setattr(lp.requests, "post", post)
    monkeypatch.setattr(lp.OllamaClient, "capabilities", lambda self, model: ["completion", "tools"])
    return sent


def _tool(name):
    return {"type": "function", "function": {"name": name, "description": name, "parameters": {"type": "object"}}}


def test_each_tier_sends_its_num_ctx_to_ollama(tiers, monkeypatch):
    from modules import prompter as P
    sent = _ollama_payloads(monkeypatch)
    tools = [_tool(n) for n in ("read_file", "write_file", "run_command", "web_search", "spotify_play", "task_complete")]

    async def go(prompt, messages):
        router = mr.TurnRouter(prompt)
        chat = P.Prompter._routed_chat(None, router, "sonnet", False, {"temperature": 0.5}, "FULL LUMI PROMPT")
        return await chat(messages, tools)

    coding = [{"role": "system", "content": P.CODING_SYSTEM_PROMPT}, {"role": "user", "content": "fix the bug in app.py"}]
    asyncio.run(go("fix the bug in app.py", coding))
    p = sent[-1]
    assert p["model"] == "qwen2.5-coder:14b" and p["options"]["num_ctx"] == 4096
    assert {t["function"]["name"] for t in p["tools"]} == {"read_file", "write_file", "run_command", "task_complete"}

    asyncio.run(go("search the web for ramen", [{"role": "system", "content": "FULL LUMI PROMPT"},
                                                 {"role": "user", "content": "search the web for ramen"}]))
    p = sent[-1]
    assert p["model"] == "qwen3.5:9b" and p["options"]["num_ctx"] == 16384 and len(p["tools"]) == len(tools)

    # A coding turn that searched the web finishes on daily, with the full prompt back.
    after_web = coding + [{"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "web_search", "arguments": {}}}]},
                          {"role": "tool", "name": "web_search", "content": "results"}]
    asyncio.run(go("fix the bug in app.py", after_web))
    p = sent[-1]
    assert p["model"] == "qwen3.5:9b" and p["options"]["num_ctx"] == 16384
    assert p["messages"][0]["content"] == "FULL LUMI PROMPT"


def test_a_model_without_native_tools_gets_the_text_format(tiers, monkeypatch):
    from modules import prompter as P
    sent = _ollama_payloads(monkeypatch)
    monkeypatch.setattr(mr, "_caps_cache", {"qwen3.5:9b": ["completion"]})

    async def go():
        router = mr.TurnRouter("search the web for ramen")
        chat = P.Prompter._routed_chat(None, router, "sonnet", False, {}, "FULL")
        return await chat([{"role": "system", "content": "FULL"}, {"role": "user", "content": "hi"}],
                          [_tool("web_search")])
    asyncio.run(go())
    p = sent[-1]
    assert "tools" not in p                                            # Ollama would refuse them
    assert '{"name": "<tool name>", "arguments"' in p["messages"][-1]["content"]
    assert "web_search" in p["messages"][-1]["content"]


def test_the_coding_prompt_leaves_room_in_4k():
    from modules import prompter as P
    from modules.agent_tools import TOOL_SCHEMAS
    from modules.harness.tasks import TASK_TOOL_SCHEMAS
    import json
    coding_tools = mr.TurnRouter.coding_tools(TOOL_SCHEMAS + TASK_TOOL_SCHEMAS)
    used = (len(P.CODING_SYSTEM_PROMPT) + len(hcfg.rules_text()) + len(json.dumps(coding_tools))) // 4
    assert used < 4096 // 2, used                                    # at least half the window left to work


def test_text_tool_calls_with_raw_newlines_in_code_are_recovered():
    """qwen2.5-coder writes its calls as fenced JSON text, often with real line
    breaks inside the code string, which strict JSON refuses."""
    from modules.prompter import _recover_text_tool_calls
    reply = ('```json\n{"name": "write_file", "arguments": {"path": "rev.py", '
             '"content": "def rev(s):\n    return s[::-1]\n"}}\n```')
    assert "\n    return" in reply                                   # a raw newline inside the string
    calls = _recover_text_tool_calls(reply)
    assert [c["function"]["name"] for c in calls] == ["write_file"]
    assert calls[0]["function"]["arguments"]["content"].startswith("def rev(s):")


# ─── agent model timeouts: wait longer, retry the same model once, then stop ──

def _ask(prompt="search the web for ramen"):
    """One routed agent call (built inside a running loop, as in Lumi)."""
    from modules import prompter as P

    async def go():
        chat = P.Prompter._routed_chat(None, mr.TurnRouter(prompt), "sonnet", False, {}, "FULL")
        return await chat([{"role": "user", "content": "hi"}], [])
    return asyncio.run(go())


def test_a_timeout_is_retried_once_on_the_same_model(tiers, monkeypatch):
    from modules import prompter as P
    calls = []

    def flaky(model, messages, **kw):
        calls.append((model, kw.get("timeout")))
        if len(calls) == 1:
            raise requests.exceptions.ReadTimeout("slow")
        return P.LLMResponse(content="ok")
    monkeypatch.setattr(P, "chat_sync", flaky)
    resp = _ask()
    assert resp.content == "ok"
    assert calls == [("qwen3.5:9b", 300.0), ("qwen3.5:9b", 300.0)]      # same model, LUMI.md timeout


def test_two_timeouts_stop_with_a_clear_message_and_no_smaller_model(tiers, monkeypatch):
    from modules import prompter as P
    models = []

    def slow(model, messages, **kw):
        models.append(model)
        raise requests.exceptions.ReadTimeout("slow")
    monkeypatch.setattr(P, "chat_sync", slow)
    with pytest.raises(RuntimeError, match="didn't answer within 300s, twice"):
        _ask()
    assert models == ["qwen3.5:9b", "qwen3.5:9b"]                        # never qwen2.5:3b


def test_the_timeout_comes_from_lumi_md_and_reaches_ollama(tiers, monkeypatch):
    with open(tiers, "a", encoding="utf-8") as f:
        f.write("- agent_timeout_s: 450\n")
    assert hcfg.agent_timeout_s() == 450.0
    from modules import llm_providers as lp
    seen = {}

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"role": "assistant", "content": "ok"}, "done": True}

    def post(url, json=None, timeout=None, **kw):
        seen["timeout"] = timeout
        return Resp()
    monkeypatch.setattr(lp.requests, "post", post)
    monkeypatch.setattr(lp.OllamaClient, "capabilities", lambda self, model: ["completion", "tools"])
    _ask()
    assert seen["timeout"] == 450.0
    lp.chat_sync("qwen3:8b", [{"role": "user", "content": "x"}])            # other callers keep the default
    assert seen["timeout"] == lp.get_client("ollama").timeout


def test_the_routers_quick_sort_never_picks_the_agents_model():
    """route_model only labels the turn (haiku/sonnet/opus) with the small sorter and
    falls back to 'sonnet' on a timeout; the agent's model comes from TurnRouter."""
    assert mr.TIMEOUT_S <= 10 and mr.DEFAULT_TIER == "sonnet"

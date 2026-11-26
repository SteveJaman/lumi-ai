import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import types

import pytest

from modules import claude_bridge as cb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_token_is_created_once_and_reused(tmp_path):
    p = str(tmp_path / "creds" / "claude_bridge.token")
    a = cb.ensure_token(p)
    b = cb.ensure_token(p)
    assert a == b and len(a) >= 32
    assert open(p, encoding="utf-8").read().strip() == a


def test_only_loopback_with_the_right_token_and_no_origin():
    tok = "x" * 40
    good = {"Authorization": f"Bearer {tok}"}
    assert cb.authorized(good, is_local=True, token=tok)
    assert not cb.authorized(good, is_local=False, token=tok)            # tunnel or LAN
    assert not cb.authorized({**good, "Origin": "http://evil.example"}, True, tok)  # a web page
    assert not cb.authorized({"Authorization": "Bearer nope"}, True, tok)
    assert not cb.authorized({}, True, tok)


def test_find_reply_skips_her_own_earlier_messages():
    hist = [{"role": "assistant", "content": "old"}, {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"}]
    assert cb.find_reply(hist, 2) == 2
    assert cb.find_reply(hist, 3) is None


class _Signals:
    def __init__(self):
        self.history = []
        self.AI_thinking = False


def test_wait_for_reply_returns_her_settled_answer():
    s = _Signals()
    s.history.append({"role": "user", "content": "q"})
    start = len(s.history)

    async def go():
        async def later():
            await asyncio.sleep(0.05)
            s.history.append({"role": "assistant", "content": "answer"})
        asyncio.get_running_loop().create_task(later())
        return await cb.wait_for_reply(s, start, timeout=3, settle=0.1, poll=0.02)
    assert asyncio.run(go()) == "answer"


def test_wait_for_reply_gives_up_with_none_when_she_never_answers():
    s = _Signals()
    out = asyncio.run(cb.wait_for_reply(s, 0, timeout=0.2, settle=0.05, poll=0.02))
    assert out is None


def test_wait_for_reply_waits_while_she_is_still_thinking():
    s = _Signals()
    s.history.append({"role": "assistant", "content": "partial"})
    s.AI_thinking = True

    async def go():
        async def finish():
            await asyncio.sleep(0.15)
            s.AI_thinking = False
            s.history[0]["content"] = "full answer"
        asyncio.get_running_loop().create_task(finish())
        return await cb.wait_for_reply(s, 0, timeout=3, settle=0.05, poll=0.02)
    assert asyncio.run(go()) == "full answer"


def _load_mcp():
    spec = importlib.util.spec_from_file_location("lumi_mcp", os.path.join(ROOT, "scripts", "lumi_mcp.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_mcp_lists_the_four_tools_and_refuses_unknown_methods(capsys):
    m = _load_mcp()
    m.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    out = json.loads(capsys.readouterr().out)
    names = [t["name"] for t in out["result"]["tools"]]
    assert names == ["ask_lumi", "lumi_status", "lumi_recent_chat", "lumi_search_memory"]
    m.handle({"jsonrpc": "2.0", "id": 2, "method": "nope"})
    assert json.loads(capsys.readouterr().out)["error"]["code"] == -32601


def test_mcp_reports_a_missing_token_as_a_tool_error(capsys, monkeypatch, tmp_path):
    m = _load_mcp()
    monkeypatch.setattr(m, "TOKEN_PATH", str(tmp_path / "missing.token"))
    m.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
              "params": {"name": "lumi_status", "arguments": {}}})
    res = json.loads(capsys.readouterr().out)["result"]
    assert res["isError"] is True and "token" in res["content"][0]["text"]


def test_mcp_notifications_get_no_reply(capsys):
    m = _load_mcp()
    m.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert capsys.readouterr().out == ""

"""Lumi Island's socket: the hello handshake, approvals still working behind
it, and the Home / Chat / Calendar messages. Real aiohttp server and real
websocket clients on a free port; the workspace hub is faked."""

import asyncio
import json
import socket
import threading
import types

import aiohttp
import pytest

from modules import hitl_server, island_bridge
from modules.workspace_hub import InvalidInput, NotConnected, ProviderError
from signals import Signals, _History

TOKEN = "s3cret-token"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Hub:
    def __init__(self):
        self.listeners, self.range_calls, self.fail = [], [], None

    def get_compressed_snapshot(self): return {"v": 1, "next_event": None, "unread_count": 2, "unread": [], "connected": {"google": True, "ksu": False}}
    def add_listener(self, fn): self.listeners.append(fn)
    def get_events_range(self, start, end, refresh=False):
        self.range_calls.append((start, end, refresh))
        if self.fail:
            raise self.fail
        return {"events": [{"id": "e1", "title": "Exam", "start": f"{start}T10:00:00+00:00", "end": f"{start}T11:00:00+00:00",
                            "all_day": False, "location": "", "description": "", "attendees": [], "link": "", "status": ""}], "at": 123.0}


class FakeSignals:
    """Uses the real Signals assignment hook, so wrapping and notifying history match production."""
    __setattr__ = Signals.__setattr__


def make_signals(history=None, hub=True):
    sig = FakeSignals()
    sig.AI_thinking = sig.AI_speaking = sig.new_message = False
    sig.last_message_time, sig.saved = 0, 0
    sig.workspace_hub = Hub() if hub else None
    sig.save_history = lambda: setattr(sig, "saved", sig.saved + 1)
    sig.history = list(history or [])
    return sig


class Env:
    """A running server + bridge. Use inside `async with`."""
    def __init__(self, token=TOKEN, history=None, hub=True):
        self.sig = make_signals(history, hub)
        self.server = hitl_server.HITLServer("127.0.0.1", free_port())
        self.token = token
        self.sessions = []

    async def __aenter__(self):
        if self.token:
            self.server.set_token(self.token)
        await self.server.start(self.sig)
        island_bridge.install(self.sig, self.server)
        return self

    async def __aexit__(self, *exc):
        for s in self.sessions:
            await s.close()
        await self.server.stop()

    async def connect(self, token=TOKEN, hello=True, origin=None):
        s = aiohttp.ClientSession()
        self.sessions.append(s)
        headers = {"Origin": origin} if origin else {}
        ws = await s.ws_connect(f"http://127.0.0.1:{self.server.port}/hitl", headers=headers)
        if hello:
            await ws.send_json({"type": "hello", "token": token})
        return ws


async def recv(ws, kind, timeout=3):
    """Next message of `kind` (others are skipped)."""
    async def loop():
        while True:
            msg = await ws.receive()
            if msg.type != aiohttp.WSMsgType.TEXT:
                raise AssertionError(f"socket closed ({msg.type}, {ws.close_code}) waiting for {kind}")
            data = json.loads(msg.data)
            if data.get("type") == kind:
                return data
    return await asyncio.wait_for(loop(), timeout)


def run(coro):
    return asyncio.run(coro)


# ── handshake ─────────────────────────────────────────────────────

def test_good_token_is_welcomed_and_gets_status():
    async def t():
        async with Env() as e:
            ws = await e.connect()
            assert (await recv(ws, "hello_ok"))["gaming"] in (True, False)
            assert (await recv(ws, "lumi_status"))["state"] == "idle"
    run(t())


def test_wrong_token_is_refused_and_never_counted_as_a_client():
    async def t():
        async with Env() as e:
            ws = await e.connect(token="nope")
            msg = await asyncio.wait_for(ws.receive(), 3)
            assert msg.type == aiohttp.WSMsgType.CLOSE and ws.close_code == 4401
            assert e.server.has_client is False
    run(t())


def test_first_frame_must_be_hello():
    async def t():
        async with Env() as e:
            ws = await e.connect(hello=False)
            await ws.send_json({"type": "chat_send", "text": "sneaky"})
            msg = await asyncio.wait_for(ws.receive(), 3)
            assert msg.type == aiohttp.WSMsgType.CLOSE and ws.close_code == 4401
            assert len(e.sig.history) == 0
    run(t())


def test_unauthenticated_clients_never_receive_approvals(monkeypatch):
    async def t():
        async with Env() as e:
            bad = await e.connect(token="nope")
            await asyncio.wait_for(bad.receive(), 3)
            monkeypatch.setattr("builtins.input", lambda *_: "n")      # the terminal fallback answers
            assert await e.server.request_approval("run_command", "ls", timeout=2) is False
    run(t())


def test_foreign_origins_are_still_refused():
    async def t():
        async with Env() as e:
            with pytest.raises(aiohttp.WSServerHandshakeError) as err:
                await e.connect(origin="https://evil.example")
            assert err.value.status == 403
            ws = await e.connect(origin="http://tauri.localhost")
            await recv(ws, "hello_ok")
    run(t())


def test_without_a_token_the_old_open_behaviour_remains():
    async def t():
        async with Env(token=None) as e:
            ws = await e.connect(hello=False)
            assert (await recv(ws, "lumi_status"))["state"] == "idle"
            await ws.send_json({"type": "hello"})
            await recv(ws, "hello_ok")
    run(t())


def test_approvals_still_round_trip_behind_the_handshake():
    async def t():
        async with Env() as e:
            ws = await e.connect()
            await recv(ws, "hello_ok")
            task = asyncio.create_task(e.server.request_approval("write_file", '{"path": "x"}', timeout=5))
            req = await recv(ws, "approval_request")
            assert req["tool"] == "write_file"
            await ws.send_json({"type": "approval_response", "id": req["id"], "decision": "approve"})
            assert await task is True
    run(t())


# ── chat ──────────────────────────────────────────────────────────

def test_get_chat_returns_only_what_a_person_would_see():
    hist = [{"role": "user", "content": "hi"},
            {"role": "assistant", "content": "[happy] hello!"},
            {"role": "assistant", "content": "event", "source": "sdk_event"},
            {"role": "system", "content": "wiped"},
            {"role": "assistant", "content": "secret", "hidden": True},
            {"role": "user", "content": "   "},
            {"role": "user", "content": "bye"}]

    async def t():
        async with Env(history=hist) as e:
            ws = await e.connect()
            await ws.send_json({"type": "get_chat", "id": 7, "limit": 10})
            res = await recv(ws, "chat")
            assert res["id"] == 7
            assert [(m["n"], m["content"]) for m in res["messages"]] == [(0, "hi"), (1, "[happy] hello!"), (6, "bye")]
            await ws.send_json({"type": "get_chat", "limit": 1})
            assert [m["n"] for m in (await recv(ws, "chat"))["messages"]] == [6]
    run(t())


def test_chat_send_queues_a_host_message():
    async def t():
        async with Env() as e:
            ws = await e.connect()
            await ws.send_json({"type": "chat_send", "id": 1, "text": "  hello lumi  "})
            assert (await recv(ws, "chat_ack"))["ok"] is True
            msg = e.sig.history[-1]
            assert msg == {"role": "user", "content": "hello lumi", "from_host": True}
            assert e.sig.new_message is True and e.sig.saved == 1
    run(t())


def test_chat_send_rejects_empty_throttles_and_truncates():
    async def t():
        async with Env() as e:
            ws = await e.connect()
            await ws.send_json({"type": "chat_send", "text": "   "})
            assert (await recv(ws, "chat_ack"))["ok"] is False
            await ws.send_json({"type": "chat_send", "text": "x" * 9000})
            assert (await recv(ws, "chat_ack"))["ok"] is True
            assert len(e.sig.history[-1]["content"]) == island_bridge.MAX_CHAT_CHARS
            await ws.send_json({"type": "chat_send", "text": "too fast"})          # straight after
            ack = await recv(ws, "chat_ack")
            assert ack["ok"] is False and "Slow" in ack["error"]
    run(t())


def test_new_messages_are_pushed_from_any_thread_and_hidden_ones_are_not():
    async def t():
        async with Env() as e:
            ws = await e.connect()
            await recv(ws, "hello_ok")
            e.sig.history.append({"role": "user", "content": "first"})
            t = threading.Thread(target=lambda: e.sig.history.append({"role": "assistant", "content": "[calm] reply"}))
            t.start()
            t.join()                                                             # appended from another thread
            e.sig.history.append({"role": "assistant", "content": "x", "source": "sdk_event"})   # hidden: not pushed
            e.sig.history.append({"role": "user", "content": "last"})
            got = [(await recv(ws, "chat_message"))["message"] for _ in range(3)]
            assert [(m["n"], m["content"]) for m in got] == [(0, "first"), (1, "[calm] reply"), (3, "last")]
            e.sig.history = [{"role": "user", "content": "fresh"}]               # a wipe or rewrite
            await recv(ws, "chat_reset")
            assert isinstance(e.sig.history, _History)                           # still observable
    run(t())


def test_history_list_behaves_like_a_list():
    seen = []
    owner = types.SimpleNamespace(on_history_change=lambda *a: seen.append(a))
    h = _History([{"a": 1}], owner)
    h.append({"b": 2})
    h.extend([{"c": 3}, {"d": 4}])
    assert [s[:2] for s in seen] == [("append", 1), ("append", 2)] and seen[1][2] == [{"c": 3}, {"d": 4}]
    assert type(h[:2]) is list and json.dumps(h)                                 # slices are plain lists; JSON works
    h.clear()
    assert seen[-1][0] == "reset" and len(h) == 0
    _History([1], None).append(2)                                                # no owner: silent


# ── calendar + home + push ────────────────────────────────────────

def test_calendar_requests_go_to_the_hub_and_reply_with_events():
    async def t():
        async with Env() as e:
            ws = await e.connect()
            await ws.send_json({"type": "get_calendar", "id": 3, "start": "2026-11-01", "end": "2026-12-01"})
            res = await recv(ws, "calendar")
            assert res["id"] == 3 and res["events"][0]["title"] == "Exam" and res["at"] == 123.0
            assert e.sig.workspace_hub.range_calls == [("2026-11-01", "2026-12-01", False)]
    run(t())


@pytest.mark.parametrize("exc,code", [(NotConnected("Connect it"), "not_connected"), (InvalidInput("bad dates"), "bad_request"),
                                      (ProviderError("Google down"), "provider")])
def test_calendar_errors_become_codes_not_crashes(exc, code):
    async def t():
        async with Env() as e:
            e.sig.workspace_hub.fail = exc
            ws = await e.connect()
            await ws.send_json({"type": "get_calendar", "start": "2026-11-01", "end": "2026-11-02"})
            res = await recv(ws, "calendar")
            assert res["code"] == code and res["error"] == str(exc) and res["events"] == []
            await ws.send_json({"type": "get_home"})                              # socket is still alive
            await recv(ws, "home")
    run(t())


def test_calendar_without_a_workspace_is_a_clean_error():
    async def t():
        async with Env(hub=False) as e:
            ws = await e.connect()
            await ws.send_json({"type": "get_calendar", "start": "2026-11-01", "end": "2026-11-02"})
            assert (await recv(ws, "calendar"))["code"] == "unavailable"
            await ws.send_json({"type": "get_home"})
            assert (await recv(ws, "home"))["snapshot"] is None
    run(t())


def test_home_has_the_snapshot_and_state():
    async def t():
        async with Env() as e:
            e.sig.AI_thinking = True
            ws = await e.connect()
            await ws.send_json({"type": "get_home", "id": 9})
            res = await recv(ws, "home")
            assert res["id"] == 9 and res["snapshot"]["unread_count"] == 2 and res["state"] == "thinking"
    run(t())


def test_snapshot_changes_are_pushed():
    async def t():
        async with Env() as e:
            ws = await e.connect()
            await recv(ws, "hello_ok")
            e.sig.workspace_hub.listeners[0]({"v": 1, "unread_count": 5})
            assert (await recv(ws, "snapshot"))["snapshot"]["unread_count"] == 5
    run(t())


def test_gaming_mode_is_announced():
    async def t():
        async with Env() as e:
            ws = await e.connect()
            await recv(ws, "hello_ok")
            e.server.broadcast_threadsafe({"type": "lumi_mode", "gaming": True})
            assert (await recv(ws, "lumi_mode"))["gaming"] is True
    run(t())


def test_a_handler_that_blows_up_reports_and_keeps_the_socket():
    async def t():
        async with Env() as e:
            async def boom(ws, data): raise RuntimeError("kaboom")
            e.server.register_handler("boom", boom)
            ws = await e.connect()
            await ws.send_json({"type": "boom", "id": 4})
            err = await recv(ws, "error")
            assert err["id"] == 4 and "kaboom" in err["message"]
            await ws.send_json({"type": "get_home"})
            await recv(ws, "home")
            await ws.send_str("not json")                                         # garbage is ignored
            await ws.send_json({"type": "no-such-thing"})
            await ws.send_json({"type": "get_home"})
            await recv(ws, "home")
    run(t())

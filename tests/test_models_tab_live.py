"""The Models tab's event path end to end: a real socket.io server (aiohttp),
a real socket.io client in the workspace room, and claude_router running
from a worker thread like Lumi's chat does. Claude itself is faked here;
LUMI_LIVE_CLAUDE=1 makes one real (tiny, haiku) call instead."""

import asyncio
import os
import socket
import threading

import pytest
import socketio
from aiohttp import web

from modules import claude_router as cr
from modules import workspace
from modules.llm_providers import LLMResponse

LIVE = os.environ.get("LUMI_LIVE_CLAUDE") == "1"
EVENTS = ("claude_model_switch", "claude_token_stream", "model_route", "autonomous_step", "model_pipeline_state",
          "claude_call_failed")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeClaude:
    def is_configured(self):
        return True

    def chat(self, messages, tools=None, think=None, options=None, keep_alive=None, model=""):
        return LLMResponse(content="hi!", raw={"usage": {"input_tokens": 120, "output_tokens": 30}},
                           provider="anthropic", model=model)


class Server:
    """Just what workspace.install needs from SocketIOServer: .sio and ._loop."""
    def __init__(self):
        self.sio = socketio.AsyncServer(async_mode="aiohttp", cors_allowed_origins="*")
        self._loop = None


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "USAGE_PATH", str(tmp_path / "claude_usage.json"))
    for name in ("_server", "_last_switch", "_last_tokens", "_last_route"):
        monkeypatch.setattr(workspace, name, None)
    monkeypatch.setattr(workspace, "_viewers", set())
    monkeypatch.setattr(workspace, "_recent_steps", [])
    return tmp_path


def test_models_tab_receives_router_events_live(isolated):
    got = {}

    async def main():
        srv = Server()
        srv._loop = asyncio.get_running_loop()

        @srv.sio.event
        async def connect(sid, environ, auth=None):
            await srv.sio.save_session(sid, {"user": {"username": "me"}, "is_local": True})

        workspace.install(srv)
        app = web.Application()
        srv.sio.attach(app)
        runner = web.AppRunner(app)
        await runner.setup()
        port = free_port()
        await web.TCPSite(runner, "127.0.0.1", port).start()

        client = socketio.AsyncClient()
        for ev in EVENTS:
            client.on(ev, (lambda name: (lambda data: got.setdefault(name, []).append(data)))(ev))
        await client.connect(f"http://127.0.0.1:{port}", transports=["websocket"])
        await client.emit("workspace_join")
        for _ in range(50):
            if "model_pipeline_state" in got:
                break
            await asyncio.sleep(0.05)

        # What prompter.py does for a chat turn, then the router call from a worker thread.
        workspace.emit_model_route("opus", "anthropic:auto", "claude", "sorted by qwen2.5:3b", "chat")
        client_obj = None if LIVE else FakeClaude()
        try:
            resp = await asyncio.get_running_loop().run_in_executor(
                None, lambda: cr.call_claude_sync([{"role": "user", "content": "hey lumi!"}], "Reply in five words.",
                                                  tier="haiku" if LIVE else None, client=client_obj))
            got["reply"] = resp.content
        except RuntimeError as e:                 # live only: e.g. the account is out of credit
            if not LIVE:
                raise
            got["error"] = str(e)
        workspace.emit_autonomous_step("plan", {"step": "Planning", "status": "running", "session_id": "auto-x"})
        for _ in range(100):
            if ("claude_token_stream" in got or "claude_call_failed" in got) and "autonomous_step" in got:
                break
            await asyncio.sleep(0.05)
        await client.disconnect()
        await runner.cleanup()

    asyncio.run(main())

    state = got["model_pipeline_state"][0]
    assert {"switch", "tokens", "steps", "route", "usage", "router"} <= set(state)
    assert state["router"]["state"] in ("off", "ready")
    assert got["model_route"][0]["where"] == "claude"
    sw = got["claude_model_switch"][0]
    assert sw["model"] == "haiku" and sw["tier_cost"] == "low" and sw["reason"]
    assert got["autonomous_step"][0]["phase"] == "plan"
    if "error" in got:                            # live, and Claude refused the call
        fail = got["claude_call_failed"]
        print("LIVE call failed; the tab was told:", fail)
        assert fail[0]["model"] == "haiku" and fail[0]["retry_tier"] is None   # an account error isn't retried
        assert len(got["claude_model_switch"]) == 1
        return
    tok = got["claude_token_stream"][-1]
    assert tok["model"] == "haiku" and tok["tokens"] == tok["input_tokens"] + tok["output_tokens"] > 0
    assert tok["totals"]["calls_by_tier"]["haiku"] == 1 and tok["totals"]["cost_usd"] > 0
    assert os.path.isfile(cr.USAGE_PATH)                      # the usage file was written
    if LIVE:
        print("\nLIVE reply:", got["reply"], "| tokens:", tok["tokens"], "| cost $", tok["totals"]["cost_usd"])

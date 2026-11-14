"""The island socket's Pipeline and remote-pairing behaviour, on a real
server and real websocket clients (free port, never 5090):

* agent_update on connect and on every tracker change
* approvals carry the agent, show it as waiting_approval, then restore it
* a remote peer (another device) needs the pairing code, whatever the token
* the Minecraft card follows the SDK's "Currently:" line
"""

import asyncio
import types

import aiohttp
import pytest

from modules import agent_tracker, hitl_server, island_remote
from tests.test_island_bridge import TOKEN, Env, recv, run

TS = "100.101.102.103"


@pytest.fixture(autouse=True)
def fresh_tracker(monkeypatch):
    monkeypatch.setattr(agent_tracker, "tracker", agent_tracker.AgentTracker())


@pytest.fixture
def as_remote(monkeypatch):
    """Every connection counts as another device (we can't fake a Tailscale
    peer on loopback)."""
    monkeypatch.setattr(hitl_server.HITLServer, "_is_remote_peer", staticmethod(lambda request: True))
    monkeypatch.setattr(island_remote, "tailscale_ip", lambda: TS)


async def connect_remote(e, pair):
    s = aiohttp.ClientSession()
    e.sessions.append(s)
    ws = await s.ws_connect(f"http://127.0.0.1:{e.server.port}/hitl")
    await ws.send_json({"type": "hello", "pair": pair})
    return ws


async def closed_with(ws):
    """The close code, skipping whatever was queued before the close."""
    async def wait():
        while True:
            msg = await ws.receive()
            if msg.type != aiohttp.WSMsgType.TEXT:
                # the frame's code: aiohttp's client reports 1006 in close_code when the
                # server hangs up right after its close frame (browsers report the frame's)
                return msg.data if msg.type == aiohttp.WSMsgType.CLOSE else ws.close_code
    return await asyncio.wait_for(wait(), 3)


# ── agent_update ──────────────────────────────────────────────────

def test_a_new_island_gets_every_agents_state():
    async def t():
        agent_tracker.update("office", "executing", task="Claude Code: tests", progress=50, model="opus")
        async with Env() as e:
            ws = await e.connect()
            agents = (await recv(ws, "agent_update"))["agents"]
            assert set(agents) == {"main", "minecraft", "office"}
            assert agents["office"]["status"] == "executing" and agents["office"]["model"] == "opus"
    run(t())


def test_changes_from_any_thread_are_pushed():
    async def t():
        async with Env() as e:
            ws = await e.connect()
            await recv(ws, "agent_update")
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: agent_tracker.update("main", "thinking", task="Generating response", model="haiku"))
            main = (await recv(ws, "agent_update"))["agents"]["main"]
            assert main["status"] == "thinking" and main["model"] == "haiku"
    run(t())


def test_a_stopped_server_stops_listening_to_the_tracker():
    async def t():
        async with Env() as e:
            assert e.server._tracking in agent_tracker.tracker._callbacks
            cb = e.server._tracking
        assert cb not in agent_tracker.tracker._callbacks
    run(t())


# ── approvals on the agent's card ─────────────────────────────────

def test_an_approval_marks_its_agent_waiting_then_restores_it():
    async def t():
        async with Env() as e:
            ws = await e.connect()
            await recv(ws, "hello_ok")                 # connected before asking, or it goes to the terminal
            agent_tracker.update("office", "executing", task="Claude Code: refactor", progress=50)
            ask = asyncio.ensure_future(e.server.request_approval("ask_claude_code", "{}", timeout=5, agent="office"))
            req = await recv(ws, "approval_request")
            assert req["agent"] == "office"
            office = agent_tracker.get("office")
            assert office["status"] == "waiting_approval" and "ask_claude_code" in office["task"]
            await ws.send_json({"type": "approval_response", "id": req["id"], "decision": "approve"})
            assert await ask is True
            office = agent_tracker.get("office")
            assert office["status"] == "executing" and office["task"] == "Claude Code: refactor"
    run(t())


def test_an_unknown_agent_is_treated_as_main():
    async def t():
        async with Env() as e:
            ws = await e.connect()
            await recv(ws, "hello_ok")
            ask = asyncio.ensure_future(e.server.request_approval("write_file", "{}", timeout=5, agent="boss"))
            req = await recv(ws, "approval_request")
            assert req["agent"] == "main" and agent_tracker.get("main")["status"] == "waiting_approval"
            await ws.send_json({"type": "approval_response", "id": req["id"], "decision": "decline"})
            assert await ask is False
            assert agent_tracker.get("main")["status"] == "idle"
    run(t())


# ── remote pairing ────────────────────────────────────────────────

def test_remote_devices_are_refused_while_remote_is_off(as_remote):
    async def t():
        async with Env() as e:
            with pytest.raises(aiohttp.WSServerHandshakeError) as err:
                await e.connect()
            assert err.value.status == 403
    run(t())


def test_the_right_pairing_code_gets_a_remote_island_in(as_remote):
    async def t():
        code = island_remote.enable()["code"]
        async with Env() as e:                         # Lumi's own token is set too; it isn't enough remotely
            ws = await connect_remote(e, code)
            hello = await recv(ws, "hello_ok")
            assert hello["remote"] is True
            await recv(ws, "agent_update")
            assert e.server.has_client
    run(t())


def test_the_local_token_is_not_a_remote_key(as_remote):
    async def t():
        island_remote.enable()
        async with Env() as e:
            ws = await e.connect(token=TOKEN)          # hello with the token, no pair
            assert await closed_with(ws) == 4401
            assert not e.server.has_client
    run(t())


def test_wrong_codes_lock_the_device_out(as_remote):
    async def t():
        code = island_remote.enable()["code"]
        async with Env() as e:
            for _ in range(island_remote.MAX_FAILURES):
                ws = await connect_remote(e, "WRNG-WRNG-WRNG-WRNG")
                assert await closed_with(ws) == 4401
            with pytest.raises(aiohttp.WSServerHandshakeError) as err:
                await connect_remote(e, code)
            assert err.value.status == 429
    run(t())


def test_a_new_code_disconnects_paired_islands(as_remote):
    async def t():
        code = island_remote.enable()["code"]
        async with Env() as e:
            ws = await connect_remote(e, code)
            await recv(ws, "hello_ok")
            island_remote.enable()                     # "New code" in the web app
            await e.server.apply_remote()
            assert await closed_with(ws) == 4401
            ws2 = await connect_remote(e, code)        # the old code is dead
            assert await closed_with(ws2) == 4401
    run(t())


def test_loopback_is_never_treated_as_remote():
    req = types.SimpleNamespace
    assert not hitl_server.HITLServer._is_remote_peer(req(remote="127.0.0.1"))
    assert not hitl_server.HITLServer._is_remote_peer(req(remote="::1"))
    assert hitl_server.HITLServer._is_remote_peer(req(remote=TS))


# ── Minecraft card ────────────────────────────────────────────────

def track(text="", gone=False, game="MinecraftAgent"):
    from sdk.server import SDKServer
    client = types.SimpleNamespace(game_name=game)
    SDKServer._track_minecraft(SDKServer.__new__(SDKServer), client, text, gone)
    return agent_tracker.get("minecraft")


def test_minecraft_card_follows_the_currently_line():
    mc = track("Health 20/20\nCurrently: mining iron at y=12\nNearby: zombie")
    assert mc["status"] == "executing" and mc["task"] == "mining iron at y=12" and mc["model_name"]
    assert track("Currently: idle, waiting for orders")["status"] == "idle"
    assert track("no status line here")["status"] == "idle"
    mc = track(gone=True)
    assert mc["status"] == "idle" and mc["task"] == "Not running"


def test_other_games_dont_touch_the_minecraft_card():
    track("Currently: mining", game="SomethingElse")
    assert agent_tracker.get("minecraft")["status"] == "idle" and agent_tracker.get("minecraft")["task"] == ""

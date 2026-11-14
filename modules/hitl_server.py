"""
Human-in-the-loop approval server.

A tiny aiohttp WebSocket endpoint that replaces the blocking input()
prompt for tool permissions. A client (dashboard, phone, tray app)
connects to ws://127.0.0.1:5090/hitl and answers approval requests.

Protocol
    server -> client  {"type": "approval_request", "id": <uuid>,
                       "tool": <name>, "preview": <text>}
    client -> server  {"type": "approval_response", "id": <uuid>,
                       "decision": "approve" | "decline"}

Idle cost is zero: no polling loops and no heartbeat timers. The loop
only wakes for socket I/O, a new request, or a request's timeout timer.

Must run on the same event loop that calls request_approval() (the
prompter's loop), so Futures resolve without cross-thread hand-offs.
"""

import asyncio
import json
import secrets
import uuid
from typing import Awaitable, Callable, Dict, Optional, Set
from urllib.parse import urlparse

from aiohttp import web, WSMsgType

from modules import agent_tracker, island_remote

HITL_HOST = "127.0.0.1"
HITL_PORT = 5090
HITL_PATH = "/hitl"

GRAY = "\033[90m"
YELLOW = "\033[93m"
RESET = "\033[0m"

# Browsers send an Origin header on WebSocket upgrades; any web page
# could otherwise open ws://127.0.0.1:5090 and approve shell commands.
# Non-browser clients (Python, Node) send no Origin and are allowed.
# tauri.localhost: the Lumi Island desktop app (island/), a Tauri webview.
_ALLOWED_ORIGIN_HOSTS = {"127.0.0.1", "localhost", "::1", "tauri.localhost"}

# Sentinel: the last client disconnected mid-request -> use terminal.
_NO_CLIENT = object()


class HITLServer:
    def __init__(self, host: str = HITL_HOST, port: int = HITL_PORT):
        self.host = host
        self.port = port
        self._clients: Set[web.WebSocketResponse] = set()
        self._pending: Dict[str, asyncio.Future] = {}
        self._runner: Optional[web.AppRunner] = None
        self._terminal_lock: Optional[asyncio.Lock] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._status = "idle"
        self._token: Optional[str] = None
        self._remote_site: Optional[web.TCPSite] = None     # the Tailscale listener, when turned on
        self._remote_host: Optional[str] = None
        self._remote_clients: Set[web.WebSocketResponse] = set()
        self._tracking = None          # our agent_tracker callback while running
        self._handlers: Dict[str, Callable[[web.WebSocketResponse, dict], Awaitable[None]]] = {}
        self.signals = None

    @property
    def running(self) -> bool:
        return self._runner is not None

    @property
    def has_client(self) -> bool:
        return bool(self._clients)

    # ── lifecycle ────────────────────────────────────────────────────

    async def start(self, signals=None) -> None:
        if self._runner is not None:
            return
        self.signals = signals
        self._terminal_lock = asyncio.Lock()
        self._loop = asyncio.get_running_loop()
        if signals is not None:
            signals.on_state_change = self._on_signals
            self._status = self._status_of(signals)
        app = web.Application()
        app.router.add_get(HITL_PATH, self._ws_handler)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, self.host, self.port).start()
        except OSError as e:
            await runner.cleanup()
            print(f"{YELLOW}[HITL] could not bind {self.host}:{self.port} "
                  f"({e}); approvals will use the terminal.{RESET}")
            return
        self._runner = runner
        print(f"{GRAY}[HITL] approval server on "
              f"ws://{self.host}:{self.port}{HITL_PATH}{RESET}")
        if self._tracking is None:
            self._tracking = lambda snap: self.broadcast_threadsafe({"type": "agent_update", "agents": snap})
            agent_tracker.register(self._tracking)
        err = await self.apply_remote()
        if err:
            print(f"{YELLOW}[HITL] remote island: {err}{RESET}")

    async def apply_remote(self) -> Optional[str]:
        """Open, move or close the Tailscale listener to match the saved
        setting (modules/island_remote.py). Every change also disconnects the
        remote islands, so a new code or turning it off takes effect at once.
        Returns an error message, or None."""
        want = island_remote.bind_host()
        for ws in list(self._remote_clients):
            await ws.close(code=4401, message=b"pairing changed")
        if self._remote_site is not None and want != self._remote_host:
            await self._remote_site.stop()
            self._remote_site, self._remote_host = None, None
            print(f"{GRAY}[HITL] remote island listener closed{RESET}")
        if want and self._remote_site is None and self._runner is not None:
            site = web.TCPSite(self._runner, want, self.port)
            try:
                await site.start()
            except OSError as e:
                return f"couldn't listen on {want}:{self.port} ({e.__class__.__name__}). Is Tailscale up?"
            self._remote_site, self._remote_host = site, want
            print(f"{GRAY}[HITL] remote islands can pair at ws://{want}:{self.port}{HITL_PATH}{RESET}")
        return None

    async def stop(self) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_result(False)
        self._pending.clear()
        for ws in list(self._clients):
            await ws.close()
        self._clients.clear()
        self._remote_clients.clear()
        if self._tracking is not None:
            agent_tracker.unregister(self._tracking)
            self._tracking = None
        if self._runner is not None:
            await self._runner.cleanup()      # also stops the Tailscale site
            self._runner = None
        self._remote_site, self._remote_host = None, None

    # ── websocket ────────────────────────────────────────────────────

    async def _ws_handler(self, request: web.Request) -> web.StreamResponse:
        origin = request.headers.get("Origin")
        if origin and urlparse(origin).hostname not in _ALLOWED_ORIGIN_HOSTS:
            return web.Response(status=403, text="origin not allowed")

        peer = request.remote or ""
        remote = self._is_remote_peer(request)
        if remote and not island_remote.is_enabled():
            return web.Response(status=403, text="remote islands are off")
        if remote and island_remote.locked(peer):
            return web.Response(status=429, text="too many wrong pairing codes; try again later")

        # heartbeat=None (the default) -> no ping timer while idle.
        ws = web.WebSocketResponse(heartbeat=None, max_msg_size=64 * 1024)
        await ws.prepare(request)
        if remote:
            # Another device: the pairing code is always required, whatever
            # the local token setting is.
            if not await self._authenticate_remote(ws, peer):
                await ws.close(code=4401, message=b"pairing failed")
                return ws
            self._remote_clients.add(ws)
        elif self._token is not None and not await self._authenticate(ws):
            await ws.close(code=4401, message=b"auth failed")
            return ws
        self._clients.add(ws)
        await self._safe_send(ws, {**self._hello_payload(), "remote": remote})
        await self._safe_send(ws, {"type": "lumi_status", "state": self._status})
        await self._safe_send(ws, {"type": "agent_update", "agents": agent_tracker.snapshot()})

        # A client that connects while requests are already waiting
        # should see them; otherwise they'd only ever time out.
        for req_id, fut in list(self._pending.items()):
            payload = getattr(fut, "hitl_payload", None)
            if payload and not fut.done():
                await self._safe_send(ws, payload)

        try:
            # Suspends on the socket until a frame arrives — no polling.
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    self._dispatch(ws, msg.data)
                elif msg.type == WSMsgType.ERROR:
                    break
        finally:
            self._clients.discard(ws)
            self._remote_clients.discard(ws)
            if not self._clients:
                # Nobody left to answer: hand open requests back to the
                # terminal instead of letting them silently time out.
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_result(_NO_CLIENT)
        return ws

    # ── handshake and extra message types ────────────────────────────

    def set_token(self, token: Optional[str]) -> None:
        """Require clients to open with {"type":"hello","token":...}. Set by
        the island manager, which starts the island with the same secret in
        its environment, so only the process Lumi launched can connect.
        With no token set the server behaves as before (loopback + origin
        check only)."""
        self._token = token or None

    def register_handler(self, msg_type: str,
                         fn: Callable[[web.WebSocketResponse, dict], Awaitable[None]]) -> None:
        """Route {"type": msg_type, ...} from a client to `await fn(ws, data)`.
        Used by the Lumi Island bridge (modules/island_bridge.py) so chat and
        calendar requests don't live in the approval code."""
        self._handlers[msg_type] = fn

    async def send(self, ws: web.WebSocketResponse, payload: dict) -> bool:
        return await self._safe_send(ws, payload)

    def broadcast_threadsafe(self, payload: dict) -> None:
        """Push to every client from any thread. No-op with no loop/clients."""
        loop = self._loop
        if loop is None or not self._clients:
            return
        try:
            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._broadcast(payload)))
        except RuntimeError:
            pass  # loop closed during shutdown

    async def _authenticate(self, ws: web.WebSocketResponse) -> bool:
        """The first frame must be hello with the right token (5 s to send it)."""
        try:
            msg = await asyncio.wait_for(ws.receive(), 5)
            if msg.type != WSMsgType.TEXT:
                return False
            data = json.loads(msg.data)
        except (asyncio.TimeoutError, ValueError, TypeError):
            return False
        if not isinstance(data, dict) or data.get("type") != "hello":
            return False
        return secrets.compare_digest(str(data.get("token") or ""), self._token or "")

    @staticmethod
    def _is_remote_peer(request) -> bool:
        return (request.remote or "") not in ("127.0.0.1", "::1")

    async def _authenticate_remote(self, ws: web.WebSocketResponse, peer: str) -> bool:
        """A remote island's first frame must be hello with the pairing code."""
        try:
            msg = await asyncio.wait_for(ws.receive(), 5)
            if msg.type != WSMsgType.TEXT:
                return False
            data = json.loads(msg.data)
        except (asyncio.TimeoutError, ValueError, TypeError):
            return False
        if not isinstance(data, dict) or data.get("type") != "hello":
            return False
        return island_remote.verify(data.get("pair"), peer)

    def _hello_payload(self) -> dict:
        import constants
        return {"type": "hello_ok", "gaming": bool(getattr(constants, "GAMING_MODE", False))}

    def _dispatch(self, ws: web.WebSocketResponse, raw: str) -> None:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return
        if not isinstance(data, dict):
            return
        kind = data.get("type")
        if kind == "approval_response":
            self._handle_approval(data)
            return
        if kind == "hello":      # a client without a token set still says hello
            asyncio.ensure_future(self._safe_send(ws, self._hello_payload()))
            return
        handler = self._handlers.get(kind)
        if handler is not None:
            asyncio.ensure_future(self._run_handler(handler, ws, data))

    async def _run_handler(self, handler, ws, data) -> None:
        try:
            await handler(ws, data)
        except Exception as e:                      # a bad request must not drop the socket
            await self._safe_send(ws, {"type": "error", "id": data.get("id"),
                                       "message": f"{type(e).__name__}: {str(e)[:120]}"})

    def _handle_approval(self, data: dict) -> None:
        fut = self._pending.get(str(data.get("id")))
        if fut is None or fut.done():
            return  # unknown, late (already timed out), or answered elsewhere
        decision = data.get("decision")
        if decision in ("approve", "decline"):
            fut.set_result(decision == "approve")

    async def _safe_send(self, ws: web.WebSocketResponse, payload: dict) -> bool:
        try:
            await ws.send_json(payload)
            return True
        except (ConnectionError, RuntimeError):
            self._clients.discard(ws)
            return False

    # ── live status (idle / thinking / speaking) ─────────────────────

    @staticmethod
    def _status_of(signals) -> str:
        if getattr(signals, "AI_speaking", False):
            return "speaking"
        if getattr(signals, "AI_thinking", False):
            return "thinking"
        return "idle"

    def _on_signals(self, signals) -> None:
        """Called from whichever thread flipped a flag; hop to our loop."""
        state = self._status_of(signals)
        if state == self._status or self._loop is None:
            return
        self._status = state
        try:
            self._loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(
                    self._broadcast({"type": "lumi_status", "state": state})))
        except RuntimeError:
            pass  # loop closed during shutdown

    # ── approval API ─────────────────────────────────────────────────

    async def request_approval(self, tool_name: str, preview_text: str,
                               timeout: float = 120, agent: str = "main") -> bool:
        """Ask the connected client(s) to approve a tool call.

        Returns True on approve, False on decline or timeout. Falls back
        to a terminal y/n prompt when no client is connected. The agent
        (main / minecraft / office) shows as waiting_approval meanwhile and
        goes back to what it was doing afterwards.
        """
        agent = agent if agent in agent_tracker.AGENTS else "main"
        before = agent_tracker.get(agent)
        agent_tracker.update(agent, "waiting_approval", task=f"Approve {tool_name}?")
        try:
            return await self._ask(tool_name, preview_text, timeout, agent)
        finally:
            agent_tracker.update(agent, before["status"], task=before["task"], progress=before["progress"])

    async def _ask(self, tool_name: str, preview_text: str, timeout: float, agent: str) -> bool:
        if not self._clients:
            return await self._terminal_confirm(tool_name, preview_text)

        loop = asyncio.get_running_loop()
        req_id = str(uuid.uuid4())
        payload = {
            "type": "approval_request",
            "id": req_id,
            "tool": tool_name,
            "preview": preview_text,
            "agent": agent,
        }
        fut = loop.create_future()
        fut.hitl_payload = payload
        self._pending[req_id] = fut

        try:
            # Broadcast; the first answer from any client wins.
            sent = [await self._safe_send(ws, payload) for ws in list(self._clients)]
            if not any(sent):
                return await self._terminal_confirm(tool_name, preview_text)

            print(f"{GRAY}[HITL] waiting for approval of '{tool_name}' "
                  f"({int(timeout)}s)...{RESET}")
            try:
                # wait_for arms a single timer handle — no polling.
                result = await asyncio.wait_for(asyncio.shield(fut), timeout)
            except asyncio.TimeoutError:
                print(f"{YELLOW}[HITL] '{tool_name}' timed out; "
                      f"auto-declined.{RESET}")
                await self._broadcast({"type": "approval_expired", "id": req_id})
                return False

            if result is _NO_CLIENT:
                return await self._terminal_confirm(tool_name, preview_text)
            print(f"{GRAY}[HITL] '{tool_name}' "
                  f"{'approved' if result else 'declined'}.{RESET}")
            return bool(result)
        finally:
            self._pending.pop(req_id, None)
            if not fut.done():
                fut.cancel()

    async def _broadcast(self, payload: dict) -> None:
        for ws in list(self._clients):
            await self._safe_send(ws, payload)

    async def _terminal_confirm(self, tool_name: str, preview_text: str) -> bool:
        # Serialize so two concurrent requests don't fight over stdin.
        lock = self._terminal_lock or asyncio.Lock()
        async with lock:
            if preview_text:
                print(f"\n{GRAY}{preview_text}{RESET}")
            prompt = f"{GRAY}  -> Allow '{tool_name}'? (y/n): {RESET}"
            loop = asyncio.get_running_loop()
            if self.signals is not None:
                self.signals.input_paused = True
            try:
                answer = await loop.run_in_executor(None, input, prompt)
            except (EOFError, KeyboardInterrupt):
                return False
            finally:
                if self.signals is not None:
                    self.signals.input_paused = False
            return answer.strip().lower() in ("y", "yes")


# Module-level singleton so callers can just import request_approval.
_server = HITLServer()


async def start(signals=None) -> HITLServer:
    await _server.start(signals)
    return _server


async def stop() -> None:
    await _server.stop()


async def request_approval(tool_name: str, preview_text: str,
                           timeout: float = 120, agent: str = "main") -> bool:
    return await _server.request_approval(tool_name, preview_text, timeout, agent)


# ── Helpers for the Lumi Island bridge (modules/island_bridge.py) ────

def set_token(token: Optional[str]) -> None:
    _server.set_token(token)


def register_handler(msg_type: str, fn) -> None:
    _server.register_handler(msg_type, fn)


def broadcast_threadsafe(payload: dict) -> None:
    _server.broadcast_threadsafe(payload)


async def send(ws, payload: dict) -> bool:
    return await _server.send(ws, payload)

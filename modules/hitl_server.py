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
import uuid
from typing import Dict, Optional, Set
from urllib.parse import urlparse

from aiohttp import web, WSMsgType

HITL_HOST = "127.0.0.1"
HITL_PORT = 5090
HITL_PATH = "/hitl"

GRAY = "\033[90m"
YELLOW = "\033[93m"
RESET = "\033[0m"

# Browsers send an Origin header on WebSocket upgrades; any web page
# could otherwise open ws://127.0.0.1:5090 and approve shell commands.
# Non-browser clients (Python, Node) send no Origin and are allowed.
_ALLOWED_ORIGIN_HOSTS = {"127.0.0.1", "localhost", "::1"}

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

    async def stop(self) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_result(False)
        self._pending.clear()
        for ws in list(self._clients):
            await ws.close()
        self._clients.clear()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # ── websocket ────────────────────────────────────────────────────

    async def _ws_handler(self, request: web.Request) -> web.StreamResponse:
        origin = request.headers.get("Origin")
        if origin and urlparse(origin).hostname not in _ALLOWED_ORIGIN_HOSTS:
            return web.Response(status=403, text="origin not allowed")

        # heartbeat=None (the default) -> no ping timer while idle.
        ws = web.WebSocketResponse(heartbeat=None, max_msg_size=64 * 1024)
        await ws.prepare(request)
        self._clients.add(ws)

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
                    self._handle_message(msg.data)
                elif msg.type == WSMsgType.ERROR:
                    break
        finally:
            self._clients.discard(ws)
            if not self._clients:
                # Nobody left to answer: hand open requests back to the
                # terminal instead of letting them silently time out.
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_result(_NO_CLIENT)
        return ws

    def _handle_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return
        if not isinstance(data, dict) or data.get("type") != "approval_response":
            return
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

    # ── approval API ─────────────────────────────────────────────────

    async def request_approval(self, tool_name: str, preview_text: str,
                               timeout: float = 120) -> bool:
        """Ask the connected client(s) to approve a tool call.

        Returns True on approve, False on decline or timeout. Falls back
        to a terminal y/n prompt when no client is connected.
        """
        if not self._clients:
            return await self._terminal_confirm(tool_name, preview_text)

        loop = asyncio.get_running_loop()
        req_id = str(uuid.uuid4())
        payload = {
            "type": "approval_request",
            "id": req_id,
            "tool": tool_name,
            "preview": preview_text,
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
                           timeout: float = 120) -> bool:
    return await _server.request_approval(tool_name, preview_text, timeout)

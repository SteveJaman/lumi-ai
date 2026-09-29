"""
Lumi SDK — server side.

Runs inside Lumi's main process. Binds strictly to 127.0.0.1:5056,
accepts websocket connections from external apps, tracks registered
actions, pushes context into the prompt, and dispatches invocations.

The server exposes three methods the prompter uses:

    get_action_schemas()    → tool schemas in Ollama's format
    get_context_injections() → list[str] to append to the prompt
    find_action(tool_name)  → resolve "sdk__<name>" to a spec
    invoke_action_blocking(name, args) → run an action, return result

The public API surface is intentionally small. Everything else is
protocol plumbing.
"""

from __future__ import annotations

import os
import json
import time
import uuid
import asyncio
import secrets
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from aiohttp import web, WSMsgType

from constants import WORKSPACE_ROOT, SDK_HOST, SDK_PORT, SDK_VERSION
from modules.module import Module
from modules.log_hub import emit as hub_emit

from sdk.errors import AuthError
from sdk.protocol import (
    SDK_PROTOCOL_VERSION,
    ClientCommand,
    ServerCommand,
    ActionSpec,
    Session,
    action_message,
    error_message,
    speech_finished_message,
    startup_message,
)


HELLO_TIMEOUT_SECONDS = 10
INVOKE_TIMEOUT_SECONDS = 30
PING_INTERVAL_SECONDS = 30

_TOKEN_PATH = os.path.join(WORKSPACE_ROOT, "memories", "sdk_token.txt")


def _load_or_create_token() -> str:
    """Read the SDK token from disk, or generate and persist a fresh
    one. Written with 0600 permissions on POSIX."""
    if os.path.exists(_TOKEN_PATH):
        try:
            with open(_TOKEN_PATH, "r", encoding="utf-8") as f:
                tok = f.read().strip()
            if tok:
                return tok
        except Exception:
            pass

    tok = secrets.token_urlsafe(32)
    try:
        os.makedirs(os.path.dirname(_TOKEN_PATH), exist_ok=True)
        with open(_TOKEN_PATH, "w", encoding="utf-8") as f:
            f.write(tok)
        try:
            os.chmod(_TOKEN_PATH, 0o600)
        except Exception:
            pass
    except Exception as e:
        print(f"\033[91mSDK: could not persist token: {e}\033[0m")
    return tok


# ─── Internal state ───────────────────────────────────────────────

@dataclass
class RegisteredAction:
    spec: ActionSpec
    client_id: str
    registered_at: float


@dataclass
class SDKClient:
    client_id: str
    client_name: str
    client_version: str
    game_name: str
    ws: web.WebSocketResponse
    connected_at: float
    actions: Dict[str, str] = field(default_factory=dict)
    pending_invocations: Dict[str, asyncio.Future] = field(default_factory=dict)
    context_text: str = ""
    context_priority: int = 150


# ─── Server ───────────────────────────────────────────────────────

class SDKServer(Module):
    """Websocket server module. Spawned by main.py like any other
    module and given its own event loop thread."""

    def __init__(
        self,
        signals,
        enabled: bool = True,
        host: str = SDK_HOST,
        port: int = SDK_PORT,
    ):
        super().__init__(signals, enabled)
        self.host = host
        self.port = port
        self.token = _load_or_create_token()
        self.clients: Dict[str, SDKClient] = {}
        self.actions: Dict[str, RegisteredAction] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._runner: Optional[web.AppRunner] = None

        # Register on signals so the prompter finds us.
        signals.sdk = self

    # ─── Lifecycle ────────────────────────────────────────────

    async def run(self):
        if not self.enabled:
            return

        self._loop = asyncio.get_running_loop()

        app = web.Application()
        app.router.add_get("/ws", self._handle_ws)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        try:
            await site.start()
        except Exception as e:
            hub_emit(self.signals, "sdk",
                     f"bind failed on {self.host}:{self.port} — {e}",
                     level="error")
            return

        hub_emit(self.signals, "sdk",
                 f"listening on ws://{self.host}:{self.port}/ws "
                 f"(protocol v{SDK_PROTOCOL_VERSION}, sdk v{SDK_VERSION})")

        ping_task = asyncio.create_task(self._ping_loop())
        try:
            while not self.signals.terminate:
                await asyncio.sleep(1.0)
        finally:
            ping_task.cancel()
            try:
                await self._runner.cleanup()
            except Exception:
                pass

    async def _ping_loop(self):
        while not self.signals.terminate:
            await asyncio.sleep(PING_INTERVAL_SECONDS)
            for cid in list(self.clients.keys()):
                client = self.clients.get(cid)
                if client is None or client.ws.closed:
                    self._cleanup_client(cid)
                    continue
                try:
                    await client.ws.send_json({"command": ServerCommand.PING})
                except Exception:
                    self._cleanup_client(cid)

    # ─── Connection handling ──────────────────────────────────

    async def _handle_ws(self, request):
        ws = web.WebSocketResponse(heartbeat=None,
                                    max_msg_size=4 * 1024 * 1024)
        await ws.prepare(request)

        client_id: Optional[str] = None
        try:
            hello = await self._receive_hello(ws)
            if hello is None:
                return ws

            try:
                game_name = self._validate_hello(hello)
            except AuthError as e:
                await ws.send_json(error_message(str(e)))
                hub_emit(self.signals, "sdk",
                         f"rejected client — {e}", level="warn")
                await ws.close()
                return ws

            client_id = str(uuid.uuid4())
            client = SDKClient(
                client_id=client_id,
                client_name=str(hello.get("client_name") or game_name),
                client_version=str(hello.get("client_version") or ""),
                game_name=game_name,
                ws=ws,
                connected_at=time.time(),
            )
            self.clients[client_id] = client

            await ws.send_json(startup_message(
                Session(session_id=client_id, display_name="Lumi Voidwright"),
                protocol_version=SDK_PROTOCOL_VERSION,
            ))
            hub_emit(self.signals, "sdk",
                     f"client connected: {client.client_name} "
                     f"({client_id[:8]}) game={game_name!r}")

            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        await ws.send_json(error_message("invalid JSON"))
                        continue
                    await self._handle_message(client, data)
                elif msg.type == WSMsgType.ERROR:
                    break
        finally:
            if client_id:
                self._cleanup_client(client_id)
        return ws

    async def _receive_hello(self, ws) -> Optional[Dict[str, Any]]:
        try:
            raw = await asyncio.wait_for(
                ws.receive_json(), timeout=HELLO_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            await ws.send_json(error_message("hello timeout"))
            await ws.close()
            return None
        except Exception:
            await ws.close()
            return None

        if not isinstance(raw, dict):
            await ws.send_json(error_message("hello must be an object"))
            await ws.close()
            return None

        # Accept legacy {"type": "hello"} shape for backwards compat
        # with the old sdk_example.py and the Minecraft bridge.
        if raw.get("type") == "hello" and "command" not in raw:
            raw["command"] = "hello"

        return raw

    def _validate_hello(self, msg: Dict[str, Any]) -> str:
        provided = str(msg.get("auth_token") or "")
        if not secrets.compare_digest(provided, self.token):
            raise AuthError("invalid auth_token")
        game = str(msg.get("game") or msg.get("client_name") or "unnamed")
        return game

    # ─── Message dispatch ─────────────────────────────────────

    async def _handle_message(self, client: SDKClient, data: Dict[str, Any]):
        # Accept both new protocol and legacy shapes.
        cmd = data.get("command") or data.get("type")

        if cmd in (ClientCommand.ACTIONS_REGISTER, "register_action"):
            await self._handle_register(client, data)
        elif cmd in (ClientCommand.ACTIONS_UNREGISTER, "unregister_action"):
            self._handle_unregister(client, data)
        elif cmd in (ClientCommand.CONTEXT, "context"):
            self._handle_context(client, data)
        elif cmd in (ClientCommand.CONTEXT_CLEAR, "clear_context"):
            client.context_text = ""
        elif cmd in (ClientCommand.ACTION_RESULT, "invoke_result"):
            self._handle_action_result(client, data)
        elif cmd in (ClientCommand.PONG, "pong"):
            pass
        elif cmd == ClientCommand.ACTIONS_FORCE:
            await self._handle_force(client, data)
        else:
            await client.ws.send_json(error_message(f"unknown command: {cmd}"))

    async def _handle_register(self, client: SDKClient, data: Dict[str, Any]):
        # New shape: {"command": "actions/register", "data": {"actions": [...]}}
        # Legacy shape: {"type": "register_action", "action": {...}}
        payload: List[Dict[str, Any]] = []
        if isinstance(data.get("data"), dict) and "actions" in data["data"]:
            payload = list(data["data"]["actions"])
        elif isinstance(data.get("action"), dict):
            payload = [data["action"]]

        for raw in payload:
            spec = ActionSpec.from_wire(raw)
            if not spec.name:
                continue
            # Replace per Neuro spec.
            self.actions.pop(spec.name, None)
            self.actions[spec.name] = RegisteredAction(
                spec=spec, client_id=client.client_id,
                registered_at=time.time(),
            )
            client.actions[spec.name] = spec.name

            await client.ws.send_json({
                "command": ServerCommand.ACTIONS_REGISTERED,
                "data": {"action_name": spec.name},
            })
            hub_emit(self.signals, "sdk",
                     f"registered: {spec.name} "
                     f"(client={client.client_name}, caps={spec.capabilities})")

    def _handle_unregister(self, client: SDKClient, data: Dict[str, Any]):
        if isinstance(data.get("data"), dict) and "action_names" in data["data"]:
            names = list(data["data"]["action_names"])
        elif data.get("action_name"):
            names = [data["action_name"]]
        else:
            names = []
        for name in names:
            rec = self.actions.get(name)
            if rec and rec.client_id == client.client_id:
                self.actions.pop(name, None)
                client.actions.pop(name, None)

    def _handle_context(self, client: SDKClient, data: Dict[str, Any]):
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        text = str(payload.get("message") or data.get("text") or "")
        client.context_text = text
        priority = payload.get("priority")
        if isinstance(priority, int):
            client.context_priority = priority

    def _handle_action_result(self, client: SDKClient, data: Dict[str, Any]):
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        inv_id = str(payload.get("id") or payload.get("invocation_id") or "")
        future = client.pending_invocations.pop(inv_id, None)
        if future is None or future.done():
            return
        success = bool(payload.get("success", True))
        future.set_result({
            "success": success,
            "result": payload.get("message") or payload.get("result") or "",
            "error": None if success else (payload.get("message") or "unknown error"),
        })

    async def _handle_force(self, client: SDKClient, data: Dict[str, Any]):
        """Force action handling is deferred — it needs a prompter
        hook that bypasses normal tool selection. For now, log it and
        return an error so clients don't wait forever."""
        hub_emit(self.signals, "sdk",
                 "actions/force received but not yet wired to the prompter",
                 level="warn")
        await client.ws.send_json(error_message(
            "actions/force is not yet implemented on this build"
        ))

    def _cleanup_client(self, client_id: str):
        client = self.clients.pop(client_id, None)
        if client is None:
            return
        for fut in client.pending_invocations.values():
            if not fut.done():
                fut.set_result({"success": False, "error": "client disconnected"})
        client.pending_invocations.clear()
        for name in list(client.actions.keys()):
            self.actions.pop(name, None)
        hub_emit(self.signals, "sdk",
                 f"client disconnected: {client.client_name} ({client_id[:8]})")

    # ─── Public API used by the prompter ──────────────────────

    def get_action_schemas(self) -> List[Dict[str, Any]]:
        """Tool schemas in Ollama's format, with an 'sdk__' prefix so
        they can't collide with built-in tools."""
        out = []
        for rec in self.actions.values():
            out.append({
                "type": "function",
                "function": {
                    "name": f"sdk__{rec.spec.name}",
                    "description": rec.spec.description,
                    "parameters": rec.spec.schema
                        or {"type": "object", "properties": {}},
                },
            })
        return out

    def get_context_injections(self) -> List[str]:
        """Free-text context from connected clients, sorted by priority
        ascending (lower numbers come first)."""
        items = []
        for c in self.clients.values():
            if c.context_text.strip():
                items.append((c.context_priority, c.context_text))
        items.sort(key=lambda x: x[0])
        return [text for _, text in items]

    def find_action(self, tool_name: str) -> Optional[ActionSpec]:
        """Resolve 'sdk__<name>' back to the ActionSpec, or None."""
        if not tool_name.startswith("sdk__"):
            return None
        rec = self.actions.get(tool_name[len("sdk__"):])
        return rec.spec if rec else None

    def invoke_action_blocking(
        self,
        action_name: str,
        arguments: Dict[str, Any],
        timeout: int = INVOKE_TIMEOUT_SECONDS,
    ) -> Dict[str, Any]:
        """Thread-safe blocking invocation for the prompter's executor.
        Returns {'success': bool, 'result': str|None, 'error': str|None}."""
        if self._loop is None:
            return {"success": False, "error": "SDK is not running"}
        fut = asyncio.run_coroutine_threadsafe(
            self._invoke_action(action_name, arguments, timeout),
            self._loop,
        )
        try:
            return fut.result(timeout=timeout + 5)
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _invoke_action(
        self,
        action_name: str,
        arguments: Dict[str, Any],
        timeout: int,
    ) -> Dict[str, Any]:
        rec = self.actions.get(action_name)
        if rec is None:
            return {"success": False, "error": f"unknown action '{action_name}'"}
        client = self.clients.get(rec.client_id)
        if client is None or client.ws.closed:
            return {"success": False, "error": "client disconnected"}

        inv_id = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        client.pending_invocations[inv_id] = future

        try:
            await client.ws.send_json(action_message(
                inv_id, action_name, arguments or {},
            ))
        except Exception as e:
            client.pending_invocations.pop(inv_id, None)
            return {"success": False, "error": f"send failed: {e}"}

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            client.pending_invocations.pop(inv_id, None)
            return {"success": False, "error": "action timed out"}

    def broadcast_lumi_message(self, content: str, role: str = "assistant"):
        """Fan out a speech_finished message to every connected client.
        Called by the prompter on every reply."""
        if not self.clients:
            return
        payload = speech_finished_message(content, role=role,
                                          is_final=True, cancelled=False)

        async def _send_all():
            for c in list(self.clients.values()):
                if c.ws.closed:
                    continue
                try:
                    await c.ws.send_json(payload)
                except Exception:
                    pass

        if self._loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(_send_all(), self._loop)
        except Exception:
            pass
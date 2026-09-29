"""
Lumi External SDK — server module.

Inverts the tool direction. External applications connect over
websocket, register their own actions, send context, and receive
invocations. Lumi treats each registered action as a tool that her
LLM can call, exactly like her built-in tools.

Protocol
--------
JSON messages, one per websocket frame. Connection URL is
ws://127.0.0.1:5056/ws

Client → Server:
  {"type": "hello", "client_name": "...", "client_version": "...",
   "auth_token": "..."}
  {"type": "register_action", "action": {
      "name": "take_screenshot",
      "description": "Capture the primary monitor",
      "parameters": {JSON Schema},          # same shape as Ollama tools
      "capabilities": ["read"],             # read | write | dangerous
      "requires_confirm": false,
  }}
  {"type": "unregister_action", "action_name": "take_screenshot"}
  {"type": "context", "text": "...", "priority": 150}
  {"type": "clear_context"}
  {"type": "invoke_result", "invocation_id": "...", "success": true,
   "result": "..."}
  {"type": "invoke_result", "invocation_id": "...", "success": false,
   "error": "..."}
  {"type": "pong"}

Server → Client:
  {"type": "welcome", "session_id": "...", "server_version": "0.10.14"}
  {"type": "error", "message": "..."}
  {"type": "action_registered", "action_name": "..."}
  {"type": "action_unregistered", "action_name": "..."}
  {"type": "invoke", "invocation_id": "...", "action_name": "...",
   "arguments": {...}}
  {"type": "lumi_message", "content": "...", "role": "assistant",
   "timestamp": 1234567890.0}
  {"type": "ping"}

Auth
----
On first run, a random token is generated and written to
memories/sdk_token.txt with 0600 perms on POSIX. Clients must
present it in the hello message. Mismatch = connection refused.
The server binds strictly to 127.0.0.1.

Capability scoping
------------------
  read       no side effects, auto-approved
  write      modifies external state, auto-approved but logged
  dangerous  always requires the user's y/n confirmation

Tool naming
-----------
Registered actions are exposed to the LLM as "sdk__<action_name>".
The prefix prevents collisions with built-in tools and lets the
prompter route invocations cleanly.
"""

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

from modules.module import Module
from constants import WORKSPACE_ROOT, SDK_HOST, SDK_PORT, SDK_VERSION
from modules.log_hub import emit as hub_emit


HELLO_TIMEOUT_SECONDS = 10
DEFAULT_INVOKE_TIMEOUT = 30
PING_INTERVAL = 30

_TOKEN_PATH = os.path.join(WORKSPACE_ROOT, "memories", "sdk_token.txt")


def _load_or_create_token() -> str:
    """Read the SDK token from disk, or generate and save a new one."""
    if os.path.exists(_TOKEN_PATH):
        try:
            with open(_TOKEN_PATH, "r", encoding="utf-8") as f:
                token = f.read().strip()
            if token:
                return token
        except Exception:
            pass

    token = secrets.token_urlsafe(32)
    try:
        os.makedirs(os.path.dirname(_TOKEN_PATH), exist_ok=True)
        with open(_TOKEN_PATH, "w", encoding="utf-8") as f:
            f.write(token)
        try:
            os.chmod(_TOKEN_PATH, 0o600)
        except Exception:
            pass
    except Exception as e:
        print(f"\033[91mSDK: could not persist token: {e}\033[0m")
    return token


@dataclass
class RegisteredAction:
    name: str
    description: str
    parameters: Dict[str, Any]
    capabilities: List[str]
    requires_confirm: bool
    client_id: str
    registered_at: float


@dataclass
class SDKClient:
    client_id: str
    client_name: str
    client_version: str
    ws: web.WebSocketResponse
    connected_at: float
    actions: Dict[str, str] = field(default_factory=dict)
    pending_invocations: Dict[str, asyncio.Future] = field(default_factory=dict)
    context_text: str = ""
    context_priority: int = 150


class SDKModule(Module):
    def __init__(self, signals, enabled=True, port=SDK_PORT):
        super().__init__(signals, enabled)
        self.port = port
        self.token = _load_or_create_token()
        self.clients: Dict[str, SDKClient] = {}
        self.actions: Dict[str, RegisteredAction] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._runner: Optional[web.AppRunner] = None
        self._lock = threading.Lock()

        # Register on signals so prompter can find us.
        signals.sdk = self

    async def run(self):
        if not self.enabled:
            return

        self._loop = asyncio.get_running_loop()

        app = web.Application()
        app.router.add_get("/ws", self._handle_ws)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, SDK_HOST, self.port)
        try:
            await site.start()
        except Exception as e:
            hub_emit(self.signals, "sdk",
                     f"could not bind to {SDK_HOST}:{self.port} — {e}",
                     level="error")
            return

        hub_emit(self.signals, "sdk",
                 f"listening on ws://{SDK_HOST}:{self.port}/ws")

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
            await asyncio.sleep(PING_INTERVAL)
            dead = []
            for cid, client in list(self.clients.items()):
                if client.ws.closed:
                    dead.append(cid)
                    continue
                try:
                    await client.ws.send_json({"type": "ping"})
                except Exception:
                    dead.append(cid)
            for cid in dead:
                self._cleanup_client(cid)

    async def _handle_ws(self, request):
        ws = web.WebSocketResponse(heartbeat=None,
                                    max_msg_size=4 * 1024 * 1024)
        await ws.prepare(request)

        client_id = None
        try:
            # First message must be hello, within timeout.
            try:
                first = await asyncio.wait_for(
                    ws.receive_json(), timeout=HELLO_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                await ws.send_json({
                    "type": "error",
                    "message": f"hello timeout — send hello within "
                               f"{HELLO_TIMEOUT_SECONDS}s",
                })
                await ws.close()
                return ws
            except Exception:
                await ws.close()
                return ws

            if not isinstance(first, dict) or first.get("type") != "hello":
                await ws.send_json({
                    "type": "error",
                    "message": "first message must be hello",
                })
                await ws.close()
                return ws

            provided_token = str(first.get("auth_token") or "")
            if not secrets.compare_digest(provided_token, self.token):
                await ws.send_json({
                    "type": "error",
                    "message": "invalid auth_token",
                })
                hub_emit(self.signals, "sdk",
                         "rejected client — bad token", level="warn")
                await ws.close()
                return ws

            client_id = str(uuid.uuid4())
            client = SDKClient(
                client_id=client_id,
                client_name=str(first.get("client_name") or "unnamed"),
                client_version=str(first.get("client_version") or ""),
                ws=ws,
                connected_at=time.time(),
            )
            self.clients[client_id] = client

            await ws.send_json({
                "type": "welcome",
                "session_id": client_id,
                "server_version": SDK_VERSION,
            })
            hub_emit(self.signals, "sdk",
                     f"client connected: {client.client_name} "
                     f"({client_id[:8]})")

            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        await ws.send_json({
                            "type": "error",
                            "message": "invalid JSON",
                        })
                        continue
                    await self._handle_message(client, data)
                elif msg.type == WSMsgType.ERROR:
                    break
        finally:
            if client_id:
                self._cleanup_client(client_id)
        return ws

    async def _handle_message(self, client, data):
        mtype = data.get("type")

        if mtype == "register_action":
            await self._handle_register(client, data.get("action") or {})
        elif mtype == "unregister_action":
            self._handle_unregister(client, data.get("action_name") or "")
        elif mtype == "context":
            client.context_text = str(data.get("text") or "")
            client.context_priority = int(data.get("priority") or 150)
        elif mtype == "clear_context":
            client.context_text = ""
        elif mtype == "invoke_result":
            self._handle_invoke_result(client, data)
        elif mtype == "pong":
            pass
        else:
            await client.ws.send_json({
                "type": "error",
                "message": f"unknown message type: {mtype}",
            })

    async def _handle_register(self, client, action):
        name = str(action.get("name") or "").strip()
        if not name:
            await client.ws.send_json({
                "type": "error",
                "message": "action.name is required",
            })
            return

        if name in self.actions:
            await client.ws.send_json({
                "type": "error",
                "message": f"action '{name}' is already registered",
            })
            return

        spec = RegisteredAction(
            name=name,
            description=str(action.get("description") or ""),
            parameters=action.get("parameters") or {
                "type": "object", "properties": {},
            },
            capabilities=list(action.get("capabilities") or ["read"]),
            requires_confirm=bool(action.get("requires_confirm", False)),
            client_id=client.client_id,
            registered_at=time.time(),
        )
        self.actions[name] = spec
        client.actions[name] = name

        await client.ws.send_json({
            "type": "action_registered",
            "action_name": name,
        })
        hub_emit(self.signals, "sdk",
                 f"registered action: {name} "
                 f"(client={client.client_name}, "
                 f"caps={spec.capabilities})")

    def _handle_unregister(self, client, name):
        spec = self.actions.get(name)
        if not spec or spec.client_id != client.client_id:
            return
        del self.actions[name]
        client.actions.pop(name, None)

    def _handle_invoke_result(self, client, data):
        inv_id = str(data.get("invocation_id") or "")
        future = client.pending_invocations.pop(inv_id, None)
        if future is None or future.done():
            return
        success = bool(data.get("success", True))
        if success:
            future.set_result({
                "success": True,
                "result": data.get("result", ""),
            })
        else:
            future.set_result({
                "success": False,
                "error": str(data.get("error") or "unknown error"),
            })

    def _cleanup_client(self, client_id):
        client = self.clients.pop(client_id, None)
        if client is None:
            return
        for fut in client.pending_invocations.values():
            if not fut.done():
                fut.set_result({
                    "success": False,
                    "error": "client disconnected",
                })
        client.pending_invocations.clear()
        for name in list(client.actions.keys()):
            self.actions.pop(name, None)
        hub_emit(self.signals, "sdk",
                 f"client disconnected: {client.client_name} "
                 f"({client_id[:8]})")

    # ─── Public API for the prompter ───────────────────────────

    def get_action_schemas(self):
        """Tool schemas for every registered action, in the same shape
        as TOOL_SCHEMAS. Names are prefixed with 'sdk__'."""
        out = []
        for spec in self.actions.values():
            out.append({
                "type": "function",
                "function": {
                    "name": f"sdk__{spec.name}",
                    "description": spec.description,
                    "parameters": spec.parameters,
                },
            })
        return out

    def get_context_injections(self):
        """Return free-text context from connected clients, sorted by
        priority ascending."""
        items = []
        for client in self.clients.values():
            if client.context_text:
                items.append((client.context_priority, client.context_text))
        items.sort(key=lambda x: x[0])
        return [text for _, text in items]

    def find_action(self, tool_name):
        """Resolve 'sdk__<name>' to a RegisteredAction, or None."""
        if not tool_name.startswith("sdk__"):
            return None
        return self.actions.get(tool_name[len("sdk__"):])

    def invoke_action_blocking(self, action_name, arguments, timeout=30):
        """Thread-safe blocking invocation for the prompter's executor."""
        if self._loop is None:
            return {"success": False, "error": "SDK not running"}
        fut = asyncio.run_coroutine_threadsafe(
            self._invoke_action(action_name, arguments, timeout),
            self._loop,
        )
        try:
            return fut.result(timeout=timeout + 5)
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _invoke_action(self, action_name, arguments, timeout):
        spec = self.actions.get(action_name)
        if spec is None:
            return {"success": False,
                    "error": f"unknown action '{action_name}'"}
        client = self.clients.get(spec.client_id)
        if client is None or client.ws.closed:
            return {"success": False, "error": "client disconnected"}

        inv_id = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        client.pending_invocations[inv_id] = future

        try:
            await client.ws.send_json({
                "type": "invoke",
                "invocation_id": inv_id,
                "action_name": action_name,
                "arguments": arguments or {},
            })
        except Exception as e:
            client.pending_invocations.pop(inv_id, None)
            return {"success": False, "error": f"send failed: {e}"}

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            client.pending_invocations.pop(inv_id, None)
            return {"success": False, "error": "action timed out"}

    # ─── Lifecycle hook ────────────────────────────────────────

    def broadcast_lumi_message(self, content, role="assistant"):
        """Called by the prompter on every reply. Fans the message out
        to every connected SDK client."""
        if not self.clients:
            return
        payload = {
            "type": "lumi_message",
            "content": str(content),
            "role": role,
            "timestamp": time.time(),
        }

        async def _send_all():
            for cid, client in list(self.clients.items()):
                if client.ws.closed:
                    continue
                try:
                    await client.ws.send_json(payload)
                except Exception:
                    pass

        if self._loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(_send_all(), self._loop)
        except Exception:
            pass
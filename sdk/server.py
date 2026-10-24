"""
Lumi SDK — server side.

Runs inside Lumi's main process. Binds strictly to 127.0.0.1:5056,
accepts websocket connections from external apps, tracks registered
actions, pushes context into the prompt, and dispatches invocations.

Phase 9.2 / 9.3 — per-client approval and capability policy:

  - An allowlist lives at data/memories/sdk_allowlist.json.
  - On connect, the client's game name is looked up.
  - If the client is not in the allowlist, or is listed with
    approved=false, the connection stays open but ALL action
    registrations are refused. The client still receives
    speech_finished broadcasts so it can observe.
  - Each approved client has a capability ceiling. Actions whose
    declared capabilities exceed the ceiling are refused at
    registration time.
  - is_action_allowed(name) is consulted by the prompter before
    dispatch. client_autoapproves_writes(name) tells the prompter
    whether a 'write' action needs an interactive y/n.
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
from typing import Any, Dict, List, Optional, Tuple

from aiohttp import web, WSMsgType

from constants import WORKSPACE_ROOT, SDK_HOST, SDK_PORT, SDK_VERSION, DATA_ROOT
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

_TOKEN_PATH = os.path.join(DATA_ROOT, "memories", "sdk_token.txt")
_ALLOWLIST_PATH = os.path.join(DATA_ROOT, "memories", "sdk_allowlist.json")

_VALID_CAPABILITIES = {"read", "write", "dangerous"}


# ─── Token + allowlist persistence ─────────────────────────────────

def _load_or_create_token() -> str:
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


def _load_allowlist() -> Dict[str, Any]:
    """Return {'deny_by_default': bool, 'clients': {name: {...}}}.
    Creates an empty file on first call."""
    if not os.path.exists(_ALLOWLIST_PATH):
        default = {"deny_by_default": True, "clients": {}}
        try:
            os.makedirs(os.path.dirname(_ALLOWLIST_PATH), exist_ok=True)
            with open(_ALLOWLIST_PATH, "w", encoding="utf-8") as f:
                json.dump(default, f, indent=2)
        except Exception:
            pass
        return default
    try:
        with open(_ALLOWLIST_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"deny_by_default": True, "clients": {}}
        data.setdefault("deny_by_default", True)
        data.setdefault("clients", {})
        if not isinstance(data["clients"], dict):
            data["clients"] = {}
        return data
    except Exception:
        return {"deny_by_default": True, "clients": {}}


def _save_allowlist(data: Dict[str, Any]) -> bool:
    try:
        os.makedirs(os.path.dirname(_ALLOWLIST_PATH), exist_ok=True)
        with open(_ALLOWLIST_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        try:
            os.chmod(_ALLOWLIST_PATH, 0o600)
        except Exception:
            pass
        return True
    except Exception:
        return False


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
    approved: bool = False
    capability_ceiling: Tuple[str, ...] = ("read",)
    auto_approve_writes: bool = False
    actions: Dict[str, str] = field(default_factory=dict)
    pending_invocations: Dict[str, asyncio.Future] = field(default_factory=dict)
    context_text: str = ""
    context_priority: int = 150
    allow_reactions: bool = True
    last_reaction: float = 0.0


# Minimum gap between event turns one client can trigger. Events that
# arrive sooner, or while Lumi is mid-turn, are dropped: a reaction to
# something that happened a minute ago reads worse than no reaction.
REACTION_COOLDOWN_S = 12.0
MAX_REACTION_CHARS = 600


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
        self._allowlist_lock = threading.Lock()
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
        try:
            from constants import ALLOW_LAN_EXPOSURE
        except Exception:
            ALLOW_LAN_EXPOSURE = False

        bind_host = "0.0.0.0" if ALLOW_LAN_EXPOSURE else SDK_HOST
        if bind_host != "127.0.0.1":
            hub_emit(self.signals, "sdk",
                    f"WARNING — binding to {bind_host} (LAN exposure enabled)",
                    level="warn")

        site = web.TCPSite(self._runner, bind_host, self.port)
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

    # ─── Allowlist helpers ────────────────────────────────────

    def _lookup_client_policy(self, game_name: str) -> Dict[str, Any]:
        """Return the allowlist entry for this client, or a default
        deny entry if not present."""
        with self._allowlist_lock:
            data = _load_allowlist()
        clients = data.get("clients", {})
        entry = clients.get(game_name)
        if entry is None:
            return {
                "approved": False,
                "capability_ceiling": ["read"],
                "auto_approve_writes": False,
                "allow_reactions": False,
            }
        approved = bool(entry.get("approved", False))
        ceiling = entry.get("capability_ceiling") or ["read"]
        if not isinstance(ceiling, list):
            ceiling = ["read"]
        ceiling = [c for c in ceiling if c in _VALID_CAPABILITIES]
        if "read" not in ceiling:
            ceiling.append("read")
        return {
            "approved": approved,
            "capability_ceiling": ceiling,
            "auto_approve_writes": bool(entry.get("auto_approve_writes", False)),
            "allow_reactions": bool(entry.get("allow_reactions", True)),
        }

    def _record_seen_client(self, game_name: str, client_version: str):
        """If the client isn't in the allowlist yet, add a denied entry
        so the user has a template to edit. Never auto-approves."""
        with self._allowlist_lock:
            data = _load_allowlist()
            clients = data.get("clients", {})
            if game_name in clients:
                return
            clients[game_name] = {
                "approved": False,
                "client_version_at_first_seen": client_version,
                "capability_ceiling": ["read"],
                "auto_approve_writes": False,
                "note": "Auto-added on first connect. Set approved=true to allow.",
            }
            data["clients"] = clients
            _save_allowlist(data)

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

            policy = self._lookup_client_policy(game_name)
            self._record_seen_client(
                game_name, str(hello.get("client_version") or "")
            )

            client_id = str(uuid.uuid4())
            client = SDKClient(
                client_id=client_id,
                client_name=str(hello.get("client_name") or game_name),
                client_version=str(hello.get("client_version") or ""),
                game_name=game_name,
                ws=ws,
                connected_at=time.time(),
                approved=bool(policy["approved"]),
                capability_ceiling=tuple(policy["capability_ceiling"]),
                auto_approve_writes=bool(policy["auto_approve_writes"]),
                allow_reactions=bool(policy["allow_reactions"]),
            )
            self.clients[client_id] = client

            await ws.send_json(startup_message(
                Session(session_id=client_id, display_name="Lumi Voidwright"),
                protocol_version=SDK_PROTOCOL_VERSION,
            ))

            if not client.approved:
                hub_emit(
                    self.signals, "sdk",
                    f"client connected UNVERIFIED: {client.client_name} "
                    f"({client_id[:8]}) game={game_name!r} — action "
                    f"registrations refused. Edit "
                    f"data/memories/sdk_allowlist.json to approve.",
                    level="warn",
                )
            else:
                hub_emit(
                    self.signals, "sdk",
                    f"client connected: {client.client_name} "
                    f"({client_id[:8]}) game={game_name!r} "
                    f"ceiling={list(client.capability_ceiling)}"
                )

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
        # Phase 9.2 — deny-by-default. Registrations from unapproved
        # clients are refused, but the client stays connected so it can
        # still receive speech_finished broadcasts.
        if not client.approved:
            await client.ws.send_json(error_message(
                f"client '{client.game_name}' is not approved — "
                f"action registration refused. Add an entry to "
                f"data/memories/sdk_allowlist.json with approved=true "
                f"and restart Lumi."
            ))
            hub_emit(
                self.signals, "sdk",
                f"refused registration from unapproved client "
                f"{client.game_name!r}",
                level="warn",
            )
            return

        payload: List[Dict[str, Any]] = []
        if isinstance(data.get("data"), dict) and "actions" in data["data"]:
            payload = list(data["data"]["actions"])
        elif isinstance(data.get("action"), dict):
            payload = [data["action"]]

        for raw in payload:
            spec = ActionSpec.from_wire(raw)
            if not spec.name:
                continue

            # Phase 9.3 — enforce capability ceiling at registration.
            requested = set(spec.capabilities or ["read"])
            ceiling = set(client.capability_ceiling) | {"read"}
            if not requested.issubset(ceiling):
                excess = sorted(requested - ceiling)
                await client.ws.send_json(error_message(
                    f"action '{spec.name}' declares capabilities "
                    f"{sorted(requested)} but client ceiling is "
                    f"{sorted(ceiling)} — refused ({excess} exceeds "
                    f"ceiling)"
                ))
                hub_emit(
                    self.signals, "sdk",
                    f"refused registration of '{spec.name}' from "
                    f"{client.game_name!r}: capabilities {excess} "
                    f"exceed ceiling",
                    level="warn",
                )
                continue

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
                     f"(client={client.client_name}, "
                     f"caps={spec.capabilities})")

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
        # silent=false is a one-shot event for Lumi to react to. It
        # starts a turn and leaves the client's standing context alone.
        if payload.get("silent", True) is False:
            self._trigger_reaction(client, text)
            return
        client.context_text = text
        priority = payload.get("priority")
        if isinstance(priority, int):
            client.context_priority = priority

    def _trigger_reaction(self, client: SDKClient, text: str):
        text = text.strip()[:MAX_REACTION_CHARS]
        if not text or not client.approved or not client.allow_reactions:
            return
        s = self.signals
        now = time.time()
        if now - client.last_reaction < REACTION_COOLDOWN_S:
            return
        if s.new_message or s.AI_thinking or s.AI_speaking:
            hub_emit(s, "sdk", f"{client.game_name} event dropped (Lumi busy)")
            return
        client.last_reaction = now
        s.history.append({
            "role": "user",
            "content": f"[FROM SDK: {client.game_name}]\n{text}\n[END FROM SDK]",
            "source": "sdk_event",
        })
        s.new_message = True
        hub_emit(s, "sdk", f"{client.game_name} event → turn: {text[:80]}")

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
        items = []
        for c in self.clients.values():
            if c.approved and c.context_text.strip():
                items.append((c.context_priority, c.context_text))
        items.sort(key=lambda x: x[0])
        return [text for _, text in items]

    def find_action(self, tool_name: str) -> Optional[ActionSpec]:
        if not tool_name.startswith("sdk__"):
            return None
        rec = self.actions.get(tool_name[len("sdk__"):])
        return rec.spec if rec else None

    def is_action_allowed(self, action_name: str) -> Tuple[bool, str]:
        """Phase 9.3 — called by the prompter before dispatch. Returns
        (True, '') if the action is allowed, or (False, reason)."""
        rec = self.actions.get(action_name)
        if rec is None:
            return False, f"action '{action_name}' is not registered"
        client = self.clients.get(rec.client_id)
        if client is None or client.ws.closed:
            return False, "client disconnected"
        if not client.approved:
            return False, f"client '{client.game_name}' is not approved"
        requested = set(rec.spec.capabilities or ["read"])
        ceiling = set(client.capability_ceiling) | {"read"}
        if not requested.issubset(ceiling):
            excess = sorted(requested - ceiling)
            return False, (
                f"action capabilities {excess} exceed client ceiling "
                f"{sorted(ceiling)}"
            )
        return True, ""

    def client_autoapproves_writes(self, action_name: str) -> bool:
        """Phase 9.3 — whether 'write' actions from this client skip
        the interactive y/n. Default is False (confirm required)."""
        rec = self.actions.get(action_name)
        if rec is None:
            return False
        client = self.clients.get(rec.client_id)
        if client is None:
            return False
        return bool(client.auto_approve_writes)

    def invoke_action_blocking(
        self,
        action_name: str,
        arguments: Dict[str, Any],
        timeout: int = INVOKE_TIMEOUT_SECONDS,
    ) -> Dict[str, Any]:
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
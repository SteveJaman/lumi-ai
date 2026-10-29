"""
Lumi SDK — client library.

Third-party apps subclass `LumiClient` or instantiate it with
callbacks. The library handles the websocket, auth handshake,
heartbeat, action registration, invocation dispatch, and reconnect.

Requires: `pip install websockets`.

Copy `sdk/client.py`, `sdk/protocol.py`, `sdk/actions.py`, and
`sdk/errors.py` to any project that wants to talk to Lumi. Nothing
here imports from Lumi's internals — the SDK is standalone.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Awaitable, Callable, Dict, List, Optional

try:
    import websockets
except ImportError:
    websockets = None

from sdk.protocol import (
    ActionSpec,
    ClientCommand,
    ServerCommand,
    hello_message,
    context_message,
    context_clear_message,
    register_actions_message,
    unregister_actions_message,
    action_result_message,
    force_action_message,
)


ActionHandler = Callable[[Dict[str, Any]], Awaitable[Any]]


class LumiClient:
    """Base client. Subclass and override `setup()` to declare
    actions, or pass handlers directly to `register_action()`."""

    def __init__(
        self,
        *,
        game: str,
        host: str = "127.0.0.1",
        port: int = 5056,
        token: Optional[str] = None,
        token_path: Optional[str] = None,
        reconnect_delay: float = 5.0,
        on_lumi_message: Optional[Callable[[str, str], None]] = None,
        on_connected: Optional[Callable[[], None]] = None,
    ):
        if websockets is None:
            raise RuntimeError(
                "websockets not installed — pip install websockets"
            )

        self.game = game
        self.host = host
        self.port = port
        self.reconnect_delay = reconnect_delay
        self.on_lumi_message = on_lumi_message
        self.on_connected = on_connected

        if token is None:
            if token_path is None:
                raise ValueError("must pass token or token_path")
            with open(token_path, "r", encoding="utf-8") as f:
                token = f.read().strip()
        self.token = token

        self._ws = None
        self._actions: Dict[str, ActionSpec] = {}
        self._handlers: Dict[str, ActionHandler] = {}
        self._stop = False

    # ─── Public API ───────────────────────────────────────────

    def register_action(self, spec: ActionSpec, handler: ActionHandler) -> None:
        """Register an action + its async handler. The wire message is
        sent immediately if we're connected, otherwise on next connect."""
        self._actions[spec.name] = spec
        self._handlers[spec.name] = handler

    def unregister_action(self, name: str) -> None:
        self._actions.pop(name, None)
        self._handlers.pop(name, None)

    async def send_context(self, text: str, silent: bool = True,
                           event_kind: Optional[str] = None) -> None:
        """event_kind ("death", "collect:diamond", ...) also moves Lumi's
        emotional state."""
        await self._send(context_message(self.game, text, silent=silent, event_kind=event_kind))

    async def feel(self, event_kind: str) -> None:
        """Just the emotional nudge of a game moment: no context, no turn."""
        await self._send(context_message(self.game, "", event_kind=event_kind, event_only=True))

    async def remember(self, text: str) -> None:
        """Store a milestone ("Built a house at 120, 64, -300") as one of
        Lumi's long-term memories."""
        await self._send(context_message(self.game, text, remember=True))

    async def clear_context(self) -> None:
        await self._send(context_clear_message(self.game))

    async def force_action(
        self,
        query: str,
        action_names: List[str],
        state: Optional[str] = None,
        priority: str = "low",
    ) -> None:
        await self._send(force_action_message(
            self.game, query, action_names,
            state=state, priority=priority,
        ))

    # ─── Extension points ─────────────────────────────────────

    async def setup(self) -> None:
        """Override to declare actions. Called after the handshake."""

    async def on_disconnect(self) -> None:
        """Override to react to socket drops."""

    # ─── Run loop ─────────────────────────────────────────────

    async def run(self) -> None:
        while not self._stop:
            try:
                await self._connect_and_loop()
            except Exception as e:
                print(f"[sdk] connection error: {e}")
            if self._stop:
                break
            print(f"[sdk] reconnecting in {self.reconnect_delay}s")
            await asyncio.sleep(self.reconnect_delay)

    def stop(self) -> None:
        self._stop = True

    # ─── Internals ────────────────────────────────────────────

    async def _connect_and_loop(self) -> None:
        uri = f"ws://{self.host}:{self.port}/ws"
        print(f"[sdk] connecting to {uri}")
        async with websockets.connect(uri) as ws:
            self._ws = ws
            await ws.send(json.dumps(hello_message(
                game=self.game,
                token=self.token,
                client_name=self.game,
                client_version="0.1.0",
            )))

            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                await self._handle(msg)
                if self._stop:
                    break

        self._ws = None
        await self.on_disconnect()

    async def _handle(self, msg: Dict[str, Any]) -> None:
        cmd = msg.get("command")

        if cmd == ServerCommand.STARTUP:
            session = (msg.get("data") or {}).get("session") or {}
            proto = (msg.get("data") or {}).get("protocolVersion", "?")
            print(f"[sdk] welcome — session={session.get('sessionId')} "
                  f"protocol={proto}")
            await self.setup()
            if self._actions:
                await self._send(register_actions_message(
                    self.game, list(self._actions.values()),
                ))
            if self.on_connected:
                self.on_connected()

        elif cmd == ServerCommand.ACTIONS_REGISTERED:
            name = (msg.get("data") or {}).get("action_name")
            print(f"[sdk] registered: {name}")

        elif cmd == ServerCommand.ACTION:
            await self._dispatch_action(msg)

        elif cmd == ServerCommand.SPEECH_FINISHED:
            data = msg.get("data") or {}
            if data.get("isFinal") and self.on_lumi_message:
                self.on_lumi_message(
                    data.get("message") or "",
                    data.get("role") or "assistant",
                )

        elif cmd == ServerCommand.PING:
            await self._send({"command": ClientCommand.PONG})

        elif cmd == ServerCommand.ERROR:
            message = (msg.get("data") or {}).get("message")
            print(f"[sdk] server error: {message}")

    async def _dispatch_action(self, msg: Dict[str, Any]) -> None:
        data = msg.get("data") or {}
        action_id = data.get("id")
        name = data.get("name")

        raw_args = data.get("data") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
        except json.JSONDecodeError:
            await self._send(action_result_message(
                self.game, action_id, False, "invalid JSON in arguments",
            ))
            return

        handler = self._handlers.get(name)
        if handler is None:
            await self._send(action_result_message(
                self.game, action_id, False, f"no handler for {name}",
            ))
            return

        try:
            result = await handler(args)
            await self._send(self._format_result(action_id, result))
        except Exception as e:
            await self._send(action_result_message(
                self.game, action_id, False, f"handler raised: {e}",
            ))

    def _format_result(self, action_id: str, result: Any) -> Dict[str, Any]:
        """Handlers may return:
            (True, "message")     → success with a message
            (False, "error")      → failure with an error
            True / False          → success or failure, no message
            "just a string"       → success with a message
            {"success":, "message": } → explicit
        """
        if isinstance(result, tuple) and len(result) == 2:
            ok, msg = result
            return action_result_message(
                self.game, action_id, bool(ok), str(msg or ""),
            )
        if isinstance(result, dict) and "success" in result:
            return action_result_message(
                self.game, action_id,
                bool(result["success"]),
                str(result.get("message") or result.get("error") or ""),
            )
        if isinstance(result, bool):
            return action_result_message(
                self.game, action_id, result, "",
            )
        return action_result_message(
            self.game, action_id, True, str(result or ""),
        )

    async def _send(self, payload: Dict[str, Any]) -> None:
        if self._ws is None:
            return
        try:
            await self._ws.send(json.dumps(payload))
        except Exception as e:
            print(f"[sdk] send failed: {e}")
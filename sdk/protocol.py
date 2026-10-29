"""
Lumi SDK — wire protocol.

Modeled after Vedal's Neuro SDK (https://github.com/VedalAI/neuro-sdk)
so third-party developers find the shapes familiar. All messages are
plaintext JSON. Binary frames are rejected.

    Client → Server:  {"command": str, "game": str, "data": {...}?}
    Server → Client:  {"command": str, "data": {...}?}

Commands are namespaced with slashes ("actions/register") and follow
Neuro's vocabulary where a matching command exists.

    Neuro command       Lumi command        Purpose
    ----------------    ------------------  ------------------------
    (implicit)          hello               handshake + auth
    startup             startup             ack with session info
    context             context             push free-text context
    actions/register    actions/register    register actions
    actions/unregister  actions/unregister  drop actions
    actions/force       actions/force       force an action choice
    action              action              invoke an action
    action/result       action/result       action return value
    (n/a)               speech_finished     Lumi finished a reply
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


SDK_PROTOCOL_VERSION = "1.0"


# ─── Client → Server commands ─────────────────────────────────────
class ClientCommand:
    HELLO               = "hello"
    CONTEXT             = "context"
    CONTEXT_CLEAR       = "context/clear"
    ACTIONS_REGISTER    = "actions/register"
    ACTIONS_UNREGISTER  = "actions/unregister"
    ACTIONS_FORCE       = "actions/force"
    ACTION_RESULT       = "action/result"
    PONG                = "pong"


# ─── Server → Client commands ─────────────────────────────────────
class ServerCommand:
    STARTUP          = "startup"
    ACTION           = "action"
    SPEECH_FINISHED  = "speech_finished"
    PING             = "ping"
    ERROR            = "error"
    ACTIONS_REGISTERED = "actions/registered"


# ─── Priority levels for actions/force ────────────────────────────
class Priority:
    LOW       = "low"
    MEDIUM    = "medium"
    HIGH      = "high"
    CRITICAL  = "critical"


_VALID_PRIORITIES = {Priority.LOW, Priority.MEDIUM,
                     Priority.HIGH, Priority.CRITICAL}


# ─── Common data shapes ───────────────────────────────────────────

@dataclass
class ActionSpec:
    """One registerable action.

    `schema` uses JSON-Schema vocabulary so it can be passed straight
    through to Ollama's tool interface without translation.

    `capabilities` and `requires_confirm` are Lumi extensions — not
    part of Neuro's spec. Clients that don't understand them can
    safely ignore them; the server uses them for the confirmation
    gate.
    """
    name: str
    description: str
    schema: Dict[str, Any] = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )
    capabilities: List[str] = field(default_factory=lambda: ["read"])
    requires_confirm: bool = False

    def to_wire(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "schema": self.schema,
            "capabilities": list(self.capabilities),
            "requires_confirm": bool(self.requires_confirm),
        }

    @classmethod
    def from_wire(cls, raw: Dict[str, Any]) -> "ActionSpec":
        return cls(
            name=str(raw.get("name") or "").strip(),
            description=str(raw.get("description") or ""),
            schema=raw.get("schema") or {"type": "object", "properties": {}},
            capabilities=list(raw.get("capabilities") or ["read"]),
            requires_confirm=bool(raw.get("requires_confirm", False)),
        )


@dataclass
class Session:
    session_id: str
    character_id: str = "lumi"
    display_name: str = "Lumi Voidwright"

    def to_wire(self) -> Dict[str, Any]:
        return {
            "sessionId": self.session_id,
            "characterId": self.character_id,
            "displayName": self.display_name,
        }


# ─── Message builders (client side) ───────────────────────────────

def hello_message(
    game: str,
    token: str,
    client_name: Optional[str] = None,
    client_version: str = "0.1.0",
) -> Dict[str, Any]:
    return {
        "command": ClientCommand.HELLO,
        "game": game,
        "client_name": client_name or game,
        "client_version": client_version,
        "auth_token": token,
    }


def context_message(
    game: str,
    message: str,
    silent: bool = True,
    event_kind: Optional[str] = None,
    remember: bool = False,
    event_only: bool = False,
) -> Dict[str, Any]:
    """event_kind moves Lumi's emotional state, remember stores message as
    a long-term memory, event_only sends just those (no context, no turn)."""
    data: Dict[str, Any] = {"message": message, "silent": silent}
    if event_kind:
        data["event_kind"] = event_kind
    if remember:
        data["remember"] = True
    if event_only:
        data["event_only"] = True
    return {"command": ClientCommand.CONTEXT, "game": game, "data": data}


def context_clear_message(game: str) -> Dict[str, Any]:
    return {"command": ClientCommand.CONTEXT_CLEAR, "game": game}


def register_actions_message(
    game: str,
    actions: List[ActionSpec],
) -> Dict[str, Any]:
    return {
        "command": ClientCommand.ACTIONS_REGISTER,
        "game": game,
        "data": {"actions": [a.to_wire() for a in actions]},
    }


def unregister_actions_message(
    game: str,
    names: List[str],
) -> Dict[str, Any]:
    return {
        "command": ClientCommand.ACTIONS_UNREGISTER,
        "game": game,
        "data": {"action_names": list(names)},
    }


def force_action_message(
    game: str,
    query: str,
    action_names: List[str],
    state: Optional[str] = None,
    priority: str = Priority.LOW,
    ephemeral_context: bool = False,
) -> Dict[str, Any]:
    if priority not in _VALID_PRIORITIES:
        priority = Priority.LOW
    data: Dict[str, Any] = {
        "query": query,
        "action_names": list(action_names),
        "priority": priority,
        "ephemeral_context": ephemeral_context,
    }
    if state is not None:
        data["state"] = state
    return {"command": ClientCommand.ACTIONS_FORCE, "game": game, "data": data}


def action_result_message(
    game: str,
    action_id: str,
    success: bool,
    message: Optional[str] = None,
) -> Dict[str, Any]:
    data: Dict[str, Any] = {"id": action_id, "success": success}
    if message is not None:
        data["message"] = message
    return {"command": ClientCommand.ACTION_RESULT, "game": game, "data": data}


# ─── Message builders (server side) ───────────────────────────────

def startup_message(session: Session, protocol_version: str = SDK_PROTOCOL_VERSION) -> Dict[str, Any]:
    return {
        "command": ServerCommand.STARTUP,
        "data": {
            "session": session.to_wire(),
            "protocolVersion": protocol_version,
        },
    }


def action_message(
    action_id: str,
    action_name: str,
    arguments: Dict[str, Any],
) -> Dict[str, Any]:
    """Note: `data` is a JSON-encoded string, not an object. This
    matches Neuro's convention so clients can forward the payload
    without parsing it if they don't need to."""
    import json as _json
    return {
        "command": ServerCommand.ACTION,
        "data": {
            "id": action_id,
            "name": action_name,
            "data": _json.dumps(arguments or {}),
        },
    }


def speech_finished_message(
    content: str,
    role: str = "assistant",
    is_final: bool = True,
    cancelled: bool = False,
) -> Dict[str, Any]:
    return {
        "command": ServerCommand.SPEECH_FINISHED,
        "data": {
            "isFinal": bool(is_final),
            "cancelled": bool(cancelled),
            "message": str(content),
            "role": role,
        },
    }


def error_message(message: str) -> Dict[str, Any]:
    return {"command": ServerCommand.ERROR, "data": {"message": str(message)}}
"""
Lumi Island bridge: the island's mini-apps (Home, Chat, Calendar) over the
same authenticated socket as tool approvals (modules/hitl_server.py).

Client -> server (after the hello handshake), each with an optional "id"
that is echoed back:
    get_home                       -> home {snapshot, state, gaming}
    get_calendar {start, end}      -> calendar {events, at} | {error, code}
    get_chat {limit}               -> chat {messages: [{n, role, content}]}
    chat_send {text}               -> chat_ack {ok} | error

Server -> client (pushed, never polled):
    lumi_status {state}            idle / thinking / speaking
    lumi_mode {gaming}             Gaming Mode toggled
    chat_message {message}         a message was added to the chat
    chat_reset                     the chat was wiped or rewritten
    snapshot {snapshot}            next event + unread changed

Everything the island can do is something the signed-in owner can already do
in the web client; the socket is loopback-only, origin-checked, and (when
Lumi launches the island) needs the per-launch secret.
"""

import asyncio
import time

from modules import hitl_server
from modules.workspace_hub import NotConnected, ProviderError

MAX_CHAT_CHARS = 4000
MAX_MESSAGE_CHARS = 2000        # per message sent to the island
DEFAULT_CHAT_LIMIT = 40
MIN_SEND_GAP_S = 0.4

_HIDDEN_SOURCES = ("sdk_event", "command")


def visible(msg) -> bool:
    """The messages a person would see in the chat (the web client's rule)."""
    return (isinstance(msg, dict)
            and msg.get("role") in ("user", "assistant")
            and not msg.get("hidden")
            and msg.get("source") not in _HIDDEN_SOURCES
            and isinstance(msg.get("content"), str)
            and bool(msg["content"].strip()))


def shape(n: int, msg: dict) -> dict:
    return {"n": n, "role": msg["role"], "content": msg["content"][:MAX_MESSAGE_CHARS]}


def install(signals, server=None) -> None:
    """Register the handlers and the push hooks. Safe to call once, at boot,
    after the approval server exists. `server` is for tests; production uses
    the module singleton."""
    reg = server.register_handler if server else hitl_server.register_handler
    send = server.send if server else hitl_server.send
    push = server.broadcast_threadsafe if server else hitl_server.broadcast_threadsafe
    state = {"last_send": 0.0}

    def hub():
        return getattr(signals, "workspace_hub", None)

    async def home(ws, data):
        h = hub()
        await send(ws, {
            "type": "home", "id": data.get("id"),
            "snapshot": h.get_compressed_snapshot() if h is not None else None,
            "state": hitl_server.HITLServer._status_of(signals),
        })

    async def calendar(ws, data):
        base = {"type": "calendar", "id": data.get("id")}
        h = hub()
        if h is None:
            return await send(ws, {**base, "events": [], "error": "Workspace isn't available.", "code": "unavailable"})
        loop = asyncio.get_running_loop()
        try:
            res = await loop.run_in_executor(None, h.get_events_range, data.get("start"), data.get("end"),
                                             bool(data.get("refresh")))
        except NotConnected as e:
            return await send(ws, {**base, "events": [], "error": str(e), "code": "not_connected"})
        except ValueError as e:                       # InvalidInput is one
            return await send(ws, {**base, "events": [], "error": str(e), "code": "bad_request"})
        except ProviderError as e:
            return await send(ws, {**base, "events": [], "error": str(e), "code": "provider"})
        await send(ws, {**base, "events": res["events"], "at": res["at"]})

    async def chat(ws, data):
        try:
            limit = max(1, min(int(data.get("limit") or DEFAULT_CHAT_LIMIT), 200))
        except (TypeError, ValueError):
            limit = DEFAULT_CHAT_LIMIT
        hist = list(signals.history)
        shown = [shape(i, m) for i, m in enumerate(hist) if visible(m)][-limit:]
        await send(ws, {"type": "chat", "id": data.get("id"), "messages": shown})

    async def chat_send(ws, data):
        text = str(data.get("text") or "").strip()
        base = {"type": "chat_ack", "id": data.get("id")}
        if not text:
            return await send(ws, {**base, "ok": False, "error": "Nothing to send."})
        now = time.time()
        if now - state["last_send"] < MIN_SEND_GAP_S:
            return await send(ws, {**base, "ok": False, "error": "Slow down a little."})
        state["last_send"] = now
        # Same as typing in the web client on this PC: a host message.
        signals.history.append({"role": "user", "content": text[:MAX_CHAT_CHARS], "from_host": True})
        signals.new_message = True
        signals.last_message_time = now
        if hasattr(signals, "save_history"):
            signals.save_history()
        await send(ws, {**base, "ok": True})

    reg("get_home", home)
    reg("get_calendar", calendar)
    reg("get_chat", chat)
    reg("chat_send", chat_send)

    def on_history(kind, start, msgs):
        if kind == "reset":
            push({"type": "chat_reset"})
            return
        for offset, m in enumerate(msgs):
            if visible(m):
                push({"type": "chat_message", "message": shape(start + offset, m)})

    signals.on_history_change = on_history

    h = hub()
    if h is not None:
        h.add_listener(lambda snap: push({"type": "snapshot", "snapshot": snap}))

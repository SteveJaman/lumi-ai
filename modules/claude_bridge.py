"""
Claude bridge: lets Claude Desktop (or Claude Code) talk to Lumi on your Claude plan.

scripts/lumi_mcp.py is a small MCP server that Claude Desktop starts. Claude does
the thinking on your own subscription, and when it wants Lumi it calls the
endpoints below on this PC's web server. Nothing here calls Anthropic: no API key,
no copied login, no driving the Claude app's window.

    POST /api/bridge/chat       {"text": "..."} -> {"reply": "..."}   say something to Lumi, wait for her answer
    GET  /api/bridge/status     what she's doing right now
    GET  /api/bridge/history    the last few chat messages (?n=20)
    GET  /api/bridge/memories   search her memories (?q=...)

Who may call: only this PC (the loopback listener, never the tunnel port) and
only with the bridge token, a random secret written to
data/credentials/claude_bridge.token (gitignored) the first time Lumi starts.
The MCP script reads that file; a web page can't, so a browser tab on this PC
can't use the bridge either (requests that carry an Origin header are refused).

Messages from the bridge go into the normal chat as yours, marked "via": "claude"
on the history entry. Chat commands ("!agent_on", ...) are refused, and the
message never counts as a host order for the Minecraft agent.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import time
from typing import Any, Dict, List, Optional

from constants import DATA_ROOT

TOKEN_PATH = os.path.join(DATA_ROOT, "credentials", "claude_bridge.token")
MAX_TEXT_CHARS = 4000
REPLY_TIMEOUT_S = 180.0
SETTLE_S = 1.5                     # a reply that stops changing for this long is finished
HISTORY_MAX = 50

_chat_lock: Optional[asyncio.Lock] = None


def ensure_token(path: str = TOKEN_PATH) -> str:
    """The bridge token, created on first use (owner-only where the OS allows)."""
    try:
        with open(path, encoding="utf-8") as f:
            tok = f.read().strip()
        if len(tok) >= 32:
            return tok
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tok = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(tok)
    return tok


def authorized(headers: Dict[str, str], is_local: bool, token: str) -> bool:
    """Loopback only, no browser Origin, and the right bearer token."""
    if not is_local or headers.get("Origin"):
        return False
    auth = headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    return secrets.compare_digest(auth[len("Bearer "):].strip(), token)


def _text_of(entry: Dict[str, Any]) -> str:
    c = entry.get("content")
    return c if isinstance(c, str) else str(c or "")


def find_reply(history: List[Dict[str, Any]], start: int) -> Optional[int]:
    """Index of the first assistant message at or after `start`."""
    for i in range(max(0, start), len(history)):
        if history[i].get("role") == "assistant":
            return i
    return None


async def wait_for_reply(signals, start: int, timeout: float = REPLY_TIMEOUT_S,
                         settle: float = SETTLE_S, poll: float = 0.25) -> Optional[str]:
    """Her answer to the message at history[start - 1]: the first assistant
    message after it, once she has stopped thinking and it stopped changing."""
    deadline = time.monotonic() + timeout
    last, since = None, time.monotonic()
    while time.monotonic() < deadline:
        hist = getattr(signals, "history", [])
        i = find_reply(hist, start)
        if i is not None:
            text = _text_of(hist[i])
            if text != last:
                last, since = text, time.monotonic()
            busy = bool(getattr(signals, "AI_thinking", False))
            if not busy and time.monotonic() - since >= settle:
                return last
        await asyncio.sleep(poll)
    return last


def install(server) -> None:
    """Add the bridge routes to the web server (socketIO.SocketIOServer)."""
    from aiohttp import web

    token = ensure_token()

    def guard(request) -> Optional[web.Response]:
        if not authorized(request.headers, server._is_local(request), token):
            return web.json_response({"error": "Not allowed."}, status=403)
        return None

    async def status(request):
        bad = guard(request)
        if bad:
            return bad
        s = server.signals
        return web.json_response({
            "thinking": bool(getattr(s, "AI_thinking", False)),
            "speaking": bool(getattr(s, "AI_speaking", False)),
            "agent_mode": bool(getattr(server.prompter, "agent_mode_enabled", False)) if server.prompter else False,
            "messages": len(getattr(s, "history", [])),
        })

    async def history(request):
        bad = guard(request)
        if bad:
            return bad
        try:
            n = max(1, min(int(request.query.get("n", 20)), HISTORY_MAX))
        except ValueError:
            n = 20
        rows = [{"role": h.get("role"), "content": _text_of(h)[:2000]}
                for h in getattr(server.signals, "history", [])[-n:]
                if h.get("role") in ("user", "assistant")]
        return web.json_response({"messages": rows})

    async def memories(request):
        bad = guard(request)
        if bad:
            return bad
        q = (request.query.get("q") or "").strip()[:300]
        if not q:
            return web.json_response({"error": "Give a search, ?q=..."}, status=400)
        api = server._memory_api()
        if api is None:
            return web.json_response({"error": "Lumi's memory isn't loaded."}, status=503)
        hits = await server._run_mem(api.query_temporal, q, limit=10)
        return web.json_response({"memories": [
            {"fact": h.get("fact", ""), "when": h.get("timestamp", "")} for h in hits]})

    async def chat(request):
        global _chat_lock
        bad = guard(request)
        if bad:
            return bad
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON."}, status=400)
        text = str((body or {}).get("text") or "").strip()
        if not text:
            return web.json_response({"error": "Empty message."}, status=400)
        if text.startswith("!"):
            return web.json_response({"error": "Chat commands only work from Lumi's own app."}, status=403)
        text = text[:MAX_TEXT_CHARS]
        if _chat_lock is None:
            _chat_lock = asyncio.Lock()
        async with _chat_lock:                      # one bridge message at a time
            s = server.signals
            s.history.append({"role": "user", "content": text, "via": "claude"})
            start = len(s.history)
            s.new_message = True
            s.last_message_time = time.time()
            if hasattr(s, "save_history"):
                s.save_history()
            try:
                await server.sio.emit("web_message", {"role": "user", "content": text, "ts": time.time()})
            except Exception:
                pass
            reply = await wait_for_reply(s, start)
        if reply is None:
            return web.json_response({"error": "Lumi didn't answer in time.", "reply": ""}, status=504)
        return web.json_response({"reply": reply})

    r = server.app.router
    r.add_get("/api/bridge/status", status)
    r.add_get("/api/bridge/history", history)
    r.add_get("/api/bridge/memories", memories)
    r.add_post("/api/bridge/chat", chat)

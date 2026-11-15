"""
Remote Lumi Island: let an island on another device (your Surface, over
Tailscale) connect to this PC.

Off by default. When you turn it on (web client: Settings → General → Remote
island):
  * The approval socket ALSO listens on this PC's Tailscale address, and only
    that address: never 0.0.0.0, so it isn't reachable on Wi-Fi or a LAN.
  * A pairing code is generated and shown once. Only a salted SHA-256 hash of
    it is stored (data/island_remote.json, git-ignored). A remote island must
    open with {"type": "hello", "pair": "<code>"}; anything else is closed.
  * Five wrong codes from one address in ten minutes locks that address out
    for ten minutes (the code is 80 random bits, so guessing is hopeless
    anyway; this keeps logs quiet).
  * Turning it off, or making a new code, disconnects every remote island at
    once.

A paired island can do what the local one does: approve tools, chat as you,
see your calendar and mail summary. That's why it's off until you choose it,
and why only someone holding the code (typed into that device) gets in.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import socket
import subprocess
import threading
import time
import uuid
from typing import Any, Dict, Optional

from constants import DATA_ROOT

STATE_PATH = os.path.join(DATA_ROOT, "island_remote.json")
_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"        # no 0/O, 1/I/L, U
CODE_GROUPS, GROUP_LEN = 4, 4                        # 16 symbols, ~78 bits
MAX_FAILURES, FAIL_WINDOW_S, LOCKOUT_S = 5, 600, 600
TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")

_lock = threading.RLock()
_failures: Dict[str, list] = {}


class RemoteError(Exception):
    """Something worth showing to the user as-is."""


# ── state ────────────────────────────────────────────────────────────

def _load(path: str = None) -> Dict[str, Any]:
    try:
        with open(path or STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: Dict[str, Any], path: str = None) -> None:
    path = path or STATE_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{uuid.uuid4().hex[:6]}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def is_enabled() -> bool:
    s = _load()
    return bool(s.get("enabled") and s.get("hash") and s.get("host"))


def bind_host() -> Optional[str]:
    s = _load()
    return s.get("host") if is_enabled() else None


# ── Tailscale address ───────────────────────────────────────────────

def _is_tailscale(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip) in TAILSCALE_NET
    except ValueError:
        return False


def tailscale_ip() -> Optional[str]:
    """This PC's Tailscale IPv4 (100.64.0.0/10), or None."""
    try:
        kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=4, **kw).stdout
        for line in out.split():
            if _is_tailscale(line.strip()):
                return line.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if _is_tailscale(ip):
                return ip
    except OSError:
        pass
    return None


# ── pairing code ─────────────────────────────────────────────────────

def _normalize(code: str) -> str:
    return "".join(ch for ch in str(code or "").upper() if ch in _ALPHABET)


def _hash(salt: str, code: str) -> str:
    return hashlib.sha256((salt + ":" + _normalize(code)).encode()).hexdigest()


def new_code() -> str:
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(CODE_GROUPS * GROUP_LEN))
    return "-".join(raw[i:i + GROUP_LEN] for i in range(0, len(raw), GROUP_LEN))


def enable(host: Optional[str] = None, path: str = None) -> Dict[str, Any]:
    """Turn remote islands on with a fresh code. Returns the code ONCE."""
    host = host or tailscale_ip()
    if not host:
        raise RemoteError("Tailscale doesn't seem to be running on this PC. Start Tailscale, sign in, "
                          "and try again.")
    if not _is_tailscale(host):
        raise RemoteError("That isn't a Tailscale address. For safety the island only listens on "
                          "Tailscale (100.x.x.x), never on your Wi-Fi or LAN.")
    code = new_code()
    salt = secrets.token_hex(16)
    with _lock:
        _save({"enabled": True, "host": host, "salt": salt, "hash": _hash(salt, code),
               "created": time.time()}, path)
        _failures.clear()
    return {"code": code, "host": host}


def disable(path: str = None) -> None:
    with _lock:
        _save({"enabled": False}, path)


def verify(code: Any, peer: str, path: str = None) -> bool:
    """Is this the current pairing code? Counts failures per address."""
    if locked(peer):
        return False
    s = _load(path)
    ok = bool(s.get("enabled") and s.get("hash") and s.get("salt")
              and _normalize(code) and hmac.compare_digest(_hash(s["salt"], str(code)), s["hash"]))
    if not ok:
        with _lock:
            now = time.time()
            hits = [t for t in _failures.get(peer, []) if now - t < FAIL_WINDOW_S]
            hits.append(now)
            _failures[peer] = hits
    return ok


def locked(peer: str) -> bool:
    with _lock:
        now = time.time()
        hits = [t for t in _failures.get(peer, []) if now - t < max(FAIL_WINDOW_S, LOCKOUT_S)]
        _failures[peer] = hits
        recent = [t for t in hits if now - t < FAIL_WINDOW_S]
        return len(recent) >= MAX_FAILURES


def status(path: str = None) -> Dict[str, Any]:
    """For the web client. Never includes the code or its hash."""
    s = _load(path)
    on = bool(s.get("enabled") and s.get("hash") and s.get("host"))
    from modules.hitl_server import HITL_PORT
    return {"enabled": on, "host": s.get("host") if on else None, "port": HITL_PORT,
            "paired_since": s.get("created") if on else None, "tailscale_ip": tailscale_ip()}


# ── web routes ───────────────────────────────────────────────────────

def register(router, server) -> None:
    """GET/POST /api/island/remote (host-only: not in _REMOTE_API_ALLOW)."""
    import asyncio
    from aiohttp import web
    from modules import hitl_server
    from modules.socketIO import _json_error

    async def apply_to_socket():
        """Make the running approval server match the saved setting."""
        srv = hitl_server._server
        loop = srv._loop
        if loop is None:
            return None                                  # applies when Lumi's socket starts
        fut = asyncio.run_coroutine_threadsafe(srv.apply_remote(), loop)
        return await asyncio.wrap_future(fut)

    async def get_status(request):
        if not server._current_user(request):
            return _json_error(401, "Not logged in.")
        loop = asyncio.get_running_loop()
        return web.json_response(await loop.run_in_executor(None, status))

    async def post_action(request):
        if not server._origin_ok(request):
            return _json_error(403, "Bad origin.")
        if not server._current_user(request):
            return _json_error(401, "Not logged in.")
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        ok, err = server._require_csrf(request, body)
        if not ok:
            return _json_error(403, err)
        action = body.get("action")
        loop = asyncio.get_running_loop()
        out: Dict[str, Any] = {}
        try:
            if action in ("enable", "regenerate"):
                out.update(await loop.run_in_executor(None, enable))
            elif action == "disable":
                await loop.run_in_executor(None, disable)
            else:
                return _json_error(400, "action must be enable, regenerate or disable.")
        except RemoteError as e:
            return _json_error(409, str(e))
        bind_err = await apply_to_socket()
        out["status"] = await loop.run_in_executor(None, status)
        if bind_err:
            out["warning"] = bind_err
        return web.json_response(out)

    router.add_get("/api/island/remote", get_status)
    router.add_post("/api/island/remote", post_action)

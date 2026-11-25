"""
Shortcuts for the Lumi Island Hub: the user's favourite sites, kept in
data/shortcuts.json and opened in the default browser by the island (Rust:
open_external_url). Nothing here fetches a URL.

    list_shortcuts()          -> [{id, title, url}]    seeds the defaults on first use
    add_shortcut(title, url)  -> the new shortcut       ShortcutError if invalid or full
    remove_shortcut(id)       -> True if it existed
    add_listener(fn)          fn(list) after every change (the island bridge pushes it)

HTTP (logged in, same origin; changes are host-only):
    GET    /api/shortcuts           {shortcuts: [...]}
    POST   /api/shortcuts           {title, url} -> {shortcut}
    DELETE /api/shortcuts/{id}      {ok: true}

Only http(s) URLs are accepted, so a shortcut can never launch a file or a
program. The island's Rust command checks the same thing again before opening.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from constants import DATA_ROOT

PATH = os.path.join(DATA_ROOT, "shortcuts.json")
MAX_SHORTCUTS = 48
MAX_TITLE_CHARS = 40
MAX_URL_CHARS = 2048

DEFAULTS = (
    ("Amazon", "https://amazon.com"),
    ("Canvas", "https://canvas.instructure.com"),
    ("Google", "https://google.com"),
    ("YouTube", "https://youtube.com"),
)

_lock = threading.Lock()
_listeners: List[Callable[[List[Dict[str, Any]]], None]] = []
_CONTROL_RE = re.compile(r"[\x00-\x20\x7f]")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:(?!\d)")


class ShortcutError(ValueError):
    """A shortcut that can't be saved; the message is safe to show."""


def clean_url(url: Any) -> str:
    """The URL if it's a plain http(s) link to a host, else ShortcutError.
    A bare "example.com" gets https:// in front."""
    u = str(url or "").strip()
    if not u:
        raise ShortcutError("Enter a link.")
    if "://" not in u:
        # "javascript:…", "ms-settings:…" are schemes, not hosts; "localhost:8080" is a host and port.
        if _SCHEME_RE.match(u):
            raise ShortcutError("Only http and https links can be shortcuts.")
        u = "https://" + u
    if len(u) > MAX_URL_CHARS:
        raise ShortcutError("That link is too long.")
    if _CONTROL_RE.search(u):
        raise ShortcutError("A link can't contain spaces.")
    p = urlparse(u)
    if p.scheme.lower() not in ("http", "https"):
        raise ShortcutError("Only http and https links can be shortcuts.")
    host = p.hostname or ""
    if not host or p.username or p.password:
        raise ShortcutError("That isn't a web address.")
    return u


def clean_title(title: Any, url: str) -> str:
    t = " ".join(str(title or "").split())[:MAX_TITLE_CHARS]
    if t:
        return t
    host = (urlparse(url).hostname or "").removeprefix("www.")
    return (host.split(".")[0] or host or "Link").capitalize()[:MAX_TITLE_CHARS]


def _new_id() -> str:
    return uuid.uuid4().hex[:10]


def _seed() -> List[Dict[str, Any]]:
    return [{"id": _new_id(), "title": t, "url": u} for t, u in DEFAULTS]


def _valid(item: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(item, dict):
        return None
    try:
        url = clean_url(item.get("url"))
    except ShortcutError:
        return None
    sid = str(item.get("id") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", sid):
        sid = _new_id()
    return {"id": sid, "title": clean_title(item.get("title"), url), "url": url}


def _read(path: str) -> Optional[List[Dict[str, Any]]]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return []                                  # unreadable: start empty, don't resurrect defaults
    items = data.get("shortcuts") if isinstance(data, dict) else data
    out = [v for v in (_valid(i) for i in (items or [])) if v]
    return out[:MAX_SHORTCUTS]


def _write(path: str, items: List[Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"shortcuts": items, "updated": time.time()}, f, indent=2)
    os.replace(tmp, path)


def list_shortcuts(path: Optional[str] = None) -> List[Dict[str, Any]]:
    path = path or PATH
    with _lock:
        items = _read(path)
        if items is None:                          # first run
            items = _seed()
            try:
                _write(path, items)
            except OSError:
                pass
        return [dict(i) for i in items]


def add_shortcut(title: Any, url: Any, path: Optional[str] = None) -> Dict[str, Any]:
    path = path or PATH
    u = clean_url(url)
    item = {"id": _new_id(), "title": clean_title(title, u), "url": u}
    with _lock:
        items = _read(path)
        items = _seed() if items is None else items
        if len(items) >= MAX_SHORTCUTS:
            raise ShortcutError(f"You already have {MAX_SHORTCUTS} shortcuts. Remove one first.")
        items.append(item)
        _write(path, items)
        snapshot = [dict(i) for i in items]
    _notify(snapshot)
    return dict(item)


def remove_shortcut(sid: Any, path: Optional[str] = None) -> bool:
    path = path or PATH
    sid = str(sid or "")
    with _lock:
        items = _read(path)
        items = _seed() if items is None else items
        kept = [i for i in items if i["id"] != sid]
        if len(kept) == len(items):
            return False
        _write(path, kept)
        snapshot = [dict(i) for i in kept]
    _notify(snapshot)
    return True


def add_listener(fn: Callable[[List[Dict[str, Any]]], None]) -> None:
    _listeners.append(fn)


def _notify(items: List[Dict[str, Any]]) -> None:
    for fn in list(_listeners):
        try:
            fn(items)
        except Exception:
            pass


# ─── HTTP ────────────────────────────────────────────────────────────

def register(router, server) -> None:
    import asyncio
    from aiohttp import web
    from modules.socketIO import _json_error

    def gate(request, change: bool):
        if not server._origin_ok(request):
            return _json_error(403, "Bad origin.")
        if not server._current_user(request):
            return _json_error(401, "Not logged in.")
        if change and not server._is_local(request):
            return _json_error(403, "Shortcuts can only be changed on the host PC.")
        return None

    async def get_all(request):
        err = gate(request, False)
        if err is not None:
            return err
        items = await asyncio.get_running_loop().run_in_executor(None, list_shortcuts)
        return web.json_response({"shortcuts": items}, headers={"Cache-Control": "no-store"})

    async def create(request):
        err = gate(request, True)
        if err is not None:
            return err
        try:
            body = await request.json()
        except Exception:
            return _json_error(400, "Send JSON: {title, url}.")
        if not isinstance(body, dict):
            return _json_error(400, "Send JSON: {title, url}.")
        try:
            item = await asyncio.get_running_loop().run_in_executor(
                None, add_shortcut, body.get("title"), body.get("url"))
        except ShortcutError as e:
            return _json_error(400, str(e))
        except OSError:
            return _json_error(500, "Couldn't save the shortcut.")
        return web.json_response({"shortcut": item}, status=201)

    async def delete(request):
        err = gate(request, True)
        if err is not None:
            return err
        sid = request.match_info.get("sid", "")
        try:
            ok = await asyncio.get_running_loop().run_in_executor(None, remove_shortcut, sid)
        except OSError:
            return _json_error(500, "Couldn't save the change.")
        if not ok:
            return _json_error(404, "No such shortcut.")
        return web.json_response({"ok": True})

    router.add_get("/api/shortcuts", get_all)
    router.add_post("/api/shortcuts", create)
    router.add_delete("/api/shortcuts/{sid}", delete)

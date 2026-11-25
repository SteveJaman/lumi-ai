"""
Spotify: see what's playing and control playback with your own account.

Sign-in is OAuth 2.0 with PKCE, so only a Client ID is needed (no secret). You
create a free app at https://developer.spotify.com/dashboard, add the redirect
URI http://127.0.0.1:5000/api/spotify/callback, and paste its Client ID in
Settings -> Connections. The sign-in stays on this PC in data/credentials/spotify.json.

Reading what's playing works on any account. Play, pause, skip, volume and
shuffle are Spotify Premium features: on a free account Spotify answers 403
and Lumi says so.

    status()                    {configured, connected, user, product, premium}
    save_client(client_id)
    begin(redirect_uri) -> url  then finish(state, code) from the callback
    disconnect()
    now_playing() -> dict|None  the player state, trimmed for the UI
    control(action, value=None) play | pause | next | previous | volume | shuffle
    play_search(query, kind)    search and play the best match

Lumi's tools (everyday-tool interface, modules/everyday_common.py):
    spotify_now_playing, spotify_control, spotify_play
None asks for approval: they only change what's playing on your Spotify.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import requests

from constants import DATA_ROOT
from modules.everyday_common import ToolContext

PATH = os.path.join(DATA_ROOT, "credentials", "spotify.json")
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
SCOPES = ("user-read-playback-state user-modify-playback-state user-read-currently-playing "
          "user-read-recently-played user-top-read playlist-read-private")
TIMEOUT_S = 10
PENDING_TTL_S = 600

_lock = threading.Lock()
_pending: Dict[str, Dict[str, Any]] = {}        # state -> {verifier, redirect, at}


class SpotifyError(Exception):
    """A message safe to show the user (and to hand to the model)."""


class NotConnected(SpotifyError):
    pass


# ─── storage ─────────────────────────────────────────────────────────

def _load() -> Dict[str, Any]:
    try:
        with open(PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, PATH)


def save_client(client_id: str) -> None:
    cid = "".join(str(client_id or "").split())
    if not cid:                                     # "use a different Client ID": forget the app
        with _lock:
            _save({})
        return
    if not (16 <= len(cid) <= 64) or not cid.isalnum():
        raise ValueError("That doesn't look like a Spotify Client ID (32 letters and digits).")
    with _lock:
        data = _load()
        if data.get("client_id") != cid:
            data = {"client_id": cid}               # a new app means signing in again
        _save(data)


def status() -> Dict[str, Any]:
    data = _load()
    user = data.get("user") or {}
    return {"configured": bool(data.get("client_id")), "connected": bool((data.get("token") or {}).get("refresh_token")),
            "user": user.get("display_name") or user.get("id"), "product": user.get("product"),
            "premium": user.get("product") == "premium"}


def connected() -> bool:
    return status()["connected"]


def disconnect() -> None:
    with _lock:
        data = _load()
        _save({"client_id": data["client_id"]} if data.get("client_id") else {})


# ─── sign-in (PKCE) ──────────────────────────────────────────────────

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def begin(redirect_uri: str) -> str:
    data = _load()
    cid = data.get("client_id")
    if not cid:
        raise ValueError("Save your Spotify Client ID first.")
    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    state = secrets.token_urlsafe(24)
    now = time.time()
    with _lock:
        for k in [k for k, v in _pending.items() if now - v["at"] > PENDING_TTL_S]:
            _pending.pop(k, None)
        _pending[state] = {"verifier": verifier, "redirect": redirect_uri, "at": now}
    return AUTH_URL + "?" + urlencode({
        "client_id": cid, "response_type": "code", "redirect_uri": redirect_uri, "scope": SCOPES,
        "code_challenge_method": "S256", "code_challenge": challenge, "state": state})


def finish(state: str, code: str) -> Dict[str, Any]:
    with _lock:
        p = _pending.pop(state, None)
    if p is None or time.time() - p["at"] > PENDING_TTL_S:
        raise SpotifyError("That sign-in link expired. Start again from Settings -> Connections.")
    data = _load()
    r = requests.post(TOKEN_URL, data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": p["redirect"],
        "client_id": data.get("client_id", ""), "code_verifier": p["verifier"]}, timeout=TIMEOUT_S)
    if r.status_code != 200:
        raise SpotifyError(f"Spotify refused the sign-in ({_err_text(r)}).")
    tok = r.json()
    data["token"] = {"access_token": tok["access_token"], "refresh_token": tok.get("refresh_token", ""),
                     "expires_at": time.time() + int(tok.get("expires_in", 3600)) - 60,
                     "scope": tok.get("scope", "")}
    with _lock:
        _save(data)
    me = _api("GET", "/me")
    data = _load()
    data["user"] = {"id": me.get("id"), "display_name": me.get("display_name"), "product": me.get("product")}
    with _lock:
        _save(data)
    return status()


def _access_token() -> str:
    with _lock:
        data = _load()
        tok = data.get("token") or {}
        if not tok.get("refresh_token"):
            raise NotConnected("Spotify isn't connected. Connect it in Settings -> Connections.")
        if tok.get("access_token") and time.time() < float(tok.get("expires_at", 0)):
            return tok["access_token"]
        r = requests.post(TOKEN_URL, data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                           "client_id": data.get("client_id", "")}, timeout=TIMEOUT_S)
        if r.status_code != 200:
            if r.status_code in (400, 401):
                data.pop("token", None)
                _save(data)
                raise NotConnected("Spotify signed you out. Connect it again in Settings -> Connections.")
            raise SpotifyError(f"Couldn't refresh the Spotify sign-in ({_err_text(r)}).")
        new = r.json()
        tok.update(access_token=new["access_token"], expires_at=time.time() + int(new.get("expires_in", 3600)) - 60)
        if new.get("refresh_token"):
            tok["refresh_token"] = new["refresh_token"]
        data["token"] = tok
        _save(data)
        return tok["access_token"]


# ─── Web API ─────────────────────────────────────────────────────────

def _err_text(r) -> str:
    try:
        err = r.json().get("error")
        if isinstance(err, dict):
            return f"{r.status_code}: {err.get('message') or err.get('reason') or ''}".strip()
        return f"{r.status_code}: {r.json().get('error_description') or err}"
    except Exception:
        return str(r.status_code)


def _api(method: str, path: str, params=None, body=None, _retry=True) -> Any:
    r = requests.request(method, API + path, params=params, json=body, timeout=TIMEOUT_S,
                         headers={"Authorization": f"Bearer {_access_token()}"})
    if r.status_code == 401 and _retry:
        with _lock:
            data = _load()
            if data.get("token"):
                data["token"]["expires_at"] = 0
                _save(data)
        return _api(method, path, params, body, _retry=False)
    if r.status_code == 204 or (r.status_code in (200, 202) and not r.content):
        return None
    if r.status_code == 403 and "PREMIUM" in (r.text or "").upper():
        raise SpotifyError("Playback control needs Spotify Premium. Lumi can still see what's playing.")
    if r.status_code == 404 and "NO_ACTIVE_DEVICE" in (r.text or "").upper():
        raise SpotifyError("NO_ACTIVE_DEVICE")
    if r.status_code == 429:
        raise SpotifyError(f"Spotify is rate-limiting requests; try again in {r.headers.get('Retry-After', 'a few')} s.")
    if r.status_code >= 400:
        raise SpotifyError(f"Spotify said {_err_text(r)}.")
    try:
        return r.json()
    except ValueError:
        return None


def _images(album: Dict[str, Any]) -> str:
    imgs = album.get("images") or []
    pick = min(imgs, key=lambda i: abs((i.get("width") or 300) - 300)) if imgs else {}
    url = pick.get("url") or ""
    return url if url.startswith("https://") else ""


def now_playing() -> Optional[Dict[str, Any]]:
    """The player state trimmed for the UI, or None when nothing is playing."""
    st = _api("GET", "/me/player", params={"additional_types": "episode"})
    if not st or not st.get("item"):
        return None
    item, dev = st["item"], st.get("device") or {}
    is_episode = item.get("type") == "episode"
    artists = ([(item.get("show") or {}).get("name", "")] if is_episode
               else [a.get("name", "") for a in item.get("artists") or []])
    album = (item.get("show") or {}) if is_episode else (item.get("album") or {})
    return {"title": item.get("name", ""), "artists": ", ".join(a for a in artists if a),
            "album": album.get("name", ""), "art": _images(album), "uri": item.get("uri", ""),
            "url": ((item.get("external_urls") or {}).get("spotify") or ""),
            "playing": bool(st.get("is_playing")), "progress_ms": st.get("progress_ms") or 0,
            "duration_ms": item.get("duration_ms") or 0, "shuffle": bool(st.get("shuffle_state")),
            "device": dev.get("name", ""), "volume": dev.get("volume_percent"),
            "at": time.time()}


def _devices() -> List[Dict[str, Any]]:
    return (_api("GET", "/me/player/devices") or {}).get("devices") or []


def _with_device(fn):
    """Run a player command; if no device is active, wake the first available one."""
    try:
        return fn(None)
    except SpotifyError as e:
        if str(e) != "NO_ACTIVE_DEVICE":
            raise
    devs = _devices()
    if not devs:
        raise SpotifyError("No Spotify device is open. Open Spotify on your PC or phone first.")
    dev = next((d for d in devs if d.get("type") == "Computer"), devs[0])
    return fn(dev.get("id"))


ACTIONS = ("play", "pause", "next", "previous", "volume", "shuffle")


def control(action: str, value: Any = None) -> str:
    action = str(action or "").lower()
    if action not in ACTIONS:
        raise ValueError(f"Unknown action {action!r}. Use one of: {', '.join(ACTIONS)}.")
    q = lambda dev: ({"device_id": dev} if dev else None)
    if action == "play":
        _with_device(lambda dev: _api("PUT", "/me/player/play", params=q(dev)))
        return "Resumed playback."
    if action == "pause":
        _with_device(lambda dev: _api("PUT", "/me/player/pause", params=q(dev)))
        return "Paused."
    if action == "next":
        _with_device(lambda dev: _api("POST", "/me/player/next", params=q(dev)))
        return "Skipped to the next track."
    if action == "previous":
        _with_device(lambda dev: _api("POST", "/me/player/previous", params=q(dev)))
        return "Went back a track."
    if action == "volume":
        try:
            pct = max(0, min(100, int(value)))
        except (TypeError, ValueError):
            raise ValueError("Volume must be a number from 0 to 100.")
        _with_device(lambda dev: _api("PUT", "/me/player/volume", params={"volume_percent": pct, **(q(dev) or {})}))
        return f"Volume set to {pct}%."
    on = str(value).lower() in ("1", "true", "on", "yes") if value is not None else True
    _with_device(lambda dev: _api("PUT", "/me/player/shuffle", params={"state": str(on).lower(), **(q(dev) or {})}))
    return f"Shuffle {'on' if on else 'off'}."


KINDS = ("track", "artist", "album", "playlist")


def play_search(query: str, kind: str = "track") -> str:
    query = " ".join(str(query or "").split())[:200]
    if not query:
        raise ValueError("Say what to play.")
    kind = kind if kind in KINDS else "track"
    res = _api("GET", "/search", params={"q": query, "type": kind, "limit": 1}) or {}
    items = [i for i in ((res.get(kind + "s") or {}).get("items") or []) if i]
    if not items:
        raise SpotifyError(f"Spotify found no {kind} for {query!r}.")
    top = items[0]
    body = {"uris": [top["uri"]]} if kind == "track" else {"context_uri": top["uri"]}
    _with_device(lambda dev: _api("PUT", "/me/player/play", params={"device_id": dev} if dev else None, body=body))
    who = ", ".join(a.get("name", "") for a in top.get("artists") or []) if kind in ("track", "album") else ""
    return f"Playing {kind} {top.get('name', '')!r}" + (f" by {who}" if who else "") + "."


def top_artists(limit: int = 5) -> List[str]:
    """Your most-played artists lately (for the profile Lumi keeps of your taste)."""
    res = _api("GET", "/me/top/artists", params={"limit": max(1, min(limit, 20)), "time_range": "short_term"}) or {}
    return [a.get("name", "") for a in res.get("items") or [] if a.get("name")]


def describe(np: Optional[Dict[str, Any]]) -> str:
    if not np:
        return "Nothing is playing on Spotify right now."
    t = lambda ms: f"{int(ms // 60000)}:{int(ms // 1000 % 60):02d}"
    state = "Playing" if np["playing"] else "Paused"
    by = f" by {np['artists']}" if np["artists"] else ""
    on = f" on {np['device']}" if np["device"] else ""
    return (f"{state}: {np['title']!r}{by} ({np['album']}){on}, "
            f"{t(np['progress_ms'])} of {t(np['duration_ms'])}.")


# ─── Lumi's tools ────────────────────────────────────────────────────

AGENT = "main"
CONFIRM_TOOLS: set = set()
TASKS = {"spotify_now_playing": "Checking Spotify", "spotify_control": "Controlling Spotify",
         "spotify_play": "Playing music on Spotify"}
TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "spotify_now_playing",
        "description": "Get the song playing on the user's Spotify right now.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "spotify_control",
        "description": "Play, pause, skip, go back, set volume or shuffle on the user's Spotify.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": list(ACTIONS)},
            "value": {"type": ["integer", "boolean", "string"],
                      "description": "Volume 0-100, or true/false for shuffle."}},
            "required": ["action"]}}},
    {"type": "function", "function": {
        "name": "spotify_play",
        "description": "Search Spotify and play the best match for a song, artist, album or playlist.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "What to play, e.g. 'lofi beats' or 'Bohemian Rhapsody'."},
            "kind": {"type": "string", "enum": list(KINDS)}},
            "required": ["query"]}}},
]


def available(ctx: ToolContext) -> bool:
    return connected()


async def run_tool(name: str, args: Dict[str, Any], ctx: ToolContext) -> str:
    import asyncio
    loop = asyncio.get_running_loop()
    args = args if isinstance(args, dict) else {}
    try:
        if name == "spotify_now_playing":
            return describe(await loop.run_in_executor(None, now_playing))
        if name == "spotify_control":
            return await loop.run_in_executor(None, control, args.get("action"), args.get("value"))
        if name == "spotify_play":
            return await loop.run_in_executor(None, play_search, args.get("query"), args.get("kind") or "track")
    except (SpotifyError, ValueError) as e:
        return f"Error: {e}"
    except requests.RequestException as e:
        return f"Error: couldn't reach Spotify ({type(e).__name__})."
    return f"Error: unknown Spotify tool {name}."


# ─── HTTP ────────────────────────────────────────────────────────────

def register(router, server) -> None:
    """/api/spotify/*: status, client, connect, callback, disconnect, player."""
    import asyncio
    import html
    from urllib.parse import urlparse
    from aiohttp import web
    from modules.socketIO import _json_error

    loopback = {"127.0.0.1", "localhost", "::1", "[::1]"}

    async def gate(request, mutate=False):
        if not server._origin_ok(request):
            return _json_error(403, "Bad origin."), {}
        if not server._current_user(request):
            return _json_error(401, "Not logged in."), {}
        body = {}
        if mutate:
            try:
                body = await request.json()
            except Exception:
                body = {}
            body = body if isinstance(body, dict) else {}
            ok, err = server._require_csrf(request, body)
            if not ok:
                return _json_error(403, err), {}
        return None, body

    async def run(fn, *args):
        try:
            out = await asyncio.get_running_loop().run_in_executor(None, fn, *args)
        except NotConnected as e:
            return _json_error(409, str(e))
        except ValueError as e:
            return _json_error(400, str(e))
        except SpotifyError as e:
            return _json_error(502, str(e))
        except requests.RequestException as e:
            return _json_error(502, f"Couldn't reach Spotify ({type(e).__name__}).")
        return web.json_response(out if out is not None else {"ok": True}, headers={"Cache-Control": "no-store"})

    async def get_status(request):
        err, _ = await gate(request)
        return err or web.json_response(status(), headers={"Cache-Control": "no-store"})

    async def set_client(request):
        err, body = await gate(request, True)
        return err or await run(lambda: (save_client(str(body.get("client_id") or "")), status())[1])

    async def connect(request):
        err, _ = await gate(request, True)
        if err:
            return err
        host = urlparse(f"//{request.host}").hostname or ""
        if host not in loopback:
            return _json_error(403, "Connect Spotify from the PC that runs Lumi, at 127.0.0.1:5000.")
        # Spotify only accepts 127.0.0.1 (not "localhost") as a loopback redirect.
        port = urlparse(f"//{request.host}").port
        redirect = f"http://127.0.0.1{':' + str(port) if port else ''}/api/spotify/callback"
        return await run(lambda: {"auth_url": begin(redirect), "redirect_uri": redirect})

    async def callback(request):
        def page(title, message, code=200):
            return web.Response(text=(f"<!doctype html><meta charset=utf-8><title>{html.escape(title)}</title>"
                                      "<body style='font:16px system-ui;max-width:32rem;margin:4rem auto;padding:0 1rem'>"
                                      f"<h2>{html.escape(title)}</h2><p>{html.escape(message)}</p>"),
                                status=code, content_type="text/html")
        if not server._current_user(request):
            return page("Sign in to Lumi first", "Open Lumi at 127.0.0.1:5000, sign in, and connect again.", 401)
        if request.query.get("error"):
            return page("Spotify sign-in cancelled", f'Spotify said: {request.query["error"][:80]}', 400)
        code, state = request.query.get("code", ""), request.query.get("state", "")
        if not code or not state:
            return page("Something's missing", "That link doesn't carry a sign-in code.", 400)
        try:
            st = await asyncio.get_running_loop().run_in_executor(None, finish, state, code)
        except (SpotifyError, requests.RequestException) as e:
            return page("Couldn't connect Spotify", str(e), 400)
        note = "" if st["premium"] else " Your account isn't Premium, so Lumi can show what's playing but not control it."
        return page("Spotify connected", f"Signed in as {st['user'] or 'your account'}. "
                                         f"You can close this tab and go back to Lumi.{note}")

    async def do_disconnect(request):
        err, _ = await gate(request, True)
        return err or await run(lambda: (disconnect(), status())[1])

    async def player(request):
        err, _ = await gate(request)
        return err or await run(lambda: {"now": now_playing()})

    async def player_action(request):
        err, body = await gate(request, True)
        if err:
            return err
        action = request.match_info.get("action", "")
        if action == "search":
            return await run(lambda: {"message": play_search(str(body.get("query") or ""),
                                                             str(body.get("kind") or "track"))})
        return await run(lambda: {"message": control(action, body.get("value"))})

    router.add_get("/api/spotify/status", get_status)
    router.add_post("/api/spotify/client", set_client)
    router.add_post("/api/spotify/connect", connect)
    router.add_get("/api/spotify/callback", callback)
    router.add_post("/api/spotify/disconnect", do_disconnect)
    router.add_get("/api/spotify/player", player)
    router.add_post("/api/spotify/player/{action}", player_action)

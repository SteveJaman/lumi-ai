"""
Spotify: see what's playing and control playback with your own account.

Sign-in is OAuth 2.0 with PKCE, so only a Client ID is needed (no secret). You
create a free app at https://developer.spotify.com/dashboard, add the redirect
URI http://127.0.0.1:5000/api/spotify/callback, and paste its Client ID in
Integrations. The sign-in stays on this PC in data/credentials/spotify.json.

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
    media_seek, media_shuffle, media_repeat   through modules/media_bridge.py, so they
                                              also work on Free via the Spotify app
    media_playlist_tracks                     a playlist (by name), album or Liked Songs
None asks for approval: they only change what's playing on your Spotify.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
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
          "user-read-recently-played user-top-read playlist-read-private "
          "user-library-read user-library-modify")        # Liked Songs, podcasts, the like button
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
    tok = data.get("token") or {}
    granted = set(str(tok.get("scope") or "").split())
    connected_ = bool(tok.get("refresh_token"))
    product = user.get("product")
    # True / False when known; None when Spotify hasn't said yet (the first player call tells).
    premium = (product == "premium") if product else data.get("plan_premium")
    return {"configured": bool(data.get("client_id")), "connected": connected_,
            "user": user.get("display_name") or user.get("id"), "product": product,
            "premium": premium,
            # Signed in before a feature needed a new permission (e.g. Liked Songs): sign in again.
            "needs_reconnect": connected_ and bool(granted) and not set(SCOPES.split()) <= granted}


def connected() -> bool:
    return status()["connected"]


def disconnect() -> None:
    with _lock:
        data = _load()
        _save({"client_id": data["client_id"]} if data.get("client_id") else {})
    _plan.update(premium=None, loaded=False)


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
        raise SpotifyError("That sign-in link expired. Start again from Integrations.")
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
            raise NotConnected("Spotify isn't connected. Connect it in Integrations.")
        if tok.get("access_token") and time.time() < float(tok.get("expires_at", 0)):
            return tok["access_token"]
        r = requests.post(TOKEN_URL, data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                           "client_id": data.get("client_id", "")}, timeout=TIMEOUT_S)
        if r.status_code != 200:
            if r.status_code in (400, 401):
                data.pop("token", None)
                _save(data)
                raise NotConnected("Spotify signed you out. Connect it again in Integrations.")
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


class PremiumRequired(SpotifyError):
    """Spotify refused a player call because the account isn't Premium."""


class ApiError(SpotifyError):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


# Your plan, learned from what Spotify allows: Development Mode apps no longer
# get the "product" field from /me (Feb 2026), so a Premium-only call that works
# means Premium, and a PREMIUM_REQUIRED refusal means Free. Saved with the sign-in.
_plan = {"premium": None, "loaded": False}


def _remember_plan(premium: bool) -> None:
    if _plan["loaded"] and _plan["premium"] is premium:
        return
    with _lock:
        data = _load()
        if data.get("plan_premium") is not premium:
            data["plan_premium"] = premium
            if data.get("client_id"):
                _save(data)
    _plan.update(premium=premium, loaded=True)


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
    player_write = method in ("PUT", "POST") and path.startswith("/me/player")
    if r.status_code == 403 and "PREMIUM" in (r.text or "").upper():
        _remember_plan(False)
        raise PremiumRequired("That needs Spotify Premium.")
    if r.status_code == 404 and "NO_ACTIVE_DEVICE" in (r.text or "").upper():
        raise SpotifyError("NO_ACTIVE_DEVICE")
    if r.status_code == 429:
        raise ApiError(f"Spotify is rate-limiting requests; try again in {r.headers.get('Retry-After', 'a few')} s.", 429)
    if r.status_code >= 400:
        raise ApiError(f"Spotify said {_err_text(r)}.", r.status_code)
    if player_write:
        _remember_plan(True)                              # a Premium-only call just worked
    if r.status_code == 204 or (r.status_code in (200, 202) and not r.content):
        return None
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
            "id": item.get("id", ""), "type": item.get("type", "track"),
            "repeat": st.get("repeat_state") or "off", "device_id": dev.get("id", ""),
            "device_type": dev.get("type", ""), "context_uri": (st.get("context") or {}).get("uri", ""),
            "at": time.time()}


def _devices() -> List[Dict[str, Any]]:
    return (_api("GET", "/me/player/devices") or {}).get("devices") or []


# ─── library, queue, devices (the Media tab) ────────────────────────

def _song_count(playlist: Dict[str, Any]) -> int:
    """Song count; Spotify's Feb 2026 change moved it from "tracks" to "items"."""
    for key in ("items", "tracks"):
        box = playlist.get(key)
        if isinstance(box, dict) and box.get("total"):
            return int(box["total"])
    return 0


def _item(obj: Dict[str, Any], kind: str) -> Optional[Dict[str, Any]]:
    """Any Spotify object as a row: {uri, title, subtitle, art, kind}."""
    if not obj or not obj.get("uri"):
        return None
    if kind == "track":
        sub = ", ".join(a.get("name", "") for a in obj.get("artists") or [])
        art = _images(obj.get("album") or {})
    elif kind == "episode":
        sub, art = (obj.get("show") or {}).get("name", ""), _images(obj.get("show") or obj)
    elif kind == "playlist":
        sub = ((obj.get("owner") or {}).get("display_name") or "") + (
            f" · {n} songs" if (n := _song_count(obj)) else "")
        art = _images(obj)
    elif kind == "album":
        sub, art = ", ".join(a.get("name", "") for a in obj.get("artists") or []), _images(obj)
    elif kind == "show":
        sub, art = obj.get("publisher", ""), _images(obj)
    else:                                                    # artist
        sub, art = "Artist", _images(obj)
    return {"uri": obj["uri"], "title": obj.get("name", ""), "subtitle": sub, "art": art, "kind": kind,
            "id": obj.get("id", "")}


def _rows(objs, kind) -> List[Dict[str, Any]]:
    return [r for r in (_item(o, kind) for o in objs or []) if r]


def library(limit: int = 20) -> Dict[str, List[Dict[str, Any]]]:
    """Playlists, recent Liked Songs, recently played and saved podcasts."""
    def get(path, params=None):
        try:
            return _api("GET", path, params=params) or {}
        except SpotifyError:                       # e.g. a scope from an older sign-in: skip that shelf
            return {}
    recent, seen = [], set()
    for it in get("/me/player/recently-played", {"limit": 30}).get("items") or []:
        t = it.get("track") or {}
        if t.get("uri") and t["uri"] not in seen:
            seen.add(t["uri"])
            recent.append(t)
    return {
        "playlists": _rows(get("/me/playlists", {"limit": limit}).get("items"), "playlist"),
        "liked": _rows([i.get("track") for i in get("/me/tracks", {"limit": limit}).get("items") or []], "track"),
        "recent": _rows(recent[:limit], "track"),
        "shows": _rows([i.get("show") for i in get("/me/shows", {"limit": limit}).get("items") or []], "show"),
        "albums": _rows([i.get("album") for i in get("/me/albums", {"limit": limit}).get("items") or []], "album"),
    }


def search(query: str, kinds=("track", "playlist", "album", "show"), limit: int = 6) -> List[Dict[str, Any]]:
    query = " ".join(str(query or "").split())[:200]
    limit = max(1, min(int(limit), 10))                   # Spotify's maximum since Feb 2026
    if not query:
        return []
    res = _api("GET", "/search", params={"q": query, "type": ",".join(kinds), "limit": limit}) or {}
    out: List[Dict[str, Any]] = []
    for k in kinds:
        out += _rows((res.get(k + "s") or {}).get("items"), k)
    return out


def queue() -> Dict[str, Any]:
    """What's playing and what's up next (Spotify can't remove or reorder these)."""
    q = _api("GET", "/me/player/queue") or {}
    cur = q.get("currently_playing")
    rows = [r for r in (_item(t, t.get("type", "track")) for t in q.get("queue") or [] if t) if r]
    return {"current": _item(cur, cur.get("type", "track")) if cur else None, "queue": rows[:30]}


def add_to_queue(uri: str) -> str:
    if not str(uri or "").startswith("spotify:"):
        raise ValueError("That isn't a Spotify item.")
    _with_device(lambda dev: _api("POST", "/me/player/queue", params={"uri": uri, **({"device_id": dev} if dev else {})}))
    return "Added to the queue."


def play_uri(uri: str) -> str:
    """Play a track/episode now, or start a playlist, album, artist or show."""
    uri = str(uri or "")
    if not uri.startswith("spotify:"):
        raise ValueError("That isn't a Spotify item.")
    kind = uri.split(":")[1] if uri.count(":") >= 2 else ""
    body = {"uris": [uri]} if kind in ("track", "episode") else {"context_uri": uri}
    _with_device(lambda dev: _api("PUT", "/me/player/play", params={"device_id": dev} if dev else None, body=body))
    return "Playing."


def play_uris(uris: List[str]) -> str:
    uris = [u for u in uris if str(u).startswith("spotify:track:")][:50]
    if not uris:
        raise ValueError("Nothing to play.")
    _with_device(lambda dev: _api("PUT", "/me/player/play", params={"device_id": dev} if dev else None,
                                  body={"uris": uris}))
    return "Playing."


def devices() -> List[Dict[str, Any]]:
    return [{"id": d.get("id"), "name": d.get("name", ""), "type": d.get("type", ""),
             "active": bool(d.get("is_active")), "volume": d.get("volume_percent")}
            for d in _devices() if d.get("id")]


def transfer(device_id: str, play: bool = True) -> str:
    if not device_id:
        raise ValueError("Pick a device.")
    _api("PUT", "/me/player", body={"device_ids": [device_id], "play": bool(play)})
    return "Switched device."


REPEAT = ("off", "context", "track")


def set_repeat(state: str) -> str:
    if state not in REPEAT:
        raise ValueError("Repeat is off, context or track.")
    _with_device(lambda dev: _api("PUT", "/me/player/repeat", params={"state": state, **({"device_id": dev} if dev else {})}))
    return {"off": "Repeat off.", "context": "Repeating this playlist.", "track": "Repeating this song."}[state]


# Liking goes through /me/library with URIs (Feb 2026: /me/tracks save/remove/contains were replaced).
LIBRARY_BATCH = 40


def as_uri(x: str, kind: str = "track") -> str:
    x = str(x or "")
    return x if x.startswith("spotify:") else (f"spotify:{kind}:{x}" if x else "")


def liked_many(uris: List[str]) -> List[bool]:
    """Whether each URI is in your library (Liked Songs for tracks), in order."""
    uris = [as_uri(u) for u in uris]
    out: List[bool] = []
    for i in range(0, len(uris), LIBRARY_BATCH):
        chunk = [u for u in uris[i:i + LIBRARY_BATCH]]
        res = _api("GET", "/me/library/contains", params={"uris": ",".join(chunk)}) or []
        out += [bool(v) for v in res] + [False] * (len(chunk) - len(res))
    return out


def is_liked(track: str) -> bool:
    if not track:
        return False
    return liked_many([track])[0]


def set_liked(track: str, on: bool) -> str:
    """Save or remove any track (a URI or an id), from any row, not just what's playing."""
    uri = as_uri(track)
    if not uri.startswith("spotify:track:") and not uri.startswith("spotify:episode:"):
        raise ValueError("Nothing to like.")
    _api("PUT" if on else "DELETE", "/me/library", params={"uris": uri})
    return "Saved to your Liked Songs." if on else "Removed from your Liked Songs."


# ─── pages: library shelves and track lists (the mini Spotify view) ─

PAGE_MAX = 50
SHELVES = ("playlists", "liked", "albums", "shows", "recent")


def _me_id() -> str:
    return str((_load().get("user") or {}).get("id") or "")


def _page(res: Dict[str, Any], rows: List[Dict[str, Any]], offset: int, raw_count: int) -> Dict[str, Any]:
    return {"items": rows, "total": int(res.get("total") or len(rows)), "offset": offset,
            "next_offset": offset + raw_count if res.get("next") else None}


def _limit(limit: int) -> int:
    try:
        return max(1, min(int(limit), PAGE_MAX))
    except (TypeError, ValueError):
        return 20


def library_page(shelf: str, offset: int = 0, limit: int = 20) -> Dict[str, Any]:
    """One page of a library shelf: playlists, liked (songs), albums, shows or recent."""
    if shelf not in SHELVES:
        raise ValueError(f"Shelf is one of: {', '.join(SHELVES)}.")
    offset, limit = max(0, int(offset or 0)), _limit(limit)
    if shelf == "recent":
        res = _api("GET", "/me/player/recently-played", params={"limit": limit}) or {}
        tracks = [i.get("track") for i in res.get("items") or []]
        return {"items": _rows(tracks, "track"), "total": len(tracks), "offset": 0, "next_offset": None}
    path = {"playlists": "/me/playlists", "liked": "/me/tracks", "albums": "/me/albums", "shows": "/me/shows"}[shelf]
    # Spotify sometimes answers with fewer items than asked (e.g. 2 playlists for limit=10)
    # while saying there are more: keep asking until the page is full, at most 5 calls.
    res: Dict[str, Any] = {}
    raw: List[Dict[str, Any]] = []
    at = offset
    for _ in range(5):
        res = _api("GET", path, params={"limit": limit - len(raw), "offset": at}) or {}
        got = [i for i in res.get("items") or []]
        raw += got
        at += len(got)
        if not got or not res.get("next") or len(raw) >= limit:
            break
    if shelf == "playlists":
        me = _me_id()
        rows = []
        for p in raw:
            row = _item(p, "playlist")
            if row:
                owner = str((p.get("owner") or {}).get("id") or "")
                # Spotify only lets apps list the songs of playlists you own or collaborate on.
                row["browsable"] = bool(p.get("collaborative")) or (bool(me) and owner == me)
                rows.append(row)
    elif shelf == "liked":
        rows = [dict(r, is_liked=True) for r in _rows([i.get("track") for i in raw], "track")]
    elif shelf == "albums":
        rows = _rows([i.get("album") for i in raw], "album")
    else:
        rows = _rows([i.get("show") for i in raw], "show")
    return _page(res, rows, offset, len(raw))


def _track_row(t: Dict[str, Any], number: int, local: bool = False, album: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    alb = album or t.get("album") or t.get("show") or {}
    artists = ", ".join(a.get("name", "") for a in t.get("artists") or []) or (t.get("show") or {}).get("name", "")
    return {"uri": t.get("uri", ""), "id": t.get("id") or "", "title": t.get("name", ""), "artist": artists,
            "album": alb.get("name", ""), "art_url": _images(alb), "duration_ms": t.get("duration_ms") or 0,
            "is_liked": None, "number": number, "kind": t.get("type", "track"),
            "playable": not local and bool(t.get("uri")) and t.get("is_playable", True) is not False}


def _fill_liked(rows: List[Dict[str, Any]]) -> None:
    uris = [r["uri"] for r in rows if r["playable"] and r["uri"].startswith(("spotify:track:", "spotify:episode:"))]
    if not uris:
        return
    try:
        flags = dict(zip(uris, liked_many(uris)))
    except SpotifyError:
        return                                            # the hearts stay unknown; the list still shows
    for r in rows:
        if r["uri"] in flags:
            r["is_liked"] = flags[r["uri"]]


def playlist_tracks(playlist_id: str, offset: int = 0, limit: int = 50) -> Dict[str, Any]:
    """Songs of one of your playlists, via GET /playlists/{id}/items (the old /tracks is deprecated)."""
    pid = str(playlist_id or "").split(":")[-1]
    if not pid.isalnum():
        raise ValueError("That isn't a Spotify playlist.")
    offset, limit = max(0, int(offset or 0)), _limit(limit)
    try:
        res = _api("GET", f"/playlists/{pid}/items",
                   params={"offset": offset, "limit": limit, "additional_types": "track,episode"}) or {}
    except ApiError as e:
        if e.status in (403, 404):
            raise SpotifyError("Spotify only lets apps list the songs of playlists you own or collaborate on.")
        raise
    rows = []
    for n, entry in enumerate(res.get("items") or [], start=offset + 1):
        t = (entry or {}).get("item") or (entry or {}).get("track")
        if not t:
            continue                                      # a removed song leaves an empty slot
        rows.append(_track_row(t, n, local=bool(entry.get("is_local"))))
    _fill_liked(rows)
    return _page(res, rows, offset, len(res.get("items") or []))


def liked_tracks(offset: int = 0, limit: int = 50) -> Dict[str, Any]:
    offset, limit = max(0, int(offset or 0)), _limit(limit)
    res = _api("GET", "/me/tracks", params={"offset": offset, "limit": limit}) or {}
    raw = res.get("items") or []
    rows = [dict(_track_row(i.get("track") or {}, n), is_liked=True)
            for n, i in enumerate(raw, start=offset + 1) if (i or {}).get("track")]
    return _page(res, rows, offset, len(raw))


_album_cache: Dict[str, Dict[str, Any]] = {}


def album_tracks(album_id: str, offset: int = 0, limit: int = 50) -> Dict[str, Any]:
    aid = str(album_id or "").split(":")[-1]
    if not aid.isalnum():
        raise ValueError("That isn't a Spotify album.")
    offset, limit = max(0, int(offset or 0)), _limit(limit)
    if aid not in _album_cache:
        a = _api("GET", f"/albums/{aid}") or {}
        _album_cache[aid] = {"name": a.get("name", ""), "images": a.get("images") or []}
        if len(_album_cache) > 64:
            _album_cache.pop(next(iter(_album_cache)))
    res = _api("GET", f"/albums/{aid}/tracks", params={"offset": offset, "limit": limit}) or {}
    raw = res.get("items") or []
    rows = [_track_row(t, n, album=_album_cache[aid]) for n, t in enumerate(raw, start=offset + 1) if t]
    _fill_liked(rows)
    return _page(res, rows, offset, len(raw))


def tracks(kind: str, item_id: str = "", offset: int = 0, limit: int = 50) -> Dict[str, Any]:
    """The main view's track list: a playlist, an album, or your Liked Songs."""
    if kind == "playlist":
        return playlist_tracks(item_id, offset, limit)
    if kind == "album":
        return album_tracks(item_id, offset, limit)
    if kind == "liked":
        return liked_tracks(offset, limit)
    raise ValueError("Pick a playlist, an album or Liked Songs.")


# ─── player controls (Premium) ───────────────────────────────────────

def seek(position_ms: int) -> str:
    try:
        ms = max(0, int(position_ms))
    except (TypeError, ValueError):
        raise ValueError("Seek needs a position in milliseconds.")
    _with_device(lambda dev: _api("PUT", "/me/player/seek", params={"position_ms": ms, **({"device_id": dev} if dev else {})}))
    return f"Jumped to {ms // 60000}:{ms // 1000 % 60:02d}."


def set_shuffle(on: bool) -> str:
    return control("shuffle", bool(on))


def set_volume(percent: int) -> str:
    return control("volume", percent)


def queue_track(uri: str) -> str:
    return add_to_queue(uri)


def play_in_context(context_uri: str = "", track_uri: str = "", uris: Optional[List[str]] = None) -> str:
    """Start a playlist or album at a given song (or a list of songs, e.g. Liked Songs, at one of them)."""
    body: Dict[str, Any]
    if context_uri:
        if not str(context_uri).startswith("spotify:"):
            raise ValueError("That isn't a Spotify playlist or album.")
        body = {"context_uri": context_uri}
        if track_uri:
            body["offset"] = {"uri": track_uri}
    else:
        lst = [u for u in (uris or []) if str(u).startswith("spotify:track:")][:PAGE_MAX]
        if not lst:
            raise ValueError("Nothing to play.")
        body = {"uris": lst, "offset": {"position": lst.index(track_uri) if track_uri in lst else 0}}
    _with_device(lambda dev: _api("PUT", "/me/player/play", params={"device_id": dev} if dev else None, body=body))
    return "Playing."


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

AGENT = "operate"
CONFIRM_TOOLS: set = set()
TASKS = {"spotify_now_playing": "Checking Spotify", "spotify_control": "Controlling Spotify",
         "spotify_play": "Playing music on Spotify", "media_seek": "Seeking in the song",
         "media_shuffle": "Setting shuffle", "media_repeat": "Setting repeat",
         "media_playlist_tracks": "Reading a playlist"}
REPEAT_MODES = {"off": "off", "all": "context", "song": "track"}      # what Lumi says -> what Spotify says
TRACKS_SHOWN = 25
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
    {"type": "function", "function": {
        "name": "media_seek",
        "description": "Jump to a point in the song playing on Spotify.",
        "parameters": {"type": "object", "properties": {
            "position": {"type": "string",
                         "description": "'1:30' or '90' (seconds from the start), or '+30' / '-15' to move from here."}},
            "required": ["position"]}}},
    {"type": "function", "function": {
        "name": "media_shuffle",
        "description": "Turn shuffle on or off on Spotify.",
        "parameters": {"type": "object", "properties": {"on": {"type": "boolean"}}, "required": ["on"]}}},
    {"type": "function", "function": {
        "name": "media_repeat",
        "description": "Set Spotify's repeat to off, all (the playlist or album), or song (this song).",
        "parameters": {"type": "object", "properties": {
            "mode": {"type": "string", "enum": list(REPEAT_MODES)}}, "required": ["mode"]}}},
    {"type": "function", "function": {
        "name": "media_playlist_tracks",
        "description": "List the songs in one of the user's Spotify playlists, an album, or their Liked Songs.",
        "parameters": {"type": "object", "properties": {
            "playlist": {"type": "string",
                         "description": "Playlist name, 'liked' for Liked Songs, or a Spotify playlist/album link or URI."},
            "offset": {"type": "integer", "description": f"Skip this many songs, to page past the first {TRACKS_SHOWN}."}},
            "required": ["playlist"]}}},
]


# ─── media_* tools: through the media bridge (Premium first, the Spotify app's controls on Free) ──

def parse_position(text: Any, now_ms: int) -> int:
    """'1:30', '90', '+30', '-15' -> a position in ms (relative ones from `now_ms`)."""
    s = str(text if text is not None else "").strip().lower().replace(" ", "")
    s = s[:-1] if s.endswith("s") else s
    m = re.fullmatch(r"([+-]?)(?:(\d+):)?(\d+(?:\.\d+)?)", s)
    if not m or (m.group(2) is not None and float(m.group(3)) >= 60):
        raise ValueError("Position looks like '1:30', '90', '+30' or '-15'.")
    secs = int(m.group(2) or 0) * 60 + float(m.group(3))
    if m.group(1) == "+":
        return max(0, int(now_ms + secs * 1000))
    if m.group(1) == "-":
        return max(0, int(now_ms - secs * 1000))
    return int(secs * 1000)


def _clock(ms: int) -> str:
    s = max(0, int(ms // 1000))
    return f"{s // 60}:{s % 60:02d}"


def _media_command(action: str, value: Any) -> str:
    from modules import media_bridge
    b = media_bridge.bridge()
    st = b.state(watch=False)
    if st.get("source") != "spotify":
        b.refresh()
    if action == "seek":
        st = b.state(watch=False)
        if not st.get("duration_ms"):
            raise ValueError("Nothing is playing on Spotify.")
        elapsed = (st.get("progress_ms") or 0) + (
            (time.time() - (st.get("at") or time.time())) * 1000 if st.get("playing") else 0)
        target = min(parse_position(value, int(elapsed)), int(st["duration_ms"]) - 500)
        b.command("seek", target)
        return f"Jumped to {_clock(target)} of {_clock(st['duration_ms'])}."
    b.command(action, value)
    if action == "shuffle":
        return "Shuffle is on." if value else "Shuffle is off."
    return {"off": "Repeat is off.", "context": "Repeating the playlist or album.",
            "track": "Repeating this song."}[value]


def _find_list(name: str) -> Dict[str, Any]:
    """A playlist by name, 'liked', or a playlist/album link or URI -> {kind, id, title, browsable}."""
    raw = str(name or "").strip()
    if not raw:
        raise ValueError("Which playlist? Give its name, or 'liked' for Liked Songs.")
    if raw.lower() in ("liked", "liked songs", "my liked songs", "favorites", "favourites"):
        return {"kind": "liked", "id": "", "title": "Liked Songs", "browsable": True}
    m = re.search(r"(playlist|album)[:/]([A-Za-z0-9]{10,})", raw)
    if m:
        return {"kind": m.group(1), "id": m.group(2), "title": "", "browsable": True}
    want, offset, best = raw.lower(), 0, None
    for _ in range(10):                                      # up to ~500 playlists
        page = library_page("playlists", offset, PAGE_MAX)
        for p in page["items"]:
            title = (p.get("title") or "").lower()
            if title == want:
                return {"kind": "playlist", **p}
            if best is None and want in title:
                best = p
        if page.get("next_offset") is None:
            break
        offset = page["next_offset"]
    if best:
        return {"kind": "playlist", **best}
    raise ValueError(f"No playlist called {raw!r} in the user's library.")


def describe_tracks(name: str, offset: int = 0) -> str:
    found = _find_list(name)
    if found["kind"] == "playlist" and not found.get("browsable", True):
        return (f"Spotify doesn't let apps list the songs of {found.get('title') or 'that playlist'!r}, "
                "because the user doesn't own it. It can still be played with spotify_play.")
    page = tracks(found["kind"], found["id"], max(0, int(offset or 0)), TRACKS_SHOWN)
    title = found.get("title") or found["kind"].title()
    items = page["items"]
    if not items:
        return f"{title!r} has no songs at that point (it has {page['total']})."
    first = items[0]["number"]
    lines = [f"{title!r}: {page['total']} songs; showing {first}-{items[-1]['number']}."]
    for r in items:
        heart = " ♥" if r.get("is_liked") else ""
        lines.append(f"{r['number']}. {r['title']} - {r['artist']} ({_clock(r['duration_ms'])}){heart}")
    if page.get("next_offset") is not None:
        lines.append(f"More: call again with offset {page['next_offset']}.")
    return "\n".join(lines)


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
        if name == "media_seek":
            return await loop.run_in_executor(None, _media_command, "seek", args.get("position"))
        if name == "media_shuffle":
            on = args.get("on")
            on = on.strip().lower() in ("true", "on", "yes", "1") if isinstance(on, str) else bool(on)
            return await loop.run_in_executor(None, _media_command, "shuffle", on)
        if name == "media_repeat":
            mode = str(args.get("mode") or "").lower()
            if mode not in REPEAT_MODES:
                return f"Error: repeat mode is one of {', '.join(REPEAT_MODES)}."
            return await loop.run_in_executor(None, _media_command, "repeat", REPEAT_MODES[mode])
        if name == "media_playlist_tracks":
            try:
                offset = int(args.get("offset") or 0)
            except (TypeError, ValueError):
                offset = 0
            return await loop.run_in_executor(None, describe_tracks, args.get("playlist"), offset)
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

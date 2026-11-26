"""
Media bridge: Spotify's "now playing" for Lumi Island's Media tab and the web
sidebar. Spotify only: YouTube, browsers and other players are ignored.

  * State, library, queue, devices and likes come from your Spotify account
    over the Web API (modules/spotify.py).
  * Play / pause / skip / shuffle / repeat: the Web API on Premium. On a Free
    account Spotify refuses those, so they go through the Spotify app's own
    Windows media controls on this PC (winrt), which work on any account.
  * Volume: the Spotify app's own volume on this PC (pycaw, never the system
    volume); Spotify's volume API for Spotify playing on another device.

    bridge()                     the singleton
    bridge().state(watch=True)   the current state dict (cached; polling runs while watched)
    bridge().command(action, value)  -> (message, state)
    bridge().library(query)      Spotify playlists / liked / recent / podcasts, or search results
    bridge().up_next()           queue + devices
    bridge().add_listener(fn)    fn(state) when something changes (the island push)

Voice ducking (Ducker): while you're talking (Whisper) or Lumi is speaking
(TTS), Spotify is lowered to ~20% of its volume and brought back smoothly
afterwards. No other app is touched. Set MEDIA_DUCKING = False in
constants.py to turn it off.
"""

from __future__ import annotations

import asyncio
import base64
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

import constants

POLL_PLAYING_S = 2.5
POLL_IDLE_S = 6.0
POLL_GAMING_S = 6.0
WATCH_S = 25.0                       # a state request keeps polling on this long
ART_MAX_BYTES = 400_000
VOLUME_STEP = 5

# The only app the Media tab and voice ducking touch: Spotify on this PC.
MEDIA_APPS = frozenset({"spotify.exe"})


def _gaming() -> bool:
    return bool(getattr(constants, "GAMING_MODE", False))


# ─── Windows: the Spotify app's media controls (winrt) ──────────────

class WinMedia:
    """Windows media sessions, used only to reach the Spotify app's own
    controls (Free accounts). winrt is asynchronous; its calls run on one
    private event-loop thread."""

    def __init__(self):
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock = threading.Lock()
        self.available = True

    def _run(self, coro_fn, timeout=6.0):
        with self._lock:
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                threading.Thread(target=self._loop.run_forever, name="lumi-winmedia", daemon=True).start()
        fut = asyncio.run_coroutine_threadsafe(coro_fn(), self._loop)
        return fut.result(timeout)

    async def _session(self, prefer: str = ""):
        """The current session, or the one whose app id contains `prefer`
        (e.g. "spotify": the Spotify app's own media controls)."""
        from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as M
        mgr = await M.request_async()
        if prefer:
            for s in mgr.get_sessions():
                if prefer in str(s.source_app_user_model_id or "").lower():
                    return s
            return None
        return mgr.get_current_session()

    def has_app(self, prefer: str) -> bool:
        try:
            return self._run(lambda: self._session(prefer)) is not None
        except Exception:
            return False

    def command(self, action: str, prefer: str = "", value: Any = None) -> bool:
        async def go():
            s = await self._session(prefer)
            if s is None:
                return False
            info = s.get_playback_info()
            if action == "toggle":
                return await s.try_toggle_play_pause_async()
            if action == "play":
                return await s.try_play_async()
            if action == "pause":
                return await s.try_pause_async()
            if action == "next":
                return await s.try_skip_next_async()
            if action == "previous":
                return await s.try_skip_previous_async()
            if action == "shuffle":
                on = (not bool(info.is_shuffle_active)) if value is None else bool(value)
                return await s.try_change_shuffle_active_async(on)
            if action == "repeat":
                from winrt.windows.media import MediaPlaybackAutoRepeatMode as R
                want = {"off": 0, "track": 1, "context": 2}.get(value)
                cur = int(info.auto_repeat_mode or 0)
                return await s.try_change_auto_repeat_mode_async(R(want if want is not None else (cur + 1) % 3))
            if action == "seek":
                return await s.try_change_playback_position_async(int(value) * 10_000)   # ms -> 100 ns ticks
            return False
        try:
            return bool(self._run(go))
        except Exception:
            return False


# ─── Windows: volume (pycaw) ────────────────────────────────────────

class Audio:
    """Per-app volume (Spotify's). Every call initialises COM on its thread."""

    def _com(self):
        import comtypes
        try:
            comtypes.CoInitialize()
        except OSError:
            pass

    def sessions(self) -> List[Any]:
        self._com()
        from pycaw.pycaw import AudioUtilities
        return [s for s in AudioUtilities.GetAllSessions() if s.Process]

    def app_volume(self, process: str) -> Optional[float]:
        for s in self.sessions():
            if s.Process.name().lower() == process:
                return s.SimpleAudioVolume.GetMasterVolume()
        return None

    def set_app_volume(self, process: str, level: float) -> bool:
        hit = False
        for s in self.sessions():
            if s.Process.name().lower() == process:
                s.SimpleAudioVolume.SetMasterVolume(max(0.0, min(1.0, level)), None)
                hit = True
        return hit

# ─── voice ducking ──────────────────────────────────────────────────

class Ducker:
    """Lowers Spotify while someone is talking, then fades it back. If you
    change its volume while it's ducked, it's left where you put it."""

    def __init__(self, signals, audio: Optional[Audio] = None, level: Optional[float] = None,
                 own_pid: Optional[int] = None):
        self.signals = signals
        self.audio = audio or Audio()
        self.level = float(level if level is not None else getattr(constants, "MEDIA_DUCK_LEVEL", 0.2))
        self.own_pid = own_pid if own_pid is not None else os.getpid()
        self.saved: Dict[int, Tuple[Any, float, float]] = {}      # pid -> (session, original, ducked-to)
        self._quiet_since = 0.0
        self._stop = threading.Event()

    def talking(self) -> bool:
        s = self.signals
        return bool(getattr(s, "AI_speaking", False) or getattr(s, "human_speaking", False))

    def _media_sessions(self):
        out = []
        for s in self.audio.sessions():
            try:
                if s.Process.pid == self.own_pid or s.Process.name().lower() not in MEDIA_APPS:
                    continue
                if s.State != 1:                                  # only apps making sound right now
                    continue
                out.append(s)
            except Exception:
                continue
        return out

    def duck(self, steps: int = 5, step_s: float = 0.05) -> None:
        for s in self._media_sessions():
            pid = s.Process.pid
            if pid in self.saved:
                continue
            vol = s.SimpleAudioVolume.GetMasterVolume()
            self.saved[pid] = (s, vol, vol * self.level)
        self._ramp([(s, orig, low) for s, orig, low in self.saved.values()], steps, step_s)

    def restore(self, steps: int = 12, step_s: float = 0.05) -> None:
        moves = []
        for pid, (s, orig, low) in list(self.saved.items()):
            try:
                cur = s.SimpleAudioVolume.GetMasterVolume()
            except Exception:
                continue
            if abs(cur - low) <= 0.03:                            # you didn't move it meanwhile
                moves.append((s, low, orig))
        self.saved.clear()
        self._ramp(moves, steps, step_s)

    def _ramp(self, moves, steps, step_s):
        for i in range(1, steps + 1):
            for s, a, b in moves:
                try:
                    s.SimpleAudioVolume.SetMasterVolume(max(0.0, min(1.0, a + (b - a) * i / steps)), None)
                except Exception:
                    pass
            if step_s:
                time.sleep(step_s)

    def tick(self, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        if self.talking():
            self._quiet_since = 0.0
            self.duck()                                           # also catches an app that started later
        elif self.saved:
            if not self._quiet_since:
                self._quiet_since = now
            elif now - self._quiet_since >= 0.8:                  # a pause between sentences isn't the end
                self.restore()
                self._quiet_since = 0.0

    def run(self) -> None:
        try:
            self.audio._com()
        except Exception:
            pass
        while not self._stop.is_set():
            try:
                if getattr(constants, "MEDIA_DUCKING", True):
                    self.tick()
                elif self.saved:
                    self.restore()
            except Exception:
                pass
            self._stop.wait(0.12)
        if self.saved:
            self.restore(steps=1, step_s=0)

    def stop(self) -> None:
        self._stop.set()


# ─── the bridge ─────────────────────────────────────────────────────

class MediaBridge:
    def __init__(self, spotify_mod=None, win: Optional[WinMedia] = None, audio: Optional[Audio] = None,
                 fetch=None):
        if spotify_mod is None:
            from modules import spotify as spotify_mod
        self.sp = spotify_mod
        self.win = win or WinMedia()
        self.audio = audio or Audio()
        self.fetch = fetch or (lambda url: requests.get(url, timeout=5))
        self._state: Optional[Dict[str, Any]] = None
        self._lock = threading.Lock()
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []
        self._watch_until = 0.0
        self._thread: Optional[threading.Thread] = None
        self._wake = threading.Event()
        self._art_cache: Dict[str, str] = {}
        self._liked: Tuple[str, Optional[bool]] = ("", None)

    # ---- listeners / polling ----
    def add_listener(self, fn) -> None:
        self._listeners.append(fn)

    def watch(self, seconds: float = WATCH_S) -> None:
        self._watch_until = max(self._watch_until, time.time() + seconds)
        self._ensure_thread()
        self._wake.set()

    def _ensure_thread(self):
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, name="lumi-media", daemon=True)
            self._thread.start()

    def _loop(self):
        while True:
            if time.time() > self._watch_until:
                self._wake.wait(30)
                self._wake.clear()
                continue
            try:
                self.refresh()
            except Exception:
                pass
            st = self._state or {}
            delay = POLL_GAMING_S if _gaming() else (POLL_PLAYING_S if st.get("playing") else POLL_IDLE_S)
            self._wake.wait(delay)
            self._wake.clear()

    def state(self, watch: bool = True) -> Dict[str, Any]:
        if watch:
            self.watch()
        if self._state is None:
            self.refresh()
        return dict(self._state or self._empty())

    def _empty(self) -> Dict[str, Any]:
        return {"source": None, "title": "", "artists": "", "album": "", "art": "", "playing": False,
                "progress_ms": 0, "duration_ms": 0, "at": time.time(), "spotify": self._sp_status(),
                "gaming": _gaming()}

    def _sp_status(self) -> Dict[str, Any]:
        try:
            st = self.sp.status()
        except Exception:
            st = {}
        return {"connected": bool(st.get("connected")), "premium": st.get("premium"),   # True / False / None (not known yet)
                "needs_reconnect": bool(st.get("needs_reconnect"))}

    # ---- building the state ----
    def refresh(self) -> Dict[str, Any]:
        sp_st = self._sp_status()
        sp_np = None
        if sp_st["connected"]:
            try:
                sp_np = self.sp.now_playing()
            except Exception:
                sp_np = None
        st = self._from_spotify(sp_np) if sp_np else self._empty()   # Spotify only: other apps are ignored
        st["spotify"] = sp_st
        st["gaming"] = _gaming()
        st["at"] = time.time()
        st["volume"] = self._volume(st)
        st["can"] = self._caps(st)
        changed = self._changed(self._state, st)
        self._state = st
        if changed:
            for fn in list(self._listeners):
                try:
                    fn(dict(st))
                except Exception:
                    pass
        return st

    @staticmethod
    def _changed(old, new) -> bool:
        if old is None:
            return True
        keys = ("source", "key", "playing", "shuffle", "repeat", "liked", "volume", "art", "device", "title")
        if any(old.get(k) != new.get(k) for k in keys):
            return True
        # A seek or a drift of more than 3 s is worth a push; normal progress is drawn by the UI.
        expected = old.get("progress_ms", 0) + ((new["at"] - old.get("at", new["at"])) * 1000 if old.get("playing") else 0)
        return abs(new.get("progress_ms", 0) - expected) > 3000

    def _from_spotify(self, np: Dict[str, Any]) -> Dict[str, Any]:
        tid = np.get("id") or ""
        if np.get("type") == "track" and tid and self._liked[0] != tid:
            try:
                self._liked = (tid, self.sp.is_liked(tid))
            except Exception:
                self._liked = (tid, None)
        return {"source": "spotify", "app": "Spotify", "title": np.get("title", ""), "artists": np.get("artists", ""),
                "album": np.get("album", ""), "art": self._art(np.get("art") or ""), "playing": bool(np.get("playing")),
                "progress_ms": np.get("progress_ms") or 0, "duration_ms": np.get("duration_ms") or 0,
                "shuffle": bool(np.get("shuffle")), "repeat": np.get("repeat") or "off",
                "liked": self._liked[1] if self._liked[0] == tid else None, "id": tid, "uri": np.get("uri", ""),
                "device": np.get("device", ""), "device_type": np.get("device_type", ""),
                "sp_volume": np.get("volume"), "key": np.get("uri") or np.get("title", ""),
                "can_next": True, "can_prev": True, "can_shuffle": True, "can_repeat": True}

    def _art(self, url: str) -> str:
        """Spotify art as a data URL (the island only loads images from itself)."""
        if not url.startswith("https://"):
            return ""
        if url in self._art_cache:
            return self._art_cache[url]
        try:
            r = self.fetch(url)
            body = r.content if r.status_code == 200 else b""
        except Exception:
            body = b""
        data = ""
        if body and len(body) <= ART_MAX_BYTES:
            mime = "image/png" if body[:4] == b"\x89PNG" else "image/jpeg"
            data = f"data:{mime};base64," + base64.b64encode(body).decode()
        if len(self._art_cache) > 16:
            self._art_cache.clear()
        self._art_cache[url] = data
        return data

    def _spotify_here(self, st) -> bool:
        return st.get("source") == "spotify" and st.get("device_type", "").lower() == "computer" and \
            self._safe(lambda: self.audio.app_volume("spotify.exe")) is not None

    @staticmethod
    def _safe(fn):
        try:
            return fn()
        except Exception:
            return None

    def _volume(self, st) -> Optional[int]:
        if st.get("source") == "spotify":
            if self._spotify_here(st):
                v = self._safe(lambda: self.audio.app_volume("spotify.exe"))
                return round(v * 100) if v is not None else None
            return st.get("sp_volume")
        return None                                         # nothing on Spotify: no volume to show

    def _caps(self, st) -> Dict[str, bool]:
        sp = st.get("source") == "spotify"
        conn = bool((st.get("spotify") or {}).get("connected"))
        premium = (st.get("spotify") or {}).get("premium")       # True, False, or None (not known yet)
        api_ok = conn and premium is not False
        return {"control": bool(st.get("source")), "like": sp and st.get("liked") is not None,
                "library": conn, "devices": conn,
                # Premium-only Web API calls; on Free the Spotify app's own controls stand in where they can.
                "queue": api_ok, "play_track": api_ok,
                "seek": sp, "shuffle": sp, "repeat": sp, "next": sp, "prev": sp,
                "volume": st.get("volume") is not None, "premium": premium}

    # ---- commands ----
    def command(self, action: str, value: Any = None) -> Tuple[str, Dict[str, Any]]:
        st = self._state or self.refresh()
        src = st.get("source")
        action = str(action or "")
        msg = ""
        if action == "volume":
            msg = self._set_volume(st, value)
        elif action in ("play_uri", "queue_uri", "transfer", "play_liked", "play_in_context", "like_uri"):
            msg = self._spotify_only(action, value)
        elif src == "spotify":
            msg = self._spotify_cmd(st, action, value)
        else:
            raise ValueError("Nothing is playing on Spotify.")
        time.sleep(0.35)                                          # let the app report the change
        return msg, self.refresh()

    TRANSPORT = ("toggle", "play", "pause", "next", "previous", "shuffle", "repeat", "seek")

    def _spotify_cmd(self, st, action, value=None):
        """Premium first (the Web API). If Spotify says the account isn't Premium, the
        Spotify app's own Windows media controls on this PC stand in."""
        if action in self.TRANSPORT:
            if (st.get("spotify") or {}).get("premium") is not False:
                try:
                    return self._api_transport(st, action, value)
                except self._premium_required():
                    pass                                      # learned: Free. Fall through to the app.
            if self.win.command(action, prefer="spotify", value=self._win_value(st, action, value)):
                return ""
            raise ValueError("That needs Spotify Premium, and the Spotify app on this PC didn't take it "
                             "either. Open the Spotify app and try again.")
        if action == "like":
            on = not st.get("liked")
            msg = self.sp.set_liked(st.get("id"), on)
            self._liked = (st.get("id"), on)
            return msg
        raise ValueError(f"Unknown action {action!r}.")

    def _premium_required(self):
        return getattr(self.sp, "PremiumRequired", None) or type("NeverRaised", (Exception,), {})

    def _api_transport(self, st, action, value):
        if action == "toggle":
            return self.sp.control("pause" if st.get("playing") else "play")
        if action in ("play", "pause", "next", "previous"):
            return self.sp.control(action)
        if action == "shuffle":
            return self.sp.control("shuffle", (not st.get("shuffle")) if value is None else bool(value))
        if action == "repeat":
            order = self.sp.REPEAT
            if value is None:
                value = order[(order.index(st.get("repeat") or "off") + 1) % len(order)]
            return self.sp.set_repeat(str(value))
        return self.sp.seek(self._seek_ms(st, value))

    def _seek_ms(self, st, value) -> int:
        try:
            ms = int(float(value))
        except (TypeError, ValueError):
            raise ValueError("Seek needs a position in milliseconds.")
        dur = st.get("duration_ms") or 0
        return max(0, min(ms, dur - 500)) if dur else max(0, ms)

    def _win_value(self, st, action, value):
        if action == "seek":
            return self._seek_ms(st, value)
        if action == "shuffle" and value is not None:
            return bool(value)
        if action == "repeat" and value is not None:
            return str(value)
        return None

    def _spotify_only(self, action, value):
        if not self._sp_status()["connected"]:
            raise ValueError("Connect Spotify in Settings, then Connections.")
        if action == "play_in_context":
            v = value if isinstance(value, dict) else {}
            return self.sp.play_in_context(str(v.get("context_uri") or ""), str(v.get("track_uri") or ""),
                                           v.get("uris") if isinstance(v.get("uris"), list) else None)
        if action == "like_uri":
            v = value if isinstance(value, dict) else {}
            msg = self.sp.set_liked(str(v.get("uri") or ""), bool(v.get("on")))
            cur = self._state or {}
            if cur.get("uri") and cur.get("uri") == v.get("uri"):
                self._liked = (cur.get("id"), bool(v.get("on")))      # the player's heart follows a row's heart
            return msg
        if action == "play_uri":
            return self.sp.play_uri(str(value or ""))
        if action == "queue_uri":
            return self.sp.add_to_queue(str(value or ""))
        if action == "play_liked":
            return self.sp.play_uris([r["uri"] for r in self.sp.library(50).get("liked") or []])
        return self.sp.transfer(str(value or ""))

    def _set_volume(self, st, value) -> str:
        cur = st.get("volume")
        v = str(value if value is not None else "")
        if v.startswith(("+", "-")) and cur is not None:
            target = cur + int(v)
        else:
            try:
                target = int(float(v))
            except ValueError:
                raise ValueError("Volume is a number from 0 to 100, or +5 / -5.")
        target = max(0, min(100, target))
        if st.get("source") != "spotify":
            raise ValueError("Nothing is playing on Spotify.")
        if not self._spotify_here(st):
            return self.sp.control("volume", target)          # Spotify on another device: its own volume
        self.audio.set_app_volume("spotify.exe", target / 100)  # the Spotify app on this PC, not the system
        return f"Volume {target}%."

    # ---- library / queue ----
    def library(self, query: str = "") -> Dict[str, Any]:
        if not self._sp_status()["connected"]:
            return {"connected": False}
        if (query or "").strip():
            return {"connected": True, "results": self.sp.search(query)}
        return {"connected": True, **self.sp.library()}

    def library_page(self, shelf: str, offset: int = 0, limit: int = 20) -> Dict[str, Any]:
        if not self._sp_status()["connected"]:
            return {"connected": False, "items": [], "total": 0, "offset": 0, "next_offset": None}
        return {"connected": True, "shelf": shelf, **self.sp.library_page(shelf, offset, limit)}

    def tracks(self, kind: str, item_id: str = "", offset: int = 0, limit: int = 50) -> Dict[str, Any]:
        if not self._sp_status()["connected"]:
            return {"connected": False, "items": [], "total": 0, "offset": 0, "next_offset": None}
        return {"connected": True, "kind": kind, "item_id": item_id, **self.sp.tracks(kind, item_id, offset, limit)}

    def up_next(self) -> Dict[str, Any]:
        if not self._sp_status()["connected"]:
            return {"connected": False, "queue": [], "devices": []}
        out: Dict[str, Any] = {"connected": True}
        try:
            out.update(self.sp.queue())
        except Exception as e:
            out.update(current=None, queue=[], queue_error=str(e))
        try:
            out["devices"] = self.sp.devices()
        except Exception:
            out["devices"] = []
        return out


_bridge: Optional[MediaBridge] = None
_ducker: Optional[Ducker] = None


def bridge() -> MediaBridge:
    global _bridge
    if _bridge is None:
        _bridge = MediaBridge()
    return _bridge


def start(signals) -> None:
    """main.py: start voice ducking (the bridge itself starts on first use)."""
    global _ducker
    if _ducker is None and getattr(constants, "MEDIA_DUCKING", True):
        _ducker = Ducker(signals)
        threading.Thread(target=_ducker.run, name="lumi-ducking", daemon=True).start()


# ─── HTTP (web sidebar player) ──────────────────────────────────────

def register(router, server) -> None:
    from aiohttp import web
    from modules.socketIO import _json_error

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

    async def run(fn):
        try:
            return web.json_response(await asyncio.get_running_loop().run_in_executor(None, fn),
                                     headers={"Cache-Control": "no-store"})
        except ValueError as e:
            return _json_error(400, str(e))
        except Exception as e:
            return _json_error(502, str(e) or type(e).__name__)

    async def get_state(request):
        err, _ = await gate(request)
        return err or await run(lambda: bridge().state())

    async def post_cmd(request):
        err, body = await gate(request, True)
        if err:
            return err
        return await run(lambda: dict(zip(("message", "state"), bridge().command(
            str(body.get("action") or ""), body.get("value")))))

    def _int(request, name, default):
        try:
            return int(request.query.get(name, default))
        except ValueError:
            return default

    async def get_library(request):
        err, _ = await gate(request)
        if err:
            return err
        shelf = request.query.get("shelf", "")
        if shelf:
            return await run(lambda: bridge().library_page(shelf, _int(request, "offset", 0), _int(request, "limit", 20)))
        return await run(lambda: bridge().library(request.query.get("q", "")))

    async def get_tracks(request):
        err, _ = await gate(request)
        if err:
            return err
        return await run(lambda: bridge().tracks(request.query.get("kind", ""), request.query.get("id", ""),
                                                 _int(request, "offset", 0), _int(request, "limit", 50)))

    router.add_get("/api/media/state", get_state)
    router.add_post("/api/media/command", post_cmd)
    router.add_get("/api/media/library", get_library)          # ?shelf=playlists&offset=0, or ?q=search
    router.add_get("/api/media/tracks", get_tracks)            # ?kind=playlist|album|liked&id=…&offset=0

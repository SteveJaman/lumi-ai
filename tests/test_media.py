"""Media controller: modules/media_bridge.py (Spotify + Windows media + volume),
the island's Media messages and tab.
Every backend is faked: no audio, Spotify or Ollama is touched."""

import os
import types

import pytest

from modules import media_bridge as mb
from tests.test_island_bridge import Env, recv, run

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ─── fakes ──────────────────────────────────────────────────────

class FakeSpotify:
    REPEAT = ("off", "context", "track")

    def __init__(self, connected=True, premium=True, np=None):
        self.st = {"connected": connected, "premium": premium, "needs_reconnect": False}
        self.np = np
        self.calls = []
        self.liked = False

    def status(self): return dict(self.st)
    def now_playing(self): return dict(self.np) if self.np else None
    def is_liked(self, tid): self.calls.append(("is_liked", tid)); return self.liked
    def control(self, a, v=None): self.calls.append(("control", a, v)); return f"did {a}"
    def set_repeat(self, s): self.calls.append(("repeat", s)); return f"repeat {s}"
    def set_liked(self, tid, on): self.calls.append(("like", tid, on)); self.liked = on; return "liked" if on else "unliked"
    def play_uri(self, u): self.calls.append(("play_uri", u)); return "Playing."
    def add_to_queue(self, u): self.calls.append(("queue", u)); return "Added to the queue."
    def transfer(self, d): self.calls.append(("transfer", d)); return "Switched device."
    def library(self, n=20): return {"playlists": [], "liked": [{"uri": "spotify:track:L1"}], "recent": [], "shows": []}
    def play_uris(self, uris): self.calls.append(("play_uris", uris)); return "Playing."
    def search(self, q, kinds=("track",), limit=6): return [{"uri": "spotify:track:S1", "title": "Snow halation",
                                                              "subtitle": "μ's", "kind": "track"}]
    def queue(self): return {"current": None, "queue": [{"uri": "spotify:track:Q1", "title": "Next one"}]}
    def devices(self): return [{"id": "pc", "name": "DESKTOP", "type": "Computer", "active": True}]


class FakeWin:
    def __init__(self, st=None, ok=True):
        self.st, self.ok, self.calls = st, ok, []
    def state(self): return dict(self.st) if self.st else None
    def command(self, action, prefer="", value=None):
        self.calls.append((action, prefer) if value is None else (action, prefer, value))
        return self.ok


class Sess:
    def __init__(self, name, pid, vol=1.0, active=1):
        self.Process = types.SimpleNamespace(name=lambda n=name: n, pid=pid)
        self.State = active
        self.vol = vol
        self.SimpleAudioVolume = types.SimpleNamespace(GetMasterVolume=lambda s=self: s.vol,
                                                       SetMasterVolume=lambda v, ctx, s=self: setattr(s, "vol", v))


class FakeAudio:
    def __init__(self, sessions=(), master=0.5):
        self._s, self.m = list(sessions), master
    def _com(self): pass
    def sessions(self): return self._s
    def app_volume(self, p):
        return next((s.vol for s in self._s if s.Process.name().lower() == p), None)
    def set_app_volume(self, p, lvl):
        for s in self._s:
            if s.Process.name().lower() == p:
                s.vol = lvl
        return True
    def master(self): return self.m
    def set_master(self, v): self.m = v


SP_NP = {"title": "Idol", "artists": "YOASOBI", "album": "Idol", "art": "https://i.scdn.co/a.jpg", "playing": True,
         "progress_ms": 1000, "duration_ms": 200000, "shuffle": False, "repeat": "off", "device": "DESKTOP",
         "device_type": "Computer", "id": "T1", "uri": "spotify:track:T1", "type": "track", "volume": 40}
SYS_NP = {"source": "system", "app": "Firefox", "title": "Lofi stream", "artists": "Lofi Girl", "playing": True,
          "progress_ms": 0, "duration_ms": 0, "key": "Lofi stream|Lofi Girl", "art": "", "can_next": False,
          "can_prev": False, "can_shuffle": False, "can_repeat": False}


def fetch_ok(url):
    return types.SimpleNamespace(status_code=200, content=b"\xff\xd8\xff\xe0jpegdata")


def make(sp=None, win=None, audio=None):
    return mb.MediaBridge(spotify_mod=sp or FakeSpotify(np=SP_NP), win=win or FakeWin(),
                          audio=audio or FakeAudio([Sess("Spotify.exe", 10, 0.6)]), fetch=fetch_ok)


# ─── state ──────────────────────────────────────────────────────

def test_playing_spotify_wins_with_art_liked_and_app_volume():
    b = make()
    st = b.refresh()
    assert st["source"] == "spotify" and st["title"] == "Idol" and st["art"].startswith("data:image/jpeg;base64,")
    assert st["liked"] is False and st["volume"] == 60                       # Spotify.exe's own volume
    assert st["can"]["like"] and st["can"]["queue"] and st["can"]["volume"]


def test_other_apps_are_ignored_spotify_only():
    # YouTube/Firefox playing while Spotify is paused: the tab still shows Spotify.
    b = make(sp=FakeSpotify(np={**SP_NP, "playing": False}), win=FakeWin(SYS_NP), audio=FakeAudio(master=0.3))
    st = b.refresh()
    assert st["source"] == "spotify" and st["title"] == "Idol" and st["playing"] is False
    # Nothing on Spotify: nothing at all, even with a browser playing; no system volume either.
    b = make(sp=FakeSpotify(np=None), win=FakeWin(SYS_NP), audio=FakeAudio(master=0.3))
    st = b.refresh()
    assert st["source"] is None and st["volume"] is None and st["can"]["control"] is False


def test_nothing_playing():
    b = make(sp=FakeSpotify(connected=False), win=FakeWin(None))
    st = b.refresh()
    assert st["source"] is None and st["spotify"]["connected"] is False and st["can"]["control"] is False


def test_listeners_fire_on_change_only():
    b = make()
    seen = []
    b.add_listener(seen.append)
    b.refresh()
    b.refresh()                                                             # nothing changed
    assert len(seen) == 1
    b.sp.np = {**SP_NP, "playing": False}
    b.refresh()
    assert len(seen) == 2 and seen[-1]["playing"] is False


def test_art_is_fetched_once_per_url_and_bad_art_is_dropped():
    calls = []
    b = make()
    b.fetch = lambda url: (calls.append(url), fetch_ok(url))[1]
    b.refresh(); b.refresh()
    assert len(calls) == 1
    assert b._art("http://insecure/a.jpg") == ""


# ─── commands ───────────────────────────────────────────────────

def test_premium_spotify_commands_use_the_web_api(monkeypatch):
    monkeypatch.setattr(mb.time, "sleep", lambda s: None)
    sp = FakeSpotify(np=SP_NP)
    b = make(sp=sp)
    b.refresh()
    b.command("toggle"); b.command("next"); b.command("shuffle"); b.command("repeat")
    msg, st = b.command("like")
    assert ("control", "pause", None) in sp.calls and ("control", "next", None) in sp.calls
    assert ("control", "shuffle", True) in sp.calls and ("repeat", "context") in sp.calls
    assert ("like", "T1", True) in sp.calls and st["liked"] is True


def test_free_spotify_uses_the_apps_windows_controls(monkeypatch):
    monkeypatch.setattr(mb.time, "sleep", lambda s: None)
    sp, win = FakeSpotify(premium=False, np=SP_NP), FakeWin()
    b = make(sp=sp, win=win)
    b.refresh()
    b.command("next")
    assert win.calls == [("next", "spotify")] and not [c for c in sp.calls if c[0] == "control"]
    win.ok = False
    with pytest.raises(ValueError, match="Premium"):
        b.command("next")


def test_without_spotify_nothing_is_controlled(monkeypatch):
    monkeypatch.setattr(mb.time, "sleep", lambda s: None)
    win, audio = FakeWin(SYS_NP), FakeAudio(master=0.5)
    b = make(sp=FakeSpotify(connected=False), win=win, audio=audio)
    b.refresh()
    for action, value in (("toggle", None), ("next", None), ("like", None), ("volume", "+10")):
        with pytest.raises(ValueError, match="Spotify"):
            b.command(action, value)
    assert win.calls == [] and audio.m == 0.5                       # the browser and system volume untouched
    with pytest.raises(ValueError):
        make().command("volume", "loud")


def test_spotify_volume_on_this_pc_is_the_apps_own(monkeypatch):
    monkeypatch.setattr(mb.time, "sleep", lambda s: None)
    audio = FakeAudio([Sess("Spotify.exe", 10, 0.6)])
    sp = FakeSpotify(np=SP_NP)
    b = make(sp=sp, audio=audio)
    b.refresh()
    b.command("volume", "-10")
    assert audio._s[0].vol == pytest.approx(0.5) and not [c for c in sp.calls if c[:2] == ("control", "volume")]
    b.sp.np = {**SP_NP, "device_type": "Smartphone"}                      # playing on the phone
    b.refresh()
    b.command("volume", 70)
    assert ("control", "volume", 70) in sp.calls


def test_library_queue_and_spotify_only_actions(monkeypatch):
    monkeypatch.setattr(mb.time, "sleep", lambda s: None)
    sp = FakeSpotify(np=SP_NP)
    b = make(sp=sp)
    b.refresh()
    assert b.library("snow")["results"][0]["uri"] == "spotify:track:S1"
    assert b.up_next()["queue"][0]["title"] == "Next one" and b.up_next()["devices"][0]["name"] == "DESKTOP"
    b.command("play_uri", "spotify:playlist:9"); b.command("queue_uri", "spotify:track:2")
    b.command("transfer", "pc"); b.command("play_liked")
    assert ("play_uri", "spotify:playlist:9") in sp.calls and ("queue", "spotify:track:2") in sp.calls
    assert ("transfer", "pc") in sp.calls and ("play_uris", ["spotify:track:L1"]) in sp.calls
    off = make(sp=FakeSpotify(connected=False))
    assert off.library() == {"connected": False}
    with pytest.raises(ValueError, match="Connect Spotify"):
        off.command("play_uri", "spotify:track:1")


# ─── island ─────────────────────────────────────────────────────

async def reply(ws, kind, rid):
    """The answer to request `rid` (pushes of the same type are skipped)."""
    while True:
        m = await recv(ws, kind, timeout=5)
        if m.get("id") == rid:
            return m


def test_island_media_messages(monkeypatch):
    monkeypatch.setattr(mb.time, "sleep", lambda s: None)
    fake = make()
    monkeypatch.setattr(mb, "bridge", lambda: fake)

    async def t():
        async with Env() as e:
            ws = await e.connect(); await recv(ws, "hello_ok")
            await ws.send_json({"type": "media_get", "id": 1})
            r = await reply(ws, "media_state", 1)
            assert r["ok"] and r["state"]["title"] == "Idol"
            await ws.send_json({"type": "media_cmd", "id": 2, "action": "like"})
            r = await reply(ws, "media_state", 2)
            assert r["state"]["liked"] is True and r["message"] == "liked"
            await ws.send_json({"type": "media_cmd", "id": 3, "action": "volume", "value": "loud"})
            r = await reply(ws, "media_state", 3)
            assert r["ok"] is False and "Volume" in r["error"]
            await ws.send_json({"type": "media_next", "id": 4})
            r = await reply(ws, "media_next", 4)
            assert r["queue"][0]["title"] == "Next one"
            await ws.send_json({"type": "media_library", "id": 5, "q": "snow"})
            r = await reply(ws, "media_library", 5)
            assert r["results"][0]["title"] == "Snow halation"
    run(t())


def test_island_has_the_media_tab_and_gaming_lite_mode():
    html = open(os.path.join(ROOT, "island", "src", "index.html"), encoding="utf-8").read()
    js = open(os.path.join(ROOT, "island", "src", "island.js"), encoding="utf-8").read()
    css = open(os.path.join(ROOT, "island", "src", "island.css"), encoding="utf-8").read()
    assert 'data-app="media"' in html and 'id="app-media"' in html
    for msg in ("'media_get'", "'media_cmd'", "'media_library'", "'media_next'", "case 'media_state'"):
        assert msg in js
    assert ".island[data-gaming=\"true\"] .mx-bg { display: none; }" in css
    assert "island.addEventListener('wheel'" in js                      # the volume crown

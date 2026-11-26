"""Phase 1 of the mini Spotify view: paged library shelves, track lists
(playlists through GET /playlists/{id}/items, albums, Liked Songs), liking any
row through /me/library, seek / shuffle / repeat / play-from-a-row, and
learning the plan from what Spotify allows. Spotify is faked."""

import asyncio
import json
import types

import pytest

from modules import media_bridge as mb
from modules import spotify as sp
from tests.test_media import FakeAudio, FakeWin, Sess, SP_NP, fetch_ok
from tests.test_spotify_profile import Resp, connect, spot  # noqa: F401  (spot is a fixture)
from tests.test_island_bridge import Env, recv, run


def track(n, liked_uri=None, **kw):
    t = {"uri": f"spotify:track:t{n}", "id": f"t{n}", "name": f"Song {n}", "type": "track", "duration_ms": 1000 * n,
         "artists": [{"name": "Artist"}], "album": {"name": "Album", "images": [{"url": "https://i/a.jpg", "width": 300}]}}
    t.update(kw)
    return t


# ─── track lists ────────────────────────────────────────────────

def test_playlist_items_endpoint_rows_numbers_and_hearts(spot):
    connect()
    spot.script += [
        Resp(200, {"items": [{"item": track(1), "is_local": False},
                             {"item": None, "is_local": False},                      # a removed song
                             {"track": track(2), "is_local": False},                 # the deprecated key
                             {"item": {"uri": "spotify:local:x", "name": "Local file", "type": "track"}, "is_local": True}],
                   "total": 54, "next": "https://api.spotify.com/v1/…"}),
        Resp(200, [True, False]),                                                    # /me/library/contains
    ]
    page = sp.playlist_tracks("spotify:playlist:abc123", offset=10, limit=4)
    call = spot.calls["api"][-2]
    assert call[:2] == ("GET", "/playlists/abc123/items") and call[2]["offset"] == 10
    assert spot.calls["api"][-1][:3] == ("GET", "/me/library/contains", {"uris": "spotify:track:t1,spotify:track:t2"})
    rows = page["items"]
    assert [r["number"] for r in rows] == [11, 13, 14]
    assert [r["is_liked"] for r in rows] == [True, False, None]
    assert rows[2]["playable"] is False and rows[0]["art_url"] == "https://i/a.jpg"
    assert rows[0] == {**rows[0], "title": "Song 1", "artist": "Artist", "album": "Album", "duration_ms": 1000}
    assert page["total"] == 54 and page["next_offset"] == 14


def test_playlists_you_dont_own_get_a_clear_message(spot):
    connect()
    spot.script.append(Resp(403, {"error": {"status": 403, "message": "Forbidden"}}))
    with pytest.raises(sp.SpotifyError, match="own or collaborate on"):
        sp.playlist_tracks("abc")
    with pytest.raises(ValueError):
        sp.playlist_tracks("../me")


def test_liked_and_album_tracks(spot):
    connect()
    spot.script.append(Resp(200, {"items": [{"track": track(1)}, {"track": track(2)}], "total": 1635, "next": "x"}))
    lk = sp.liked_tracks(0, 2)
    assert [r["is_liked"] for r in lk["items"]] == [True, True] and lk["next_offset"] == 2
    spot.script += [Resp(200, {"name": "Album X", "images": [{"url": "https://i/x.jpg", "width": 300}]}),
                    Resp(200, {"items": [track(1, album=None)], "total": 1, "next": None}),
                    Resp(200, [False])]
    al = sp.album_tracks("alb1")
    assert al["items"][0]["album"] == "Album X" and al["items"][0]["art_url"] == "https://i/x.jpg"
    assert al["next_offset"] is None
    with pytest.raises(ValueError):
        sp.tracks("artist", "x")


def test_library_pages_fill_up_and_mark_browsable_playlists(spot):
    connect()
    data = json.load(open(sp.PATH)); data["user"]["id"] = "me"; json.dump(data, open(sp.PATH, "w"))
    pl = lambda i, owner, collab=False: {"uri": f"spotify:playlist:p{i}", "id": f"p{i}", "name": f"P{i}",
                                         "owner": {"id": owner}, "collaborative": collab}
    spot.script += [Resp(200, {"items": [pl(1, "me"), pl(2, "spotify")], "total": 38, "next": "x"}),
                    Resp(200, {"items": [pl(3, "friend", True)], "total": 38, "next": "x"}),
                    Resp(200, {"items": [pl(4, "me")], "total": 38, "next": "x"})]
    page = sp.library_page("playlists", 0, 4)
    assert [p["browsable"] for p in page["items"]] == [True, False, True, True]
    assert page["next_offset"] == 4 and len(spot.calls["api"]) >= 4
    offsets = [c[2]["offset"] for c in spot.calls["api"] if c[1] == "/me/playlists"]
    assert offsets == [0, 2, 3]                       # kept asking where Spotify left off
    spot.script.append(Resp(200, {"items": [{"album": {"uri": "spotify:album:a", "name": "A", "artists": []}}],
                                  "total": 58, "next": None}))
    assert sp.library_page("albums")["items"][0]["kind"] == "album"
    with pytest.raises(ValueError):
        sp.library_page("artists")


# ─── liking any row ─────────────────────────────────────────────

def test_liking_uses_the_library_endpoints_with_uris(spot):
    connect()
    spot.script.append(Resp(200, None, text=""))
    assert sp.set_liked("spotify:track:t9", True) == "Saved to your Liked Songs."
    assert spot.calls["api"][-1][:3] == ("PUT", "/me/library", {"uris": "spotify:track:t9"})
    spot.script.append(Resp(200, [True]))
    assert sp.is_liked("t9") is True                                         # a bare id becomes a URI
    with pytest.raises(ValueError):
        sp.set_liked("spotify:playlist:x", True)
    uris = [f"spotify:track:{i}" for i in range(45)]
    spot.script += [Resp(200, [True] * 40), Resp(200, [False] * 5)]
    assert sp.liked_many(uris) == [True] * 40 + [False] * 5                   # 40 per call, Spotify's max
    assert [c[1] for c in spot.calls["api"][-2:]] == ["/me/library/contains"] * 2


# ─── player controls and the plan ───────────────────────────────

def test_seek_and_play_from_a_row(spot):
    connect()
    spot.script.append(Resp(204))
    assert sp.seek(65_000) == "Jumped to 1:05."
    assert spot.calls["api"][-1][:3] == ("PUT", "/me/player/seek", {"position_ms": 65000})
    spot.script.append(Resp(204))
    sp.play_in_context("spotify:playlist:p1", "spotify:track:t3")
    assert spot.calls["api"][-1][3] == {"context_uri": "spotify:playlist:p1", "offset": {"uri": "spotify:track:t3"}}
    spot.script.append(Resp(204))
    sp.play_in_context("", "spotify:track:t2", ["spotify:track:t1", "spotify:track:t2"])   # Liked Songs page
    assert spot.calls["api"][-1][3] == {"uris": ["spotify:track:t1", "spotify:track:t2"], "offset": {"position": 1}}


def test_the_plan_is_learned_from_what_spotify_allows(spot):
    connect()
    data = json.load(open(sp.PATH)); data["user"]["product"] = None; json.dump(data, open(sp.PATH, "w"))
    sp._plan.update(premium=None, loaded=False)
    assert sp.status()["premium"] is None                  # Dev Mode apps no longer get "product"
    spot.script.append(Resp(204))
    sp.control("pause")
    assert sp.status()["premium"] is True                  # a Premium-only call worked
    spot.script.append(Resp(403, text='{"error":{"reason":"PREMIUM_REQUIRED"}}'))
    with pytest.raises(sp.PremiumRequired):
        sp.seek(1)
    assert sp.status()["premium"] is False
    sp.disconnect()
    assert sp.status()["premium"] is None


# ─── the bridge ─────────────────────────────────────────────────

class LearningSpotify:
    """Premium unknown; the API either works or says PREMIUM_REQUIRED."""
    REPEAT = ("off", "context", "track")
    PremiumRequired = sp.PremiumRequired

    def __init__(self, premium_ok=True, premium=None):
        self.premium_ok, self.premium, self.calls = premium_ok, premium, []

    def status(self): return {"connected": True, "premium": self.premium, "needs_reconnect": False}
    def now_playing(self): return dict(SP_NP)
    def is_liked(self, tid): return False

    def _player(self, *call):
        self.calls.append(call)
        if not self.premium_ok:
            self.premium = False
            raise sp.PremiumRequired("That needs Spotify Premium.")
        self.premium = True
        return "ok"

    def control(self, a, v=None): return self._player("control", a, v)
    def seek(self, ms): return self._player("seek", ms)
    def set_repeat(self, s): return self._player("repeat", s)
    def set_liked(self, uri, on): self.calls.append(("like", uri, on)); return "liked" if on else "unliked"
    def play_in_context(self, c, t, uris=None): return self._player("ctx", c, t, uris)
    def library_page(self, shelf, offset, limit): return {"items": [shelf], "total": 1, "offset": offset, "next_offset": None}
    def tracks(self, kind, item_id, offset, limit): return {"items": [kind, item_id], "total": 2, "offset": offset, "next_offset": None}


def bridge_with(spm, win=None):
    return mb.MediaBridge(spotify_mod=spm, win=win or FakeWin(), audio=FakeAudio([Sess("Spotify.exe", 1, 0.5)]),
                          fetch=fetch_ok)


def test_unknown_plan_tries_premium_first(monkeypatch):
    monkeypatch.setattr(mb.time, "sleep", lambda s: None)
    spm, win = LearningSpotify(premium_ok=True), FakeWin()
    b = bridge_with(spm, win)
    b.refresh()
    b.command("seek", 999_999_999)
    b.command("shuffle", True)
    b.command("repeat", "track")
    assert ("seek", SP_NP["duration_ms"] - 500) in spm.calls                 # clamped to the song
    assert ("control", "shuffle", True) in spm.calls and ("repeat", "track") in spm.calls
    assert win.calls == []                                                    # Premium: no Windows fallback


def test_a_free_answer_falls_back_to_the_spotify_app(monkeypatch):
    monkeypatch.setattr(mb.time, "sleep", lambda s: None)
    spm, win = LearningSpotify(premium_ok=False), FakeWin()
    b = bridge_with(spm, win)
    b.refresh()
    b.command("seek", 30_000)
    assert spm.calls == [("seek", 30_000)] and win.calls == [("seek", "spotify", 30_000)]
    b.command("next")                                                          # now known Free: straight to the app
    assert win.calls[-1] == ("next", "spotify") and len(spm.calls) == 1
    st = b.refresh()
    assert st["can"]["queue"] is False and st["can"]["play_track"] is False and st["can"]["seek"] is True
    win.ok = False
    with pytest.raises(ValueError, match="Premium"):
        b.command("pause")


def test_row_actions_and_reads(monkeypatch):
    monkeypatch.setattr(mb.time, "sleep", lambda s: None)
    spm = LearningSpotify(premium=True)
    b = bridge_with(spm)
    b.refresh()
    msg, st = b.command("like_uri", {"uri": SP_NP["uri"], "on": True})
    assert msg == "liked" and st["liked"] is True                            # the player's heart follows the row
    b.command("play_in_context", {"context_uri": "spotify:playlist:p", "track_uri": "spotify:track:t2"})
    assert ("ctx", "spotify:playlist:p", "spotify:track:t2", None) in spm.calls
    assert b.library_page("albums", 20)["items"] == ["albums"]
    tr = b.tracks("playlist", "p1")
    assert tr["items"] == ["playlist", "p1"] and tr["item_id"] == "p1" and "id" not in tr   # "id" is the island request id


def test_island_media_tracks_and_paged_library(monkeypatch):
    monkeypatch.setattr(mb.time, "sleep", lambda s: None)
    fake = bridge_with(LearningSpotify(premium=True))
    monkeypatch.setattr(mb, "bridge", lambda: fake)

    async def reply(ws, kind, rid):
        while True:
            m = await recv(ws, kind, timeout=5)
            if m.get("id") == rid:
                return m

    async def t():
        async with Env() as e:
            ws = await e.connect(); await recv(ws, "hello_ok")
            await ws.send_json({"type": "media_tracks", "id": 1, "kind": "playlist", "item_id": "p1", "offset": 50})
            r = await reply(ws, "media_tracks", 1)
            assert r["ok"] and r["items"] == ["playlist", "p1"] and r["offset"] == 50
            await ws.send_json({"type": "media_library", "id": 2, "shelf": "albums", "offset": 20})
            r = await reply(ws, "media_library", 2)
            assert r["items"] == ["albums"] and r["shelf"] == "albums"
    run(t())


# ─── phase 2: the web player bar and library view ──────────────

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def test_web_has_the_player_bar_and_library_view():
    html, js, card = read("web", "index.html"), read("web", "sp-player.js"), read("web", "spotify.js")
    assert 'id="sp-dock"' in html and 'id="sp-full"' in html and 'id="sp-mini"' not in html
    assert "import * as player from './sp-player.js'" in card and "player.refresh()" in card
    for needle in ("/api/media/library?shelf=", "/api/media/tracks?", "'play_in_context'", "'like_uri'",
                   "act('seek'", "act('volume'", "can.play_track", "can.seek", "can.volume"):
        assert needle in js, needle
    # Text from Spotify only ever goes in through textContent; innerHTML is only our own icons.
    for rhs in re.findall(r"\.innerHTML\s*=\s*([^;]+);", js):
        assert re.fullmatch(r"(icon|ICON\.\w+|playing \? ICON\.pause : ICON\.play|volOk && now\.volume === 0 \? ICON\.mute : ICON\.vol)",
                            rhs.strip()), rhs
    css = read("web", "style.css")
    assert ".app.sp-on" in css and ".spx-row" in css and ".sp-mini" not in css


def test_playlist_song_counts_and_the_playing_context():
    assert sp._song_count({"items": {"total": 28}}) == 28            # the Feb 2026 name
    assert sp._song_count({"tracks": {"total": 7}}) == 7
    assert sp._song_count({}) == 0
    row = sp._item({"uri": "spotify:playlist:p", "name": "P", "owner": {"display_name": "Me"}, "items": {"total": 3}}, "playlist")
    assert row["subtitle"] == "Me · 3 songs"
    b = bridge_with(LearningSpotify(premium=True))
    assert "context_uri" in b.refresh()

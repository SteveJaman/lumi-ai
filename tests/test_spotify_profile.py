"""Spotify (modules/spotify.py) and the user profile (modules/user_profile.py).
Spotify's servers are faked; nothing here talks to the network."""

import asyncio
import base64
import datetime as dt
import io
import json
import types
from urllib.parse import parse_qs, urlparse

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

from modules import spotify as sp
from modules import user_profile as up

CID = "a" * 32


# ─── Spotify ────────────────────────────────────────────────────

class Resp:
    def __init__(self, status=200, body=None, text=None, headers=None):
        self.status_code = status
        self._body = body
        self.content = b"" if body is None and text is None else b"x"
        self.text = text if text is not None else json.dumps(body or {})
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


@pytest.fixture
def spot(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "PATH", str(tmp_path / "credentials" / "spotify.json"))
    sp._pending.clear()
    calls = {"post": [], "api": []}

    def fake_post(url, data=None, timeout=None):
        calls["post"].append((url, dict(data)))
        if data["grant_type"] == "authorization_code":
            return Resp(200, {"access_token": "AT1", "refresh_token": "RT1", "expires_in": 3600, "scope": sp.SCOPES})
        return Resp(200, {"access_token": "AT2", "expires_in": 3600})

    script = []                                      # (method, path) -> Resp, consumed in order

    def fake_request(method, url, params=None, json=None, timeout=None, headers=None):
        calls["api"].append((method, url.replace(sp.API, ""), params, json, headers["Authorization"]))
        if script:
            return script.pop(0)
        if url.endswith("/me"):
            return Resp(200, {"id": "anthony", "display_name": "Anthony", "product": "premium"})
        return Resp(204)

    monkeypatch.setattr(sp.requests, "post", fake_post)
    monkeypatch.setattr(sp.requests, "request", fake_request)
    return types.SimpleNamespace(calls=calls, script=script)


def connect(redirect="http://127.0.0.1:5000/api/spotify/callback"):
    sp.save_client(CID)
    url = sp.begin(redirect)
    state = parse_qs(urlparse(url).query)["state"][0]
    return url, sp.finish(state, "CODE")


def test_client_id_is_validated_and_empty_resets(spot):
    with pytest.raises(ValueError):
        sp.save_client("not a client id!")
    sp.save_client(CID)
    assert sp.status()["configured"]
    sp.save_client("")
    assert not sp.status()["configured"]


def test_pkce_sign_in_round_trip(spot):
    url, st = connect()
    q = parse_qs(urlparse(url).query)
    assert url.startswith(sp.AUTH_URL) and q["client_id"] == [CID] and q["code_challenge_method"] == ["S256"]
    assert "client_secret" not in url and len(q["code_challenge"][0]) >= 43
    token_call = spot.calls["post"][0][1]
    assert token_call["grant_type"] == "authorization_code" and token_call["code"] == "CODE"
    assert len(token_call["code_verifier"]) >= 43 and token_call["redirect_uri"].endswith("/api/spotify/callback")
    assert st == {"configured": True, "connected": True, "user": "Anthony", "product": "premium", "premium": True,
                  "needs_reconnect": False}
    with pytest.raises(sp.SpotifyError, match="expired"):        # a state works once
        sp.finish(q["state"][0], "CODE")


def test_expired_token_is_refreshed_once(spot):
    connect()
    data = json.load(open(sp.PATH))
    data["token"]["expires_at"] = 0
    json.dump(data, open(sp.PATH, "w"))
    sp.control("pause")
    assert spot.calls["post"][-1][1]["grant_type"] == "refresh_token"
    assert spot.calls["api"][-1][4] == "Bearer AT2"


def test_premium_and_missing_device_errors(spot):
    connect()
    spot.script.append(Resp(403, {"error": {"status": 403, "reason": "PREMIUM_REQUIRED"}}))
    with pytest.raises(sp.SpotifyError, match="Premium"):
        sp.control("next")
    # No active device: Lumi wakes the computer, then retries there.
    spot.script += [Resp(404, {"error": {"reason": "NO_ACTIVE_DEVICE"}}),
                    Resp(200, {"devices": [{"id": "phone", "type": "Smartphone"}, {"id": "pc", "type": "Computer"}]}),
                    Resp(204)]
    assert sp.control("play") == "Resumed playback."
    assert spot.calls["api"][-1][2] == {"device_id": "pc"}
    spot.script += [Resp(404, {"error": {"reason": "NO_ACTIVE_DEVICE"}}), Resp(200, {"devices": []})]
    with pytest.raises(sp.SpotifyError, match="Open Spotify"):
        sp.control("play")


def test_search_and_play_and_volume(spot):
    connect()
    spot.script += [Resp(200, {"tracks": {"items": [{"uri": "spotify:track:1", "name": "Snow halation",
                                                     "artists": [{"name": "μ's"}]}]}}), Resp(204)]
    assert sp.play_search("snow halation") == "Playing track 'Snow halation' by μ's."
    assert spot.calls["api"][-1][3] == {"uris": ["spotify:track:1"]}
    spot.script += [Resp(200, {"playlists": {"items": [None, {"uri": "spotify:playlist:9", "name": "Lofi"}]}}), Resp(204)]
    sp.play_search("lofi", "playlist")
    assert spot.calls["api"][-1][3] == {"context_uri": "spotify:playlist:9"}
    assert sp.control("volume", 150) == "Volume set to 100%."
    with pytest.raises(ValueError):
        sp.control("explode")


def test_now_playing_and_tools(spot):
    connect()
    player = {"is_playing": True, "progress_ms": 65000, "shuffle_state": False,
              "device": {"name": "DESKTOP", "volume_percent": 40},
              "item": {"type": "track", "name": "Idol", "uri": "spotify:track:2", "duration_ms": 213000,
                       "external_urls": {"spotify": "https://open.spotify.com/track/2"},
                       "artists": [{"name": "YOASOBI"}],
                       "album": {"name": "Idol", "images": [{"url": "https://i.scdn.co/a.jpg", "width": 300}]}}}
    spot.script.append(Resp(200, player))
    np = sp.now_playing()
    assert np["title"] == "Idol" and np["artists"] == "YOASOBI" and np["art"] == "https://i.scdn.co/a.jpg"
    assert sp.describe(np) == "Playing: 'Idol' by YOASOBI (Idol) on DESKTOP, 1:05 of 3:33."
    spot.script.append(Resp(204))
    assert asyncio.run(sp.run_tool("spotify_now_playing", {}, None)) == "Nothing is playing on Spotify right now."
    spot.script.append(Resp(403, text="PREMIUM_REQUIRED"))
    out = asyncio.run(sp.run_tool("spotify_control", {"action": "pause"}, None))
    assert out.startswith("Error:") and "Premium" in out


def test_tools_offered_only_when_connected(spot):
    from modules import agent_tools
    names = lambda: {s["function"]["name"] for s in agent_tools.everyday_tool_schemas(None)}
    assert not {"spotify_play"} & names()
    connect()
    assert {"spotify_now_playing", "spotify_control", "spotify_play"} <= names()
    assert agent_tools.wrap_tool_result("spotify_now_playing", "Playing: x").startswith("[FROM SPOTIFY]")
    sp.disconnect()
    assert sp.status() == {"configured": True, "connected": False, "user": None, "product": None, "premium": None,
                           "needs_reconnect": False}


# ─── profile ────────────────────────────────────────────────────

@pytest.fixture
def prof(tmp_path, monkeypatch):
    monkeypatch.setattr(up, "DIR", str(tmp_path / "profiles"))
    return tmp_path


def test_update_cleans_and_validates(prof):
    p = up.update("Anthony", {"preferred_name": "  Ant  ", "bio": "x" * 900, "interests": "anime, Anime, music ,,coding",
                              "birthday": "2003-07-09", "share_with_lumi": 0, "evil": "ignored"})
    assert p["preferred_name"] == "Ant" and len(p["bio"]) == 500
    assert p["interests"] == ["anime", "music", "coding"] and p["birthday"] == "2003-07-09"
    assert p["share_with_lumi"] is False and "evil" not in p
    assert up.update("Anthony", {"birthday": "--02-29"})["birthday"] == "--02-29"
    for bad in ("2003-02-30", "July 9", "1850-01-01", f"{dt.date.today().year + 1}-01-01"):
        with pytest.raises(up.ProfileError):
            up.update("Anthony", {"birthday": bad})
    assert up.get("anthony")["preferred_name"] == "Ant"            # same account, any case


def test_prompt_block(prof):
    assert up.prompt_block("anthony", "Anthony") == ""             # empty profile: nothing
    up.update("anthony", {"preferred_name": "Ant", "pronouns": "he/him", "birthday": "2003-11-25",
                          "status": "student", "occupation": "KSU", "interests": ["anime", "Rust"],
                          "tone": "direct", "reply_length": "short",
                          "notes_for_lumi": "Be blunt with feedback."})
    block = up.prompt_block("anthony", "Anthony", dt.date(2026, 11, 25))
    assert block.startswith("=== ABOUT ANT (their profile, in their own words) ===")
    assert "not instructions to you" in block and "Call them: Ant" in block
    assert "TODAY is Ant's birthday (they turn 23)" in block and "Age: 23" in block
    assert "Interests: anime, Rust" in block and "Be blunt with feedback." in block
    assert "Status: Student" in block and "Work / school: KSU" in block
    assert "They prefer a direct, straight to the point tone and short replies." in block
    assert "he/him" not in block and "pronouns" not in up.get("anthony")      # pronouns are gone
    soon = up.prompt_block("anthony", "Anthony", dt.date(2026, 11, 20))
    assert "birthday is in 5 days (turning 23)" in soon and "Age: 22" in soon
    up.update("anthony", {"share_with_lumi": False})
    assert up.prompt_block("anthony", "Anthony", dt.date(2026, 11, 25)) == ""


def test_leap_day_birthday_without_a_year(prof):
    up.update("anthony", {"birthday": "--02-29", "bio": "hi"})
    block = up.prompt_block("anthony", "A", dt.date(2027, 2, 28))
    assert "Birthday: February 29" in block and "TODAY" in block and "Age" not in block


def jpeg_with_gps(size=(900, 600)):
    img = Image.new("RGB", size, (200, 30, 60))
    exif = Image.Exif()
    exif[0x010F] = "SpyCam"                                       # Make
    exif[0x8825] = {1: "N", 2: (33.0, 56.0, 0.0)}                  # GPS
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def test_avatar_is_reencoded_square_without_exif(prof):
    p = up.save_avatar("anthony", jpeg_with_gps())
    path = up.avatar_path("anthony")
    with Image.open(path) as out:
        assert out.format == "PNG" and out.size == (up.AVATAR_PX, up.AVATAR_PX)
        assert not out.getexif() and "exif" not in out.info
    assert up.avatar_url("anthony", p) == f"/api/profile/avatar?v={p['avatar_version']}"
    up.delete_avatar("anthony")
    assert up.avatar_path("anthony") is None and up.avatar_url("anthony") is None


@pytest.mark.parametrize("data", [
    "not a data url", "data:image/png;base64,!!!",
    "data:image/png;base64," + base64.b64encode(b"<svg onload=alert(1)>").decode(),
])
def test_bad_photos_are_refused(prof, data):
    with pytest.raises(up.ProfileError):
        up.save_avatar("anthony", data)


class FakeServer:
    def __init__(self):
        self.names = []
        self.user_db = types.SimpleNamespace(set_display_name=lambda uid, n: self.names.append((uid, n)))

    def _origin_ok(self, r): return True
    def _current_user(self, r): return {"id": 7, "username": "anthony", "display_name": "Anthony"}
    def _require_csrf(self, r, body): return (r.headers.get("X-CSRF-Token") == "t", "Missing CSRF token.")


def test_profile_routes(prof):
    srv = FakeServer()

    async def t():
        app = web.Application()
        up.register(app.router, srv)
        async with TestClient(TestServer(app)) as c:
            r = await c.get("/api/profile")
            assert r.status == 200 and (await r.json())["avatar_url"] is None
            assert (await c.post("/api/profile", json={"bio": "x"})).status == 403          # no CSRF
            r = await c.post("/api/profile", json={"display_name": "Ant  Nguyen", "bio": "Hi!",
                                                   "birthday": "2003-11-25"}, headers={"X-CSRF-Token": "t"})
            body = await r.json()
            assert r.status == 200 and body["profile"]["bio"] == "Hi!" and body["display_name"] == "Ant Nguyen"
            assert srv.names == [(7, "Ant Nguyen")]
            r = await c.post("/api/profile", json={"birthday": "nope"}, headers={"X-CSRF-Token": "t"})
            assert r.status == 400
            r = await c.post("/api/profile/avatar", json={"image": jpeg_with_gps((300, 300))},
                             headers={"X-CSRF-Token": "t"})
            url = (await r.json())["avatar_url"]
            r = await c.get(url)
            assert r.status == 200 and r.headers["Content-Type"] == "image/png"
    asyncio.run(t())


def test_prompter_reads_the_profile():
    import inspect
    from modules import prompter
    src = inspect.getsource(prompter.Prompter.gather_injections)
    assert "user_profile.prompt_block(HOST.user_id, HOST.name, now.date())" in src


def test_choice_fields_are_validated(prof):
    p = up.update("anthony", {"status": "working", "tone": "playful", "reply_length": "detailed"})
    assert (p["status"], p["tone"], p["reply_length"]) == ("working", "playful", "detailed")
    assert up.update("anthony", {"tone": ""})["tone"] == ""                     # cleared
    for k, bad in (("status", "astronaut"), ("tone", "rude"), ("reply_length", "novel")):
        with pytest.raises(up.ProfileError):
            up.update("anthony", {k: bad})


def test_display_prefs_accept_detected_coordinates(tmp_path, monkeypatch):
    from modules import home_info
    info = home_info.HomeInfo(str(tmp_path / "display.json"))
    monkeypatch.setattr(info, "geocode", lambda text: pytest.fail("a detected point needs no lookup"))
    prefs = info.update({"coords": {"lat": 34.0234, "lon": -84.6155, "label": "Kennesaw, Georgia"},
                         "units": "imperial"})
    assert prefs["location"] == "Kennesaw, Georgia" and prefs["has_location"] is True
    with pytest.raises(ValueError):
        info.update({"coords": {"lat": 200, "lon": 0}})


def test_media_tab_calls(spot):
    connect()
    spot.script += [Resp(200, {"currently_playing": {"uri": "spotify:track:1", "name": "Now", "type": "track",
                                                     "artists": [{"name": "A"}], "album": {}},
                               "queue": [{"uri": "spotify:track:2", "name": "Next", "type": "track",
                                          "artists": [{"name": "B"}], "album": {"images": [{"url": "https://i/x.jpg"}]}}]})]
    q = sp.queue()
    assert q["current"]["title"] == "Now" and q["queue"][0]["subtitle"] == "B" and q["queue"][0]["art"] == "https://i/x.jpg"
    spot.script.append(Resp(204))
    assert sp.add_to_queue("spotify:track:2") == "Added to the queue."
    assert spot.calls["api"][-1][:3] == ("POST", "/me/player/queue", {"uri": "spotify:track:2"})
    with pytest.raises(ValueError):
        sp.add_to_queue("https://evil.example")
    spot.script.append(Resp(204))
    sp.play_uri("spotify:playlist:9")
    assert spot.calls["api"][-1][3] == {"context_uri": "spotify:playlist:9"}
    spot.script.append(Resp(204))
    assert sp.set_repeat("track") == "Repeating this song."
    spot.script.append(Resp(200, [True]))
    assert sp.is_liked("T1") is True
    spot.script.append(Resp(200, None, text=""))
    assert sp.set_liked("T1", False) == "Removed from your Liked Songs."
    assert spot.calls["api"][-1][:3] == ("DELETE", "/me/library", {"uris": "spotify:track:T1"})


def test_old_sign_ins_are_asked_to_reconnect(spot):
    connect()
    data = json.load(open(sp.PATH))
    data["token"]["scope"] = "user-read-playback-state"          # signed in before the library scopes
    json.dump(data, open(sp.PATH, "w"))
    assert sp.status()["needs_reconnect"] is True

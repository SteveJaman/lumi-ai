"""Tests for streaming + remote access: subtitles, OBS error handling,
public-origin checks, the remote registration lock, and the stream
routes. No OBS, cloudflared, or network required."""

import asyncio
import os
import time
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

import modules.socketIO as sio_mod
from modules.obs_control import OBSControl, OBSError
from modules.subtitles import (SubtitleWriter, chunk_subtitle,
                               clean_subtitle_text)
from modules.user_auth import UserDB

PUBLIC = "https://lumi.example.com"
LOCAL = "http://127.0.0.1:5000"


# ─────────────────────────────────────────────────────────────
# Subtitles
# ─────────────────────────────────────────────────────────────
def test_clean_strips_tags_and_actions():
    text = "[sighing] Oh. *rolls eyes* It's  you. [sarcastic] Great."
    assert clean_subtitle_text(text) == "Oh. It's you. Great."


def test_chunks_respect_limit_and_keep_every_word():
    text = ("This is a fairly long sentence that will certainly need to be "
            "split somewhere. Short one. And another reasonably long "
            "sentence follows it here.")
    chunks = chunk_subtitle(text, max_chars=40)
    assert all(len(c) <= 40 for c in chunks)
    assert " ".join(chunks).split() == text.split()


def test_short_sentences_merge():
    assert chunk_subtitle("Oh. It's you.", max_chars=90) == ["Oh. It's you."]


def test_overlong_word_is_its_own_chunk():
    word = "x" * 50
    assert chunk_subtitle(f"hi {word} there", max_chars=10) == \
        ["hi", word, "there"]


class _FakeObs:
    def __init__(self):
        self.sent = []

    def set_subtitle(self, text):
        self.sent.append(text)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_writer_plays_chunks_then_clears(tmp_path):
    path = str(tmp_path / "subs.txt")
    obs = _FakeObs()
    w = SubtitleWriter(obs=obs, path=path, enabled=True, hold_seconds=0.05)
    # show() floors duration at 0.5 s; plus the hold, done by ~0.55 s.
    w.show("[happy] First part here. Second part there.", duration=0.2)
    time.sleep(1.0)
    assert obs.sent[-1] == ""
    assert _read(path) == ""
    assert any("First part" in t for t in obs.sent)


def test_writer_new_line_cancels_old(tmp_path):
    path = str(tmp_path / "subs.txt")
    obs = _FakeObs()
    w = SubtitleWriter(obs=obs, path=path, enabled=True, hold_seconds=5)
    w.show("old line " * 30, duration=10)
    time.sleep(0.05)
    w.show("new line", duration=10)
    time.sleep(0.1)
    assert _read(path) == "new line"
    w.clear()
    assert _read(path) == ""


def test_writer_disabled_writes_nothing(tmp_path):
    path = str(tmp_path / "subs.txt")
    SubtitleWriter(path=path, enabled=False).show("hello", 1)
    assert not os.path.exists(path)


# ─────────────────────────────────────────────────────────────
# OBS control (no OBS running)
# ─────────────────────────────────────────────────────────────
def _closed_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_obs_unreachable_reports_instead_of_raising(monkeypatch):
    monkeypatch.setenv("OBS_HOST", "127.0.0.1")
    monkeypatch.setenv("OBS_PORT", str(_closed_port()))
    obs = OBSControl(SimpleNamespace())
    st = obs.status()
    assert st["connected"] is False
    assert "isn't reachable" in st["error"]
    with pytest.raises(OBSError):
        obs.switch_scene("Main")


def test_obs_subtitle_push_backs_off(monkeypatch):
    monkeypatch.setenv("OBS_PORT", str(_closed_port()))
    monkeypatch.setenv("OBS_SUBTITLE_SOURCE", "Subs")
    obs = OBSControl(SimpleNamespace())
    obs.status()  # fails and arms the backoff
    t = time.time()
    obs.set_subtitle("hello")  # must not pay the connect timeout again
    assert time.time() - t < 0.2


def test_obs_disabled():
    obs = OBSControl(SimpleNamespace(), enabled=False)
    assert obs.status()["connected"] is False
    obs.set_subtitle("ignored")
    with pytest.raises(OBSError, match="disabled"):
        obs.start_stream()


# ─────────────────────────────────────────────────────────────
# Public origins
# ─────────────────────────────────────────────────────────────
@pytest.fixture
def env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    monkeypatch.setattr(sio_mod, "_ENV_PATH", str(path))
    monkeypatch.setattr(sio_mod, "_public_origins_cache",
                        (object(), frozenset()))
    monkeypatch.delenv("LUMI_PUBLIC_ORIGINS", raising=False)

    def write(text):
        path.write_text(text, encoding="utf-8")
        # Force a distinct mtime even on coarse-resolution filesystems.
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    return write


def test_local_origins_always_allowed(env_file):
    assert sio_mod._origin_allowed(LOCAL)
    assert sio_mod._origin_allowed("http://localhost:5000")
    assert not sio_mod._origin_allowed(PUBLIC)
    assert not sio_mod._origin_allowed(None)


def test_public_origins_parse_and_reload(env_file):
    env_file(f"LUMI_PUBLIC_ORIGINS={PUBLIC}/, http://insecure.example.com,"
             f" https://a.example.com/path\n")
    assert sio_mod._public_origins() == frozenset({PUBLIC})
    assert sio_mod._origin_allowed("https://LUMI.example.com")

    env_file("LUMI_PUBLIC_ORIGINS=\n")
    assert not sio_mod._origin_allowed(PUBLIC)


# ─────────────────────────────────────────────────────────────
# HTTP: registration lock, cookies, stream routes
# ─────────────────────────────────────────────────────────────
class _FakeObsModule:
    def __init__(self):
        self.calls = []
        self.scene = "Main"

    def status(self):
        return {"connected": True, "scenes": ["Main", "BRB"],
                "current_scene": self.scene, "streaming": False}

    def switch_scene(self, name):
        if name not in ("Main", "BRB"):
            raise OBSError("OBS couldn't find that resource.")
        self.calls.append(("scene", name))
        self.scene = name

    def start_stream(self):
        self.calls.append(("start",))

    def stop_stream(self):
        self.calls.append(("stop",))


@pytest.fixture
def server(tmp_path, monkeypatch, env_file):
    env_file(f"LUMI_PUBLIC_ORIGINS={PUBLIC}\n"
             f"STREAM_EMBED_URL=https://player.twitch.tv/?channel=x\n")
    db_path = str(tmp_path / "users.db")
    # Streaming ships disabled; these tests cover the dormant routes.
    monkeypatch.setattr(sio_mod, "STREAMING_ENABLED", True)
    monkeypatch.setattr(sio_mod, "UserDB", lambda: UserDB(db_path))
    monkeypatch.setattr(sio_mod.HOST, "update", lambda **kw: None,
                        raising=False)
    signals = SimpleNamespace(history=[], terminate=False,
                              last_message_time=0.0)
    obs = _FakeObsModule()
    srv = sio_mod.SocketIOServer(signals, modules={"obs": obs})
    return srv, obs


def _run(server_fixture, scenario):
    srv, obs = server_fixture

    async def go():
        client = TestClient(TestServer(srv.app))
        await client.start_server()
        try:
            await scenario(client, obs)
        finally:
            await client.close()
    asyncio.run(go())


async def _register(client, origin=LOCAL, headers=None, username="host"):
    return await client.post(
        "/auth/register",
        json={"username": username, "password": "correct-horse-1"},
        headers={"Origin": origin, **(headers or {})},
    )


def test_remote_registration_refused(server):
    async def scenario(client, _obs):
        r = await _register(client, origin=PUBLIC,
                            headers={"Cf-Connecting-Ip": "203.0.113.9"})
        assert r.status == 403
        assert "host machine" in (await r.json())["error"]
        # Same request without the tunnel markers is a local signup.
        r = await _register(client)
        assert r.status == 200
    _run(server, scenario)


def test_unlisted_origin_refused(server):
    async def scenario(client, _obs):
        r = await _register(client, origin="https://evil.example.com")
        assert r.status == 403
    _run(server, scenario)


def test_remote_login_sets_secure_cookie(server):
    async def scenario(client, _obs):
        assert (await _register(client)).status == 200
        r = await client.post(
            "/auth/login",
            json={"username": "host", "password": "correct-horse-1"},
            headers={"Origin": PUBLIC, "Cf-Connecting-Ip": "203.0.113.9",
                     "X-Forwarded-Proto": "https"},
        )
        assert r.status == 200
        cookies = r.headers.getall("Set-Cookie")
        assert cookies and all("Secure" in c for c in cookies)
    _run(server, scenario)


def test_stream_routes(server):
    async def scenario(client, obs):
        r = await _register(client)
        csrf = (await r.json())["csrf"]

        r = await client.get("/api/stream/status")
        data = await r.json()
        assert r.status == 200
        assert data["obs"]["scenes"] == ["Main", "BRB"]
        assert data["embed_url"].startswith("https://player.twitch.tv/")
        assert data["public_origins"] == [PUBLIC]

        # CSRF is required on every action.
        r = await client.post("/api/stream/start", json={},
                              headers={"Origin": LOCAL})
        assert r.status == 403
        assert obs.calls == []

        h = {"Origin": LOCAL, "X-CSRF-Token": csrf}
        r = await client.post("/api/stream/scene", json={"scene": "BRB"},
                              headers=h)
        assert r.status == 200
        assert (await r.json())["obs"]["current_scene"] == "BRB"

        r = await client.post("/api/stream/scene", json={"scene": "Nope"},
                              headers=h)
        assert r.status == 502

        assert (await client.post("/api/stream/start", json={},
                                  headers=h)).status == 200
        assert (await client.post("/api/stream/stop", json={},
                                  headers=h)).status == 200
        assert obs.calls == [("scene", "BRB"), ("start",), ("stop",)]
    _run(server, scenario)


def test_socketio_handshake_uses_public_origins(server):
    async def scenario(client, _obs):
        url = "/socket.io/?EIO=4&transport=polling"
        r = await client.get(url, headers={"Origin": PUBLIC})
        assert r.status == 200
        assert r.headers.get("Access-Control-Allow-Origin") == PUBLIC
        r = await client.get(url, headers={"Origin": "https://evil.example.com"})
        assert r.status == 400
    _run(server, scenario)


def test_stream_status_requires_login(server):
    async def scenario(client, _obs):
        r = await client.get("/api/stream/status")
        assert r.status == 401
    _run(server, scenario)


def test_non_https_embed_url_is_dropped(server, env_file):
    env_file("STREAM_EMBED_URL=javascript:alert(1)\n")

    async def scenario(client, _obs):
        await _register(client)
        data = await (await client.get("/api/stream/status")).json()
        assert data["embed_url"] == ""
    _run(server, scenario)


# ─────────────────────────────────────────────────────────────
# setup_tunnel.py .env writer
# ─────────────────────────────────────────────────────────────
def test_tunnel_env_upsert_and_remove(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "setup_tunnel", os.path.join(os.path.dirname(__file__), "..",
                                     "scripts", "setup_tunnel.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    env = tmp_path / ".env"
    env.write_text("# keep me\nFOO=1\nLUMI_PUBLIC_ORIGINS=https://old\n",
                   encoding="utf-8")
    monkeypatch.setattr(mod, "_ENV_PATH", str(env))

    mod.set_env_var("LUMI_PUBLIC_ORIGINS", PUBLIC)
    assert env.read_text(encoding="utf-8") == \
        f"# keep me\nFOO=1\nLUMI_PUBLIC_ORIGINS={PUBLIC}\n"
    mod.set_env_var("LUMI_PUBLIC_ORIGINS", None)
    assert env.read_text(encoding="utf-8") == "# keep me\nFOO=1\n"

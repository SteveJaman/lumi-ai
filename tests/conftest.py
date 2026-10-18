"""Shared fixtures for the web-server tests.

`live_lumi` runs the real SocketIOServer.start_sites() on two free
loopback ports, exactly as main.py does with 5000 and the tunnel port,
against a throwaway user DB and .env. Requests to `live.local` arrive
on the main port; requests to `live.remote` arrive on the tunnel port,
which is how cloudflared reaches Lumi in production.
"""

import asyncio
import os
from types import SimpleNamespace

import aiohttp
import pytest

import modules.socketIO as sio_mod
from modules.log_hub import LogHub
from modules.user_auth import UserDB

PUBLIC = "https://lumi.example.com"
LOCAL = "http://127.0.0.1:5000"
PASSWORD = "correct-horse-1"


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    """Point the server at a temp .env; returns a writer for it."""
    path = tmp_path / ".env"
    monkeypatch.setattr(sio_mod, "_ENV_PATH", str(path))
    monkeypatch.setattr(sio_mod, "_public_origins_cache",
                        (object(), frozenset()))
    monkeypatch.setattr(sio_mod, "_tunnel_origins", frozenset())
    monkeypatch.delenv("LUMI_PUBLIC_ORIGINS", raising=False)

    def write(text):
        path.write_text(text, encoding="utf-8")
        # Force a distinct mtime even on coarse-resolution filesystems.
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    return write


@pytest.fixture
def make_server(tmp_path, monkeypatch, env_file):
    """Factory for a SocketIOServer wired to temp state. Keyword args
    become SocketIOServer kwargs (e.g. modules=, prompter=)."""
    env_file(f"LUMI_PUBLIC_ORIGINS={PUBLIC}\n")
    db_path = str(tmp_path / "users.db")
    monkeypatch.setattr(sio_mod, "UserDB", lambda: UserDB(db_path))
    monkeypatch.setattr(sio_mod.HOST, "update", lambda **kw: None,
                        raising=False)

    def factory(**kwargs):
        hub = LogHub(log_file=str(tmp_path / "events.log"))
        signals = SimpleNamespace(history=[], terminate=False,
                                  last_message_time=0.0, log_hub=hub)
        kwargs.setdefault("prompter",
                          SimpleNamespace(agent_mode_enabled=False))
        return sio_mod.SocketIOServer(signals, **kwargs)
    return factory


class Live:
    def __init__(self, srv):
        self.srv = srv
        self.local_url = f"http://127.0.0.1:{srv.port}"
        self.tunnel_url = f"http://127.0.0.1:{srv.tunnel_port}"

    async def __aenter__(self):
        # unsafe=True: aiohttp otherwise refuses cookies for IP hosts.
        self.local = aiohttp.ClientSession(
            base_url=self.local_url,
            cookie_jar=aiohttp.CookieJar(unsafe=True))
        self.remote = aiohttp.ClientSession(
            base_url=self.tunnel_url,
            cookie_jar=aiohttp.CookieJar(unsafe=True))
        return self

    async def __aexit__(self, *exc):
        await self.local.close()
        await self.remote.close()

    async def register(self, username="host", password=PASSWORD):
        """Create an account from the host and return its CSRF token."""
        r = await self.local.post(
            "/auth/register", json={"username": username,
                                    "password": password},
            headers={"Origin": LOCAL})
        assert r.status == 200, await r.text()
        return (await r.json())["csrf"]

    async def remote_login(self, username="host", password=PASSWORD,
                           headers=None):
        """Sign in through the tunnel port the way a browser on the
        tunnel hostname would. Returns (response, csrf)."""
        r = await self.remote.post(
            "/auth/login", json={"username": username,
                                 "password": password},
            headers={"Origin": PUBLIC, **(headers or {})})
        data = await r.json()
        return r, data.get("csrf")


@pytest.fixture
def live_lumi(make_server):
    """Run `scenario(live)` against a server on real sockets."""
    def run(scenario, **server_kwargs):
        srv = make_server(**server_kwargs)

        async def go():
            runner = await srv.start_sites("127.0.0.1", 0, 0)
            try:
                async with Live(srv) as live:
                    await scenario(live)
            finally:
                await runner.cleanup()
        asyncio.run(go())
    return run

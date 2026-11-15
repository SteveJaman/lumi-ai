"""GET/POST /api/island/remote: login, origin and CSRF, the code shown once,
and the route staying host-only (never on the tunnel)."""

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from modules import hitl_server, island_remote as ir
from modules import socketIO as sio_mod

TS = "100.101.102.103"
H = {"X-CSRF-Token": "tok"}


class StubServer:
    def _origin_ok(self, request): return request.headers.get("Origin") in (None, "http://127.0.0.1")
    def _current_user(self, request): return None if request.headers.get("X-Anon") else {"id": 1}
    def _require_csrf(self, request, body):
        return (True, None) if request.headers.get("X-CSRF-Token") == "tok" else (False, "Missing CSRF token.")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(ir, "tailscale_ip", lambda: TS)
    monkeypatch.setattr(hitl_server._server, "_loop", None)        # no live approval socket to move
    app = web.Application()
    ir.register(app.router, StubServer())

    def run(coro_fn):
        async def go():
            async with TestClient(TestServer(app)) as c:
                return await coro_fn(c)
        return asyncio.run(go())
    return run


def test_needs_login_csrf_and_a_local_origin(client):
    async def t(c):
        assert (await c.get("/api/island/remote", headers={"X-Anon": "1"})).status == 401
        assert (await c.post("/api/island/remote", json={"action": "enable"})).status == 403
        assert (await c.post("/api/island/remote", json={"action": "enable"},
                             headers={**H, "Origin": "http://evil.test"})).status == 403
        assert (await c.post("/api/island/remote", json={"action": "nuke"}, headers=H)).status == 400
        assert not ir.is_enabled()
    client(t)


def test_enable_shows_the_code_once_then_status_never_does(client):
    async def t(c):
        st = await (await c.get("/api/island/remote")).json()
        assert st["enabled"] is False and st["tailscale_ip"] == TS
        r = await c.post("/api/island/remote", json={"action": "enable"}, headers=H)
        body = await r.json()
        assert r.status == 200 and body["status"]["enabled"] and body["status"]["host"] == TS
        code = body["code"]
        again = await (await c.get("/api/island/remote")).text()
        assert code not in again
        assert ir.verify(code, "100.1.2.3")
        r = await c.post("/api/island/remote", json={"action": "regenerate"}, headers=H)
        assert (await r.json())["code"] != code and not ir.verify(code, "100.1.2.3")
        r = await c.post("/api/island/remote", json={"action": "disable"}, headers=H)
        assert (await r.json())["status"]["enabled"] is False and "code" not in await r.json()
    client(t)


def test_no_tailscale_is_a_clear_409(client, monkeypatch):
    monkeypatch.setattr(ir, "tailscale_ip", lambda: None)

    async def t(c):
        r = await c.post("/api/island/remote", json={"action": "enable"}, headers=H)
        assert r.status == 409 and "Tailscale" in (await r.json())["error"]
    client(t)


def test_the_route_is_never_reachable_through_the_tunnel():
    assert not any(path.startswith("/api/island") for _, path in sio_mod._REMOTE_API_ALLOW)

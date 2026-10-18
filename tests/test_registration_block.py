"""Registration through the tunnel is refused, and the check can't be
talked around with headers.

Why not a peer-address check: cloudflared runs on the host and connects
to Lumi over loopback, so tunnel traffic always has peer 127.0.0.1. The
deciding signal is the port the request arrived on. These tests use real
sockets, so the peer really is 127.0.0.1 on both ports.
"""

from unittest import mock

from aiohttp.test_utils import make_mocked_request

import modules.socketIO as sio_mod
from tests.conftest import LOCAL, PASSWORD, PUBLIC


async def _register(session, username, origin, headers=None):
    return await session.post(
        "/auth/register",
        json={"username": username, "password": PASSWORD},
        headers={"Origin": origin, **(headers or {})})


def test_host_port_can_register(live_lumi):
    async def scenario(live):
        r = await _register(live.local, "host", LOCAL)
        assert r.status == 200
    live_lumi(scenario)


def test_tunnel_port_cannot_register_even_with_local_origin(live_lumi):
    async def scenario(live):
        r = await _register(live.remote, "intruder", LOCAL,
                            headers={"Host": "127.0.0.1:5000"})
        assert r.status == 403
        assert "Registration is disabled" in (await r.json())["error"]
        assert live.srv.user_db.user_count() == 0
    live_lumi(scenario)


def test_tunnel_port_cannot_register_with_public_origin(live_lumi):
    async def scenario(live):
        r = await _register(live.remote, "intruder", PUBLIC)
        assert r.status == 403
    live_lumi(scenario)


def test_cloudflare_header_on_host_port_cannot_register(live_lumi):
    async def scenario(live):
        r = await _register(live.local, "intruder", LOCAL,
                            headers={"Cf-Connecting-Ip": "203.0.113.9"})
        assert r.status == 403
    live_lumi(scenario)


def test_cf_prefix_reserved(live_lumi):
    async def scenario(live):
        r = await _register(live.local, "cf-alice-12345678", LOCAL)
        assert r.status == 400
    live_lumi(scenario)


def test_non_loopback_peer_is_remote(make_server):
    """A LAN or edge peer (ALLOW_LAN_EXPOSURE) is remote even when it
    claims a local Origin."""
    srv = make_server()
    transport = mock.Mock()
    transport.get_extra_info.side_effect = lambda key, default=None: {
        "peername": ("172.64.0.10", 443),
        "sockname": ("0.0.0.0", 5000),
    }.get(key, default)
    req = make_mocked_request("POST", "/auth/register",
                              headers={"Origin": LOCAL},
                              transport=transport)
    assert req.remote == "172.64.0.10"
    assert srv._is_local(req) is False


def test_loopback_peer_on_main_port_is_local(make_server):
    srv = make_server()
    srv.tunnel_port = 5080
    transport = mock.Mock()
    transport.get_extra_info.side_effect = lambda key, default=None: {
        "peername": ("127.0.0.1", 50123),
        "sockname": ("127.0.0.1", 5000),
    }.get(key, default)
    req = make_mocked_request("GET", "/", transport=transport)
    assert srv._is_local(req) is True
    assert sio_mod._is_chat_command("!agent_on")

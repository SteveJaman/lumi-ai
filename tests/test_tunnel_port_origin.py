"""A browser opened directly at http://127.0.0.1:<tunnel port> (testing
the remote tier without cloudflared) sends that address as its Origin.
It must be able to sign in and connect socket.io, and must still get
the remote tier: accepting an origin grants nothing by itself."""

import socketio
from yarl import URL

from tests.conftest import PASSWORD


def test_direct_tunnel_port_browser(live_lumi):
    async def scenario(live):
        await live.register()
        origin = live.tunnel_url               # http://127.0.0.1:<port>
        h = {"Origin": origin}

        # Login POST passes the origin check (was "Bad origin.").
        r = await live.remote.post(
            "/auth/login", json={"username": "host", "password": PASSWORD},
            headers=h)
        assert r.status == 200, await r.text()
        csrf = (await r.json())["csrf"]

        # Still the remote tier.
        status = await (await live.remote.get("/api/status")).json()
        assert status["is_local"] is False
        r = await live.remote.get("/api/settings/about", headers=h)
        assert r.status == 403
        r = await live.remote.post("/api/chat/send", json={"text": "hi"},
                                   headers={**h, "X-CSRF-Token": csrf})
        assert r.status == 200

        # socket.io handshake + connect with the same origin.
        jar = live.remote.cookie_jar.filter_cookies(URL(live.tunnel_url))
        cookie = "; ".join(f"{k}={v.value}" for k, v in jar.items())
        got = {}
        client = socketio.AsyncClient()

        @client.on("web_ready")
        async def _ready(data):
            got["ready"] = data

        await client.connect(live.tunnel_url, transports=["polling"],
                             headers={"Origin": origin, "Cookie": cookie})
        for _ in range(30):
            if "ready" in got:
                break
            await client.sleep(0.1)
        await client.disconnect()
        assert got["ready"]["user"]["username"] == "host"

        # localhost spelling of the same port is accepted too.
        r = await live.remote.get(
            "/socket.io/?EIO=4&transport=polling",
            headers={"Origin": f"http://localhost:{live.srv.tunnel_port}"})
        assert r.status == 200
    live_lumi(scenario)


def test_other_loopback_ports_still_refused(live_lumi):
    async def scenario(live):
        r = await live.remote.get(
            "/socket.io/?EIO=4&transport=polling",
            headers={"Origin": "http://127.0.0.1:8080"})
        assert r.status == 400
    live_lumi(scenario)

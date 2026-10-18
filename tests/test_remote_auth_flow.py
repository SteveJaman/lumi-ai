"""The sign-in path a browser takes through the tunnel, on real sockets:
page redirect, login with the tunnel origin, cookie flags, an
authenticated API call, and a real socket.io connection."""

import socketio
from yarl import URL

from tests.conftest import PASSWORD, PUBLIC

TUNNEL_HEADERS = {
    # What cloudflared forwards for a browser on https://lumi.example.com
    "Origin": PUBLIC,
    "X-Forwarded-Proto": "https",
    "Cf-Connecting-Ip": "203.0.113.9",
    "X-Forwarded-For": "203.0.113.9",
}


def test_unauthenticated_root_redirects_to_login(live_lumi):
    async def scenario(live):
        await live.register()
        r = await live.remote.get("/", allow_redirects=False)
        assert r.status == 302
        assert r.headers["Location"] == "/login"
        r = await live.remote.get("/login")
        assert r.status == 200
        for asset in ("/web/app.js", "/web/style.css", "/web/login.js",
                      "/web/theme-init.js"):
            assert (await live.remote.get(asset)).status == 200, asset
    live_lumi(scenario)


def test_login_cookie_flags_and_status(live_lumi):
    async def scenario(live):
        await live.register()
        r = await live.remote.post(
            "/auth/login", json={"username": "host", "password": PASSWORD},
            headers=TUNNEL_HEADERS)
        assert r.status == 200

        cookies = {}
        for raw in r.headers.getall("Set-Cookie"):
            name, _, rest = raw.partition("=")
            cookies[name] = raw
            attrs = {a.strip().split("=")[0].lower()
                     for a in raw.split(";")[1:]}
            assert {"secure", "path", "samesite"} <= attrs, raw
            assert "SameSite=Lax" in raw and "Path=/" in raw
        assert "httponly" in {a.strip().lower() for a in
                              cookies["lumi_session"].split(";")}

        # A browser sends Secure cookies back over the HTTPS tunnel;
        # aiohttp won't over plain http, so pass the cookie by hand.
        session = cookies["lumi_session"].split(";")[0]
        r = await live.remote.get("/api/status",
                                  headers={**TUNNEL_HEADERS,
                                           "Cookie": session})
        assert r.status == 200
        assert (await r.json())["is_local"] is False
    live_lumi(scenario)


def test_socketio_connects_through_tunnel_port(live_lumi):
    async def scenario(live):
        await live.register()
        r, _ = await live.remote_login()
        session = live.remote.cookie_jar.filter_cookies(URL(live.tunnel_url))
        cookie = "; ".join(f"{k}={v.value}" for k, v in session.items())

        got = {}
        client = socketio.AsyncClient()

        @client.on("web_ready")
        async def _ready(data):
            got["ready"] = data

        await client.connect(live.tunnel_url, transports=["polling"],
                             headers={"Origin": PUBLIC, "Cookie": cookie})
        await client.sleep(0.3)
        await client.disconnect()
        assert got["ready"]["user"]["username"] == "host"

        # Unlisted origin is refused at the handshake.
        bad = socketio.AsyncClient()
        try:
            await bad.connect(live.tunnel_url, transports=["polling"],
                              headers={"Origin": "https://evil.example.com",
                                       "Cookie": cookie})
            connected = bad.connected
            await bad.disconnect()
        except socketio.exceptions.ConnectionError:
            connected = False
        assert not connected
    live_lumi(scenario)

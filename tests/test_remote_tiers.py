"""Remote (tunnel-port) requests get the reduced tier; local requests
keep full access. Runs against real sockets via conftest.live_lumi."""

from tests.conftest import LOCAL, PUBLIC

# Host-only routes. The tier middleware refuses these before any
# handler runs, so even the writes here have no side effects.
HOST_ONLY = [
    ("GET",    "/api/settings/general"),
    ("PUT",    "/api/settings/general"),
    ("GET",    "/api/settings/about"),
    ("POST",   "/api/settings/security/password"),
    ("GET",    "/api/integrations"),
    ("PUT",    "/api/integrations/discord"),
    ("POST",   "/api/integrations/ollama/test"),
    ("GET",    "/api/providers"),
    ("PUT",    "/api/providers/openai"),
    ("GET",    "/api/models/pull"),
    ("POST",   "/api/models/load"),
    ("POST",   "/api/models/delete"),
    ("PUT",    "/api/models/routing"),
    ("GET",    "/api/persona"),
    ("PUT",    "/api/persona"),
    ("POST",   "/api/persona/raw"),
    ("GET",    "/api/memories"),
    ("GET",    "/api/memories/export"),
    ("DELETE", "/api/memories/abc"),
    ("POST",   "/api/agent/toggle"),
    ("GET",    "/api/game/status"),
    ("POST",   "/api/game/toggle"),
    ("POST",   "/api/games/minecraft/directive"),
    ("GET",    "/api/log_events"),
    ("GET",    "/api/some-future-route"),   # default-deny
]


def test_remote_gets_403_on_host_only_routes(live_lumi):
    async def scenario(live):
        await live.register()
        r, csrf = await live.remote_login()
        assert r.status == 200
        for method, path in HOST_ONLY:
            r = await live.remote.request(
                method, path, json={},
                headers={"Origin": PUBLIC, "X-CSRF-Token": csrf})
            assert r.status == 403, (method, path, r.status)
            assert (await r.json())["error"] == \
                "Only available on the host machine."
    live_lumi(scenario)


def test_remote_keeps_chat_history_status_models(live_lumi):
    async def scenario(live):
        await live.register()
        _, csrf = await live.remote_login()
        h = {"Origin": PUBLIC, "X-CSRF-Token": csrf}

        r = await live.remote.get("/api/status")
        assert r.status == 200
        status = await r.json()
        assert status["is_local"] is False
        assert status["features"] == {"stream": False}

        assert (await live.remote.get("/api/history")).status == 200
        r = await live.remote.post("/api/chat/send",
                                   json={"text": "hello"}, headers=h)
        assert r.status == 200
        assert live.srv.signals.history[-1]["content"] == "hello"

        # Allowed through the tier; may fail later if Ollama is down,
        # but never with the tier's 403.
        r = await live.remote.get("/api/models")
        assert r.status != 403
    live_lumi(scenario)


def test_remote_chat_commands_refused(live_lumi):
    async def scenario(live):
        await live.register()
        _, csrf = await live.remote_login()
        h = {"Origin": PUBLIC, "X-CSRF-Token": csrf}
        for text in ("!agent_on", "  WIPE MEMORY ", "set mood bratty",
                     "!set foo 1"):
            r = await live.remote.post("/api/chat/send",
                                       json={"text": text}, headers=h)
            assert r.status == 403, text
        assert live.srv.signals.history == []
    live_lumi(scenario)


def test_local_gets_200(live_lumi):
    async def scenario(live):
        csrf = await live.register()
        h = {"Origin": LOCAL, "X-CSRF-Token": csrf}

        r = await live.local.get("/api/status")
        assert (await r.json())["is_local"] is True

        for path in ("/api/settings/about", "/api/persona",
                     "/api/log_events"):
            r = await live.local.get(path)
            assert r.status == 200, (path, await r.text())

        r = await live.local.post("/api/agent/toggle", json={}, headers=h)
        assert r.status == 200
        assert (await r.json())["enabled"] is True

        # Local commands still pass through to the prompter.
        r = await live.local.post("/api/chat/send",
                                  json={"text": "!agent_off"}, headers=h)
        assert r.status == 200
    live_lumi(scenario)


def test_local_game_toggle(live_lumi):
    """The dashboard's Minecraft AI switch: validated, CSRF-checked, and
    passed to the game manager."""
    async def scenario(live):
        calls = []

        class FakeGames:
            def status(self, game):
                if game != "minecraft":
                    raise ValueError(f"unknown game: {game}")
                return {"game": game, "enabled": bool(calls and calls[-1][1])}

            def set_enabled(self, game, enabled):
                self.status(game)
                calls.append((game, enabled))
                return self.status(game)

        live.srv.signals.game_manager = FakeGames()
        csrf = await live.register()
        h = {"Origin": LOCAL, "X-CSRF-Token": csrf}

        r = await live.local.get("/api/game/status")
        assert r.status == 200 and (await r.json())["enabled"] is False

        r = await live.local.post("/api/game/toggle",
                                  json={"game": "minecraft", "enabled": True}, headers=h)
        assert r.status == 200 and (await r.json())["enabled"] is True
        assert calls == [("minecraft", True)]

        r = await live.local.post("/api/game/toggle",
                                  json={"game": "minecraft", "enabled": "yes"}, headers=h)
        assert r.status == 400
        r = await live.local.post("/api/game/toggle",
                                  json={"game": "chess", "enabled": True}, headers=h)
        assert r.status == 400
        r = await live.local.post("/api/game/toggle",
                                  json={"game": "minecraft", "enabled": False},
                                  headers={"Origin": LOCAL})
        assert r.status == 403          # no CSRF token
        assert calls == [("minecraft", True)]
    live_lumi(scenario)


def test_local_game_directive(live_lumi):
    """The Games page's directive box: validated, CSRF-checked, passed to
    the game manager, and a stopped bot is reported as 503."""
    async def scenario(live):
        sent = []

        class FakeGames:
            running = True

            def send_directive(self, game, text):
                if game != "minecraft":
                    raise ValueError(f"unknown game: {game}")
                if not self.running:
                    raise RuntimeError("Minecraft AI isn't running.")
                sent.append(text)
                return {"success": True, "message": f"Order received: {text}"}

        games = live.srv.signals.game_manager = FakeGames()
        csrf = await live.register()
        h = {"Origin": LOCAL, "X-CSRF-Token": csrf}

        r = await live.local.post("/api/games/minecraft/directive",
                                  json={"text": "get me 5 diamonds"}, headers=h)
        assert r.status == 200 and sent == ["get me 5 diamonds"]
        for bad in ({"text": "   "}, {"text": 5}, {}):
            r = await live.local.post("/api/games/minecraft/directive", json=bad, headers=h)
            assert r.status == 400, bad
        r = await live.local.post("/api/games/chess/directive", json={"text": "go"}, headers=h)
        assert r.status == 400
        r = await live.local.post("/api/games/minecraft/directive",
                                  json={"text": "follow me"}, headers={"Origin": LOCAL})
        assert r.status == 403          # no CSRF token
        games.running = False
        r = await live.local.post("/api/games/minecraft/directive",
                                  json={"text": "follow me"}, headers=h)
        assert r.status == 503
        assert sent == ["get me 5 diamonds"]
    live_lumi(scenario)


def test_proxy_headers_downgrade_main_port(live_lumi):
    """cloudflared pointed at :5000 by mistake still lands in the
    remote tier, because Cloudflare adds these headers."""
    async def scenario(live):
        await live.register()
        for header in ("Cf-Connecting-Ip", "Cf-Ray", "X-Forwarded-For"):
            r = await live.local.get("/api/settings/about",
                                     headers={header: "203.0.113.9"})
            assert r.status == 403, header
    live_lumi(scenario)


def test_remote_cannot_upgrade_with_headers(live_lumi):
    """Nothing a remote client sends can make the tunnel port local."""
    async def scenario(live):
        await live.register()
        _, csrf = await live.remote_login()
        r = await live.remote.get(
            "/api/settings/about",
            headers={"Origin": LOCAL, "Host": "127.0.0.1:5000",
                     "X-Forwarded-For": "127.0.0.1",
                     "X-Real-Ip": "127.0.0.1"})
        assert r.status == 403
    live_lumi(scenario)


def test_remote_ui_flags_on_auth_me(live_lumi):
    async def scenario(live):
        await live.register()
        await live.remote_login()
        me = await (await live.remote.get("/auth/me")).json()
        assert me["is_local"] is False
        me = await (await live.local.get("/auth/me")).json()
        assert me["is_local"] is True
    live_lumi(scenario)


def test_remote_setup_page_redirects_to_login(live_lumi):
    async def scenario(live):
        r = await live.remote.get("/setup", allow_redirects=False)
        assert r.status == 302
        assert r.headers["Location"] == "/login"
        status = await (await live.remote.get("/setup-status")).json()
        assert status == {"needs_setup": True, "user_count": 0,
                          "remote": True}
    live_lumi(scenario)

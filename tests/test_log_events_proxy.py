"""/api/log_events serves the log hub's events to local sessions so the
dashboard never has to reach 127.0.0.1:5001 from the browser."""


def test_matches_log_hub(live_lumi):
    async def scenario(live):
        await live.register()
        hub = live.srv.signals.log_hub
        hub.emit("system", "booted")
        hub.emit("memory", "stored 3 facts")
        hub.emit("system", "api key sk-abcdefghijklmnopqrstuvwxyz123456")

        r = await live.local.get("/api/log_events")
        assert r.status == 200
        assert (await r.json())["events"] == hub.get_events()

        r = await live.local.get("/api/log_events?limit=1")
        assert (await r.json())["events"] == hub.get_events(limit=1)
    live_lumi(scenario)


def test_requires_auth(live_lumi):
    async def scenario(live):
        r = await live.local.get("/api/log_events")
        assert r.status == 401
    live_lumi(scenario)


def test_remote_refused(live_lumi):
    async def scenario(live):
        await live.register()
        live.srv.signals.log_hub.emit("system", "secret window title")
        r, _ = await live.remote_login()
        assert r.status == 200
        r = await live.remote.get("/api/log_events")
        assert r.status == 403
        assert "secret" not in await r.text()
    live_lumi(scenario)


def test_no_hub_returns_empty(live_lumi):
    async def scenario(live):
        await live.register()
        live.srv.signals.log_hub = None
        r = await live.local.get("/api/log_events")
        assert (await r.json()) == {"events": []}
    live_lumi(scenario)

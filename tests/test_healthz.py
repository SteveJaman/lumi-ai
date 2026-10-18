"""/healthz answers without auth on both ports."""

from constants import LUMI_VERSION


def test_healthz_no_auth(live_lumi):
    async def scenario(live):
        for session in (live.local, live.remote):
            r = await session.get("/healthz")
            assert r.status == 200
            assert await r.json() == {"ok": True, "version": LUMI_VERSION}
    live_lumi(scenario)

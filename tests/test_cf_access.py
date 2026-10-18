"""TRUST_CLOUDFLARE_ACCESS: a remote request with a valid Access JWT is
signed in; forged, mis-addressed, or header-only requests are not."""

import base64
import json
import time

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

import modules.cf_access as cf_access
import modules.socketIO as sio_mod
from tests.conftest import PUBLIC

TEAM = "lumi-test.cloudflareaccess.com"
AUD = "aud-tag-123"
KID = "key-1"


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_jwt(key, email="alice@students.kennesaw.edu", **overrides):
    claims = {"iss": f"https://{TEAM}", "aud": [AUD], "email": email,
              "exp": time.time() + 600, "nbf": time.time() - 10}
    claims.update(overrides)
    head = _b64(json.dumps({"alg": "RS256", "kid": KID}).encode())
    body = _b64(json.dumps(claims).encode())
    sig = key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(),
                   hashes.SHA256())
    return f"{head}.{body}.{_b64(sig)}"


@pytest.fixture
def access(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(cf_access, "_fetch_keys",
                        lambda team: {KID: key.public_key()})
    monkeypatch.setattr(cf_access, "_keys", {})
    monkeypatch.setattr(cf_access, "_keys_fetched_at", 0.0)
    monkeypatch.setattr(cf_access, "_keys_team", None)
    monkeypatch.setattr(cf_access, "_ENV_PATH", "does-not-exist.env")
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "lumi-test")
    monkeypatch.setenv("CF_ACCESS_AUD", AUD)
    monkeypatch.setattr(sio_mod, "TRUST_CLOUDFLARE_ACCESS", True)
    return key


def test_verify_rejects_bad_tokens(access):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    good = make_jwt(access)
    assert cf_access.verify_access_jwt(good, TEAM, AUD) == \
        "alice@students.kennesaw.edu"
    for bad in (make_jwt(other),                       # wrong signer
                make_jwt(access, aud=["someone-else"]),
                make_jwt(access, iss="https://evil.cloudflareaccess.com"),
                make_jwt(access, exp=time.time() - 3600),
                make_jwt(access, email=""),
                good[:-4] + "AAAA",
                "not.a.jwt"):
        with pytest.raises(cf_access.AccessError):
            cf_access.verify_access_jwt(bad, TEAM, AUD)


def test_valid_jwt_signs_in_remote_user(live_lumi, access):
    async def scenario(live):
        await live.register()
        token = make_jwt(access)
        r = await live.remote.get("/auth/me",
                                  headers={"Cf-Access-Jwt-Assertion": token})
        assert r.status == 200
        me = await r.json()
        assert me["user"]["username"].startswith("cf-alice-")
        assert me["user"]["display_name"] == "alice@students.kennesaw.edu"
        assert me["is_local"] is False
        assert me["csrf"]
        assert any("lumi_session=" in c for c in r.headers.getall("Set-Cookie"))

        # Still the remote tier.
        r = await live.remote.get("/api/settings/about",
                                  headers={"Cf-Access-Jwt-Assertion": token})
        assert r.status == 403

        # Same email maps to the same account on the next visit.
        r = await live.remote.get("/auth/me",
                                  headers={"Cf-Access-Jwt-Assertion": token})
        assert (await r.json())["user"]["id"] == me["user"]["id"]
        assert live.srv.user_db.user_count() == 2
    live_lumi(scenario)


def test_email_header_alone_is_ignored(live_lumi, access):
    async def scenario(live):
        await live.register()
        r = await live.remote.get("/auth/me", headers={
            "Cf-Access-Authenticated-User-Email": "host@example.com"})
        assert r.status == 401
    live_lumi(scenario)


def test_forged_jwt_is_ignored(live_lumi, access):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    async def scenario(live):
        await live.register()
        r = await live.remote.get("/auth/me", headers={
            "Cf-Access-Jwt-Assertion": make_jwt(other)})
        assert r.status == 401
        assert live.srv.user_db.user_count() == 1
    live_lumi(scenario)


def test_trust_off_ignores_valid_jwt(live_lumi, access, monkeypatch):
    monkeypatch.setattr(sio_mod, "TRUST_CLOUDFLARE_ACCESS", False)

    async def scenario(live):
        await live.register()
        r = await live.remote.get("/auth/me", headers={
            "Cf-Access-Jwt-Assertion": make_jwt(access)})
        assert r.status == 401
    live_lumi(scenario)


def test_page_redirect_gets_session_too(live_lumi, access):
    async def scenario(live):
        await live.register()
        r = await live.remote.get("/login", allow_redirects=False, headers={
            "Cf-Access-Jwt-Assertion": make_jwt(access), "Origin": PUBLIC})
        # Signed in by Access, so /login bounces to the app...
        assert r.status == 302
        # ...and the redirect carries the new session cookie.
        assert any("lumi_session=" in c for c in r.headers.getall("Set-Cookie"))
    live_lumi(scenario)


def test_username_is_stable_and_reserved():
    a = cf_access.username_for_email("Bob.Smith+x@ksu.edu".lower())
    assert a == cf_access.username_for_email("bob.smith+x@ksu.edu")
    assert a.startswith("cf-bobsmithx-") and len(a) <= 32

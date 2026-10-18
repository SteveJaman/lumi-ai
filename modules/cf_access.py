"""
Cloudflare Access identity for remote requests.

When Cloudflare Access sits in front of the tunnel, every request it
lets through carries Cf-Access-Jwt-Assertion: an RS256 JWT signed by
your Access team, with the visitor's email inside. This module checks
that JWT and returns the email.

The Cf-Access-Authenticated-User-Email header is never trusted on its
own. Anyone who reaches the origin without passing Access (a quick
tunnel, a misconfigured application, a second hostname) can set it to
anything. The signature can't be forged.

Settings, from .env:
    CF_ACCESS_TEAM_DOMAIN   yourteam.cloudflareaccess.com (or "yourteam")
    CF_ACCESS_AUD           the Application Audience (AUD) tag from the
                            Access application's Overview page

Only consulted when TRUST_CLOUDFLARE_ACCESS is True in constants.py.
Blocking (fetches the team's keys over HTTPS); call from a thread.
"""

import base64
import hashlib
import json
import os
import re
import threading
import time
import urllib.request

from dotenv import dotenv_values

from constants import WORKSPACE_ROOT

_ENV_PATH = os.path.join(WORKSPACE_ROOT, ".env")
_KEY_TTL_SECONDS = 3600
_KEY_REFETCH_MIN_SECONDS = 60   # on an unknown kid, at most once a minute
_CLOCK_SKEW_SECONDS = 60

_lock = threading.Lock()
_keys = {}           # kid -> RSAPublicKey
_keys_fetched_at = 0.0
_keys_team = None


class AccessError(Exception):
    pass


def read_access_config():
    env = dotenv_values(_ENV_PATH)
    team = (env.get("CF_ACCESS_TEAM_DOMAIN")
            or os.environ.get("CF_ACCESS_TEAM_DOMAIN") or "").strip().lower()
    team = team.removeprefix("https://").rstrip("/")
    if team and "." not in team:
        team = f"{team}.cloudflareaccess.com"
    aud = (env.get("CF_ACCESS_AUD")
           or os.environ.get("CF_ACCESS_AUD") or "").strip()
    return team, aud


def _b64url(data):
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _fetch_keys(team):
    from cryptography.hazmat.primitives.asymmetric.rsa import \
        RSAPublicNumbers
    url = f"https://{team}/cdn-cgi/access/certs"
    with urllib.request.urlopen(url, timeout=5) as resp:
        jwks = json.load(resp)
    keys = {}
    for k in jwks.get("keys", []):
        if k.get("kty") != "RSA" or not k.get("kid"):
            continue
        n = int.from_bytes(_b64url(k["n"]), "big")
        e = int.from_bytes(_b64url(k["e"]), "big")
        keys[k["kid"]] = RSAPublicNumbers(e, n).public_key()
    return keys


def _key_for(team, kid):
    global _keys, _keys_fetched_at, _keys_team
    with _lock:
        now = time.time()
        stale = (_keys_team != team
                 or now - _keys_fetched_at > _KEY_TTL_SECONDS)
        unknown = kid not in _keys \
            and now - _keys_fetched_at > _KEY_REFETCH_MIN_SECONDS
        if stale or unknown:
            try:
                _keys = _fetch_keys(team)
                _keys_team = team
                _keys_fetched_at = now
            except Exception as e:
                raise AccessError(f"Couldn't fetch Access keys from "
                                  f"{team}: {e}") from None
        key = _keys.get(kid)
    if key is None:
        raise AccessError("JWT signed with an unknown key.")
    return key


def verify_access_jwt(token, team, aud, now=None):
    """Return the verified email, or raise AccessError."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    if not team or not aud:
        raise AccessError("CF_ACCESS_TEAM_DOMAIN and CF_ACCESS_AUD must "
                          "both be set in .env.")
    try:
        head_b64, body_b64, sig_b64 = token.split(".")
        header = json.loads(_b64url(head_b64))
        claims = json.loads(_b64url(body_b64))
        signature = _b64url(sig_b64)
    except Exception:
        raise AccessError("Malformed JWT.") from None

    if header.get("alg") != "RS256":
        raise AccessError(f"Unexpected JWT alg {header.get('alg')!r}.")

    key = _key_for(team, header.get("kid"))
    try:
        key.verify(signature, f"{head_b64}.{body_b64}".encode("ascii"),
                   padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        raise AccessError("JWT signature is invalid.") from None

    now = time.time() if now is None else now
    if claims.get("iss") != f"https://{team}":
        raise AccessError("JWT issuer doesn't match CF_ACCESS_TEAM_DOMAIN.")
    auds = claims.get("aud")
    auds = [auds] if isinstance(auds, str) else (auds or [])
    if aud not in auds:
        raise AccessError("JWT audience doesn't match CF_ACCESS_AUD.")
    if float(claims.get("exp", 0)) < now - _CLOCK_SKEW_SECONDS:
        raise AccessError("JWT has expired.")
    if float(claims.get("nbf", 0)) > now + _CLOCK_SKEW_SECONDS:
        raise AccessError("JWT is not valid yet.")
    email = (claims.get("email") or "").strip().lower()
    if "@" not in email:
        raise AccessError("JWT has no email (service tokens can't sign in).")
    return email


def username_for_email(email):
    """Stable Lumi username for an Access email. Prefixed and hashed so
    it can never equal an account created through the login form."""
    local = re.sub(r"[^a-z0-9_-]", "", email.split("@", 1)[0].lower())[:16]
    digest = hashlib.sha256(email.encode("utf-8")).hexdigest()[:8]
    return f"cf-{local or 'user'}-{digest}"

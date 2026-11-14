"""
Workspace hub: Google Calendar + Gmail and KSU student mail for Lumi.

One place that owns the accounts, a local cache, and the tools:

  * Google (Calendar + Gmail): OAuth 2.0 with google-auth-oauthlib. You
    supply your own OAuth client (data/credentials/google_client.json);
    the token it produces lives next to it. See docs/workspace.md.
  * KSU mail: a provider interface with two implementations.
      - "graph": Microsoft Graph, signed in with the device-code flow
        (needs an app registration; no secret is stored, it is a public
        client).
      - "gmail": fallback for when the university tenant won't consent to
        a third-party app. KSU mail is forwarded into Gmail and given a
        label; Lumi treats that label as the KSU inbox. Read-only for
        sending: mail can't be sent *as* KSU this way.
  * A cache of upcoming events and recent inbox threads (memory, mirrored
    to data/workspace/cache.json) so the UI paints instantly. It is
    refreshed on demand with a TTL and never by a background timer, so an
    idle Lumi (or one running next to a game) does no network work.
  * get_compressed_snapshot(): next event + top 3 unread, built from the
    cache only, small enough to push over a socket. add_listener() calls
    back whenever it changes (Lumi Island can subscribe later).
  * Agent tools (TOOL_SCHEMAS / call_tool). Outbound ones (send_email,
    delete_event, create_event) are listed in CONFIRM_TOOLS; the prompter
    routes them through the HITL approval flow.

Mail and calendar text is attacker-controlled (anyone can email you or
invite you to an event), so everything that reaches the model is cleaned
and any look-alike provenance markers are defused. The prompter wraps tool
results in [FROM EMAIL] / [FROM CALENDAR] markers.
"""

from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time as dtime, timedelta
from email.message import EmailMessage
from email.utils import parseaddr
from html.parser import HTMLParser
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from constants import DATA_ROOT
from modules.log_hub import emit as hub_emit

CRED_DIR = os.path.join(DATA_ROOT, "credentials")
WS_DIR = os.path.join(DATA_ROOT, "workspace")
CACHE_PATH = os.path.join(WS_DIR, "cache.json")

EVENTS_TTL = 300        # seconds before cached events count as stale
MAIL_TTL = 120
EVENT_WINDOW_DAYS = 30  # what the cache holds; callers can ask for fewer
MAIL_LIMIT = 30
MAX_RANGE_DAYS = 400     # one year view, with slack
MAX_CACHED_RANGES = 12
MAX_BODY_CHARS = 6000
MAX_RECIPIENTS = 10
MAX_SENDS_PER_HOUR = 10

GOOGLE_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]
_SCOPE_KEYS = {
    "calendar": "https://www.googleapis.com/auth/calendar.events",
    "gmail": "https://www.googleapis.com/auth/gmail.modify",
    "send": "https://www.googleapis.com/auth/gmail.send",
}
MS_SCOPES = "offline_access Mail.ReadWrite Mail.Send User.Read"
GRAPH = "https://graph.microsoft.com/v1.0"


class ProviderError(Exception):
    """A problem worth showing to the user as-is."""


class NotConnected(ProviderError):
    """The account isn't connected, or its sign-in expired."""


class InvalidInput(ProviderError, ValueError):
    """The user typed or pasted something unusable (a 400, not a 502)."""


# ───────────────────────────── text safety ─────────────────────────────

_MARKER_RE = re.compile(r"\[(END )?FROM ", re.I)
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏  ‪-‮⁦-⁩]")
_EMAIL_RE = re.compile(r"^[^@\s<>,;\"'()\[\]\\]+@[^@\s<>,;\"'()\[\]\\]+\.[^@\s<>,;\"'()\[\]\\]+$")


def neutralize(text: str) -> str:
    """Defuse text that imitates a provenance marker, so mail can't close
    its own [FROM EMAIL] block and talk to the model from outside it."""
    return _MARKER_RE.sub(lambda m: "(" + (m.group(1) or "") + "FROM ", text)


def clean_text(text: Any, limit: int = MAX_BODY_CHARS) -> str:
    s = _CTRL_RE.sub("", str(text or "")).replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    s = neutralize(s)
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def one_line(text: Any, limit: int = 120) -> str:
    return clean_text(" ".join(str(text or "").split()), limit)


class _TextExtractor(HTMLParser):
    _BLOCKS = {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: List[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self._skip += 1
        elif tag in self._BLOCKS:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head"):
            self._skip = max(0, self._skip - 1)
        elif tag in self._BLOCKS:
            self.out.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.out.append(data)


def html_to_text(markup: str) -> str:
    p = _TextExtractor()
    try:
        p.feed(markup)
        p.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", markup)
    return "".join(p.out)


def _atomic_write(path: str, text: str, private: bool = False) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{uuid.uuid4().hex[:6]}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    if private:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _parse_dt(value: str) -> Optional[datetime]:
    """ISO date or datetime -> aware local datetime (all-day = 00:00)."""
    if not value:
        return None
    try:
        if len(value) == 10:
            return datetime.combine(date.fromisoformat(value), dtime.min).astimezone()
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.astimezone()
    except ValueError:
        return None


# ───────────────────────────── Google account ──────────────────────────

class GoogleAccount:
    """OAuth client + token for Calendar and Gmail."""

    def __init__(self, cred_dir: str = CRED_DIR) -> None:
        self.client_path = os.path.join(cred_dir, "google_client.json")
        self.token_path = os.path.join(cred_dir, "google_token.json")
        self._lock = threading.RLock()
        self._pending: Dict[str, Tuple[Any, float]] = {}
        self._creds = None
        self.last_error: Optional[str] = None

    # client file ------------------------------------------------------
    def client_configured(self) -> bool:
        cfg = _read_json(self.client_path)
        return bool(cfg and self._client_block(cfg))

    @staticmethod
    def _client_block(cfg: dict) -> Optional[dict]:
        for key in ("installed", "web"):
            block = cfg.get(key)
            if isinstance(block, dict) and block.get("client_id") and block.get("client_secret"):
                return block
        return None

    def save_client(self, raw: str) -> str:
        try:
            cfg = json.loads(raw)
        except (TypeError, ValueError):
            raise InvalidInput("That isn't valid JSON. Paste the whole file you downloaded from Google Cloud.")
        if not isinstance(cfg, dict) or not self._client_block(cfg):
            raise InvalidInput("That JSON has no OAuth client in it. In Google Cloud, create an "
                               "OAuth client ID of type \"Desktop app\" and download its JSON.")
        _atomic_write(self.client_path, json.dumps(cfg), private=True)
        return "installed" if "installed" in cfg else "web"

    # token ------------------------------------------------------------
    def _info(self) -> Optional[dict]:
        return _read_json(self.token_path)

    def connected(self) -> bool:
        info = self._info()
        return bool(info and info.get("refresh_token"))

    def email(self) -> Optional[str]:
        return (self._info() or {}).get("email")

    def has(self, service: str) -> bool:
        scope = _SCOPE_KEYS[service]
        return scope in ((self._info() or {}).get("granted") or [])

    # sign-in ----------------------------------------------------------
    def begin(self, redirect_uri: str) -> str:
        from google_auth_oauthlib.flow import Flow
        cfg = _read_json(self.client_path)
        if not cfg or not self._client_block(cfg):
            raise NotConnected("Add your Google OAuth client first (see Settings → Connections).")
        flow = Flow.from_client_config(cfg, scopes=GOOGLE_SCOPES, redirect_uri=redirect_uri,
                                       autogenerate_code_verifier=True)
        url, state = flow.authorization_url(access_type="offline", prompt="consent",
                                            include_granted_scopes="true")
        with self._lock:
            cutoff = time.time() - 600
            self._pending = {k: v for k, v in self._pending.items() if v[1] > cutoff}
            self._pending[state] = (flow, time.time())
        return url

    def finish(self, state: str, code: str) -> Dict[str, Any]:
        with self._lock:
            entry = self._pending.pop(state, None)
        if entry is None:
            raise ProviderError("That sign-in link expired or wasn't started here. Start again from Settings.")
        flow = entry[0]
        os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")   # user may untick scopes
        try:
            flow.fetch_token(code=code)
        except Exception as e:
            raise ProviderError(f"Google rejected the sign-in: {e}")
        creds = flow.credentials
        granted = list(getattr(creds, "granted_scopes", None) or creds.scopes or [])
        if not creds.refresh_token:
            raise ProviderError("Google didn't return a refresh token. Remove Lumi under "
                                "myaccount.google.com/permissions and connect again.")
        email = self._fetch_email(creds)
        self._write(creds, email, granted)
        self.last_error = None
        return {"email": email, "granted": granted}

    @staticmethod
    def _fetch_email(creds) -> Optional[str]:
        try:
            r = requests.get("https://openidconnect.googleapis.com/v1/userinfo",
                             headers={"Authorization": f"Bearer {creds.token}"}, timeout=10)
            return r.json().get("email") if r.ok else None
        except (requests.RequestException, ValueError):
            return None

    def _write(self, creds, email: Optional[str], granted: List[str]) -> None:
        info = json.loads(creds.to_json())
        info["email"] = email
        info["granted"] = granted
        with self._lock:
            _atomic_write(self.token_path, json.dumps(info), private=True)
            self._creds = creds

    def credentials(self):
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        with self._lock:
            info = self._info()
            if not info or not info.get("refresh_token"):
                raise NotConnected("Google isn't connected. Connect it in Settings → Connections.")
            creds = self._creds or Credentials.from_authorized_user_info(info)
            if not creds.valid:
                try:
                    creds.refresh(Request())
                except RefreshError as e:
                    self.last_error = "Google sign-in expired. Reconnect it in Settings → Connections."
                    raise NotConnected(self.last_error) from e
                except requests.RequestException as e:
                    raise ProviderError("Couldn't reach Google to refresh the sign-in.") from e
                self._write(creds, info.get("email"), info.get("granted") or [])
            self._creds = creds
            return creds

    def service(self, name: str, version: str):
        import google_auth_httplib2
        import httplib2
        from googleapiclient.discovery import build
        creds = self.credentials()
        http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=20))
        return build(name, version, http=http, cache_discovery=False)

    def run(self, fn: Callable[[], Any]) -> Any:
        """Call into the Google client; turn its failures into ProviderErrors."""
        import httplib2
        from google.auth.exceptions import RefreshError
        from googleapiclient.errors import HttpError
        try:
            return fn()
        except HttpError as e:
            raise self._map_http_error(e)
        except RefreshError as e:
            self.last_error = "Google sign-in expired. Reconnect it in Settings → Connections."
            raise NotConnected(self.last_error) from e
        except (httplib2.HttpLib2Error, OSError) as e:
            raise ProviderError(f"Couldn't reach Google ({type(e).__name__}).") from e

    @staticmethod
    def _map_http_error(e) -> ProviderError:
        status = getattr(e.resp, "status", 0)
        reason = ""
        try:
            reason = json.loads(e.content.decode("utf-8", "replace"))["error"]["errors"][0]["reason"]
        except Exception:
            pass
        if status == 401:
            return NotConnected("Google sign-in expired. Reconnect it in Settings → Connections.")
        if reason in ("accessNotConfigured", "SERVICE_DISABLED") or "has not been used" in str(e):
            return ProviderError("Enable the Gmail API and the Google Calendar API in your Google "
                                 "Cloud project (APIs & Services → Library), then try again.")
        if status == 403:
            return ProviderError(f"Google refused the request ({reason or 'forbidden'}). "
                                 "Check that every permission was ticked when you connected.")
        if status == 404:
            return ProviderError("Google couldn't find that item.")
        if status == 429:
            return ProviderError("Google is rate-limiting requests. Try again in a minute.")
        return ProviderError(f"Google returned an error ({status}).")

    def disconnect(self) -> None:
        info = self._info()
        if info and info.get("refresh_token"):
            try:
                requests.post("https://oauth2.googleapis.com/revoke",
                              params={"token": info["refresh_token"]}, timeout=10)
            except requests.RequestException:
                pass            # still remove it locally
        with self._lock:
            self._creds = None
            self.last_error = None
            try:
                os.remove(self.token_path)
            except OSError:
                pass


# ───────────────────────────── Microsoft account ───────────────────────

_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_TENANT_RE = re.compile(r"^[A-Za-z0-9.\-]{1,100}$")


def ms_error_message(payload: dict) -> str:
    """Turn an Azure AD error into something actionable."""
    desc = str(payload.get("error_description") or payload.get("error") or "unknown error")
    code = re.search(r"AADSTS(\d+)", desc)
    n = code.group(1) if code else ""
    if n in ("65001", "90094", "65002", "900971", "53003", "50105", "50020"):
        return ("KSU's Microsoft 365 tenant needs administrator approval for this app, or blocks "
                "outside apps. Use the Gmail-label fallback instead: forward your KSU mail to "
                "Gmail and label it.")
    if n == "700016":
        return "Microsoft doesn't recognise that Application (client) ID. Check it in Azure."
    if n in ("7000218", "70002"):
        return ("The app registration isn't set up as a public client. In Azure, open the app → "
                "Authentication → turn on \"Allow public client flows\".")
    if payload.get("error") == "authorization_declined":
        return "Sign-in was declined."
    if payload.get("error") == "expired_token":
        return "The sign-in code expired. Start again."
    return one_line(desc, 240)


class MicrosoftAccount:
    """Device-code sign-in for Microsoft Graph (public client)."""

    def __init__(self, cred_dir: str = CRED_DIR) -> None:
        self.config_path = os.path.join(cred_dir, "microsoft.json")
        self.token_path = os.path.join(cred_dir, "microsoft_token.json")
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._device: Dict[str, Any] = {"status": "idle"}
        self.last_error: Optional[str] = None

    def config(self) -> Dict[str, str]:
        cfg = _read_json(self.config_path) or {}
        return {"client_id": cfg.get("client_id") or "", "tenant": cfg.get("tenant") or "organizations"}

    def save_config(self, client_id: str, tenant: str) -> None:
        client_id = (client_id or "").strip()
        tenant = (tenant or "organizations").strip() or "organizations"
        if not _GUID_RE.match(client_id):
            raise InvalidInput("The Application (client) ID should look like "
                               "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx.")
        if not _TENANT_RE.match(tenant):
            raise InvalidInput("The tenant should be \"organizations\", \"common\", or a domain/ID.")
        _atomic_write(self.config_path, json.dumps({"client_id": client_id, "tenant": tenant}), private=True)

    def _tokens(self) -> Optional[dict]:
        return _read_json(self.token_path)

    def connected(self) -> bool:
        t = self._tokens()
        return bool(t and t.get("refresh_token"))

    def account(self) -> Optional[str]:
        return (self._tokens() or {}).get("account")

    def _url(self, leg: str) -> str:
        return f"https://login.microsoftonline.com/{self.config()['tenant']}/oauth2/v2.0/{leg}"

    # device flow ------------------------------------------------------
    def start_device_flow(self) -> Dict[str, Any]:
        cfg = self.config()
        if not cfg["client_id"]:
            raise InvalidInput("Enter the Application (client) ID first.")
        try:
            r = requests.post(self._url("devicecode"),
                              data={"client_id": cfg["client_id"], "scope": MS_SCOPES}, timeout=15)
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            raise ProviderError("Couldn't reach Microsoft to start the sign-in.") from e
        if not r.ok or "device_code" not in data:
            raise ProviderError(ms_error_message(data))
        self._cancel.set()                      # stop any earlier attempt
        self._cancel = cancel = threading.Event()
        state = {
            "status": "pending",
            "user_code": data["user_code"],
            "verification_uri": data.get("verification_uri") or "https://microsoft.com/devicelogin",
            "expires_at": time.time() + int(data.get("expires_in", 900)),
        }
        with self._lock:
            self._device = state
        threading.Thread(
            target=self._poll, name="ms-device-login", daemon=True,
            args=(data["device_code"], max(1, int(data.get("interval", 5))), cfg, cancel),
        ).start()
        return dict(state)

    def _poll(self, device_code: str, interval: int, cfg: dict, cancel: threading.Event) -> None:
        deadline = self._device["expires_at"]
        while not cancel.wait(interval):
            if time.time() > deadline:
                self._set_device("error", error="The sign-in code expired. Start again.")
                return
            try:
                r = requests.post(self._url("token"), timeout=15, data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id": cfg["client_id"], "device_code": device_code})
                data = r.json()
            except (requests.RequestException, ValueError):
                continue                        # transient; keep trying until expiry
            if r.ok and "access_token" in data:
                self._save_tokens(data)
                self._set_device("ok")
                return
            err = data.get("error")
            if err == "authorization_pending":
                continue
            if err == "slow_down":
                interval += 5
                continue
            self._set_device("error", error=ms_error_message(data))
            return

    def _set_device(self, status: str, **extra) -> None:
        with self._lock:
            if self._device.get("status") == "pending" or status == "pending":
                self._device = {**self._device, "status": status, **extra}
        if status == "error":
            self.last_error = extra.get("error")

    def device_state(self) -> Dict[str, Any]:
        with self._lock:
            d = dict(self._device)
        d.pop("device_code", None)
        return d

    def cancel(self) -> None:
        self._cancel.set()
        with self._lock:
            self._device = {"status": "idle"}

    # tokens -----------------------------------------------------------
    def _save_tokens(self, data: dict) -> None:
        old = self._tokens() or {}
        tokens = {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token") or old.get("refresh_token"),
            "expires_at": time.time() + int(data.get("expires_in", 3600)) - 60,
            "account": old.get("account"),
        }
        if not tokens["account"]:
            try:
                me = requests.get(f"{GRAPH}/me?$select=mail,userPrincipalName",
                                  headers={"Authorization": f"Bearer {tokens['access_token']}"},
                                  timeout=10).json()
                tokens["account"] = me.get("mail") or me.get("userPrincipalName")
            except (requests.RequestException, ValueError):
                pass
        _atomic_write(self.token_path, json.dumps(tokens), private=True)

    def access_token(self) -> str:
        with self._lock:
            t = self._tokens()
            if not t or not t.get("refresh_token"):
                raise NotConnected("KSU mail isn't connected. Connect it in Settings → Connections.")
            if t.get("access_token") and time.time() < t.get("expires_at", 0):
                return t["access_token"]
            cfg = self.config()
            try:
                r = requests.post(self._url("token"), timeout=15, data={
                    "grant_type": "refresh_token", "client_id": cfg["client_id"],
                    "refresh_token": t["refresh_token"], "scope": MS_SCOPES})
                data = r.json()
            except (requests.RequestException, ValueError) as e:
                raise ProviderError("Couldn't reach Microsoft to refresh the sign-in.") from e
            if not r.ok or "access_token" not in data:
                self.last_error = "KSU sign-in expired. Reconnect it in Settings → Connections."
                raise NotConnected(self.last_error)
            self._save_tokens(data)
            return data["access_token"]

    def disconnect(self) -> None:
        self.cancel()
        self.last_error = None
        try:
            os.remove(self.token_path)
        except OSError:
            pass


# ───────────────────────────── Mail providers ──────────────────────────

class MailProvider:
    """What the hub needs from a mailbox. Messages are plain dicts:
    id, thread_id, provider, from, from_name, from_addr, to, subject,
    snippet, date (ISO), ts (epoch), unread, count."""

    id = ""
    label = ""
    can_send = True

    def connected(self) -> bool: raise NotImplementedError
    def list_recent(self, limit: int) -> List[dict]: raise NotImplementedError
    def get_message(self, mid: str) -> dict: raise NotImplementedError
    def set_read(self, mid: str, read: bool) -> None: raise NotImplementedError
    def archive(self, mid: str) -> None: raise NotImplementedError
    def send(self, to: List[str], cc: List[str], subject: str, body: str,
             reply_to: Optional[str] = None) -> None: raise NotImplementedError


def _b64url_decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")


def _walk_parts(payload: dict):
    yield payload
    for p in payload.get("parts") or []:
        yield from _walk_parts(p)


def gmail_body_text(payload: dict) -> str:
    plain, markup = [], []
    for part in _walk_parts(payload or {}):
        if (part.get("filename") or "").strip():
            continue                              # attachment
        data = (part.get("body") or {}).get("data")
        if not data:
            continue
        mime = part.get("mimeType", "")
        if mime == "text/plain":
            plain.append(_b64url_decode(data))
        elif mime == "text/html":
            markup.append(_b64url_decode(data))
    if plain:
        return "\n".join(plain)
    return html_to_text("\n".join(markup)) if markup else ""


def _headers(msg: dict) -> Dict[str, str]:
    return {h["name"].lower(): h["value"] for h in (msg.get("payload") or {}).get("headers", [])
            if "name" in h and "value" in h}


def _summary(provider: str, mid: str, thread_id: str, frm: str, to: str, subject: str,
             snippet: str, ts: float, unread: bool, count: int) -> dict:
    name, addr = parseaddr(frm or "")
    return {
        "id": mid, "thread_id": thread_id, "provider": provider,
        "from": one_line(frm, 120), "from_name": one_line(name or addr or "Unknown", 80),
        "from_addr": one_line(addr, 120), "to": one_line(to, 200),
        "subject": one_line(subject or "(no subject)", 200),
        "snippet": one_line(snippet, 200),
        "date": datetime.fromtimestamp(ts).astimezone().isoformat(timespec="minutes") if ts else "",
        "ts": ts, "unread": bool(unread), "count": count,
    }


def gmail_thread_summary(provider: str, thread: dict) -> dict:
    msgs = thread.get("messages") or []
    last = msgs[-1] if msgs else {}
    h = _headers(last)
    unread = any("UNREAD" in (m.get("labelIds") or []) for m in msgs)
    ts = int(last.get("internalDate") or 0) / 1000.0
    return _summary(provider, thread["id"], thread["id"], h.get("from", ""), h.get("to", ""),
                    h.get("subject", ""), last.get("snippet") or thread.get("snippet", ""),
                    ts, unread, len(msgs))


def gmail_thread_full(provider: str, thread: dict) -> dict:
    out = gmail_thread_summary(provider, thread)
    msgs = []
    for m in (thread.get("messages") or [])[-6:]:
        h = _headers(m)
        msgs.append({
            "from": one_line(h.get("from", ""), 120),
            "date": one_line(h.get("date", ""), 60),
            "body": clean_text(gmail_body_text(m.get("payload") or {}) or m.get("snippet", ""), MAX_BODY_CHARS),
        })
    out["messages"] = msgs
    last_h = _headers((thread.get("messages") or [{}])[-1])
    out["message_id"] = one_line(last_h.get("message-id", ""), 200)
    out["cc"] = one_line(last_h.get("cc", ""), 200)
    return out


class GmailProvider(MailProvider):
    """Gmail, optionally restricted to (or excluding) one label. With a
    label it is the KSU fallback; with exclude_label it is the plain Gmail
    view that leaves the KSU-labelled mail to the KSU provider."""

    def __init__(self, account: GoogleAccount, provider_id: str = "gmail",
                 label: Optional[str] = None, exclude_label: Optional[str] = None) -> None:
        self.account = account
        self.id = provider_id
        self.label = "KSU" if provider_id == "ksu" else "Gmail"
        self.only_label = label
        self.exclude_label = exclude_label
        self.can_send = provider_id != "ksu"          # sending would not come from the KSU address

    def connected(self) -> bool:
        return self.account.connected() and self.account.has("gmail")

    @staticmethod
    def _query_label(name: str) -> str:
        return re.sub(r"[\s/]+", "-", name.strip())

    def _label_id(self, svc, name: str) -> str:
        labels = self.account.run(lambda: svc.users().labels().list(userId="me").execute()).get("labels", [])
        for lab in labels:
            if lab.get("name", "").lower() == name.lower():
                return lab["id"]
        raise ProviderError(f"There's no Gmail label called \"{name}\". Create it (and a filter that "
                            "applies it to your forwarded KSU mail) in Gmail first.")

    def list_recent(self, limit: int) -> List[dict]:
        svc = self.account.service("gmail", "v1")
        label_ids = ["INBOX"]
        if self.only_label:
            label_ids.append(self._label_id(svc, self.only_label))
        q = f"-label:{self._query_label(self.exclude_label)}" if self.exclude_label else None
        ids = self.account.run(lambda: svc.users().threads().list(
            userId="me", labelIds=label_ids, q=q, maxResults=limit).execute()).get("threads", [])
        if not ids:
            return []
        results: Dict[str, dict] = {}
        failures: List[Exception] = []

        def got(req_id, resp, exc):
            if exc is not None:
                failures.append(exc)
            else:
                results[resp["id"]] = resp

        batch = svc.new_batch_http_request(callback=got)
        for t in ids[:limit]:
            batch.add(svc.users().threads().get(
                userId="me", id=t["id"], format="metadata",
                metadataHeaders=["From", "To", "Subject", "Date"]))
        self.account.run(batch.execute)
        if failures and not results:
            raise ProviderError("Gmail didn't return your messages. Try again.")
        out = [gmail_thread_summary(self.id, results[t["id"]]) for t in ids if t["id"] in results]
        return sorted(out, key=lambda m: m["ts"], reverse=True)

    def get_message(self, mid: str) -> dict:
        svc = self.account.service("gmail", "v1")
        thread = self.account.run(lambda: svc.users().threads().get(
            userId="me", id=mid, format="full").execute())
        return gmail_thread_full(self.id, thread)

    def _modify(self, mid: str, add: List[str], remove: List[str]) -> None:
        svc = self.account.service("gmail", "v1")
        self.account.run(lambda: svc.users().threads().modify(
            userId="me", id=mid, body={"addLabelIds": add, "removeLabelIds": remove}).execute())

    def set_read(self, mid: str, read: bool) -> None:
        self._modify(mid, [] if read else ["UNREAD"], ["UNREAD"] if read else [])

    def archive(self, mid: str) -> None:
        self._modify(mid, [], ["INBOX"])

    def send(self, to, cc, subject, body, reply_to=None) -> None:
        if not self.can_send:
            raise ProviderError("Mail can't be sent as KSU through the Gmail fallback. Reply from "
                                "your KSU mailbox, or connect KSU through Microsoft Graph.")
        if not self.account.has("send"):
            raise ProviderError("Gmail sending wasn't granted. Reconnect Google and tick every permission.")
        msg = EmailMessage()
        msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg["Subject"] = subject
        thread_id = None
        if reply_to:
            orig = self.get_message(reply_to)
            thread_id = reply_to
            if orig.get("message_id"):
                msg["In-Reply-To"] = orig["message_id"]
                msg["References"] = orig["message_id"]
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        payload = {"raw": raw}
        if thread_id:
            payload["threadId"] = thread_id
        svc = self.account.service("gmail", "v1")
        self.account.run(lambda: svc.users().messages().send(userId="me", body=payload).execute())


def graph_summary(m: dict) -> dict:
    frm = (m.get("from") or {}).get("emailAddress") or {}
    from_s = f'{frm.get("name", "")} <{frm.get("address", "")}>'.strip() if frm.get("address") else frm.get("name", "")
    to_s = ", ".join((r.get("emailAddress") or {}).get("address", "") for r in m.get("toRecipients") or [])
    ts = 0.0
    when = _parse_dt(m.get("receivedDateTime") or "")
    if when:
        ts = when.timestamp()
    return _summary("ksu", m["id"], m.get("conversationId") or m["id"], from_s, to_s,
                    m.get("subject", ""), m.get("bodyPreview", ""), ts, not m.get("isRead", True), 1)


class GraphMailProvider(MailProvider):
    id = "ksu"
    label = "KSU"

    def __init__(self, account: MicrosoftAccount) -> None:
        self.account = account

    def connected(self) -> bool:
        return self.account.connected()

    def _call(self, method: str, path: str, *, retry: bool = True, **kw) -> Any:
        try:
            r = requests.request(
                method, f"{GRAPH}{path}", timeout=20,
                headers={"Authorization": f"Bearer {self.account.access_token()}", **kw.pop("headers", {})},
                **kw)
        except requests.RequestException as e:
            raise ProviderError("Couldn't reach Microsoft Graph.") from e
        if r.status_code == 401 and retry:
            self.account._tokens() and self._expire()
            return self._call(method, path, retry=False, **kw)
        if r.status_code == 401:
            raise NotConnected("KSU sign-in expired. Reconnect it in Settings → Connections.")
        if r.status_code == 403:
            raise ProviderError("Microsoft refused access. The tenant may have blocked the "
                                "mail permissions; use the Gmail-label fallback.")
        if r.status_code == 404:
            raise ProviderError("Microsoft couldn't find that message.")
        if r.status_code == 429:
            raise ProviderError("Microsoft is rate-limiting requests. Try again in a minute.")
        if not r.ok:
            raise ProviderError(f"Microsoft Graph returned an error ({r.status_code}).")
        return r.json() if r.content else {}

    def _expire(self) -> None:
        t = self.account._tokens()
        if t:
            t["expires_at"] = 0
            _atomic_write(self.account.token_path, json.dumps(t), private=True)

    def list_recent(self, limit: int) -> List[dict]:
        data = self._call("GET", "/me/mailFolders/inbox/messages", params={
            "$top": limit, "$orderby": "receivedDateTime desc",
            "$select": "id,conversationId,subject,from,toRecipients,receivedDateTime,isRead,bodyPreview"})
        return [graph_summary(m) for m in data.get("value", [])]

    def get_message(self, mid: str) -> dict:
        m = self._call("GET", f"/me/messages/{requests.utils.quote(mid, safe='')}",
                       headers={"Prefer": 'outlook.body-content-type="text"'},
                       params={"$select": "id,conversationId,subject,from,toRecipients,ccRecipients,"
                                          "receivedDateTime,isRead,bodyPreview,body,internetMessageId"})
        out = graph_summary(m)
        body = (m.get("body") or {})
        text = body.get("content", "")
        if body.get("contentType") == "html":
            text = html_to_text(text)
        out["messages"] = [{"from": out["from"], "date": out["date"],
                            "body": clean_text(text or m.get("bodyPreview", ""), MAX_BODY_CHARS)}]
        out["message_id"] = one_line(m.get("internetMessageId", ""), 200)
        out["cc"] = ", ".join((r.get("emailAddress") or {}).get("address", "") for r in m.get("ccRecipients") or [])
        return out

    def set_read(self, mid: str, read: bool) -> None:
        self._call("PATCH", f"/me/messages/{requests.utils.quote(mid, safe='')}", json={"isRead": read})

    def archive(self, mid: str) -> None:
        self._call("POST", f"/me/messages/{requests.utils.quote(mid, safe='')}/move",
                   json={"destinationId": "archive"})

    def send(self, to, cc, subject, body, reply_to=None) -> None:
        def addrs(xs): return [{"emailAddress": {"address": a}} for a in xs]
        self._call("POST", "/me/sendMail", json={"saveToSentItems": True, "message": {
            "subject": subject, "body": {"contentType": "Text", "content": body},
            "toRecipients": addrs(to), "ccRecipients": addrs(cc)}})


# ───────────────────────────── Calendar ────────────────────────────────

def norm_event(e: dict) -> dict:
    s, en = e.get("start") or {}, e.get("end") or {}
    attendees = [{"name": one_line(a.get("displayName", ""), 60), "email": one_line(a.get("email", ""), 120),
                  "response": a.get("responseStatus", "")} for a in (e.get("attendees") or [])[:20]]
    return {
        "id": e.get("id", ""),
        "title": one_line(e.get("summary") or "(no title)", 200),
        "start": s.get("dateTime") or s.get("date") or "",
        "end": en.get("dateTime") or en.get("date") or "",
        "all_day": "date" in s,
        "location": one_line(e.get("location", ""), 200),
        "description": clean_text(e.get("description", ""), 1500),
        "attendees": attendees,
        "link": e.get("htmlLink", ""),
        "status": e.get("status", ""),
    }


class GoogleCalendar:
    def __init__(self, account: GoogleAccount) -> None:
        self.account = account

    def connected(self) -> bool:
        return self.account.connected() and self.account.has("calendar")

    def list_events(self, days: int = EVENT_WINDOW_DAYS, limit: int = 100) -> List[dict]:
        svc = self.account.service("calendar", "v3")
        now = datetime.now().astimezone()
        res = self.account.run(lambda: svc.events().list(
            calendarId="primary", timeMin=now.isoformat(), timeMax=(now + timedelta(days=days)).isoformat(),
            singleEvents=True, orderBy="startTime", maxResults=limit).execute())
        return [norm_event(e) for e in res.get("items", []) if e.get("status") != "cancelled"]

    def list_range(self, start: date, end: date, pages: int = 4) -> List[dict]:
        """Events overlapping [start, end) (dates, local time). Up to
        pages x 250 events; a year of a busy calendar fits."""
        svc = self.account.service("calendar", "v3")
        tmin = datetime.combine(start, dtime.min).astimezone().isoformat()
        tmax = datetime.combine(end, dtime.min).astimezone().isoformat()
        items: List[dict] = []
        token = None
        for _ in range(pages):
            res = self.account.run(lambda: svc.events().list(
                calendarId="primary", timeMin=tmin, timeMax=tmax, singleEvents=True,
                orderBy="startTime", maxResults=250, pageToken=token).execute())
            items += res.get("items", [])
            token = res.get("nextPageToken")
            if not token:
                break
        return [norm_event(e) for e in items if e.get("status") != "cancelled"]

    def get_event(self, event_id: str) -> dict:
        svc = self.account.service("calendar", "v3")
        return norm_event(self.account.run(lambda: svc.events().get(calendarId="primary", eventId=event_id).execute()))

    def create_event(self, body: dict, notify: bool) -> dict:
        svc = self.account.service("calendar", "v3")
        res = self.account.run(lambda: svc.events().insert(
            calendarId="primary", body=body, sendUpdates="all" if notify else "none").execute())
        return norm_event(res)

    def delete_event(self, event_id: str) -> None:
        svc = self.account.service("calendar", "v3")
        self.account.run(lambda: svc.events().delete(calendarId="primary", eventId=event_id).execute())


def _overlaps(ev: dict, lo: datetime, hi: datetime) -> bool:
    """Does the event overlap [lo, hi)? (All-day ends are exclusive dates.)"""
    start = _parse_dt(ev.get("start") or "")
    if start is None:
        return False
    end = _parse_dt(ev.get("end") or "") or start
    return start < hi and (end > lo or start >= lo)


def _is_naive(value: str) -> bool:
    """True for '2026-11-17T15:00' (no offset or Z)."""
    return not re.search(r"(Z|[+-]\d\d:?\d\d)$", value or "")


def build_event_body(title: str, start: str, end: Optional[str], all_day: bool, location: str,
                     description: str, attendees: List[str], timezone: Optional[str]) -> dict:
    title = (title or "").strip()
    if not title:
        raise ValueError("An event needs a title.")
    if not start:
        raise ValueError("An event needs a start.")
    body: Dict[str, Any] = {"summary": title[:200]}
    if location:
        body["location"] = location[:200]
    if description:
        body["description"] = description[:2000]
    if all_day:
        try:
            d0 = date.fromisoformat(start[:10])
            d1 = date.fromisoformat((end or start)[:10])
        except ValueError:
            raise ValueError("Dates should look like 2026-11-17.")
        if d1 < d0:
            raise ValueError("The event ends before it starts.")
        body["start"] = {"date": d0.isoformat()}
        body["end"] = {"date": (d1 + timedelta(days=1)).isoformat()}   # end date is exclusive
    else:
        s = _parse_dt(start)
        if s is None:
            raise ValueError("Start should look like 2026-11-17T15:00.")
        e = _parse_dt(end) if end else s + timedelta(hours=1)
        if e is None:
            raise ValueError("End should look like 2026-11-17T16:00.")
        if e <= s:
            raise ValueError("The event ends before it starts.")
        if timezone and _is_naive(start) and (not end or _is_naive(end)):
            # Wall-clock times in the user's own zone (the browser sends it).
            s_naive = datetime.fromisoformat(start[:19])
            e_naive = datetime.fromisoformat(end[:19]) if end else s_naive + timedelta(hours=1)
            body["start"] = {"dateTime": s_naive.isoformat(), "timeZone": timezone}
            body["end"] = {"dateTime": e_naive.isoformat(), "timeZone": timezone}
        else:
            body["start"] = {"dateTime": s.isoformat()}
            body["end"] = {"dateTime": e.isoformat()}
    if attendees:
        body["attendees"] = [{"email": a} for a in attendees]
    return body


# ───────────────────────────── Agent tools ─────────────────────────────

def _tool(name: str, description: str, props: Dict[str, Any], required: List[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props, "required": required}}}


_ACCOUNT = {"type": "string", "enum": ["gmail", "ksu"], "description": "Which mailbox."}

TOOL_SCHEMAS = [
    _tool("list_events", "List the user's upcoming Google Calendar events.",
          {"days": {"type": "integer", "description": "How many days ahead (1-30, default 7)."}}, []),
    _tool("create_event", "Add an event to the user's Google Calendar. Asks the user first.",
          {"title": {"type": "string"},
           "start": {"type": "string", "description": "2026-11-17T15:00 (local time), or 2026-11-17 if all_day."},
           "end": {"type": "string", "description": "Same format. Defaults to one hour later."},
           "all_day": {"type": "boolean"}, "location": {"type": "string"},
           "attendees": {"type": "array", "items": {"type": "string"}, "description": "Email addresses to invite."}},
          ["title", "start"]),
    _tool("delete_event", "Delete a calendar event by id (from list_events). Asks the user first.",
          {"event_id": {"type": "string"}}, ["event_id"]),
    _tool("list_emails", "List recent inbox messages (newest first).",
          {"account": {"type": "string", "enum": ["all", "gmail", "ksu"]},
           "unread_only": {"type": "boolean"}, "limit": {"type": "integer", "description": "1-15, default 10."}}, []),
    _tool("read_email", "Read one email thread by id (from list_emails).",
          {"account": _ACCOUNT, "id": {"type": "string"}}, ["account", "id"]),
    _tool("send_email", "Send an email. Asks the user first. Draft in chat and let the user "
                        "approve the wording before calling this.",
          {"account": _ACCOUNT, "to": {"type": "array", "items": {"type": "string"}},
           "subject": {"type": "string"}, "body": {"type": "string"},
           "cc": {"type": "array", "items": {"type": "string"}},
           "reply_to_id": {"type": "string", "description": "Thread id to reply within."}},
          ["to", "subject", "body"]),
    _tool("mark_email_read", "Mark an email thread read or unread.",
          {"account": _ACCOUNT, "id": {"type": "string"}, "read": {"type": "boolean"}}, ["account", "id"]),
    _tool("archive_email", "Archive an email thread (remove it from the inbox).",
          {"account": _ACCOUNT, "id": {"type": "string"}}, ["account", "id"]),
]
TOOL_NAMES = {t["function"]["name"] for t in TOOL_SCHEMAS}
CONFIRM_TOOLS = {"send_email", "delete_event", "create_event"}


def _addr_list(value: Any) -> List[str]:
    if value is None or value == "":
        return []
    items = [value] if isinstance(value, str) else list(value)
    out: List[str] = []
    for it in items:
        for part in re.split(r"[,;]", str(it)):
            part = part.strip()
            if not part:
                continue
            addr = parseaddr(part)[1] or part
            if not _EMAIL_RE.match(addr):
                raise ValueError(f"'{part[:60]}' isn't a valid email address.")
            out.append(addr)
    return out


# ───────────────────────────── The hub ─────────────────────────────────

class WorkspaceHub:
    def __init__(self, signals=None, *, google: Optional[GoogleAccount] = None,
                 microsoft: Optional[MicrosoftAccount] = None, cred_dir: str = CRED_DIR,
                 cache_path: str = CACHE_PATH) -> None:
        self.signals = signals
        self.google = google or GoogleAccount(cred_dir)
        self.microsoft = microsoft or MicrosoftAccount(cred_dir)
        self.calendar = GoogleCalendar(self.google)
        self._ksu_path = os.path.join(cred_dir, "ksu.json")
        self._cache_path = cache_path
        self._lock = threading.RLock()
        self._pool: Optional[ThreadPoolExecutor] = None
        self._refreshing: set = set()
        self._listeners: List[Callable[[dict], None]] = []
        self._last_snapshot: Optional[str] = None
        self._sends: deque = deque()
        self._providers: Dict[str, MailProvider] = {}      # tests may inject
        self._ranges: Dict[str, dict] = {}                 # month/year views; memory only
        self._cache: Dict[str, dict] = (_read_json(cache_path) or {})
        if signals is not None:
            signals.workspace_hub = self

    # -- configuration -------------------------------------------------
    def _ksu(self) -> Dict[str, Any]:
        cfg = _read_json(self._ksu_path) or {}
        return {"mode": cfg.get("mode") if cfg.get("mode") in ("graph", "gmail") else None,
                "label": cfg.get("label") or "KSU"}

    def set_ksu_gmail(self, label: str) -> None:
        label = (label or "").strip()
        if not label or len(label) > 100 or re.search(r"[\"\\]", label):
            raise InvalidInput("Enter the name of the Gmail label your KSU mail gets.")
        if not self.google.connected():
            raise NotConnected("Connect Google first; the fallback reads KSU mail from Gmail.")
        _atomic_write(self._ksu_path, json.dumps({"mode": "gmail", "label": label}), private=True)
        self._drop_cache("mail:gmail", "mail:ksu")
        self._log(f"KSU mail set to Gmail label '{label}'")

    def set_ksu_graph(self, client_id: str, tenant: str) -> Dict[str, Any]:
        self.microsoft.save_config(client_id, tenant)
        state = self.microsoft.start_device_flow()
        cfg = self._ksu()
        _atomic_write(self._ksu_path, json.dumps({"mode": "graph", "label": cfg["label"]}), private=True)
        self._drop_cache("mail:gmail", "mail:ksu")
        return state

    def disconnect_ksu(self) -> None:
        self.microsoft.disconnect()
        try:
            os.remove(self._ksu_path)
        except OSError:
            pass
        self._drop_cache("mail:gmail", "mail:ksu")
        self._log("KSU mail disconnected")

    def disconnect_google(self) -> None:
        self.google.disconnect()
        if self._ksu()["mode"] == "gmail":
            try:
                os.remove(self._ksu_path)
            except OSError:
                pass
        self._drop_cache("events", "mail:gmail", "mail:ksu")
        self._log("Google disconnected")

    # -- providers -----------------------------------------------------
    def _provider(self, pid: str) -> Optional[MailProvider]:
        if pid in self._providers:
            return self._providers[pid]
        ksu = self._ksu()
        if pid == "gmail":
            p = GmailProvider(self.google, "gmail",
                              exclude_label=ksu["label"] if ksu["mode"] == "gmail" else None)
            return p if p.connected() else None
        if pid == "ksu":
            if ksu["mode"] == "graph":
                p = GraphMailProvider(self.microsoft)
            elif ksu["mode"] == "gmail":
                p = GmailProvider(self.google, "ksu", label=ksu["label"])
            else:
                return None
            return p if p.connected() else None
        return None

    def _require(self, pid: str) -> MailProvider:
        if pid not in ("gmail", "ksu"):
            raise ValueError("Unknown account. Use 'gmail' or 'ksu'.")
        p = self._provider(pid)
        if p is None:
            raise NotConnected(f"{'KSU mail' if pid == 'ksu' else 'Gmail'} isn't connected. "
                               "Connect it in Settings → Connections.")
        return p

    def any_connected(self) -> bool:
        return (self.calendar.connected() or self._provider("gmail") is not None
                or self._provider("ksu") is not None)

    # -- status (never includes secrets) -------------------------------
    def status(self) -> Dict[str, Any]:
        ksu = self._ksu()
        ms = self.microsoft.config()
        google_ok = self.google.connected()
        return {
            "google": {
                "client_configured": self.google.client_configured(),
                "connected": google_ok,
                "email": self.google.email() if google_ok else None,
                "services": {k: google_ok and self.google.has(k) for k in ("calendar", "gmail", "send")},
                "error": self.google.last_error,
            },
            "ksu": {
                "mode": ksu["mode"],
                "label": ksu["label"],
                "connected": self._provider("ksu") is not None,
                "account": self.microsoft.account() if ksu["mode"] == "graph" else self.google.email() if ksu["mode"] == "gmail" else None,
                "graph": {"client_id": ms["client_id"], "tenant": ms["tenant"],
                          "device": self.microsoft.device_state()},
                "error": self.microsoft.last_error,
            },
            "cache": {k: v.get("at") for k, v in self._cache.items()},
        }

    # -- cache ----------------------------------------------------------
    def _log(self, msg: str) -> None:
        if self.signals is not None:
            hub_emit(self.signals, "workspace", msg)

    def _executor(self) -> ThreadPoolExecutor:
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="workspace")
        return self._pool

    def _persist(self) -> None:
        try:
            _atomic_write(self._cache_path, json.dumps(self._cache), private=True)
        except OSError:
            pass

    def _store(self, key: str, data: Any) -> None:
        with self._lock:
            self._cache[key] = {"at": time.time(), "data": data}
            self._persist()
        self._notify()

    def _drop_cache(self, *keys: str) -> None:
        with self._lock:
            for k in keys:
                self._cache.pop(k, None)
            self._persist()
        self._notify()

    def _cached(self, key: str, ttl: float, fetch: Callable[[], Any], refresh: bool) -> Dict[str, Any]:
        """Serve the cache at once. Fetch synchronously only when there is
        nothing cached or the caller forces it; a stale cache is refreshed
        in the background and flagged."""
        entry = self._cache.get(key)
        if entry is None or refresh:
            data = fetch()
            self._store(key, data)
            return {"data": data, "at": self._cache[key]["at"], "stale": False, "refreshing": False}
        stale = time.time() - entry["at"] > ttl
        refreshing = False
        if stale:
            with self._lock:
                if key in self._refreshing:
                    refreshing = True
                else:
                    self._refreshing.add(key)
                    refreshing = True

                    def job():
                        try:
                            self._store(key, fetch())
                        except ProviderError:
                            pass                     # the next explicit load reports it
                        finally:
                            with self._lock:
                                self._refreshing.discard(key)
                    self._executor().submit(job)
        return {"data": entry["data"], "at": entry["at"], "stale": stale, "refreshing": refreshing}

    # -- calendar -------------------------------------------------------
    def get_events(self, days: int = 14, refresh: bool = False) -> Dict[str, Any]:
        if not self.calendar.connected():
            raise NotConnected("Google Calendar isn't connected. Connect it in Settings → Connections.")
        days = max(1, min(int(days), EVENT_WINDOW_DAYS))
        res = self._cached("events", EVENTS_TTL, lambda: self.calendar.list_events(EVENT_WINDOW_DAYS), refresh)
        horizon = datetime.now().astimezone() + timedelta(days=days)
        res["events"] = [e for e in res.pop("data")
                         if (_parse_dt(e["start"]) or horizon) <= horizon]
        return res

    def get_events_range(self, start: str, end: str, refresh: bool = False) -> Dict[str, Any]:
        """Events overlapping [start, end), dates as YYYY-MM-DD (the island's
        Day, Month and Year views). Served from the 30-day cache when the
        range sits inside it; otherwise fetched, kept in memory for a few
        minutes, and bounded to MAX_CACHED_RANGES so it can't grow."""
        try:
            s, e = date.fromisoformat(str(start)[:10]), date.fromisoformat(str(end)[:10])
        except ValueError:
            raise InvalidInput("Dates should look like 2026-11-17.")
        if e <= s or (e - s).days > MAX_RANGE_DAYS:
            raise InvalidInput(f"Ask for between 1 and {MAX_RANGE_DAYS} days.")
        if not self.calendar.connected():
            raise NotConnected("Google Calendar isn't connected. Connect it in Settings → Connections.")
        now = time.time()
        today = date.today()
        entry = self._cache.get("events")
        if (not refresh and entry and now - entry["at"] < EVENTS_TTL
                and s >= today and e <= today + timedelta(days=EVENT_WINDOW_DAYS)):
            lo = datetime.combine(s, dtime.min).astimezone()
            hi = datetime.combine(e, dtime.min).astimezone()
            return {"events": [ev for ev in entry["data"] if _overlaps(ev, lo, hi)], "at": entry["at"]}
        key = f"{s}:{e}"
        with self._lock:
            hit = self._ranges.get(key)
        if hit and not refresh and now - hit["at"] < EVENTS_TTL:
            return {"events": hit["data"], "at": hit["at"]}
        data = self.calendar.list_range(s, e)
        with self._lock:
            self._ranges[key] = {"at": now, "data": data}
            while len(self._ranges) > MAX_CACHED_RANGES:
                self._ranges.pop(min(self._ranges, key=lambda k: self._ranges[k]["at"]))
        return {"events": data, "at": now}

    def create_event(self, title: str, start: str, end: Optional[str] = None, all_day: bool = False,
                     location: str = "", description: str = "", attendees: Any = None,
                     timezone: Optional[str] = None) -> dict:
        if not self.calendar.connected():
            raise NotConnected("Google Calendar isn't connected. Connect it in Settings → Connections.")
        people = _addr_list(attendees)
        if len(people) > MAX_RECIPIENTS:
            raise ValueError(f"At most {MAX_RECIPIENTS} attendees.")
        body = build_event_body(title, start, end, bool(all_day), location, description, people, timezone)
        ev = self.calendar.create_event(body, notify=bool(people))
        self._ranges.clear()
        self._drop_cache("events")
        self._log("calendar event created")
        return ev

    def delete_event(self, event_id: str) -> None:
        if not self.calendar.connected():
            raise NotConnected("Google Calendar isn't connected. Connect it in Settings → Connections.")
        if not re.fullmatch(r"[A-Za-z0-9_\-]{5,1024}", event_id or ""):
            raise ValueError("That doesn't look like an event id. Use list_events to get one.")
        self.calendar.delete_event(event_id)
        self._ranges.clear()
        self._drop_cache("events")
        self._log("calendar event deleted")

    # -- mail -----------------------------------------------------------
    def get_mail(self, filter: str = "all", refresh: bool = False, limit: int = MAIL_LIMIT) -> Dict[str, Any]:
        wanted = ["gmail", "ksu"] if filter == "all" else [filter]
        if any(w not in ("gmail", "ksu") for w in wanted):
            raise ValueError("filter must be all, gmail or ksu")
        items: List[dict] = []
        errors: Dict[str, str] = {}
        at: Optional[float] = None
        stale = refreshing = False
        connected = 0
        for pid in wanted:
            p = self._provider(pid)
            if p is None:
                continue
            connected += 1
            try:
                res = self._cached(f"mail:{pid}", MAIL_TTL, lambda p=p: p.list_recent(MAIL_LIMIT), refresh)
            except ProviderError as e:
                errors[pid] = str(e)
                continue
            items += res["data"]
            at = res["at"] if at is None else min(at, res["at"])
            stale |= res["stale"]
            refreshing |= res["refreshing"]
        if not connected:
            raise NotConnected("No mail account is connected. Connect Gmail or KSU in Settings → Connections.")
        if errors and not items and len(errors) == connected:
            raise ProviderError(next(iter(errors.values())))
        items.sort(key=lambda m: m["ts"], reverse=True)
        return {"messages": items[:max(1, min(limit, 100))], "errors": errors, "at": at,
                "stale": stale, "refreshing": refreshing}

    def get_message(self, provider: str, mid: str) -> dict:
        return self._require(provider).get_message(mid)

    def set_read(self, provider: str, mid: str, read: bool) -> None:
        self._require(provider).set_read(mid, read)
        self._patch_cached(provider, mid, unread=not read)

    def archive(self, provider: str, mid: str) -> None:
        self._require(provider).archive(mid)
        self._patch_cached(provider, mid, remove=True)

    def _patch_cached(self, provider: str, mid: str, unread: Optional[bool] = None, remove: bool = False) -> None:
        with self._lock:
            entry = self._cache.get(f"mail:{provider}")
            if not entry:
                return
            if remove:
                entry["data"] = [m for m in entry["data"] if m["id"] != mid]
            else:
                for m in entry["data"]:
                    if m["id"] == mid:
                        m["unread"] = bool(unread)
            self._persist()
        self._notify()

    def send_email(self, account: str, to: Any, subject: str, body: str, cc: Any = None,
                   reply_to_id: Optional[str] = None) -> None:
        provider = self._require(account)
        recipients, ccs = _addr_list(to), _addr_list(cc)
        if not recipients:
            raise ValueError("Who should it go to?")
        if len(recipients) + len(ccs) > MAX_RECIPIENTS:
            raise ValueError(f"At most {MAX_RECIPIENTS} recipients.")
        subject = " ".join(str(subject or "").split())[:200]
        body = str(body or "").strip()
        if not subject or not body:
            raise ValueError("An email needs a subject and a body.")
        now = time.time()
        with self._lock:
            while self._sends and now - self._sends[0] > 3600:
                self._sends.popleft()
            if len(self._sends) >= MAX_SENDS_PER_HOUR:
                raise ProviderError(f"Lumi has already sent {MAX_SENDS_PER_HOUR} emails this hour. "
                                    "Send the rest yourself, or wait.")
            self._sends.append(now)
        try:
            provider.send(recipients, ccs, subject, body[:20000], reply_to_id)
        except Exception:
            with self._lock:
                if self._sends:
                    self._sends.pop()
            raise
        self._log(f"email sent via {account} to {len(recipients) + len(ccs)} recipient(s)")

    def build_ask_text(self, provider: str, mid: str, action: str) -> str:
        """The chat message for the "Ask Lumi" button."""
        instruction = {
            "summarize": "Summarize this email thread in a few bullet points, and tell me if anything "
                         "needs a reply or action from me.",
            "draft_reply": "Draft a reply to this email thread for me. Show me the draft only. "
                           "Don't send anything.",
        }.get(action)
        if instruction is None:
            raise ValueError("action must be summarize or draft_reply")
        msg = self.get_message(provider, mid)
        parts = [f"Account: {msg['provider'].upper()}", f"Subject: {msg['subject']}"]
        for m in msg.get("messages", []):
            parts.append(f"--- From: {m['from']} | {m['date']}\n{m['body']}")
        body = clean_text("\n".join(parts), 9000)
        return f"{instruction}\n\n[FROM EMAIL]\n{body}\n[END FROM EMAIL]"

    # -- compressed snapshot (for Lumi Island) --------------------------
    def get_compressed_snapshot(self) -> Dict[str, Any]:
        """Next event + top 3 unread, from the cache only (no network)."""
        now = datetime.now().astimezone()
        nxt = None
        for e in (self._cache.get("events") or {}).get("data", []):
            end = _parse_dt(e.get("end") or e.get("start") or "")
            start = _parse_dt(e.get("start") or "")
            if start is None or (end or start) <= now:
                continue
            nxt = {"title": e["title"][:60], "start": e["start"], "all_day": e["all_day"],
                   "location": e["location"][:40],
                   "in_min": int((start - now).total_seconds() // 60)}
            break
        unread: List[dict] = []
        for pid in ("gmail", "ksu"):
            unread += [m for m in (self._cache.get(f"mail:{pid}") or {}).get("data", []) if m.get("unread")]
        unread.sort(key=lambda m: m["ts"], reverse=True)
        return {
            "v": 1,
            "next_event": nxt,
            "unread_count": len(unread),
            "unread": [{"from": m["from_name"][:24], "subject": m["subject"][:60],
                        "provider": m["provider"], "ts": int(m["ts"])} for m in unread[:3]],
            "connected": {"google": self.google.connected(), "ksu": self._provider("ksu") is not None},
        }

    def add_listener(self, fn: Callable[[dict], None]) -> None:
        """fn(snapshot) runs when the snapshot changes (any thread)."""
        self._listeners.append(fn)

    def _notify(self) -> None:
        if not self._listeners:
            return
        snap = self.get_compressed_snapshot()
        digest = json.dumps({k: v for k, v in snap.items()}, sort_keys=True)
        # in_min changes every minute; it shouldn't count as a change on its own
        stable = re.sub(r'"in_min": -?\d+', '"in_min": 0', digest)
        if stable == self._last_snapshot:
            return
        self._last_snapshot = stable
        for fn in list(self._listeners):
            try:
                fn(snap)
            except Exception:
                pass

    # -- tools ----------------------------------------------------------
    def preview_for(self, name: str, args: dict) -> Dict[str, Any]:
        """What the approval prompt should show for an outbound tool."""
        args = args or {}
        if name == "send_email":
            return {"action": "Send email", "account": args.get("account") or "gmail",
                    "to": args.get("to"), "cc": args.get("cc") or None,
                    "subject": one_line(args.get("subject"), 200),
                    "body": clean_text(args.get("body"), 1500)}
        if name == "delete_event":
            eid = str(args.get("event_id") or "")
            ev = next((e for e in (self._cache.get("events") or {}).get("data", []) if e["id"] == eid), None)
            if ev is None:
                try:
                    ev = self.calendar.get_event(eid)
                except (ProviderError, ValueError, Exception):
                    ev = None
            return {"action": "Delete calendar event",
                    "event": f'{ev["title"]} ({ev["start"]})' if ev else f"unknown event {eid[:40]}"}
        if name == "create_event":
            return {"action": "Create calendar event", **{k: args.get(k) for k in
                    ("title", "start", "end", "all_day", "location", "attendees") if args.get(k) not in (None, "")}}
        return dict(args)

    def call_tool(self, name: str, args: dict) -> str:
        """Run one tool and return text for the model. Never raises."""
        args = args if isinstance(args, dict) else {}
        try:
            return self._run_tool(name, args)
        except (ProviderError, ValueError) as e:
            return f"Error: {e}"
        except Exception as e:                       # a bug here must not kill the turn
            return f"Error running {name}: {type(e).__name__}"

    def _run_tool(self, name: str, a: dict) -> str:
        if name == "list_events":
            days = int(a.get("days") or 7)
            events = self.get_events(days)["events"][:25]
            if not events:
                return f"No events in the next {days} day(s)."
            return "\n".join(
                f'{e["id"]} | {e["start"]}{" (all day)" if e["all_day"] else " to " + e["end"]} | '
                f'{e["title"]}{" @ " + e["location"] if e["location"] else ""}' for e in events)
        if name == "create_event":
            ev = self.create_event(a.get("title", ""), str(a.get("start") or ""), a.get("end") or None,
                                   bool(a.get("all_day")), a.get("location", ""), "", a.get("attendees"))
            return f'Created "{ev["title"]}" on {ev["start"]} (id {ev["id"]}).'
        if name == "delete_event":
            self.delete_event(str(a.get("event_id") or ""))
            return "Event deleted."
        if name == "list_emails":
            account = a.get("account") or "all"
            limit = max(1, min(int(a.get("limit") or 10), 15))
            msgs = self.get_mail(account if account in ("gmail", "ksu") else "all")["messages"]
            if a.get("unread_only"):
                msgs = [m for m in msgs if m["unread"]]
            if not msgs:
                return "No messages."
            return "\n".join(f'{m["provider"]} | {m["id"]} | {m["from_name"]} | {m["subject"]} | '
                             f'{m["date"]}{" | UNREAD" if m["unread"] else ""}' for m in msgs[:limit])
        if name == "read_email":
            m = self.get_message(str(a.get("account") or ""), str(a.get("id") or ""))
            lines = [f'Subject: {m["subject"]}']
            for part in m.get("messages", []):
                lines.append(f'--- From: {part["from"]} | {part["date"]}\n{part["body"]}')
            return clean_text("\n".join(lines), 7000)
        if name == "send_email":
            self.send_email(str(a.get("account") or "gmail"), a.get("to"), a.get("subject", ""),
                            a.get("body", ""), a.get("cc"), a.get("reply_to_id") or None)
            return "Email sent."
        if name == "mark_email_read":
            self.set_read(str(a.get("account") or ""), str(a.get("id") or ""), bool(a.get("read", True)))
            return "Done."
        if name == "archive_email":
            self.archive(str(a.get("account") or ""), str(a.get("id") or ""))
            return "Archived."
        return f"Unknown workspace tool: {name}"

    def shutdown(self) -> None:
        self.microsoft._cancel.set()
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)

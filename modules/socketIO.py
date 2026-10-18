"""
Socket.io server + local HTTP/SPA host for the Phase 10 web client.

Serves:
  1. Socket.io events on the default namespace
  2. HTTP routes: /auth/*, /api/*, /api/memories/*, /api/persona/*,
     /api/models/*, /api/providers/*, /api/integrations/*,
     /api/settings/*, /api/voice_message, /api/chat/upload
  3. Static files from web/
  4. Auth pages: /login (login/register), /setup (first-run wizard)

Page gating (server-side, not client-side):
  /            → /setup if no users, /login if not authed, else index.html
  /login       → /setup if no users, /         if authed, else login.html
  /setup       → wizard (client picks the step based on auth + ?step=)

Phase 10.19 — voice in browser:
  - tts.py calls signals.broadcast_tts_audio(bytes) to fan out WAV
    audio to every opted-in web client.
  - POST /api/voice_message accepts a webm/ogg/wav blob, transcribes
    with faster-whisper, injects the transcript as a user message.

File upload (new):
  - POST /api/chat/upload accepts a multipart file, saves it under
    data/library/, extracts text (PDF/DOCX/text) or base64-encodes
    images, and returns the extraction so the browser can attach it
    to the next chat message.
  - POST /api/chat/send now accepts an `attachments` array. The
    prompter expands those into either content blocks (for images
    on models that support vision) or inlined text.

Remote access (Cloudflare Tunnel):
  - Two loopback listeners share one app: :5000 for the host's own
    browser and TUNNEL_PORT (:5080) for cloudflared. cloudflared runs
    on this PC, so the peer address of tunnel traffic is always
    127.0.0.1 and can't tell local from remote. The listening port can.
    Non-loopback peers and Cloudflare headers also count as remote;
    every signal can only lower privilege, never raise it.
  - Remote requests may only call the routes in _REMOTE_API_ALLOW;
    everything else under /api/ is 403 (default-deny). Chat commands
    (!agent_on, wipe memory, …) are refused for remote senders.
  - Public origins come from LUMI_PUBLIC_ORIGINS in .env, re-read when
    the file changes, and pass the same origin checks as localhost.
  - Remote requests can't register accounts unless
    REMOTE_ALLOW_REGISTRATION is set.
  - With TRUST_CLOUDFLARE_ACCESS, a verified Access JWT signs the
    visitor in (modules/cf_access.py).
  - GET /healthz is public; GET /api/log_events proxies the log hub
    for local sessions so the dashboard never talks to :5001 directly.

Stream control (off unless STREAMING_ENABLED):
  - GET  /api/stream/status, POST /api/stream/scene|start|stop.

Security posture (Phase 10.17):
  - Bound strictly to 127.0.0.1
  - HttpOnly, SameSite=Lax session cookie
  - CSRF on every state-changing POST/PATCH/PUT/DELETE
  - Origin validated on socket.io handshake and HTTP POSTs
  - Static handler refuses path traversal
  - Uploads capped at 25 MB; filenames sanitized; extension allowlist
"""

import os
import re
import socket
import time
import base64
import secrets
import asyncio
import functools
import mimetypes
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from aiohttp import web
from dotenv import dotenv_values
import socketio

from constants import (PATIENCE, HOST, WORKSPACE_ROOT, LUMI_VERSION,
                       TUNNEL_PORT, REMOTE_ALLOW_REGISTRATION,
                       TRUST_CLOUDFLARE_ACCESS, STREAMING_ENABLED,
                       OBS_SUBTITLES_ENABLED, OBS_SUBTITLE_FILE)
from modules.user_auth import UserDB

_LOCAL_ORIGINS = frozenset({
    "http://127.0.0.1:5000",
    "http://localhost:5000",
})

_ENV_PATH = os.path.join(WORKSPACE_ROOT, ".env")
_public_origins_cache = (object(), frozenset())


def _public_origins():
    """HTTPS origins from LUMI_PUBLIC_ORIGINS, re-parsed whenever .env
    changes so a tunnel hostname added at runtime works without a
    restart."""
    global _public_origins_cache
    try:
        mtime = os.path.getmtime(_ENV_PATH)
    except OSError:
        mtime = None
    if _public_origins_cache[0] == mtime:
        return _public_origins_cache[1]

    raw = ""
    if mtime is not None:
        raw = dotenv_values(_ENV_PATH).get("LUMI_PUBLIC_ORIGINS") or ""
    raw = raw or os.environ.get("LUMI_PUBLIC_ORIGINS", "")

    origins = set()
    for item in raw.split(","):
        item = item.strip().rstrip("/").lower()
        if not item:
            continue
        # Origin only: scheme + host, no path. Plain http would send
        # the session cookie in the clear across the internet.
        if item.startswith("https://") and "/" not in item[len("https://"):]:
            origins.add(item)
        else:
            print(f"\033[93mSOCKETIO: ignoring LUMI_PUBLIC_ORIGINS entry "
                  f"{item!r} — expected https://host\033[0m")
    _public_origins_cache = (mtime, frozenset(origins))
    return _public_origins_cache[1]


_LOOPBACK = frozenset({"127.0.0.1", "::1"})

# Any of these means the request passed through Cloudflare (or some
# other proxy). Their presence only ever downgrades a request.
_PROXY_HEADERS = ("Cf-Connecting-Ip", "Cf-Ray", "X-Forwarded-For")

# The only /api/ routes a remote request may call. Default-deny: a
# route added later stays host-only until it is listed here.
_REMOTE_API_ALLOW = frozenset({
    ("GET",  "/api/status"),
    ("GET",  "/api/history"),
    ("POST", "/api/chat/send"),
    ("POST", "/api/chat/upload"),
    ("POST", "/api/voice_message"),
    ("GET",  "/api/models"),          # list only; feeds the Ollama dot
})

_HOST_ONLY_MSG = "Only available on the host machine."

# Per-request storage keys (aiohttp >= 3.10 prefers typed keys).
if hasattr(web, "RequestKey"):
    _IS_LOCAL = web.RequestKey("is_local", bool)
    _ACCESS_USER = web.RequestKey("access_user", dict)
    _ACCESS_CSRF = web.RequestKey("access_csrf", str)
else:
    _IS_LOCAL = "is_local"
    _ACCESS_USER = "access_user"
    _ACCESS_CSRF = "access_csrf"


def _is_chat_command(text):
    """Mirror of the prompter's command detection (prompter.run). These
    reach settings, memory, and agent mode, so remote users can't send
    them."""
    t = (text or "").strip().lower()
    return (t.startswith("!")
            or t.startswith("set mood ")
            or t.startswith("switch mood ")
            or t in ("wipe memory", "clear memory", "reset memory"))


def _local_port(request):
    transport = request.transport
    if transport is None:
        return None
    sockname = transport.get_extra_info("sockname")
    return sockname[1] if sockname else None


def _origin_allowed(origin, environ=None):
    """Shared by HTTP handlers and the socket.io CORS check
    (engine.io calls it as fn(origin, environ))."""
    if not origin:
        return False
    origin = origin.rstrip("/").lower()
    return origin in _LOCAL_ORIGINS or origin in _public_origins()

_WEB_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web"
)

_SESSION_COOKIE = "lumi_session"
_CSRF_COOKIE = "lumi_csrf"


# ===============================================================
# Upload limits and allowlists
# ===============================================================
_MAX_UPLOAD_BYTES = 25 * 1024 * 1024   # 25 MB hard cap on the raw upload
_MAX_INJECT_CHARS = 15000              # cap on text injected into a turn
_MAX_VOICE_BYTES  = 10 * 1024 * 1024   # 10 MB cap on a push-to-talk clip

_VOICE_EXTS = {".webm", ".ogg", ".mp4", ".m4a", ".wav"}

_ALLOWED_UPLOAD_EXTS = {
    # Documents
    ".pdf", ".docx", ".doc",
    # Text-like
    ".txt", ".md", ".json", ".yaml", ".yml", ".csv", ".log",
    # Images
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
}

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

# Subfolder routing — every upload goes into one of these under
# data/library/. Keeps the top level scannable even after hundreds
# of files.
_SUBFOLDER_IMAGES    = "images"
_SUBFOLDER_DOCUMENTS = "documents"
_SUBFOLDER_TEXT      = "text"

def _boot_block(sig):
    """Assemble the boot progress dict for /api/status.

    Each subsystem reports one of:
      "loading" | "ready" | "skipped" | "error"
    The frontend uses these to draw the bloom loader and to
    colour the sidebar status pills. Missing attributes default to
    "skipped" so an un-wired subsystem doesn't hang the loader.
    """
    def _flag(attr):
        v = getattr(sig, attr, None)
        if v is True:  return "ready"
        if v is False: return "error"
        return "loading"

    vts_state  = getattr(sig, "vts_state",  None) or "loading"
    vts_detail = getattr(sig, "vts_detail", "") or ""

    if getattr(sig, "discord_configured", False):
        discord_state  = getattr(sig, "discord_state", "loading")
        discord_detail = getattr(sig, "discord_detail", "") or ""
    else:
        discord_state  = "skipped"
        discord_detail = ""

    boot = {
        "memory": {
            "state":  _flag("memory_ready"),
            "detail": f"{getattr(sig, 'memory_count', 0)} memories",
        },
        "mic": {
            "state":  _flag("mic_ready"),
            "detail": "local whisper",
        },
        "tts": {
            "state":  _flag("tts_ready"),
            "detail": "fish audio",
        },
        "vts": {
            "state":  vts_state,
            "detail": vts_detail,
        },
        "self_model": {
            "state":  _flag("self_model_ready"),
            "detail": "",
        },
        "discord": {
            "state":  discord_state,
            "detail": discord_detail,
        },
    }
    boot["complete"] = all(
        s["state"] in ("ready", "skipped", "error")
        for s in boot.values()
    )
    return boot

def _classify_ext(ext):
    """Ext-based kind classifier. Mirrors _classify_upload(path) but
    works before the file lands on disk, which we need to pick the
    target subfolder."""
    ext = (ext or "").lower()
    if ext in _IMAGE_EXTS:
        return "image"
    if ext == ".pdf":
        return "pdf"
    if ext in (".docx", ".doc"):
        return "doc"
    return "text"


def _subfolder_for_kind(kind):
    if kind == "image":
        return _SUBFOLDER_IMAGES
    if kind in ("pdf", "doc"):
        return _SUBFOLDER_DOCUMENTS
    return _SUBFOLDER_TEXT


def _sha256_of_bytes(raw):
    import hashlib
    return hashlib.sha256(raw).hexdigest()


def _sha256_of_file(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# path -> (mtime, size, sha256). Library files are write-once, so each
# one is hashed at most once per process unless it changes on disk.
_library_hash_cache = {}


def _cached_sha256(path, st):
    key = (st.st_mtime, st.st_size)
    hit = _library_hash_cache.get(path)
    if hit and hit[:2] == key:
        return hit[2]
    digest = _sha256_of_file(path)
    _library_hash_cache[path] = (*key, digest)
    return digest


def _find_library_duplicate(library_root, new_hash, new_size=None):
    """Walk data/library/{images,documents,text}/ and return metadata
    for the first file whose contents hash matches `new_hash`. Returns
    None if nothing matches.

    Returned dict:
      stored_name   e.g. "20261010_143022_report.pdf"
      stored_path   e.g. "data/library/documents/20261010_143022_report.pdf"
                    (relative to WORKSPACE_ROOT)
      source        e.g. "documents/20261010_143022_report.pdf"
                    (relative to library root — this is the ChromaDB key)
      original_name e.g. "report.pdf"
      subfolder     e.g. "documents"
      size          bytes
      modified_at   epoch seconds
    """
    from constants import WORKSPACE_ROOT
    if not new_hash or not os.path.isdir(library_root):
        return None
    for sub in sorted(os.listdir(library_root)):
        sub_dir = os.path.join(library_root, sub)
        if not os.path.isdir(sub_dir) or sub.startswith("."):
            continue
        for fname in sorted(os.listdir(sub_dir)):
            fpath = os.path.join(sub_dir, fname)
            try:
                st = os.stat(fpath)
                if not os.path.isfile(fpath):
                    continue
                # Different size can't be identical content — skip the
                # hash entirely. This rules out nearly every file.
                if new_size is not None and st.st_size != new_size:
                    continue
                if _cached_sha256(fpath, st) != new_hash:
                    continue
            except Exception:
                continue

            # Stored name convention is "<timestamp>_<original>".
            # Recover the original name for the UI.
            parts = fname.split("_", 2)
            original = parts[2] if len(parts) >= 3 else fname

            return {
                "stored_name": fname,
                "stored_path": os.path.relpath(
                    fpath, WORKSPACE_ROOT
                ).replace("\\", "/"),
                "source": f"{sub}/{fname}",
                "original_name": original,
                "subfolder": sub,
                "size": st.st_size,
                "modified_at": st.st_mtime,
            }
    return None


def _json_error(status, message):
    return web.json_response({"error": message}, status=status)


# Paths whose callers (app.js `api()`) expect a JSON body on every
# response, including failures.
_JSON_PREFIXES = ("/api/", "/auth/")


@web.middleware
async def _json_error_middleware(request, handler):
    """Turn unhandled errors on API routes into `{"error": ...}` JSON
    instead of aiohttp's default HTML error page. Pages, static files
    and socket.io are left untouched, as are redirects."""
    if not request.path.startswith(_JSON_PREFIXES):
        return await handler(request)
    try:
        return await handler(request)
    except web.HTTPException as e:
        if e.status < 400:
            raise
        return _json_error(e.status, e.reason or "Request failed.")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        import traceback
        print(f"\033[91mHTTP: {request.method} {request.path} failed — "
              f"{type(e).__name__}: {e}\033[0m")
        traceback.print_exc()
        return _json_error(500, f"Internal error: {type(e).__name__}")


def _parse_cookies(cookie_header):
    out = {}
    if not cookie_header:
        return out
    for part in cookie_header.split(";"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip()] = v.strip()
    return out


# ---------------------------------------------------------------
# Upload helpers
# ---------------------------------------------------------------
def _sanitize_upload_name(name):
    """Strip path components and dangerous characters from a client-
    supplied filename. Keeps the extension."""
    name = os.path.basename(name or "upload")
    name = re.sub(r"[^A-Za-z0-9._\- ]", "_", name)
    name = name.strip(". ") or "upload"
    return name[:120]


def _classify_upload(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return "pdf"
    if ext in (".docx", ".doc"):
        return "doc"
    if ext in _IMAGE_EXTS:
        return "image"
    return "text"


def _extract_upload_text(path, kind):
    """Return (text, error) for a stored upload."""
    try:
        if kind == "pdf":
            from modules.agent_tools import _read_pdf
            return _read_pdf(path), None
        if kind == "doc":
            from modules.agent_tools import _read_docx
            return _read_docx(path), None
        if kind == "image":
            # No text extraction — the model gets pixels directly, or
            # the prompter describes via VLM at turn time.
            return "", None
        if kind == "text":
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read(), None
    except Exception as e:
        return "", f"{type(e).__name__}: {e}"
    return "", "unknown file type"

def _write_upload_sidecar(target_dir, stored_name, original_name,
                          subfolder, kind, size, file_hash, text):
    """Write a .meta.md sidecar next to the uploaded file.

    The sidecar contains upload metadata plus the extracted content
    (full VLM report for images, preview for documents). The library
    indexer picks it up and stores its chunks in ChromaDB, so future
    turns can retrieve the upload by semantic query — 'the screenshot
    I showed you', 'that PDF about X', 'what did I send you yesterday'.

    Without this step, image analyses vanish the moment the
    conversation scrolls past them: the .png itself is not text and
    cannot be indexed.

    Returns the absolute path to the sidecar, or None on failure.
    """
    stem = os.path.splitext(stored_name)[0]
    sidecar_name = f"{stem}.meta.md"
    sidecar_path = os.path.join(target_dir, sidecar_name)

    uploaded_at = time.strftime("%Y-%m-%d %H:%M:%S")
    hash_short = (file_hash or "")[:16]

    header = (
        f"# Uploaded file: {original_name}\n\n"
        f"- **Uploaded:** {uploaded_at}\n"
        f"- **Stored as:** {subfolder}/{stored_name}\n"
        f"- **Type:** {kind}\n"
        f"- **Size:** {size} bytes\n"
        f"- **Content hash (sha256):** {hash_short}\n\n"
    )

    if kind == "image":
        body = (
            "## Visual analysis\n\n"
            f"{text}\n"
        )
    elif kind in ("pdf", "doc"):
        preview = (text or "")[:800].strip()
        body = (
            "## Preview (first 800 chars)\n\n"
            f"{preview}\n\n"
            f"_Full text of this document is indexed directly from "
            f"`{subfolder}/{stored_name}`._\n"
        )
    else:
        # Text-like uploads (txt, md, json) — store the full content
        body = (
            "## Content\n\n"
            f"{text}\n"
        )

    content = header + body
    try:
        with open(sidecar_path, "w", encoding="utf-8") as f:
            f.write(content)
        return sidecar_path
    except Exception as e:
        print(f"\033[91mUPLOAD: sidecar write failed — {e}\033[0m")
        return None

# Lazy faster-whisper loader for the voice-in-browser path.
_voice_whisper = None


def _get_voice_whisper_model():
    global _voice_whisper
    if _voice_whisper is None:
        from faster_whisper import WhisperModel
        _voice_whisper = WhisperModel("base", device="cpu", compute_type="int8")
    return _voice_whisper


class SocketIOServer:
    def __init__(self, signals, stt=None, tts=None, llmWrapper=None,
                 prompter=None, modules=None, model_manager=None):
        self.signals = signals
        self.stt = stt
        self.tts = tts
        self.llmWrapper = llmWrapper
        self.prompter = prompter
        self.modules = modules if modules is not None else {}
        self.model_manager = model_manager

        self.user_db = UserDB()
        self.sio = socketio.AsyncServer(
            async_mode="aiohttp",
            cors_allowed_origins=_origin_allowed,
            logger=False,
            engineio_logger=False,
        )
        # Set by start_sites(); requests arriving on this port are remote.
        self.tunnel_port = None
        self.app = web.Application(middlewares=[
            _json_error_middleware, self._make_tier_middleware(),
        ])
        self.sio.attach(self.app)

        # Memory/library calls (ChromaDB, embeddings, file parsing) are
        # blocking. Run them on one dedicated worker so they don't stall
        # the event loop, while staying serialized the way they were
        # when they ran inline.
        self._mem_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="web-memory",
        )

        # Phase 10.19 — voice in browser.
        self._tts_clients = set()
        self._loop = None
        self.signals.broadcast_tts_audio = self._broadcast_tts_audio

        self._register_http_routes()
        self._register_socket_events()

    async def _run_mem(self, fn, *args, **kwargs):
        """Run a blocking memory/library call on the memory worker."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._mem_executor, functools.partial(fn, *args, **kwargs),
        )

    def _model_manager(self):
        if self.model_manager is not None:
            return self.model_manager
        try:
            from modules.model_manager import ModelManager
        except Exception:
            return None
        self.model_manager = ModelManager()
        return self.model_manager

    # ==================================================================
    # HTTP routes
    # ==================================================================
    def _register_http_routes(self):
        r = self.app.router

        # --- Pages ---
        r.add_get("/",         self._handle_index)
        r.add_get("/login",    self._handle_login_page)
        r.add_get("/register", self._handle_register_page)
        r.add_get("/setup",    self._handle_setup_page)

        # --- Static files ---
        r.add_get("/web/{tail:.*}", self._handle_static)

        # --- Setup status (public) ---
        r.add_get("/setup-status", self._handle_setup_status)

        # --- Auth ---
        r.add_post("/auth/register", self._handle_register)
        r.add_post("/auth/login",    self._handle_login)
        r.add_post("/auth/logout",   self._handle_logout)
        r.add_get("/auth/me",        self._handle_me)

        # --- API ---
        r.add_get("/api/status",        self._handle_status)
        r.add_get("/api/history",       self._handle_history)
        r.add_post("/api/chat/send",    self._handle_chat_send)
        r.add_post("/api/chat/upload",  self._handle_chat_upload)
        r.add_post("/api/agent/toggle", self._handle_agent_toggle)

        # Memory browser — export route MUST register before the
        # dynamic {mem_id} route or aiohttp will treat "export" as an id.
        r.add_get("/api/memories/export",       self._handle_memories_export)
        r.add_get("/api/memories",              self._handle_memories_list)
        r.add_post("/api/memories",             self._handle_memories_create)
        r.add_get("/api/memories/{mem_id}",     self._handle_memory_get)
        r.add_patch("/api/memories/{mem_id}",   self._handle_memory_update)
        r.add_delete("/api/memories/{mem_id}",  self._handle_memory_delete)

        # Persona editor — Phase 10.10
        r.add_get("/api/persona",           self._handle_persona_get)
        r.add_put("/api/persona",           self._handle_persona_put)
        r.add_post("/api/persona/raw",      self._handle_persona_raw)
        r.add_post("/api/persona/preview",  self._handle_persona_preview)

        # Model manager — Phase 10.12
        r.add_get("/api/models",          self._handle_models_get)
        r.add_get("/api/models/pull",     self._handle_models_pull_status)
        r.add_post("/api/models/load",    self._handle_models_load)
        r.add_post("/api/models/unload",  self._handle_models_unload)
        r.add_post("/api/models/pull",    self._handle_models_start_pull)
        r.add_post("/api/models/delete",  self._handle_models_delete)
        r.add_put("/api/models/routing",  self._handle_models_routing_save)

        # LLM providers — Phase 10.12b / 10.20
        r.add_get("/api/providers",              self._handle_providers_list)
        r.add_put("/api/providers/{pid}",        self._handle_provider_save)
        r.add_post("/api/providers/{pid}/test",  self._handle_provider_test)
        r.add_get("/api/providers/{pid}/models", self._handle_provider_models)

        # Integrations — Phase 10.13
        r.add_get("/api/integrations",               self._handle_integrations_list)
        r.add_put("/api/integrations/{iid}",         self._handle_integration_save)
        r.add_post("/api/integrations/{iid}/test",   self._handle_integration_test)
        r.add_post("/api/integrations/{iid}/toggle", self._handle_integration_toggle)

        # Settings — Phase 10.14
        r.add_get("/api/settings/general",                     self._handle_settings_general_get)
        r.add_put("/api/settings/general",                     self._handle_settings_general_put)
        r.add_get("/api/settings/about",                       self._handle_settings_about)
        r.add_get("/api/settings/advanced",                    self._handle_settings_advanced_get)
        r.add_post("/api/settings/advanced/reset",             self._handle_settings_reset)
        r.add_post("/api/settings/security/password",          self._handle_settings_password)
        r.add_get("/api/settings/security/sessions",           self._handle_settings_sessions_list)
        r.add_post("/api/settings/security/sessions/revoke",   self._handle_settings_session_revoke)
        r.add_post("/api/settings/security/sessions/revoke-others", self._handle_settings_session_revoke_others)

        # Voice in browser — Phase 10.19
        r.add_post("/api/voice_message", self._handle_voice_message)

        # Remote access support
        r.add_get("/healthz",         self._handle_healthz)
        r.add_get("/api/log_events",  self._handle_log_events)

        # Stream control (OBS) — off unless STREAMING_ENABLED
        if STREAMING_ENABLED:
            r.add_get("/api/stream/status",  self._handle_stream_status)
            r.add_post("/api/stream/scene",  self._handle_stream_scene)
            r.add_post("/api/stream/start",  self._handle_stream_start)
            r.add_post("/api/stream/stop",   self._handle_stream_stop)

    def _current_user(self, request):
        # Signed in this request by Cloudflare Access (tier middleware).
        if request.get(_ACCESS_USER):
            return request[_ACCESS_USER]
        cookies = _parse_cookies(request.headers.get("Cookie", ""))
        token = cookies.get(_SESSION_COOKIE)
        return self.user_db.validate_session(token) if token else None

    def _require_csrf(self, request, body):
        cookies = _parse_cookies(request.headers.get("Cookie", ""))
        cookie_token = cookies.get(_CSRF_COOKIE)
        header_token = request.headers.get("X-CSRF-Token") or body.get("csrf")
        if not cookie_token or not header_token:
            return False, "Missing CSRF token."
        if not secrets.compare_digest(cookie_token, header_token):
            return False, "CSRF token mismatch."
        return True, None

    def _require_csrf_header(self, request):
        """Header-only CSRF check for multipart uploads where the body
        can't be parsed as JSON."""
        cookies = _parse_cookies(request.headers.get("Cookie", ""))
        cookie_token = cookies.get(_CSRF_COOKIE)
        header_token = request.headers.get("X-CSRF-Token")
        if not cookie_token or not header_token:
            return False, "Missing CSRF token."
        if not secrets.compare_digest(cookie_token, header_token):
            return False, "CSRF token mismatch."
        return True, None

    def _origin_ok(self, request):
        origin = request.headers.get("Origin")
        if origin is None:
            return True
        return _origin_allowed(origin)

    def _is_local(self, request):
        """Full-access (host) request? See the module docstring for why
        the listening port, not the peer address, is the deciding
        signal. The Origin header is not consulted: a remote user can
        set it to anything."""
        cached = request.get(_IS_LOCAL)
        if cached is not None:
            return cached
        local = True
        if self.tunnel_port is not None \
                and _local_port(request) == self.tunnel_port:
            local = False
        elif request.remote not in _LOOPBACK:
            local = False      # LAN peer (ALLOW_LAN_EXPOSURE)
        elif any(h in request.headers for h in _PROXY_HEADERS):
            local = False      # cloudflared pointed at :5000 by mistake
        request[_IS_LOCAL] = local
        return local

    def _is_remote(self, request):
        return not self._is_local(request)

    @staticmethod
    def _remote_allowed(request):
        path = request.path
        if not path.startswith("/api/"):
            return True        # pages, /auth/*, static, socket.io, /healthz
        return (request.method, path) in _REMOTE_API_ALLOW

    def _make_tier_middleware(self):
        @web.middleware
        async def tier_middleware(request, handler):
            if self._is_local(request):
                return await handler(request)

            new_session = None
            if TRUST_CLOUDFLARE_ACCESS:
                new_session = await self._access_sign_in(request)

            if not self._remote_allowed(request):
                return _json_error(403, _HOST_ONLY_MSG)

            if new_session is None:
                return await handler(request)
            try:
                response = await handler(request)
            except web.HTTPException as e:   # page redirects
                self._set_session_cookies(e, *new_session, secure=True)
                raise
            self._set_session_cookies(response, *new_session, secure=True)
            return response
        return tier_middleware

    async def _access_sign_in(self, request):
        """Sign a remote request in from its Cloudflare Access JWT if
        it has no Lumi session yet. Returns (session, csrf) to set as
        cookies, or None."""
        cookies = _parse_cookies(request.headers.get("Cookie", ""))
        if self.user_db.validate_session(cookies.get(_SESSION_COOKIE)):
            return None
        token = request.headers.get("Cf-Access-Jwt-Assertion")
        if not token:
            return None

        from modules import cf_access
        team, aud = cf_access.read_access_config()
        loop = asyncio.get_running_loop()
        try:
            email = await loop.run_in_executor(
                None, cf_access.verify_access_jwt, token, team, aud)
        except cf_access.AccessError as e:
            print(f"\033[93mAUTH: Cloudflare Access JWT rejected — "
                  f"{e}\033[0m")
            return None

        user = await loop.run_in_executor(
            None, self.user_db.get_or_create_external_user,
            cf_access.username_for_email(email), email)
        if not user:
            return None
        session = self.user_db.create_session(user["id"])
        csrf = secrets.token_urlsafe(32)
        request[_ACCESS_USER] = user
        request[_ACCESS_CSRF] = csrf
        print(f"\033[92mAUTH: Cloudflare Access sign-in for {email} "
              f"as {user['username']}\033[0m")
        return session, csrf

    @staticmethod
    def _is_https(request):
        return (request.secure or
                request.headers.get("X-Forwarded-Proto", "") == "https")

    def _set_session_cookies(self, response, session_token, csrf_token,
                             secure=False):
        response.set_cookie(
            _SESSION_COOKIE, session_token,
            httponly=True, samesite="Lax", secure=secure, path="/",
            max_age=60 * 60 * 24 * 30,
        )
        response.set_cookie(
            _CSRF_COOKIE, csrf_token,
            httponly=False, samesite="Lax", secure=secure, path="/",
            max_age=60 * 60 * 24 * 30,
        )

    def _clear_session_cookies(self, response):
        response.del_cookie(_SESSION_COOKIE, path="/")
        response.del_cookie(_CSRF_COOKIE, path="/")

    # ------------------------------------------------------------------
    # Setup / auth
    # ------------------------------------------------------------------
    async def _handle_setup_status(self, request):
        return web.json_response({
            "needs_setup": not self.user_db.has_users(),
            "user_count":  self.user_db.user_count(),
            "remote":      self._is_remote(request),
        })

    async def _handle_register(self, request):
        if not self._origin_ok(request):
            return _json_error(403, "Bad origin.")
        if self._is_remote(request) and not REMOTE_ALLOW_REGISTRATION:
            return _json_error(403, (
                "Registration is disabled here. New accounts can only be "
                "created on the host machine at http://127.0.0.1:5000."
            ))
        try:
            body = await request.json()
        except Exception:
            return _json_error(400, "Invalid JSON.")

        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        display = (body.get("display_name") or "").strip() or None
        if username.lower().startswith("cf-"):
            # Reserved for Cloudflare Access accounts (cf_access.py).
            return _json_error(400, "Usernames starting with cf- are reserved.")

        ok, payload = self.user_db.register(username, password, display)
        if not ok:
            return _json_error(400, payload)

        session_token = self.user_db.create_session(payload["id"])
        csrf_token = secrets.token_urlsafe(32)

        HOST.update(name=payload["display_name"], user_id=payload["username"])

        response = web.json_response({
            "ok": True,
            "user": payload,
            "csrf": csrf_token,
        })
        self._set_session_cookies(response, session_token, csrf_token,
                                  secure=self._is_https(request))
        return response

    async def _handle_login(self, request):
        if not self._origin_ok(request):
            return _json_error(403, "Bad origin.")
        try:
            body = await request.json()
        except Exception:
            return _json_error(400, "Invalid JSON.")

        username = (body.get("username") or "").strip()
        password = body.get("password") or ""

        ok, payload = self.user_db.authenticate(username, password)
        if not ok:
            if self._is_remote(request):
                print(f"\033[93mAUTH: remote login failed for "
                      f"{username!r} — {payload}\033[0m")
            return _json_error(401, payload)
        if self._is_remote(request):
            print(f"\033[92mAUTH: remote login ok for {username!r} "
                  f"(origin {request.headers.get('Origin')!r}, secure "
                  f"cookie={self._is_https(request)})\033[0m")

        session_token = self.user_db.create_session(payload["id"])
        csrf_token = secrets.token_urlsafe(32)

        HOST.update(name=payload["display_name"], user_id=payload["username"])

        response = web.json_response({
            "ok": True,
            "user": payload,
            "csrf": csrf_token,
        })
        self._set_session_cookies(response, session_token, csrf_token,
                                  secure=self._is_https(request))
        return response

    async def _handle_logout(self, request):
        cookies = _parse_cookies(request.headers.get("Cookie", ""))
        token = cookies.get(_SESSION_COOKIE)
        if token:
            self.user_db.delete_session(token)
        response = web.json_response({"ok": True})
        self._clear_session_cookies(response)
        return response

    async def _handle_me(self, request):
        user = self._current_user(request)
        if not user:
            return _json_error(401, "Not logged in.")
        cookies = _parse_cookies(request.headers.get("Cookie", ""))
        csrf = request.get(_ACCESS_CSRF) or cookies.get(_CSRF_COOKIE, "")
        return web.json_response({
            "user": user,
            "csrf": csrf,
            "host": {"name": HOST.name, "user_id": HOST.user_id},
            "is_local": self._is_local(request),
            "features": {"stream": STREAMING_ENABLED},
        })

    # ------------------------------------------------------------------
    # Status / history / chat
    # ------------------------------------------------------------------
    async def _handle_status(self, request):
        user = self._current_user(request)
        if not user:
            return _json_error(401, "Not logged in.")
        signals = self.signals
        return web.json_response({
            "host":         {"name": HOST.name, "user_id": HOST.user_id},
            "ai_thinking":  bool(getattr(signals, "AI_thinking", False)),
            "ai_speaking":  bool(getattr(signals, "AI_speaking", False)),
            "history_len":  len(getattr(signals, "history", [])),
            "boot":        _boot_block(self.signals), 
            "agent_mode":   bool(getattr(
                self.prompter, "agent_mode_enabled", False
            )) if self.prompter else False,
            "log_hub_port": getattr(
                getattr(signals, "log_hub", None), "port", 5001
            ),
            "is_local": self._is_local(request),
            "features": {"stream": STREAMING_ENABLED},
        })

    async def _handle_healthz(self, request):
        """Public liveness probe for uptime checks and tunnel testing."""
        return web.json_response({"ok": True, "version": LUMI_VERSION})

    async def _handle_log_events(self, request):
        """Dashboard events, proxied from the log hub so the browser
        never has to reach :5001. Remote requests are refused by the
        tier middleware before they get here."""
        user = self._current_user(request)
        if not user:
            return _json_error(401, "Not logged in.")
        hub = getattr(self.signals, "log_hub", None)
        if hub is None:
            return web.json_response({"events": []})
        try:
            limit = int(request.query.get("limit", 400))
        except ValueError:
            limit = 400
        limit = max(1, min(limit, 800))
        return web.json_response({"events": hub.get_events(limit=limit)})

    async def _handle_history(self, request):
        user = self._current_user(request)
        if not user:
            return _json_error(401, "Not logged in.")
        try:
            limit = int(request.query.get("limit", 40))
        except ValueError:
            limit = 40
        limit = max(1, min(limit, 200))
        history = getattr(self.signals, "history", [])[-limit:]
        # The chat only renders attachment badges, so don't ship the
        # extracted text on every poll.
        slim = []
        for msg in history:
            atts = msg.get("attachments")
            if atts:
                msg = {**msg, "attachments": [
                    {"name": a.get("name"), "kind": a.get("kind")}
                    for a in atts if isinstance(a, dict)
                ]}
            slim.append(msg)
        return web.json_response({"history": slim})

    async def _handle_chat_send(self, request):
        if not self._origin_ok(request):
            return _json_error(403, "Bad origin.")
        user = self._current_user(request)
        if not user:
            return _json_error(401, "Not logged in.")
        try:
            body = await request.json()
        except Exception:
            return _json_error(400, "Invalid JSON.")

        ok, err = self._require_csrf(request, body)
        if not ok:
            return _json_error(403, err)

        text = (body.get("text") or "").strip()
        attachments = body.get("attachments") or []

        if not text and not attachments:
            return _json_error(400, "Empty message.")
        if _is_chat_command(text) and self._is_remote(request):
            return _json_error(403, "Commands only work on the host machine.")

        # Normalize attachments — only keep the fields we need, cap
        # the count, cap the injected text length.
        clean_atts = []
        for att in attachments[:6]:
            if not isinstance(att, dict):
                continue
            clean_atts.append({
                "name": str(att.get("name") or "file")[:200],
                "stored_path": str(att.get("stored_path") or ""),
                "kind": str(att.get("kind") or "text"),
                "text": str(att.get("text") or "")[:_MAX_INJECT_CHARS],
            })

        history_msg = {"role": "user", "content": text}
        if clean_atts:
            history_msg["attachments"] = clean_atts

        self.signals.history.append(history_msg)
        self.signals.new_message = True
        self.signals.last_message_time = time.time()
        if hasattr(self.signals, "save_history"):
            self.signals.save_history()

        await self.sio.emit("web_message", {
            "role": "user",
            "content": text,
            "ts": time.time(),
            "attachments": [
                {"name": a["name"], "kind": a["kind"]}
                for a in clean_atts
            ],
        })

        return web.json_response({"ok": True})

    async def _handle_chat_upload(self, request):
        """Accept a multipart upload, save it into a categorized
        subfolder under data/library/, run VLM extraction for images
        or text extraction for documents, and return the result to
        the client.

        Persistence: every upload gets a .meta.md sidecar containing
        metadata plus the extracted content. The sidecar is indexed
        into the library RAG immediately so the upload is searchable
        from the very next turn. Non-image uploads also kick off
        full-document indexing in the background.

        Duplicate handling: if a file with identical content already
        exists, the first request returns HTTP 409 with details; on
        ?replace=1 the old file (and its sidecar) are removed and
        the new one lands.
        """
        if not self._origin_ok(request):
            return _json_error(403, "Bad origin.")
        user = self._current_user(request)
        if not user:
            return _json_error(401, "Not logged in.")
        ok, err = self._require_csrf_header(request)
        if not ok:
            return _json_error(403, err)

        # ── Read multipart ────────────────────────────────────────
        try:
            reader = await request.multipart()
            field = await reader.next()
            if field is None or field.name != "file":
                return _json_error(400, "Expected multipart field 'file'.")

            chunks_in = []
            total = 0
            while True:
                chunk = await field.read_chunk(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_UPLOAD_BYTES:
                    return _json_error(
                        413,
                        f"File too large "
                        f"(limit {_MAX_UPLOAD_BYTES // (1024*1024)} MB).",
                    )
                chunks_in.append(chunk)

            raw = b"".join(chunks_in)
            if not raw:
                return _json_error(400, "Empty file.")
            original_name = field.filename or "upload"
        except Exception as e:
            return _json_error(400, f"Could not read upload: {e}")

        # ── Validate extension + pick target folder ──────────────
        safe_name = _sanitize_upload_name(original_name)
        ext = os.path.splitext(safe_name)[1].lower()
        if ext not in _ALLOWED_UPLOAD_EXTS:
            return _json_error(
                400,
                f"Unsupported file type: {ext or '(none)'}. "
                f"Allowed: {', '.join(sorted(_ALLOWED_UPLOAD_EXTS))}",
            )

        kind = _classify_ext(ext)
        subfolder = _subfolder_for_kind(kind)

        from constants import DATA_ROOT, WORKSPACE_ROOT

        library_root = os.path.join(DATA_ROOT, "library")
        target_dir = os.path.join(library_root, subfolder)
        os.makedirs(target_dir, exist_ok=True)

        # ── Duplicate check ───────────────────────────────────────
        new_hash = _sha256_of_bytes(raw)
        replace_flag = (
            (request.query.get("replace", "") or "").lower()
            in ("1", "true", "yes")
        )

        existing_dup = await self._run_mem(
            _find_library_duplicate, library_root, new_hash, len(raw),
        )

        if existing_dup and not replace_flag:
            try:
                from modules.log_hub import emit as hub_emit
                hub_emit(
                    self.signals, "memory",
                    f"upload: duplicate detected for '{original_name}' — "
                    f"matches {existing_dup['source']}",
                    level="warn",
                )
            except Exception:
                pass

            return web.json_response({
                "error": "duplicate",
                "message": (
                    f"A file with identical contents already exists "
                    f"in the library as '{existing_dup['original_name']}'."
                ),
                "existing": existing_dup,
                "incoming": {
                    "name": original_name,
                    "size": len(raw),
                    "kind": kind,
                    "hash": new_hash,
                },
                "can_replace": True,
            }, status=409)

        # ── Replace path: remove old file + old sidecar + RAG rows ─
        if existing_dup and replace_flag:
            old_path = os.path.join(
                WORKSPACE_ROOT, existing_dup["stored_path"],
            )
            old_source = existing_dup["source"]
            try:
                os.remove(old_path)
            except Exception:
                pass
            _library_hash_cache.pop(old_path, None)

            # Remove the old sidecar if it exists.
            old_stem = os.path.splitext(old_path)[0]
            for suffix in (".meta.md", ".analysis.md"):
                try:
                    os.remove(old_stem + suffix)
                except Exception:
                    pass

            try:
                mem = self.modules.get("memory")
                if mem and hasattr(mem.API, "delete_library_file"):
                    await self._run_mem(
                        mem.API.delete_library_file, old_source,
                    )
                    # Also try the sidecar source if it existed.
                    old_rel = os.path.splitext(old_source)[0]
                    await self._run_mem(
                        mem.API.delete_library_file, f"{old_rel}.meta.md",
                    )
            except Exception:
                pass

            try:
                from modules.log_hub import emit as hub_emit
                hub_emit(
                    self.signals, "memory",
                    f"upload: replacing {old_source} with "
                    f"'{original_name}'",
                )
            except Exception:
                pass

        # ── Save ──────────────────────────────────────────────────
        stamp = time.strftime("%Y%m%d_%H%M%S")
        stored_name = f"{stamp}_{safe_name}"
        stored_path = os.path.join(target_dir, stored_name)

        counter = 1
        while os.path.exists(stored_path):
            stem, suffix = os.path.splitext(stored_name)
            stored_name = f"{stem}_{counter}{suffix}"
            stored_path = os.path.join(target_dir, stored_name)
            counter += 1

        try:
            with open(stored_path, "wb") as f:
                f.write(raw)
        except Exception as e:
            return _json_error(500, f"Could not save file: {e}")

        # ── Extract (VLM for images, direct parse for docs) ──────
        text = ""
        extract_err = None

        if kind == "image":
            try:
                from modules.agent_tools import read_image_vlm
            except Exception as e:
                extract_err = f"image analyzer unavailable: {e}"
            else:
                loop = asyncio.get_running_loop()
                vlm_text, vlm_err = await loop.run_in_executor(
                    None,
                    lambda: read_image_vlm(
                        stored_path, signals=self.signals,
                    ),
                )
                if vlm_text:
                    text = vlm_text
                else:
                    extract_err = vlm_err or "VLM extraction failed"
        else:
            # PDF/DOCX parsing can take seconds on large files.
            text, extract_err = await asyncio.get_running_loop().run_in_executor(
                None, _extract_upload_text, stored_path, kind,
            )

        rel_path = os.path.relpath(
            stored_path, WORKSPACE_ROOT
        ).replace("\\", "/")
        source = f"{subfolder}/{stored_name}"

        # ── Persist + index a searchable sidecar ─────────────────
        # This is what makes the upload retrievable on future turns.
        # The sidecar carries metadata plus the extracted content;
        # the library indexer picks it up and stores its chunks in
        # the same ChromaDB collection used for regular library
        # files. The passive library injection in prompter.py then
        # surfaces matching chunks whenever the user's message
        # resembles the sidecar's content.
        sidecar_rel = None
        if text:
            sidecar_path = _write_upload_sidecar(
                target_dir, stored_name, original_name, subfolder,
                kind, len(raw), new_hash, text,
            )
            if sidecar_path:
                sidecar_rel = os.path.relpath(
                    sidecar_path, WORKSPACE_ROOT,
                ).replace("\\", "/")

                # Index the sidecar right now — it's small and fast.
                try:
                    from modules.library_indexer import index_single_file
                    await self._run_mem(
                        index_single_file, self.signals, sidecar_path,
                    )
                except Exception as e:
                    try:
                        from modules.log_hub import emit as hub_emit
                        hub_emit(
                            self.signals, "memory",
                            f"upload: sidecar index failed — {e}",
                            level="warn",
                        )
                    except Exception:
                        pass

                # For non-image uploads, queue the raw document for
                # background indexing. The sidecar has a preview, but
                # "what does the PDF say about X" needs the full
                # text. Queued on the memory worker (not awaited) so the
                # HTTP response isn't blocked on PDF parsing.
                if kind != "image":
                    try:
                        from modules.library_indexer import index_single_file
                        self._mem_executor.submit(
                            index_single_file,
                            self.signals,
                            stored_path,
                        )
                    except Exception:
                        pass

            # Short personal-memory fact so `recall_memory` also
            # surfaces the upload. Kept brief — the full content
            # lives in the library collection, this is just a
            # breadcrumb for "do you remember when I sent you...".
            try:
                mem = self.modules.get("memory")
                if mem and hasattr(mem.API, "create_memory"):
                    first_line = text.strip().split("\n", 1)[0][:120]
                    fact = (
                        f"On {time.strftime('%Y-%m-%d')}, user "
                        f"uploaded a {kind} named '{original_name}'."
                    )
                    if first_line:
                        fact += f" Content started with: {first_line}"
                    await self._run_mem(
                        mem.API.create_memory, fact, importance=0.55,
                    )
            except Exception:
                pass

        try:
            from modules.log_hub import emit as hub_emit
            hub_emit(
                self.signals, "memory",
                f"upload: {source} ({kind}, {len(raw)} bytes, "
                f"{len(text or '')} chars extracted)"
                + (f" — ERROR: {extract_err}" if extract_err else ""),
                level="warn" if extract_err else "info",
            )
        except Exception:
            pass

        return web.json_response({
            "ok": True,
            "name": original_name,
            "stored_name": stored_name,
            "stored_path": rel_path,
            "source": source,
            "subfolder": subfolder,
            "kind": kind,
            "size": len(raw),
            "text": (text or "")[:_MAX_INJECT_CHARS],
            "text_truncated": bool(text and len(text) > _MAX_INJECT_CHARS),
            "text_chars": len(text or ""),
            "extract_error": extract_err,
            "replaced": bool(existing_dup and replace_flag),
            "sidecar": sidecar_rel,
        }) 
            
    async def _handle_agent_toggle(self, request):
        if not self._origin_ok(request):
            return _json_error(403, "Bad origin.")
        user = self._current_user(request)
        if not user:
            return _json_error(401, "Not logged in.")
        try:
            body = await request.json()
        except Exception:
            body = {}

        ok, err = self._require_csrf(request, body)
        if not ok:
            return _json_error(403, err)

        if not self.prompter:
            return _json_error(503, "Prompter not available.")

        self.prompter.agent_mode_enabled = \
            not self.prompter.agent_mode_enabled
        return web.json_response({
            "enabled": self.prompter.agent_mode_enabled,
        })

    # ------------------------------------------------------------------
    # Memory browser (Phase 10.11)
    # ------------------------------------------------------------------
    def _memory_api(self):
        mem = self.modules.get("memory")
        if mem is None or not hasattr(mem, "API"):
            return None
        return mem.API

    async def _handle_memories_list(self, request):
        user = self._current_user(request)
        if not user:
            return _json_error(401, "Not logged in.")
        api = self._memory_api()
        if api is None:
            return _json_error(503, "Memory module is not loaded.")

        q = (request.query.get("q") or "").strip()
        sort = (request.query.get("sort") or "recent").lower()
        try:
            limit = int(request.query.get("limit", 200))
        except ValueError:
            limit = 200
        limit = max(1, min(limit, 500))
        try:
            offset = int(request.query.get("offset", 0))
        except ValueError:
            offset = 0
        offset = max(0, offset)

        try:
            if q:
                hits = await self._run_mem(api.query_temporal, q, limit=limit)
                rows = []
                for h in hits:
                    rows.append({
                        "id": "",
                        "fact": h.get("fact", ""),
                        "timestamp": h.get("timestamp", ""),
                        "importance": h.get("importance", 0.5),
                        "valence": h.get("valence"),
                        "session_id": "",
                        "type": "search-hit",
                    })
                return web.json_response({
                    "total": len(rows),
                    "memories": rows,
                    "search": True,
                })

            data = await self._run_mem(
                api.list_all, limit=limit, offset=offset, sort=sort,
            )
            return web.json_response(data)
        except Exception as e:
            return _json_error(500, f"Memory list failed: {e}")

    async def _handle_memories_export(self, request):
        user = self._current_user(request)
        if not user:
            return _json_error(401, "Not logged in.")
        api = self._memory_api()
        if api is None:
            return _json_error(503, "Memory module is not loaded.")
        try:
            memories = await self._run_mem(api.export_all)
            return web.json_response({"memories": memories})
        except Exception as e:
            return _json_error(500, f"Export failed: {e}")

    async def _handle_memories_create(self, request):
        if not self._origin_ok(request):
            return _json_error(403, "Bad origin.")
        user = self._current_user(request)
        if not user:
            return _json_error(401, "Not logged in.")
        try:
            body = await request.json()
        except Exception:
            return _json_error(400, "Invalid JSON.")
        ok, err = self._require_csrf(request, body)
        if not ok:
            return _json_error(403, err)

        api = self._memory_api()
        if api is None:
            return _json_error(503, "Memory module is not loaded.")

        fact = (body.get("fact") or "").strip()
        if not fact:
            return _json_error(400, "Fact cannot be empty.")

        try:
            importance = float(body.get("importance", 0.9))
        except (TypeError, ValueError):
            importance = 0.9
        importance = max(0.0, min(1.0, importance))

        valence = body.get("valence")
        try:
            new_id = await self._run_mem(
                api.create_memory, fact, importance=importance,
                valence=valence,
            )
        except Exception as e:
            return _json_error(500, f"Create failed: {e}")

        return web.json_response({"ok": True, "id": new_id})

    async def _handle_memory_get(self, request):
        user = self._current_user(request)
        if not user:
            return _json_error(401, "Not logged in.")
        api = self._memory_api()
        if api is None:
            return _json_error(503, "Memory module is not loaded.")

        mem_id = request.match_info.get("mem_id", "")
        row = await self._run_mem(api.get_by_id, mem_id)
        if row is None:
            return _json_error(404, "Memory not found.")
        return web.json_response(row)

    async def _handle_memory_update(self, request):
        if not self._origin_ok(request):
            return _json_error(403, "Bad origin.")
        user = self._current_user(request)
        if not user:
            return _json_error(401, "Not logged in.")
        try:
            body = await request.json()
        except Exception:
            return _json_error(400, "Invalid JSON.")
        ok, err = self._require_csrf(request, body)
        if not ok:
            return _json_error(403, err)

        api = self._memory_api()
        if api is None:
            return _json_error(503, "Memory module is not loaded.")

        mem_id = request.match_info.get("mem_id", "")
        fact = body.get("fact")
        importance = body.get("importance")
        valence = body.get("valence")

        ok, err = await self._run_mem(
            api.update_memory,
            mem_id,
            document=fact,
            importance=importance,
            valence=valence,
        )
        if not ok:
            return _json_error(400, err or "Update failed.")
        return web.json_response({"ok": True})

    async def _handle_memory_delete(self, request):
        if not self._origin_ok(request):
            return _json_error(403, "Bad origin.")
        user = self._current_user(request)
        if not user:
            return _json_error(401, "Not logged in.")
        try:
            body = await request.json()
        except Exception:
            body = {}
        ok, err = self._require_csrf(request, body)
        if not ok:
            return _json_error(403, err)

        api = self._memory_api()
        if api is None:
            return _json_error(503, "Memory module is not loaded.")

        mem_id = request.match_info.get("mem_id", "")
        if not mem_id:
            return _json_error(400, "Missing memory id.")

        deleted = await self._run_mem(api.delete_memory, mem_id)
        if not deleted:
            return _json_error(404, "Memory not found or delete failed.")
        return web.json_response({"ok": True})

    # ------------------------------------------------------------------
    # Persona editor (Phase 10.10)
    # ------------------------------------------------------------------
    async def _handle_persona_get(self, request):
        user = self._current_user(request)
        if not user:
            return _json_error(401, "Not logged in.")
        try:
            from modules.persona_editor import (
                load_raw_text, load_parsed, parsed_to_plain,
                backend_name, backend_preserves_comments,
            )
            from constants import YAML_PATH, build_system_prompt, HOST
        except Exception as e:
            return _json_error(500, f"Persona module unavailable: {e}")

        try:
            raw = load_raw_text(YAML_PATH)
            parsed = load_parsed(YAML_PATH)
            plain = parsed_to_plain(parsed)
            prompt = build_system_prompt(HOST.name)
        except Exception as e:
            return _json_error(500, f"Failed to read persona: {e}")

        return web.json_response({
            "yaml": plain,
            "raw": raw,
            "system_prompt": prompt,
            "backend": backend_name(),
            "preserves_comments": backend_preserves_comments(),
        })

    async def _handle_persona_put(self, request):
        if not self._origin_ok(request):
            return _json_error(403, "Bad origin.")
        user = self._current_user(request)
        if not user:
            return _json_error(401, "Not logged in.")
        try:
            body = await request.json()
        except Exception:
            return _json_error(400, "Invalid JSON.")
        ok, err = self._require_csrf(request, body)
        if not ok:
            return _json_error(403, err)

        persona = body.get("yaml")
        if not isinstance(persona, dict):
            return _json_error(400, "Missing 'yaml' object.")

        from constants import save_persona_parsed, build_system_prompt, HOST
        ok, err = save_persona_parsed(persona)
        if not ok:
            return _json_error(400, f"Save failed: {err}")

        prompt = build_system_prompt(HOST.name)
        return web.json_response({"ok": True, "system_prompt": prompt})

    async def _handle_persona_raw(self, request):
        if not self._origin_ok(request):
            return _json_error(403, "Bad origin.")
        user = self._current_user(request)
        if not user:
            return _json_error(401, "Not logged in.")
        try:
            body = await request.json()
        except Exception:
            return _json_error(400, "Invalid JSON.")
        ok, err = self._require_csrf(request, body)
        if not ok:
            return _json_error(403, err)

        raw = body.get("raw")
        if not isinstance(raw, str):
            return _json_error(400, "Missing 'raw' string.")

        from constants import save_persona_raw, build_system_prompt, HOST
        ok, err = save_persona_raw(raw)
        if not ok:
            return _json_error(400, err)

        prompt = build_system_prompt(HOST.name)
        return web.json_response({"ok": True, "system_prompt": prompt})

    async def _handle_persona_preview(self, request):
        if not self._origin_ok(request):
            return _json_error(403, "Bad origin.")
        user = self._current_user(request)
        if not user:
            return _json_error(401, "Not logged in.")
        try:
            body = await request.json()
        except Exception:
            return _json_error(400, "Invalid JSON.")
        ok, err = self._require_csrf(request, body)
        if not ok:
            return _json_error(403, err)

        persona = body.get("yaml")
        if not isinstance(persona, dict):
            return _json_error(400, "Missing 'yaml' object.")

        from constants import build_system_prompt, HOST
        try:
            prompt = build_system_prompt(HOST.name, profile_dict=persona)
        except Exception as e:
            return _json_error(500, f"Preview failed: {e}")

        return web.json_response({"system_prompt": prompt})

    # ------------------------------------------------------------------
    # Model manager (Phase 10.12)
    # ------------------------------------------------------------------
    async def _handle_models_get(self, request):
        user = self._current_user(request)
        if not user:
            return _json_error(401, "Not logged in.")
        mm = self._model_manager()
        if mm is None:
            return _json_error(503, "Model manager unavailable.")

        loop = asyncio.get_event_loop()
        try:
            installed_task = loop.run_in_executor(None, mm.list_installed)
            loaded_task    = loop.run_in_executor(None, mm.list_loaded)
            installed, loaded = await asyncio.gather(
                installed_task, loaded_task,
            )
        except Exception as e:
            return _json_error(500, f"Ollama query failed: {e}")

        return web.json_response({
            "endpoint":        mm.endpoint,
            "installed":       installed.get("models", []),
            "installed_error": installed.get("error"),
            "loaded":          loaded.get("models", []),
            "loaded_error":    loaded.get("error"),
            "routing":         mm.get_routing(),
            "pull":            mm.get_pull_status(),
        })

    async def _handle_models_pull_status(self, request):
        user = self._current_user(request)
        if not user:
            return _json_error(401, "Not logged in.")
        mm = self._model_manager()
        if mm is None:
            return _json_error(503, "Model manager unavailable.")
        return web.json_response({"pull": mm.get_pull_status()})

    async def _handle_models_load(self, request):
        if not self._origin_ok(request):
            return _json_error(403, "Bad origin.")
        user = self._current_user(request)
        if not user:
            return _json_error(401, "Not logged in.")
        try:
            body = await request.json()
        except Exception:
            return _json_error(400, "Invalid JSON.")
        ok, err = self._require_csrf(request, body)
        if not ok:
            return _json_error(403, err)

        mm = self._model_manager()
        if mm is None:
            return _json_error(503, "Model manager unavailable.")

        name = (body.get("model") or "").strip()
        if not name:
            return _json_error(400, "Missing 'model'.")

        set_routing = bool(body.get("set_routing", True))
        target = (body.get("target") or "main").lower()

        loop = asyncio.get_event_loop()

        ok, err = await loop.run_in_executor(None, mm.preload, name)
        if not ok:
            return _json_error(400, f"Load failed: {err}")

        if set_routing:
            routing_patch = {}
            if target in ("main", "both"):
                routing_patch["MODEL_NAME"] = name
            if target in ("fast", "both"):
                routing_patch["FAST_MODEL_NAME"] = name

            if routing_patch:
                ok, err = await loop.run_in_executor(
                    None, mm.save_routing, routing_patch,
                )
                if not ok:
                    return _json_error(
                        400,
                        f"Model loaded but routing failed: {err}",
                    )

        return web.json_response({
            "ok": True,
            "model": name,
            "routed": bool(set_routing),
            "target": target,
        })

    async def _handle_models_unload(self, request):
        if not self._origin_ok(request):
            return _json_error(403, "Bad origin.")
        user = self._current_user(request)
        if not user:
            return _json_error(401, "Not logged in.")
        try:
            body = await request.json()
        except Exception:
            return _json_error(400, "Invalid JSON.")
        ok, err = self._require_csrf(request, body)
        if not ok:
            return _json_error(403, err)

        mm = self._model_manager()
        if mm is None:
            return _json_error(503, "Model manager unavailable.")

        name = (body.get("model") or "").strip()
        if not name:
            return _json_error(400, "Missing 'model'.")

        loop = asyncio.get_event_loop()
        ok, err = await loop.run_in_executor(None, mm.unload, name)
        if not ok:
            return _json_error(400, f"Unload failed: {err}")
        return web.json_response({"ok": True})

    async def _handle_models_start_pull(self, request):
        if not self._origin_ok(request):
            return _json_error(403, "Bad origin.")
        user = self._current_user(request)
        if not user:
            return _json_error(401, "Not logged in.")
        try:
            body = await request.json()
        except Exception:
            return _json_error(400, "Invalid JSON.")
        ok, err = self._require_csrf(request, body)
        if not ok:
            return _json_error(403, err)

        mm = self._model_manager()
        if mm is None:
            return _json_error(503, "Model manager unavailable.")

        name = (body.get("model") or "").strip()
        ok, err = mm.start_pull(name)
        if not ok:
            return _json_error(400, err)
        return web.json_response({"ok": True, "pull": mm.get_pull_status()})

    async def _handle_models_delete(self, request):
        if not self._origin_ok(request):
            return _json_error(403, "Bad origin.")
        user = self._current_user(request)
        if not user:
            return _json_error(401, "Not logged in.")
        try:
            body = await request.json()
        except Exception:
            return _json_error(400, "Invalid JSON.")
        ok, err = self._require_csrf(request, body)
        if not ok:
            return _json_error(403, err)

        mm = self._model_manager()
        if mm is None:
            return _json_error(503, "Model manager unavailable.")

        name = (body.get("model") or "").strip()
        if not name:
            return _json_error(400, "Missing 'model'.")

        loop = asyncio.get_event_loop()
        ok, err = await loop.run_in_executor(None, mm.delete_model, name)
        if not ok:
            return _json_error(400, f"Delete failed: {err}")
        return web.json_response({"ok": True})

    async def _handle_models_routing_save(self, request):
        if not self._origin_ok(request):
            return _json_error(403, "Bad origin.")
        user = self._current_user(request)
        if not user:
            return _json_error(401, "Not logged in.")
        try:
            body = await request.json()
        except Exception:
            return _json_error(400, "Invalid JSON.")
        ok, err = self._require_csrf(request, body)
        if not ok:
            return _json_error(403, err)

        mm = self._model_manager()
        if mm is None:
            return _json_error(503, "Model manager unavailable.")

        patch = body.get("routing")
        if not isinstance(patch, dict):
            return _json_error(400, "Missing 'routing' object.")

        loop = asyncio.get_event_loop()
        ok, err = await loop.run_in_executor(None, mm.save_routing, patch)
        if not ok:
            return _json_error(400, f"Save failed: {err}")

        return web.json_response({
            "ok": True,
            "routing": mm.get_routing(),
        })

    # ------------------------------------------------------------------
    # LLM providers (Phase 10.12b / 10.20)
    # ------------------------------------------------------------------
    def _provider_list_for_ui(self):
        try:
            from modules.llm_providers import list_providers, get_client
        except Exception as e:
            return None, f"Provider module unavailable: {e}"

        providers = list_providers()

        catalog = []
        errors = {}
        for p in providers:
            if p["id"] == "ollama":
                continue
            if not p["configured"]:
                continue
            try:
                client = get_client(p["id"])
                models, err = client.list_models()
                if err:
                    errors[p["id"]] = err
                    continue
                if not models:
                    errors[p["id"]] = "no models returned"
                    continue
                for m in models:
                    catalog.append({
                        "provider": p["id"],
                        "provider_label": p["label"],
                        "name": m["name"],
                        "spec": f"{p['id']}:{m['name']}",
                    })
            except Exception as e:
                errors[p["id"]] = f"{type(e).__name__}: {e}"
                continue

        catalog.sort(key=lambda x: (x["provider"], x["name"]))
        return {
            "providers": providers,
            "catalog": catalog,
            "errors": errors,
        }, None

    async def _handle_providers_list(self, request):
        user = self._current_user(request)
        if not user:
            return _json_error(401, "Not logged in.")

        loop = asyncio.get_event_loop()
        data, err = await loop.run_in_executor(None, self._provider_list_for_ui)
        if err:
            return _json_error(500, err)
        return web.json_response(data)

    async def _handle_provider_save(self, request):
        if not self._origin_ok(request):
            return _json_error(403, "Bad origin.")
        user = self._current_user(request)
        if not user:
            return _json_error(401, "Not logged in.")
        try:
            body = await request.json()
        except Exception:
            return _json_error(400, "Invalid JSON.")
        ok, err = self._require_csrf(request, body)
        if not ok:
            return _json_error(403, err)

        try:
            from modules.llm_providers import save_provider_config
        except Exception as e:
            return _json_error(503, f"Provider module unavailable: {e}")

        pid = request.match_info.get("pid", "")

        api_key = None
        if "api_key" in body:
            api_key = str(body.get("api_key") or "")

        base_url = None
        if "base_url" in body:
            base_url = str(body.get("base_url") or "")

        loop = asyncio.get_event_loop()
        ok, err = await loop.run_in_executor(
            None, save_provider_config, pid, api_key, base_url,
        )
        if not ok:
            return _json_error(400, f"Save failed: {err}")

        data, err = await loop.run_in_executor(None, self._provider_list_for_ui)
        if err:
            return _json_error(500, err)
        return web.json_response({"ok": True, **data})

    async def _handle_provider_test(self, request):
        if not self._origin_ok(request):
            return _json_error(403, "Bad origin.")
        user = self._current_user(request)
        if not user:
            return _json_error(401, "Not logged in.")
        try:
            body = await request.json()
        except Exception:
            body = {}
        ok, err = self._require_csrf(request, body)
        if not ok:
            return _json_error(403, err)

        try:
            from modules.llm_providers import test_provider
        except Exception as e:
            return _json_error(503, f"Provider module unavailable: {e}")

        pid = request.match_info.get("pid", "")
        loop = asyncio.get_event_loop()
        ok, msg = await loop.run_in_executor(None, test_provider, pid)
        return web.json_response({"ok": ok, "message": msg})

    async def _handle_provider_models(self, request):
        user = self._current_user(request)
        if not user:
            return _json_error(401, "Not logged in.")

        try:
            from modules.llm_providers import get_client
        except Exception as e:
            return _json_error(503, f"Provider module unavailable: {e}")

        pid = request.match_info.get("pid", "")
        loop = asyncio.get_event_loop()
        try:
            client = get_client(pid)
            models, err = await loop.run_in_executor(None, client.list_models)
        except Exception as e:
            return _json_error(400, str(e))

        if err:
            return _json_error(400, err)
        return web.json_response({
            "provider": pid,
            "models": models,
        })

    # ------------------------------------------------------------------
    # Integrations (Phase 10.13)
    # ------------------------------------------------------------------
    async def _handle_integrations_list(self, request):
        user = self._current_user(request)
        if not user:
            return _json_error(401, "Not logged in.")
        try:
            from modules.integrations import list_integrations
        except Exception as e:
            return _json_error(503, f"Integrations module unavailable: {e}")

        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(
            None, list_integrations, self.modules, self.signals,
        )
        return web.json_response({"integrations": data})

    async def _handle_integration_save(self, request):
        if not self._origin_ok(request):
            return _json_error(403, "Bad origin.")
        user = self._current_user(request)
        if not user:
            return _json_error(401, "Not logged in.")
        try:
            body = await request.json()
        except Exception:
            return _json_error(400, "Invalid JSON.")
        ok, err = self._require_csrf(request, body)
        if not ok:
            return _json_error(403, err)

        try:
            from modules.integrations import save_integration_config
        except Exception as e:
            return _json_error(503, f"Integrations module unavailable: {e}")

        iid = request.match_info.get("iid", "")
        values = body.get("values") or {}
        loop = asyncio.get_event_loop()
        ok, err = await loop.run_in_executor(
            None, save_integration_config, iid, values,
        )
        if not ok:
            return _json_error(400, f"Save failed: {err}")
        return web.json_response({"ok": True})

    async def _handle_integration_test(self, request):
        if not self._origin_ok(request):
            return _json_error(403, "Bad origin.")
        user = self._current_user(request)
        if not user:
            return _json_error(401, "Not logged in.")
        try:
            body = await request.json()
        except Exception:
            body = {}
        ok, err = self._require_csrf(request, body)
        if not ok:
            return _json_error(403, err)

        try:
            from modules.integrations import test_integration
        except Exception as e:
            return _json_error(503, f"Integrations module unavailable: {e}")

        iid = request.match_info.get("iid", "")
        loop = asyncio.get_event_loop()
        ok, msg = await loop.run_in_executor(
            None, test_integration, iid, self.modules, self.signals,
        )
        return web.json_response({"ok": ok, "message": msg})

    async def _handle_integration_toggle(self, request):
        if not self._origin_ok(request):
            return _json_error(403, "Bad origin.")
        user = self._current_user(request)
        if not user:
            return _json_error(401, "Not logged in.")
        try:
            body = await request.json()
        except Exception:
            return _json_error(400, "Invalid JSON.")
        ok, err = self._require_csrf(request, body)
        if not ok:
            return _json_error(403, err)

        try:
            from modules.integrations import toggle_integration
        except Exception as e:
            return _json_error(503, f"Integrations module unavailable: {e}")

        iid = request.match_info.get("iid", "")
        enabled = bool(body.get("enabled", True))
        loop = asyncio.get_event_loop()
        ok, msg = await loop.run_in_executor(
            None, toggle_integration, iid, enabled,
            self.modules, self.signals,
        )
        if not ok:
            return _json_error(400, msg)
        return web.json_response({"ok": True, "message": msg})

    # ------------------------------------------------------------------
    # Settings (Phase 10.14)
    # ------------------------------------------------------------------
    def _current_session_token(self, request) -> Optional[str]:
        cookies = _parse_cookies(request.headers.get("Cookie", ""))
        return cookies.get(_SESSION_COOKIE)

    async def _handle_settings_general_get(self, request):
        user = self._current_user(request)
        if not user:
            return _json_error(401, "Not logged in.")
        try:
            from modules.settings_manager import get_general
        except Exception as e:
            return _json_error(503, f"Settings module unavailable: {e}")
        return web.json_response(get_general())

    async def _handle_settings_general_put(self, request):
        if not self._origin_ok(request):
            return _json_error(403, "Bad origin.")
        user = self._current_user(request)
        if not user:
            return _json_error(401, "Not logged in.")
        try:
            body = await request.json()
        except Exception:
            return _json_error(400, "Invalid JSON.")
        ok, err = self._require_csrf(request, body)
        if not ok:
            return _json_error(403, err)

        try:
            from modules.settings_manager import save_general, get_general
        except Exception as e:
            return _json_error(503, f"Settings module unavailable: {e}")

        values = body.get("values") or {}
        loop = asyncio.get_event_loop()
        ok, err = await loop.run_in_executor(None, save_general, values)
        if not ok:
            return _json_error(400, f"Save failed: {err}")

        return web.json_response({
            "ok": True,
            "general": get_general(),
        })

    async def _handle_settings_about(self, request):
        user = self._current_user(request)
        if not user:
            return _json_error(401, "Not logged in.")
        try:
            from modules.settings_manager import get_about
        except Exception as e:
            return _json_error(503, f"Settings module unavailable: {e}")

        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(
            None, get_about, self.modules, self.signals,
        )
        return web.json_response(data)

    async def _handle_settings_advanced_get(self, request):
        user = self._current_user(request)
        if not user:
            return _json_error(401, "Not logged in.")
        try:
            from modules.settings_manager import list_reset_targets
        except Exception as e:
            return _json_error(503, f"Settings module unavailable: {e}")
        return web.json_response({"targets": list_reset_targets()})

    async def _handle_settings_reset(self, request):
        if not self._origin_ok(request):
            return _json_error(403, "Bad origin.")
        user = self._current_user(request)
        if not user:
            return _json_error(401, "Not logged in.")
        try:
            body = await request.json()
        except Exception:
            return _json_error(400, "Invalid JSON.")
        ok, err = self._require_csrf(request, body)
        if not ok:
            return _json_error(403, err)

        try:
            from modules.settings_manager import reset_target
        except Exception as e:
            return _json_error(503, f"Settings module unavailable: {e}")

        target = (body.get("target") or "").strip()
        if not target:
            return _json_error(400, "Missing 'target'.")

        loop = asyncio.get_event_loop()
        ok, msg = await loop.run_in_executor(
            None, reset_target, target, self.modules, self.signals,
        )
        if not ok:
            return _json_error(400, msg)
        return web.json_response({"ok": True, "message": msg})

    async def _handle_settings_password(self, request):
        if not self._origin_ok(request):
            return _json_error(403, "Bad origin.")
        user = self._current_user(request)
        if not user:
            return _json_error(401, "Not logged in.")
        try:
            body = await request.json()
        except Exception:
            return _json_error(400, "Invalid JSON.")
        ok, err = self._require_csrf(request, body)
        if not ok:
            return _json_error(403, err)

        old_pw = body.get("old_password") or ""
        new_pw = body.get("new_password") or ""

        ok, msg = self.user_db.change_password(user["id"], old_pw, new_pw)
        if not ok:
            return _json_error(400, msg)
        return web.json_response({"ok": True, "message": msg})

    async def _handle_settings_sessions_list(self, request):
        user = self._current_user(request)
        if not user:
            return _json_error(401, "Not logged in.")
        token = self._current_session_token(request)
        sessions = self.user_db.list_sessions(user["id"], current_token=token)
        return web.json_response({"sessions": sessions})

    async def _handle_settings_session_revoke(self, request):
        if not self._origin_ok(request):
            return _json_error(403, "Bad origin.")
        user = self._current_user(request)
        if not user:
            return _json_error(401, "Not logged in.")
        try:
            body = await request.json()
        except Exception:
            return _json_error(400, "Invalid JSON.")
        ok, err = self._require_csrf(request, body)
        if not ok:
            return _json_error(403, err)

        prefix = (body.get("prefix") or "").strip()
        if not prefix:
            return _json_error(400, "Missing session prefix.")

        current = self._current_session_token(request)
        if current and current.startswith(prefix.rstrip("…")):
            return _json_error(
                400,
                "Cannot revoke the current session. Use Sign out instead.",
            )

        ok, msg = self.user_db.delete_session_by_prefix(user["id"], prefix)
        if not ok:
            return _json_error(400, msg)
        return web.json_response({"ok": True, "message": msg})

    async def _handle_settings_session_revoke_others(self, request):
        if not self._origin_ok(request):
            return _json_error(403, "Bad origin.")
        user = self._current_user(request)
        if not user:
            return _json_error(401, "Not logged in.")
        try:
            body = await request.json()
        except Exception:
            return _json_error(400, "Invalid JSON.")
        ok, err = self._require_csrf(request, body)
        if not ok:
            return _json_error(403, err)

        token = self._current_session_token(request)
        n = self.user_db.delete_other_sessions(user["id"], keep_token=token)
        return web.json_response({
            "ok": True,
            "revoked": int(n),
            "message": f"Revoked {n} other session(s).",
        })

    # ------------------------------------------------------------------
    # Voice in browser (Phase 10.19)
    # ------------------------------------------------------------------
    def _broadcast_tts_audio(self, audio_bytes):
        """Called by tts.py when an utterance starts playing.

        Runs on the TTS playback thread, not the aiohttp event loop.
        The coroutine must be scheduled onto the socket.io loop with
        run_coroutine_threadsafe; start_background_task() does not
        work cross-thread.
        """
        if not self._tts_clients:
            return
        if self._loop is None or self._loop.is_closed():
            return

        try:
            b64 = base64.b64encode(audio_bytes).decode("ascii")
        except Exception:
            return

        async def _send():
            try:
                await self.sio.emit("tts_audio", {
                    "format": "wav",
                    "data": b64,
                })
            except Exception:
                pass

        try:
            asyncio.run_coroutine_threadsafe(_send(), self._loop)
        except Exception as e:
            print(f"\033[90mSOCKETIO: tts broadcast failed — {e}\033[0m")

    async def _handle_voice_message(self, request):
        if not self._origin_ok(request):
            return _json_error(403, "Bad origin.")
        user = self._current_user(request)
        if not user:
            return _json_error(401, "Not logged in.")
        ok, err = self._require_csrf_header(request)
        if not ok:
            return _json_error(403, err)

        try:
            reader = await request.multipart()
            field = await reader.next()
            if field is None or field.name != "audio":
                return _json_error(400, "Expected multipart field 'audio'.")
            chunks_in = []
            total = 0
            while True:
                chunk = await field.read_chunk(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_VOICE_BYTES:
                    return _json_error(
                        413,
                        f"Voice clip too large "
                        f"(limit {_MAX_VOICE_BYTES // (1024*1024)} MB).",
                    )
                chunks_in.append(chunk)
            blob = b"".join(chunks_in)
            if not blob:
                return _json_error(400, "Empty audio blob.")
            # The browser names the clip after the container it actually
            # recorded (webm on Chrome/Firefox, mp4 on Safari).
            suffix = os.path.splitext(field.filename or "")[1].lower()
            if suffix not in _VOICE_EXTS:
                suffix = ".webm"
        except Exception as e:
            return _json_error(400, f"Could not read audio: {e}")

        loop = asyncio.get_event_loop()

        def _transcribe():
            try:
                from faster_whisper import WhisperModel  # noqa: F401
            except ImportError:
                return None, "faster-whisper is not installed."
            try:
                import tempfile
                fd, tmp = tempfile.mkstemp(suffix=suffix)
                os.close(fd)
                try:
                    with open(tmp, "wb") as f:
                        f.write(blob)
                    model = _get_voice_whisper_model()
                    segments, _ = model.transcribe(tmp, beam_size=5)
                    text = " ".join(s.text for s in segments).strip()
                finally:
                    try:
                        os.remove(tmp)
                    except Exception:
                        pass
                return text, None
            except Exception as e:
                return None, str(e)

        text, err = await loop.run_in_executor(None, _transcribe)
        if err:
            return _json_error(500, f"Transcription failed: {err}")
        if not text:
            return web.json_response({"ok": True, "text": ""})
        if _is_chat_command(text) and self._is_remote(request):
            return _json_error(403, "Commands only work on the host machine.")

        self.signals.history.append({"role": "user", "content": text})
        self.signals.new_message = True
        self.signals.last_message_time = time.time()
        if hasattr(self.signals, "save_history"):
            self.signals.save_history()

        await self.sio.emit("web_message", {
            "role": "user",
            "content": text,
            "ts": time.time(),
        })

        return web.json_response({"ok": True, "text": text})

    # ------------------------------------------------------------------
    # Page handlers (server-side gating)
    # ------------------------------------------------------------------
    async def _handle_index(self, request):
        if not self.user_db.has_users():
            raise web.HTTPFound("/setup")
        user = self._current_user(request)
        if not user:
            raise web.HTTPFound("/login")
        return await self._serve_file(os.path.join(_WEB_ROOT, "index.html"))

    async def _handle_login_page(self, request):
        if not self.user_db.has_users():
            raise web.HTTPFound("/setup")
        if self._current_user(request):
            raise web.HTTPFound("/")
        return await self._serve_file(os.path.join(_WEB_ROOT, "login.html"))

    async def _handle_register_page(self, request):
        return await self._handle_login_page(request)

    async def _handle_setup_page(self, request):
        # The client decides which step to show based on auth + ?step=.
        # Every step after the first account calls host-only APIs, and
        # the first account can't be made remotely, so remote visitors
        # go to the login page instead.
        if self._is_remote(request):
            raise web.HTTPFound("/login")
        return await self._serve_file(os.path.join(_WEB_ROOT, "setup.html"))

    # ------------------------------------------------------------------
    # Static file serving
    # ------------------------------------------------------------------
    async def _handle_static(self, request):
        rel = request.match_info.get("tail", "")
        if not rel or ".." in rel.split("/") or rel.startswith("/"):
            raise web.HTTPForbidden()
        if "\x00" in rel:
            raise web.HTTPForbidden()

        target = os.path.normpath(os.path.join(_WEB_ROOT, rel))
        if not target.startswith(os.path.abspath(_WEB_ROOT) + os.sep):
            raise web.HTTPForbidden()
        if not os.path.isfile(target):
            raise web.HTTPNotFound()
        return await self._serve_file(target)

    async def _serve_file(self, path):
        if not os.path.isfile(path):
            raise web.HTTPNotFound()
        ctype, _ = mimetypes.guess_type(path)
        return web.FileResponse(path, headers={
            "Content-Type": ctype or "application/octet-stream",
            "Cache-Control": "no-store",
        })

    # ==================================================================
    # Socket.io events
    # ==================================================================
    def _register_socket_events(self):
        sio = self.sio
        signals = self.signals

        @sio.event
        async def get_blacklist(sid):
            if self.llmWrapper:
                await sio.emit(
                    "get_blacklist",
                    self.llmWrapper.API.get_blacklist(), to=sid,
                )

        @sio.event
        async def set_blacklist(sid, message):
            if self.llmWrapper:
                self.llmWrapper.API.set_blacklist(message)

        @sio.event
        async def disable_LLM(sid):
            if self.llmWrapper:
                self.llmWrapper.API.set_LLM_status(False)

        @sio.event
        async def enable_LLM(sid):
            if self.llmWrapper:
                self.llmWrapper.API.set_LLM_status(True)

        @sio.event
        async def cancel_next_message(sid):
            if self.llmWrapper:
                self.llmWrapper.API.cancel_next()

        @sio.event
        async def disable_TTS(sid):
            if self.tts:
                self.tts.API.set_TTS_status(False)

        @sio.event
        async def enable_TTS(sid):
            if self.tts:
                self.tts.API.set_TTS_status(True)

        @sio.event
        async def abort_current_message(sid):
            if self.tts:
                self.tts.API.abort_current()

        @sio.event
        async def disable_STT(sid):
            if self.stt:
                self.stt.API.set_STT_status(False)

        @sio.event
        async def enable_STT(sid):
            if self.stt:
                self.stt.API.set_STT_status(True)

        @sio.event
        async def disable_movement(sid):
            if "vtube_studio" in self.modules:
                self.modules["vtube_studio"].API.set_movement_status(False)

        @sio.event
        async def enable_movement(sid):
            if "vtube_studio" in self.modules:
                self.modules["vtube_studio"].API.set_movement_status(True)

        @sio.event
        async def get_hotkeys(sid):
            if "vtube_studio" in self.modules:
                self.modules["vtube_studio"].API.get_hotkeys()

        @sio.event
        async def send_hotkey(sid, hotkey):
            if "vtube_studio" in self.modules:
                self.modules["vtube_studio"].API.send_hotkey(hotkey)

        @sio.event
        async def trigger_prop(sid, prop_action):
            if "vtube_studio" in self.modules:
                self.modules["vtube_studio"].API.trigger_prop(prop_action)

        @sio.event
        async def move_model(sid, mode):
            if "vtube_studio" in self.modules:
                self.modules["vtube_studio"].API.move_model(mode)

        @sio.event
        async def disable_multimodal(sid):
            if "multimodal" in self.modules:
                self.modules["multimodal"].API.set_multimodal_status(False)

        @sio.event
        async def enable_multimodal(sid):
            if "multimodal" in self.modules:
                self.modules["multimodal"].API.set_multimodal_status(True)

        @sio.event
        async def fun_fact(sid):
            signals.history.append({
                "role": "user",
                "content": "Let's move on. Can you share a quick "
                           "interesting fact?",
            })
            signals.new_message = True

        @sio.event
        async def new_topic(sid, message):
            signals.history.append({"role": "user", "content": message})
            signals.new_message = True

        @sio.event
        async def nuke_history(sid):
            signals.history = []

        @sio.event
        async def play_audio(sid, file_name):
            if "audio_player" in self.modules:
                self.modules["audio_player"].API.play_audio(file_name)

        @sio.event
        async def pause_audio(sid):
            if "audio_player" in self.modules:
                self.modules["audio_player"].API.pause_audio()

        @sio.event
        async def resume_audio(sid):
            if "audio_player" in self.modules:
                self.modules["audio_player"].API.resume_audio()

        @sio.event
        async def abort_audio(sid):
            if "audio_player" in self.modules:
                self.modules["audio_player"].API.stop_audio()

        @sio.event
        async def clear_short_term(sid):
            if "memory" in self.modules:
                self.modules["memory"].API.clear_short_term()
                await sio.emit(
                    "get_memories",
                    self.modules["memory"].API.get_memories(),
                )

        @sio.event
        async def import_json(sid):
            if "memory" in self.modules:
                self.modules["memory"].API.import_json()

        @sio.event
        async def export_json(sid):
            if "memory" in self.modules:
                self.modules["memory"].API.export_json()

        @sio.event
        async def delete_memory(sid, data):
            if "memory" in self.modules:
                self.modules["memory"].API.delete_memory(data)
                await sio.emit(
                    "get_memories",
                    self.modules["memory"].API.get_memories(),
                )

        @sio.event
        async def get_memories(sid, data=""):
            if "memory" in self.modules:
                await sio.emit(
                    "get_memories",
                    self.modules["memory"].API.get_memories(data),
                )

        @sio.event
        async def create_memory(sid, data):
            if "memory" in self.modules:
                self.modules["memory"].API.create_memory(data)
                await sio.emit(
                    "get_memories",
                    self.modules["memory"].API.get_memories(),
                )

        @sio.event
        async def connect(sid, environ):
            origin = environ.get("HTTP_ORIGIN")
            cookie_header = environ.get("HTTP_COOKIE", "")
            has_cookie_header = bool(cookie_header)
            print(f"\033[90mSOCKETIO: connect attempt sid={sid} "
                  f"origin={origin!r} cookie_header={has_cookie_header}\033[0m")

            if origin and not _origin_allowed(origin):
                print(f"\033[91mSOCKETIO:   rejected — bad origin "
                      f"{origin!r}\033[0m")
                raise ConnectionRefusedError("Bad origin.")

            cookies = _parse_cookies(cookie_header)
            has_session_cookie = _SESSION_COOKIE in cookies
            user = self.user_db.validate_session(
                cookies.get(_SESSION_COOKIE)
            )
            if not user:
                print(f"\033[91mSOCKETIO:   rejected — no valid session "
                      f"(cookie present: {has_session_cookie})\033[0m")
                raise ConnectionRefusedError("Not authenticated.")

            req = environ.get("aiohttp.request")
            is_local = self._is_local(req) if req is not None else False
            print(f"\033[92mSOCKETIO:   accepted as {user['username']}"
                  f"{'' if is_local else ' (remote)'}\033[0m")
            await sio.save_session(sid, {"user": user, "is_local": is_local})

            await sio.emit("history", signals.history, to=sid)
            await sio.emit("patience_update", {
                "crr_time": time.time() - signals.last_message_time,
                "total_time": PATIENCE,
            }, to=sid)

            if "audio_player" in self.modules:
                await sio.emit(
                    "audio_list",
                    self.modules["audio_player"].API.get_audio_list(),
                    to=sid,
                )
            if "vtube_studio" in self.modules:
                await sio.emit(
                    "movement_status",
                    self.modules["vtube_studio"].API.get_movement_status(),
                    to=sid,
                )
            if "multimodal" in self.modules:
                await sio.emit(
                    "multimodal_status",
                    self.modules["multimodal"].API.get_multimodal_status(),
                    to=sid,
                )

            await sio.emit("web_ready", {
                "user": user,
                "host": {"name": HOST.name, "user_id": HOST.user_id},
            }, to=sid)

        @sio.event
        def disconnect(sid):
            self._tts_clients.discard(sid)
            print(f"\033[90mSOCKETIO: client {sid} disconnected\033[0m")

        @sio.event
        async def web_send(sid, data):
            sess = await sio.get_session(sid)
            if not sess or not sess.get("user"):
                return
            text = (data or {}).get("text", "").strip()
            if not text:
                return
            if _is_chat_command(text) and not sess.get("is_local"):
                return
            signals.history.append({"role": "user", "content": text})
            signals.new_message = True
            signals.last_message_time = time.time()
            if hasattr(signals, "save_history"):
                signals.save_history()
            await sio.emit("web_message", {
                "role": "user",
                "content": text,
                "ts": time.time(),
            })

        @sio.event
        async def web_toggle_agent(sid):
            sess = await sio.get_session(sid)
            if not sess or not sess.get("user") or not sess.get("is_local"):
                return
            if self.prompter:
                self.prompter.agent_mode_enabled = \
                    not self.prompter.agent_mode_enabled
                await sio.emit("web_agent_mode", {
                    "enabled": self.prompter.agent_mode_enabled,
                })

        @sio.event
        async def web_voice_optin(sid, payload):
            sess = await sio.get_session(sid)
            if not sess or not sess.get("user"):
                return
            enabled = bool((payload or {}).get("enabled", True))
            if enabled:
                self._tts_clients.add(sid)
            else:
                self._tts_clients.discard(sid)

    # ------------------------------------------------------------------
    # Stream control (OBS)
    # ------------------------------------------------------------------
    async def _handle_stream_status(self, request):
        user = self._current_user(request)
        if not user:
            return _json_error(401, "Not logged in.")

        obs_mod = self.modules.get("obs")
        if obs_mod is None:
            obs_status = {"enabled": False, "connected": False,
                          "error": "OBS module is not loaded."}
        else:
            loop = asyncio.get_running_loop()
            obs_status = await loop.run_in_executor(None, obs_mod.status)

        embed = (dotenv_values(_ENV_PATH).get("STREAM_EMBED_URL") or "").strip()
        if not embed.lower().startswith("https://"):
            embed = ""

        return web.json_response({
            "obs": obs_status,
            "embed_url": embed,
            "subtitles": {
                "enabled": OBS_SUBTITLES_ENABLED,
                "file": OBS_SUBTITLE_FILE,
            },
            "remote": self._is_remote(request),
            "public_origins": sorted(_public_origins()),
        })

    async def _stream_action(self, request, action):
        """Shared guard + dispatch for the POST stream routes. action
        takes (obs_module, body) and runs on a worker thread."""
        if not self._origin_ok(request):
            return _json_error(403, "Bad origin.")
        user = self._current_user(request)
        if not user:
            return _json_error(401, "Not logged in.")
        try:
            body = await request.json()
        except Exception:
            body = {}
        ok, err = self._require_csrf(request, body)
        if not ok:
            return _json_error(403, err)

        obs_mod = self.modules.get("obs")
        if obs_mod is None:
            return _json_error(503, "OBS module is not loaded.")

        from modules.obs_control import OBSError
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, action, obs_mod, body)
        except OBSError as e:
            return _json_error(502, str(e))
        status = await loop.run_in_executor(None, obs_mod.status)
        return web.json_response({"ok": True, "obs": status})

    async def _handle_stream_scene(self, request):
        return await self._stream_action(
            request, lambda m, body: m.switch_scene(body.get("scene")))

    async def _handle_stream_start(self, request):
        return await self._stream_action(
            request, lambda m, body: m.start_stream())

    async def _handle_stream_stop(self, request):
        return await self._stream_action(
            request, lambda m, body: m.stop_stream())

    # ==================================================================
    # Background: broadcast new history entries
    # ==================================================================
    async def _broadcast_history(self):
        last_len = len(self.signals.history)
        last_content = (self.signals.history[-1].get("content", "")
                        if self.signals.history else "")
        while not self.signals.terminate:
            try:
                hist = self.signals.history
                if len(hist) > last_len:
                    for msg in hist[last_len:]:
                        await self.sio.emit("web_message", {
                            "role": msg.get("role"),
                            "content": msg.get("content"),
                            "ts": time.time(),
                        })
                    last_len = len(hist)
                    last_content = hist[-1].get("content", "") if hist else ""
                elif hist and hist[-1].get("content") != last_content:
                    last_content = hist[-1].get("content", "")
                    await self.sio.emit("web_message_edit", {
                        "content": last_content,
                    })
                elif len(hist) < last_len:
                    last_len = len(hist)
            except Exception as e:
                print(f"\033[90mSOCKETIO broadcast: {e}\033[0m")
            await self.sio.sleep(0.25)

    # ==================================================================
    # Entry point
    # ==================================================================
    def run(self):
        self.start_server()

    def start_server(self):
        try:
            from modules.log_hub import emit as hub_emit
            hub_emit(self.signals, "system",
                     "socket.io + web client on 127.0.0.1:5000")
        except Exception:
            pass

        async def _on_startup(app):
            self._loop = asyncio.get_running_loop()
            self.sio.start_background_task(self._broadcast_history)

        self.app.on_startup.append(_on_startup)

        try:
            from constants import ALLOW_LAN_EXPOSURE
        except Exception:
            ALLOW_LAN_EXPOSURE = False

        bind_host = "0.0.0.0" if ALLOW_LAN_EXPOSURE else "127.0.0.1"
        if bind_host != "127.0.0.1":
            print(
                f"\033[93mSOCKETIO: WARNING — binding to "
                f"{bind_host} (LAN exposure enabled)\033[0m"
            )

        async def _serve():
            runner = await self.start_sites(bind_host, 5000, TUNNEL_PORT)
            try:
                while not self.signals.terminate:
                    await asyncio.sleep(0.5)
            finally:
                await runner.cleanup()

        asyncio.run(_serve())

    async def start_sites(self, host="127.0.0.1", port=5000,
                          tunnel_port=TUNNEL_PORT):
        """Start the main listener and the loopback-only tunnel listener
        on one app. Port 0 picks a free port (tests). Returns the runner;
        the bound ports end up in self.port and self.tunnel_port."""
        runner = web.AppRunner(self.app)
        await runner.setup()

        main_sock = socket.create_server((host, port))
        await web.SockSite(runner, main_sock).start()
        self.port = main_sock.getsockname()[1]

        if tunnel_port is not None:
            try:
                tsock = socket.create_server(("127.0.0.1", tunnel_port))
            except OSError as e:
                print(f"\033[91mSOCKETIO: tunnel port {tunnel_port} "
                      f"unavailable ({e}) — remote access disabled\033[0m")
            else:
                await web.SockSite(runner, tsock).start()
                self.tunnel_port = tsock.getsockname()[1]
        return runner

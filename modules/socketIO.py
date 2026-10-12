"""
Socket.io server + local HTTP/SPA host for the Phase 10 web client.

Serves:
  1. Socket.io events on the default namespace
  2. HTTP routes: /auth/*, /api/*, /api/memories/*, /api/persona/*,
     /api/models/*, /api/providers/*, /api/integrations/*,
     /api/settings/*, /api/voice_message
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

Security posture (Phase 10.17):
  - Bound strictly to 127.0.0.1
  - HttpOnly, SameSite=Lax session cookie
  - CSRF on every state-changing POST/PATCH/PUT/DELETE
  - Origin validated on socket.io handshake and HTTP POSTs
  - Static handler refuses path traversal
"""

import os
import io
import time
import base64
import secrets
import asyncio
import mimetypes
from typing import Optional

from aiohttp import web
import socketio

from constants import PATIENCE, HOST
from modules.user_auth import UserDB

_ALLOWED_ORIGINS = {
    "http://127.0.0.1:5000",
    "http://localhost:5000",
}

_WEB_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web"
)

_SESSION_COOKIE = "lumi_session"
_CSRF_COOKIE = "lumi_csrf"


def _json_error(status, message):
    return web.json_response({"error": message}, status=status)


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
            cors_allowed_origins=list(_ALLOWED_ORIGINS),
            logger=False,
            engineio_logger=False,
        )
        self.app = web.Application()
        self.sio.attach(self.app)

        # Phase 10.19 — voice in browser.
        self._tts_clients = set()
        self._loop = None 
        self.signals.broadcast_tts_audio = self._broadcast_tts_audio

        self._register_http_routes()
        self._register_socket_events()

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

    def _current_user(self, request):
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

    def _origin_ok(self, request):
        origin = request.headers.get("Origin")
        if origin is None:
            return True
        return origin in _ALLOWED_ORIGINS

    def _set_session_cookies(self, response, session_token, csrf_token):
        response.set_cookie(
            _SESSION_COOKIE, session_token,
            httponly=True, samesite="Lax", secure=False, path="/",
            max_age=60 * 60 * 24 * 30,
        )
        response.set_cookie(
            _CSRF_COOKIE, csrf_token,
            httponly=False, samesite="Lax", secure=False, path="/",
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
        })

    async def _handle_register(self, request):
        if not self._origin_ok(request):
            return _json_error(403, "Bad origin.")
        try:
            body = await request.json()
        except Exception:
            return _json_error(400, "Invalid JSON.")

        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        display = (body.get("display_name") or "").strip() or None

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
        self._set_session_cookies(response, session_token, csrf_token)
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
            return _json_error(401, payload)

        session_token = self.user_db.create_session(payload["id"])
        csrf_token = secrets.token_urlsafe(32)

        HOST.update(name=payload["display_name"], user_id=payload["username"])

        response = web.json_response({
            "ok": True,
            "user": payload,
            "csrf": csrf_token,
        })
        self._set_session_cookies(response, session_token, csrf_token)
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
        csrf = cookies.get(_CSRF_COOKIE, "")
        return web.json_response({
            "user": user,
            "csrf": csrf,
            "host": {"name": HOST.name, "user_id": HOST.user_id},
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
            "agent_mode":   bool(getattr(
                self.prompter, "agent_mode_enabled", False
            )) if self.prompter else False,
            "log_hub_port": getattr(
                getattr(signals, "log_hub", None), "port", 5001
            ),
        })

    async def _handle_history(self, request):
        user = self._current_user(request)
        if not user:
            return _json_error(401, "Not logged in.")
        limit = int(request.query.get("limit", 40))
        limit = max(1, min(limit, 200))
        history = getattr(self.signals, "history", [])[-limit:]
        return web.json_response({"history": history})

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
        if not text:
            return _json_error(400, "Empty message.")

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

        return web.json_response({"ok": True})

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
                hits = api.query_temporal(q, limit=limit)
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

            data = api.list_all(limit=limit, offset=offset, sort=sort)
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
            return web.json_response({"memories": api.export_all()})
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
            new_id = api.create_memory(fact, importance=importance,
                                       valence=valence)
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
        row = api.get_by_id(mem_id)
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

        if valence is None and "valence" in body:
            valence = body.get("valence")

        ok, err = api.update_memory(
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

        deleted = api.delete_memory(mem_id)
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
            installed = await loop.run_in_executor(None, mm.list_installed)
            loaded    = await loop.run_in_executor(None, mm.list_loaded)
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

        # NEW: optional flag — when True, also set routing
        set_routing = bool(body.get("set_routing", True))
        target = (body.get("target") or "main").lower()  # "main" | "fast" | "both"

        loop = asyncio.get_event_loop()

        # 1. Load into VRAM first
        ok, err = await loop.run_in_executor(None, mm.preload, name)
        if not ok:
            return _json_error(400, f"Load failed: {err}")

        # 2. Optionally patch routing
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
        for p in providers:
            if p["id"] == "ollama":
                continue
            if not p["configured"]:
                continue
            try:
                client = get_client(p["id"])
                models, err = client.list_models()
                if err or not models:
                    continue
                for m in models:
                    catalog.append({
                        "provider": p["id"],
                        "provider_label": p["label"],
                        "name": m["name"],
                        "spec": f"{p['id']}:{m['name']}",
                    })
            except Exception:
                continue

        catalog.sort(key=lambda x: (x["provider"], x["name"]))
        return {"providers": providers, "catalog": catalog}, None

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

        try:
            reader = await request.multipart()
            field = await reader.next()
            if field is None or field.name != "audio":
                return _json_error(400, "Expected multipart field 'audio'.")
            blob = await field.read()
            if not blob:
                return _json_error(400, "Empty audio blob.")
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
                fd, tmp = tempfile.mkstemp(suffix=".webm")
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
        # Always serve the wizard. The client decides which step to
        # show based on auth + ?step=.
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

            if origin and origin not in _ALLOWED_ORIGINS:
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

            print(f"\033[92mSOCKETIO:   accepted as {user['username']}\033[0m")
            await sio.save_session(sid, {"user": user})

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
            if not sess or not sess.get("user"):
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

        web.run_app(self.app, host=bind_host, port=5000, print=None)
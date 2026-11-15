"""
Diagnostics: catch what goes wrong anywhere in Lumi and say how to fix it.

Collected into one ring buffer (deduplicated: a repeating error is one row
with a count):

  * uncaught exceptions in any thread (sys.excepthook, threading.excepthook)
  * Python logging at ERROR+, which includes asyncio's "Task exception was
    never retrieved" and library errors
  * red console output: Lumi's modules report most failures by printing in
    red (\\033[91m) rather than raising, so stdout is teed and red lines
    are kept
  * log_hub events at warn/error level (modules/log_hub.py forwards them)
  * errors from the web app in the browser (POST /api/debug/client_error)

Each error is matched against FIX_RULES (known failure -> plain-language
cause and fix). run_checks() adds live health checks (Ollama and models,
VTube Studio, voice, mic, accounts, packages, disk, memory, encoding...).
The web Dashboard shows both: GET /api/debug/diagnostics.

Everything here is best-effort and must never take Lumi down: every hook
swallows its own failures.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import re
import shutil
import sys
import threading
import time
import traceback
from collections import OrderedDict
from typing import Any, Dict, List, Optional

MAX_ERRORS = 200
CHECK_CACHE_S = 10.0
_RED = "\x1b[91m"
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_SECRETS = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|ghp_[A-Za-z0-9]{8,}|AIza[0-9A-Za-z_\-]{10,}|"
                      r"(?i:(api[_-]?key|token|password|secret)\s*[=:]\s*)\S+)")


# ─── known problems -> fixes ──────────────────────────────────────────
# (pattern, title, fix). First match wins; keep specific rules first.
FIX_RULES = [
    (r"11434|ollama.*(refused|unreachable|connect)|couldn't reach ollama|Max retries exceeded.*11434",
     "Ollama isn't running",
     "Start Ollama (run `ollama serve`, or open the Ollama app), then try again. Lumi expects it at "
     "http://127.0.0.1:11434 (LLM_ENDPOINT in constants.py)."),
    (r"model ['\"]?([\w.:\-/]+)['\"]? not found|isn't pulled|try pulling it|pull model manifest",
     "A model isn't downloaded",
     "Pull it: `ollama pull <model>` (the name is in the error). Check MODEL_NAME, AGENT_MODEL_NAME and "
     "FAST_MODEL_NAME in constants.py match what `ollama list` shows."),
    (r"can't call tools|does not support tools",
     "The agent model can't use tools",
     "Set AGENT_MODEL_NAME in constants.py to a tool-capable model such as qwen3:8b."),
    (r"CUDA out of memory|out of memory|cudaMalloc|OOM",
     "Out of GPU memory",
     "Close other GPU-heavy apps (games, other models), use a smaller model, or lower MAIN_NUM_CTX in "
     "constants.py. `ollama ps` shows what's loaded."),
    (r"VTube Studio.*(connection failed|refused)|8001.*refused|vts.*connection lost",
     "Can't reach VTube Studio",
     "Open VTube Studio, then Settings → Start API (port 8001). The first time, click Allow on the "
     "\"Lumi VTS Plugin\" popup. Lumi reconnects on her own."),
    (r"vts.*auth(entication)? failed|authenticationToken",
     "VTube Studio refused the plugin",
     "In VTube Studio, Settings → Plugins: allow \"Lumi VTS Plugin\". If you revoked it, delete "
     "data/vtubeStudio_token.txt and restart Lumi to ask again."),
    (r"keybinding|could not focus VTS window",
     "VTube Studio hotkey couldn't be sent",
     "The keyboard hotkey path needs the VTS window: make sure it isn't minimized, and that the key combo in "
     "HOTKEY_KEYBINDINGS (modules/vtubeStudio.py) matches VTS's hotkey settings."),
    (r"fish.*(401|403|unauthori)|FISH_API_KEY|invalid api key.*fish",
     "Fish Audio rejected the key",
     "Check FISH_API_KEY in .env (Settings → Integrations in the web app), then restart."),
    (r"fish.*(429|quota|credit)|insufficient (balance|credit)",
     "Fish Audio quota used up",
     "Top up Fish Audio or switch TTS to the local voice in Settings."),
    (r"PyAudio|portaudio|Invalid output device|Invalid number of channels|PaErrorCode",
     "Audio device problem",
     "The output device index in constants.py (OUTPUT_DEVICE_INDEX) may have changed after plugging "
     "devices in or out. Run `python -m sounddevice` or check Settings and pick the device again."),
    (r"ffmpeg",
     "FFmpeg missing",
     "Install FFmpeg and put it on PATH (`ffmpeg -version` should work). Discord voice needs it."),
    (r"invalid_grant|token has been expired or revoked|Reconnect (it|Google)|sign-in expired",
     "An account sign-in expired",
     "Open Settings → Connections in the web app and reconnect the account."),
    (r"pywinauto isn't installed|No module named 'pywinauto'",
     "Desktop tools need pywinauto",
     "Run `pip install pywinauto` in the lumi environment."),
    (r"No module named '([\w.]+)'",
     "A Python package is missing",
     "Install it in the lumi environment: `pip install -r requirements.txt` (or `pip install <package>` for the "
     "name in the error)."),
    (r"UnicodeEncodeError|charmap.*codec can't encode",
     "The console can't print some characters",
     "Set PYTHONIOENCODING=utf-8 (setx PYTHONIOENCODING utf-8, then open a new terminal), or run Lumi in "
     "Windows Terminal."),
    (r"address already in use|WinError 10048|only one usage of each socket address",
     "A port is already taken",
     "Another Lumi (or another app) is already using the port. Close the other copy, or check "
     "`netstat -ano | findstr :5000` (5090, 8001...) to find it."),
    (r"database is locked|chroma.*(lock|corrupt)|sqlite3\.OperationalError",
     "The memory database is busy or damaged",
     "Make sure only one Lumi is running. If it persists, restart Lumi; as a last resort back up and remove "
     "data/memories/chroma."),
    (r"Playwright|Executable doesn't exist|browserType\.launch",
     "The browser for browse_url isn't installed",
     "Run `playwright install firefox` in the lumi environment."),
    (r"DDGSException|ratelimit|202 Ratelimit",
     "Web search was rate-limited",
     "DuckDuckGo throttled the searches. Wait a minute and ask again."),
    (r"Timeout|timed out|ReadTimeout",
     "Something took too long",
     "A service (model, web page or account) didn't answer in time. If it keeps happening with the model, "
     "it may be too large for your GPU: try a smaller one."),
    (r"\b40[13]\b|Unauthorized|Forbidden",
     "Access was refused",
     "A key or sign-in is wrong or missing for the service named in the error. Check Settings → Integrations."),
    (r"Permission ?Error|Access is denied",
     "Windows denied access",
     "Another program may have the file open, or Lumi needs to run without admin restrictions for that "
     "folder. Close the other program and try again."),
]
_COMPILED = [(re.compile(p, re.I), t, f) for p, t, f in FIX_RULES]


def suggest_fix(text: str) -> Optional[Dict[str, str]]:
    for rx, title, fix in _COMPILED:
        if rx.search(text or ""):
            return {"title": title, "fix": fix}
    return None


def _scrub(text: str) -> str:
    """No ANSI colours, no keys or passwords in what the dashboard shows."""
    def hide(m):
        return f"{m.group(2)}=[hidden]" if m.group(2) else "[hidden]"
    return _SECRETS.sub(hide, _ANSI.sub("", str(text or "")))


def _signature(source: str, message: str) -> str:
    """Group repeats: drop numbers, ids and paths that change each time."""
    m = re.sub(r"0x[0-9a-f]+|\b\d+(\.\d+)?\b|[0-9a-f]{8,}", "#", message.lower())
    return f"{source}:{m[:160]}"


# ─── the collector ────────────────────────────────────────────────────

class Collector:
    def __init__(self, maxlen: int = MAX_ERRORS):
        self._lock = threading.Lock()
        self._items: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self.maxlen = maxlen
        self.installed = False
        self.signals = None
        self._client_times: List[float] = []

    def record(self, source: str, message: str, level: str = "error", detail: str = "") -> None:
        try:
            message = _scrub(message).strip()
            if not message:
                return
            detail = _scrub(detail)[-4000:]
            sig = _signature(source, message)
            now = time.time()
            with self._lock:
                item = self._items.pop(sig, None)
                if item:
                    item["count"] += 1
                    item["last"] = now
                    if detail:
                        item["detail"] = detail
                else:
                    item = {"source": source, "level": level, "message": message[:600], "detail": detail,
                            "first": now, "last": now, "count": 1,
                            "fix": suggest_fix(message + "\n" + detail)}
                self._items[sig] = item                       # most recent last
                while len(self._items) > self.maxlen:
                    self._items.popitem(last=False)
        except Exception:
            pass

    def record_client(self, data: Dict[str, Any]) -> bool:
        """A browser error (rate-limited: 30 a minute)."""
        now = time.time()
        with self._lock:
            self._client_times = [t for t in self._client_times if now - t < 60]
            if len(self._client_times) >= 30:
                return False
            self._client_times.append(now)
        msg = str(data.get("message") or "")[:500]
        where = " ".join(str(data.get(k) or "") for k in ("source", "line")).strip()
        page = str(data.get("view") or "")
        self.record("browser", f"{msg}" + (f" (page: {page})" if page else ""), "error",
                    f"{where}\n{str(data.get('stack') or '')[:3000]}".strip())
        return True

    def errors(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock:
            items = list(self._items.values())[-limit:]
        return [dict(i) for i in reversed(items)]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


collector = Collector()


def record(source, message, level="error", detail=""):
    collector.record(source, message, level, detail)


# ─── hooks ────────────────────────────────────────────────────────────

class _LogHandler(logging.Handler):
    def emit(self, rec: logging.LogRecord) -> None:
        try:
            detail = "".join(traceback.format_exception(*rec.exc_info)) if rec.exc_info else ""
            collector.record(f"log:{rec.name}", rec.getMessage(), "error", detail)
        except Exception:
            pass


class _RedTee:
    """stdout wrapper: passes everything through, keeps red lines."""

    def __init__(self, stream):
        self._s = stream
        self._buf = ""

    def write(self, data):
        try:
            if _RED in data or self._buf:
                self._buf += data
                while "\n" in self._buf:
                    line, self._buf = self._buf.split("\n", 1)
                    if _RED in line:
                        clean = _ANSI.sub("", line).strip()
                        if clean:
                            src = re.match(r"^\[?([A-Za-z][\w .-]{1,30}?)[\]:]", clean)
                            collector.record(f"console:{src.group(1).strip().lower()}" if src else "console",
                                             clean, "error")
                if len(self._buf) > 4000:
                    self._buf = ""
        except Exception:
            self._buf = ""
        return self._s.write(data)

    def flush(self):
        return self._s.flush()

    def __getattr__(self, name):
        return getattr(self._s, name)


def install(signals=None) -> None:
    """Hook everything once, early in main.py."""
    if collector.installed:
        return
    collector.installed = True
    collector.signals = signals

    prev_hook = sys.excepthook

    def excepthook(t, v, tb):
        collector.record("uncaught", f"{t.__name__}: {v}", "error", "".join(traceback.format_exception(t, v, tb)))
        prev_hook(t, v, tb)
    sys.excepthook = excepthook

    prev_thread_hook = threading.excepthook

    def thread_hook(args):
        if args.exc_type is not SystemExit:
            collector.record(f"thread:{getattr(args.thread, 'name', '?')}",
                             f"{args.exc_type.__name__}: {args.exc_value}", "error",
                             "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))
        prev_thread_hook(args)
    threading.excepthook = thread_hook

    h = _LogHandler(level=logging.ERROR)
    logging.getLogger().addHandler(h)

    if not isinstance(sys.stdout, _RedTee):
        sys.stdout = _RedTee(sys.stdout)


def on_log_event(category: str, level: str, message: str) -> None:
    """Called by modules/log_hub.py for warn/error events."""
    if level in ("error", "warn", "warning"):
        collector.record(f"log:{category}", message, "error" if level == "error" else "warn")


# ─── health checks ────────────────────────────────────────────────────

def _check(cid, name, status, detail="", fix=""):
    return {"id": cid, "name": name, "status": status, "detail": detail, "fix": fix}


def _ollama_checks(constants) -> List[Dict[str, str]]:
    import requests
    base = getattr(constants, "LLM_ENDPOINT", "http://127.0.0.1:11434").rstrip("/")
    try:
        r = requests.get(f"{base}/api/tags", timeout=3)
        r.raise_for_status()
        have = {m.get("name", "") for m in r.json().get("models", [])}
    except Exception as e:
        return [_check("ollama", "Ollama", "error", f"Not reachable at {base} ({type(e).__name__}).",
                       "Start Ollama (`ollama serve` or the Ollama app).")]
    out = [_check("ollama", "Ollama", "ok", f"Running, {len(have)} model(s) installed.")]

    def has(m):
        return m in have or f"{m}:latest" in have or any(h.split(":")[0] == m for h in have if ":" not in m)

    wanted = [("MODEL_NAME", True), ("AGENT_MODEL_NAME", True), ("FAST_MODEL_NAME", False),
              ("ROUTER_MODEL", False), ("VISION_MODEL_NAME", False)]
    from modules.llm_providers import parse_model_spec
    for key, required in wanted:
        spec = getattr(constants, key, None)
        if not spec:
            continue
        provider, model = parse_model_spec(spec)
        if provider != "ollama" or not model:
            continue
        if key == "FAST_MODEL_NAME" and not getattr(constants, "FAST_MODEL_ENABLED", False):
            continue
        if has(model):
            out.append(_check(f"model:{key}", key, "ok", f"{model} is installed."))
        else:
            out.append(_check(f"model:{key}", key, "error" if required else "warn",
                              f"{model} isn't downloaded.", f"Run `ollama pull {model}`."))
    return out


def run_checks(signals=None) -> List[Dict[str, str]]:
    import constants
    s = signals or collector.signals or object()
    checks: List[Dict[str, str]] = []

    try:
        checks += _ollama_checks(constants)
    except Exception as e:
        checks.append(_check("ollama", "Ollama", "warn", f"Check failed: {e}"))

    vts = getattr(s, "vts_state", None)
    if vts == "ready":
        checks.append(_check("vts", "VTube Studio", "ok", "Connected."))
    elif vts == "skipped":
        checks.append(_check("vts", "VTube Studio", "ok", "Turned off."))
    elif vts == "error":
        checks.append(_check("vts", "VTube Studio", "error", getattr(s, "vts_detail", "") or "Couldn't connect.",
                             "Open VTube Studio → Settings → Start API (port 8001), allow \"Lumi VTS Plugin\", "
                             "then restart Lumi (she reconnects on her own after a drop)."))
    else:
        checks.append(_check("vts", "VTube Studio", "warn", getattr(s, "vts_detail", "") or "Connecting…",
                             "If this doesn't clear, check that VTube Studio is open with its API on."))

    for flag, name, fix in (
            ("tts_ready", "Voice (TTS)", "Check FISH_API_KEY in Settings → Integrations, or switch to the local voice."),
            ("mic_ready", "Microphone", "Check the input device in Windows sound settings, or Lumi's mic settings."),
            ("memory_ready", "Memory", "Restart Lumi. If it keeps failing, see the errors below for the database message.")):
        v = getattr(s, flag, None)
        if v is True:
            checks.append(_check(flag, name, "ok", "Ready."))
        elif v is False:
            checks.append(_check(flag, name, "error", "Failed to start.", fix))

    hub = getattr(s, "workspace_hub", None)
    try:
        if hub is not None:
            checks.append(_check("workspace", "Email & calendar", "ok" if hub.any_connected() else "warn",
                                 "Connected." if hub.any_connected() else "No account connected: email tools are hidden.",
                                 "" if hub.any_connected() else "Connect Google or KSU in Settings → Connections."))
    except Exception:
        pass

    try:
        from modules import hitl_server
        checks.append(_check("hitl", "Approvals server", "ok" if hitl_server._server.running else "warn",
                             "Listening on 127.0.0.1:5090." if hitl_server._server.running
                             else "Not running: approvals use the terminal.",
                             "" if hitl_server._server.running else
                             "Port 5090 may be taken by another Lumi. Close it and restart."))
    except Exception:
        pass

    wd = getattr(s, "watchdog", None)
    if wd is not None:
        checks.append(_check("watchdog", "Watchdog", "ok" if wd.enabled else "warn",
                             f"Watching CPU/RAM and {', '.join(wd.games) or 'no games'}." if wd.enabled
                             else "Off.", "" if wd.enabled else "Set WATCHDOG_ENABLED = True in constants.py."))

    missing = [(mod, why) for mod, why in (
        ("psutil", "the watchdog"), ("mss", "the VTS live view"), ("PIL", "images and the live view"),
        ("ddgs", "web search"), ("playwright", "browse_url"), ("numpy", "lip-sync vowels"),
        ("pywinauto", "read_window_text / click_button")) if importlib.util.find_spec(mod) is None]
    if missing:
        checks.append(_check("packages", "Python packages", "warn",
                             "Missing: " + ", ".join(f"{m} ({why})" for m, why in missing),
                             "Run `pip install -r requirements.txt` in the lumi environment."))
    else:
        checks.append(_check("packages", "Python packages", "ok", "All optional packages installed."))

    try:
        from constants import DATA_ROOT
        free = shutil.disk_usage(DATA_ROOT).free / 2**30
        checks.append(_check("disk", "Disk space", "ok" if free > 5 else "warn" if free > 1 else "error",
                             f"{free:.1f} GB free on Lumi's drive.",
                             "" if free > 5 else "Free up space: logs (data/logs) and models (`ollama rm`) are the big ones."))
    except Exception:
        pass

    try:
        import psutil
        vm = psutil.virtual_memory()
        checks.append(_check("ram", "Memory (RAM)", "ok" if vm.percent < 90 else "warn",
                             f"{vm.percent:.0f}% used ({vm.available / 2**30:.1f} GB free).",
                             "" if vm.percent < 90 else "Close heavy apps, or use smaller models."))
    except Exception:
        pass

    enc = (getattr(getattr(sys.stdout, "_s", sys.stdout), "encoding", "") or "").lower()
    if enc and "utf" not in enc:
        checks.append(_check("encoding", "Console encoding", "warn",
                             f"The console uses {enc}: some characters (arrows, emoji) can crash a print.",
                             "Run `setx PYTHONIOENCODING utf-8`, then start Lumi from a new terminal."))

    from constants import DATA_ROOT
    if not os.path.isfile(os.path.join(DATA_ROOT, "finances.csv")):
        checks.append(_check("finances", "Finances file", "info",
                             "No data/finances.csv: track_expenses has nothing to read.",
                             "Export transactions from your bank as CSV and save it as data/finances.csv."))
    return checks


_check_cache: Dict[str, Any] = {"at": 0.0, "data": []}


def diagnostics(signals=None, refresh: bool = False) -> Dict[str, Any]:
    now = time.time()
    if refresh or now - _check_cache["at"] > CHECK_CACHE_S:
        try:
            _check_cache["data"] = run_checks(signals)
        except Exception as e:
            _check_cache["data"] = [_check("checks", "Health checks", "warn", f"Couldn't run: {e}")]
        _check_cache["at"] = now
    checks = _check_cache["data"]
    errors = collector.errors()
    recent = [e for e in errors if now - e["last"] < 3600]
    bad = sum(1 for c in checks if c["status"] == "error")
    warn = sum(1 for c in checks if c["status"] == "warn")
    return {
        "summary": {"checks_failed": bad, "checks_warn": warn,
                    "errors_last_hour": sum(e["count"] for e in recent if e["level"] == "error"),
                    "status": "error" if bad or any(e["level"] == "error" for e in recent[:20]) else
                              "warn" if warn or recent else "ok",
                    "hooked": collector.installed},
        "checks": checks,
        "errors": errors,
        "at": now,
    }


# ─── routes (socketIO.py) ─────────────────────────────────────────────

def register(router, server) -> None:
    """GET /api/debug/diagnostics, POST /api/debug/clear (host only),
    POST /api/debug/client_error (any signed-in page)."""
    from aiohttp import web
    import asyncio
    from modules.socketIO import _json_error

    async def get_diag(request):
        if not server._current_user(request):
            return _json_error(401, "Not logged in.")
        refresh = request.query.get("refresh") == "1"
        data = await asyncio.get_running_loop().run_in_executor(None, diagnostics, server.signals, refresh)
        return web.json_response(data)

    async def clear(request):
        if not server._origin_ok(request) or not server._current_user(request):
            return _json_error(401, "Not logged in.")
        try:
            body = await request.json()
        except Exception:
            body = {}
        ok, err = server._require_csrf(request, body if isinstance(body, dict) else {})
        if not ok:
            return _json_error(403, err)
        collector.clear()
        return web.json_response({"ok": True})

    async def client_error(request):
        if not server._origin_ok(request) or not server._current_user(request):
            return _json_error(401, "Not logged in.")
        try:
            body = await request.json()
        except Exception:
            return _json_error(400, "Bad JSON.")
        if not isinstance(body, dict):
            return _json_error(400, "Bad JSON.")
        ok, err = server._require_csrf(request, body)
        if not ok:
            return _json_error(403, err)
        return web.json_response({"ok": collector.record_client(body)})

    router.add_get("/api/debug/diagnostics", get_diag)
    router.add_post("/api/debug/clear", clear)
    router.add_post("/api/debug/client_error", client_error)

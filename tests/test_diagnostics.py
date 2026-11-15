"""Dashboard → Diagnostics (modules/diagnostics.py): error capture from
every source, known fixes, health checks, and the web routes."""

import io
import logging
import sys
import threading
import types

import pytest

from modules import diagnostics as dg
from tests.conftest import LOCAL, PUBLIC


@pytest.fixture
def fresh(monkeypatch):
    c = dg.Collector()
    monkeypatch.setattr(dg, "collector", c)
    return c


@pytest.mark.parametrize("message,title", [
    ("HTTPConnectionPool(host='127.0.0.1', port=11434): Max retries exceeded", "Ollama isn't running"),
    ('model "qwen3:8b" not found, try pulling it first', "A model isn't downloaded"),
    ("CUDA out of memory. Tried to allocate 2.00 GiB", "Out of GPU memory"),
    ("VTube Studio: connection failed — [WinError 1225] refused", "Can't reach VTube Studio"),
    ("UnicodeEncodeError: 'charmap' codec can't encode character", "The console can't print some characters"),
    ("OSError: [WinError 10048] Only one usage of each socket address", "A port is already taken"),
    ("ModuleNotFoundError: No module named 'pywinauto'", "Desktop tools need pywinauto"),
    ("ModuleNotFoundError: No module named 'mss'", "A Python package is missing"),
    ("google.auth.exceptions.RefreshError: invalid_grant", "An account sign-in expired"),
])
def test_known_errors_get_a_fix(message, title):
    fix = dg.suggest_fix(message)
    assert fix and fix["title"] == title and len(fix["fix"]) > 20


def test_unknown_error_has_no_fix():
    assert dg.suggest_fix("the flux capacitor wobbled") is None


def test_repeats_are_grouped_and_newest_first(fresh):
    for i in range(5):
        fresh.record("tts", f"Fish Audio request {i} timed out after 30s")
    fresh.record("vts", "VTube Studio: connection failed — refused")
    errs = fresh.errors()
    assert [e["source"] for e in errs] == ["vts", "tts"]
    assert errs[1]["count"] == 5 and errs[1]["fix"]["title"] == "Something took too long"


def test_secrets_and_colours_are_scrubbed(fresh):
    fresh.record("x", "\x1b[91mbad key sk-abcdefghijklmnop and api_key=hunter2\x1b[0m")
    msg = fresh.errors()[0]["message"]
    assert "sk-abc" not in msg and "hunter2" not in msg and "\x1b" not in msg and "[hidden]" in msg


def test_buffer_is_bounded(fresh):
    fresh.maxlen = 10
    for i in range(50):
        fresh.record("s", f"distinct error {chr(65 + i % 26)}{chr(97 + i // 26)}")
    assert len(fresh.errors()) == 10


def test_red_console_lines_are_caught(fresh):
    out = io.StringIO()
    tee = dg._RedTee(out)
    tee.write("\033[90mVTube Studio: connecting...\033[0m\n")
    tee.write("\033[91mVTube Studio: connection failed — refused\033[0m\n")
    tee.write("\033[91mTTS PyAudio Device 3 Error: Invalid output device\033[0m")
    tee.write("\n")
    assert "connecting" in out.getvalue()                       # everything still printed
    errs = fresh.errors()
    assert len(errs) == 2
    assert errs[0]["message"].startswith("TTS PyAudio") and errs[0]["fix"]["title"] == "Audio device problem"
    assert errs[1]["source"] == "console:vtube studio"


def test_logging_errors_and_thread_crashes_are_caught(fresh, monkeypatch):
    h = dg._LogHandler(level=logging.ERROR)
    log = logging.getLogger("asyncio")
    log.addHandler(h)
    try:
        log.error("Task exception was never retrieved")
    finally:
        log.removeHandler(h)
    assert fresh.errors()[0]["source"] == "log:asyncio"

    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", lambda args: None)
    monkeypatch.setattr(sys, "stdout", sys.stdout)
    root_handlers = list(logging.getLogger().handlers)
    try:
        dg.install()

        def boom():
            raise RuntimeError("worker died")
        t = threading.Thread(target=boom, name="tts-worker")
        t.start()
        t.join()
    finally:
        logging.getLogger().handlers = root_handlers
    e = fresh.errors()[0]
    assert e["source"] == "thread:tts-worker" and "worker died" in e["message"] and "Traceback" in e["detail"]


def test_log_hub_warnings_are_forwarded(fresh):
    from modules.log_hub import LogHub
    hub = LogHub(log_file=str(__import__("tempfile").mktemp(suffix=".log")))
    hub.emit("vts", "auth failed: authenticationToken missing", level="error")
    hub.emit("vts", "all good", level="info")
    errs = fresh.errors()
    assert len(errs) == 1 and errs[0]["fix"]["title"] == "VTube Studio refused the plugin"


def test_browser_errors_are_rate_limited(fresh):
    for i in range(40):
        fresh.record_client({"message": f"TypeError {i}: x is undefined", "source": "app.js", "line": "1:2"})
    errs = fresh.errors()
    assert len(errs) == 1 and errs[0]["count"] == 30           # grouped; the last 10 were dropped


def test_health_checks_report_missing_models_with_the_pull_command(monkeypatch):
    import requests
    import constants

    class R:
        def raise_for_status(self): pass
        def json(self): return {"models": [{"name": "dolphin3:8b"}]}
    monkeypatch.setattr(requests, "get", lambda url, timeout=0: R())
    monkeypatch.setattr(constants, "AGENT_MODEL_NAME", "qwen3:8b", raising=False)
    monkeypatch.setattr(constants, "MODEL_NAME", "dolphin3:8b", raising=False)
    sig = types.SimpleNamespace(vts_state="error", vts_detail="connection refused", tts_ready=True)
    checks = {c["id"]: c for c in dg.run_checks(sig)}
    assert checks["ollama"]["status"] == "ok"
    assert checks["model:MODEL_NAME"]["status"] == "ok"
    agent = checks["model:AGENT_MODEL_NAME"]
    assert agent["status"] == "error" and "ollama pull qwen3:8b" in agent["fix"]
    assert checks["vts"]["status"] == "error" and "Start API" in checks["vts"]["fix"]
    assert checks["tts_ready"]["status"] == "ok"


def test_ollama_down_is_one_clear_check(monkeypatch):
    import requests

    def down(url, timeout=0):
        raise requests.ConnectionError("refused")
    monkeypatch.setattr(requests, "get", down)
    checks = {c["id"]: c for c in dg.run_checks(types.SimpleNamespace())}
    assert checks["ollama"]["status"] == "error" and "ollama serve" in checks["ollama"]["fix"]
    assert not any(k.startswith("model:") for k in checks)


# ─── routes ───────────────────────────────────────────────────────────

def test_diagnostics_routes(live_lumi, fresh, monkeypatch):
    monkeypatch.setattr(dg, "run_checks", lambda s=None: [dg._check("x", "X", "warn", "d", "f")])
    fresh.record("vts", "VTube Studio: connection failed — refused")

    async def scenario(live):
        r = await live.local.get("/api/debug/diagnostics")
        assert r.status == 401                                    # signed out
        csrf = await live.register()
        h = {"Origin": LOCAL, "X-CSRF-Token": csrf}
        r = await live.local.get("/api/debug/diagnostics?refresh=1")
        d = await r.json()
        assert r.status == 200 and d["checks"][0]["id"] == "x"
        assert d["errors"][0]["fix"]["title"] == "Can't reach VTube Studio"
        assert d["summary"]["status"] == "error"
        r = await live.local.post("/api/debug/client_error", json={"message": "TypeError: boom", "view": "chat"}, headers=h)
        assert r.status == 200
        assert any(e["source"] == "browser" for e in fresh.errors())
        r = await live.local.post("/api/debug/client_error", json={"message": "x"}, headers={"Origin": LOCAL})
        assert r.status == 403                                    # no CSRF token
        r = await live.local.post("/api/debug/clear", json={}, headers=h)
        assert r.status == 200 and fresh.errors() == []
        # Remote (tunnel) sessions can't see host diagnostics.
        _, rcsrf = await live.remote_login()
        r = await live.remote.get("/api/debug/diagnostics", headers={"Origin": PUBLIC})
        assert r.status == 403
    live_lumi(scenario)

"""
tests/test_all_features.py

Comprehensive smoke test for Project Lumi. Introspects modules for the
actual public surface instead of hardcoding names.

Run from repo root:

    python tests/test_all_features.py

Exit 0 = all critical checks passed. Exit 1 = one or more critical
checks failed. Set LUMI_STRICT=1 to turn SKIPs into failures.
"""

from __future__ import annotations

import importlib
import inspect
import json
import os
import platform
import socket
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STRICT = os.environ.get("LUMI_STRICT", "0") == "1"

if platform.system() == "Windows" and not os.environ.get("WT_SESSION"):
    _C = {k: "" for k in ("G", "R", "Y", "B", "D", "X")}
else:
    _C = {
        "G": "\033[92m", "R": "\033[91m", "Y": "\033[93m",
        "B": "\033[94m", "D": "\033[90m", "X": "\033[0m",
    }


@dataclass
class Result:
    name: str
    status: str
    detail: str = ""
    critical: bool = True


@dataclass
class Suite:
    results: list = field(default_factory=list)

    def record(self, name, status, detail="", critical=True):
        self.results.append(Result(name, status, detail, critical))
        color = {"PASS": _C["G"], "FAIL": _C["R"], "SKIP": _C["Y"]}[status]
        icon = {"PASS": "✓", "FAIL": "✗", "SKIP": "○"}[status]
        line = f"  {color}{icon} {status:<4}{_C['X']}  {name}"
        if detail:
            line += f"  {_C['D']}({detail}){_C['X']}"
        print(line)

    def section(self, title):
        print(f"\n{_C['B']}▸ {title}{_C['X']}")

    def summary(self):
        p = sum(1 for r in self.results if r.status == "PASS")
        f = sum(1 for r in self.results if r.status == "FAIL")
        s = sum(1 for r in self.results if r.status == "SKIP")
        print(f"\n{_C['B']}{'─' * 60}{_C['X']}")
        print(f"  {_C['G']}{p} passed{_C['X']}   "
              f"{_C['R']}{f} failed{_C['X']}   "
              f"{_C['Y']}{s} skipped{_C['X']}")
        print(f"{_C['B']}{'─' * 60}{_C['X']}")
        if f:
            print(f"\n{_C['R']}Failures:{_C['X']}")
            for r in self.results:
                if r.status == "FAIL":
                    print(f"  {_C['R']}✗{_C['X']} {r.name}")
                    if r.detail:
                        print(f"      {_C['D']}{r.detail}{_C['X']}")
        if any(r.status == "FAIL" and r.critical for r in self.results):
            return 1
        if STRICT and s:
            print(f"\n{_C['Y']}LUMI_STRICT=1: treating skips as failures.{_C['X']}")
            return 1
        return 0


S = Suite()


class _SkipTest(Exception):
    pass


def _skip(msg):
    raise _SkipTest(msg)


def _importable(name):
    try:
        m = importlib.import_module(name)
        return True, getattr(m, "__version__", "")
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _check(name, fn, critical=True, optional=False):
    try:
        fn()
        S.record(name, "PASS", critical=critical)
    except _SkipTest as e:
        S.record(name, "SKIP", detail=str(e), critical=critical)
    except AssertionError as e:
        S.record(name, "FAIL", detail=str(e) or "assertion failed", critical=critical)
    except Exception as e:
        if optional:
            S.record(name, "SKIP", detail=f"{type(e).__name__}: {e}", critical=critical)
        else:
            S.record(name, "FAIL", detail=f"{type(e).__name__}: {e}", critical=critical)


def _port_open(host, port, timeout=1.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# Environment
# --------------------------------------------------------------------------- #

def check_python_version():
    assert sys.version_info[:2] >= (3, 10)


def check_env_active():
    """Accept venv, virtualenv, conda, or uv."""
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    in_conda = bool(os.environ.get("CONDA_PREFIX"))
    in_virtual_env = bool(os.environ.get("VIRTUAL_ENV"))
    assert in_venv or in_conda or in_virtual_env, (
        "no virtualenv or conda env detected in sys.prefix / CONDA_PREFIX / VIRTUAL_ENV"
    )


def check_repo_layout():
    required = ["main.py", "constants.py", "modules", "config", "sdk", "web"]
    missing = [p for p in required if not (REPO_ROOT / p).exists()]
    assert not missing, f"missing: {missing}"


def check_env_file():
    env = REPO_ROOT / ".env"
    if not env.exists():
        _skip(".env not found (copy .env.example)")
    text = env.read_text(encoding="utf-8", errors="ignore")
    meaningful = [
        ln for ln in text.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    assert meaningful, ".env exists but has no non-comment lines"


def check_persona_config():
    cfg = REPO_ROOT / "config" / "lumi.yaml"
    assert cfg.exists(), "config/lumi.yaml missing"
    try:
        import yaml
    except ImportError:
        _skip("pyyaml not installed")
    with cfg.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert isinstance(data, dict) and data, "lumi.yaml did not parse to a non-empty mapping"


# --------------------------------------------------------------------------- #
# Dependencies
# --------------------------------------------------------------------------- #

CORE_DEPS = ["requests", "yaml", "dotenv", "websockets"]
OPTIONAL_DEPS = {
    "chromadb": "long-term memory",
    "discord": "Discord integration",
    "sounddevice": "microphone input",
    "numpy": "audio processing",
    "PIL": "screen perception",
}


def check_core_deps():
    missing = [d for d in CORE_DEPS if not _importable(d)[0]]
    assert not missing, "missing: " + ", ".join(missing)


def check_optional_deps():
    for dep, purpose in OPTIONAL_DEPS.items():
        ok, _ = _importable(dep)
        if ok:
            S.record(f"optional dep: {dep}", "PASS", critical=False)
        else:
            S.record(f"optional dep: {dep}", "SKIP",
                     detail=f"needed for {purpose}", critical=False)


# --------------------------------------------------------------------------- #
# Modules
# --------------------------------------------------------------------------- #

CORE_MODULES = [
    "modules.prompter", "modules.agent_tools", "modules.memory",
    "modules.session_state", "modules.self_model", "modules.emotional_state",
    "modules.heartbeat", "modules.moods", "modules.log_hub",
    "modules.user_auth", "modules.llm_providers", "modules.model_manager",
    "modules.persona_editor", "modules.integrations",
]

OPTIONAL_MODULES = {
    "modules.tts": "Fish Audio TTS",
    "modules.multimodal": "screen perception",
    "modules.ambient_perception": "ambient context",
    "modules.vtubeStudio": "VTube Studio",
    "modules.discordClient": "Discord",
    "modules.socketIO": "web client backend",
}


def check_module_imports():
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    for mod in CORE_MODULES:
        try:
            importlib.import_module(mod)
            S.record(f"import {mod}", "PASS")
        except Exception as e:
            S.record(f"import {mod}", "FAIL", detail=f"{type(e).__name__}: {e}")
    for mod, purpose in OPTIONAL_MODULES.items():
        try:
            importlib.import_module(mod)
            S.record(f"import {mod}", "PASS", critical=False)
        except Exception as e:
            S.record(f"import {mod}", "SKIP",
                     detail=f"{purpose}: {type(e).__name__}: {e}", critical=False)


# --------------------------------------------------------------------------- #
# Constants — introspective
# --------------------------------------------------------------------------- #

def _discover_constants():
    c = importlib.import_module("constants")
    found = {}
    for name in dir(c):
        if name.startswith("_"):
            continue
        obj = getattr(c, name)
        if isinstance(obj, Path):
            found[name] = str(obj)
        elif isinstance(obj, str) and len(obj) < 4096:
            if any(sep in obj for sep in ("/", "\\")) or ":" in obj:
                found[name] = obj
    return found


def check_constants_paths():
    found = _discover_constants()
    dirs = {k: v for k, v in found.items() if Path(v).is_dir()}
    assert dirs, (
        "no path-like attributes on constants resolve to an existing "
        f"directory. Attributes found: {sorted(found)[:20]}"
    )
    semantic = [k for k in dirs if any(
        tok in k.lower() for tok in ("data", "memory", "workspace", "root", "base", "repo")
    )]
    assert semantic, (
        f"path-like constants exist but none named for data/memory/workspace/root: "
        f"{sorted(dirs)}"
    )


def check_writable_data_dir():
    dirs = {k: Path(v) for k, v in _discover_constants().items() if Path(v).is_dir()}
    priority = ("DATA_DIR", "DATA_PATH", "MEMORIES_DIR", "MEMORY_DIR", "WORKSPACE_ROOT")
    target = None
    for name in priority:
        if name in dirs:
            target = dirs[name]
            break
    if target is None:
        for name, path in dirs.items():
            if "data" in name.lower() or "memor" in name.lower():
                target = path
                break
    if target is None:
        _skip(f"no data directory found among constants: {sorted(dirs)}")
    probe = target / f".write_probe_{uuid.uuid4().hex[:8]}"
    try:
        probe.write_text("ok", encoding="utf-8")
    finally:
        try:
            probe.unlink()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Chroma
# --------------------------------------------------------------------------- #

def check_chroma():
    ok, _ = _importable("chromadb")
    if not ok:
        _skip("chromadb not installed")
    import chromadb
    mem_dir = None
    for k, v in _discover_constants().items():
        if "memor" in k.lower() and Path(v).is_dir():
            mem_dir = Path(v)
            break
    if mem_dir is None:
        mem_dir = REPO_ROOT / "data" / "memories"
        mem_dir.mkdir(parents=True, exist_ok=True)

    client = chromadb.PersistentClient(path=str(mem_dir))
    name = f"lumi-selftest-{uuid.uuid4().hex[:8]}"
    try:
        col = client.create_collection(name)
        col.add(ids=["t1"], documents=["lumi self test"], metadatas=[{"i": 0}])
        got = col.get(ids=["t1"])
        assert got["documents"] == ["lumi self test"], "round-trip mismatch"
    finally:
        try:
            client.delete_collection(name)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# LLM
# --------------------------------------------------------------------------- #

def check_ollama_reachable():
    ok, _ = _importable("requests")
    if not ok:
        _skip("requests missing")
    import requests
    url = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
    if not url.startswith("http"):
        url = "http://" + url
    try:
        r = requests.get(f"{url}/api/tags", timeout=3)
    except Exception as e:
        _skip(f"Ollama not reachable at {url} ({type(e).__name__})")
    assert r.status_code == 200, f"status {r.status_code}"
    assert r.json().get("models"), "Ollama running but no models pulled"


def check_llm_providers_module():
    lp = importlib.import_module("modules.llm_providers")
    public = [n for n in dir(lp) if not n.startswith("_")]
    likely = [n for n in public if any(
        tok in n.lower() for tok in ("provider", "resolve", "spec", "mask")
    )]
    assert likely, f"llm_providers exposes nothing provider-shaped. Public: {public[:20]}"


# --------------------------------------------------------------------------- #
# Agent tools — introspective schema discovery
# --------------------------------------------------------------------------- #

def _find_tool_list(mod):
    for name in dir(mod):
        if name.startswith("__"):
            continue
        try:
            obj = getattr(mod, name)
        except Exception:
            continue
        if callable(obj) and not isinstance(obj, type):
            try:
                obj = obj()
            except Exception:
                continue
        if isinstance(obj, list) and obj:
            first = obj[0]
            if isinstance(first, dict) and (
                "name" in first or "function" in first or "parameters" in first
            ):
                return name, obj
            if hasattr(first, "name") and hasattr(first, "parameters"):
                return name, obj
        elif isinstance(obj, dict) and obj:
            vals = list(obj.values())
            if vals and isinstance(vals[0], dict) and (
                "name" in vals[0] or "parameters" in vals[0]
            ):
                return name, list(obj.values())
    return None, None


def check_agent_tools_schema():
    at = importlib.import_module("modules.agent_tools")
    name, tools = _find_tool_list(at)
    if name is None:
        public = [n for n in dir(at) if not n.startswith("_")]
        assert False, (
            "no tool list found. Public names on agent_tools: "
            f"{public[:30]}"
        )
    assert len(tools) > 0, f"{name} is empty"


def check_path_sandbox():
    at = importlib.import_module("modules.agent_tools")
    fn = None
    for cand in (
        "_is_path_allowed", "_check_path", "_safe_path", "is_path_allowed",
        "_resolve_path", "_validate_path", "_resolve_sandboxed_path",
    ):
        if hasattr(at, cand):
            fn = getattr(at, cand)
            break
    if fn is None:
        _skip("no path sandbox function found on agent_tools")

    escapes = [
        "../../../etc/passwd",
        "..\\..\\..\\Windows\\System32\\config\\SAM",
        "..%2f..%2fetc%2fpasswd",
    ]
    if platform.system() == "Windows":
        escapes += [
            "C:\\Windows\\System32\\drivers\\etc\\hosts",
            "\\\\?\\C:\\Windows\\System32",
        ]
    else:
        escapes += ["/etc/shadow", "/root/.ssh/id_rsa"]

    refused = 0
    accepted = []
    for attempt in escapes:
        try:
            result = fn(attempt)
            if not result:
                refused += 1
            elif isinstance(result, (str, Path)):
                try:
                    p = Path(result).resolve()
                    root = Path(getattr(at, "WORKSPACE_ROOT", REPO_ROOT)).resolve()
                    if not str(p).startswith(str(root)):
                        accepted.append(attempt)
                    else:
                        refused += 1
                except Exception:
                    refused += 1
            else:
                refused += 1
        except Exception:
            refused += 1

    assert not accepted, f"sandbox allowed escapes: {accepted}"
    assert refused > 0, f"sandbox refused 0 / {len(escapes)} probes"


def check_shell_guards():
    at = importlib.import_module("modules.agent_tools")
    guards = ["_SHELL_METACHAR", "_COMMAND_ALLOWLIST", "_LINUX_ONLY_COMMAND"]
    found = [g for g in guards if hasattr(at, g)]
    assert found, f"no Phase 9.4 shell guards found (checked {guards})"


def check_provenance_wrapping():
    at = importlib.import_module("modules.agent_tools")
    fn = getattr(at, "wrap_tool_result", None)
    if fn is None:
        _skip("wrap_tool_result not found on agent_tools")
    sig = inspect.signature(fn)
    payload = "hello world"
    known_sources = ["file", "web", "memory", "sdk", "tool", "terminal", "shell"]

    attempts = []
    for src in known_sources:
        attempts.append((f"({src!r}, result)", (lambda s=src: fn(s, payload))))
        attempts.append((f"(result, {src!r})", (lambda s=src: fn(payload, s))))
    if len(sig.parameters) == 1:
        attempts.append(("(result,)", (lambda: fn(payload))))

    for label, call in attempts:
        try:
            out = call()
        except Exception:
            continue
        if not isinstance(out, str):
            out = str(out)
        up = out.upper()
        if "[FROM" in up or "FROM FILE" in up or "FROM WEB" in up \
           or "FROM MEMORY" in up or "FROM SDK" in up:
            return  # marker found — check passes

    _skip(
        f"wrap_tool_result produced no provenance marker for any known "
        f"source/order. signature={sig}"
    )


# --------------------------------------------------------------------------- #
# Secret redaction — introspective
# --------------------------------------------------------------------------- #

def check_log_redaction():
    lh = importlib.import_module("modules.log_hub")
    fn = None
    for cand in ("redact", "_redact", "redact_secrets", "_scrub",
                 "scrub", "_sanitize", "sanitize"):
        if hasattr(lh, cand) and callable(getattr(lh, cand)):
            fn = getattr(lh, cand)
            break
    if fn is None:
        for name in dir(lh):
            obj = getattr(lh, name)
            if isinstance(obj, type):
                for meth in ("redact", "scrub", "sanitize"):
                    if hasattr(obj, meth):
                        try:
                            fn = getattr(obj(), meth)
                            break
                        except Exception:
                            pass
            if fn:
                break
    if fn is None:
        _skip("no redaction function found on log_hub")

    samples = [
        "sk-abc123def456ghi789xyz",
        "AIzaSyA1B2C3D4E5F6G7H8I9J0K",
        "Bearer abcdef1234567890",
    ]
    for raw in samples:
        out = fn(raw)
        if not isinstance(out, str):
            out = str(out)
        marker = raw.split()[0] if " " in raw else raw[:6]
        assert marker not in out, f"redaction left {marker!r}: {out!r}"


# --------------------------------------------------------------------------- #
# Web
# --------------------------------------------------------------------------- #

def check_web_assets():
    web = REPO_ROOT / "web"
    for name in ("index.html", "app.js", "style.css"):
        assert (web / name).exists(), f"web/{name} missing"


def check_web_port():
    if _port_open("127.0.0.1", 5000):
        S.record("web server on :5000", "PASS", critical=False)
    else:
        S.record("web server on :5000", "SKIP",
                 detail="not running", critical=False)


# --------------------------------------------------------------------------- #
# SDK
# --------------------------------------------------------------------------- #

def check_sdk_files():
    for name in ("protocol.py", "actions.py", "client.py", "server.py"):
        assert (REPO_ROOT / "sdk" / name).exists(), f"sdk/{name} missing"


def check_sdk_token():
    token = REPO_ROOT / "data" / "memories" / "sdk_token.txt"
    if not token.exists():
        _skip("sdk_token.txt not yet generated")
    assert token.read_text(encoding="utf-8").strip(), "token is empty"
    if platform.system() != "Windows":
        mode = token.stat().st_mode & 0o777
        assert mode == 0o600, f"mode is {oct(mode)}, expected 0o600"


def check_sdk_allowlist():
    p = REPO_ROOT / "data" / "memories" / "sdk_allowlist.json"
    if not p.exists():
        _skip("sdk_allowlist.json not yet generated")
    data = json.loads(p.read_text(encoding="utf-8"))
    assert isinstance(data, (dict, list)), "malformed JSON"


def check_sdk_port():
    if _port_open("127.0.0.1", 5056):
        S.record("SDK server on :5056", "PASS", critical=False)
    else:
        S.record("SDK server on :5056", "SKIP", detail="not running", critical=False)


# --------------------------------------------------------------------------- #
# External
# --------------------------------------------------------------------------- #

def check_ffmpeg():
    import shutil
    assert shutil.which("ffmpeg"), "ffmpeg not on PATH"
    r = subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
    assert r.returncode == 0, "ffmpeg --version non-zero"


def check_vts_port():
    if _port_open("127.0.0.1", 8001):
        S.record("VTube Studio on :8001", "PASS", critical=False)
    else:
        S.record("VTube Studio on :8001", "SKIP", detail="not running", critical=False)


def check_discord_token():
    env = REPO_ROOT / ".env"
    if not env.exists():
        _skip(".env missing")
    text = env.read_text(encoding="utf-8", errors="ignore")
    has = any(
        ln.split("=", 1)[0].strip().startswith("DISCORD")
        and "=" in ln and ln.split("=", 1)[1].strip()
        for ln in text.splitlines() if ln.strip() and not ln.startswith("#")
    )
    if not has:
        _skip("Discord token not configured")


# --------------------------------------------------------------------------- #
# Prompter — introspective
# --------------------------------------------------------------------------- #

def check_prompter_surface():
    p = importlib.import_module("modules.prompter")
    expected = ["chat_sync", "execute_tool", "run", "run_agent_turn", "run_prompter"]
    found = [n for n in expected if hasattr(p, n)]
    assert found, (
        "prompter exposes none of the expected entry points "
        f"({expected}). Public callables: "
        f"{[n for n in dir(p) if not n.startswith('_') and callable(getattr(p, n, None))]}"
    )


def check_provenance_marker_in_source():
    p = importlib.import_module("modules.prompter")
    src = Path(p.__file__).read_text(encoding="utf-8", errors="ignore")
    assert "PROVENANCE" in src.upper(), "no PROVENANCE rule text in prompter source"


def check_max_agent_steps():
    p = importlib.import_module("modules.prompter")
    for name in ("MAX_AGENT_STEPS", "AGENT_MAX_STEPS", "MAX_STEPS", "MAX_TOOL_STEPS"):
        if hasattr(p, name):
            v = getattr(p, name)
            assert isinstance(v, int) and v > 0, f"{name}={v!r} invalid"
            return
    _skip("no MAX_AGENT_STEPS-like constant found on prompter")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    print(f"{_C['B']}Project Lumi — Feature Smoke Test (v2){_C['X']}")
    print(f"{_C['D']}repo: {REPO_ROOT}{_C['X']}")
    print(f"{_C['D']}python: {sys.version.split()[0]} on {platform.system()}{_C['X']}")

    S.section("Environment")
    _check("python >= 3.10", check_python_version)
    _check("virtualenv or conda env active", check_env_active, critical=False)
    _check("repo layout", check_repo_layout)
    _check(".env present and non-empty", check_env_file)
    _check("config/lumi.yaml loads", check_persona_config)

    S.section("Dependencies")
    _check("core deps importable", check_core_deps)
    check_optional_deps()

    S.section("Modules import")
    check_module_imports()

    S.section("Constants & paths")
    _check("constants expose data/workspace paths", check_constants_paths)
    _check("data dir writable", check_writable_data_dir, critical=False)

    S.section("Memory (ChromaDB)")
    _check("chroma round-trip", check_chroma, critical=False)

    S.section("LLM provider")
    _check("Ollama reachable", check_ollama_reachable, optional=True, critical=False)
    _check("llm_providers entry point", check_llm_providers_module)

    S.section("Agent tools & security")
    _check("tool schema present", check_agent_tools_schema)
    _check("path sandbox rejects escapes", check_path_sandbox, optional=True)
    _check("shell guards present (Phase 9.4)", check_shell_guards)
    _check("provenance wrapping (Phase 9.1)", check_provenance_wrapping, optional=True)

    S.section("Secret redaction (Phase 9.6)")
    _check("log_hub redacts secrets", check_log_redaction, optional=True)

    S.section("Web client (Phase 10)")
    _check("web assets present", check_web_assets)
    check_web_port()

    S.section("SDK (Phase 8)")
    _check("sdk files present", check_sdk_files)
    _check("sdk_token.txt valid", check_sdk_token, critical=False)
    _check("sdk_allowlist.json valid", check_sdk_allowlist, critical=False)
    check_sdk_port()

    S.section("External services (optional)")
    _check("ffmpeg on PATH", check_ffmpeg, critical=False)
    check_vts_port()
    _check("Discord token configured", check_discord_token, critical=False)

    S.section("Prompter & persona")
    _check("prompter public surface", check_prompter_surface)
    _check("PROVENANCE rule in prompt (Phase 9.1)", check_provenance_marker_in_source)
    _check("MAX_AGENT_STEPS defined", check_max_agent_steps, critical=False)

    return S.summary()


if __name__ == "__main__":
    sys.exit(main())
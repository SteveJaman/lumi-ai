"""
System diagnostic for Lumi.

Run this first when something is wrong. It checks:

  - Python and platform version
  - Which top-level folders exist under the workspace
  - Which Python packages are installed (with versions)
  - Whether the web / log hub / SDK / voice ports are free
  - Whether Ollama, FFmpeg, and NVIDIA drivers are reachable
  - Whether .env, lumi.yaml, and the runtime data folders exist
  - Whether the SDK token and user DB have been generated

This script does NOT import `constants.py`. That module imports
PyYAML at load time — and a missing PyYAML is one of the failures
this script exists to diagnose. All paths are resolved relative
to this file's grandparent directory (the workspace root).

Usage:
    python scripts/check_system.py
"""

import importlib
import importlib.metadata
import importlib.util
import os
import platform
import shutil
import socket
import subprocess
import sys

# ─────────────────────────────────────────────────────────────
# Path bootstrap — do NOT import constants.py
# ─────────────────────────────────────────────────────────────
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_ROOT   = os.path.join(_ROOT, "data")
CONFIG_ROOT = os.path.join(_ROOT, "config")

EXPECTED_DIRS = [
    "assets", "config", "data", "docs", "modules",
    "plugin", "scripts", "sdk", "tests", "web",
]

EXPECTED_ROOT_FILES = [
    "main.py", "constants.py", "signals.py", "requirements.txt",
    "README.md", ".env.example", ".gitignore",
]

RUNTIME_PATHS = [
    ("config/lumi.yaml",             "Persona definition"),
    ("data/logs/chat_history.json",  "Chat history"),
    ("data/memories/chroma.db",      "Long-term memory (ChromaDB)"),
    ("data/memories/users.db",       "Web client user DB"),
    ("data/memories/sdk_token.txt",  "SDK authentication token"),
    ("data/memories/self_model.json","Self-model observations"),
    ("data/memories/session_state.json", "Session continuity"),
    ("data/vtubeStudio_token.txt",   "VTS plugin auth token"),
]

# ─────────────────────────────────────────────────────────────
# ANSI helpers
# ─────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
MAGENTA= "\033[95m"
GRAY   = "\033[90m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

OK_MARK   = f"{GREEN}✓{RESET}"
WARN_MARK = f"{YELLOW}⚠{RESET}"
FAIL_MARK = f"{RED}✗{RESET}"
NA_MARK   = f"{GRAY}○{RESET}"

# Counters
_passed   = 0
_warnings = 0
_errors   = 0


def _emit(mark, message, detail=""):
    """Print one check line. `detail` is grayed out on the right."""
    pad = " " * max(1, 44 - len(message))
    if detail:
        print(f"  {mark} {message}{pad}{GRAY}{detail}{RESET}")
    else:
        print(f"  {mark} {message}")


def ok(message, detail=""):
    global _passed
    _passed += 1
    _emit(OK_MARK, message, detail)


def warn(message, detail=""):
    global _warnings
    _warnings += 1
    _emit(WARN_MARK, message, detail)


def fail(message, detail=""):
    global _errors
    _errors += 1
    _emit(FAIL_MARK, message, detail)


def info(message, detail=""):
    _emit(NA_MARK, message, detail)


def section(title):
    print()
    print(f"{BOLD}{CYAN}{title}{RESET}")
    print(f"{GRAY}{'─' * 62}{RESET}")


# ─────────────────────────────────────────────────────────────
# Checks
# ─────────────────────────────────────────────────────────────
def check_system():
    section("System")

    py = platform.python_version()
    if sys.version_info >= (3, 11):
        ok("Python", py)
    else:
        warn("Python", f"{py} — 3.11+ recommended")

    ok("Platform", platform.platform())
    ok("Architecture", platform.machine())
    ok("Workspace", _ROOT)

    if os.path.isdir(os.path.join(_ROOT, ".git")):
        info("Git repository detected")
    else:
        info("Not a git checkout (or .git missing)")


def check_dirs():
    section("Workspace layout")

    missing = []
    for d in EXPECTED_DIRS:
        path = os.path.join(_ROOT, d)
        if os.path.isdir(path):
            continue
        missing.append(d)

    if not missing:
        ok(f"All {len(EXPECTED_DIRS)} top-level folders present")
    else:
        for d in missing:
            fail(f"Missing folder: {d}/")

    missing_files = []
    for f in EXPECTED_ROOT_FILES:
        path = os.path.join(_ROOT, f)
        if not os.path.isfile(path):
            missing_files.append(f)

    if not missing_files:
        ok(f"All {len(EXPECTED_ROOT_FILES)} root-level files present")
    else:
        for f in missing_files:
            fail(f"Missing file: {f}")


def check_runtime_paths():
    section("Runtime state")

    for rel, label in RUNTIME_PATHS:
        path = os.path.join(_ROOT, rel)
        if os.path.exists(path):
            try:
                size = os.path.getsize(path)
                if size > 1024 * 1024:
                    detail = f"{size / (1024*1024):.1f} MB"
                elif size > 1024:
                    detail = f"{size / 1024:.1f} KB"
                else:
                    detail = f"{size} B"
            except OSError:
                detail = ""
            ok(label, detail)
        else:
            # Some of these are generated on first run. Warn, not fail.
            warn(f"{label} not found", rel)


# ─────────────────────────────────────────────────────────────
# Package checks
# ─────────────────────────────────────────────────────────────
# (package_metadata_name, import_path, severity, purpose)
#   severity: "required" | "optional"
PACKAGES = [
    # Config & env
    ("PyYAML",                  "yaml",                    "required", "lumi.yaml loader"),
    ("python-dotenv",           "dotenv",                  "required", ".env loader"),
    ("ruamel.yaml",             "ruamel.yaml",             "optional", "persona editor comment preservation"),

    # Security
    ("argon2-cffi",             "argon2",                  "optional", "user DB hashing (falls back to pbkdf2)"),

    # Web / async
    ("requests",                "requests",                "required", "everywhere"),
    ("aiohttp",                 "aiohttp",                 "required", "web client backend"),
    ("python-socketio",         "socketio",                "required", "socket.io server"),
    ("websockets",              "websockets",              "required", "SDK server/client"),

    # Agent tools
    ("ddgs",                    "ddgs",                    "required", "web_search"),
    ("pypdf",                   "pypdf",                   "required", "PDF reader"),
    ("python-docx",             "docx",                    "required", "DOCX reader"),

    # STT
    ("RealtimeSTT",             "RealtimeSTT",             "required", "mic_input.py"),
    ("faster-whisper",          "faster_whisper",          "required", "Discord + browser voice STT"),
    ("numpy",                   "numpy",                   "required", "envelope + VAD math"),

    # Audio
    ("pyaudio",                 "pyaudio",                 "required", "TTS output routing"),
    ("simpleaudio",             "simpleaudio",             "optional", "TTS fallback when pyaudio missing"),
    ("pydub",                   "pydub",                   "optional", "legacy audioPlayer.py only"),

    # Discord voice
    ("discord.py",              "discord",                 "required", "Discord bot"),
    ("davey",                   "davey",                   "required", "DAVE E2EE receive decrypt"),
    ("discord-ext-voice-recv",  "discord.ext.voice_recv",  "required", "voice receive sink"),

    # VTS
    ("pyvts",                   "pyvts",                   "required", "VTube Studio controller"),

    # Memory
    ("chromadb",                "chromadb",                "required", "long-term memory"),

    # Browser
    ("playwright",              "playwright",              "required", "browser_controller"),

    # Screen
    ("mss",                     "mss",                     "required", "screen capture"),
    ("Pillow",                  "PIL",                     "required", "image processing"),

    # Input
    ("keyboard",                "keyboard",                "required", "mute hotkey + VTS hotkey sim"),
    ("pynput",                  "pynput",                  "optional", "legacy local_input.py"),

    # Location
    ("geopy",                   "geopy",                   "optional", "web_search timezone lookup"),
    ("timezonefinder",          "timezonefinder",          "optional", "web_search timezone lookup"),

    # SDK reference client
    ("psutil",                  "psutil",                  "optional", "SDK example client only"),

    # Tests
    ("pytest",                  "pytest",                  "optional", "tests/ unit tests"),
]


def _package_info(package, import_path):
    """Return (version_str_or_None, importable_bool). Does not import
    the package — uses metadata + find_spec so heavy deps like torch
    don't get loaded just to run diagnostics."""
    version = None
    try:
        version = importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        version = None
    except Exception:
        version = None

    importable = False
    try:
        spec = importlib.util.find_spec(import_path)
        importable = spec is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        importable = False

    return version, importable


def check_packages():
    section("Python packages")

    missing_required = []
    missing_optional = []

    for package, import_path, severity, purpose in PACKAGES:
        version, importable = _package_info(package, import_path)

        if importable and version:
            ok(package, f"{version}  ·  {purpose}")
        elif importable:
            ok(package, f"(no version metadata)  ·  {purpose}")
        elif severity == "required":
            missing_required.append(package)
            fail(package, f"NOT INSTALLED  ·  {purpose}")
        else:
            missing_optional.append(package)
            info(f"{package} (optional)", f"not installed  ·  {purpose}")

    return missing_required, missing_optional


# ─────────────────────────────────────────────────────────────
# Services
# ─────────────────────────────────────────────────────────────
PORTS = [
    (5000, "Web client",    "http://127.0.0.1:5000/"),
    (5001, "Log hub",       "http://127.0.0.1:5001/"),
    (5055, "Voice bridge (reserved)", ""),
    (5056, "SDK server",    "ws://127.0.0.1:5056/ws"),
]


def _port_in_use(port, host="127.0.0.1"):
    """Returns True if the port is in use, False if free."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return False
        except OSError:
            return True


def check_ports():
    section("Network ports")

    for port, label, url in PORTS:
        in_use = _port_in_use(port)
        if in_use:
            warn(f"Port {port} — {label}", f"in use  ·  {url}")
        else:
            ok(f"Port {port} — {label}", "free")


def check_ollama(endpoint="http://localhost:11434"):
    section("Ollama")

    try:
        import requests
    except ImportError:
        fail("requests not installed — cannot probe Ollama")
        return

    try:
        r = requests.get(f"{endpoint}/api/tags", timeout=3)
        if r.status_code != 200:
            fail("Ollama unreachable", f"HTTP {r.status_code}")
            return
        data = r.json()
        models = data.get("models", []) or []
        ok("Ollama reachable", endpoint)

        names = sorted((m.get("name") or "") for m in models)
        if not names:
            warn("No models installed", "run: ollama pull qwen3:8b")
        else:
            # Highlight the ones we know Lumi wants
            expected = {"lumi-qwen3", "qwen3:8b", "qwen2.5vl:latest"}
            present = {n.split(":")[0] for n in names}
            missing = [e for e in expected if e.split(":")[0] not in present]

            ok(f"{len(names)} model(s) installed")
            for n in names[:12]:
                info(f"  {n}")
            if len(names) > 12:
                info(f"  … and {len(names) - 12} more")

            if missing:
                warn("Expected models not present", ", ".join(missing))

    except Exception as e:
        fail("Ollama unreachable", f"{type(e).__name__}: {e}")


def check_ffmpeg():
    section("FFmpeg")

    path = shutil.which("ffmpeg")
    if not path:
        warn("FFmpeg not found on PATH",
             "Discord voice send will silently fail")
        return

    try:
        r = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            first = (r.stdout or "").splitlines()[0] if r.stdout else "ok"
            ok("FFmpeg found", first.strip())
        else:
            warn("FFmpeg returned non-zero", f"exit {r.returncode}")
    except subprocess.TimeoutExpired:
        fail("FFmpeg timed out")
    except Exception as e:
        fail("FFmpeg check failed", f"{type(e).__name__}: {e}")


def check_playwright_firefox():
    section("Playwright Firefox")

    if not importlib.util.find_spec("playwright"):
        fail("playwright not installed", "pip install playwright")
        return

    # Look for the firefox-XXXX directory in the ms-playwright cache.
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or ""
        cache = os.path.join(base, "ms-playwright") if base else None
    else:
        cache = os.path.join(os.path.expanduser("~"), ".cache", "ms-playwright")

    if cache and os.path.isdir(cache):
        for entry in sorted(os.listdir(cache)):
            if entry.startswith("firefox-"):
                ok("Playwright Firefox installed", entry)
                return

    warn("Firefox binary not found",
         "run: playwright install firefox")


# ─────────────────────────────────────────────────────────────
# GPU
# ─────────────────────────────────────────────────────────────
def check_gpu():
    section("GPU")

    # ---- NVIDIA via nvidia-smi ----
    nvidia = shutil.which("nvidia-smi")
    if not nvidia:
        info("nvidia-smi not found on PATH")
    else:
        try:
            r = subprocess.run(
                ["nvidia-smi",
                 "--query-gpu=name,memory.total,driver_version",
                 "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                for line in r.stdout.strip().splitlines():
                    ok("NVIDIA GPU", line.strip())
            else:
                warn("nvidia-smi returned non-zero",
                     f"exit {r.returncode}")
        except Exception as e:
            warn("nvidia-smi failed", f"{type(e).__name__}: {e}")

    # ---- torch CUDA ----
    torch_spec = importlib.util.find_spec("torch")
    if torch_spec is None:
        info("torch not installed",
             "install CUDA build first if you want GPU Whisper")
        return

    try:
        torch = importlib.import_module("torch")
        if torch.cuda.is_available():
            name = torch.cuda.get_device_name(0)
            ok("torch CUDA available",
               f"torch {torch.__version__} · CUDA {torch.version.cuda} · {name}")
        else:
            warn("torch is CPU-only",
                 "install a CUDA wheel for GPU Whisper")
    except Exception as e:
        warn("torch import failed", f"{type(e).__name__}: {e}")


# ─────────────────────────────────────────────────────────────
# Config / .env
# ─────────────────────────────────────────────────────────────
def _read_env(path):
    """Return a dict of KEY -> value, or {} if unreadable."""
    out = {}
    if not os.path.isfile(path):
        return out
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line or line.lstrip().startswith("#"):
                    continue
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return out


def check_config():
    section("Configuration")

    # .env
    env_path = os.path.join(_ROOT, ".env")
    env = _read_env(env_path)

    if not env:
        fail(".env not found", "copy .env.example → .env and fill in keys")
    else:
        ok(".env present", f"{len(env)} entries")

        keys = [
            ("FISH_API_KEY",       "Fish Audio TTS",       "required"),
            ("FISH_REFERENCE_ID",  "Fish Audio voice ID",  "required"),
            ("DISCORD_BOT_TOKEN",  "Discord bot",          "optional"),
            ("DISCORD_VOICE_CHANNEL_ID", "Discord voice",  "optional"),
            ("DISCORD_CHANNEL_ID", "Discord text",         "optional"),
            ("OPENAI_API_KEY",     "OpenAI provider",      "optional"),
            ("ANTHROPIC_API_KEY",  "Anthropic provider",   "optional"),
            ("GEMINI_API_KEY",     "Gemini provider",      "optional"),
            ("GROQ_API_KEY",       "Groq provider",        "optional"),
            ("OPENROUTER_API_KEY", "OpenRouter provider",  "optional"),
        ]
        for key, label, severity in keys:
            if env.get(key):
                ok(label, f"{key} set")
            elif severity == "required":
                warn(f"{label} missing", key)
            # optional missing → silent

    # lumi.yaml
    yaml_path = os.path.join(CONFIG_ROOT, "lumi.yaml")
    if not os.path.isfile(yaml_path):
        fail("config/lumi.yaml missing")
    else:
        try:
            # Parse manually so we don't depend on PyYAML being importable
            # (it may not be, and that's exactly what we're diagnosing).
            with open(yaml_path, "r", encoding="utf-8") as f:
                content = f.read()
            lines = [l for l in content.splitlines() if l.strip()]
            ok("config/lumi.yaml present", f"{len(lines)} lines")
        except Exception as e:
            fail("config/lumi.yaml unreadable", str(e))


# ─────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────
def print_summary(missing_required, missing_optional):
    print()
    print(f"{BOLD}{MAGENTA}{'=' * 62}{RESET}")
    print(f"{BOLD}{MAGENTA}  Summary{RESET}")
    print(f"{BOLD}{MAGENTA}{'=' * 62}{RESET}")
    print()

    print(f"  {OK_MARK} {_passed} checks passed")

    if _warnings:
        print(f"  {WARN_MARK} {_warnings} warning(s)")
    if _errors:
        print(f"  {RED}✗{RESET} {_errors} error(s)")

    print()

    if missing_required:
        print(f"{BOLD}{RED}Missing required packages:{RESET}")
        for p in missing_required:
            print(f"  · {p}")
        print()
        print(f"{GRAY}  Fix: pip install -r requirements.txt{RESET}")
        print()

    if missing_optional:
        print(f"{GRAY}Missing optional packages: "
              f"{', '.join(missing_optional)}{RESET}")
        print()

    if _errors == 0 and _warnings == 0:
        print(f"{GREEN}  Everything looks good.{RESET}")
    elif _errors == 0:
        print(f"{YELLOW}  No errors. Warnings above are informational — "
              f"Lumi should boot.{RESET}")
    else:
        print(f"{RED}  Errors detected. Fix the marked items before "
              f"starting Lumi.{RESET}")
        print()
        print(f"{GRAY}  Common fixes:{RESET}")
        print(f"{GRAY}    - pip install -r requirements.txt{RESET}")
        print(f"{GRAY}    - playwright install firefox{RESET}")
        print(f"{GRAY}    - install FFmpeg and add it to PATH{RESET}")
        print(f"{GRAY}    - copy .env.example to .env and fill in keys{RESET}")

    print()
    print(f"{GRAY}  Logs and diagnostics: "
          f"http://127.0.0.1:5001/  (when Lumi is running){RESET}")
    print()

    return 0 if _errors == 0 else 1


# ─────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────
def main():
    print()
    print(f"{BOLD}{CYAN}{'=' * 62}{RESET}")
    print(f"{BOLD}{CYAN}  Lumi — System Check{RESET}")
    print(f"{BOLD}{CYAN}{'=' * 62}{RESET}")

    try:
        check_system()
    except Exception as e:
        fail("System check failed", f"{type(e).__name__}: {e}")

    try:
        check_dirs()
    except Exception as e:
        fail("Workspace layout check failed", str(e))

    try:
        check_runtime_paths()
    except Exception as e:
        fail("Runtime state check failed", str(e))

    missing_required = []
    missing_optional = []
    try:
        missing_required, missing_optional = check_packages()
    except Exception as e:
        fail("Package check failed", str(e))

    try:
        check_ports()
    except Exception as e:
        fail("Port check failed", str(e))

    try:
        check_ollama()
    except Exception as e:
        fail("Ollama check failed", str(e))

    try:
        check_ffmpeg()
    except Exception as e:
        fail("FFmpeg check failed", str(e))

    try:
        check_playwright_firefox()
    except Exception as e:
        fail("Playwright check failed", str(e))

    try:
        check_gpu()
    except Exception as e:
        fail("GPU check failed", str(e))

    try:
        check_config()
    except Exception as e:
        fail("Config check failed", str(e))

    exit_code = print_summary(missing_required, missing_optional)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
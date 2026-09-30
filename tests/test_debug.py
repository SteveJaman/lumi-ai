#!/usr/bin/env python
"""
Lumi general debug harness.

Checks the whole project top-down for the failure modes that make Lumi
refuse to boot or misbehave in ways that are hard to trace from the
terminal. Designed to be the first thing a user runs when something
is wrong.

What it does NOT do:
  - Does not start Lumi.
  - Does not open listening sockets.
  - Does not call any LLM or cloud API (except a 2-second Ollama ping).
  - Does not write to any file except a temp file it deletes itself.

Usage:
    python tests/test_debug.py
    python tests/test_debug.py -v          # show passing details
    python tests/test_debug.py --section imports
    python tests/test_debug.py --list      # list section names
"""

import argparse
import ast
import json
import os
import socket
import sys
import traceback

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
GRAY   = "\033[90m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"


# ========================================================================
# Reporter
# ========================================================================
class Reporter:
    def __init__(self, verbose=False):
        self.verbose = verbose
        self.current = ""
        self.counts = {"ok": 0, "warn": 0, "fail": 0, "skip": 0}
        self.failures = []

    def section(self, name):
        self.current = name
        print(f"\n{BOLD}{CYAN}[ {name} ]{RESET}")

    def ok(self, name, detail=""):
        self.counts["ok"] += 1
        if self.verbose:
            d = f"  {GRAY}{detail}{RESET}" if detail else ""
            print(f"  {GREEN}✓{RESET} {name}{d}")

    def warn(self, name, detail="", hint=""):
        self.counts["warn"] += 1
        d = f"  {GRAY}{detail}{RESET}" if detail else ""
        print(f"  {YELLOW}⚠{RESET} {name}{d}")
        if hint:
            print(f"      {GRAY}hint: {hint}{RESET}")

    def fail(self, name, detail="", hint=""):
        self.counts["fail"] += 1
        self.failures.append((self.current, name, detail, hint))
        d = f"  {GRAY}{detail}{RESET}" if detail else ""
        print(f"  {RED}✗{RESET} {name}{d}")
        if hint:
            print(f"      {GRAY}hint: {hint}{RESET}")

    def skip(self, name, detail=""):
        self.counts["skip"] += 1
        d = f"  {GRAY}{detail}{RESET}" if detail else ""
        print(f"  {GRAY}○{RESET} {name}{d}")

    def summary(self):
        print()
        print(f"{BOLD}{CYAN}=" * 60 + f"{RESET}")
        c = self.counts
        print(f"  {GREEN}{c['ok']} passed{RESET}  ·  "
              f"{YELLOW}{c['warn']} warnings{RESET}  ·  "
              f"{RED}{c['fail']} failures{RESET}  ·  "
              f"{GRAY}{c['skip']} skipped{RESET}")

        if self.failures:
            print()
            print(f"{BOLD}{RED}Failures need attention:{RESET}")
            for section, name, detail, hint in self.failures:
                print(f"  [{section}] {name}")
                if detail:
                    print(f"    {GRAY}{detail}{RESET}")
                if hint:
                    print(f"    {YELLOW}→ {hint}{RESET}")

        return 0 if c["fail"] == 0 else 1


# ========================================================================
# Helpers
# ========================================================================
def safe_import(name):
    """Import a module and return (module, error). Never raises."""
    try:
        import importlib
        mod = importlib.import_module(name)
        return mod, None
    except BaseException as e:
        return None, e


def walk_py_files(folder):
    out = []
    base = os.path.join(_ROOT, folder)
    if not os.path.isdir(base):
        return out
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs
                   if d not in ("__pycache__", ".pytest_cache")]
        for f in files:
            if f.endswith(".py"):
                out.append(os.path.join(root, f))
    return out


def syntax_check(path):
    """AST-parse a Python file without executing it. Returns error or None."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
        ast.parse(src, filename=path)
        return None
    except SyntaxError as e:
        return f"SyntaxError at line {e.lineno}: {e.msg}"
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def port_free(port, host="127.0.0.1"):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


# ========================================================================
# Section: workspace layout
# ========================================================================
def check_workspace(r, args):
    r.section("Workspace layout")

    r.ok("workspace root", _ROOT)

    expected_dirs = [
        "assets", "config", "data", "docs", "modules",
        "plugin", "scripts", "sdk", "tests", "web",
    ]
    for d in expected_dirs:
        p = os.path.join(_ROOT, d)
        if os.path.isdir(p):
            r.ok(f"{d}/")
        else:
            r.fail(f"{d}/ missing",
                   hint=f"re-clone or restore the {d}/ folder")

    expected_root = [
        "main.py", "constants.py", "signals.py",
        "requirements.txt", "README.md",
    ]
    for f in expected_root:
        p = os.path.join(_ROOT, f)
        if os.path.isfile(p):
            r.ok(f)
        else:
            r.fail(f"{f} missing",
                   hint=f"restore {f} from the repo")


# ========================================================================
# Section: syntax
# ========================================================================
def check_syntax(r, args):
    r.section("Python syntax")

    total = 0
    broken = 0
    for folder in ("modules", "sdk", "plugin", "scripts", "tests", "web"):
        for path in walk_py_files(folder):
            total += 1
            err = syntax_check(path)
            if err:
                broken += 1
                r.fail(os.path.relpath(path, _ROOT), err,
                       hint="fix the syntax error before anything else")

    # Root-level .py files
    for f in ("main.py", "constants.py", "signals.py"):
        p = os.path.join(_ROOT, f)
        if os.path.isfile(p):
            total += 1
            err = syntax_check(p)
            if err:
                broken += 1
                r.fail(f, err)

    if broken == 0:
        r.ok(f"all {total} python files parse cleanly")


# ========================================================================
# Section: imports
# ========================================================================
CORE_MODULES = [
    "constants",
    "signals",
]

LIVE_MODULES = [
    "modules.module",
    "modules.prompter",
    "modules.agent_tools",
    "modules.memory",
    "modules.emotional_state",
    "modules.heartbeat",
    "modules.self_model",
    "modules.session_state",
    "modules.ambient_perception",
    "modules.multimodal",
    "modules.tts",
    "modules.mic_input",
    "modules.console_input",
    "modules.discordClient",
    "modules.vtubeStudio",
    "modules.browser_controller",
    "modules.log_hub",
    "modules.user_auth",
    "modules.persona_editor",
    "modules.model_manager",
    "modules.settings_manager",
    "modules.integrations",
    "modules.llm_providers",
    "modules.socketIO",
    "modules.customPrompt",
    "modules.doc_manager",
    "modules.injection",
    "modules.library_indexer",
]

SDK_MODULES = [
    "sdk",
    "sdk.protocol",
    "sdk.actions",
    "sdk.client",
    "sdk.errors",
    "sdk.server",
]


def check_imports(r, args):
    r.section("Imports")

    for name in CORE_MODULES:
        mod, err = safe_import(name)
        if err:
            r.fail(name, f"{type(err).__name__}: {err}",
                   hint="core module — Lumi cannot start without this")
        else:
            r.ok(name)

    for name in LIVE_MODULES:
        mod, err = safe_import(name)
        if err:
            # Missing optional dependency is a warning; a real
            # ModuleNotFoundError for the module itself is a failure.
            if isinstance(err, ModuleNotFoundError) and \
               err.name and not err.name.startswith("modules."):
                r.warn(name, f"missing dependency: {err.name}",
                       hint=f"pip install {err.name}")
            else:
                r.fail(name, f"{type(err).__name__}: {err}",
                       hint="check the module's imports against "
                            "requirements.txt")
        else:
            r.ok(name)

    for name in SDK_MODULES:
        mod, err = safe_import(name)
        if err:
            r.fail(name, f"{type(err).__name__}: {err}",
                   hint="sdk is required for Phase 8+ features")
        else:
            r.ok(name)


# ========================================================================
# Section: constants
# ========================================================================
REQUIRED_CONSTANTS = [
    "WORKSPACE_ROOT", "DATA_ROOT", "CONFIG_ROOT",
    "LLM_ENDPOINT", "MODEL_NAME", "FAST_MODEL_NAME",
    "FAST_MODEL_ENABLED", "TIMEZONE",
    "ALLOW_LAN_EXPOSURE",
    "SDK_HOST", "SDK_PORT",
    "HOST", "AI_NAME",
    "LUMI_VERSION",
]


def check_constants(r, args):
    r.section("Constants")

    constants, err = safe_import("constants")
    if err:
        r.fail("import constants", str(err),
               hint="fix constants.py before continuing")
        return

    for name in REQUIRED_CONSTANTS:
        if hasattr(constants, name):
            r.ok(name)
        else:
            r.fail(f"missing: {name}",
                   hint=f"add {name} to constants.py")

    # Path sanity
    if hasattr(constants, "WORKSPACE_ROOT"):
        ws = constants.WORKSPACE_ROOT
        if os.path.isdir(ws):
            r.ok("WORKSPACE_ROOT resolves", ws)
        else:
            r.fail("WORKSPACE_ROOT does not exist", ws,
                   hint="re-clone the repo")

    for path_attr in ("DATA_ROOT", "CONFIG_ROOT"):
        if hasattr(constants, path_attr):
            p = getattr(constants, path_attr)
            if os.path.isdir(p):
                r.ok(f"{path_attr} exists")
            else:
                r.warn(f"{path_attr} does not exist", p,
                       hint="will be created on first boot")


# ========================================================================
# Section: persona YAML
# ========================================================================
def check_persona(r, args):
    r.section("Persona (lumi.yaml)")

    constants, err = safe_import("constants")
    if err:
        r.skip("needs constants")
        return

    yaml_path = getattr(constants, "YAML_PATH", None)
    if not yaml_path:
        r.fail("YAML_PATH not defined in constants")
        return
    if not os.path.isfile(yaml_path):
        r.fail("lumi.yaml missing", yaml_path,
               hint="restore config/lumi.yaml from the repo")
        return
    r.ok("lumi.yaml exists")

    # Parse with ruamel if available, else PyYAML
    try:
        from modules.persona_editor import (
            load_parsed, backend_name, backend_preserves_comments,
        )
        parsed = load_parsed(yaml_path)
        r.ok("parses", f"backend={backend_name()}")
        if not backend_preserves_comments():
            r.warn("comments not preserved on save",
                   hint="pip install ruamel.yaml")
    except Exception as e:
        r.fail("lumi.yaml does not parse",
               f"{type(e).__name__}: {e}",
               hint="validate YAML syntax")
        return

    # Required top-level keys
    required = ("name", "host", "greeting", "personality")
    for key in required:
        if key in parsed:
            r.ok(f"key: {key}")
        else:
            r.warn(f"missing top-level key: {key}")

    # Prompt builder produces something
    try:
        prompt = constants.build_system_prompt(constants.HOST.name)
        if prompt and len(prompt) > 200:
            r.ok("build_system_prompt produced output",
                   f"{len(prompt)} chars")
        else:
            r.fail("build_system_prompt returned empty or too short",
                   hint="check lumi.yaml required fields")
    except Exception as e:
        r.fail("build_system_prompt raised",
               f"{type(e).__name__}: {e}")


# ========================================================================
# Section: signals
# ========================================================================
def check_signals(r, args):
    r.section("Signals")

    Signals, err = safe_import("signals")
    if err:
        r.skip("cannot import signals")
        return
    Signals = getattr(Signals, "Signals", None)
    if Signals is None:
        r.fail("Signals class not found in signals.py")
        return

    try:
        sig = Signals()
    except Exception as e:
        r.fail("Signals() raised",
               f"{type(e).__name__}: {e}",
               hint="check signals.py __init__ for file/log path issues")
        return

    required = [
        "terminate", "new_message", "AI_thinking", "AI_speaking",
        "history", "last_message_time",
        "sio_queue", "vtube_queue", "tts_queue",
        "log_dir", "log_file",
    ]
    for name in required:
        if hasattr(sig, name):
            r.ok(name)
        else:
            r.fail(f"missing attribute: {name}")

    if isinstance(getattr(sig, "history", None), list):
        r.ok("history is a list", f"{len(sig.history)} entries")
    else:
        r.fail("history is not a list")

    for q in ("sio_queue", "vtube_queue", "tts_queue"):
        obj = getattr(sig, q, None)
        if obj is not None and hasattr(obj, "put") and hasattr(obj, "get"):
            r.ok(f"{q} is queue-like")
        else:
            r.fail(f"{q} is not a queue")


# ========================================================================
# Section: agent tools
# ========================================================================
def check_agent_tools(r, args):
    r.section("Agent tools")

    tools, err = safe_import("modules.agent_tools")
    if err:
        r.skip("cannot import modules.agent_tools")
        return

    schemas = getattr(tools, "TOOL_SCHEMAS", None)
    if not isinstance(schemas, list) or not schemas:
        r.fail("TOOL_SCHEMAS is missing or empty")
        return
    r.ok(f"{len(schemas)} tool schemas")

    dispatch = getattr(tools, "_DISPATCH", {})
    schema_names = set()
    for i, s in enumerate(schemas):
        fn = s.get("function", {})
        name = fn.get("name")
        if not name:
            r.fail(f"schema #{i} has no function.name")
            continue
        schema_names.add(name)
        if not fn.get("description"):
            r.warn(f"{name}: no description")
        if not isinstance(fn.get("parameters"), dict):
            r.fail(f"{name}: parameters missing")

    # Extra schemas live in prompter
    prompter, err = safe_import("modules.prompter")
    if not err:
        extra = getattr(prompter, "EXTRA_TOOL_SCHEMAS", [])
        for s in extra:
            name = s.get("function", {}).get("name")
            if name:
                schema_names.add(name)
        r.ok(f"{len(extra)} extra schemas from prompter")

    # Every schema should be dispatchable
    for name in sorted(schema_names):
        if name in dispatch:
            r.ok(f"dispatch: {name}")
        elif name in ("web_search", "browse_url", "recall_memory",
                      "vtube_set_expression", "vtube_move"):
            r.ok(f"dispatch (prompter): {name}")
        else:
            r.warn(f"no dispatch for tool: {name}")

    # Confirm set sanity
    confirm = getattr(tools, "CONFIRM_REQUIRED_TOOLS", set())
    for name in confirm:
        if name not in schema_names:
            r.warn(f"CONFIRM_REQUIRED_TOOLS has unknown tool: {name}")
    if confirm:
        r.ok(f"confirm-required: {sorted(confirm)}")

    # Sandbox sanity
    try:
        tools._safe_path("../../../etc/passwd")
        r.fail("_safe_path did not refuse traversal")
    except Exception:
        r.ok("_safe_path refuses traversal")

    # Phase 9.1 provenance helper
    if hasattr(tools, "wrap_tool_result"):
        w = tools.wrap_tool_result("read_file", "content")
        if "[FROM FILE]" in w:
            r.ok("provenance wrapper present")
        else:
            r.warn("provenance wrapper returned unexpected output")
    else:
        r.warn("wrap_tool_result not found (Phase 9.1 incomplete)")


# ========================================================================
# Section: library indexing & RAG
# ========================================================================
def check_library(r, args):
    r.section("Library Indexer & RAG")

    constants, err = safe_import("constants")
    if err:
        r.skip("needs constants")
        return

    data_root = getattr(constants, "DATA_ROOT", None)
    if not data_root:
        r.fail("DATA_ROOT not found")
        return

    lib_dir = os.path.join(data_root, "library")
    if os.path.isdir(lib_dir):
        files = os.listdir(lib_dir)
        r.ok("data/library/ folder exists", f"{len(files)} files found")
    else:
        r.warn("data/library/ folder does not exist",
               hint="will be created automatically on boot")

    # Check chromadb library_docs collection if chroma.db exists
    chroma_path = os.path.join(data_root, "memories", "chroma.db")
    if os.path.isdir(chroma_path):
        try:
            import chromadb
            client = chromadb.PersistentClient(path=chroma_path)
            collections = [c.name for c in client.list_collections()]
            if "library_docs" in collections:
                col = client.get_collection("library_docs")
                r.ok("library_docs ChromaDB collection found", f"{col.count()} chunks indexed")
            else:
                r.skip("library_docs collection not yet initialized (created on first index run)")
        except Exception as e:
            r.warn("Could not query ChromaDB collection", str(e))
    else:
        r.skip("ChromaDB storage not initialized yet")


# ========================================================================
# Section: LLM connectivity
# ========================================================================
def check_llm(r, args):
    r.section("LLM connectivity")

    constants, err = safe_import("constants")
    if err:
        r.skip("needs constants")
        return

    endpoint = getattr(constants, "LLM_ENDPOINT", "http://localhost:11434")
    model = getattr(constants, "MODEL_NAME", "")
    fast = getattr(constants, "FAST_MODEL_NAME", "")

    try:
        import requests
    except ImportError:
        r.fail("requests not installed",
               hint="pip install requests")
        return

    try:
        r_ = requests.get(f"{endpoint}/api/tags", timeout=3)
    except Exception as e:
        r.fail(f"cannot reach Ollama at {endpoint}",
               f"{type(e).__name__}: {e}",
               hint="is 'ollama serve' running?")
        return

    if r_.status_code != 200:
        r.fail(f"Ollama returned HTTP {r_.status_code}")
        return

    r.ok("Ollama reachable", endpoint)

    try:
        names = {m.get("name", "") for m in r_.json().get("models", [])}
    except Exception:
        names = set()

    for label, name in (("MODEL_NAME", model),
                        ("FAST_MODEL_NAME", fast)):
        if not name:
            r.warn(f"{label} is empty")
            continue
        base = name.split(":")[0]
        match = any(base in n for n in names)
        if match:
            r.ok(f"{label} installed", name)
        else:
            r.fail(f"{label} not installed", name,
                   hint=f"ollama pull {name}")

    # Fast model, if enabled, should also be present (already checked)
    if not getattr(constants, "FAST_MODEL_ENABLED", False):
        r.skip("FAST_MODEL_ENABLED is off")
    else:
        r.ok("fast path enabled")

    # Vision model (only a warning if missing)
    vlm = getattr(constants, "VISION_MODEL_NAME", "")
    if vlm:
        base = vlm.split(":")[0]
        if any(base in n for n in names):
            r.ok("vision model installed", vlm)
        else:
            r.warn("vision model not installed", vlm,
                   hint=f"ollama pull {vlm}  (only needed for screen "
                        f"description)")


# ========================================================================
# Section: storage
# ========================================================================
def check_storage(r, args):
    r.section("Storage")

    constants, err = safe_import("constants")
    if err:
        r.skip("needs constants")
        return

    data_root = getattr(constants, "DATA_ROOT", None)
    if not data_root or not os.path.isdir(data_root):
        r.fail("DATA_ROOT does not exist", data_root)
        return

    for sub in ("logs", "memories", "library"):
        p = os.path.join(data_root, sub)
        if os.path.isdir(p):
            r.ok(f"data/{sub}/")
        else:
            r.warn(f"data/{sub}/ does not exist", hint="will be created")

    # Writable check — write a tiny temp file
    for sub in ("logs", "memories", "library"):
        p = os.path.join(data_root, sub)
        if not os.path.isdir(p):
            continue
        try:
            tmp = os.path.join(p, "._lumi_debug_tmp")
            with open(tmp, "w") as f:
                f.write("ok")
            os.remove(tmp)
            r.ok(f"data/{sub}/ writable")
        except Exception as e:
            r.fail(f"data/{sub}/ not writable",
                   f"{type(e).__name__}: {e}",
                   hint="check folder permissions")

    # Chat history parses
    ch = os.path.join(data_root, "logs", "chat_history.json")
    if os.path.isfile(ch):
        try:
            with open(ch, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                r.ok("chat_history.json parses", f"{len(data)} entries")
            else:
                r.warn("chat_history.json is not a list")
        except Exception as e:
            r.fail("chat_history.json does not parse",
                   f"{type(e).__name__}: {e}",
                   hint="delete it to start fresh — Signals will "
                        "recreate the file")
    else:
        r.skip("chat_history.json not created yet")

    # ChromaDB
    chroma = os.path.join(data_root, "memories", "chroma.db")
    if os.path.isdir(chroma):
        r.ok("chroma.db/")
    else:
        r.skip("chroma.db not created yet (first boot creates it)")

    # users.db
    udb = os.path.join(data_root, "memories", "users.db")
    if os.path.isfile(udb):
        r.ok("users.db")
    else:
        r.skip("users.db not created yet (first web login creates it)")


# ========================================================================
# Section: ports
# ========================================================================
PORT_MAP = [
    (5000, "Web client", "http://127.0.0.1:5000/"),
    (5001, "Log hub", "http://127.0.0.1:5001/"),
    (5056, "SDK server", "ws://127.0.0.1:5056/ws"),
]


def check_ports(r, args):
    r.section("Network ports")

    for port, label, url in PORT_MAP:
        if port_free(port):
            r.ok(f"{port} free ({label})")
        else:
            r.warn(f"{port} in use ({label})",
                   hint=f"if Lumi is already running this is fine; "
                        f"otherwise something else has it — "
                        f"try {url}")


# ========================================================================
# Section: config files
# ========================================================================
def check_config_files(r, args):
    r.section("Config files")

    env = os.path.join(_ROOT, ".env")
    if not os.path.isfile(env):
        r.warn(".env missing",
               hint="copy .env.example to .env and fill in the keys")
    else:
        keys = {}
        try:
            with open(env, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        keys[k.strip()] = v.strip().strip('"').strip("'")
        except Exception as e:
            r.fail(".env unreadable", str(e))
            return

        r.ok(".env present", f"{len(keys)} entries")

        required_keys = ["FISH_API_KEY", "FISH_REFERENCE_ID"]
        for k in required_keys:
            if keys.get(k):
                r.ok(f"{k} set")
            else:
                r.warn(f"{k} not set",
                       hint="required for TTS")

        # Optional but useful
        for k in ("DISCORD_BOT_TOKEN", "DISCORD_VOICE_CHANNEL_ID"):
            if not keys.get(k):
                r.skip(f"{k} not set (Discord disabled)")

    # Modelfile
    mf = os.path.join(_ROOT, "config", "Modelfile-qwen3")
    if os.path.isfile(mf):
        r.ok("config/Modelfile-qwen3")
    else:
        r.warn("config/Modelfile-qwen3 missing",
               hint="needed to build the lumi-qwen3 model")

    # Blacklist
    bl = os.path.join(_ROOT, "config", "blacklist.txt")
    if os.path.isfile(bl):
        r.ok("config/blacklist.txt")
    else:
        r.skip("config/blacklist.txt not present")


# ========================================================================
# Section: optional dependencies
# ========================================================================
OPTIONAL_DEPS = [
    ("mss",              "screen capture"),
    ("PIL",              "image processing (Pillow)"),
    ("playwright",       "browser control"),
    ("pyvts",            "VTube Studio"),
    ("discord",          "Discord bot"),
    ("davey",            "Discord voice (DAVE decrypt)"),
    ("chromadb",         "long-term memory"),
    ("faster_whisper",   "speech-to-text"),
    ("pyaudio",          "audio output routing"),
    ("keyboard",         "hotkeys / VTS keyboard sim"),
    ("pypdf",            "PDF reading"),
    ("docx",             "DOCX reading (python-docx)"),
    ("psutil",           "SDK example clients"),
    ("argon2",           "password hashing (falls back to pbkdf2)"),
    ("ruamel.yaml",      "persona comment preservation"),
    ("ddgs",             "web_search tool"),
    ("pytest",           "running the test suite"),
]


def check_optional_deps(r, args):
    r.section("Optional dependencies")

    for mod, purpose in OPTIONAL_DEPS:
        _, err = safe_import(mod)
        if err is None:
            r.ok(mod, purpose)
        else:
            # Some are warnings (features will be limited), some skips.
            r.warn(f"{mod} not installed", purpose,
                   hint=f"pip install {mod.split('.')[0]}")


# ========================================================================
# Section: module shape
# ========================================================================
MODULE_CLASS_MAP = {
    "modules.prompter":             "Prompter",
    "modules.memory":               "Memory",
    "modules.emotional_state":      "EmotionalStateModule",
    "modules.heartbeat":            "Heartbeat",
    "modules.self_model":           "SelfModel",
    "modules.ambient_perception":   "AmbientPerception",
    "modules.multimodal":           "MultiModal",
    "modules.tts":                  "TTS",
    "modules.browser_controller":   "BrowserController",
    "modules.log_hub":              "LogHub",
    "modules.user_auth":            "UserDB",
    "modules.library_indexer":      "LibraryIndexer",
}


def check_module_shape(r, args):
    r.section("Module shape")

    for modname, clsname in MODULE_CLASS_MAP.items():
        mod, err = safe_import(modname)
        if err:
            r.skip(f"{modname} (import failed)")
            continue
        cls = getattr(mod, clsname, None)
        if cls is None:
            r.fail(f"{modname}.{clsname} missing")
            continue

        # Only check run() on classes that actually subclass Module.
        # Plain helper classes (TTS, LogHub, UserDB) don't participate
        # in the module lifecycle.
        try:
            from modules.module import Module
            is_module = issubclass(cls, Module)
        except Exception:
            is_module = False

        if is_module:
            if hasattr(cls, "run"):
                r.ok(f"{clsname}.run")
            else:
                r.fail(f"{clsname} subclasses Module but has no run()")
        else:
            r.ok(f"{clsname} (helper class)")

        if hasattr(cls, "get_prompt_injection"):
            r.ok(f"{clsname}.get_prompt_injection")


# ========================================================================
# Section: module smoke tests
# ========================================================================
def check_module_smoke(r, args):
    """Construct the safe-to-construct modules with a stub signals and
    call the read-only methods. Skips modules that would spawn threads
    or talk to hardware."""
    r.section("Module smoke tests")

    # persona editor round-trip
    try:
        from modules.persona_editor import (
            backend_name, backend_preserves_comments, load_parsed,
        )
        r.ok("persona_editor load_parsed",
             f"backend={backend_name()}")
    except Exception as e:
        r.fail("persona_editor round-trip failed", str(e))

    # llm_providers
    try:
        from modules.llm_providers import list_providers
        providers = list_providers()
        r.ok("llm_providers.list_providers",
             f"{len(providers)} providers")
    except Exception as e:
        r.fail("llm_providers.list_providers failed", str(e))

    # model_manager routing read
    try:
        from modules.model_manager import ModelManager
        mm = ModelManager()
        routing = mm.get_routing()
        r.ok("model_manager.get_routing",
             f"main={routing.get('MODEL_NAME')} "
             f"fast={routing.get('FAST_MODEL_NAME')}")
    except Exception as e:
        r.fail("model_manager.get_routing failed", str(e))

    # integrations list
    try:
        from modules.integrations import INTEGRATIONS
        r.ok("integrations registry", f"{len(INTEGRATIONS)} entries")
    except Exception as e:
        r.fail("integrations registry failed", str(e))

    # settings_manager
    try:
        from modules.settings_manager import get_general
        g = get_general()
        r.ok("settings_manager.get_general",
             f"{len(g)} fields")
    except Exception as e:
        r.fail("settings_manager.get_general failed", str(e))

    # log_hub instance
    try:
        from modules.log_hub import LogHub
        hub = LogHub(port=0)   # port 0 means don't bind
        r.ok("LogHub instance")
    except Exception as e:
        r.fail("LogHub instance failed", str(e))


# ========================================================================
# Section runner
# ========================================================================
SECTIONS = [
    ("workspace",    check_workspace),
    ("syntax",       check_syntax),
    ("imports",      check_imports),
    ("constants",    check_constants),
    ("persona",      check_persona),
    ("signals",      check_signals),
    ("agent_tools",  check_agent_tools),
    ("library",      check_library),
    ("llm",          check_llm),
    ("storage",      check_storage),
    ("ports",        check_ports),
    ("config_files", check_config_files),
    ("module_shape", check_module_shape),
    ("module_smoke", check_module_smoke),
    ("optional",     check_optional_deps),
]


def main():
    ap = argparse.ArgumentParser(description="Lumi general debug harness")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="show details for every check")
    ap.add_argument("--section", default=None,
                    help="run only this section")
    ap.add_argument("--list", action="store_true",
                    help="list section names and exit")
    args = ap.parse_args()

    if args.list:
        for name, _ in SECTIONS:
            print(name)
        return 0

    print()
    print(f"{BOLD}{CYAN}{'=' * 60}{RESET}")
    print(f"{BOLD}{CYAN}  Lumi Debug Harness{RESET}")
    print(f"{BOLD}{CYAN}{'=' * 60}{RESET}")
    print(f"  workspace: {GRAY}{_ROOT}{RESET}")

    r = Reporter(verbose=args.verbose)

    if args.section:
        fn = dict(SECTIONS).get(args.section)
        if fn is None:
            print(f"Unknown section: {args.section}")
            return 2
        try:
            fn(r, args)
        except Exception:
            print(f"{RED}section crashed:{RESET}")
            traceback.print_exc()
        return r.summary()

    for name, fn in SECTIONS:
        try:
            fn(r, args)
        except Exception as e:
            r.section(name)
            r.fail(f"section crashed: {type(e).__name__}",
                   str(e)[:200])

    return r.summary()


if __name__ == "__main__":
    sys.exit(main())
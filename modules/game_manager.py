"""
Game manager — runs Lumi's game agents as background processes.

Lumi's Minecraft "other half" is two processes:

  bot.js                   the body: mineflayer, joined to the world
                           (sdk/examples/minecraft/bot.js)
  plugin/minecraft_bot.py  the game agent: picks goals with its own small
                           model (GAME_AGENT_MODEL) and talks to Lumi
                           through the SDK as "MinecraftAgent"

The web dashboard toggles them through POST /api/game/toggle. This module
spawns and stops them, forwards their output to the log hub under the
"minecraft" category (the dashboard's Event Stream), writes it to
data/logs/minecraft/, and reports a crash with the last lines of output.

data/minecraft/autoplay.json holds {"minecraft": true|false}. The agent
reads it at the top of every loop, and Lumi restarts the processes on
boot when it is true.

If bot.js is already running (started by hand), it is reused and never
stopped by this module. On Windows the children are put in a job object
that kills them when Lumi's process exits, even on a crash.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Any, Deque, Dict, Optional

from constants import WORKSPACE_ROOT, DATA_ROOT
from modules.log_hub import emit as hub_emit

MC_DIR         = os.path.join(DATA_ROOT, "minecraft")
AUTOPLAY_PATH  = os.path.join(MC_DIR, "autoplay.json")
TELEMETRY_PATH = os.path.join(MC_DIR, "telemetry.json")
SERVER_PATH    = os.path.join(MC_DIR, "server.json")   # read by bot.js at startup
LOG_DIR        = os.path.join(DATA_ROOT, "logs", "minecraft")
BOT_DIR        = os.path.join(WORKSPACE_ROOT, "sdk", "examples", "minecraft")
AGENT_SCRIPT   = os.path.join(WORKSPACE_ROOT, "plugin", "minecraft_bot.py")
BOT_API        = os.environ.get("BOT_API", "http://127.0.0.1:5057")

GAMES = ("minecraft",)
TAIL_LINES = 40
MAX_DIRECTIVE = 200

# Item and block textures shipped with prismarine-viewer (the live viewer's
# package), served to the Games page's HUD so it needs no outside CDN.
TEXTURE_ROOT = os.path.join(BOT_DIR, "node_modules", "prismarine-viewer", "public", "textures")
DEFAULT_TEXTURE_VERSION = "1.19"
ITEM_NAME_RE = re.compile(r"^[a-z0-9_]{1,64}$")
# Actions the Games page may send to bot.js. Everything else stays with the
# agent and Lumi's tools.
HUD_ACTIONS = {"drop_item"}
MAX_SKIN_BYTES = 64 * 1024
HOST_RE = re.compile(r"^[A-Za-z0-9.:\[\]-]{1,253}$")

# Chatty startup lines that would push real events out of the hub's buffer.
_QUIET = re.compile(r"^\[sdk\] (registered:|connecting to)|^\[voyager\] running ")
_ERRORISH = re.compile(r"\b(error|exception|traceback|crash|failed|fail)\b", re.I)


# ─── Autoplay flag ────────────────────────────────────────────────────────────

def read_autoplay() -> Dict[str, bool]:
    try:
        with open(AUTOPLAY_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return {k: bool(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except Exception:
        return {}


def read_telemetry() -> Dict[str, Any]:
    """The game agent's latest state (current_thought, health, ...)."""
    return _read_json(TELEMETRY_PATH)


def write_autoplay(game: str, enabled: bool) -> None:
    os.makedirs(MC_DIR, exist_ok=True)
    data = read_autoplay()
    data[game] = enabled
    tmp = AUTOPLAY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, AUTOPLAY_PATH)


# ─── Kill-on-exit job object (Windows) ────────────────────────────────────────

class _KillOnCloseJob:
    """Windows job object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE. The
    handle closes when Lumi's process ends for any reason, and Windows
    then kills every process assigned to it. No-op elsewhere."""

    def __init__(self) -> None:
        self._job = None
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes

            class IO_COUNTERS(ctypes.Structure):
                _fields_ = [(n, ctypes.c_ulonglong) for n in (
                    "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                    "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

            class BASIC_LIMIT(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                            ("PerJobUserTimeLimit", ctypes.c_int64),
                            ("LimitFlags", wintypes.DWORD),
                            ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t),
                            ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t),
                            ("PriorityClass", wintypes.DWORD),
                            ("SchedulingClass", wintypes.DWORD)]

            class EXTENDED_LIMIT(ctypes.Structure):
                _fields_ = [("BasicLimitInformation", BASIC_LIMIT),
                            ("IoInfo", IO_COUNTERS),
                            ("ProcessMemoryLimit", ctypes.c_size_t),
                            ("JobMemoryLimit", ctypes.c_size_t),
                            ("PeakProcessMemoryUsed", ctypes.c_size_t),
                            ("PeakJobMemoryUsed", ctypes.c_size_t)]

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                    ctypes.c_void_p, wintypes.DWORD]
            k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            job = k32.CreateJobObjectW(None, None)
            if not job:
                return
            info = EXTENDED_LIMIT()
            info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
                return
            self._k32, self._job = k32, job
        except Exception:
            self._job = None

    def add(self, proc: subprocess.Popen) -> None:
        if self._job is None:
            return
        try:
            self._k32.AssignProcessToJobObject(self._job, int(proc._handle))
        except Exception:
            pass


# ─── Child process ────────────────────────────────────────────────────────────

class _Child:
    def __init__(self, name: str, proc: subprocess.Popen, log_path: str):
        self.name = name
        self.proc = proc
        self.log_path = log_path
        self.started_at = time.time()
        self.tail: Deque[str] = deque(maxlen=TAIL_LINES)
        self.stopping = False
        self.exit_code: Optional[int] = None

    @property
    def running(self) -> bool:
        return self.proc.poll() is None

    def info(self) -> Dict[str, Any]:
        crashed = not self.running and not self.stopping and self.exit_code not in (None, 0)
        return {
            "running": self.running,
            "pid": self.proc.pid,
            "uptime_s": int(time.time() - self.started_at) if self.running else None,
            "exit_code": self.exit_code,
            "crashed": crashed,
            "last_output": list(self.tail)[-8:] if crashed else [],
            "log": os.path.relpath(self.log_path, WORKSPACE_ROOT),
        }


class GameManager:
    def __init__(self, signals):
        self.signals = signals
        self._lock = threading.Lock()
        self._children: Dict[str, _Child] = {}   # "agent", "bot"
        self._job = _KillOnCloseJob()
        self._viewer_cache = (0.0, None)
        signals.game_manager = self

    # ─── Public API (called from the web server's executor) ──────

    def set_enabled(self, game: str, enabled: bool) -> Dict[str, Any]:
        if game not in GAMES:
            raise ValueError(f"unknown game: {game}")
        write_autoplay(game, enabled)
        if enabled:
            self._start_minecraft()
        else:
            self._stop_minecraft()
        return self.status(game)

    def status(self, game: str = "minecraft") -> Dict[str, Any]:
        if game not in GAMES:
            raise ValueError(f"unknown game: {game}")
        with self._lock:
            agent = self._children.get("agent")
            bot = self._children.get("bot")
            agent_info = agent.info() if agent else None
            bot_info = bot.info() if bot else None
        bot_status = _get_json(f"{BOT_API}/status")
        bot_up = bot_status is not None
        tel = read_telemetry()
        live = bool(agent_info and agent_info["running"])
        return {
            "game": game,
            "enabled": read_autoplay().get(game, False),
            "agent": agent_info,
            "bot": bot_info,
            "bot_api": bot_up,
            "bot_external": bot_up and not (bot_info and bot_info["running"]),
            "loop_state": tel.get("loop_state") if live else None,
            "thought": tel.get("current_thought") if live else None,
            "goal": tel.get("goal_description") if live else None,
            "health": tel.get("health") if live else None,
            "order": tel.get("current_order") if live else None,
            "mode": tel.get("mode") if live else None,
            "food": tel.get("food") if live else None,
            "position": tel.get("position") if live else None,
            "viewer_port": self._viewer_port() if bot_up else None,
            "hud": _hud(bot_status),
            "server": _server_info(bot_status, bot_up),
        }

    def send_directive(self, game: str, text: str, source: str = "web") -> Dict[str, Any]:
        """Hand an order to the game agent through bot.js's /directive
        inbox. bot.js interrupts the running skill; the agent picks the
        order up on its next tick. `source` is "web" (the Games page) or
        "talk" (said to Lumi in chat or out loud). Raises ValueError for a
        bad order and RuntimeError when bot.js isn't running."""
        if game not in GAMES:
            raise ValueError(f"unknown game: {game}")
        clean = " ".join(str(text or "").split())[:MAX_DIRECTIVE]
        clean = "".join(ch for ch in clean if ch.isprintable())
        if not clean:
            raise ValueError("The order is empty.")
        source = source if source in ("web", "talk") else "web"
        body = json.dumps({"text": clean, "source": source}).encode()
        req = urllib.request.Request(f"{BOT_API}/directive", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                result = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise ValueError(json.loads(e.read() or b"{}").get("message") or f"bot.js said {e.code}")
        except Exception:
            raise RuntimeError("Minecraft AI isn't running (bot.js isn't answering on :5057).")
        agent = self._children.get("agent")
        if not (agent and agent.running):
            result["message"] = ((result.get("message") or "Order received") +
                                 " — the game agent isn't running, so it will wait "
                                 "until Minecraft AI is switched on.")
        hub_emit(self.signals, "minecraft",
                 f"order {'said to Lumi' if source == 'talk' else 'from the Games page'}: {clean}")
        return result

    def hud_action(self, game: str, action: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Run one of the few bot.js actions the Games page may send
        (dropping items from the HUD). Raises ValueError for anything
        else, RuntimeError when bot.js isn't running."""
        if game not in GAMES:
            raise ValueError(f"unknown game: {game}")
        if action not in HUD_ACTIONS:
            raise ValueError(f"'{action}' can't be sent from the Games page")
        name = str(args.get("name") or "")
        if not ITEM_NAME_RE.match(name):
            raise ValueError("name must be an item id like cobblestone")
        try:
            count = max(1, min(int(args.get("count") or 1), 64 * 36))
        except (TypeError, ValueError):
            raise ValueError("count must be a number")
        body = json.dumps({"action": action, "args": {"name": name, "count": count}}).encode()
        req = urllib.request.Request(f"{BOT_API}/action", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            return json.loads(e.read() or b"{}") or {"success": False, "message": f"bot.js said {e.code}"}
        except Exception:
            raise RuntimeError("Minecraft AI isn't running (bot.js isn't answering on :5057).")

    def set_server(self, game: str, host: str, port: Any) -> Dict[str, Any]:
        """Point the bot at another world. With bot.js running, it checks
        the world answers and reconnects (POST /reconnect), keeping the
        current connection if it doesn't. Without bot.js, the choice is
        saved for the next start. Raises ValueError for a bad address or
        an unreachable world."""
        if game not in GAMES:
            raise ValueError(f"unknown game: {game}")
        host = str(host or "").strip()
        try:
            port = int(port)
        except (TypeError, ValueError):
            port = 0
        if not HOST_RE.match(host) or not 1 <= port <= 65535:
            raise ValueError("Enter a host (like localhost) and a port from 1 to 65535.")
        if not self._bot_api_up():
            os.makedirs(MC_DIR, exist_ok=True)
            with open(SERVER_PATH, "w", encoding="utf-8") as f:
                json.dump({"host": host, "port": port}, f)
            return {"success": True, "saved_only": True,
                    "message": f"Saved {host}:{port}. The bot will join it when Minecraft AI starts."}
        body = json.dumps({"host": host, "port": port}).encode()
        req = urllib.request.Request(f"{BOT_API}/reconnect", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                result = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise ValueError(json.loads(e.read() or b"{}").get("message") or f"bot.js said {e.code}")
        except Exception as e:
            raise ValueError(f"bot.js didn't answer: {e}")
        hub_emit(self.signals, "minecraft", f"server switched from the Games page: {host}:{port}")
        return result

    # ─── Skin ────────────────────────────────────────────────────

    def skin_path(self) -> str:
        """Where bot.js reads the skin: SKIN_PATH in its .env, or
        lumi_skin.png next to bot.js."""
        configured = _read_env_value(os.path.join(BOT_DIR, ".env"), "SKIN_PATH")
        if configured:
            return configured if os.path.isabs(configured) else os.path.normpath(os.path.join(BOT_DIR, configured))
        return os.path.join(BOT_DIR, "lumi_skin.png")

    def save_skin(self, data: bytes) -> Dict[str, Any]:
        """Replace the skin with an uploaded PNG after checking it's a
        real 64x64 (or legacy 64x32) skin. The previous one is kept as
        <name>.bak.png. Raises ValueError for anything else."""
        if len(data) > MAX_SKIN_BYTES:
            raise ValueError("That file is too big for a skin (64 KB max).")
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("A skin has to be a PNG file.")
        try:
            from PIL import Image
            import io
            with Image.open(io.BytesIO(data)) as img:
                img.verify()
            with Image.open(io.BytesIO(data)) as img:
                size = img.size
        except Exception:
            raise ValueError("That PNG couldn't be read.")
        if size not in ((64, 64), (64, 32)):
            raise ValueError(f"A skin is 64x64 pixels (or 64x32); this one is {size[0]}x{size[1]}.")
        path = self.skin_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            root_name, _ = os.path.splitext(path)
            shutil.copyfile(path, root_name + ".bak.png")
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        hub_emit(self.signals, "minecraft", f"skin replaced from the Games page ({size[0]}x{size[1]})")
        return {"success": True, "message": "Skin updated. bot.js serves the new one on :3001 right away."}

    def resume_if_enabled(self) -> None:
        """Called once at boot: restart games left switched on."""
        for game in GAMES:
            if read_autoplay().get(game):
                hub_emit(self.signals, game, "autoplay is on — starting game agent")
                threading.Thread(target=self._start_minecraft, daemon=True).start()

    def shutdown(self) -> None:
        """Stop every child without touching the autoplay flag, so the
        games come back on the next boot."""
        self._stop_minecraft()

    # ─── Minecraft ───────────────────────────────────────────────

    def _start_minecraft(self) -> None:
        with self._lock:
            bot = self._children.get("bot")
            agent = self._children.get("agent")
            if not (bot and bot.running) and not self._bot_api_up():
                self._spawn_bot()
            if not (agent and agent.running):
                self._spawn("agent", [sys.executable, "-u", AGENT_SCRIPT], cwd=WORKSPACE_ROOT)

    def _stop_minecraft(self) -> None:
        with self._lock:
            # Agent first, so it doesn't log a burst of "bot unreachable".
            for name in ("agent", "bot"):
                child = self._children.get(name)
                if child and child.running:
                    child.stopping = True
                    child.proc.terminate()
                    try:
                        child.proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.proc.kill()
                    hub_emit(self.signals, "minecraft", f"{name} stopped")

    def _spawn_bot(self) -> None:
        node = shutil.which("node")
        if not node:
            hub_emit(self.signals, "minecraft",
                     "can't start bot.js: Node.js is not on PATH", level="error")
            return
        if not os.path.isdir(os.path.join(BOT_DIR, "node_modules")):
            hub_emit(self.signals, "minecraft",
                     "can't start bot.js: run `npm install` in sdk/examples/minecraft first",
                     level="error")
            return
        # An explicit heap ceiling: Node's default depends on the machine's
        # RAM and can be as low as 2 GB. bot.js also pauses its live viewer
        # well before this (VIEWER_HEAP_LIMIT_MB).
        self._spawn("bot", [node, "--max-old-space-size=4096", "bot.js"], cwd=BOT_DIR)

    def _spawn(self, name: str, cmd, cwd: str) -> None:
        os.makedirs(LOG_DIR, exist_ok=True)
        log_path = os.path.join(LOG_DIR, f"{name}.log")
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            proc = subprocess.Popen(
                cmd, cwd=cwd, env=env, creationflags=flags,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
        except Exception as e:
            hub_emit(self.signals, "minecraft", f"failed to start {name}: {e}", level="error")
            return
        self._job.add(proc)
        child = _Child(name, proc, log_path)
        self._children[name] = child
        hub_emit(self.signals, "minecraft", f"{name} started (pid {proc.pid})")
        threading.Thread(target=self._pump, args=(child,), daemon=True,
                         name=f"game-{name}-output").start()

    def _pump(self, child: _Child) -> None:
        """Forward a child's output to its log file and the log hub, then
        report how it ended."""
        with open(child.log_path, "a", encoding="utf-8") as log:
            log.write(f"\n=== {child.name} started {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
            for raw in child.proc.stdout:
                line = raw.rstrip()
                if not line:
                    continue
                child.tail.append(line)
                log.write(line + "\n")
                log.flush()
                if not _QUIET.search(line):
                    level = "error" if _ERRORISH.search(line) else "info"
                    hub_emit(self.signals, "minecraft", f"[{child.name}] {line}", level=level)
        child.exit_code = child.proc.wait()
        if child.stopping:
            return
        last = " | ".join(list(child.tail)[-3:]) or "no output"
        level = "error" if child.exit_code else "warn"
        hub_emit(self.signals, "minecraft",
                 f"{child.name} exited unexpectedly (code {child.exit_code}). Last output: {last}",
                 level=level)

    # ─── bot.js probes ───────────────────────────────────────────

    def _bot_api_up(self) -> bool:
        return _get_json(f"{BOT_API}/status") is not None

    def _viewer_port(self) -> Optional[int]:
        ts, port = self._viewer_cache
        if time.time() - ts < 5:
            return port
        cfg = _get_json(f"{BOT_API}/config") or {}
        port = cfg.get("viewer_port")
        self._viewer_cache = (time.time(), port)
        return port


def _hud(bot_status: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """What the Games page's HUD shows. Inventory is cleared when the bot
    isn't in a world, so a stale one never shows."""
    if bot_status is None:
        return {"online": False, "reason": "bot.js isn't running"}
    base = {k: bot_status.get(k) for k in ("username", "server", "auth")}
    if not bot_status.get("ready"):
        return dict(base, online=False, reason="not in a world yet")
    return dict(base, online=True,
                **{k: bot_status.get(k) for k in ("version", "health", "food", "armor",
                                                  "held_slot", "slots", "position")})


def _server_info(bot_status: Optional[Dict[str, Any]], bot_up: bool) -> Dict[str, Any]:
    """The server the bot uses (or will use), and LAN worlds bot.js has
    heard announced."""
    if bot_up:
        lan = _get_json(f"{BOT_API}/lan") or {}
        cur = lan.get("current") or {}
        return {"host": cur.get("host"), "port": cur.get("port"),
                "lan_worlds": lan.get("worlds") or [], "connected": bool((bot_status or {}).get("ready"))}
    saved = _read_json(SERVER_PATH)
    return {"host": saved.get("host"), "port": saved.get("port"), "lan_worlds": [], "connected": False}


def texture_file(name: str, version: Optional[str] = None) -> Optional[str]:
    """The local PNG for an item: its item icon, or for blocks a face
    texture (side, front, top). None if prismarine-viewer has none."""
    if not ITEM_NAME_RE.match(name or ""):
        return None
    root = os.path.join(TEXTURE_ROOT, version or DEFAULT_TEXTURE_VERSION)
    if not os.path.isdir(root):
        root = os.path.join(TEXTURE_ROOT, DEFAULT_TEXTURE_VERSION)
    # Blocks have no item icon: planks and logs use their block texture
    # (bark for logs); a crafting table or furnace, its front.
    candidates = [("items", name), ("blocks", name)]
    candidates += [("blocks", f"{name}_{face}") for face in ("front", "side", "top")]
    for folder, stem in candidates:
        path = os.path.join(root, folder, stem + ".png")
        if os.path.isfile(path):
            return path
    return None


def _read_env_value(path: str, key: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                k, sep, v = line.strip().partition("=")
                if sep and k.strip() == key and not k.strip().startswith("#"):
                    return v.strip().strip('"').strip("'") or None
    except OSError:
        pass
    return None


def _get_json(url: str) -> Optional[Dict[str, Any]]:
    try:
        with urllib.request.urlopen(url, timeout=0.6) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _read_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

"""
Island manager: starts and stops Lumi Island (island/, a Tauri app) for
the Settings switch, so it never needs its own terminal.

  - The switch state lives in data/island.json; when it's on, Lumi starts
    the island at boot.
  - The process runs with no console window and is tied to Lumi through a
    Windows job object, so it closes when Lumi does, however Lumi ends.
  - The island must be built once first: `npm install` then
    `npm run build` in island/ (needs Rust; see island/README.md).
  - The page is baked into the exe, so an exe older than the island's
    sources shows an old UI. At boot (start_at_boot, from main.py) Lumi
    rebuilds a stale exe in the background, then starts it.
    LUMI_ISLAND_AUTOBUILD=0 turns that off; status() still reports "stale".
"""

import json
import os
import secrets
import shutil
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from constants import DATA_ROOT, WORKSPACE_ROOT
from modules import hitl_server
from modules.game_manager import _KillOnCloseJob
from modules.log_hub import emit as hub_emit

ISLAND_DIR = os.path.join(WORKSPACE_ROOT, "island")
EXE_NAME = "lumi-island.exe" if os.name == "nt" else "lumi-island"
EXE_PATH = os.path.join(ISLAND_DIR, "src-tauri", "target", "release", EXE_NAME)
STATE_PATH = os.path.join(DATA_ROOT, "island.json")

NOT_BUILT_MSG = ("Lumi Island isn't built yet. In the island folder run "
                 "`npm install` and then `npm run build` (needs Rust).")
BUILDING_MSG = "Lumi Island is being updated; it starts as soon as the build finishes."
STALE_MSG = ("Lumi Island is older than its source. Restart Lumi to rebuild it, "
             "or run `npm run build` in island/.")
BUILD_TIMEOUT_S = 30 * 60

# What goes into the exe: the page, the Rust shell and its config.
SOURCE_PATHS = (
    os.path.join(ISLAND_DIR, "src"),
    os.path.join(ISLAND_DIR, "src-tauri", "src"),
    os.path.join(ISLAND_DIR, "src-tauri", "capabilities"),
    os.path.join(ISLAND_DIR, "src-tauri", "tauri.conf.json"),
    os.path.join(ISLAND_DIR, "src-tauri", "Cargo.toml"),
)
SKIP_DIRS = frozenset({"node_modules", "target", "gen", ".git", "__pycache__"})


def newest_source_mtime(paths=None) -> float:
    newest = 0.0
    for p in (SOURCE_PATHS if paths is None else paths):
        if os.path.isfile(p):
            newest = max(newest, os.path.getmtime(p))
            continue
        for root, dirs, files in os.walk(p):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for name in files:
                try:
                    newest = max(newest, os.path.getmtime(os.path.join(root, name)))
                except OSError:
                    pass
    return newest


def is_stale(exe: Optional[str] = None, paths=None) -> bool:
    """True when the built exe is older than anything that goes into it."""
    exe = exe or EXE_PATH
    if not os.path.isfile(exe):
        return False
    return newest_source_mtime(paths) > os.path.getmtime(exe) + 1.0


def autobuild_enabled() -> bool:
    return os.environ.get("LUMI_ISLAND_AUTOBUILD", "1").strip().lower() not in ("0", "false", "no", "off")


def build_command() -> Optional[List[str]]:
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm") or shutil.which("npm")
    return [npm, "run", "build"] if npm else None


class IslandManager:
    def __init__(self, signals) -> None:
        self.signals = signals
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._job = _KillOnCloseJob()
        self._building = False
        self.last_build: Optional[Dict[str, Any]] = None
        signals.island_manager = self

    # ── state ──────────────────────────────────────────────────────

    def is_enabled(self) -> bool:
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                return bool(json.load(f).get("enabled", False))
        except (OSError, ValueError):
            return False

    def _save(self, enabled: bool) -> None:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"enabled": enabled}, f)
        os.replace(tmp, STATE_PATH)

    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def status(self) -> Dict[str, Any]:
        built = os.path.isfile(EXE_PATH)
        stale = built and is_stale()
        return {
            "enabled": self.is_enabled(),
            "running": self.running(),
            "built": built,
            "stale": stale,
            "building": self._building,
            "message": (BUILDING_MSG if self._building else NOT_BUILT_MSG if not built
                        else STALE_MSG if stale else None),
        }

    # ── switch ─────────────────────────────────────────────────────

    def set_enabled(self, enabled: bool) -> Dict[str, Any]:
        """Raises RuntimeError when turning on an island that isn't built."""
        with self._lock:
            if enabled:
                self._start()
            else:
                self._stop()
            self._save(enabled)
        return self.status()

    def resume_if_enabled(self) -> None:
        """At boot: bring the island back if the switch was left on."""
        if not self.is_enabled():
            return
        with self._lock:
            try:
                self._start()
            except RuntimeError as e:
                hub_emit(self.signals, "system", f"island not started: {e}", level="warn")

    def start_at_boot(self) -> threading.Thread:
        """main.py: if the switch is on and the exe is older than its source,
        rebuild it in the background, then start it. Boot never waits on this."""
        t = threading.Thread(target=self._boot, name="island-boot", daemon=True)
        t.start()
        return t

    def _boot(self) -> None:
        if self.is_enabled() and autobuild_enabled() and is_stale():
            self.rebuild()
        self.resume_if_enabled()

    def rebuild(self, runner=None) -> Tuple[bool, str]:
        """Run `npm run build` in island/. The island is stopped first, since
        Windows won't replace a running exe. Returns (ok, message); never raises."""
        cmd = build_command()
        if cmd is None:
            return self._built(False, "npm isn't on PATH, so Lumi Island can't be rebuilt.")
        with self._lock:
            if self._building:
                return False, BUILDING_MSG
            self._building = True
            self._stop()
        hub_emit(self.signals, "system", "island: rebuilding with the latest source (a few minutes)")
        t0 = time.time()
        try:
            run = runner or subprocess.run
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            r = run(cmd, cwd=ISLAND_DIR, capture_output=True, text=True, errors="replace",
                    timeout=BUILD_TIMEOUT_S, creationflags=flags)
            ok = r.returncode == 0 and os.path.isfile(EXE_PATH)
            tail = " ".join(((r.stderr or "") + (r.stdout or "")).strip().splitlines()[-3:])[:300]
            msg = (f"island rebuilt in {time.time() - t0:.0f}s" if ok
                   else f"island build failed (exit {r.returncode}): {tail}")
        except subprocess.TimeoutExpired:
            ok, msg = False, f"island build took over {BUILD_TIMEOUT_S // 60} minutes and was stopped"
        except OSError as e:
            ok, msg = False, f"island build couldn't run: {e}"
        finally:
            with self._lock:
                self._building = False
        return self._built(ok, msg)

    def _built(self, ok: bool, msg: str) -> Tuple[bool, str]:
        self.last_build = {"ok": ok, "message": msg, "at": time.time()}
        hub_emit(self.signals, "system", msg, level="info" if ok else "warn")
        return ok, msg

    def shutdown(self) -> None:
        with self._lock:
            self._stop()

    # ── process ────────────────────────────────────────────────────

    def _start(self) -> None:
        if self.running():
            return
        if self._building:
            raise RuntimeError(BUILDING_MSG)
        if not os.path.isfile(EXE_PATH):
            raise RuntimeError(NOT_BUILT_MSG)
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        # A fresh secret each launch. The island gets it through its
        # environment, and the socket refuses clients without it, so only the
        # process Lumi started can approve tools, send chat or read mail.
        token = secrets.token_urlsafe(32)
        hitl_server.set_token(token)
        self._proc = subprocess.Popen(
            [EXE_PATH], cwd=os.path.dirname(EXE_PATH), creationflags=flags,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=dict(os.environ, LUMI_ISLAND_TOKEN=token,
                     LUMI_ISLAND_WS=f"ws://{hitl_server.HITL_HOST}:{hitl_server.HITL_PORT}{hitl_server.HITL_PATH}"),
        )
        self._job.add(self._proc)
        hub_emit(self.signals, "system", f"island started (pid {self._proc.pid})")

    def _stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        hub_emit(self.signals, "system", "island stopped")

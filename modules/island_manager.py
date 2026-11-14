"""
Island manager: starts and stops Lumi Island (island/, a Tauri app) for
the Settings switch, so it never needs its own terminal.

  - The switch state lives in data/island.json; when it's on, Lumi starts
    the island at boot.
  - The process runs with no console window and is tied to Lumi through a
    Windows job object, so it closes when Lumi does, however Lumi ends.
  - The island must be built once first: `npm install` then
    `npm run build` in island/ (needs Rust; see island/README.md).
"""

import json
import os
import secrets
import subprocess
import threading
from typing import Any, Dict, Optional

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


class IslandManager:
    def __init__(self, signals) -> None:
        self.signals = signals
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._job = _KillOnCloseJob()
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
        return {
            "enabled": self.is_enabled(),
            "running": self.running(),
            "built": built,
            "message": None if built else NOT_BUILT_MSG,
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

    def shutdown(self) -> None:
        with self._lock:
            self._stop()

    # ── process ────────────────────────────────────────────────────

    def _start(self) -> None:
        if self.running():
            return
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

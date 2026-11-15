"""
Proactive Watchdog (Pipeline card: System).

Watches the PC so Lumi can speak up:

  * CPU and RAM, every RESOURCE_INTERVAL_S (60 s). Over CPU_LIMIT (90 %)
    or RAM_LIMIT (95 %) for STRIKES (3) checks in a row fires
        [WATCHDOG EVENT] High RAM usage detected. chrome.exe is using 41%.
    (the top process by that resource). Once fired it stays quiet until
    usage drops back under the limit, and at most once per ALERT_COOLDOWN_S.
  * Games in WATCHED_GAMES (window-title substrings, e.g. "Stellar Blade"),
    every GAME_INTERVAL_S (10 s):
      - not responding (Windows' IsHungAppWindow) for HUNG_STRIKES checks:
        [WATCHDOG EVENT] Stellar Blade stopped responding.
      - crashed: the window and process are gone AND there's evidence of a
        crash, so quitting normally isn't reported: a Windows Error
        Reporting dialog for its process, an Application Error (event 1000)
        for its exe in the last few minutes, or it was hung just before:
        [WATCHDOG EVENT] Game crashed. Stellar Blade closed unexpectedly.

Every event:
  * sets the System agent to error ("alarm": the pixel System character
    flashes red, and the VTS avatar flinches),
  * goes into Lumi's history as a "watchdog_event" turn; the prompter speaks
    a fixed line ("Hey, your game just crashed. Want me to try and restart
    it?") instead of running a model, and remembers the offer, so a plain
    "yes" restarts the game (after approval) even with agent mode off.

While a game runs, its exe, arguments and folder are remembered so
restart_crashed_game can relaunch it. That tool is approval-gated (it
launches a program) and also available to the agent.

Config (constants.py, all optional): WATCHDOG_ENABLED, WATCHED_GAMES,
WATCHDOG_CPU_LIMIT, WATCHDOG_RAM_LIMIT.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import constants
from modules.module import Module
from modules import agent_tracker
from modules.log_hub import emit as hub_emit
from modules.everyday_common import ToolContext

AGENT = "system"

RESOURCE_INTERVAL_S = 60.0
GAME_INTERVAL_S = 10.0
CPU_LIMIT = float(getattr(constants, "WATCHDOG_CPU_LIMIT", 90.0))
RAM_LIMIT = float(getattr(constants, "WATCHDOG_RAM_LIMIT", 95.0))
STRIKES = 3
HUNG_STRIKES = 2
ALERT_COOLDOWN_S = 30 * 60
OFFER_TTL_S = 10 * 60
WATCHED_GAMES: List[str] = list(getattr(constants, "WATCHED_GAMES", ["Stellar Blade"]))

# ─── pure logic (tested) ──────────────────────────────────────────────


class StrikeCounter:
    """N consecutive over-limit checks fire once; firing re-arms only after
    a check under the limit, and never more often than cooldown_s."""

    def __init__(self, limit: float, strikes: int = STRIKES, cooldown_s: float = ALERT_COOLDOWN_S):
        self.limit, self.strikes, self.cooldown_s = limit, strikes, cooldown_s
        self.count = 0
        self.armed = True
        self.last_fired = -1e18

    def check(self, value: float, now: float) -> bool:
        if value <= self.limit:
            self.count = 0
            self.armed = True
            return False
        self.count += 1
        if self.count >= self.strikes and self.armed and now - self.last_fired >= self.cooldown_s:
            self.armed = False
            self.last_fired = now
            return True
        return False


@dataclass
class GameState:
    title: str                      # the watched name ("Stellar Blade")
    pid: Optional[int] = None
    exe: Optional[str] = None
    cmdline: List[str] = field(default_factory=list)
    cwd: Optional[str] = None
    hung: int = 0
    was_hung: bool = False
    reported_hang: bool = False


def resource_message(kind: str, top_name: str, usage: float) -> str:
    return f"[WATCHDOG EVENT] High {kind} usage detected. {top_name} is using {usage:.0f}%."


SAY = {
    "crash": "[worried] Hey, your game just crashed. Want me to try and restart it?",
    "hung": "[worried] Hey, {game} stopped responding. Want me to close it and restart it?",
    "RAM": "[worried] Heads up, your memory is almost full. {top} is using {usage:.0f} percent of it.",
    "CPU": "[worried] Heads up, your CPU is maxed out. {top} is using {usage:.0f} percent.",
}

_YES = re.compile(r"^\s*(yes|yeah|yep|yup|sure|ok(ay)?|please|do it|go ahead|restart( it)?|"
                  r"yes please|please do|go for it)\b[\s.!]*$", re.I)


def is_affirmative(text: str) -> bool:
    return bool(_YES.match(text or ""))


# ─── Windows probes ───────────────────────────────────────────────────

def _windows() -> List[Tuple[int, str, int]]:
    """Visible top-level windows: (hwnd, title, pid)."""
    if sys.platform != "win32":
        return []
    from ctypes import wintypes
    u32 = ctypes.windll.user32
    out = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if u32.IsWindowVisible(hwnd):
            n = u32.GetWindowTextLengthW(hwnd)
            if n > 0:
                buf = ctypes.create_unicode_buffer(n + 1)
                u32.GetWindowTextW(hwnd, buf, n + 1)
                pid = wintypes.DWORD()
                u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                out.append((hwnd, buf.value, pid.value))
        return True
    u32.EnumWindows(cb, 0)
    return out


def _is_hung(hwnd: int) -> bool:
    try:
        return bool(ctypes.windll.user32.IsHungAppWindow(hwnd))
    except Exception:
        return False


def _wer_dialog_for(pid: int) -> bool:
    """WerFault.exe -p <pid> is Windows reporting a crash of that process."""
    import psutil
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            if (p.info["name"] or "").lower() == "werfault.exe":
                args = " ".join(p.info["cmdline"] or [])
                if re.search(rf"-p\s+{pid}\b", args):
                    return True
        except Exception:
            continue
    return False


def _crash_logged(exe_name: str, within_s: float = 300) -> bool:
    """An Application Error (event 1000) for this exe in the event log."""
    if sys.platform != "win32" or not exe_name:
        return False
    ms = int(within_s * 1000)
    q = f"*[System[(EventID=1000) and TimeCreated[timediff(@SystemTime) <= {ms}]]]"
    try:
        out = subprocess.run(["wevtutil", "qe", "Application", f"/q:{q}", "/c:10", "/rd:true", "/f:text"],
                             capture_output=True, text=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except Exception:
        return False
    return exe_name.lower() in (out or "").lower()


def top_process(kind: str) -> Tuple[str, float]:
    """(name, percent) of the process using the most RAM or CPU."""
    import psutil
    if kind == "RAM":
        best = max((p for p in psutil.process_iter(["name", "memory_percent"])
                    if p.info.get("memory_percent") is not None),
                   key=lambda p: p.info["memory_percent"], default=None)
        return (best.info["name"], best.info["memory_percent"]) if best else ("unknown", 0.0)
    procs = list(psutil.process_iter(["name"]))
    for p in procs:
        try:
            p.cpu_percent(None)
        except Exception:
            pass
    time.sleep(0.5)
    best, best_v = None, -1.0
    ncpu = psutil.cpu_count() or 1
    for p in procs:
        try:
            v = p.cpu_percent(None) / ncpu
        except Exception:
            continue
        if v > best_v and (p.info["name"] or "").lower() != "system idle process":
            best, best_v = p, v
    return (best.info["name"] if best else "unknown", max(0.0, best_v))


# ─── the module ───────────────────────────────────────────────────────

class Watchdog(Module):
    def __init__(self, signals, enabled=True, games: Optional[List[str]] = None,
                 probes: Optional[Dict[str, Callable]] = None):
        enabled = enabled and bool(getattr(constants, "WATCHDOG_ENABLED", True))
        super().__init__(signals, enabled)
        self.cpu = StrikeCounter(CPU_LIMIT)
        self.ram = StrikeCounter(RAM_LIMIT)
        self.games: Dict[str, GameState] = {g: GameState(g) for g in (games if games is not None else WATCHED_GAMES)}
        p = probes or {}
        self._windows = p.get("windows", _windows)
        self._is_hung = p.get("is_hung", _is_hung)
        self._wer = p.get("wer", _wer_dialog_for)
        self._crash_logged = p.get("crash_logged", _crash_logged)
        self._top = p.get("top", top_process)
        self._proc_info = p.get("proc_info", self._psutil_info)
        self._alive = p.get("alive", self._pid_alive)
        self.last_crashed: Optional[GameState] = None
        self.events: List[str] = []
        signals.watchdog = self

    # -- probes ----------------------------------------------------------
    @staticmethod
    def _psutil_info(pid: int) -> Tuple[Optional[str], List[str], Optional[str]]:
        import psutil
        try:
            pr = psutil.Process(pid)
            return pr.exe(), pr.cmdline(), pr.cwd()
        except Exception:
            return None, [], None

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        import psutil
        try:
            return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
        except Exception:
            return False

    # -- checks ------------------------------------------------------------
    def check_resources(self, now: Optional[float] = None) -> List[str]:
        import psutil
        now = time.time() if now is None else now
        fired = []
        cpu = psutil.cpu_percent(interval=None)
        ram = psutil.virtual_memory().percent
        for kind, counter, value in (("CPU", self.cpu, cpu), ("RAM", self.ram, ram)):
            if counter.check(value, now):
                name, usage = self._top(kind)
                self._fire(resource_message(kind, name, usage), "resource",
                           SAY[kind].format(top=name, usage=usage), f"High {kind}: {name} {usage:.0f}%")
                fired.append(kind)
        return fired

    def check_games(self) -> List[str]:
        fired = []
        wins = self._windows()
        for name, g in self.games.items():
            match = next(((h, t, pid) for h, t, pid in wins if name.lower() in t.lower()), None)
            if match:
                hwnd, _, pid = match
                if pid != g.pid:
                    g.pid = pid
                    g.exe, g.cmdline, g.cwd = self._proc_info(pid)
                    g.hung, g.was_hung, g.reported_hang = 0, False, False
                if self._is_hung(hwnd):
                    g.hung += 1
                    g.was_hung = True
                    if g.hung >= HUNG_STRIKES and not g.reported_hang:
                        g.reported_hang = True
                        self.last_crashed = g
                        self._fire(f"[WATCHDOG EVENT] {name} stopped responding.", "hung",
                                   SAY["hung"].format(game=name), f"{name} not responding")
                        fired.append(f"hung:{name}")
                else:
                    g.hung = 0
                continue
            if g.pid is None:
                continue
            # The window is gone. Crash, or did they quit?
            if self._alive(g.pid):
                continue                       # still running (loading screen, minimized to tray...)
            exe_name = os.path.basename(g.exe or "")
            crashed = g.was_hung or self._wer(g.pid) or self._crash_logged(exe_name)
            gone = GameState(name, g.pid, g.exe, g.cmdline, g.cwd)
            g.pid, g.hung, g.was_hung, g.reported_hang = None, 0, False, False
            if crashed:
                self.last_crashed = gone
                self._fire(f"[WATCHDOG EVENT] Game crashed. {name} closed unexpectedly.", "crash",
                           SAY["crash"], f"Game crashed: {name}")
                fired.append(f"crash:{name}")
            else:
                hub_emit(self.signals, "watchdog", f"{name} closed (no crash evidence: assuming you quit)")
        return fired

    # -- events ------------------------------------------------------------
    def _fire(self, event: str, kind: str, say: str, card: str) -> None:
        self.events.append(event)
        hub_emit(self.signals, "watchdog", event, level="warn")
        print(f"\033[93m{event}\033[0m")
        agent_tracker.update("system", "error", task=card)
        offer = kind in ("crash", "hung") and self.last_crashed is not None and bool(self.last_crashed.exe)
        self.signals.history.append({
            "role": "user", "content": event, "source": "watchdog_event",
            "say": say if offer or kind == "resource" else say.split("Want me")[0].strip(),
            "offer_restart": offer, "hidden": True,
        })
        self.signals.new_message = True

    # -- loop --------------------------------------------------------------
    async def run(self):
        if not self.enabled:
            return
        try:
            import psutil
            psutil.cpu_percent(interval=None)     # prime the counter
        except ImportError:
            hub_emit(self.signals, "watchdog", "psutil isn't installed; watchdog off", level="error")
            return
        hub_emit(self.signals, "watchdog", f"watching CPU/RAM and games {list(self.games)}")
        loop = asyncio.get_running_loop()
        next_res = time.time() + RESOURCE_INTERVAL_S
        while not self.signals.terminate:
            try:
                if self.games and sys.platform == "win32":
                    await loop.run_in_executor(None, self.check_games)
                if time.time() >= next_res:
                    next_res = time.time() + RESOURCE_INTERVAL_S
                    await loop.run_in_executor(None, self.check_resources)
            except Exception as e:
                hub_emit(self.signals, "watchdog", f"check failed: {type(e).__name__}: {e}", level="error")
            await asyncio.sleep(GAME_INTERVAL_S)

    # -- restart -------------------------------------------------------------
    def restart_preview(self) -> Optional[dict]:
        g = self.last_crashed
        if g is None or not g.exe:
            return None
        return {"action": "Restart game", "game": g.title, "program": g.exe,
                "arguments": g.cmdline[1:] if g.cmdline else [], "folder": g.cwd,
                "note": "It will close the frozen game first." if g.pid and self._alive(g.pid) else None}

    def restart(self) -> str:
        g = self.last_crashed
        if g is None or not g.exe:
            return "Error: I don't know which game to restart (none crashed since Lumi started)."
        if not os.path.isfile(g.exe):
            return f"Error: {g.exe} isn't there any more."
        if g.pid and self._alive(g.pid):                 # hung: close it first
            import psutil
            try:
                psutil.Process(g.pid).kill()
                psutil.Process(g.pid).wait(timeout=10)
            except Exception:
                pass
        try:
            subprocess.Popen([g.exe] + list(g.cmdline[1:]), cwd=g.cwd or os.path.dirname(g.exe),
                             creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        except Exception as e:
            return f"Error: couldn't start {g.title}: {e}"
        self.last_crashed = None
        agent_tracker.update("system", "idle")           # clears the alarm on the System card
        hub_emit(self.signals, "watchdog", f"restarted {g.title}")
        return f"Restarted {g.title}."


# ─── tool (everyday registry) ─────────────────────────────────────────

TASKS = {"restart_crashed_game": "Restarting your game"}

TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "restart_crashed_game",
        "description": ("Relaunch the game that the watchdog saw crash or freeze (it remembers the program "
                        "and its arguments). Closes it first if it's frozen. Requires the user's approval."),
        "parameters": {"type": "object", "properties": {}, "required": []}}},
]
CONFIRM_TOOLS = {"restart_crashed_game"}


def available(ctx: ToolContext) -> bool:
    wd = getattr(ctx.signals, "watchdog", None)
    return wd is not None and wd.last_crashed is not None


async def run_tool(name: str, args: dict, ctx: ToolContext) -> str:
    wd = getattr(ctx.signals, "watchdog", None)
    if name != "restart_crashed_game":
        return f"Unknown watchdog tool: {name}"
    if wd is None:
        return "Error: the watchdog isn't running."
    preview = wd.restart_preview()
    if preview is None:
        return "Error: no crashed game to restart."
    if not await ctx.confirm("restart_crashed_game", preview):
        return "The user declined to allow this action."
    return await asyncio.to_thread(wd.restart)

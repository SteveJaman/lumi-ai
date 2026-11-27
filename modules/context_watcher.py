"""
Foreground window awareness (Phase 5). CPU only, no screenshots: it reads the title
of the window you're in (Win32 GetForegroundWindow + GetWindowTextW) and turns it
into a scene preset Lumi can use.

    foreground_title() -> str            the active window's title, sanitised and capped
    scene_for(title, hour, gaming) ->    "Gaming Mode" | "Focus Mode" | "Late Night Chill" | None

Off unless the Settings toggle turns it on (CONTEXT_WATCHER in constants). Titles can
contain file names and private text, so they are cut to 120 characters, control
characters are removed, and nothing is stored: the title only feeds the preset.
"""

from __future__ import annotations

import re
import sys
from typing import Optional

MAX_TITLE = 120
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
FOCUS_WORDS = ("visual studio", "code", "pycharm", "intellij", "notepad", "word", "excel", "pdf", "docs")
LATE_HOURS = range(0, 5)


def sanitise(title: str) -> str:
    return _CONTROL.sub(" ", str(title or "")).strip()[:MAX_TITLE]


def foreground_title() -> str:
    if sys.platform != "win32":
        return ""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(min(length, MAX_TITLE) + 1)
        user32.GetWindowTextW(hwnd, buf, len(buf))
        return sanitise(buf.value)
    except Exception:
        return ""


def scene_for(title: str, hour: int, gaming: bool = False) -> Optional[str]:
    if gaming:
        return "Gaming Mode"
    low = sanitise(title).lower()
    if hour in LATE_HOURS:
        return "Late Night Chill"
    if any(w in low for w in FOCUS_WORDS):
        return "Focus Mode"
    return None

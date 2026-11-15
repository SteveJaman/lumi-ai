"""
Contextual desktop tools (Pipeline card: System). Windows only.

    get_active_window_info()               title + process of the focused window
    read_window_text(window_title)         visible text of an open window (UI Automation)
    click_button(window_title, button_text) clicks a button by its label (approval required)

get_active_window_info uses plain Win32 calls. The other two use pywinauto's
UI Automation backend (pip install pywinauto), imported lazily so Lumi starts
fine without it. UIA is COM: every call runs on one dedicated thread that
initialised COM, never on the event loop.

Safety: Lumi's own windows (Lumi Island, the console she runs in) and
password managers / credential prompts can't be read or clicked, whatever the
model asks. Otherwise she could read a vault, or click "Approve" on her own
approval card. Text read from a window is wrapped as [FROM WINDOW] data.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from modules.everyday_common import EverydayError, ToolContext, clip, one_line

AGENT = "system"

TASKS = {
    "get_active_window_info": "Checking the active window",
    "read_window_text": "Reading a window",
    "click_button": "Clicking a button",
}

TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "get_active_window_info",
        "description": "Get the title and app (process name) of the window the user is currently focused on.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "read_window_text",
        "description": ("Read the visible text of an open window, such as a document, a chat or a web page in "
                        "a browser. Match the window by part of its title; leave it empty for the focused window."),
        "parameters": {"type": "object", "properties": {
            "window_title": {"type": "string", "description": "Part of the window's title, e.g. 'Notepad' or 'Chrome'."},
        }, "required": []}}},
    {"type": "function", "function": {
        "name": "click_button",
        "description": "Click a button by its text label inside an open window. Requires the user's approval.",
        "parameters": {"type": "object", "properties": {
            "window_title": {"type": "string", "description": "Part of the window's title."},
            "button_text": {"type": "string", "description": "The button's label, e.g. 'Save' or 'OK'."},
        }, "required": ["window_title", "button_text"]}}},
]

CONFIRM_TOOLS = {"click_button"}

MAX_TEXT_CHARS = 10_000
MAX_ELEMENTS = 4000
READ_BUDGET_S = 10.0

# Never read or clicked.
_BLOCKED_PROCESSES = {
    "lumi-island.exe", "lumi island.exe",
    "1password.exe", "bitwarden.exe", "keepass.exe", "keepassxc.exe", "lastpass.exe",
    "dashlane.exe", "nordpass.exe", "enpass.exe", "roboform.exe",
    "consent.exe", "credentialuibroker.exe", "logonui.exe", "lockapp.exe",
}
_BLOCKED_TITLE_RE = re.compile(r"lumi island|password manager|windows security|credential", re.I)

_TEXT_TYPES = {"Text", "Edit", "Document", "Hyperlink", "ListItem", "DataItem", "Header",
               "HeaderItem", "TabItem", "TreeItem"}

_UIA_POOL: Optional[ThreadPoolExecutor] = None


def available(ctx: ToolContext) -> bool:
    return sys.platform == "win32"


def _pool() -> ThreadPoolExecutor:
    global _UIA_POOL
    if _UIA_POOL is None:
        def init():
            try:
                import pythoncom            # ships with pywin32, a pywinauto dependency
                pythoncom.CoInitializeEx(0)  # COINIT_MULTITHREADED: UIA is happy in the MTA
            except Exception:
                pass
        _UIA_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lumi-uia", initializer=init)
    return _UIA_POOL


async def _on_uia_thread(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(_pool(), fn, *args)


async def run_tool(name: str, args: Dict[str, Any], ctx: ToolContext) -> str:
    if sys.platform != "win32":
        return "Error: desktop tools only work on Windows."
    try:
        if name == "get_active_window_info":
            return await asyncio.to_thread(get_active_window_info)
        if name == "read_window_text":
            return await _on_uia_thread(read_window_text, str(args.get("window_title") or ""))
        if name == "click_button":
            return await click_button(ctx, str(args.get("window_title") or ""), str(args.get("button_text") or ""))
    except EverydayError as e:
        return f"Error: {e}"
    return f"Unknown desktop tool: {name}"


# ─── Win32 helpers ────────────────────────────────────────────────────

def process_name(pid: int) -> str:
    if sys.platform != "win32" or not pid:
        return ""
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_ulong(len(buf))
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        k32.CloseHandle(h)


def _foreground() -> Tuple[int, str, int]:
    """(hwnd, title, pid) of the focused window."""
    u32 = ctypes.windll.user32
    hwnd = u32.GetForegroundWindow()
    if not hwnd:
        return 0, "", 0
    n = u32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    u32.GetWindowTextW(hwnd, buf, n + 1)
    pid = ctypes.c_ulong()
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return hwnd, buf.value, pid.value


def _own_pids() -> set:
    pids = {os.getpid()}
    try:
        pids.add(os.getppid())                       # the console/terminal Lumi runs in
    except Exception:
        pass
    return pids


def blocked_reason(title: str, proc: str, pid: int) -> Optional[str]:
    if pid in _own_pids():
        return "that's Lumi's own window"
    if (proc or "").lower() in _BLOCKED_PROCESSES:
        return f"{proc} holds passwords, sign-ins or Lumi's own approvals"
    if _BLOCKED_TITLE_RE.search(title or ""):
        return "that window holds passwords, sign-ins or Lumi's own approvals"
    return None


def get_active_window_info() -> str:
    hwnd, title, pid = _foreground()
    if not hwnd:
        return "No window has focus right now (the desktop may be showing)."
    proc = process_name(pid)
    return f"Active window: \"{one_line(title, 200) or '(untitled)'}\"\nApp: {proc or 'unknown'} (pid {pid})"


# ─── UI Automation (runs on the UIA thread) ───────────────────────────

def _desktop():
    try:
        from pywinauto import Desktop
    except ImportError:
        raise EverydayError("pywinauto isn't installed. Run: pip install pywinauto")
    return Desktop(backend="uia")


def _norm(s: str) -> str:
    return " ".join(str(s or "").replace("&", "").lower().split())


def find_window(title_part: str):
    """The best open top-level window whose title contains title_part:
    an exact title match first, then the focused window, then the first.
    Empty title_part means the focused window."""
    desk = _desktop()
    fg_hwnd, _, _ = _foreground()
    wins = []
    for w in desk.windows(visible_only=True):
        try:
            t = w.window_text()
        except Exception:
            continue
        if t:
            wins.append((w, t))
    want = _norm(title_part)
    if not want:
        match = [(w, t) for w, t in wins if w.handle == fg_hwnd]
    else:
        match = [(w, t) for w, t in wins if want in _norm(t)]
    if not match:
        open_titles = ", ".join(f'"{one_line(t, 50)}"' for _, t in wins[:12])
        raise EverydayError(f"no open window matches '{title_part}'. Open windows: {open_titles or 'none'}.")
    match.sort(key=lambda wt: (_norm(wt[1]) != want, wt[0].handle != fg_hwnd))
    w, t = match[0]
    pid = w.process_id()
    proc = process_name(pid)
    why = blocked_reason(t, proc, pid)
    if why:
        raise EverydayError(f"Lumi isn't allowed to touch \"{one_line(t, 60)}\": {why}.")
    return w, t, proc


def _iter_descendants(w):
    it = getattr(w, "iter_descendants", None)
    return it() if it else iter(w.descendants())


def _element_text(el, ctype: str) -> str:
    try:
        if getattr(el.element_info.element, "CurrentIsPassword", False):
            return ""
    except Exception:
        pass
    if ctype in ("Edit", "Document"):
        for getter in ("get_value", "window_text"):
            try:
                v = getattr(el, getter)()
                if v:
                    return str(v)
            except Exception:
                continue
        return ""
    try:
        return el.window_text() or ""
    except Exception:
        return ""


def _content_root(w):
    """The page/document area when there is one (browsers, Word, PDF
    viewers), so the read starts with content rather than toolbars and
    hidden menus. Falls back to the whole window."""
    try:
        docs = [d for d in w.descendants(control_type="Document") if d.element_info.visible]
    except Exception:
        docs = []
    if not docs:
        return w
    def area(d):
        try:
            r = d.rectangle()
            return r.width() * r.height()
        except Exception:
            return 0
    return max(docs, key=area)


def read_window_text(title_part: str) -> str:
    w, title, proc = find_window(title_part)
    root = _content_root(w)
    parts: List[str] = []
    total, seen_last = 0, None
    deadline = time.monotonic() + READ_BUDGET_S
    partial = False
    for i, el in enumerate(_iter_descendants(root)):
        if i >= MAX_ELEMENTS or time.monotonic() > deadline or total > MAX_TEXT_CHARS:
            partial = True
            break
        try:
            info = el.element_info
            ctype = info.control_type
            if ctype not in _TEXT_TYPES or not info.visible:
                continue
        except Exception:
            continue
        text = " ".join(_element_text(el, ctype).split())
        if not text or text == seen_last or text == title:
            continue
        seen_last = text
        parts.append(text)
        total += len(text) + 1
    body = clip("\n".join(parts), MAX_TEXT_CHARS)
    note = " (partial: the window has more than could be read quickly)" if partial else ""
    if not body:
        return (f"Window \"{one_line(title, 120)}\" ({proc}) exposes no readable text. Some apps (games, "
                "remote desktops, canvas-drawn pages) don't publish their text to accessibility tools.")
    return f"Text of window \"{one_line(title, 120)}\" ({proc}){note}:\n{body}"


def find_button(title_part: str, button_text: str):
    w, title, proc = find_window(title_part)
    want = _norm(button_text)
    exact, partial = [], []
    for ctype in ("Button", "SplitButton"):
        try:
            found = w.descendants(control_type=ctype)
        except Exception:
            continue
        for b in found:
            try:
                label = _norm(b.window_text())
                if not (b.is_visible() and b.is_enabled()):
                    continue
            except Exception:
                continue
            if label == want:
                exact.append(b)
            elif want and want in label:
                partial.append(b)
    hits = exact or partial
    if not hits:
        raise EverydayError(f"no enabled button labelled '{button_text}' in \"{one_line(title, 60)}\".")
    if not exact and len(partial) > 1:
        labels = ", ".join(f"'{one_line(b.window_text(), 30)}'" for b in partial[:6])
        raise EverydayError(f"'{button_text}' matches several buttons ({labels}). Use the exact label.")
    return w, title, proc, hits[0]


def _describe_target(title_part: str, button_text: str) -> Dict[str, str]:
    _, title, proc, b = find_button(title_part, button_text)
    return {"action": "Click button", "button": one_line(b.window_text(), 80),
            "window": one_line(title, 120), "app": proc}


def _do_click(title_part: str, button_text: str, expect: Dict[str, str]) -> str:
    # Found again after approval: the window may have changed while the user decided.
    _, title, proc, b = find_button(title_part, button_text)
    if one_line(title, 120) != expect["window"] or proc != expect["app"]:
        raise EverydayError("the window changed while waiting for approval; nothing was clicked.")
    try:
        b.invoke()                                   # UIA Invoke: no mouse movement
    except Exception:
        b.click_input()
    return f"Clicked '{expect['button']}' in \"{expect['window']}\" ({proc})."


async def click_button(ctx: ToolContext, title_part: str, button_text: str) -> str:
    if not title_part.strip() or not button_text.strip():
        return "Error: click_button needs a window_title and a button_text."
    target = await _on_uia_thread(_describe_target, title_part, button_text)
    if not await ctx.confirm("click_button", target):
        return "The user declined to allow this action."
    return await _on_uia_thread(_do_click, title_part, button_text, target)

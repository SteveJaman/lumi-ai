"""
launch_island.py: the Island overlay as a native-feeling Windows window.

Uses pywebview on Edge WebView2 (already on Windows 10/11), so there is no
bundled Chromium the way Electron ships one.

The window is frameless, transparent and always on top, at the top centre
of the primary screen. It starts CLICK-THROUGH, so the game under it gets
every click. When an approval request arrives, the page calls
window.pywebview.api.set_interactive(true) and the window takes clicks
again, so Approve/Decline work. On collapse it goes back to click-through.

Optional: if the `keyboard` package is installed, Ctrl+Alt+L hides/shows
the island.

Usage:
    python launch_island.py
    python launch_island.py --url http://127.0.0.1:5000/island

Windows only (uses user32 for the click-through style).
"""

import argparse
import ctypes
import sys
from ctypes import wintypes

import webview

DEFAULT_URL = "http://127.0.0.1:5000/island?overlay=1"   # overlay=1: see-through page
TITLE = "Lumi Island"          # used to find our window handle
WIDTH, HEIGHT = 440, 260       # room for the expanded approval card
TOP_MARGIN = 8

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020   # mouse events fall through the window
WS_EX_TOOLWINDOW = 0x00000080    # no taskbar button, no Alt+Tab entry
LWA_ALPHA = 0x00000002
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SM_CXSCREEN = 0

if sys.platform != "win32":
    sys.exit("launch_island.py is Windows-only (click-through uses user32).")

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.DWORD,
                                              ctypes.c_ubyte, wintypes.DWORD]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                wintypes.UINT]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]


def _hwnd():
    return user32.FindWindowW(None, TITLE)


def _set_click_through(on: bool) -> None:
    """Add or remove WS_EX_TRANSPARENT on our window. Layered style is
    required for WS_EX_TRANSPARENT to take effect."""
    hwnd = _hwnd()
    if not hwnd:
        return
    ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    ex |= WS_EX_LAYERED | WS_EX_TOOLWINDOW
    ex = (ex | WS_EX_TRANSPARENT) if on else (ex & ~WS_EX_TRANSPARENT)
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex)
    # Fully opaque at the window level; the page's own alpha still shows.
    user32.SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA)
    user32.SetWindowPos(hwnd, None, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER
                        | SWP_NOACTIVATE | SWP_FRAMECHANGED)


class IslandApi:
    """Exposed to the page as window.pywebview.api."""

    def set_interactive(self, on: bool) -> bool:
        """True while an approval card is showing; False otherwise."""
        _set_click_through(not bool(on))
        return bool(on)


def _top_center_position() -> tuple:
    try:
        screen_w = int(webview.screens[0].width)   # pywebview 5+
    except Exception:
        screen_w = user32.GetSystemMetrics(SM_CXSCREEN)
    return max(0, (screen_w - WIDTH) // 2), TOP_MARGIN


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--url", default=DEFAULT_URL)
    args = parser.parse_args()

    x, y = _top_center_position()
    window = webview.create_window(
        TITLE, args.url,
        width=WIDTH, height=HEIGHT, x=x, y=y,
        frameless=True,
        transparent=True,
        on_top=True,
        focus=False,          # never steal focus from the game
        easy_drag=False,
        resizable=False,
        js_api=IslandApi(),
    )

    # The window handle exists once it's shown; start click-through then.
    window.events.shown += lambda: _set_click_through(True)

    _visible = {"on": True}
    try:
        import keyboard  # optional
        def _toggle():
            if _visible["on"]:
                window.hide()
            else:
                window.show()
            _visible["on"] = not _visible["on"]
        keyboard.add_hotkey("ctrl+alt+l", _toggle)
        print("Ctrl+Alt+L hides/shows the island.")
    except ImportError:
        pass

    webview.start(gui="edgechromium")


if __name__ == "__main__":
    main()

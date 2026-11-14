"""
launch_island.py: the Island overlay as a native-feeling Windows window.

Uses pywebview on Edge WebView2 (already on Windows 10/11), so there is no
bundled Chromium the way Electron ships one.

The window is frameless, transparent and always on top, at the top centre
of the primary screen. Instead of a big click-through window (Windows'
click-through style breaks WebView2 transparency and shows a white box),
the window is resized to fit the island in every state: the page calls
window.pywebview.api.fit(width, height) whenever the island changes size.
Only the island itself ever covers the screen.

Optional: if the `keyboard` package is installed, Ctrl+Alt+L hides/shows
the island.

Usage:
    python launch_island.py
    python launch_island.py --url http://127.0.0.1:5000/island
"""

import argparse
import ctypes
import sys

import webview

DEFAULT_URL = "http://127.0.0.1:5000/island?overlay=1"
TITLE = "Lumi Island"
IDLE_W, IDLE_H = 160, 60       # pill plus room for its shadow
MAX_W, MAX_H = 460, 420        # never larger than the biggest view needs
TOP_MARGIN = 0


def _screen_width() -> int:
    try:
        return int(webview.screens[0].width)          # pywebview 5+
    except Exception:
        if sys.platform == "win32":
            return ctypes.windll.user32.GetSystemMetrics(0)
        return 1920


class IslandApi:
    """Exposed to the page as window.pywebview.api."""

    def __init__(self) -> None:
        self.window = None
        self.screen_w = _screen_width()

    def fit(self, width, height) -> bool:
        """Resize the window to the island's size, kept at top centre."""
        if self.window is None:
            return False
        w = max(60, min(int(width), MAX_W))
        h = max(30, min(int(height), MAX_H))
        self.window.resize(w, h)
        self.window.move(max(0, (self.screen_w - w) // 2), TOP_MARGIN)
        return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Lumi Island overlay")
    parser.add_argument("--url", default=DEFAULT_URL)
    args = parser.parse_args()

    api = IslandApi()
    window = webview.create_window(
        TITLE, args.url,
        width=IDLE_W, height=IDLE_H,
        x=max(0, (api.screen_w - IDLE_W) // 2), y=TOP_MARGIN,
        frameless=True,
        transparent=True,     # page background shows through; no white box
        on_top=True,
        focus=False,          # never steal focus from the game
        easy_drag=False,
        resizable=False,
        js_api=api,
    )
    api.window = window

    visible = {"on": True}
    try:
        import keyboard  # optional
        def _toggle():
            (window.hide if visible["on"] else window.show)()
            visible["on"] = not visible["on"]
        keyboard.add_hotkey("ctrl+alt+l", _toggle)
        print("Ctrl+Alt+L hides/shows the island.")
    except ImportError:
        pass

    webview.start(gui="edgechromium")


if __name__ == "__main__":
    main()

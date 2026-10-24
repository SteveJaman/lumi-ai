from modules.module import Module
from constants import MULTIMODAL_STRATEGY, DATA_ROOT
import base64
import io
import re
import requests
import subprocess
import time
import os
import json
from datetime import datetime

# ===============================================================
# VLM OUTPUT FILTER (Phase 9.6d)
# ---------------------------------------------------------------
# The VLM occasionally hallucinates a string that looks like an API
# key, token, or long encoded blob. Anything that matches a
# key-shaped pattern is dropped before it reaches the prompt or the
# visual-memory log.
# ===============================================================
import re as _re

_VLM_SECRET_PATTERNS = [
    _re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
    _re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    _re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    _re.compile(r"[MN][A-Za-z\d]{23}\.[\w\-]{6}\.[\w\-]{27,}"),
    _re.compile(r"\b[0-9a-fA-F]{32,}\b"),
    _re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),  # long base64-ish
]


def _vlm_output_safe(text: str) -> bool:
    """Return True if the VLM output looks like an ordinary
    description and not a leaked secret. False means drop it."""
    if not text:
        return False
    for pat in _VLM_SECRET_PATTERNS:
        if pat.search(text):
            return False
    return True


try:
    import mss
    from PIL import Image
    HAS_MSS = True
except ImportError:
    HAS_MSS = False
    print("\033[91mMULTIMODAL: 'mss' and/or 'pillow' not installed — screen capture is disabled.\033[0m")

# Runtime screenshot/memory storage — under data/logs/ after the reorg.
_IMAGES_DIR = os.path.join(DATA_ROOT, "logs", "images")
_VISUAL_MEMORY_FILE = os.path.join(DATA_ROOT, "logs", "visual_memory.json")

# Screen-reference regex. Requires BOTH a vision verb AND a screen-related
# noun within a short window. This is what keeps "read STATUS.md" from
# firing the VLM while "read the screen" still does.
FLEXIBLE_VISION_PATTERN = re.compile(
    r"\b(look|see|check|view|glance|describe|tell|read|showing|open|what)\b.{0,45}\b(screen|monitor|display|desktop|computer)\b",
    re.IGNORECASE,
)

# Conversational vision triggers. These must appear at the START of the
# message to fire — otherwise "do you see the bug in this code" matches
# the substring "do you see" and incorrectly wakes the VLM. Anchoring
# to start-of-message keeps the intent precise.
CONVERSATIONAL_TRIGGERS = [
    "what do you see",
    "what are you looking at",
    "can you see",
    "do you see",
]

# Recall triggers. These are only treated as visual-recall intent when the
# message ALSO references a screen-related noun. "I remember that movie"
# no longer hijacks the turn.
RECALL_TRIGGERS = [
    "remember",
    "earlier",
    "past",
    "previously",
    "did i show you",
    "what was on",
]

# The screen-related nouns that gate the recall-trigger check above.
RECALL_SCREEN_WORDS = (
    "screen", "monitor", "display", "desktop", "computer", "screenshot"
)

# The game window's title, e.g. "Minecraft* 1.19 - Singleplayer" (not the
# launcher).
MINECRAFT_TITLE = re.compile(r"^Minecraft\*?\s+\d")


def _print_window(hwnd):
    """Render a window's client area with PrintWindow(PW_RENDERFULLCONTENT),
    which works even when other windows cover it (including Minecraft's
    OpenGL surface). Returns a PIL image, or None if it failed or came
    back blank. Windows only."""
    import ctypes
    from ctypes import wintypes
    # Private handles, so these argtypes don't leak into ctypes.windll,
    # which other modules share.
    user32, gdi32 = ctypes.WinDLL("user32"), ctypes.WinDLL("gdi32")
    user32.GetDC.restype = wintypes.HDC
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                                ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    rect = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    w, h = rect.right, rect.bottom
    if w < 50 or h < 50:
        return None
    hdc_win = user32.GetDC(hwnd)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_win)
    bmp = gdi32.CreateCompatibleBitmap(hdc_win, w, h)
    try:
        gdi32.SelectObject(hdc_mem, bmp)
        if not user32.PrintWindow(hwnd, hdc_mem, 1 | 2):  # CLIENTONLY | RENDERFULLCONTENT
            return None
        bi = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=w,
                              biHeight=-h, biPlanes=1, biBitCount=32)   # top-down
        buf = ctypes.create_string_buffer(w * h * 4)
        if not gdi32.GetDIBits(hdc_mem, bmp, 0, h, buf, ctypes.byref(bi), 0):
            return None
    finally:
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(hwnd, hdc_win)
    img = Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
    if img.convert("L").getextrema()[1] < 8:
        return None      # all black: the window's renderer didn't cooperate
    return img


def _find_window(title_re):
    """Find a visible top-level window whose title matches. Returns
    (hwnd, bounds, title, foreground, minimized), with bounds as an mss
    monitor dict in physical pixels, or None if no window matches.
    Windows only."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _visit(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if title_re.search(buf.value):
                    found.append((hwnd, buf.value))
        return True

    user32.EnumWindows(_visit, 0)
    if not found:
        return None
    hwnd, title = found[0]
    minimized = bool(user32.IsIconic(hwnd))
    rect = wintypes.RECT()
    # DWMWA_EXTENDED_FRAME_BOUNDS is in physical pixels (what mss grabs)
    # and excludes the invisible resize border GetWindowRect includes.
    if ctypes.windll.dwmapi.DwmGetWindowAttribute(
            hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect)) != 0:
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
    bounds = {"left": rect.left, "top": rect.top,
              "width": rect.right - rect.left, "height": rect.bottom - rect.top}
    return hwnd, bounds, title, user32.GetForegroundWindow() == hwnd, minimized


MONITOR_KEYWORDS = {
    1: (
        "monitor 1", "1st monitor", "first monitor", "monitor one",
        "side monitor", "secondary monitor", "left monitor", "other monitor",
    ),
    2: (
        "monitor 2", "2nd monitor", "second monitor", "monitor two",
        "main monitor", "primary monitor", "right monitor", "game monitor",
        "current monitor",
    ),
}


class MultiModal(Module):

    def __init__(self, signals, enabled=True):
        super().__init__(signals, enabled)
        self.API = self.API(self)
        self.enabled = enabled
        self.ollama_url = "http://localhost:11434/api/generate"
        self.vlm_model = "moondream"

        # Default to Monitor 2 (Main display), can be toggled to 1 by voice/text
        self.active_monitor = 2

        # Ensure visual storage directories exist — under data/logs/.
        os.makedirs(_IMAGES_DIR, exist_ok=True)
        self.memory_file = _VISUAL_MEMORY_FILE

        self._ensure_ollama_running()

        # NOTE: The live preview window (tkinter) has been removed.
        # It ran a screenshot loop in a background thread every 2 seconds,
        # which burned CPU continuously and caused GIL crashes on Windows.
        # Screenshot-on-demand still works — the module captures the screen
        # only when a vision trigger fires in `maybe_capture_from_text`.

    def _ensure_ollama_running(self):
        try:
            res = requests.get("http://localhost:11434/", timeout=2)
            if res.status_code == 200:
                return
        except Exception:
            print("\033[90mMULTIMODAL: Starting Ollama background process...\033[0m")
            try:
                subprocess.Popen(
                    ["ollama", "serve"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
                )
                time.sleep(2)
            except Exception as e:
                print(f"\033[91mMULTIMODAL Warning: Could not auto-start Ollama: {e}\033[0m")

    def _grab_monitor(self, sct, monitor_dict):
        sct_img = sct.grab(monitor_dict)
        return Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")

    def _capture_pil_image(self):
        with mss.mss() as sct:
            available = len(sct.monitors) - 1
            idx = self.active_monitor if 1 <= self.active_monitor <= available else 1
            return self._grab_monitor(sct, sct.monitors[idx])

    def _capture_screen_base64(self):
        if not HAS_MSS:
            return None, None

        try:
            img = self._capture_pil_image()
            img.thumbnail((800, 800))

            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=80)
            b64_data = base64.b64encode(buffer.getvalue()).decode("utf-8")

            # Save raw snapshot to disk under data/logs/images/.
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            img_path = os.path.join(_IMAGES_DIR, f"screen_{timestamp}.jpg")
            with open(img_path, "wb") as f:
                f.write(buffer.getvalue())

            return b64_data, img_path
        except Exception as e:
            print(f"\033[91mMULTIMODAL: Screen capture failed: {e}\033[0m")
            return None, None

    def _save_visual_memory(self, timestamp, img_path, caption):
        entry = {
            "timestamp": timestamp,
            "image_path": img_path,
            "caption": caption
        }
        data = []
        if os.path.exists(self.memory_file):
            try:
                with open(self.memory_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                pass

        data.append(entry)
        if len(data) > 50:
            data = data[-50:]

        with open(self.memory_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def get_recent_visual_memories(self, limit=3):
        if not os.path.exists(self.memory_file):
            return []
        try:
            with open(self.memory_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data[-limit:]
        except Exception:
            return []

    def get_screen_description(self, user_query="Describe what is visible on the screen in detail."):
        base64_img, img_path = self._capture_screen_base64()
        if not base64_img:
            return ""

        payload = {
            "model": self.vlm_model,
            "prompt": user_query,
            "images": [base64_img],
            "stream": False,
            "options": {
                "num_predict": 128
            }
        }

        try:
            print(f"\033[96m[MULTIMODAL: Asking {self.vlm_model} VLM to inspect Monitor {self.active_monitor}...] \033[0m")
            response = requests.post(self.ollama_url, json=payload, timeout=45)
            if response.status_code == 200:
                summary = response.json().get("response", "").strip()
                if not summary:
                    payload["prompt"] = "What windows or text are open on this screen?"
                    resp2 = requests.post(self.ollama_url, json=payload, timeout=45)
                    if resp2.status_code == 200:
                        summary = resp2.json().get("response", "").strip()

                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if summary and _vlm_output_safe(summary):
                    self._save_visual_memory(timestamp, img_path, summary)
                elif summary:
                    print(f"\033[93m[MULTIMODAL: VLM output rejected by "
                          f"secret filter]\033[0m")
                    summary = ""

                print(f"\033[92m[MULTIMODAL VLM Summary Saved]: {summary}\033[0m")
                return summary
        except Exception as e:
            print(f"\033[91mMULTIMODAL VLM Error: Could not connect to Ollama ({e})\033[0m")

        return ""

    def describe_minecraft_window(self):
        """Screenshot just the Minecraft game window and have the VLM
        describe it. Returns (description, note); description is "" when
        nothing was captured and note says why. Blocking — call from an
        executor."""
        if not HAS_MSS:
            return "", "screen capture is unavailable (mss/pillow not installed)"
        win = _find_window(MINECRAFT_TITLE)
        if win is None:
            return "", "no Minecraft game window is open"
        hwnd, bounds, _title, foreground, minimized = win
        if minimized or bounds["width"] < 50 or bounds["height"] < 50:
            return "", "the Minecraft window is minimized"
        try:
            # The window's own pixels, even if it's covered. A screen grab
            # is the fallback, and only safe when the window is in front:
            # otherwise it captures whatever covers it.
            img = _print_window(hwnd)
            if img is None:
                if not foreground:
                    return "", ("the Minecraft window is behind other windows and "
                                "couldn't be captured; bring it to the front")
                with mss.mss() as sct:
                    img = self._grab_monitor(sct, bounds)
        except Exception as e:
            return "", f"capturing the Minecraft window failed: {e}"
        img.thumbnail((800, 800))
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=80)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        img_path = os.path.join(_IMAGES_DIR, f"minecraft_{timestamp}.jpg")
        with open(img_path, "wb") as f:
            f.write(buffer.getvalue())

        payload = {
            "model": self.vlm_model,
            # Neutral on purpose: telling a small VLM "this is Minecraft"
            # makes it describe Minecraft whatever the image shows.
            "prompt": ("Describe what this screenshot shows. If it is a video game, "
                       "describe the scene: terrain, blocks, animals or monsters, "
                       "the item in hand, the health and hunger bars, and any chat "
                       "messages or open menu. Only describe what is visible."),
            "images": [base64.b64encode(buffer.getvalue()).decode("utf-8")],
            "stream": False,
            "options": {"num_predict": 160},
        }
        try:
            resp = requests.post(self.ollama_url, json=payload, timeout=60)
            summary = resp.json().get("response", "").strip() if resp.status_code == 200 else ""
        except Exception as e:
            return "", f"the vision model didn't answer ({e})"
        if not _vlm_output_safe(summary):
            return "", "the vision model gave no usable description"
        self._save_visual_memory(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), img_path, summary)
        return summary, ""

    def _phrase_triggers_vision(self, lowered):
        """True if a conversational vision trigger appears at the START
        of the message. Prevents 'do you see the bug in this code' from
        firing the VLM by requiring the trigger to be the opening phrase."""
        # Strip leading punctuation/whitespace so "hey, can you see..." still
        # matches when the trigger itself is the opening clause.
        stripped = lowered.lstrip(" \t\n.,!?;:\"'")

        for trigger in CONVERSATIONAL_TRIGGERS:
            if stripped.startswith(trigger):
                return True
        return False

    def _recall_triggers_vision(self, lowered):
        """True if the message reads as a visual-history question:
        a recall verb AND a screen-related noun."""
        if not any(kw in lowered for kw in RECALL_TRIGGERS):
            return False
        return any(word in lowered for word in RECALL_SCREEN_WORDS)

    def maybe_capture_from_text(self, text):
        if not text or not self.enabled or not self.API.multimodal_now():
            return False

        lowered = text.lower()

        # Check monitor keywords in speech/text prompt to switch dynamically if requested
        for idx, keywords in MONITOR_KEYWORDS.items():
            if any(kw in lowered for kw in keywords):
                self.active_monitor = idx
                break

        # Visual-history questions: only fire when the message explicitly
        # references something screen-related. Plain "I remember" no longer
        # hijacks the turn.
        if self._recall_triggers_vision(lowered):
            recent_mems = self.get_recent_visual_memories(limit=3)
            if recent_mems:
                history_text = "\n".join([f"- [{m['timestamp']}] {m['caption']}" for m in recent_mems])
                self.prompt_injection.text = (
                    f"\n[CRITICAL DIRECTIVE - VISUAL RECALL ACTIVE]: Anthony is asking about past screen captures. "
                    f"Here is your recorded visual memory log of recent screens:\n{history_text}\n"
                    f"Acknowledge and recall these past visual memories in your character voice to answer him.\n"
                )
                return True

        regex_match = bool(FLEXIBLE_VISION_PATTERN.search(lowered))
        phrase_match = self._phrase_triggers_vision(lowered)

        if not (regex_match or phrase_match):
            return False

        description = self.get_screen_description(
            user_query=f"Briefly state what windows, apps, or text are open on this desktop to answer: {text}"
        )

        if description:
            self.prompt_injection.text = (
                f"\n[CRITICAL DIRECTIVE - SIGHT SENSORS ACTIVE]: You are looking directly at Monitor {self.active_monitor} right now. "
                f"Your visual perception reports EXCLUSIVELY the following: \"{description}\". "
                f"STRICT RULE: You MUST base your answer only on this exact visual data. Do NOT invent or hallucinate "
                f"any system stats, performance data, or metrics not explicitly mentioned. "
                f"Acknowledge what is actually on the screen in your character voice.\n"
            )
            return True

        return False

    def get_prompt_injection(self):
        return self.prompt_injection

    async def run(self):
        pass

    def init_event_loop(self):
        super().init_event_loop()

    class API:
        def __init__(self, outer):
            self.outer = outer

        def set_multimodal_status(self, status):
            self.outer.enabled = status
            self.outer.signals.sio_queue.put(('multimodal_status', status))

        def get_multimodal_status(self):
            return self.outer.enabled

        def multimodal_now(self):
            return self.outer.enabled and MULTIMODAL_STRATEGY != "never"
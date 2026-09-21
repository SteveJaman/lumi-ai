"""
Ambient perception — background screen awareness.

Samples the active screen every SAMPLE_INTERVAL seconds. Computes a
perceptual hash of the frame. Only calls the VLM when either:

  1. The active window title changed (context switch), OR
  2. The screen changed significantly AND at least MIN_VLM_INTERVAL
     seconds have passed since the last VLM call.

The last few descriptions are stored with timestamps so the prompter
and heartbeat can reference "what Anthony is working on" without
triggering new VLM calls.

Cost profile:
  - Idle (user on same window, no big changes): 1 screenshot + 1 hash
    every 30s, no VLM. Essentially free.
  - Active (window switches or big content changes): 1 VLM call,
    rate-limited to at most once per MIN_VLM_INTERVAL seconds.
"""

import asyncio
import base64
import ctypes
import io
import time
import threading

from modules.module import Module
from constants import LLM_ENDPOINT
from modules.log_hub import emit as hub_emit

# ---------- Optional deps ----------
try:
    import mss
    from PIL import Image
    HAS_MSS = True
except ImportError:
    HAS_MSS = False
    print("\033[91mAMBIENT: 'mss' and/or 'pillow' not installed — "
          "background perception disabled.\033[0m")

import requests
from constants import LLM_ENDPOINT


# ===============================================================
# TUNING KNOBS
# ===============================================================

SAMPLE_INTERVAL       = 30       # seconds between screenshots
MIN_VLM_INTERVAL      = 90       # min seconds between VLM calls (same window)
HASH_CHANGE_THRESHOLD = 14       # hamming distance out of 64 = meaningful change
MAX_DESCRIPTION_AGE   = 300      # seconds a cached description stays "fresh"
DESCRIPTION_HISTORY   = 5        # how many recent descriptions to keep

VLM_MODEL = "moondream"
VLM_TIMEOUT = 45
VLM_MAX_TOKENS = 128

VLM_PROMPT = (
    "Briefly state what windows, apps, or text are open on this desktop. "
    "Answer in one short sentence. Do not ask questions."
)

VLM_PROMPT_FALLBACK = (
    "What windows or text are open on this screen?"
)

# ===============================================================
# HELPERS
# ===============================================================

def _get_active_window_title():
    """Read the current foreground window title via Win32."""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value.strip()[:200]
    except Exception:
        return ""


def _average_hash(img, hash_size=8):
    """8x8 average hash. Returns a 64-character bit string."""
    small = img.convert("L").resize((hash_size, hash_size), Image.LANCZOS)
    pixels = list(small.getdata())
    avg = sum(pixels) / len(pixels)
    return "".join("1" if p > avg else "0" for p in pixels)


def _hamming(bits_a, bits_b):
    """Hamming distance between two equal-length bit strings."""
    if not bits_a or not bits_b or len(bits_a) != len(bits_b):
        return 64
    return sum(1 for x, y in zip(bits_a, bits_b) if x != y)


# ===============================================================
# MODULE
# ===============================================================

class AmbientPerception(Module):
    def __init__(
        self,
        signals,
        enabled=True,
        sample_interval=SAMPLE_INTERVAL,
        min_vlm_interval=MIN_VLM_INTERVAL,
        monitor_index=2,
    ):
        super().__init__(signals, enabled)
        self.sample_interval = sample_interval
        self.min_vlm_interval = min_vlm_interval
        self.monitor_index = monitor_index

        self.current_window = ""
        self.last_window = ""
        self.last_hash = ""
        self.last_description = ""
        self.last_analysis_time = 0.0

        # (timestamp, window_title, description) tuples, most recent last.
        self.description_history = []

        self._lock = threading.Lock()

        # Register on signals so prompter + heartbeat can read us.
        signals.ambient = self

    # ---------------------------------------------------------------
    # Capture
    # ---------------------------------------------------------------
    def _grab_screen(self):
        """Returns a PIL image of the target monitor, or None."""
        if not HAS_MSS:
            return None
        try:
            with mss.mss() as sct:
                available = len(sct.monitors) - 1
                idx = self.monitor_index if 1 <= self.monitor_index <= available else 1
                shot = sct.grab(sct.monitors[idx])
                return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        except Exception:
            return None

    # ---------------------------------------------------------------
    # VLM
    # ---------------------------------------------------------------
    def _ask_vlm(self, img):
        """Sends a small screenshot to the VLM, returns description or ''.

        Tries the primary prompt first. If the response is empty, retries
        once with a short fallback prompt. Post-processes the output to
        strip moondream's habit of inventing a second question and
        answering it."""
        try:
            thumb = img.copy()
            thumb.thumbnail((800, 800))
            buffer = io.BytesIO()
            thumb.save(buffer, format="JPEG", quality=80)
            b64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
        except Exception as e:
            print(f"\033[90mAMBIENT: image prep failed — {e}\033[0m")
            return ""

        def _call(prompt_text):
            try:
                payload = {
                    "model": VLM_MODEL,
                    "prompt": prompt_text,
                    "images": [b64],
                    "stream": False,
                    "options": {
                        "num_predict": VLM_MAX_TOKENS,
                        "temperature": 0.2,
                    },
                }
                res = requests.post(
                    f"{LLM_ENDPOINT}/api/generate",
                    json=payload,
                    timeout=VLM_TIMEOUT,
                )
                res.raise_for_status()
                text = res.json().get("response", "").strip()

                # Collapse whitespace
                text = " ".join(text.split())

                # Post-process cleanup: strip moondream's habit of inventing
                # a follow-up question. This runs after the VLM finishes,
                # not via stop tokens (those truncate mid-token and were
                # the cause of namespace URI garbage on dense screens).
                for marker in [" 2.", " Question", " Q:", "? "]:
                    idx = text.find(marker)
                    if idx > 20:
                        text = text[:idx].strip()

                # Reject outputs that look like transcriptions rather than
                # descriptions — URLs, namespaces, or file paths.
                lower = text.lower()
                if (
                    "urn:" in lower
                    or "://" in lower
                    or lower.count(":") > 3
                    or lower.count("/") > 4
                ):
                    return ""

                # Hard cap so a runaway never leaks into the prompt.
                if len(text) > 180:
                    text = text[:180].rstrip(" ,;:") + "…"

                return text
            except Exception as e:
                print(f"\033[90mAMBIENT: VLM error — {e}\033[0m")
                return ""

        text = _call(VLM_PROMPT)
        if not text:
            text = _call(VLM_PROMPT_FALLBACK)
        return text

    # ---------------------------------------------------------------
    # Tick
    # ---------------------------------------------------------------
    def _tick(self):
        window = _get_active_window_title()
        self.current_window = window

        img = self._grab_screen()
        if img is None:
            return

        bits = _average_hash(img)
        now = time.time()

        window_changed = (window != self.last_window) and window
        hash_changed = bool(self.last_hash) and \
            _hamming(bits, self.last_hash) > HASH_CHANGE_THRESHOLD
        time_ok = (now - self.last_analysis_time) >= self.min_vlm_interval

        should_analyze = False
        if window_changed:
            # Context switch — analyze regardless of hash.
            should_analyze = True
        elif hash_changed and time_ok:
            # Same window but big content change.
            should_analyze = True

        # First sample of the session — always analyze once.
        if not self.last_description and not self.last_hash:
            should_analyze = True

        if should_analyze:
            t0 = time.time()
            desc = self._ask_vlm(img)
            elapsed = time.time() - t0
            if desc:
                with self._lock:
                    self.last_description = desc
                    self.last_analysis_time = now
                    self.description_history.append((now, window, desc))
                    self.description_history = self.description_history[-DESCRIPTION_HISTORY:]
                # Hub only — terminal stays clean.
                hub_emit(
                    self.signals, "ambient",
                    f"{window[:60]} → {desc[:120]} ({elapsed:.1f}s)"
                )
            else:
                hub_emit(
                    self.signals, "ambient",
                    f"VLM returned empty after {elapsed:.1f}s "
                    f"(window={window[:60]!r})",
                    level="warn"
                )
        else:
            # Skips are the noisy case — hub only, no terminal spam.
            hub_emit(
                self.signals, "ambient",
                f"skip win_chg={window_changed} hash_chg={hash_changed} "
                f"time_ok={time_ok} win={window[:60]!r}"
            )

        self.last_window = window
        self.last_hash = bits

    # ---------------------------------------------------------------
    # Public API for prompter + heartbeat
    # ---------------------------------------------------------------
    def get_context_line(self, max_age=MAX_DESCRIPTION_AGE):
        """One-line screen context, or '' if nothing recent/fresh."""
        with self._lock:
            if not self.last_description:
                return ""
            age = time.time() - self.last_analysis_time
            if age > max_age:
                return ""
            window = self.current_window or "unknown window"
            if age < 60:
                age_str = f"{int(age)}s ago"
            else:
                age_str = f"{int(age / 60)}m ago"
            return f"Screen ({age_str}): {window} — {self.last_description}"

    def get_recent_history(self, limit=3):
        """Last N descriptions, most recent last."""
        with self._lock:
            return list(self.description_history[-limit:])

    # ---------------------------------------------------------------
    # Main loop
    # ---------------------------------------------------------------
    async def run(self):
        if not self.enabled:
            return
        if not HAS_MSS:
            print("\033[90mAMBIENT: skipping — mss/pillow unavailable.\033[0m")
            return

        hub_emit(
            self.signals, "ambient",
            f"sampler started (interval={self.sample_interval}s, "
            f"min_vlm_gap={self.min_vlm_interval}s)"
        )

        loop = asyncio.get_running_loop()
        # First tick immediately so we have context from the start.
        await loop.run_in_executor(None, self._tick)

        while not self.signals.terminate:
            await asyncio.sleep(self.sample_interval)
            if self.signals.terminate:
                break
            try:
                await loop.run_in_executor(None, self._tick)
            except Exception as e:
                print(f"\033[91mAMBIENT: tick error — {e}\033[0m")
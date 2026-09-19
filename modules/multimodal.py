from modules.module import Module
from constants import MULTIMODAL_STRATEGY
import base64
import io
import re
import requests
import subprocess
import time
import os
import json
from datetime import datetime

try:
    import mss
    from PIL import Image
    HAS_MSS = True
except ImportError:
    HAS_MSS = False
    print("\033[91mMULTIMODAL: 'mss' and/or 'pillow' not installed — screen capture is disabled.\033[0m")

FLEXIBLE_VISION_PATTERN = re.compile(
    r"\b(look|see|check|view|glance|describe|tell|read|showing|open|what)\b.{0,45}\b(screen|monitor|display|desktop|computer)\b",
    re.IGNORECASE,
)

CONVERSATIONAL_TRIGGERS = [
    "what do you see",
    "what are you looking at",
    "can you see",
    "do you see",
    "look at this",
    "take a look"
]

RECALL_TRIGGERS = [
    "remember",
    "earlier",
    "past",
    "previously",
    "did i show you",
    "what was on"
]

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

        # Ensure visual storage directories exist
        os.makedirs("logs/images", exist_ok=True)
        self.memory_file = "logs/visual_memory.json"

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

            # Save raw snapshot to disk
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            img_path = os.path.join("logs", "images", f"screen_{timestamp}.jpg")
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
                if summary:
                    self._save_visual_memory(timestamp, img_path, summary)

                print(f"\033[92m[MULTIMODAL VLM Summary Saved]: {summary}\033[0m")
                return summary
        except Exception as e:
            print(f"\033[91mMULTIMODAL VLM Error: Could not connect to Ollama ({e})\033[0m")

        return ""

    def maybe_capture_from_text(self, text):
        if not text or not self.enabled or not self.API.multimodal_now():
            return False

        lowered = text.lower()

        # Check monitor keywords in speech/text prompt to switch dynamically if requested
        for idx, keywords in MONITOR_KEYWORDS.items():
            if any(kw in lowered for kw in keywords):
                self.active_monitor = idx
                break

        is_recall = any(kw in lowered for kw in RECALL_TRIGGERS)
        if is_recall:
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
        phrase_match = any(trigger in lowered for trigger in CONVERSATIONAL_TRIGGERS)

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
"""
Seeing through the bot's eyes: screenshot bot.js's live viewer
(prismarine-viewer, first person, http://127.0.0.1:3000) with Playwright and
ask the local vision model about it.

Used by the Minecraft agent when it keeps getting stuck ("I am stuck. What
is blocking my path?"), and by its mc_agent_look_around action. A small VLM
gets things wrong now and then; that's part of the charm, and the answer is
treated as a hint, never as fact the agent acts on.

Blocking: call from an executor. Playwright and its Chromium are already
installed for Lumi's browser tool; the viewer needs WebGL, which headless
Chromium renders with SwiftShader.
"""

from __future__ import annotations

import base64
import io
import json
import os
import urllib.request
from typing import Optional, Tuple

# Lumi's vision model (constants.VISION_MODEL_NAME, qwen2.5vl). moondream is
# faster but answers these frames with junk. The model is unloaded right
# after each look so it doesn't push Lumi's chat models out of VRAM; a cold
# load can take a couple of minutes while they're loaded.
try:
    from constants import VISION_MODEL_NAME as _DEFAULT_VISION
except Exception:
    _DEFAULT_VISION = "qwen2.5vl:latest"
VISION_MODEL = os.environ.get("MC_VISION_MODEL", _DEFAULT_VISION)
OLLAMA = os.environ.get("MC_VISION_ENDPOINT", "http://127.0.0.1:11434")
RENDER_WAIT_MS = 3500          # the viewer streams chunks in after load

STUCK_QUESTION = ("This is a first-person view in a blocky video game. The player is stuck "
                  "and can't move on. What is blocking the way? Answer in one or two sentences.")


def viewer_port(bot_api: str, timeout: float = 2.0) -> Optional[int]:
    """The live viewer's port from bot.js's /config, or None when it's off."""
    try:
        with urllib.request.urlopen(f"{bot_api}/config", timeout=timeout) as r:
            return json.loads(r.read()).get("viewer_port") or None
    except Exception:
        return None


def screenshot_viewer(port: int, width: int = 800, height: int = 450) -> bytes:
    """A JPEG of the live viewer. Raises on failure."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=[
            "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"])
        try:
            page = browser.new_page(viewport={"width": width, "height": height})
            page.goto(f"http://127.0.0.1:{port}", wait_until="load", timeout=15_000)
            page.wait_for_timeout(RENDER_WAIT_MS)
            return page.screenshot(type="jpeg", quality=75)
        finally:
            browser.close()


def ask_vision(image: bytes, question: str, model: str = VISION_MODEL,
               timeout: float = 240.0) -> str:
    """The vision model's answer about an image, or "" if it gave none."""
    body = json.dumps({
        "model": model, "prompt": question, "stream": False,
        "images": [base64.b64encode(image).decode("ascii")],
        "options": {"num_predict": 120}, "keep_alive": 0,
    }).encode()
    req = urllib.request.Request(f"{OLLAMA}/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return " ".join(str(json.loads(r.read()).get("response") or "").split())


def _blank(image: bytes) -> bool:
    """A flat, one-colour frame: WebGL didn't draw, or no chunks yet."""
    try:
        from PIL import Image, ImageStat
        return max(ImageStat.Stat(Image.open(io.BytesIO(image)).convert("L")).stddev) < 2
    except Exception:
        return False


def look(bot_api: str, question: str = STUCK_QUESTION) -> Tuple[str, str]:
    """(answer, note): the answer is "" when nothing could be seen, and the
    note says why."""
    port = viewer_port(bot_api)
    if not port:
        return "", "the live viewer is off"
    try:
        image = screenshot_viewer(port)
    except Exception as e:
        return "", f"couldn't screenshot the viewer ({e.__class__.__name__}: {str(e)[:120]})"
    if _blank(image):
        return "", "the viewer rendered nothing (no WebGL, or the world hasn't loaded)"
    try:
        answer = ask_vision(image, question)
    except Exception as e:
        return "", f"the vision model didn't answer ({e})"
    return (answer, "") if answer else ("", "the vision model gave no answer")

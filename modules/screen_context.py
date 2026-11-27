"""
On-demand screen look (Phase 5): capture_screen_context.

Takes one screenshot, only when asked (a tool call that you approve, or an opt-in
check-in). It's downscaled, sent to the local vision model, and only the model's short
description comes back; the image is not kept. No vision model is held in VRAM between
looks: it's loaded for the one call and released after 30 s.

    capture_screen_context(question) -> str     what's on screen, for Lumi
    downscale(image, max_side) -> image
"""

from __future__ import annotations

import base64
import io
from typing import Optional

import requests

import constants

VISION_MODEL = "qwen2.5vl:latest"
MAX_SIDE = 1280
KEEP_ALIVE = "30s"
TIMEOUT_S = 90


def downscale(image, max_side: int = MAX_SIDE):
    w, h = image.size
    scale = min(1.0, max_side / float(max(w, h)))
    if scale >= 1.0:
        return image
    return image.resize((max(1, int(w * scale)), max(1, int(h * scale))))


def grab_jpeg(max_side: int = MAX_SIDE) -> bytes:
    from PIL import ImageGrab
    img = downscale(ImageGrab.grab().convert("RGB"), max_side)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    return buf.getvalue()


def capture_screen_context(question: str = "", grab=grab_jpeg, post=requests.post) -> str:
    jpeg = grab()
    prompt = ("Describe what is on this screen in two or three short sentences: the app, any visible "
              "error text or code problem, and anything notable.")
    if question:
        prompt += f" The user asks: {question[:300]}"
    body = {
        "model": VISION_MODEL,
        "prompt": prompt,
        "images": [base64.b64encode(jpeg).decode("ascii")],
        "stream": False,
        "keep_alive": KEEP_ALIVE,
        "think": False,
        "options": {"num_predict": 200},
    }
    r = post(f"{constants.LLM_ENDPOINT.rstrip('/')}/api/generate", json=body, timeout=TIMEOUT_S)
    r.raise_for_status()
    return (r.json().get("response") or "").strip()

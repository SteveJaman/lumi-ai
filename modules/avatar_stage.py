"""
The VRM avatar for the web Stage (Phase 3).

Lumi's 3D avatar is a VRM file you put in data/avatars/ (the folder is
gitignored, like the rest of data/). Lumi serves it to the web client on this
PC only; it is not on the remote allow-list, so a paired or remote browser
can't fetch it.

    GET /api/avatar/status   {"model": bool, "file": "lumi.vrm"}
    GET /api/avatar/model    the VRM file, if there is one (404 otherwise)

The file name is fixed (lumi.vrm) so nothing from a request chooses a path.
"""

from __future__ import annotations

import json
import os
from typing import Optional

from constants import DATA_ROOT

FOLDER = os.path.join(DATA_ROOT, "avatars")
FILE = "lumi.vrm"
MAX_BYTES = 200 * 1024 * 1024        # a VRM is usually 5-50 MB; refuse anything silly


def model_path() -> str:
    return os.path.join(FOLDER, FILE)


def model_size() -> Optional[int]:
    """Size of the VRM in bytes, or None when there is no usable model."""
    try:
        size = os.path.getsize(model_path())
    except OSError:
        return None
    return size if 0 < size <= MAX_BYTES else None


def status() -> dict:
    return {"model": model_size() is not None, "file": FILE}


# ─── Live2D (Cubism) models ──────────────────────────────────────────
# A model folder you already have (e.g. VTube Studio's Live2DModels\<name>), named in
# data/avatars/live2d.json. It's served from where it is, never copied into the repo.
# The Cubism Core runtime (live2dcubismcore.min.js) is Live2D's own file: you download it
# from Live2D and put it in web/vendor/. Lumi doesn't fetch it.

CORE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "web", "vendor", "live2dcubismcore.min.js")
ALLOWED_EXT = frozenset({".json", ".moc3", ".png", ".jpg", ".jpeg"})


def live2d_folder() -> Optional[str]:
    """The configured model folder, if it exists and holds a *.model3.json."""
    try:
        with open(os.path.join(FOLDER, "live2d.json"), encoding="utf-8") as f:
            folder = str((json.load(f) or {}).get("folder") or "").strip()
    except (OSError, ValueError):
        return None
    if not folder or not os.path.isdir(folder):
        return None
    return folder if live2d_entry(folder) else None


def live2d_entry(folder: str) -> str:
    """The model file the Stage loads: the first *.model3.json in the folder ('' if none)."""
    try:
        names = sorted(n for n in os.listdir(folder) if n.lower().endswith(".model3.json"))
    except OSError:
        return ""
    return names[0] if names else ""


def core_present() -> bool:
    return os.path.isfile(CORE_FILE)


def live2d_status() -> dict:
    folder = live2d_folder()
    return {"model": folder is not None, "entry": live2d_entry(folder) if folder else "",
            "core": core_present()}


def safe_file(folder: str, rel: str) -> Optional[str]:
    """A file inside the model folder, or None: no escaping the folder (symlinks included),
    and only the extensions a Cubism model uses."""
    base = os.path.realpath(folder)
    target = os.path.realpath(os.path.join(base, rel))
    if os.path.commonpath([base, target]) != base or not os.path.isfile(target):
        return None
    if os.path.splitext(target)[1].lower() not in ALLOWED_EXT:
        return None
    return target


def register(router, server) -> None:
    from aiohttp import web
    from modules.socketIO import _json_error

    async def get_status(request):
        if not server._current_user(request):
            return _json_error(401, "Not logged in.")
        return web.json_response(status(), headers={"Cache-Control": "no-store"})

    async def get_model(request):
        if not server._current_user(request):
            return _json_error(401, "Not logged in.")
        if model_size() is None:
            return _json_error(404, "No avatar model yet. Put a .vrm file at data/avatars/lumi.vrm.")
        return web.FileResponse(model_path(), headers={
            "Content-Type": "model/gltf-binary",
            "Cache-Control": "no-store",
        })

    async def get_live2d_status(request):
        if not server._current_user(request):
            return _json_error(401, "Not logged in.")
        return web.json_response(live2d_status(), headers={"Cache-Control": "no-store"})

    async def get_live2d_file(request):
        if not server._current_user(request):
            return _json_error(401, "Not logged in.")
        folder = live2d_folder()
        path = safe_file(folder, request.match_info.get("tail", "")) if folder else None
        if path is None:
            return _json_error(404, "Not found.")
        return web.FileResponse(path, headers={"Cache-Control": "no-store"})

    router.add_get("/api/avatar/status", get_status)       # host only: not on the remote allow-list
    router.add_get("/api/avatar/model", get_model)
    router.add_get("/api/avatar/live2d/status", get_live2d_status)
    router.add_get("/api/avatar/live2d/file/{tail:.*}", get_live2d_file)

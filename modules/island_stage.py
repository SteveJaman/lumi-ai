"""The Island's Stage: the model and the Live2D runtime, sent to the Island over its own socket.

The Island can't read Lumi's web routes (it has no web session), so everything it needs comes
through the Island connection, which is already paired or token-checked:

    manifest()                -> what exists: the model files, the vendor libraries
    read_chunk(key, offset)   -> one base64 slice (under the socket's 64 KB message cap)

Keys are short names, never paths: the Island asks for "model/<rel>" or "vendor/<name>", and
each key maps to a file here through the same checks the web Stage uses (avatar_stage.safe_file,
the extension allow-list). The Cubism Core is read from web/vendor on this PC; it is Live2D's
file and is never committed or bundled. Pixi and pixi-live2d-display come from web/vendor/node_modules.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Optional

from modules import avatar_stage

CHUNK = 36 * 1024            # raw bytes per message: base64 stays under the 64 KB socket cap

_WEB_VENDOR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "vendor")
VENDOR = {
    "core": avatar_stage.CORE_FILE,                                            # Live2D's Cubism Core
    "pixi": os.path.join(_WEB_VENDOR, "node_modules", "pixi.js", "dist", "pixi.min.js"),
    "sound": os.path.join(_WEB_VENDOR, "node_modules", "@pixi", "sound", "dist", "pixi-sound.js"),   # the plugin needs it
    "live2d": os.path.join(_WEB_VENDOR, "node_modules", "pixi-live2d-display-advanced", "dist", "cubism4.min.js"),
}


def _refs(obj) -> list:
    """Every file name a model3.json points at (textures, moc, physics, pose, motions, expressions)."""
    out = []
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            out += _refs(v)
    elif isinstance(obj, list):
        for v in obj:
            out += _refs(v)
    return out


def _model_files(folder: str, entry: str) -> list:
    """The entry file plus everything it references, each one checked by avatar_stage.safe_file."""
    rels = [entry]
    try:
        with open(os.path.join(folder, entry), encoding="utf-8") as f:
            settings = json.load(f)
    except (OSError, ValueError):
        settings = {}
    rels += _refs(settings.get("FileReferences", {}))
    seen, out = set(), []
    for rel in rels:
        rel = str(rel).replace("\\", "/")
        if rel in seen or not rel:
            continue
        seen.add(rel)
        path = avatar_stage.safe_file(folder, rel)
        if path:
            out.append((f"model/{rel}", path))
    return out


def _files() -> dict:
    """key -> real path, for everything the Island may ask for right now."""
    files = {}
    folder = avatar_stage.live2d_folder()
    if folder:
        entry = avatar_stage.live2d_entry(folder)
        for key, path in _model_files(folder, entry):
            files[key] = path
    for name, path in VENDOR.items():
        if os.path.isfile(path):
            files[f"vendor/{name}"] = path
    return files


def manifest() -> dict:
    """What the Island should fetch. Sizes let it show progress and check the download."""
    files = _files()
    entries = []
    for key, path in files.items():
        entries.append({"key": key, "size": os.path.getsize(path)})
    folder = avatar_stage.live2d_folder()
    entry = avatar_stage.live2d_entry(folder) if folder else ""
    return {
        "model": bool(folder),
        "entry": f"model/{entry}" if entry else "",
        "core": avatar_stage.core_present(),
        "files": entries,
    }


def read_chunk(key: str, offset: int = 0) -> Optional[dict]:
    """One slice of a file from manifest(), base64-encoded. None when the key isn't available."""
    path = _files().get(str(key))
    if path is None:
        return None
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        return None
    size = os.path.getsize(path)
    if offset > size:
        return None
    with open(path, "rb") as f:
        f.seek(offset)
        raw = f.read(CHUNK)
    return {
        "key": key,
        "offset": offset,
        "size": size,
        "data": base64.b64encode(raw).decode("ascii"),
        "done": offset + len(raw) >= size,
    }

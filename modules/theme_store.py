"""
The UI theme shared by the web app and every Lumi Island.

Themes live in web/themes/<id>/ (theme.json, style.css, background.js, assets/) and
are copied to island/src/themes/ by scripts/sync_themes.py; web/themes/index.json
lists them. This module only remembers which one is chosen (data/ui_theme.json) and
tells every UI when it changes (ui_sync "theme_update"). An island can follow it
("Sync with Desktop") or keep its own; that choice lives on the island.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, List

from constants import DATA_ROOT, WORKSPACE_ROOT

PATH = os.path.join(DATA_ROOT, "ui_theme.json")
INDEX = os.path.join(WORKSPACE_ROOT, "web", "themes", "index.json")
DEFAULT = "default"
_lock = threading.Lock()


def available() -> List[Dict[str, str]]:
    try:
        with open(INDEX, encoding="utf-8") as f:
            themes = json.load(f).get("themes", [])
        return [t for t in themes if isinstance(t, dict) and t.get("id")]
    except (OSError, ValueError, AttributeError):
        return [{"id": DEFAULT, "name": "Lumi"}]


def current() -> str:
    try:
        with open(PATH, encoding="utf-8") as f:
            tid = json.load(f).get("theme")
    except (OSError, ValueError, AttributeError):
        tid = None
    ids = {t["id"] for t in available()}
    return tid if tid in ids else DEFAULT


def set_theme(theme_id: str, broadcast: bool = True) -> str:
    ids = {t["id"] for t in available()}
    if theme_id not in ids:
        raise ValueError(f"Unknown theme '{theme_id}'. Available: {', '.join(sorted(ids))}.")
    with _lock:
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
        tmp = PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"theme": theme_id, "updated": time.time()}, f)
        os.replace(tmp, PATH)
    if broadcast:
        from modules import ui_sync
        ui_sync.broadcast("theme_update", {"theme": theme_id})
    return theme_id


def state() -> Dict[str, Any]:
    from modules import hybrid_mode
    return {"theme": current(), "themes": available(), "hybrid": hybrid_mode.status()}

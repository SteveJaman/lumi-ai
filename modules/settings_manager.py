"""
Settings backend (Phase 10.14).

Owns three things:
  1. General settings that map onto top-level constants (TIMEZONE,
     LLM_ENDPOINT). Writes go through a line patcher that preserves
     comments and formatting, same pattern as model_manager.
  2. Read-only info for the About tab (version, paths, model names,
     uptime, Python version).
  3. Reset/wipe actions for the Advanced tab. Each target is confined
     to a fixed allowlist — no arbitrary paths from the client.
"""

from __future__ import annotations

import os
import re
import sys
import time
import shutil
import json
import platform
from typing import Any, Dict, List, Optional, Tuple

import requests

import constants
from constants import (
    WORKSPACE_ROOT,
    LLM_ENDPOINT,
    MODEL_NAME,
    FAST_MODEL_NAME,
    FAST_MODEL_ENABLED,
    TIMEZONE,
)


_CONSTANTS_PATH = os.path.join(WORKSPACE_ROOT, "constants.py")
_PROCESS_START = time.time()

# Files/dirs the Advanced tab is allowed to touch. Anything outside
# this allowlist is rejected server-side regardless of what the client
# sends.
_RESET_TARGETS = {
    "history":     "Chat history (logs/chat_history.json)",
    "session":     "Saved session continuity (memories/session_state.json)",
    "self_model":  "Self-model observations (memories/self_model.json)",
    "logs":        "Diagnostics log (logs/events.log)",
    "memory":      "Long-term memory (memories/chroma.db collection)",
}


# ===============================================================
# Constants file patcher
# ===============================================================
def _format_value(name: str, value: Any) -> str:
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, int):
        return str(value)
    if value is None:
        return "None"
    return f'"{value}"'


def _patch_constants(patch: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    try:
        with open(_CONSTANTS_PATH, "r", encoding="utf-8") as f:
            src = f.read()
    except Exception as e:
        return False, f"Could not read constants.py: {e}"

    updated = src
    for name, value in patch.items():
        new_repr = _format_value(name, value)
        pattern = re.compile(
            r"^" + re.escape(name) + r"\s*=\s*(.*?)\s*(#.*)?$",
            re.MULTILINE,
        )

        def _repl(m, _n=name, _v=new_repr):
            comment = m.group(2) or ""
            return f"{_n} = {_v}  {comment}" if comment else f"{_n} = {_v}"

        updated, count = pattern.subn(_repl, updated, count=1)
        if count == 0:
            return False, f"Could not find `{name} = ...` in constants.py"

    try:
        compile(updated, _CONSTANTS_PATH, "exec")
    except SyntaxError as e:
        return False, f"Patched constants.py would not compile: {e}"

    try:
        shutil.copy2(_CONSTANTS_PATH, _CONSTANTS_PATH + ".bak")
    except Exception:
        pass

    try:
        with open(_CONSTANTS_PATH, "w", encoding="utf-8") as f:
            f.write(updated)
    except Exception as e:
        return False, f"Could not write constants.py: {e}"

    # Live rebind so the change is effective on the next turn.
    for name, value in patch.items():
        setattr(constants, name, value)
    _rebind_consumers(patch)

    return True, None


_CONSUMERS = {
    "TIMEZONE": ["modules.prompter", "modules.ambient_perception",
                 "modules.heartbeat"],
    "LLM_ENDPOINT": ["modules.prompter", "modules.memory",
                     "modules.session_state", "modules.self_model",
                     "modules.heartbeat", "signals",
                     "modules.model_manager", "modules.settings_manager"],
}


def _rebind_consumers(patch: Dict[str, Any]):
    for name, value in patch.items():
        for mod_name in _CONSUMERS.get(name, []):
            mod = sys.modules.get(mod_name)
            if mod is not None:
                try:
                    setattr(mod, name, value)
                except Exception:
                    pass


# ===============================================================
# General
# ===============================================================
def get_general() -> Dict[str, Any]:
    return {
        "workspace_root": WORKSPACE_ROOT,
        "timezone": TIMEZONE,
        "llm_endpoint": LLM_ENDPOINT,
        "model_name": MODEL_NAME,
        "fast_model_name": FAST_MODEL_NAME,
        "fast_model_enabled": bool(FAST_MODEL_ENABLED),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
    }


def save_general(values: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    if not isinstance(values, dict):
        return False, "Expected an object of field values."

    patch: Dict[str, Any] = {}

    if "timezone" in values:
        tz = str(values["timezone"] or "").strip()
        if not tz:
            return False, "Timezone cannot be empty."
        # Cheap validation: try to load it via zoneinfo.
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(tz)
        except Exception as e:
            return False, f"Unknown timezone '{tz}': {e}"
        if tz != TIMEZONE:
            patch["TIMEZONE"] = tz

    if "llm_endpoint" in values:
        url = str(values["llm_endpoint"] or "").strip()
        if not url:
            return False, "LLM endpoint cannot be empty."
        if not re.match(r"^https?://", url):
            return False, "LLM endpoint must start with http:// or https://"
        url = url.rstrip("/")
        if url != LLM_ENDPOINT:
            patch["LLM_ENDPOINT"] = url

    if not patch:
        return True, None

    return _patch_constants(patch)


# ===============================================================
# About
# ===============================================================
def get_about(modules, signals) -> Dict[str, Any]:
    uptime = time.time() - _PROCESS_START
    hours = int(uptime // 3600)
    minutes = int((uptime % 3600) // 60)

    # Model info from the loaded modules list
    module_status = []
    for m in (modules or []):
        name = m.__class__.__name__
        module_status.append({
            "name": name,
            "enabled": bool(getattr(m, "enabled", True)),
        })

    version = getattr(constants, "LUMI_VERSION", "0.10.14")

    return {
        "version": version,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "workspace_root": WORKSPACE_ROOT,
        "llm_endpoint": LLM_ENDPOINT,
        "model_name": MODEL_NAME,
        "fast_model_name": FAST_MODEL_NAME,
        "fast_model_enabled": bool(FAST_MODEL_ENABLED),
        "uptime_seconds": int(uptime),
        "uptime_human": f"{hours}h {minutes}m" if hours else f"{minutes}m",
        "modules": sorted(module_status, key=lambda x: x["name"]),
        "log_hub_port": getattr(getattr(signals, "log_hub", None), "port", 5001),
    }


# ===============================================================
# Advanced — resets
# ===============================================================
def list_reset_targets() -> List[Dict[str, Any]]:
    """Expose the allowlist to the UI so it can render the buttons."""
    return [
        {"id": k, "label": v, "dangerous": k in ("memory",)}
        for k, v in _RESET_TARGETS.items()
    ]


def reset_target(target: str, modules, signals) -> Tuple[bool, str]:
    if target not in _RESET_TARGETS:
        return False, f"Unknown reset target: {target}"

    try:
        if target == "history":
            return _reset_history(signals)
        if target == "session":
            return _reset_file("memories/session_state.json",
                               "Session state cleared.")
        if target == "self_model":
            return _reset_file("memories/self_model.json",
                               "Self-model observations cleared.")
        if target == "logs":
            return _reset_file("logs/events.log", "Log file cleared.")
        if target == "memory":
            return _reset_memory(modules)
    except Exception as e:
        return False, str(e)

    return False, "No handler."


def _reset_history(signals) -> Tuple[bool, str]:
    try:
        signals.history = []
        if hasattr(signals, "save_history"):
            signals.save_history()
        return True, "Chat history cleared."
    except Exception as e:
        return False, f"Failed to clear history: {e}"


def _reset_file(rel_path: str, success_msg: str) -> Tuple[bool, str]:
    path = os.path.join(WORKSPACE_ROOT, rel_path)
    try:
        if os.path.exists(path):
            os.remove(path)
        return True, success_msg
    except Exception as e:
        return False, f"Failed to remove {rel_path}: {e}"


def _reset_memory(modules) -> Tuple[bool, str]:
    mem = next(
        (m for m in (modules or []) if m.__class__.__name__ == "Memory"),
        None,
    )
    if mem is None:
        return False, "Memory module is not loaded."
    try:
        mem.clear_database()
        return True, "Long-term memory wiped."
    except Exception as e:
        return False, f"Memory wipe failed: {e}"
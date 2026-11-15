"""
Gaming Mode: keep Lumi light while a AAA game is running.

  - constants.GAMING_MODE is the live flag. Read it as
    `constants.GAMING_MODE` (never `from constants import GAMING_MODE`,
    which copies the value at import time and misses toggles).
  - keep_alive(default) gives the Ollama keep_alive for a chat call: 0
    in gaming mode (unload right after the reply), `default` otherwise.
  - set_enabled() flips the flag, saves it to data/gaming_mode.json,
    unloads whatever Ollama has in VRAM, and pushes the new state to SDK
    clients (the Minecraft agent sleeps on it; see plugin/minecraft_bot.py).
  - wake_game_agents() nudges a sleeping agent when the user sends it an
    order through the API.
"""

import json
import os
import threading

import constants
from modules.log_hub import emit as hub_emit

STATE_PATH = os.path.join(constants.DATA_ROOT, "gaming_mode.json")

_lock = threading.Lock()


def load_persisted() -> bool:
    """On boot: a saved setting wins over the constants.py default."""
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            constants.GAMING_MODE = bool(json.load(f).get("enabled", False))
    except (OSError, ValueError):
        pass
    return constants.GAMING_MODE


def is_enabled() -> bool:
    return bool(constants.GAMING_MODE)


def keep_alive(default):
    return 0 if constants.GAMING_MODE else default


def set_enabled(enabled: bool, signals=None) -> dict:
    """Blocking (Ollama calls); run it in an executor from async code."""
    enabled = bool(enabled)
    with _lock:
        constants.GAMING_MODE = enabled
        try:
            os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
            tmp = STATE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"enabled": enabled}, f)
            os.replace(tmp, STATE_PATH)
        except OSError as e:
            print(f"[gaming mode] could not save the setting: {e}")

    unloaded = _unload_all() if enabled else []
    _push_to_sdk(signals, enabled, wake=not enabled)
    try:
        from modules import hitl_server
        hitl_server.broadcast_threadsafe({"type": "lumi_mode", "gaming": enabled})
    except Exception:
        pass
    if signals is not None:
        hub_emit(signals, "system",
                 f"gaming mode {'ON' if enabled else 'OFF'}"
                 + (f" (unloaded: {', '.join(unloaded)})" if unloaded else ""))
    return {"enabled": enabled, "unloaded": unloaded}


def wake_game_agents(signals) -> None:
    """An order arrived through the API: let a sleeping agent take it."""
    if constants.GAMING_MODE:
        _push_to_sdk(signals, True, wake=True)


def _unload_all() -> list:
    """Drop every model Ollama has loaded right now; without this they'd
    sit in VRAM until their old keep_alive (up to 30 min) ran out."""
    try:
        from modules.model_manager import ModelManager
        mm = ModelManager()
        names = [m["name"] for m in mm.list_loaded().get("models", []) if m.get("name")]
        return [n for n in names if mm.unload(n)[0]]
    except Exception as e:
        print(f"[gaming mode] could not unload models: {e}")
        return []


def _push_to_sdk(signals, enabled: bool, wake: bool) -> None:
    sdk = getattr(signals, "sdk", None) if signals is not None else None
    if sdk is not None and hasattr(sdk, "broadcast_gaming_mode"):
        sdk.broadcast_gaming_mode(enabled, wake=wake)

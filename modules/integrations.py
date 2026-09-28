"""
Integrations panel backend (Phase 10.13).

Centralizes status probing and configuration for the external services
Lumi talks to:

    ollama        — local LLM runtime
    vtube_studio  — Live2D avatar control
    discord       — bot + voice bridge
    fish_audio    — TTS engine
    browser       — Playwright Firefox

Each integration exposes:
  - probe(modules, signals) -> (state, message)
      Fast, side-effect-free. Called on every list request.
      state ∈ {"ok", "warn", "err", "off"}
  - test(modules, signals) -> (ok, message)
      Network / process activity allowed. Only fires when the user
      clicks "Test" in the UI.
  - fields: list of env-backed config knobs
  - toggle: module-enable switch (currently only VTS)

Config is written to .env using the same helper approach as
modules.llm_providers. Values are never returned to the UI unmasked —
mask_key() keeps the last 4 chars so users can confirm what's loaded.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from constants import LLM_ENDPOINT, WORKSPACE_ROOT


_ENV_PATH = os.path.join(WORKSPACE_ROOT, ".env")


# ===============================================================
# .env helpers (mirrors modules.llm_providers)
# ===============================================================
def _read_env() -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not os.path.exists(_ENV_PATH):
        return out
    try:
        with open(_ENV_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line or line.lstrip().startswith("#"):
                    continue
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return out


def _write_env_var(name: str, value: Optional[str]) -> bool:
    """Upsert (value=str) or remove (value=None) a key in .env.
    Preserves comments and ordering. Restricts perms on POSIX."""
    lines: List[str] = []
    if os.path.exists(_ENV_PATH):
        try:
            with open(_ENV_PATH, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        except Exception:
            lines = []

    pattern = re.compile(r"^\s*" + re.escape(name) + r"\s*=")
    new_line = f"{name}={value}" if value is not None else None
    replaced = False
    out: List[str] = []

    for line in lines:
        if pattern.match(line):
            if not replaced:
                if new_line is not None:
                    out.append(new_line)
                replaced = True
            # else drop the line
        else:
            out.append(line)

    if not replaced and new_line is not None:
        out.append(new_line)

    text = "\n".join(out)
    if text and not text.endswith("\n"):
        text += "\n"

    try:
        with open(_ENV_PATH, "w", encoding="utf-8") as f:
            f.write(text)
        try:
            os.chmod(_ENV_PATH, 0o600)
        except Exception:
            pass
        return True
    except Exception:
        return False


def _mask(value: Optional[str]) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "•" * len(value)
    return f"{value[:4]}…{value[-4:]}"


# ===============================================================
# Ollama
# ===============================================================
def _probe_ollama(modules, signals) -> Tuple[str, str]:
    try:
        r = requests.get(f"{LLM_ENDPOINT}/api/tags", timeout=3)
        if r.status_code == 200:
            n = len(r.json().get("models", []) or [])
            return "ok", f"Reachable — {n} model(s) installed"
        return "warn", f"HTTP {r.status_code}"
    except Exception as e:
        return "off", f"Not reachable — {e}"


def _test_ollama(modules, signals) -> Tuple[bool, str]:
    try:
        r = requests.get(f"{LLM_ENDPOINT}/api/tags", timeout=5)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        models = r.json().get("models", []) or []
        names = ", ".join((m.get("name") or "")[:40] for m in models[:5])
        suffix = "…" if len(models) > 5 else ""
        if not models:
            return True, "Reachable — 0 models installed."
        return True, f"{len(models)} model(s): {names}{suffix}"
    except Exception as e:
        return False, str(e)


# ===============================================================
# VTube Studio
# ===============================================================
def _probe_vts(modules, signals) -> Tuple[str, str]:
    mod = (modules or {}).get("vtube_studio")
    if mod is None:
        return "off", "Module not loaded"
    if not getattr(mod, "enabled", False):
        return "off", "Module disabled"
    if getattr(signals, "vts_ready", False):
        try:
            n = len(mod.API.list_expression_states())
            return "ok", f"Connected — {n} expression states"
        except Exception:
            return "ok", "Connected"
    return "warn", "Waiting for connection"


def _test_vts(modules, signals) -> Tuple[bool, str]:
    mod = (modules or {}).get("vtube_studio")
    if mod is None:
        return False, "VTube Studio module is not loaded."
    if not getattr(mod, "enabled", False):
        return False, "Module is disabled. Enable it on the card, then retry."
    if not getattr(signals, "vts_ready", False):
        return False, (
            "Module is enabled but not connected. Check that VTube Studio "
            "is running, the API is enabled (Settings → API), and the Lumi "
            "plugin is authorized."
        )
    try:
        states = mod.API.list_expression_states()
        return True, f"Connected. {len(states)} expression states available."
    except Exception as e:
        return False, f"Module reports connected but probe failed: {e}"


# ===============================================================
# Discord
# ===============================================================
def _probe_discord(modules, signals) -> Tuple[str, str]:
    env = _read_env()
    token = env.get("DISCORD_BOT_TOKEN")
    if not token:
        return "off", "Bot token not set"
    voice = env.get("DISCORD_VOICE_CHANNEL_ID")
    text = env.get("DISCORD_CHANNEL_ID")
    parts = []
    if voice:
        parts.append(f"voice:{voice[:6]}…")
    if text:
        parts.append(f"text:{text[:6]}…")
    detail = f" · {', '.join(parts)}" if parts else " · no channels set"
    return "ok", f"Token configured{detail}"


def _test_discord(modules, signals) -> Tuple[bool, str]:
    env = _read_env()
    token = env.get("DISCORD_BOT_TOKEN")
    if not token:
        return False, "Bot token is not set."
    try:
        r = requests.get(
            "https://discord.com/api/v10/users/@me",
            headers={"Authorization": f"Bot {token}"},
            timeout=10,
        )
        if r.status_code == 200:
            d = r.json()
            tag = d.get("username", "?")
            disc = d.get("discriminator")
            if disc and disc != "0":
                tag += f"#{disc}"
            return True, f"Bot token valid — authenticated as {tag}"
        if r.status_code == 401:
            return False, "Bot token rejected by Discord (401)."
        return False, f"HTTP {r.status_code}: {r.text[:120]}"
    except Exception as e:
        return False, str(e)


# ===============================================================
# Fish Audio
# ===============================================================
def _probe_fish(modules, signals) -> Tuple[str, str]:
    env = _read_env()
    key = env.get("FISH_API_KEY")
    if not key:
        return "off", "API key not set"
    ref = env.get("FISH_REFERENCE_ID")
    ref_str = f" · voice {_mask(ref)}" if ref else " · no voice set"
    return "ok", f"Key configured{ref_str}"


def _test_fish(modules, signals) -> Tuple[bool, str]:
    env = _read_env()
    key = env.get("FISH_API_KEY")
    if not key:
        return False, "API key is not set."
    # Fish Audio exposes /v1/voices for listing public voices.
    # A 200 confirms the key is valid; 401/403 tells us it's rejected.
    try:
        r = requests.get(
            "https://api.fish.audio/v1/voices",
            headers={"Authorization": f"Bearer {key}"},
            timeout=10,
        )
        if r.status_code == 200:
            try:
                data = r.json()
                if isinstance(data, dict):
                    items = data.get("items") or data.get("voices") or []
                elif isinstance(data, list):
                    items = data
                else:
                    items = []
                n = len(items)
            except Exception:
                n = 0
            return True, f"API key accepted — {n} voice(s) visible."
        if r.status_code == 401:
            return False, "API key rejected (401)."
        if r.status_code == 403:
            return False, "API key forbidden (403)."
        return False, f"HTTP {r.status_code}: {r.text[:120]}"
    except Exception as e:
        return False, str(e)


# ===============================================================
# Browser (Playwright Firefox)
# ===============================================================
def _firefox_install_dir():
    """Return the ms-playwright cache dir for this platform, or None."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or ""
        if base:
            return os.path.join(base, "ms-playwright")
    else:
        return os.path.join(os.path.expanduser("~"), ".cache", "ms-playwright")
    return None


def _probe_browser(modules, signals) -> Tuple[str, str]:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return "off", "playwright not installed"

    d = _firefox_install_dir()
    if d and os.path.isdir(d):
        for name in sorted(os.listdir(d)):
            if name.startswith("firefox-"):
                return "ok", f"Firefox available ({name})"
    return "warn", "Firefox binary not found — run: playwright install firefox"


def _test_browser(modules, signals) -> Tuple[bool, str]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False, "playwright is not installed."
    try:
        with sync_playwright() as p:
            browser = p.firefox.launch(headless=True, timeout=8000)
            page = browser.new_page()
            page.goto("about:blank", timeout=5000)
            browser.close()
        return True, "Playwright launched Firefox and loaded a blank page."
    except Exception as e:
        msg = str(e).splitlines()[0][:200]
        return False, f"Launch failed: {msg}"


# ===============================================================
# Registry
# ===============================================================
INTEGRATIONS: List[Dict[str, Any]] = [
    {
        "id": "ollama",
        "label": "Ollama",
        "emoji": "🧠",
        "description": (
            "Local LLM runtime. Powers chat, agent turns, memory "
            "reflection, and the heartbeat."
        ),
        "docs": "https://ollama.com/download",
        "probe": _probe_ollama,
        "test": _test_ollama,
        "fields": [],
        "toggle": None,
    },
    {
        "id": "vtube_studio",
        "label": "VTube Studio",
        "emoji": "🎭",
        "description": (
            "Live2D avatar control. Drives expressions, appearance "
            "toggles, and mouth-sync from TTS output."
        ),
        "docs": "https://denchisoft.com/",
        "probe": _probe_vts,
        "test": _test_vts,
        "fields": [],
        "toggle": "vts",
    },
    {
        "id": "discord",
        "label": "Discord",
        "emoji": "💬",
        "description": (
            "Bot reads text channels and joins voice. Needs a bot "
            "token plus the numeric IDs of the channels it should join."
        ),
        "docs": "https://discord.com/developers/applications",
        "probe": _probe_discord,
        "test": _test_discord,
        "fields": [
            {
                "key": "DISCORD_BOT_TOKEN",
                "label": "Bot token",
                "secret": True,
                "placeholder": "MTIzNDU2…",
            },
            {
                "key": "DISCORD_CHANNEL_ID",
                "label": "Text channel ID",
                "secret": False,
                "placeholder": "000000000000000000",
            },
            {
                "key": "DISCORD_VOICE_CHANNEL_ID",
                "label": "Voice channel ID",
                "secret": False,
                "placeholder": "000000000000000000",
            },
        ],
        "toggle": None,
    },
    {
        "id": "fish_audio",
        "label": "Fish Audio",
        "emoji": "🔊",
        "description": (
            "Text-to-speech engine. Turns Lumi's replies into audio "
            "with inline emotion tags as delivery instructions."
        ),
        "docs": "https://fish.audio/go-api/",
        "probe": _probe_fish,
        "test": _test_fish,
        "fields": [
            {
                "key": "FISH_API_KEY",
                "label": "API key",
                "secret": True,
                "placeholder": "sk-…",
            },
            {
                "key": "FISH_REFERENCE_ID",
                "label": "Voice reference ID",
                "secret": False,
                "placeholder": "voice model ID from fish.audio",
            },
        ],
        "toggle": None,
    },
    {
        "id": "browser",
        "label": "Browser",
        "emoji": "🌐",
        "description": (
            "Playwright Firefox instance. Lets Lumi open and read "
            "web pages on demand via the browse_url tool."
        ),
        "docs": "https://playwright.dev/python/",
        "probe": _probe_browser,
        "test": _test_browser,
        "fields": [],
        "toggle": None,
    },
]


# ===============================================================
# Public API
# ===============================================================
def _find_spec(integration_id: str) -> Optional[Dict[str, Any]]:
    return next(
        (s for s in INTEGRATIONS if s["id"] == integration_id),
        None,
    )


def list_integrations(modules, signals) -> List[Dict[str, Any]]:
    """Return the UI-facing list of integrations with current state."""
    env = _read_env()
    out: List[Dict[str, Any]] = []

    for spec in INTEGRATIONS:
        # Probe
        try:
            state, message = spec["probe"](modules, signals)
        except Exception as e:
            state, message = "err", str(e)

        # Config fields, masked
        fields_ui: List[Dict[str, Any]] = []
        for f in spec["fields"]:
            raw = env.get(f["key"])
            is_set = bool(raw)
            if f["secret"]:
                display = _mask(raw) if is_set else ""
            else:
                display = raw or ""
            fields_ui.append({
                "key": f["key"],
                "label": f["label"],
                "secret": bool(f["secret"]),
                "placeholder": f.get("placeholder", ""),
                "is_set": is_set,
                "masked": display,
            })

        # Toggle state (VTS only for now)
        toggle_on: Optional[bool] = None
        if spec["toggle"] == "vts":
            mod = (modules or {}).get("vtube_studio")
            toggle_on = bool(mod and getattr(mod, "enabled", False))

        out.append({
            "id": spec["id"],
            "label": spec["label"],
            "emoji": spec["emoji"],
            "description": spec["description"],
            "docs": spec["docs"],
            "state": state,
            "message": message,
            "fields": fields_ui,
            "has_toggle": spec["toggle"] is not None,
            "toggle_on": toggle_on,
        })

    return out


def save_integration_config(integration_id: str,
                            values: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """Write field values to .env. Empty string clears a key."""
    spec = _find_spec(integration_id)
    if spec is None:
        return False, f"Unknown integration: {integration_id}"

    if not isinstance(values, dict):
        return False, "Expected an object of field values."

    valid_keys = {f["key"] for f in spec["fields"]}
    try:
        for key, raw in values.items():
            if key not in valid_keys:
                continue
            if raw is None:
                continue
            text = str(raw).strip()
            if text == "":
                _write_env_var(key, None)
            else:
                _write_env_var(key, text)
        return True, None
    except Exception as e:
        return False, str(e)


def test_integration(integration_id: str, modules, signals) -> Tuple[bool, str]:
    spec = _find_spec(integration_id)
    if spec is None:
        return False, f"Unknown integration: {integration_id}"
    try:
        return spec["test"](modules, signals)
    except Exception as e:
        return False, str(e)


def toggle_integration(integration_id: str, enabled: bool,
                       modules, signals) -> Tuple[bool, str]:
    """Enable or disable the module that backs this integration.
    Only VTS supports this today — the module has a live enable flag."""
    spec = _find_spec(integration_id)
    if spec is None:
        return False, f"Unknown integration: {integration_id}"

    if spec["toggle"] == "vts":
        mod = (modules or {}).get("vtube_studio")
        if mod is None:
            return False, "VTube Studio module is not loaded."
        try:
            mod.API.set_movement_status(bool(enabled))
            return True, ("VTube Studio enabled."
                          if enabled else "VTube Studio disabled.")
        except Exception as e:
            return False, str(e)

    return False, "This integration does not have a toggle."
"""
Hybrid AI routing: 100% local, or local with cloud escalation for hard prompts.

    enabled()                 the switch (persisted in data/hybrid_mode.json)
    set_enabled(on)           flip it; every UI hears hybrid_mode_update
    model_for(tier, local)    the model a turn should run on
    claude_code_allowed()     Claude Code only runs with escalation on
    status()                  what the UIs show (switch, badge, cloud readiness)

Off (the default, constants.HYBRID_MODE): every tier stays on Ollama, exactly as
before: haiku on FAST_MODEL_NAME, sonnet and opus on MODEL_NAME (chat) or
AGENT_MODEL_NAME (agent turns). Nothing leaves the machine through a model.

On: opus-tier prompts (hard problems, as model_router sorts them) run on
CLOUD_ESCALATION_MODEL; haiku and sonnet stay local. If that model's provider has no
API key, Lumi stays local and the UI says why. Claude Code (which runs on your Claude
plan) is only offered while this is on.

The badge reads "100% Local Privacy" only when it's true: escalation off AND every
model Lumi is configured to use is an Ollama model.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, Optional

import constants
from constants import DATA_ROOT

PATH = os.path.join(DATA_ROOT, "hybrid_mode.json")
_lock = threading.Lock()
_state: Dict[str, Any] = {"loaded": False, "enabled": bool(getattr(constants, "HYBRID_MODE", False))}


def _load() -> None:
    if _state["loaded"]:
        return
    _state["loaded"] = True
    try:
        with open(PATH, encoding="utf-8") as f:
            _state["enabled"] = bool(json.load(f).get("enabled"))
    except (OSError, ValueError, AttributeError):
        pass


def enabled() -> bool:
    with _lock:
        _load()
        return _state["enabled"]


def set_enabled(on: bool, broadcast: bool = True) -> Dict[str, Any]:
    with _lock:
        _load()
        _state["enabled"] = bool(on)
        try:
            os.makedirs(os.path.dirname(PATH), exist_ok=True)
            tmp = PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"enabled": _state["enabled"], "updated": time.time()}, f)
            os.replace(tmp, PATH)
        except OSError:
            pass
    st = status()
    if broadcast:
        from modules import ui_sync
        ui_sync.broadcast("hybrid_mode_update", st)
    return st


def cloud_model() -> str:
    return getattr(constants, "CLOUD_ESCALATION_MODEL", "") or ""


def _is_local(spec: Optional[str]) -> bool:
    from modules.llm_providers import parse_model_spec
    return not spec or parse_model_spec(spec)[0] == "ollama"


def cloud_ready() -> Optional[str]:
    """None when the cloud model can be used, else why not."""
    spec = cloud_model()
    if not spec:
        return "No CLOUD_ESCALATION_MODEL is set in constants.py."
    if _is_local(spec):
        return f"CLOUD_ESCALATION_MODEL ({spec}) is a local model."
    try:
        from modules.llm_providers import get_client, parse_model_spec
        provider = parse_model_spec(spec)[0]
        if not get_client(provider).is_configured():
            return f"No API key for {provider}. Add one in Settings → Models → Providers."
        if provider == "anthropic":
            # A key whose account the API refused (no credit, revoked) is no better than none.
            from modules import claude_router
            ok, why = claude_router.api_status()
            if not ok:
                return f"{why}. Hard tasks are offered to Claude Code instead."
    except Exception as e:
        return f"Cloud provider unavailable ({type(e).__name__})."
    return None


def model_for(tier: Optional[str], local_model: str) -> str:
    """The model a turn of this tier runs on."""
    if tier == "opus" and enabled() and cloud_ready() is None:
        return cloud_model()
    return local_model


def escalation_blocked(tier: Optional[str]) -> Optional[str]:
    """Why a hard (opus) turn that would escalate to Claude can't, or None.
    Only for an Anthropic cloud model with escalation on; the prompter then
    offers the task to Claude Code (your Claude login) with your approval."""
    if tier != "opus" or not enabled():
        return None
    try:
        from modules.llm_providers import parse_model_spec
        if parse_model_spec(cloud_model())[0] != "anthropic":
            return None
    except Exception:
        return None
    return cloud_ready()


def claude_code_allowed() -> bool:
    return enabled()


def fully_local() -> bool:
    if enabled():
        return False
    specs = [getattr(constants, k, None) for k in ("MODEL_NAME", "FAST_MODEL_NAME", "AGENT_MODEL_NAME",
                                                    "VISION_MODEL_NAME")]
    return all(_is_local(s) for s in specs)


def status() -> Dict[str, Any]:
    on = enabled()
    why = cloud_ready() if on else None
    local = fully_local()
    return {
        "enabled": on,
        "cloud_model": cloud_model(),
        "cloud_ready": on and why is None,
        "cloud_problem": why,
        "fully_local": local,
        "badge": "100% Local Privacy" if local else
                 ("Cloud Escalation on" if on else "Cloud models configured"),
    }

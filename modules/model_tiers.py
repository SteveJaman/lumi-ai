"""
Local model tiers for Phase 6: a fast model for voice and chat, and a deeper one for
reasoning, vision and background work. The deep tier is preferred when Ollama has it,
and falls back to the fast model when it doesn't, so nothing breaks on a machine
without it.

    pick_deep(installed, preferred=DEEP_PREFERRED, fallback=FAST) -> model name
    deep_options(num_predict=DEEP_MAX_TOKENS) -> the options a deep call uses
    FAST_KEEP_ALIVE / DEEP_KEEP_ALIVE          how long Ollama keeps each model loaded

The 11 GB card can't hold a large deep model beside Whisper and Fish Audio, so the
deep model is only asked for while it's installed, and it's unloaded soon after
(short keep_alive, capped output). Installed names are matched without the tag
(":latest" is ignored).
"""

from __future__ import annotations

from typing import Iterable, Optional

FAST = "qwen3:8b"
DEEP_PREFERRED = "inkling-2-mini"
FAST_KEEP_ALIVE = "10m"
DEEP_KEEP_ALIVE = "30s"
DEEP_MAX_TOKENS = 1024          # a cap, so a looping deep model can't run on forever


def _base(name: str) -> str:
    n = (name or "").strip().lower()
    return n[:-len(":latest")] if n.endswith(":latest") else n


def pick_deep(installed: Iterable[str], preferred: str = DEEP_PREFERRED, fallback: str = FAST) -> str:
    have = {_base(n) for n in installed or []}
    return preferred if _base(preferred) in have else fallback


def has_deep(installed: Iterable[str], preferred: str = DEEP_PREFERRED) -> bool:
    return _base(preferred) in {_base(n) for n in installed or []}


def installed_names(timeout: float = 3.0) -> list:
    """Model names Ollama has installed. Empty when Ollama can't be reached: the caller then
    keeps the normal model (no deep tier) rather than failing."""
    try:
        import requests
        from constants import LLM_ENDPOINT
        r = requests.get(f"{LLM_ENDPOINT}/api/tags", timeout=timeout)
        if r.status_code != 200:
            return []
        return [m.get("name") or "" for m in r.json().get("models", []) or []]
    except Exception:
        return []


def deep_options(num_predict: Optional[int] = None) -> dict:
    return {"num_predict": int(num_predict or DEEP_MAX_TOKENS)}

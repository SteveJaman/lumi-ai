"""
The deep model tier: a bigger model for the reasoning-heavy step of an agent run (the
harness Plan subagent, which builds the task DAG).

The model chain comes from LUMI.md (`deep_models`, read by modules/harness/config.py),
never from this file. The first installed model in the chain wins; with none installed
the caller keeps the turn's own model, so nothing breaks on a machine without one.

    first_installed(chain, installed) -> the model to use, or None
    installed_names()                 -> what Ollama has installed ([] when unreachable)
    deep_options(num_ctx=...)         -> the options a deep call uses

The 11 GB card can't hold a large model beside Whisper and Fish Audio for long, so a
deep call is unloaded soon after (short keep_alive) and its output is capped.
Installed names are matched without the ":latest" tag.
"""

from __future__ import annotations

from typing import Iterable, Optional

DEEP_KEEP_ALIVE = "30s"
DEEP_MAX_TOKENS = 1024          # a cap, so a looping deep model can't run on forever


def _base(name: str) -> str:
    n = (name or "").strip().lower()
    return n[:-len(":latest")] if n.endswith(":latest") else n


def first_installed(chain: Iterable[str], installed: Iterable[str]) -> Optional[str]:
    """The first model in `chain` that Ollama has installed, else None."""
    have = {_base(n) for n in installed or []}
    for name in chain or []:
        if _base(name) in have:
            return name
    return None


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


def deep_options(num_predict: Optional[int] = None, num_ctx: Optional[int] = None) -> dict:
    opts = {"num_predict": int(num_predict or DEEP_MAX_TOKENS)}
    if num_ctx:
        opts["num_ctx"] = int(num_ctx)
    return opts

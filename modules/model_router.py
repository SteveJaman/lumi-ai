"""
Model router: sort a prompt into a tier before Lumi answers it.

    route_model(prompt) -> "haiku" | "sonnet" | "opus"

    haiku   simple chat, greetings, small talk
    sonnet  writing code, editing files, ordinary requests      (the default)
    opus    hard problems: architecture, debugging, deep analysis

A small local Ollama model (ROUTER_MODEL, qwen2.5:3b by default) does the
sorting. It's the same model the Minecraft agent uses, so it is often already
loaded. Obvious small talk is recognised without asking it, and anything that
goes wrong (Ollama down, model missing, slow, nonsense answer) falls back to
"sonnet". In Gaming Mode the model isn't asked at all, so nothing extra is
loaded into VRAM while you play.

What a tier RUNS (see TIER_INFO): Lumi's chat is local, so haiku means her fast
local model and sonnet / opus her main local model. The names matter for real
when she delegates to Claude Code: that run uses `--model haiku|sonnet|opus`
on your Claude plan. The Pipeline view shows both the tier and the real model.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Dict, Optional

import requests

import constants

TIERS = ("haiku", "sonnet", "opus")
DEFAULT_TIER = "sonnet"
ROUTER_MODEL = getattr(constants, "ROUTER_MODEL", None) or getattr(constants, "GAME_AGENT_MODEL", "qwen2.5:3b")
TIMEOUT_S = 4.0                # never hold up a reply longer than this
MAX_PROMPT_CHARS = 1500        # the router only needs the gist

# Small talk the router doesn't need a model for.
_SMALL_TALK = re.compile(
    r"^\s*(hi+|hey+|hello+|yo|sup|gm|gn|good (morning|night|evening|afternoon)|thanks?( you)?|thx|ty|"
    r"ok(ay)?|cool|nice|lol|lmao|haha+|bye|see (you|ya)|how are you|what'?s up|wyd|love you|ily)"
    r"[\s!.?~,:)(<3]*$", re.I)

_SYSTEM = (
    "You sort a user's message for an AI assistant into one of three tiers. Reply with ONE word.\n"
    "haiku: greetings, small talk, feelings, quick facts, casual questions.\n"
    "sonnet: writing or changing code, editing files, normal tasks, how-to questions.\n"
    "opus: hard problems: system architecture, tricky bugs, debugging failures, deep multi-step analysis.\n"
    "Examples:\n"
    "\"hey lumi how was your day\" -> haiku\n"
    "\"write a python script that renames my photos by date\" -> sonnet\n"
    "\"fix the typo in notes.txt\" -> sonnet\n"
    "\"my websocket reconnect loop leaks memory and crashes after an hour, find out why\" -> opus\n"
    "\"design the database schema and service boundaries for my app\" -> opus\n"
    "Answer with exactly one of: haiku, sonnet, opus."
)

_TIER_WORD = re.compile(r"\b(haiku|sonnet|opus)\b", re.I)

_last_error_log = {"t": 0.0}
_lock = threading.Lock()


def _gaming() -> bool:
    return bool(getattr(constants, "GAMING_MODE", False))


def parse_tier(text: str) -> str:
    """The first tier word in the model's answer, or the default."""
    m = _TIER_WORD.search(text or "")
    return m.group(1).lower() if m else DEFAULT_TIER


def heuristic_tier(prompt: str) -> Optional[str]:
    """A tier when the answer is obvious without a model, else None."""
    text = (prompt or "").strip()
    if not text:
        return "haiku"
    if len(text) <= 60 and _SMALL_TALK.match(text):
        return "haiku"
    return None


def _ask_model(prompt: str) -> str:
    payload = {
        "model": ROUTER_MODEL,
        "messages": [{"role": "system", "content": _SYSTEM},
                     {"role": "user", "content": prompt[:MAX_PROMPT_CHARS]}],
        "stream": False,
        "think": False,
        "keep_alive": "30m",
        "options": {"temperature": 0, "num_predict": 4, "num_ctx": 2048},
    }
    r = requests.post(f"{constants.LLM_ENDPOINT.rstrip('/')}/api/chat", json=payload, timeout=TIMEOUT_S)
    r.raise_for_status()
    return ((r.json() or {}).get("message") or {}).get("content", "")


def route_model(prompt: str) -> str:
    """Return "haiku", "sonnet" or "opus" for this prompt. Never raises."""
    try:
        quick = heuristic_tier(prompt)
        if quick is not None:
            return quick
        if _gaming():
            return DEFAULT_TIER                       # don't load the router model while gaming
        return parse_tier(_ask_model(str(prompt)))
    except Exception as e:                            # Ollama down, model missing, timeout, bad JSON
        now = time.time()
        with _lock:
            if now - _last_error_log["t"] > 300:      # say it once every 5 minutes, not every turn
                _last_error_log["t"] = now
                print(f"\033[90m[router] falling back to sonnet ({type(e).__name__}); "
                      f"is `{ROUTER_MODEL}` pulled in Ollama?\033[0m")
        return DEFAULT_TIER


# What each tier actually runs. Lumi's chat is local; Claude Code is the one
# place the Claude model names are used for real (on your plan, not an API key).
TIER_INFO: Dict[str, Dict[str, str]] = {
    "haiku":  {"local": "fast", "claude_code": "haiku"},
    "sonnet": {"local": "main", "claude_code": "sonnet"},
    "opus":   {"local": "main", "claude_code": "opus"},
}


def claude_code_model(tier: Optional[str]) -> Optional[str]:
    return TIER_INFO.get(tier or "", {}).get("claude_code")

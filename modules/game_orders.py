"""
Minecraft orders from talking to Lumi.

When the host says (or types) something like "follow me", "make a stone
axe" or "Lumi, go to sleep" in the web chat or into the mic, and the
Minecraft bot is running, the prompter hands it to the game agent through
bot.js's order inbox, the same one the Games page and in-game chat use.

Detection is rules only, so it costs nothing per turn:
  - "!mc <order>" (or "!minecraft <order>") always counts.
  - "in minecraft, ...", "in game ...", "tell your other half to ..." count
    when the rest is an order the agent understands.
  - Otherwise the message must be short, start with a command verb after
    any wake word or politeness ("hey Lumi, can you please ..."), and be
    an order the agent's own parser understands. Bare "wait" or "stop"
    only count as "stay here", "stop following" and the like, so "wait,
    what?" in conversation doesn't freeze her in game.
Only messages from the host machine count (`from_host` on the history
entry), like the Games page.
"""

from __future__ import annotations

import json
import re
import urllib.request
from typing import Optional

BOT_API = "http://127.0.0.1:5057"
MAX_WORDS = 16

_EXPLICIT = re.compile(r"^\s*!(?:mc|minecraft)\b[\s:,]*(.*)$", re.I | re.S)
_WAKE = re.compile(r"^\s*(?:(?:hey|hi|ok(?:ay)?|yo)\s+)?(?:lumi|lumibot)\b[\s,:!.-]*", re.I)
_CONTEXT = re.compile(
    r"^\s*(?:(?:in|on)\s+(?:minecraft|the game|game)|minecraft|"
    r"tell\s+(?:your\s+other\s+half|lumibot|your\s+bot)\s+to)\b[\s,:-]*", re.I)
_POLITE = re.compile(
    r"^\s*(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?|please\s+|pls\s+|"
    r"go\s+ahead\s+and\s+|i\s+(?:want|need)\s+you\s+to\s+|i'?d\s+like\s+you\s+to\s+|"
    r"(?:hey|ok(?:ay)?|alright|now)[\s,]+)", re.I)
_VERBS = {
    "follow", "come", "go", "head", "return", "get", "bring", "give", "hand", "toss",
    "pass", "fetch", "grab", "make", "craft", "build", "mine", "dig", "chop", "collect",
    "gather", "hunt", "cook", "smelt", "sleep", "set", "sort", "organize", "organise",
    "tidy", "put", "explore", "scout", "find", "kill", "fight", "defend", "stick",
}
# Mode words that are everyday speech on their own.
_SAFE_MODES = re.compile(
    r"^(?:stay|wait)(?:\s+(?:here|there|put|right\s+here|right\s+there))$"
    r"|^stop\s+following(?:\s+me)?$|^hold\s+still$|^don'?t\s+move$"
    r"|^do\s+your\s+(?:own\s+)?thing$|^go\s+(?:play|have\s+fun)$", re.I)


def _strip(text: str, pattern: re.Pattern) -> str:
    while True:
        new = pattern.sub("", text, count=1)
        if new == text:
            return text
        text = new


def _parse(text: str):
    try:
        from plugin.minecraft_bot import _parse_order
    except Exception:
        return None
    try:
        return _parse_order(text)
    except Exception:
        return None


def is_explicit(text: str) -> bool:
    """A "!mc ..." command, as opposed to an order spotted in plain speech."""
    return bool(_EXPLICIT.match(text or ""))


def extract_order(text: str) -> Optional[str]:
    """The order to send to the game, or None if this isn't one."""
    if not text:
        return None
    m = _EXPLICIT.match(text)
    if m:
        order = " ".join(m.group(1).split())
        return order or None
    t = " ".join(text.split())
    if t.startswith("!"):
        return None
    t = _strip(t, _WAKE)
    explicit = bool(_CONTEXT.match(t))
    t = _strip(_strip(t, _CONTEXT), _POLITE)
    t = t.strip(" .!?,")
    if not t or len(t.split()) > MAX_WORDS:
        return None
    if not explicit:
        first = t.split()[0].lower().strip(",")
        if first not in _VERBS and not _SAFE_MODES.match(t):
            return None
        if first in ("stay", "wait", "stop", "hold") and not _SAFE_MODES.match(t):
            return None
    return t if _parse(t) is not None else None


def bot_online(timeout: float = 0.5) -> bool:
    """Whether bot.js is up and in a world."""
    try:
        with urllib.request.urlopen(f"{BOT_API}/status", timeout=timeout) as r:
            return bool(json.loads(r.read()).get("ready"))
    except Exception:
        return False

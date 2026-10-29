"""
Minecraft orders from talking to Lumi.

When the host says (or types) "Lumi, follow me", "Lumi, collect wood" or
"make a stone axe, Lumi" in the web chat or into the mic, the prompter
hands it to the game agent through bot.js's order inbox, the same one the
Games page and in-game chat use. Only while the game is on
(game_manager.is_game_active: the Games page switch is on and the game
agent is running). With the game off, nothing is dispatched; the turn gets
a note so she says the game isn't on.

Detection is rules only, so it costs nothing per turn:
  - "!mc <order>" (or "!minecraft <order>") always counts.
  - Otherwise the message must address her: start with "Lumi" ("Lumi,
    ...", "hey Lumi ...") or end with ", Lumi". After the wake word and any
    politeness ("can you please ..."), it must be short, start with a
    command verb (follow, come, collect, get, mine, chop, craft, build,
    give, drop, stop, stay, ...) and be an order the agent's own parser
    understands. "Lumi, how do I make a stone axe?" and "Lumi, stop it lol"
    stay conversation.
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
_TRAILING_WAKE = re.compile(r"[\s,]+(?:lumi|lumibot)[\s.!?]*$", re.I)
_CONTEXT = re.compile(
    r"^\s*(?:(?:in|on)\s+(?:minecraft|the game|game)|minecraft)\b[\s,:-]*", re.I)
_POLITE = re.compile(
    r"^\s*(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?|please\s+|pls\s+|"
    r"go\s+ahead\s+and\s+|i\s+(?:want|need)\s+you\s+to\s+|i'?d\s+like\s+you\s+to\s+|"
    r"(?:hey|ok(?:ay)?|alright|now)[\s,]+)", re.I)
_VERBS = {
    # follow
    "follow", "come", "stick", "return", "head",
    # gather
    "collect", "get", "grab", "fetch", "mine", "dig", "chop", "gather", "find", "hunt",
    "kill", "fight", "defend", "smelt", "cook", "craft", "make", "explore", "scout",
    # give
    "give", "hand", "bring", "toss", "pass", "drop", "throw",
    # build and tidy
    "build", "construct", "sort", "organize", "organise", "tidy", "put", "set", "sleep",
    # stances
    "go", "stop", "stay", "wait", "hold", "halt", "freeze", "do", "resume", "carry",
}
# Stance words are everyday speech too. Addressed to her they still only
# count on their own or as a game stance, so "Lumi, stop it lol" or "Lumi,
# wait, what?" stays conversation.
_STANCE = {"stop", "stay", "wait", "hold", "halt", "freeze"}
_STANCE_OK = re.compile(
    r"^(?:stop|stay|wait|hold|halt|freeze)$"
    r"|^(?:stay|wait)(?:\s+(?:here|there|put|right\s+here|right\s+there))$"
    r"|^stop\s+following(?:\s+me)?$|^hold\s+(?:still|on)$|^don'?t\s+move$", re.I)


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
    # Addressed to her: "Lumi, ..." or "..., Lumi".
    woken = _strip(t, _WAKE)
    if woken == t:
        tail = _TRAILING_WAKE.search(t)
        if not tail:
            return None
        woken = t[:tail.start()]
    t = _strip(_strip(woken, _CONTEXT), _POLITE).strip(" .!?,")
    if not t or len(t.split()) > MAX_WORDS:
        return None
    first = t.split()[0].lower().strip(",")
    if first not in _VERBS:
        return None
    if first in _STANCE and not _STANCE_OK.match(t):
        return None
    return t if _parse(t) is not None else None


def bot_online(timeout: float = 0.5) -> bool:
    """Whether bot.js is up and in a world."""
    try:
        with urllib.request.urlopen(f"{BOT_API}/status", timeout=timeout) as r:
            return bool(json.loads(r.read()).get("ready"))
    except Exception:
        return False

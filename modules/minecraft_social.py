"""
Lumi's social side in Minecraft (used by plugin/minecraft_bot.py).

Pure functions and a small session memory, no I/O besides reading
config/lumi.yaml, so it's easy to test:

  persona_prompt()        who she is in game chat: name, core traits and the
                          `traits:` levels from lumi.yaml turned into how she
                          plays (sarcasm, chaos, patience for mining, ...)
  SocialMemory            who she has met this session: greetings, chats,
                          orders, items she handed over
  nearby_players_block()  the [NEARBY PLAYERS] block for her prompts
  wants_reply()           whether a chat line from a player gets an answer
  observe()               something worth saying about her state (hungry,
                          hurt, a creeper close by, night falling)
  clean_line()            a model reply as one short game-chat line, or None
"""

from __future__ import annotations

import os
import random
import re
import time
from typing import Any, Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "config", "lumi.yaml")

NEARBY_RADIUS = 32          # blocks: who counts as "nearby"
MAX_WORDS = 9               # a game-chat line stays under 10 words

# ── persona ───────────────────────────────────────────────────────

_DEFAULT_TRAITS = {"playfulness": 0.8, "intelligence": 0.9, "chaos": 0.7, "empathy": 0.6,
                   "sarcasm": 0.75, "warmth": 0.65, "curiosity": 0.7, "patience": 0.35}

# How each trait shows up while she plays, at a high and a low level.
_TRAIT_PLAY = {
    "playfulness":  ("tease and joke with the players", "keep jokes rare"),
    "intelligence": ("make sharp, clever remarks about what's going on", "keep remarks simple"),
    "chaos":        ("now and then say something unexpected or a little unhinged", "stay predictable"),
    "empathy":      ("notice when someone is struggling and help or cheer them on", "be blunt"),
    "sarcasm":      ("be dry and sarcastic, especially about chores", "be sincere"),
    "warmth":       ("show you like your player underneath the attitude", "stay cool and distant"),
    "curiosity":    ("get interested in new places, mobs and finds", "stay focused"),
    "patience":     ("take boring work in stride", "complain about tedious work like mining or chopping"),
}


def load_persona(path: str = CONFIG) -> Dict[str, Any]:
    """{name, summary, traits} from lumi.yaml, with defaults if it can't be read."""
    out = {"name": "Lumi", "summary": "", "traits": dict(_DEFAULT_TRAITS)}
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        return out
    name = str(cfg.get("name") or "Lumi").strip()
    out["name"] = name.split()[0] if name else "Lumi"
    pers = cfg.get("personality") or {}
    out["summary"] = " ".join(str(pers.get("core_trait_summary") or "").split())[:400]
    for k, v in (cfg.get("traits") or {}).items():
        try:
            out["traits"][str(k)] = max(0.0, min(1.0, float(v)))
        except (TypeError, ValueError):
            pass
    return out


def trait_lines(traits: Dict[str, float]) -> List[str]:
    """One line per known trait: "sarcasm 0.75: be dry and sarcastic ..."."""
    lines = []
    for k, (high, low) in _TRAIT_PLAY.items():
        if k in traits:
            v = traits[k]
            lines.append(f"- {k} {v:.2f}: {high if v >= 0.5 else low}")
    return lines


def persona_prompt(persona: Optional[Dict[str, Any]] = None) -> str:
    """Her system prompt for anything she says in game chat."""
    p = persona or load_persona()
    return (
        f"You are {p['name']}, playing Minecraft with real people and chatting in game chat. "
        + (f"Your personality: {p['summary']} " if p["summary"] else "")
        + "Your traits, and how they show while you play:\n" + "\n".join(trait_lines(p["traits"]))
        + "\nTalk like a player in chat: short, natural, in your own voice. Never mention being "
        "an AI, a bot, a model, commands, actions or tools, and never use emoji.")


# ── session memory ────────────────────────────────────────────────

GREETING_RE = re.compile(r"^\s*(hi|hey|hello|yo|sup|hiya|howdy|heya|good (morning|evening|afternoon)|gm)\b", re.I)


class SocialMemory:
    """Who she has met this session and what happened between them."""

    def __init__(self) -> None:
        self.people: Dict[str, Dict[str, Any]] = {}

    def _p(self, name: str) -> Dict[str, Any]:
        return self.people.setdefault(name, {"first_seen": time.time(), "last_seen": time.time(),
                                             "greeted": False, "chats": 0, "last_said": "",
                                             "gifts": [], "orders": 0})

    def seen(self, names: List[str]) -> List[str]:
        """Mark players as seen; returns the ones she hadn't seen before."""
        new = [n for n in names if n not in self.people]
        for n in names:
            self._p(n)["last_seen"] = time.time()
        return new

    def chat(self, name: str, message: str) -> bool:
        """Remember a chat line; True if it's a greeting she hadn't had from them."""
        p = self._p(name)
        p["chats"] += 1
        p["last_said"] = " ".join(str(message).split())[:120]
        p["last_seen"] = time.time()
        if GREETING_RE.match(message or "") and not p["greeted"]:
            p["greeted"] = True
            return True
        return False

    def order(self, name: str) -> None:
        self._p(name)["orders"] += 1

    def gift(self, name: str, item: str, count: int) -> None:
        gifts = self._p(name)["gifts"]
        gifts.append((item, int(count)))
        del gifts[:-10]

    def describe(self, name: str) -> str:
        """What she remembers about one player, as a short phrase."""
        p = self.people.get(name)
        if not p:
            return "new to you"
        bits = []
        if p["greeted"]:
            bits.append("said hi to you")
        if p["orders"]:
            bits.append(f"gave you {p['orders']} order{'s' if p['orders'] != 1 else ''}")
        if p["gifts"]:
            total: Dict[str, int] = {}
            for item, n in p["gifts"]:
                total[item] = total.get(item, 0) + n
            bits.append("you gave them " + ", ".join(f"{n} {i.replace('_', ' ')}" for i, n in total.items()))
        if p["last_said"]:
            bits.append(f'last said "{p["last_said"][:60]}"')
        return "; ".join(bits) or "seen around"


GIFT_RE = re.compile(r"^Gave (\d+) ([a-z0-9_]+) to (\S+)")


def nearby(status: Dict[str, Any], radius: float = NEARBY_RADIUS) -> List[Dict[str, Any]]:
    """Players within `radius` blocks, nearest first, from bot.js /status."""
    out = [p for p in (status.get("players") or [])
           if isinstance(p, dict) and p.get("name") and (p.get("distance") is None or p["distance"] <= radius)]
    return sorted(out, key=lambda p: p["distance"] if p.get("distance") is not None else 1e9)


def nearby_players_block(status: Dict[str, Any], memory: Optional[SocialMemory] = None) -> str:
    """[NEARBY PLAYERS] for her prompts: names within 32 blocks and what she knows of them."""
    owner = (status.get("owner") or {}).get("name")
    lines = []
    for p in nearby(status):
        d = p.get("distance")
        where = f"{d:.0f} blocks" if isinstance(d, (int, float)) else "close"
        who = ", your player" if p["name"] == owner else ""
        known = f" — {memory.describe(p['name'])}" if memory else ""
        lines.append(f"- {p['name']} ({where}{who}){known}")
    body = "\n".join(lines) if lines else f"- nobody within {NEARBY_RADIUS} blocks"
    return f"[NEARBY PLAYERS]\n{body}\n[END NEARBY PLAYERS]"


# ── banter ────────────────────────────────────────────────────────

NAME_RE = re.compile(r"\blumi(bot)?\b", re.I)
REPLY_COOLDOWN_S = 8.0          # between any two of her chat replies
PER_PLAYER_COOLDOWN_S = 20.0    # unless they talk to her by name
UNADDRESSED_CHANCE = 0.3        # a line not aimed at her, from someone nearby


def wants_reply(name: str, message: str, *, near: bool, now: float, last_reply: float,
                last_reply_to: Dict[str, float], rng: Optional[random.Random] = None) -> bool:
    """A chat line from a player: does she answer? By name: almost always (a
    short global cooldown). Otherwise sometimes, only if they're nearby."""
    rng = rng or random
    text = str(message or "").strip()
    if not text or text.startswith("/"):
        return False
    if now - last_reply < REPLY_COOLDOWN_S:
        return False
    if NAME_RE.search(text):
        return rng.random() < 0.9
    if not near or now - last_reply_to.get(name, 0) < PER_PLAYER_COOLDOWN_S:
        return False
    return rng.random() < UNADDRESSED_CHANCE


# ── observation ───────────────────────────────────────────────────

OBSERVE_EVERY_S = 30.0
OBSERVE_REPEAT_S = 300.0        # don't bring up the same thing within 5 minutes


def observe(status: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """(key, fact) worth a comment right now, most pressing first, or None.
    The key stops her repeating herself."""
    food, hp = status.get("food"), status.get("health")
    threats = sorted(status.get("threats") or [], key=lambda t: t.get("distance", 99))
    if threats and threats[0].get("distance", 99) <= 16:
        t = threats[0]
        name = str(t.get("name", "mob")).replace("_", " ")
        return f"mob:{t.get('name')}", f"there's a {name} about {t.get('distance', '?')} blocks away"
    if isinstance(hp, (int, float)) and hp <= 8:
        return "hurt", f"you're hurt (health {hp:.0f}/20)"
    if isinstance(food, (int, float)) and food <= 8:
        return "hungry", f"you're getting hungry (hunger {food:.0f}/20)"
    if status.get("is_day") is False:
        return "night", "it's night now, mobs are out"
    return None


# ── output ────────────────────────────────────────────────────────

_ROBOTIC = re.compile(r"\b(executing|action|command|bot|ai|assistant|language model)\b", re.I)


def clean_line(raw: str, max_words: int = MAX_WORDS) -> Optional[str]:
    """The model's reply as one short game-chat line, or None if it isn't one."""
    line = next((l for l in str(raw or "").splitlines() if l.strip()), "")
    line = re.sub(r"<[^>]+>|\[[^\]]*\]", "", line)               # tags like [sarcastic]
    line = re.sub(r"^\s*(?:lumi\w*)\s*[:>-]\s*", "", line, flags=re.I)
    line = line.strip().strip("\"'`*").strip()
    line = "".join(ch for ch in line if ch.isprintable() and ord(ch) < 0x2000)
    words = line.split()
    if not words or len(words) > max_words or _ROBOTIC.search(line):
        return None
    line = " ".join(words)[:90]
    return line if line[-1] in ".!?~…" else line + "."

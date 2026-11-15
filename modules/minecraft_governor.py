"""
Action governor for the Minecraft agent.

    check(action, args, state, intent=None, now=None, cfg=None) -> {"admit": bool, "reason": str}

A pure validator: no model, no network, no Minecraft calls, no side effects.
plugin/minecraft_bot.py runs it on every action before sending it to bot.js
(and on the action a picked goal is about to run). A refusal comes back to
the agent as "action refused: <reason>", which goes into the goal picker's
next prompt so the model can choose something else.

`action` and `args` are the bot.js control-API shape the agent already uses
({"action": "move_to", "args": {...}}; see modules/minecraft_actions.py).
`state` is what the agent knows right now:

    position   {"x", "y", "z"} where she stands (None if unknown)
    inventory  {item: count}
    placed     [(x, y, z, t), ...] blocks she placed (remember_placement)
    players    names of players she knows of (her owner, ...)
    food       names that count as food

Rules (thresholds from constants MINECRAFT_GOVERNOR_*):
  1. Coordinates more than BOX_XZ across or BOX_Y up/down from her position
     are refused, unless the goal is following the player or going home.
  2. Breaking a block she placed in the last PLACED_TTL_S seconds is refused.
  3. Dropping her last tool or her last food is refused.
  4. Attacking a player is refused unless the player is on ATTACK_ALLOWLIST
     (empty by default: she never attacks a player).
  5. A chat message with a URL or an email address is refused.
Everything else is admitted.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from modules.minecraft_actions import COORD_ACTIONS

REFUSED_PREFIX = "action refused: "

# Goals and actions that may go anywhere: following the player, going home,
# and her items after a death.
EXEMPT_INTENTS = frozenset({"follow_player", "return_home", "return_to_player", "go_home",
                            "retrieve_items"})

_TOOL_SUFFIXES = ("_pickaxe", "_axe", "_sword", "_shovel", "_hoe")
_TOOLS_EXTRA = {"shears", "flint_and_steel", "fishing_rod", "bow", "crossbow", "trident"}
_ATTACK_ACTIONS = {"kill_mob": "name", "attack_entity": "name", "attack_nearest": "mob_type"}
_BREAK_ACTIONS = {"dig"}

_URL_RE = re.compile(
    r"(?:\b[a-z][a-z0-9+.-]*://|\bwww\.|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\."
    r"(?:com|net|org|io|gg|xyz|me|co|dev|app|ly|link|info|biz|ru|cn|tk|tv|us|uk|de|site|online|shop)\b)",
    re.I)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


@dataclass(frozen=True)
class Config:
    enabled: bool = True
    box_xz: float = 128
    box_y: float = 64
    placed_ttl_s: float = 300
    attack_allowlist: Tuple[str, ...] = ()
    block_chat_links: bool = True

    @classmethod
    def from_constants(cls) -> "Config":
        try:
            import constants as c
        except Exception:
            return cls()
        return cls(
            enabled=bool(getattr(c, "MINECRAFT_GOVERNOR_ENABLED", True)),
            box_xz=float(getattr(c, "MINECRAFT_GOVERNOR_BOX_XZ", 128)),
            box_y=float(getattr(c, "MINECRAFT_GOVERNOR_BOX_Y", 64)),
            placed_ttl_s=float(getattr(c, "MINECRAFT_GOVERNOR_PLACED_TTL_S", 300)),
            attack_allowlist=tuple(str(p).lower() for p in
                                   getattr(c, "MINECRAFT_GOVERNOR_ATTACK_ALLOWLIST", ()) or ()),
            block_chat_links=bool(getattr(c, "MINECRAFT_GOVERNOR_BLOCK_CHAT_LINKS", True)),
        )


def _admit() -> Dict[str, Any]:
    return {"admit": True, "reason": ""}


def _refuse(reason: str) -> Dict[str, Any]:
    return {"admit": False, "reason": reason}


def _num(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _target(args: Dict[str, Any]) -> Optional[Tuple[float, float, float]]:
    x, y, z = (_num(args.get(k)) for k in ("x", "y", "z"))
    return None if None in (x, y, z) else (x, y, z)


def is_tool(name: str) -> bool:
    return name.endswith(_TOOL_SUFFIXES) or name in _TOOLS_EXTRA


def check(action: str, args: Optional[Dict[str, Any]], state: Optional[Dict[str, Any]], *,
          intent: Optional[str] = None, now: Optional[float] = None,
          cfg: Optional[Config] = None) -> Dict[str, Any]:
    """{"admit": bool, "reason": str} for one proposed action."""
    cfg = cfg or Config.from_constants()
    if not cfg.enabled:
        return _admit()
    args = args if isinstance(args, dict) else {}
    state = state or {}
    now = time.time() if now is None else now
    name = str(action or "")

    # 1. Bounding box around where she stands.
    target = _target(args)
    if name in COORD_ACTIONS and target and intent not in EXEMPT_INTENTS and name not in EXEMPT_INTENTS:
        pos = state.get("position") or {}
        here = _target(pos)
        if here is None:
            return _refuse(f"{name} to {target[0]:.0f}, {target[1]:.0f}, {target[2]:.0f}: "
                           "my own position is unknown")
        dx, dy, dz = (abs(t - h) for t, h in zip(target, here))
        if max(dx, dz) > cfg.box_xz or dy > cfg.box_y:
            return _refuse(f"{name} target {target[0]:.0f}, {target[1]:.0f}, {target[2]:.0f} is outside "
                           f"±{cfg.box_xz:.0f} blocks across / ±{cfg.box_y:.0f} up-down of where I am")

    # 2. Self-grief: a block she placed a moment ago.
    if name in _BREAK_ACTIONS and target:
        bx, by, bz = (int(round(v)) for v in target)
        for px, py, pz, t in state.get("placed") or []:
            if (int(px), int(py), int(pz)) == (bx, by, bz) and now - t < cfg.placed_ttl_s:
                return _refuse(f"I placed the block at {bx}, {by}, {bz} {int(now - t)} s ago")

    # 3. Her last tool or last food.
    if name == "drop_item":
        item = str(args.get("name") or "")
        inv: Dict[str, int] = state.get("inventory") or {}
        have = int(inv.get(item, 0) or 0)
        count = _num(args.get("count"))
        dropping = have if count is None else min(have, int(count))
        if have and dropping >= have:
            if is_tool(item) and not any(is_tool(n) and c > 0 for n, c in inv.items() if n != item):
                return _refuse(f"{item} is my last tool")
            food = set(state.get("food") or ())
            if item in food and not any(n in food and c > 0 for n, c in inv.items() if n != item):
                return _refuse(f"{item} is my last food")

    # 4. Attacking a player.
    if name in _ATTACK_ACTIONS:
        who = str(args.get(_ATTACK_ACTIONS[name]) or "").strip()
        players = {str(p).lower() for p in state.get("players") or [] if p}
        low = who.lower()
        if low and (low == "player" or low in players) and low not in cfg.attack_allowlist:
            return _refuse(f"{who} is a player" + ("" if cfg.attack_allowlist
                                                   else " and no player is on the attack allowlist"))

    # 5. Links and email addresses in chat.
    if name in ("chat", "chat_send") and cfg.block_chat_links:
        text = str(args.get("message") or args.get("content") or "")
        if _EMAIL_RE.search(text):
            return _refuse("chat message contains an email address")
        if _URL_RE.search(text):
            return _refuse("chat message contains a URL")

    return _admit()


def remember_placement(placed: List[Tuple[int, int, int, float]], action: str,
                       args: Optional[Dict[str, Any]], now: Optional[float] = None,
                       ttl_s: float = 300) -> List[Tuple[int, int, int, float]]:
    """The placed-block list with this placement added (when it named a spot)
    and anything older than ttl_s dropped. Returns a new list."""
    now = time.time() if now is None else now
    out = [p for p in placed if now - p[3] < ttl_s]
    target = _target(args or {})
    if action in ("place_block", "place_item", "place_chest") and target:
        out.append((*(int(round(v)) for v in target), now))
    return out[-500:]

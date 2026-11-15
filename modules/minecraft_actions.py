"""
The Minecraft agent's action surface, compact and in a stable order.

Two layers, both already in the repo; this module only gives them a fixed
order, short descriptions and a preamble a model can choose from reliably:

  ACTIONS   the bot.js control-API actions the game agent sends
            (POST 127.0.0.1:5057/action {action, args}). The schema is the
            one bot.js registers (registerActions in
            sdk/examples/minecraft/bot.js); tests/test_minecraft_governor.py
            checks every name here still exists in bot.js's HANDLERS.
  goals     what the model actually picks: plugin/minecraft_bot.py's goal
            tables (FREE_GOALS, CURRICULUM, TOOL_GOALS). Each goal runs one or
            more ACTIONS. The agent passes the goals it offers right now.

render_actions_prompt(goals, detail=False) is the text the goal picker puts
in front of the goal list. detail=True (cloud models, which have the
context for it) adds one line per body action. Voyager's full skill library
is not part of this; see docs/minecraft.md.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

# name, short description, args {name: type}, required args. Order is stable:
# moving, gathering, making, fighting and eating, items, building, talking,
# control. "coords" marks actions whose x/y/z the governor bounds-checks.
ACTIONS: List[Dict[str, Any]] = [
    {"name": "move_to", "desc": "Walk to a block position (pathfinding).",
     "args": {"x": "number", "y": "number", "z": "number", "range": "number"},
     "required": ["x", "y", "z"], "coords": True},
    {"name": "return_to_player", "desc": "Walk back to your player (follow them).",
     "args": {"range": "integer"}, "required": []},
    {"name": "retrieve_items", "desc": "Go back to where you died and pick up your items.",
     "args": {}, "required": []},
    {"name": "mine_block", "desc": "Find, walk to and mine blocks of one type.",
     "args": {"name": "string", "count": "integer"}, "required": ["name"]},
    {"name": "dig", "desc": "Break the block at x, y, z.",
     "args": {"x": "integer", "y": "integer", "z": "integer"}, "required": ["x", "y", "z"], "coords": True},
    {"name": "mine_branch_tunnel", "desc": "Staircase down to a depth, then a tunnel, mining ores.",
     "args": {"depth": "integer", "length": "integer"}, "required": []},
    {"name": "craft_item", "desc": "Craft an item (3x3 recipes need a crafting table nearby).",
     "args": {"name": "string", "count": "integer"}, "required": ["name"]},
    {"name": "smelt_item", "desc": "Smelt an item in a nearby furnace.",
     "args": {"name": "string", "fuel": "string", "count": "integer"}, "required": ["name"]},
    {"name": "kill_mob", "desc": "Hunt and kill the nearest mob of a type, collecting drops.",
     "args": {"name": "string"}, "required": ["name"]},
    {"name": "attack_entity", "desc": "Fight one mob until it dies (nearest hostile if unnamed).",
     "args": {"name": "string"}, "required": []},
    {"name": "defend", "desc": "Fight off hostile mobs within 8 blocks.",
     "args": {}, "required": []},
    {"name": "eat_food", "desc": "Eat the most filling food (or the one named).",
     "args": {"name": "string"}, "required": []},
    {"name": "drop_item", "desc": "Drop items on the ground.",
     "args": {"name": "string", "count": "integer"}, "required": ["name"]},
    {"name": "give_item", "desc": "Walk to a player and toss them an item.",
     "args": {"item_name": "string", "count": "integer", "username": "string"}, "required": ["item_name"]},
    {"name": "sort_inventory", "desc": "Put excess bulk items away in a chest.",
     "args": {"place_if_missing": "boolean"}, "required": []},
    {"name": "deposit_items", "desc": "Put items into a chest.",
     "args": {"item_name": "string", "count": "integer", "x": "integer", "y": "integer", "z": "integer"},
     "required": ["item_name"], "coords": True},
    {"name": "place_block", "desc": "Place a block at x, y, z (or beside you).",
     "args": {"name": "string", "x": "integer", "y": "integer", "z": "integer"},
     "required": ["name"], "coords": True},
    {"name": "build_house", "desc": "Build a 7x7 house next to you (resumable).",
     "args": {"material": "string"}, "required": []},
    {"name": "use_bed", "desc": "Sleep in a bed at night, or set your respawn point at it.",
     "args": {"mode": "string"}, "required": []},
    {"name": "chat", "desc": "Say something in game chat.",
     "args": {"message": "string"}, "required": ["message"]},
    {"name": "execute_skill", "desc": "Run one of the agent's built-in skills (code written by the agent, not the model).",
     "args": {"code": "string", "description": "string", "timeout_ms": "integer"}, "required": ["code"]},
    {"name": "stop", "desc": "Stop moving right away.",
     "args": {}, "required": []},
]

ACTIONS_BY_NAME: Dict[str, Dict[str, Any]] = {a["name"]: a for a in ACTIONS}
COORD_ACTIONS = frozenset(a["name"] for a in ACTIONS if a.get("coords"))

PREAMBLE = (
    "What you can do: each goal below is a whole task your body carries out with "
    "its own actions (walk, mine, craft, smelt, fight, eat, place, build, give, "
    "chat). Choose one goal name from the list; your body handles the steps and "
    "a safety check may refuse an action.")


def render_actions_prompt(goals: Optional[Iterable[Tuple[str, str]]] = None, *, detail: bool = False) -> str:
    """The preamble, then the goals in the order given ("- name: desc"), and
    with detail=True one line per body action ("name(args): desc")."""
    parts = [PREAMBLE]
    if goals is not None:
        parts.append("Goals you can pick:\n" + "\n".join(f"- {n}: {d}" for n, d in goals))
    if detail:
        lines = []
        for a in ACTIONS:
            args = ", ".join(f"{k}:{t}" for k, t in a["args"].items())
            lines.append(f"- {a['name']}({args}): {a['desc']}")
        parts.append("Your body's actions (what the goals use):\n" + "\n".join(lines))
    return "\n\n".join(parts)

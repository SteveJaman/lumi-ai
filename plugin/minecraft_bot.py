"""
Lumi × Minecraft — Autonomous Voyager-inspired agent.

Lumi's "other half": a background game agent, separate from her chat.
It manages its own loop (assess → survive → pick goal → execute skill →
verify) and connects to Lumi's SDK server as "MinecraftAgent" to register
status/control actions, push a silent status line every 30 s, and push
spoken events only for major moments.

The mineflayer bot (sdk/examples/minecraft/bot.js) is the execution
layer. This script talks to it through the local control API on
127.0.0.1:5057.

Goal choice: survival first (fight nearby hostiles, eat when hungry),
then the next curriculum tier or a free-play goal (chop trees, hunt,
explore, build a dirt hut at night, ...). GAME_AGENT_MODEL (a small
model, so Lumi's chat models stay loaded) picks among the goals that can
run right now; without it the agent falls back to a weighted random pick.

Usage: Lumi starts and stops this process from the web dashboard's
"Minecraft AI" switch (modules/game_manager.py). It can also be run by
hand:
    python plugin/minecraft_bot.py

The loop runs only while data/minecraft/autoplay.json allows it (a
missing file counts as allowed).

Dependencies: websockets (already in Lumi's requirements). HTTP calls
use urllib from the stdlib. Goal picking uses modules/llm_providers
when it imports; otherwise the agent runs without it.

Data written at runtime:
    data/minecraft/telemetry.json  — live bot state (read by mc_dashboard.py)
    data/minecraft/events.jsonl    — agent event log (read by mc_dashboard.py)
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

# ─── Path bootstrap ───────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from sdk import LumiClient, action

# Lumi's LLM layer, for goal picking. Optional: the agent plays without it.
try:
    from modules.llm_providers import chat_sync, parse_model_spec
    from constants import GAME_AGENT_MODEL as _GAME_MODEL, LLM_ENDPOINT as _OLLAMA
except Exception:
    chat_sync = parse_model_spec = None
    _GAME_MODEL, _OLLAMA = None, "http://127.0.0.1:11434"

# ─── Paths ────────────────────────────────────────────────────────────────────
DATA_ROOT      = os.path.join(_ROOT, "data")
MC_DIR         = os.path.join(DATA_ROOT, "minecraft")
TELEMETRY_PATH = os.path.join(MC_DIR, "telemetry.json")
EVENTS_PATH    = os.path.join(MC_DIR, "events.jsonl")
AUTOPLAY_PATH  = os.path.join(MC_DIR, "autoplay.json")
TOKEN_PATH     = os.path.join(DATA_ROOT, "memories", "sdk_token.txt")

BOT_API        = os.environ.get("BOT_API", "http://127.0.0.1:5057")
LOOP_INTERVAL  = float(os.environ.get("MC_LOOP_INTERVAL", "4"))  # seconds between steps
# Must exceed bot.js SKILL_TIMEOUT_MS (120 s) so bot.js reports the timeout itself.
ACTION_TIMEOUT = float(os.environ.get("MC_ACTION_TIMEOUT", "135"))
# "llm" asks GAME_AGENT_MODEL to pick goals; "random" skips the model.
GOAL_PICKER    = os.environ.get("MC_GOAL_PICKER", "llm").lower()
GOAL_MODEL     = os.environ.get("MC_GOAL_MODEL") or _GAME_MODEL
# Keep the small game model loaded between picks so it isn't reloaded
# (and doesn't evict anything) once a minute.
GOAL_KEEP_ALIVE = os.environ.get("MC_GOAL_KEEP_ALIVE", "30m")
# Spoken events Lumi reacts to: "major" (diamonds only, from this agent;
# bot.js covers deaths and bosses), "all" (also goal starts, milestones
# and repeated failures), or "off".
NARRATE        = os.environ.get("MC_NARRATE", "major").lower()
NARRATE_COOLDOWN = float(os.environ.get("MC_NARRATE_COOLDOWN", "75"))
STATUS_EVERY   = float(os.environ.get("MC_STATUS_INTERVAL", "30"))  # silent status push
# "follow": she sticks with the player, does their orders, and goes back to
# following after each one, until told "do your own thing". "free": she
# plays on her own near the player, as before.
DEFAULT_MODE   = os.environ.get("MC_DEFAULT_MODE", "follow").lower()
EAT_BELOW_FOOD = 14   # eat when hunger drops below this and food is on hand
DEFEND_RANGE   = 6    # proactively fight hostiles this close (blocks)
# The "semi-magnet": Lumi plays near her player like a kid near a parent.
NEAR_DIST      = 10   # closer than this: right next to them
FAR_DIST       = 25   # farther than this: go back to them
PROJECT_LEASH  = 64   # leash while on a long project (branch mine, house, ores)
PROJECT_GOALS  = {"branch_mine", "build_house", "mine_iron_ore", "mine_diamond"}
PROJECT_HOLD_S = 180  # a project keeps the long leash this long after it starts

# ─── Tech tree ────────────────────────────────────────────────────────────────
# Progress is read from the inventory (plus a crafting table or furnace placed
# within 32 blocks), never from which goal last succeeded: 40 logs from
# chop_trees count as having wood just as much as logs from collect_wood.
# The first five tiers are a fixed if/else chain (_tech_goal) the model never
# sees; after that the model chooses between free-play goals and the next of
# the later tiers.

_LOGS = ["oak_log", "birch_log", "spruce_log", "jungle_log", "acacia_log",
         "dark_oak_log", "mangrove_log"]
_STONE_MATS = ["cobblestone", "cobbled_deepslate", "blackstone"]
_PICKAXE_TIER = {"wooden_pickaxe": 1, "stone_pickaxe": 2, "iron_pickaxe": 3,
                 "diamond_pickaxe": 4, "netherite_pickaxe": 5}


def _logs(inv: Dict[str, int]) -> int:
    return sum(inv.get(n, 0) for n in _LOGS)


def _wood(inv: Dict[str, int]) -> int:
    """Wood on hand, counted in planks (one log makes four)."""
    return _logs(inv) * 4 + sum(c for n, c in inv.items() if n.endswith("_planks"))


def _cobble(inv: Dict[str, int]) -> int:
    return sum(inv.get(n, 0) for n in _STONE_MATS)


def _pickaxe(inv: Dict[str, int]) -> int:
    return max((t for n, t in _PICKAXE_TIER.items() if inv.get(n)), default=0)


def _has_table(inv: Dict[str, int], near: Dict[str, bool]) -> bool:
    return bool(inv.get("crafting_table") or near.get("crafting_table"))


def _has_furnace(inv: Dict[str, int], near: Dict[str, bool]) -> bool:
    return bool(inv.get("furnace") or near.get("furnace"))


# Every tool, not just pickaxes. Tiers match _PICKAXE_TIER; the cost is
# (head material, sticks).
_TOOL_TIER = {"wooden": 1, "stone": 2, "iron": 3, "diamond": 4}
_TOOL_COST = {"pickaxe": (3, 2), "axe": (3, 2), "sword": (2, 1), "shovel": (1, 2), "hoe": (2, 2)}
# The pickaxes already have curriculum goals; every other tool gets craft_<mat>_<tool>.
_PICKAXE_GOAL = {"wooden": "craft_wood_pickaxe", "stone": "craft_stone_pickaxe",
                 "iron": "craft_iron_pickaxe"}


def _tool_level(inv: Dict[str, int], tool: str) -> int:
    """Best tier of this kind of tool on hand, 0 for none."""
    return max((t for m, t in _TOOL_TIER.items() if inv.get(f"{m}_{tool}")), default=0)


def _craft_goal(mat: str, tool: str) -> str:
    return _PICKAXE_GOAL.get(mat) if tool == "pickaxe" and mat in _PICKAXE_GOAL else f"craft_{mat}_{tool}"


def _craft_step(mat: str, tool: str, inv: Dict[str, int], near: Dict[str, bool]) -> Optional[str]:
    """The next goal toward a tool: get whatever's missing, then craft it.
    Stone needs a pickaxe to mine it, iron a stone pickaxe and smelting,
    diamond an iron pickaxe."""
    head, sticks = _TOOL_COST[tool]
    if mat == "stone":
        if _pickaxe(inv) < 1:
            return _tech_goal(inv, near)
        if _cobble(inv) < head:
            return "mine_stone"
    elif mat == "iron":
        if _pickaxe(inv) < 2:
            return _tech_goal(inv, near)
        ingots = inv.get("iron_ingot", 0)
        if ingots < head:
            if ingots + inv.get("raw_iron", 0) < head:
                return "mine_iron_ore"
            return "smelt_iron" if _has_furnace(inv, near) or _cobble(inv) >= 8 else "mine_stone"
    elif mat == "diamond":
        if _pickaxe(inv) < 3:
            return _tool_step(3, inv, near)
        if inv.get("diamond", 0) < head:
            return "branch_mine"
    wood = ((head if mat == "wooden" else 0)
            + (0 if inv.get("stick", 0) >= sticks else 2)
            + (0 if _has_table(inv, near) else 4))
    if _wood(inv) < wood:
        return "collect_wood"
    return _craft_goal(mat, tool)


# After the stone pickaxe, the rest of a first stone kit, the way a player
# does it: an axe so trees come down fast, then a sword.
_KIT = [("axe", "an axe chops trees much faster than bare hands"),
        ("sword", "a sword for fighting mobs")]


def _kit_goal(inv: Dict[str, int], near: Dict[str, bool]) -> Optional[Tuple[str, str]]:
    """(goal, reason) toward the next missing stone tool, or None."""
    if _pickaxe(inv) < 2:
        return None
    for tool, why in _KIT:
        if _tool_level(inv, tool) < 2:
            step = _craft_step("stone", tool, inv, near)
            if step:
                return step, f"need a stone {tool}: {why}"
    return None


CURRICULUM: List[Dict[str, Any]] = [
    # Basics, chosen by _tech_goal in this order.
    {"goal": "collect_wood",        "desc": "Collect logs",
     "done": lambda inv, near: _pickaxe(inv) >= 1 or _wood(inv) >= 12},
    {"goal": "craft_workbench",     "desc": "Craft a crafting table",
     "done": lambda inv, near: _has_table(inv, near) or _pickaxe(inv) >= 1},
    {"goal": "craft_wood_pickaxe",  "desc": "Craft a wooden pickaxe",
     "done": lambda inv, near: _pickaxe(inv) >= 1},
    {"goal": "mine_stone",          "desc": "Mine cobblestone",
     "done": lambda inv, near: _cobble(inv) >= 3 or _pickaxe(inv) >= 2},
    {"goal": "craft_stone_pickaxe", "desc": "Craft a stone pickaxe",
     "done": lambda inv, near: _pickaxe(inv) >= 2},
    # Later tiers, offered to the model once ready.
    {"goal": "craft_furnace",       "desc": "Craft a furnace",
     "done": lambda inv, near: _has_furnace(inv, near),
     "ready": lambda inv, near: _cobble(inv) >= 8},
    {"goal": "mine_iron_ore",       "desc": "Find and mine iron ore",
     "done": lambda inv, near: inv.get("raw_iron", 0) >= 3 or inv.get("iron_ingot", 0) >= 3
                               or _pickaxe(inv) >= 3,
     "ready": lambda inv, near: _pickaxe(inv) >= 2},
    {"goal": "smelt_iron",          "desc": "Smelt iron ingots",
     "done": lambda inv, near: inv.get("iron_ingot", 0) >= 3 or _pickaxe(inv) >= 3,
     "ready": lambda inv, near: inv.get("raw_iron", 0) >= 3
                                and (_has_furnace(inv, near) or _cobble(inv) >= 8)},
    {"goal": "craft_iron_pickaxe",  "desc": "Craft an iron pickaxe",
     "done": lambda inv, near: _pickaxe(inv) >= 3,
     "ready": lambda inv, near: inv.get("iron_ingot", 0) >= 3},
    {"goal": "mine_diamond",        "desc": "Find and mine diamonds",
     "done": lambda inv, near: inv.get("diamond", 0) >= 1 or _pickaxe(inv) >= 4,
     "ready": lambda inv, near: _pickaxe(inv) >= 3},
]
_TIERS = {t["goal"]: t for t in CURRICULUM}


def _tech_goal(inv: Dict[str, int], near: Dict[str, bool]) -> Optional[str]:
    """The basics, decided in code: wood → crafting table → wooden pickaxe →
    cobblestone → stone pickaxe. None once there's a stone pickaxe."""
    pick = _pickaxe(inv)
    if pick >= 2:
        return None
    wood = _wood(inv)
    table = _has_table(inv, near)
    sticks_cost = 0 if inv.get("stick", 0) >= 2 else 2   # planks for two sticks
    table_cost = 0 if table else 4
    if pick == 1:
        if _cobble(inv) < 3:
            return "mine_stone"
        return "collect_wood" if wood < sticks_cost + table_cost else "craft_stone_pickaxe"
    if wood < 3 + sticks_cost + table_cost:
        return "collect_wood"
    return "craft_workbench" if not table else "craft_wood_pickaxe"


def _progress_goal(inv: Dict[str, int], near: Dict[str, bool]) -> Optional[str]:
    """The next later tier that can run now, for the model to consider."""
    for tier in CURRICULUM[5:]:
        if not tier["done"](inv, near) and tier["ready"](inv, near):
            return tier["goal"]
    if _pickaxe(inv) >= 2 and not _has_furnace(inv, near) and _cobble(inv) < 8:
        return "mine_stone"      # a furnace needs eight
    return None


def _progress_count(inv: Dict[str, int], near: Dict[str, bool]) -> int:
    return sum(1 for t in CURRICULUM if t["done"](inv, near))


# Helpers shared by the skills below. Everything else they call (mineBlock,
# craftItem, placeItem, smeltItem, killMob, exploreUntil) is a Voyager
# control primitive that bot.js puts in scope from plugin/voyager. Skills
# check their own result and throw when nothing changed, so "mine stone"
# with no stone around is a failure, not a success that repeats forever.
_PRELUDE = """
const LOGS = """ + json.dumps(_LOGS) + """;
function countOf(name) {
  return bot.inventory.items().filter(i => i.name === name).reduce((s, i) => s + i.count, 0);
}
function countWhere(pred) {
  return bot.inventory.items().filter(i => pred(i.name)).reduce((s, i) => s + i.count, 0);
}
const logCount = () => countWhere(n => LOGS.includes(n));
const plankCount = () => countWhere(n => n.endsWith("_planks"));
// An empty cell on solid ground next to the bot, for placing a table or
// furnace without hitting a wall (e.g. inside a dirt hut).
function freeSpot() {
  const base = bot.entity.position.floored();
  for (const [dx, dz] of [[1,0],[-1,0],[0,1],[0,-1],[1,1],[-1,-1],[1,-1],[-1,1],[2,0],[0,2],[-2,0],[0,-2]]) {
    const p = base.offset(dx, 0, dz);
    const here = bot.blockAt(p), below = bot.blockAt(p.offset(0, -1, 0));
    if (here && here.boundingBox === "empty" && below && below.boundingBox === "block") return p;
  }
  return base.offset(1, 0, 0);
}
async function ensurePlanks(n) {
  if (plankCount() >= n) return;
  const log = bot.inventory.items().filter(i => LOGS.includes(i.name)).sort((a, b) => b.count - a.count)[0];
  if (!log) throw new Error("No logs to make planks from");
  await craftItem(bot, log.name.replace(/_log$/, "_planks"), Math.ceil((n - plankCount()) / 4));
  if (plankCount() < n) throw new Error(`Only ${plankCount()} planks, need ${n}`);
}
async function ensureSticks(n) {
  if (countOf("stick") >= n) return;
  await ensurePlanks(2);
  await craftItem(bot, "stick", Math.ceil((n - countOf("stick")) / 4));
  if (countOf("stick") < n) throw new Error("Couldn't craft sticks");
}
async function ensureNear(blockName) {
  const id = mcData.blocksByName[blockName].id;
  if (bot.findBlock({ matching: id, maxDistance: 32 })) return;
  if (!countOf(blockName)) {
    if (blockName === "crafting_table") await ensurePlanks(4);
    else await ensureNear("crafting_table");
    await craftItem(bot, blockName, 1);
    if (!countOf(blockName)) throw new Error(`Couldn't craft a ${blockName}`);
  }
  await placeItem(bot, blockName, freeSpot());
}
async function findOrExplore(blockName, direction) {
  const id = mcData.blocksByName[blockName].id;
  const look = () => bot.findBlock({ matching: id, maxDistance: 32 });
  if (look()) return;
  if (!(await exploreUntil(bot, direction, 60, look))) {
    throw new Error(`No ${blockName} found after exploring`);
  }
}
async function chopTrees(n) {
  const ids = LOGS.map(name => mcData.blocksByName[name]).filter(Boolean).map(b => b.id);
  const look = () => bot.findBlock({ matching: ids, maxDistance: 32 });
  if (!look()) await exploreUntil(bot, new Vec3(1, 0, 1), 40, look);
  const tree = look();
  if (!tree) throw new Error("No trees anywhere around here");
  const before = logCount();
  let stopped = null;
  try { await mineBlock(bot, tree.name, n); } catch (e) { stopped = e; }
  const got = logCount() - before;
  if (got <= 0) throw stopped || new Error(`Couldn't get any ${tree.name}`);
  return `Chopped ${got} ${tree.name}` + (stopped ? ` (stopped early: ${stopped.message})` : "");
}
async function craftAndCheck(name) {
  await craftItem(bot, name, 1);
  if (!countOf(name)) throw new Error(`Couldn't craft a ${name}`);
  return `Crafted a ${name}`;
}
async function mineAndCheck(block, drops, count) {
  const have = () => drops.reduce((s, d) => s + countOf(d), 0);
  const before = have();
  // Mining several blocks can fail partway (an unreachable ore, a path that
  // takes too long). Whatever was mined by then is progress, not a failure.
  let stopped = null;
  try { await mineBlock(bot, block, count); } catch (e) { stopped = e; }
  const got = have() - before;
  if (got <= 0) throw stopped || new Error(`Mined no ${drops[0]}`);
  return `Got ${got} ${drops[0]}` + (stopped ? ` (stopped early: ${stopped.message})` : "");
}
"""

BUILTIN_SKILLS: Dict[str, str] = {
    name: _PRELUDE + body for name, body in {
        "collect_wood":        'return await chopTrees(5);',
        "craft_workbench":     'await ensurePlanks(4);\n'
                               'return await craftAndCheck("crafting_table");',
        "craft_wood_pickaxe":  'await ensureNear("crafting_table");\n'
                               'await ensureSticks(2);\n'
                               'await ensurePlanks(3);\n'
                               'return await craftAndCheck("wooden_pickaxe");',
        "mine_stone":          'await findOrExplore("stone", new Vec3(1, -1, 1));\n'
                               'return await mineAndCheck("stone", ["cobblestone", "cobbled_deepslate"], 8);',
        "craft_stone_pickaxe": 'await ensureNear("crafting_table");\n'
                               'await ensureSticks(2);\n'
                               'return await craftAndCheck("stone_pickaxe");',
        "craft_furnace":       'await ensureNear("crafting_table");\n'
                               'return await craftAndCheck("furnace");',
        "mine_iron_ore":       'await findOrExplore("iron_ore", new Vec3(1, -1, 1));\n'
                               'return await mineAndCheck("iron_ore", ["raw_iron"], 3);',
        "smelt_iron":          'await ensureNear("furnace");\n'
                               'let fuel = "coal";\n'
                               'if (!countOf("coal")) {\n'
                               '  await ensurePlanks(4);\n'
                               '  fuel = bot.inventory.items().find(i => i.name.endsWith("_planks")).name;\n'
                               '}\n'
                               'const before = countOf("iron_ingot");\n'
                               'await smeltItem(bot, "raw_iron", fuel, Math.min(countOf("raw_iron"), 3));\n'
                               'if (countOf("iron_ingot") <= before) throw new Error("Smelted no iron");\n'
                               'return `Have ${countOf("iron_ingot")} iron ingots`;',
        "craft_iron_pickaxe":  'await ensureNear("crafting_table");\n'
                               'await ensureSticks(2);\n'
                               'return await craftAndCheck("iron_pickaxe");',
        "mine_diamond":        'await findOrExplore("diamond_ore", new Vec3(1, -1, 0));\n'
                               'return await mineAndCheck("diamond_ore", ["diamond"], 1);',
    }.items()
}


def _tool_skill(mat: str, tool: str) -> str:
    head, sticks = _TOOL_COST[tool]
    planks = f"await ensurePlanks({head});\n" if mat == "wooden" else ""
    return (_PRELUDE + 'await ensureNear("crafting_table");\n'
            f"await ensureSticks({sticks});\n{planks}"
            f'return await craftAndCheck("{mat}_{tool}");')


# craft_<mat>_<tool> for every tool without a curriculum goal.
TOOL_GOALS: Dict[str, str] = {
    _craft_goal(m, t): f"Craft {'an' if m[0] in 'aeiou' else 'a'} {m} {t}"
    for m in _TOOL_TIER for t in _TOOL_COST if _craft_goal(m, t) not in _TIERS
}
BUILTIN_SKILLS.update({g: _tool_skill(*g[len("craft_"):].split("_", 1)) for g in TOOL_GOALS})

# Plain bot.js actions to run when Voyager's primitives aren't loaded. Those
# actions fall back to raw mineflayer inside bot.js. They assume oak, and
# tiers that need exploring or smelting have no fallback. A third element
# of True marks a step whose failure is ignored.
Step = Tuple[Any, ...]
CURRICULUM_STEPS: Dict[str, List[Step]] = {
    "collect_wood":        [("mine_block", {"name": "oak_log", "count": 5})],
    "craft_workbench":     [("craft_item", {"name": "oak_planks", "count": 1}),
                            ("craft_item", {"name": "crafting_table", "count": 1})],
    "craft_wood_pickaxe":  [("place_block", {"name": "crafting_table"}, True),
                            ("craft_item", {"name": "oak_planks", "count": 2}),
                            ("craft_item", {"name": "stick", "count": 1}),
                            ("craft_item", {"name": "wooden_pickaxe", "count": 1})],
    "mine_stone":          [("mine_block", {"name": "stone", "count": 8})],
    "craft_stone_pickaxe": [("craft_item", {"name": "stick", "count": 1}, True),
                            ("craft_item", {"name": "stone_pickaxe", "count": 1})],
    "craft_furnace":       [("craft_item", {"name": "furnace", "count": 1})],
    "craft_iron_pickaxe":  [("craft_item", {"name": "iron_pickaxe", "count": 1})],
}
CURRICULUM_STEPS.update({g: [("craft_item", {"name": "stick", "count": 1}, True),
                             ("craft_item", {"name": g[len("craft_"):], "count": 1})]
                         for g in TOOL_GOALS})

# ─── Free-play goals ──────────────────────────────────────────────────────────
# Repeatable goals picked when the agent isn't pushing the curriculum. Each
# has a `when(inv, status)` gate, Voyager `code` (a string or a function of
# the status), and optional plain-action `steps` for when Voyager is missing.

_DIRS = [(1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1), (1, -1), (-1, 1)]
_MEAT = ["beef", "porkchop", "chicken", "mutton"]
_PICKAXES = ["wooden_pickaxe", "stone_pickaxe", "iron_pickaxe", "diamond_pickaxe", "netherite_pickaxe"]


def _explore_dir(status: Dict) -> Tuple[int, int]:
    """Random, but back toward the player once more than 15 blocks out, so
    exploring stays nearby."""
    owner, me = status.get("owner") or {}, status.get("position") or {}
    op = owner.get("position")
    if op and me and (owner.get("distance") or 0) > 15:
        sign = lambda v: (v > 2) - (v < -2)
        d = (sign(op["x"] - me["x"]), sign(op["z"] - me["z"]))
        if d != (0, 0):
            return d
    return random.choice(_DIRS)


def _explore_code(status: Dict) -> str:
    dx, dz = _explore_dir(status)
    return (f'await exploreUntil(bot, new Vec3({dx}, 0, {dz}), 45, () => null);\n'
            f'const p = bot.entity.position;\n'
            f'return `Explored to ${{p.x.toFixed(0)}}, ${{p.y.toFixed(0)}}, ${{p.z.toFixed(0)}}`;')


def _explore_steps(status: Dict) -> List[Step]:
    p = status.get("position") or {}
    dx, dz = _explore_dir(status)
    return [("move_to", {"x": p.get("x", 0) + dx * 30, "y": p.get("y", 64),
                         "z": p.get("z", 0) + dz * 30, "range": 6})]


_CHOP_TREES = "return await chopTrees(4);"

_HUNT_FOOD = f"""
const prey = {json.dumps(["cow", "pig", "sheep", "chicken"])};
const find = () => bot.nearestEntity(e => prey.includes(e.name) &&
                                     bot.entity.position.distanceTo(e.position) < 48);
if (!find()) await exploreUntil(bot, new Vec3(-1, 0, 1), 60, find);
const target = find();
if (!target) throw new Error("No animals around to hunt");
await killMob(bot, target.name, 60);
return `Hunted a ${{target.name}}`;
"""

_COOK_MEAT = f"""
const raw = {json.dumps(_MEAT)}.find(n => countOf(n) > 0);
if (!raw) throw new Error("No raw meat to cook");
let fuel = "coal";
if (!countOf("coal")) {{
  await ensurePlanks(4);
  fuel = bot.inventory.items().find(i => i.name.endsWith("_planks")).name;
}}
await ensureNear("furnace");
await smeltItem(bot, raw, fuel, Math.min(countOf(raw), 6));
return `Cooked ${{raw}}`;
"""

_MINE_COAL = """
await findOrExplore("coal_ore", new Vec3(1, -1, -1));
await mineBlock(bot, "coal_ore", 5);
"""

# The classic panic box: a sealed 3x3 dirt hut around the bot, built from
# where it stands so it doesn't wander off mid-build.
_DIRT_HUT = """
const need = 25;
if (countOf("dirt") < need) await mineBlock(bot, "dirt", need - countOf("dirt"));
if (countOf("dirt") < need) throw new Error("Not enough dirt for a hut");
const c = bot.entity.position.floored();
const faces = [[0,-1,0],[0,1,0],[1,0,0],[-1,0,0],[0,0,1],[0,0,-1]];
async function put(p) {
  const here = bot.blockAt(p);
  if (here && here.boundingBox !== "empty") return;
  const item = bot.inventory.items().find(i => i.name === "dirt");
  if (!item) throw new Error("Ran out of dirt");
  for (const [x, y, z] of faces) {
    const ref = bot.blockAt(p.offset(x, y, z));
    if (ref && ref.boundingBox === "block") {
      await bot.equip(item, "hand");
      await bot.placeBlock(ref, new Vec3(-x, -y, -z));
      return;
    }
  }
}
const ring = [[-1,-1],[0,-1],[1,-1],[-1,0],[1,0],[-1,1],[0,1],[1,1]];
for (const dy of [0, 1]) for (const [dx, dz] of ring) await put(c.offset(dx, dy, dz));
for (const [dx, dz] of [...ring, [0, 0]]) await put(c.offset(dx, 2, dz));
return "Built a dirt hut";
"""

_WAIT_NIGHT = """
const t0 = Date.now();
while (bot.time && !bot.time.isDay && Date.now() - t0 < 100000) await bot.waitForTicks(20);
return bot.time && bot.time.isDay ? "Made it to morning" : "Still night, waited a while";
"""

_CRAFT_TORCHES = """
await ensureSticks(2);
const before = countOf("torch");
await craftItem(bot, "torch", Math.min(countOf("coal"), 4));
if (countOf("torch") <= before) throw new Error("Couldn't craft torches");
return `Have ${countOf("torch")} torches`;
"""

_GATHER_COBBLE = """
await findOrExplore("stone", new Vec3(1, -1, 1));
return await mineAndCheck("stone", ["cobblestone", "cobbled_deepslate"], 16);
"""

# A bed takes 3 wool of one colour and 3 planks. Sheep drop one wool each
# (Voyager's killMob picks it up).
_GET_BED = """
const WOOLS = mcData.itemsArray.filter(i => i.name.endsWith("_wool")).map(i => i.name);
const bestWool = () => WOOLS.map(n => [n, countOf(n)]).sort((a, b) => b[1] - a[1])[0];
const sheep = () => bot.nearestEntity(e => e.name === "sheep" &&
                                       bot.entity.position.distanceTo(e.position) < 48);
for (let i = 0; i < 6 && bestWool()[1] < 3; i++) {
  if (!sheep()) await exploreUntil(bot, new Vec3(1, 0, -1), 60, sheep);
  if (!sheep()) break;
  await killMob(bot, "sheep", 60);
}
const [wool, n] = bestWool();
if (n < 3) throw new Error(`Only ${n} wool of one colour, a bed needs 3`);
await ensureNear("crafting_table");
await ensurePlanks(3);
return await craftAndCheck(wool.replace(/_wool$/, "_bed"));
"""

Code = Union[str, Callable[[Dict], str]]


def _has(inv: Dict[str, int], names: List[str]) -> bool:
    return any(inv.get(n, 0) > 0 for n in names)


def _owner_distance(st: Dict) -> Optional[float]:
    """Horizontal distance to the player, or None when out of sight.
    Horizontal on purpose: mining straight below them is still "nearby"."""
    owner, me = st.get("owner") or {}, st.get("position") or {}
    op = owner.get("position")
    if not op or not me:
        return owner.get("distance")
    return ((op["x"] - me["x"]) ** 2 + (op["z"] - me["z"]) ** 2) ** 0.5


def _house(st: Dict) -> Dict:
    return st.get("house") or {}


def _house_center(st: Dict) -> Optional[Dict[str, float]]:
    o = _house(st).get("origin")
    return {"x": o[0] + 3, "y": o[1] + 1, "z": o[2] + 3} if o else None


def _away_from_home(st: Dict) -> bool:
    c, me = _house_center(st), st.get("position")
    if not c or not me:
        return False
    return max(abs(me["x"] - c["x"]), abs(me["z"] - c["z"])) > 3 or abs(me["y"] - c["y"]) > 2


def _at_surface(st: Dict) -> bool:
    """Roughly level with the player, so the house isn't built in a mine."""
    op, me = (st.get("owner") or {}).get("position"), st.get("position")
    return not op or not me or abs(op["y"] - me["y"]) <= 8


def _house_material(inv: Dict[str, int]) -> str:
    return "cobblestone" if _cobble(inv) >= _wood(inv) else "planks"


def _bed(st: Dict) -> Dict:
    return st.get("bed") or {}


def _bed_handy(inv: Dict[str, int], st: Dict) -> bool:
    """A bed within 32 blocks or in the inventory."""
    return bool(_bed(st).get("nearby") or any(n.endswith("_bed") for n in inv))


def _bed_routine(inv: Dict[str, int], st: Dict) -> Optional[str]:
    """Sleep at night, else set the respawn point at a bed if it isn't set."""
    if not _bed_handy(inv, st):
        return None
    if st.get("can_sleep"):
        return "sleep_in_bed"
    return None if _bed(st).get("spawn_set") else "set_spawn"


def _wants(st: Dict, *items: str) -> bool:
    """Whether a failed house build asked for one of these items. The agent
    puts its wishlist into the status it hands the gates."""
    return any(st.get("wanted", {}).get(i) for i in items)


# Each gate is checked in code, so the model is never offered a goal that
# makes no sense right now (a hut at noon, more trees with a full stack).
FREE_GOALS: Dict[str, Dict[str, Any]] = {
    "chop_trees":     {"desc": "Chop down some trees for wood",
                       "when": lambda inv, st: _logs(inv) < 10 or _wants(st, "oak_planks", "planks"),
                       "code": _CHOP_TREES,
                       "steps": [("mine_block", {"name": "oak_log", "count": 4})]},
    "hunt_food":      {"desc": "Hunt an animal for food",
                       "when": lambda inv, st: True,
                       "code": _HUNT_FOOD,
                       "steps": [("kill_mob", {"name": "cow"})]},
    "cook_meat":      {"desc": "Cook raw meat in a furnace",
                       "when": lambda inv, st: _has(inv, _MEAT) and (inv.get("coal", 0) or _wood(inv) >= 4),
                       "code": _COOK_MEAT},
    "explore":        {"desc": "Wander off and explore the area",
                       "when": lambda inv, st: True,
                       "code": _explore_code,
                       "steps": _explore_steps},
    "mine_coal":      {"desc": "Find and mine coal for torches and fuel",
                       "when": lambda inv, st: _has(inv, _PICKAXES),
                       "code": _MINE_COAL},
    "build_dirt_hut": {"desc": "Build a dirt hut to hide in for the night",
                       "when": lambda inv, st: not st.get("is_day", True) and not _house(st).get("complete"),
                       "code": _DIRT_HUT},
    "go_home":        {"desc": "Go home to the house for the night",
                       "when": lambda inv, st: (not st.get("is_day", True) and _house(st).get("complete")
                                                and _away_from_home(st)),
                       "action": ("move_to", lambda st: dict(_house_center(st), range=1))},
    "build_house":    {"desc": "Build a proper 7x7 house",
                       "when": lambda inv, st: (st.get("is_day", True) and not _house(st).get("complete")
                                                and max(_cobble(inv), _wood(inv)) >= 64 and _at_surface(st)),
                       "action": ("build_house", lambda st: {"material": _house_material(_inv(st))})},
    "gather_cobblestone": {"desc": "Mine cobblestone for the house",
                       "when": lambda inv, st: _wants(st, "cobblestone") and _pickaxe(inv) >= 1,
                       "code": _GATHER_COBBLE},
    "branch_mine":    {"desc": "Dig a branch mine and look for ores",
                       "when": lambda inv, st: _pickaxe(inv) >= 2,
                       # Iron level first; diamonds sit near the bottom of the world in 1.18+.
                       "action": ("mine_branch_tunnel",
                                  lambda st: {"depth": -54 if _pickaxe(_inv(st)) >= 3 else 12, "length": 24})},
    "craft_torches":  {"desc": "Craft torches",
                       "when": lambda inv, st: inv.get("coal", 0) >= 1 and inv.get("torch", 0) < 16
                                               and _wood(inv) >= 2,
                       "code": _CRAFT_TORCHES},
    "organize_inventory": {"desc": "Put excess stuff away in a chest",
                       "when": lambda inv, st: (st.get("free_slots") or 36) <= 6,
                       "action": ("sort_inventory", {"place_if_missing": True})},
    "get_bed":        {"desc": "Make a bed (wool from three sheep)",
                       "when": lambda inv, st: not _bed_handy(inv, st) and not _bed(st).get("remembered"),
                       "code": _GET_BED},
    # Never offered as options: chosen by an order or a routine.
    "sleep_in_bed":   {"desc": "Sleep in a bed for the night",
                       "when": lambda inv, st: False,
                       "action": ("use_bed", {"mode": "sleep"})},
    "set_spawn":      {"desc": "Set my respawn point at a bed",
                       "when": lambda inv, st: False,
                       "action": ("use_bed", {"mode": "spawn"})},
    "retrieve_items": {"desc": "Go back to where I died and pick up my items",
                       "when": lambda inv, st: False,
                       "action": ("retrieve_items", {})},
    "give_item":      {"desc": "Hand an item over to my player",
                       "when": lambda inv, st: False,
                       "action": ("give_item", {})},
    "return_to_player": {"desc": "Head back to my player",
                       "when": lambda inv, st: False,
                       "action": ("return_to_player", {"range": 4})},
    "wait_out_night": {"desc": "Hide and wait for the sun to come up",
                       "when": lambda inv, st: not st.get("is_day", True),
                       "code": _WAIT_NIGHT},
}

PICKER_SYSTEM = (
    "You choose the next goal for a Minecraft survival player. Think like a "
    "streamer playing for fun: make progress, stay alive, and vary what you do. "
    "Prefer the tool-progress goal when one is listed, shelter at night, and "
    "food when hunger is low. Pick only from the listed goal names. Reply with "
    'JSON only: {"goal": "<one of the listed names>", "reason": "<one short sentence>"}'
)

# Goal time limits (s), shorter than bot.js's 120 s default where a stuck
# pathfinder is the usual failure, so the agent moves on sooner.
GOAL_TIMEOUT_S = {"collect_wood": 60, "chop_trees": 60, "explore": 60,
                  "build_dirt_hut": 90, "hunt_food": 75, "mine_stone": 90,
                  "gather_cobblestone": 90}
# After success, don't offer a goal again for this long (s).
SUCCESS_COOLDOWN_S = {"build_dirt_hut": 600, "organize_inventory": 120}
# After a single failure, rest these this long (s): an old server that only
# sets the respawn point by sleeping, monsters she can't clear from a bed.
FAIL_REST_S = {"set_spawn": 1800, "sleep_in_bed": 90}
FAIL_STREAK = 3          # failures in a row before a goal is rested
FAIL_COOLDOWN_S = 300


# ─── Orders ───────────────────────────────────────────────────────────────────
# An order (directive) comes from the owner in game chat ("Lumi, get me 5
# diamonds") or from the web UI's Games page, through bot.js's
# /directive inbox. Common orders are parsed here; anything else goes to the
# game model as an override. Checked in this order, so "forget diamonds,
# come back to me" is a "come back".

# "give me the pickaxe", "bring me 5 diamonds", "hand over your sword"
GIVE_RE = re.compile(r"\b(?:give|hand|toss|pass|bring)\b(?:\s+(?:me|us|over|it))*\s+(.*)$")
GIVE_FILLER = re.compile(r"\b(the|a|an|some|your|my|of|please|pls|now|here|back|to me|over)\b")
# "make an iron pickaxe", "craft me a stone axe", "smelt it into an iron sword"
TOOL_RE = re.compile(r"\b(wood(?:en)?|stone|cobble(?:stone)?|iron|diamond)\s+"
                     r"(pick(?:axe)?|axe|sword|shovel|spade|hoe)s?\b")
# No material named ("make an axe"): stone, the first real tier.
BARE_TOOL_RE = re.compile(r"\b(pick(?:axe)?|axe|sword|shovel|spade|hoe)s?\b")
TOOL_MAT = {"wood": "wooden", "wooden": "wooden", "stone": "stone", "cobble": "stone",
            "cobblestone": "stone", "iron": "iron", "diamond": "diamond"}
TOOL_KIND = {"pick": "pickaxe", "spade": "shovel"}
# Orders that ask for delivery ("get me", "bring me", "for me", "craft me").
DELIVER_RE = re.compile(r"\b(?:get|bring|give|fetch|grab|make|craft|build)\s+me\b|\bfor me\b")


def _tool_order(text: str, deliver: bool) -> Optional[Dict[str, Any]]:
    """{kind: tool, ...} for an order naming a tool, or None."""
    m = TOOL_RE.search(text)
    if m:
        mat, tool = TOOL_MAT[m.group(1)], m.group(2)
    else:
        m = BARE_TOOL_RE.search(text)
        if not m:
            return None
        mat, tool = "stone", m.group(1)
    tool = TOOL_KIND.get(tool, tool)
    return {"kind": "tool", "mat": mat, "tool": tool, "item": f"{mat}_{tool}",
            "deliver": deliver, "label": f"{mat} {tool}"}
# What to hand over at the end of a collect order, as a loose item name.
DELIVER_ITEM = {"diamonds": "diamond", "iron": "iron", "coal": "coal", "wood": "log",
                "cobblestone": "cobblestone"}

ORDER_MODES = [
    (r"\b(stop|quit|don'?t) follow", "resume"),
    (r"\b(go (play|have fun)|do your (own )?thing|you'?re free|carry on|resume|as you were"
     r"|do whatever|back to (work|playing))\b", "resume"),
    (r"\bfollow( me)?\b|\bstick (with|close to) me\b|\bcome with me\b|\bstay close\b", "follow"),
    (r"\b(come (here|back)|over here|get over here|return to me|back to me|where are you)\b", "come"),
    (r"\b(stay( here| put)?|wait( here)?|stop|hold (on|still)|don'?t move|freeze|halt)\b", "stay"),
]

# (pattern, label, inventory items counted, default amount)
ORDER_COLLECT = [
    (r"diamond", "diamonds", ["diamond"], 3),
    (r"\biron\b", "iron", ["raw_iron", "iron_ingot"], 6),
    (r"\bcoal\b", "coal", ["coal"], 8),
    (r"\b(wood|logs?|trees?|timber)\b", "wood", _LOGS, 16),
    (r"\b(cobble(stone)?|stone|rocks?)\b", "cobblestone", _STONE_MATS, 32),
]

ORDER_TASKS = [
    (r"\b(make|craft|get|build)\b.*\bbed\b", "get_bed"),
    (r"\b(set|make|change)\b.*\b(re)?spawn\b|\b(re)?spawn ?point\b", "set_spawn"),
    (r"\b(sleep|go to bed|bed ?time|get some rest|nap)\b", "sleep_in_bed"),
    (r"\b(go|head|run) home\b|\bto (the|your) house\b", "go_home"),
    (r"\b(build|make)\b.*\b(house|home|base)\b|\bhouse\b", "build_house"),
    (r"\btorch", "craft_torches"),
    (r"\b(sort|organi[sz]e|tidy|chest|inventory)\b|\bput (stuff|things|it|everything) away\b",
     "organize_inventory"),
    (r"\bcook\b", "cook_meat"),
    (r"\b(food|hungry|hunt|meat|cows?|pigs?)\b", "hunt_food"),
    (r"\b(shelter|hut|hide)\b", "build_dirt_hut"),
    (r"\bfurnace\b", "craft_furnace"),
    (r"\b(branch ?mine|mine|mining|dig|ores?|caves?)\b", "branch_mine"),
    (r"\b(explore|look around|wander|scout)\b", "explore"),
]


def _parse_order(text: str) -> Optional[Dict[str, Any]]:
    """Turn an order into {kind, ...}, or None if it isn't recognized."""
    t = text.lower()
    for pattern, mode in ORDER_MODES:
        if re.search(pattern, t):
            return {"kind": mode}
    deliver = bool(DELIVER_RE.search(t))
    give = GIVE_RE.search(t)
    if give:
        rest = give.group(1)
        n = re.search(r"\b(\d{1,3})\b", rest)
        item = " ".join(GIVE_FILLER.sub(" ", re.sub(r"\b\d{1,3}\b", " ", rest)).split())
        if item:
            return {"kind": "give", "item": item,
                    "count": min(max(int(n.group(1)), 1), 64) if n else None}
    tool = _tool_order(t, deliver)
    if tool:
        return tool
    for pattern, label, items, default in ORDER_COLLECT:
        if re.search(pattern, t):
            n = re.search(r"\b(\d{1,3})\b", t)
            return {"kind": "collect", "label": label, "items": items, "deliver": deliver,
                    "amount": min(max(int(n.group(1)), 1), 64) if n else default}
    for pattern, goal in ORDER_TASKS:
        if re.search(pattern, t):
            return {"kind": "house"} if goal == "build_house" else {"kind": "once", "goal": goal}
    return None


def _held_item(inv: Dict[str, int], term: str) -> Optional[str]:
    """The inventory item a loose name refers to (mirrors bot.js's
    resolveItem), or None."""
    t = "_".join(term.lower().split())
    for cand in (t, t[:-1] if t.endswith("s") else t, t[:-2] if t.endswith("es") else t):
        if inv.get(cand):
            return cand
    word = t[:-1] if t.endswith("s") else t
    names = [n for n in inv if word in n]
    return max(names, key=lambda n: inv[n]) if names else None


def _give_fallback(item: str, deliver_count: Optional[int]) -> Optional[Dict[str, Any]]:
    """An order to get an item she doesn't have yet, then hand it over."""
    tool = _tool_order(item, True)
    if tool:
        return tool
    for pattern, label, items, default in ORDER_COLLECT:
        if re.search(pattern, item):
            return {"kind": "collect", "label": label, "items": items, "deliver": True,
                    "amount": deliver_count or default}
    return None


# When an order's next step is resting after failures, try this instead.
ORDER_ALTERNATIVE = {"mine_iron_ore": "branch_mine", "mine_coal": "branch_mine",
                     "gather_cobblestone": "branch_mine", "mine_stone": "branch_mine",
                     "chop_trees": "explore", "collect_wood": "explore"}


def _tool_step(tier: int, inv: Dict[str, int], near: Dict[str, bool]) -> Optional[str]:
    """The next goal toward a pickaxe of this tier."""
    tech = _tech_goal(inv, near)
    if tech or tier <= 2:
        return tech
    return _progress_goal(inv, near) or "mine_iron_ore"


def _collect_step(label: str, inv: Dict[str, int], near: Dict[str, bool]) -> Optional[str]:
    """The next goal toward an item order, working through the tech tree
    when a better pickaxe is needed first."""
    pick, tech = _pickaxe(inv), _tech_goal(inv, near)
    if label == "wood":
        return "chop_trees"
    if label == "cobblestone":
        return "gather_cobblestone" if pick >= 1 else tech
    if label == "coal":
        return "mine_coal" if pick >= 1 else tech
    if label == "iron":
        return "mine_iron_ore" if pick >= 2 else tech
    if label == "diamonds":
        if pick < 2:
            return tech
        if pick < 3:     # diamonds need an iron pickaxe
            return _progress_goal(inv, near) or "mine_iron_ore"
        return "branch_mine"
    return None


OVERRIDE_SYSTEM = (
    "You control a Minecraft player. Your player just gave you an order. "
    "Choose the one goal from the list that best carries it out. If the message "
    "isn't actually an order for you, choose \"none\". Reply with JSON only: "
    '{"goal": "<one of the listed names, or none>", "reason": "<one short sentence>"}'
)


# ─── Agent ────────────────────────────────────────────────────────────────────

class MinecraftAgent(LumiClient):
    """Autonomous Minecraft agent. Registers four SDK actions so Lumi can
    observe and override the loop, but the loop runs whether or not Lumi's
    agent mode is on."""

    def __init__(self) -> None:
        super().__init__(
            game="MinecraftAgent",
            token_path=TOKEN_PATH,
            reconnect_delay=5.0,
        )
        os.makedirs(MC_DIR, exist_ok=True)
        self._paused = False
        self._override_goal: Optional[str] = None
        self._loop_task: Optional[asyncio.Task] = None
        self._recent_goals: List[Tuple[str, bool]] = []   # (goal, succeeded), newest last
        self._failures: Dict[str, int] = {}                # consecutive failures per goal
        self._cooldown_until: Dict[str, float] = {}        # goal -> time it may run again
        self._hut_this_night = False
        self._progress = 0
        self._wanted: Dict[str, int] = {}      # materials a failed house build asked for
        self._project_until = 0.0              # long leash until then
        self._band: Optional[str] = None       # near / roaming / far / out of sight
        self._order: Optional[Dict[str, Any]] = None   # the player's current order
        # Attached: follow the player between orders. "do your own thing"
        # detaches her; "follow me" or "come here" attaches her again.
        self._attached = DEFAULT_MODE != "free"
        self._mode: Optional[str] = "follow" if self._attached else None   # "follow" or "stay"
        self._done_order: Optional[Tuple[Dict[str, Any], str]] = None   # finished, not yet announced
        self._last_narration = 0.0
        self._last_status_push = 0.0
        self._last_done: Optional[str] = None
        self._autoplay_was = True
        self._defend_backoff_until = 0.0
        self._llm_ok = chat_sync is not None and GOAL_PICKER == "llm" and bool(GOAL_MODEL)
        self._tel: Dict[str, Any] = {
            "bot_status": "unreachable",
            "lumi_sdk": "offline",
            "position": None,
            "health": None,
            "food": None,
            "inventory": [],
            "current_goal": None,
            "goal_description": None,
            "goal_source": None,
            "current_thought": "Starting up...",
            "current_order": None,
            "mode": None,
            "loop_state": "starting",
            "last_update": None,
            "skills_verified": 0,
            "errors": [],
        }

    # ─── SDK setup ────────────────────────────────────────────────

    async def setup(self) -> None:
        self.register_action(
            action("mc_agent_status",
                   "Get the Minecraft autonomous agent's current goal, loop state, skill progress, and live bot telemetry.")
            .capabilities("read").build(),
            self._h_status,
        )
        self.register_action(
            action("mc_agent_set_goal",
                   "Tell the Minecraft autonomous agent what to do next.")
            .param("goal", "string",
                   description="Goal name: " + ", ".join(_all_goal_names()),
                   required=True)
            .capabilities("write").build(),
            self._h_set_goal,
        )
        self.register_action(
            action("mc_agent_pause",
                   "Pause the Minecraft agent's autonomous skill loop.")
            .capabilities("write").build(),
            self._h_pause,
        )
        self.register_action(
            action("mc_agent_resume",
                   "Resume the Minecraft agent's autonomous skill loop.")
            .capabilities("write").build(),
            self._h_resume,
        )
        await self.send_context(
            "Minecraft autonomous agent is online and playing on its own: "
            "it keeps itself alive, works through the tool curriculum, and "
            "picks free-play goals (chop trees, hunt, explore, build a hut at night). "
            "Use mc_agent_status for live progress and mc_agent_set_goal to steer it.",
            silent=True,
        )
        self._tel["lumi_sdk"] = "connected"
        self._record("sdk", "Connected to Lumi SDK — status/control actions registered")

    async def on_disconnect(self) -> None:
        self._tel["lumi_sdk"] = "offline"
        self._record("sdk", "Disconnected from Lumi SDK — skill loop keeps running")

    def start_loop(self) -> None:
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.create_task(self._skill_loop())

    async def run(self) -> None:
        """Quiet reconnect: probe the port first so a missing Lumi doesn't
        spam the terminal. The skill loop runs regardless."""
        warned = False
        while not self._stop:
            if await self._lumi_port_open():
                warned = False
                try:
                    await self._connect_and_loop()
                except Exception as e:
                    self._record("error", f"Lumi SDK connection error: {e}")
            elif not warned:
                print("[mc-agent] Lumi is not running (127.0.0.1:5056) — skill loop "
                      "runs anyway; will attach to Lumi when it starts")
                self._tel["lumi_sdk"] = "offline"
                warned = True
            await asyncio.sleep(self.reconnect_delay)

    async def _lumi_port_open(self) -> bool:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=1.0)
            writer.close()
            return True
        except Exception:
            return False

    # ─── SDK handlers ─────────────────────────────────────────────

    async def _h_status(self, _args: Dict) -> tuple:
        t = self._tel
        inv = ", ".join(f'{i["name"]}×{i["count"]}' for i in t["inventory"][:8]) or "empty"
        pos = t["position"]
        pos_str = f'{pos["x"]}, {pos["y"]}, {pos["z"]}' if pos else "unknown"
        lines = [
            f'Loop: {t["loop_state"]}{" (paused)" if self._paused else ""}',
            f'Bot: {t["bot_status"]}  at {pos_str}',
            f'Health: {t["health"] or "?"}/20  Food: {t["food"] or "?"}/20  Armor: {t.get("armor") or 0}/20',
            f'Hotbar: {self._hotbar_text()}',
            f'Goal: {t["current_goal"] or "none"} — {t["goal_description"] or ""}'
            + (f' (picked by {t["goal_source"]})' if t["goal_source"] else ""),
            f'Recent goals: {self._recent_text(5)}',
            f'Resting: {self._cooldown_text()}',
            f'Order: {self._order_text() or "none"}',
            f'Inventory: {inv}',
            f'Tech progress: {self._progress}/{len(CURRICULUM)}',
        ]
        return True, "\n".join(lines)

    def _hotbar_text(self) -> str:
        """Hotbar slots 1-9 in order, with the held one marked."""
        bar, held = self._tel.get("hotbar") or [], self._tel.get("held_slot")
        if not bar:
            return "unknown"
        return ", ".join(f"{i + 1}:{name or '-'}{' (held)' if i == held else ''}"
                         for i, name in enumerate(bar))

    async def _h_set_goal(self, args: Dict) -> tuple:
        name = str(args.get("goal") or "").strip()
        if name not in _all_goal_names():
            return False, f"Unknown goal '{name}'. Valid: {_all_goal_names()}"
        self._override_goal = name
        self._record("agent", f"Goal overridden to: {name}")
        return True, f"Goal set to: {name}"

    async def _h_pause(self, _args: Dict) -> tuple:
        self._paused = True
        self._tel["loop_state"] = "paused"
        self._think("Paused. Just standing here.")
        self._record("agent", "Loop paused")
        return True, "Paused"

    async def _h_resume(self, _args: Dict) -> tuple:
        self._paused = False
        self._tel["loop_state"] = "running"
        self._record("agent", "Loop resumed")
        return True, "Resumed"

    # ─── Bot control API (sync calls run in executor) ──────────────

    async def _bot_get(self, path: str) -> Optional[Dict]:
        loop = asyncio.get_running_loop()
        try:
            def _fetch():
                with urllib.request.urlopen(f"{BOT_API}{path}", timeout=4) as r:
                    return json.loads(r.read())
            return await loop.run_in_executor(None, _fetch)
        except urllib.error.URLError:
            return None
        except Exception as e:
            self._record("error", f"GET {path}: {e}")
            return None

    async def _bot_action(self, action_name: str, args: Dict) -> Dict:
        loop = asyncio.get_running_loop()
        try:
            def _post():
                body = json.dumps({"action": action_name, "args": args}).encode()
                req = urllib.request.Request(
                    f"{BOT_API}/action", data=body,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=ACTION_TIMEOUT) as r:
                    return json.loads(r.read())
            return await loop.run_in_executor(None, _post)
        except urllib.error.URLError as e:
            self._record("error", f"bot offline during action '{action_name}': {e}")
            return {"success": False, "message": "bot unreachable"}
        except Exception as e:
            self._record("error", f"action '{action_name}': {e}")
            return {"success": False, "message": str(e)}

    # ─── Autonomous loop ───────────────────────────────────────────

    async def _skill_loop(self) -> None:
        self._tel["loop_state"] = "running"
        if self._llm_ok:
            loop = asyncio.get_running_loop()
            missing = await loop.run_in_executor(None, _missing_ollama_model, GOAL_MODEL)
            if missing:
                self._llm_ok = False
                self._record("warn", f"game model {GOAL_MODEL} is not installed — picking goals "
                                     f"at random. Run: ollama pull {missing}")
        self._record("agent", "Skill loop started"
                     + (f" (goal picker: {GOAL_MODEL})" if self._llm_ok else " (goal picker: random)"))

        while True:
            try:
                await asyncio.sleep(LOOP_INTERVAL)
                if not self._autoplay_allowed():
                    continue
                if self._paused:
                    continue

                # 1. Assess — get bot status
                status = await self._bot_get("/status")
                if not status:
                    self._tel["bot_status"] = "unreachable"
                    self._tel["loop_state"] = "waiting for bot"
                    self._think("Waiting for the game to start...")
                    continue

                self._tel.update({
                    "bot_status":  "ready" if status.get("ready") else "spawning",
                    "position":    status.get("position"),
                    "health":      status.get("health"),
                    "food":        status.get("food"),
                    "inventory":   status.get("inventory", []),
                    "armor":       status.get("armor"),
                    "held_slot":   status.get("held_slot"),
                    "hotbar":      [(s or {}).get("name") for s in (status.get("slots") or [])[:9]],
                    "last_update": time.strftime("%H:%M:%S"),
                    "loop_state":  "running",
                })
                if status.get("is_day", True):
                    self._hut_this_night = False
                self._progress = _progress_count(_inv(status), status.get("nearby") or {})
                self._flush_telemetry()
                band_changed = self._update_band(status)
                await self._push_status(status, force=band_changed)

                if not status.get("ready"):
                    self._think("Waiting to spawn in...")
                    continue

                # New orders from the player, every tick.
                await self._take_directive(status)

                # In bed: bot.js keeps her there until morning and gets her
                # up for an order, a hit or the player wandering off.
                if status.get("sleeping"):
                    self._tel["current_goal"], self._tel["goal_description"] = "sleep_in_bed", "Sleeping"
                    self._think("Zzz. Sleeping until morning.")
                    continue

                # 2. Survive — fights and meals come before any goal
                if await self._survive(status):
                    continue

                # Standing modes from an order: stay put, or follow closely.
                if await self._hold_mode(status):
                    continue

                # 3. Pick a goal
                picked = await self._pick_goal(status)
                if picked is None:
                    self._think("Nothing I can do right now. Taking a breather.")
                    await asyncio.sleep(10)
                    continue
                goal_name, source, reason = picked
                desc = _goal_desc(goal_name)
                self._tel.update({"current_goal": goal_name, "goal_description": desc,
                                  "goal_source": source})
                self._think(desc + (f" — {reason}" if reason else ""))
                self._record("curriculum", f"Goal: {desc} ({source}{': ' + reason if reason else ''})")
                await self._narrate(
                    f"I'm starting something new: {desc.lower()}."
                    + (f" Why: {reason}" if reason else ""))
                if goal_name in PROJECT_GOALS:
                    self._project_until = time.time() + PROJECT_HOLD_S
                await self._push_status(status, force=True)

                # 4. Execute
                ok, msg = await self._execute_goal(goal_name, status)
                if not ok and "interrupted by" in msg:
                    # Not a failure: a new order, or a survival emergency.
                    self._record("agent", f"{goal_name} {msg}")
                    self._tel["current_goal"] = None
                    continue
                self._remember_goal(goal_name, ok)
                if source == "order":
                    await self._order_result(goal_name, ok, msg)

                # 5. Verify and record
                if ok:
                    self._failures.pop(goal_name, None)
                    self._record("result", f"OK {goal_name}: {msg}")
                    self._think(f"Done: {desc.lower()}. What next?")
                    self._last_done = desc
                    if goal_name in SUCCESS_COOLDOWN_S:
                        self._cooldown_until[goal_name] = time.time() + SUCCESS_COOLDOWN_S[goal_name]
                    if goal_name == "build_dirt_hut":
                        self._hut_this_night = True
                    if goal_name == "build_house" and msg.startswith("Built a"):
                        self._wanted.clear()
                        await self._narrate("I just finished building a real house! "
                                            "Floor, walls, windows, a roof, the works.", major=True)
                    elif goal_name == "build_house":
                        self._wanted.clear()
                        self._project_until = time.time() + PROJECT_HOLD_S   # keep at it
                    if goal_name == "return_to_player":
                        self._project_until = 0.0
                        await self._push_status(status, force=True)
                    if goal_name == "retrieve_items":
                        await self._narrate(f"I went back to where I died and got my stuff back. {msg}",
                                            major=True)
                    if goal_name == "mine_diamond" or (goal_name == "branch_mine" and "diamond" in msg):
                        await self._narrate("I just found and mined diamonds!", major=True)
                    elif goal_name in _TIERS:
                        await self._narrate(f"I just finished a milestone: {desc.lower()}.")
                    self._tel["current_goal"] = None
                else:
                    n = self._failures[goal_name] = self._failures.get(goal_name, 0) + 1
                    self._record("error", f"FAIL {goal_name} (x{n}): {msg}")
                    missing = re.search(r"Missing materials: (\w+)×(\d+)", msg)
                    if missing:
                        self._wanted[missing.group(1)] = int(missing.group(2))
                        self._record("agent", f"house needs {missing.group(2)} more {missing.group(1)}")
                    self._think(f"That didn't work ({desc.lower()}): {msg[:120]}")
                    if goal_name in FAIL_REST_S:
                        self._cooldown_until[goal_name] = time.time() + FAIL_REST_S[goal_name]
                    if n >= FAIL_STREAK:
                        self._failures.pop(goal_name, None)
                        self._cooldown_until[goal_name] = time.time() + FAIL_COOLDOWN_S
                        self._record("warn", f"{goal_name} failed {n} times in a row — "
                                             f"resting it for {FAIL_COOLDOWN_S // 60} min")
                        await self._narrate(f"I keep failing at this: {desc.lower()}. "
                                            f"The game says: {msg[:160]}")
                    # Back off before retrying
                    await asyncio.sleep(5)

                self._flush_telemetry()

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._record("error", f"Loop exception: {e}")
                await asyncio.sleep(5)

        self._tel["loop_state"] = "stopped"
        self._flush_telemetry()

    async def _survive(self, status: Dict) -> bool:
        """Handle immediate danger and hunger. Returns True if it acted."""
        # bot.js handles drowning and lava itself and reports it here; every
        # goal waits until it's over.
        emergency = status.get("emergency")
        if emergency:
            env = status.get("environment") or {}
            self._tel["current_goal"], self._tel["goal_description"] = "survive", emergency
            self._think(f"Swimming up for air! Oxygen {env.get('oxygen', '?')}/20." if emergency == "drowning"
                        else "Lava! Getting away from it.")
            await asyncio.sleep(1)
            return True
        threats = [t for t in status.get("threats") or []
                   if t.get("distance", 99) <= DEFEND_RANGE and t.get("name") != "enderman"]
        if threats and not status.get("defending") and time.time() >= self._defend_backoff_until:
            self._tel["current_goal"], self._tel["goal_description"] = "defend", f"Fight off a {threats[0]['name']}"
            self._record("survival", f"{threats[0]['name']} at {threats[0]['distance']} blocks — defending")
            self._think(f"A {threats[0]['name'].replace('_', ' ')} is right there! Fighting it off.")
            r = await self._bot_action("defend", {})
            self._record("result" if r.get("success") else "error", f"defend: {r.get('message', '')}")
            if not r.get("success"):
                # Probably unreachable (a skeleton on a cliff): get on with
                # goals for a minute; bot.js still fights back if hit.
                self._defend_backoff_until = time.time() + 60
            return True

        inv = _inv(status)
        food = status.get("food")
        if food is not None and food < EAT_BELOW_FOOD and _has(inv, _EDIBLE):
            self._think(f"Hunger is at {food}/20. Snack time.")
            r = await self._bot_action("eat_food", {})
            self._record("survival", f"eat_food: {r.get('message', '')}")
            return bool(r.get("success"))
        return False

    async def _execute_goal(self, goal: str, status: Dict) -> Tuple[bool, str]:
        if goal == "give_item":
            give = dict((self._order or {}).get("give") or {})
            if not give:
                return False, "Nothing to give"
            self._record("skill", f"Executing: give_item ({give})")
            r = await self._bot_action("give_item", give)
            return bool(r.get("success")), r.get("message", "")
        spec = FREE_GOALS.get(goal, {}).get("action")
        if spec:
            # A plain bot.js action (house, branch mine, chests, return).
            name, args = spec
            self._record("skill", f"Executing: {goal} ({name})")
            r = await self._bot_action(name, args(status) if callable(args) else dict(args))
            return bool(r.get("success")), r.get("message", "")
        code = _skill_code(goal, status)
        if not code:
            return False, f"No skill code for '{goal}'"
        self._record("skill", f"Executing: {goal}")
        args = {"code": code, "description": goal}
        if goal in GOAL_TIMEOUT_S:
            args["timeout_ms"] = GOAL_TIMEOUT_S[goal] * 1000
        result = await self._bot_action("execute_skill", args)
        msg = result.get("message", "")
        if result.get("success") or "primitives not loaded" not in msg:
            return bool(result.get("success")), msg

        # Voyager is missing: run the goal as plain bot.js actions instead.
        steps = _goal_steps(goal, status)
        if not steps:
            return False, f"{msg} (no mineflayer fallback for {goal})"
        self._record("skill", f"Voyager unavailable — running {goal} as {len(steps)} plain action(s)")
        out = []
        for name, step_args, *optional in steps:
            r = await self._bot_action(name, step_args)
            out.append(r.get("message", ""))
            if not r.get("success") and not optional:
                return False, " | ".join(out)
        return True, " | ".join(out)

    # ─── Goal choice ───────────────────────────────────────────────

    async def _pick_goal(self, status: Dict) -> Optional[Tuple[str, str, str]]:
        """Return (goal, source, reason), or None if nothing can run."""
        if self._override_goal:
            goal, self._override_goal = self._override_goal, None
            return goal, "Lumi", ""

        # Items dropped on death despawn after 5 minutes: getting them back
        # comes before everything else.
        death = status.get("death") or {}
        if death.get("recoverable") and self._available("retrieve_items", status):
            return ("retrieve_items", "routine",
                    f"I died at {death['x']:.0f}, {death['y']:.0f}, {death['z']:.0f} "
                    f"{death['age_s']} s ago; my items are still there")

        inv = _inv(status)
        near = status.get("nearby") or {}

        # The player's order beats wandering and the leash.
        if self._order:
            step = self._order_step(status)
            if step:
                return step, "order", self._order["text"]
        await self._announce_done_order()

        # Beds: sleep at night, and set the respawn point at one by day.
        bed = _bed_routine(inv, status)
        if bed and self._available(bed, status):
            return bed, "routine", ("it's night" if bed == "sleep_in_bed"
                                    else "so I respawn here instead of at world spawn")

        # Semi-magnet: wander, but come back when called or too far.
        back = self._should_return(status)
        if back:
            return "return_to_player", "proximity", back

        # The basics are never left to the model.
        tech = _tech_goal(inv, near)
        if tech and self._available(tech, status):
            return tech, "tech tree", _TECH_REASON[tech]
        kit = _kit_goal(inv, near)
        if kit and self._available(kit[0], status):
            return kit[0], "tech tree", kit[1]
        # Then a bed, by day, like a player's first day.
        if (_pickaxe(inv) >= 2 and status.get("is_day", True)
                and FREE_GOALS["get_bed"]["when"](inv, status) and self._available("get_bed", status)):
            return "get_bed", "tech tree", "a bed to set my respawn point and sleep through the night"

        # Routines, also decided in code: home at night, a full inventory.
        gate_status = dict(status, wanted=self._wanted)
        for routine in ("go_home", "organize_inventory"):
            if FREE_GOALS[routine]["when"](inv, gate_status) and self._available(routine, status):
                return routine, "routine", ""

        options: Dict[str, str] = {}
        progress = _progress_goal(inv, near)
        if progress and self._available(progress, status):
            options[progress] = f"{_goal_desc(progress)} (next step toward better tools)"
        for name, g in FREE_GOALS.items():
            try:
                if g["when"](inv, gate_status) and self._available(name, status):
                    options[name] = g["desc"]
            except Exception:
                pass
        if not options:
            return None

        if self._llm_ok:
            picked = await self._llm_pick(status, options)
            if picked:
                return picked[0], "llm", picked[1]
        return self._random_pick(status, options, progress), "random", ""

    # ─── Orders ────────────────────────────────────────────────────

    async def _take_directive(self, status: Dict) -> None:
        """Pick up a pending order from bot.js and turn it into a mode, an
        objective, or a single goal."""
        if not status.get("directive_pending"):
            return
        got = await self._bot_get("/directive?take=1")
        d = (got or {}).get("directive")
        text = re.sub(r"\s+", " ", str((d or {}).get("text") or "")).strip()[:200]
        if not text:
            return
        in_game = d.get("source") == "chat"
        # Said to Lumi in her chat or out loud: she has already answered,
        # so only a problem with the order is narrated.
        talk = d.get("source") == "talk"
        who = (d.get("from") or (status.get("owner") or {}).get("name") or "my player") if in_game else None
        heard = (f'{who} told me in game: "{text}"' if in_game
                 else f'I was asked to "{text}" in Minecraft' if talk
                 else f'I got an order from the Games page: "{text}"')
        self._record("agent", f"order ({who or ('talking to Lumi' if talk else 'Games page')}): {text}")

        order = _parse_order(text)
        if order is None:
            order = await self._override_order(text, status)
        if order is None:
            await self._bot_action("chat", {"message": f"Sorry, I don't know how to do that: {text[:60]}"})
            self._record("warn", f"didn't understand the order: {text}")
            if talk:
                await self._narrate(f"{heard}, but I don't know how to do that in the game.", major=True)
            return

        if order["kind"] == "give":
            inv = _inv(status)
            item = _held_item(inv, order["item"])
            if item:
                # A number wins; "the diamonds" (plural) means all of them;
                # otherwise one.
                plural = order["item"].endswith("s") and not item.endswith("s")
                count = order["count"] or (inv[item] if plural else 1)
                order = {"kind": "once", "goal": "give_item",
                         "give": {"item_name": item, "count": min(count, inv[item])}}
            else:
                fallback = _give_fallback(order["item"], order["count"])
                if fallback is None:
                    await self._bot_action("chat", {"message": f"I don't have any {order['item']}."})
                    self._record("warn", f"can't give {order['item']}: not in inventory")
                    if talk:
                        await self._narrate(f"{heard}, but I don't have any {order['item']}.", major=True)
                    return
                order = fallback

        if order.get("goal") in ("sleep_in_bed", "set_spawn") and not _bed_handy(_inv(status), status):
            # No bed: make one; the bed routine sleeps or sets the spawn after.
            order = {"kind": "once", "goal": "get_bed"}
            await self._bot_action("chat", {"message": "I don't have a bed. I'll make one first."})
        elif order.get("goal") == "sleep_in_bed" and not status.get("can_sleep"):
            # "go to sleep" by day: the closest thing a bed can do now.
            order = {"kind": "once", "goal": "set_spawn"}
            await self._bot_action("chat", {"message": "I can only sleep at night. I'll set my spawn at the bed."})

        kind = order["kind"]
        if kind == "resume":
            self._order, self._mode, self._attached = None, None, False
            ack = "I'm free to do my own thing again"
        elif kind in ("follow", "stay"):
            self._order, self._mode = None, kind
            if kind == "follow":
                self._attached = True
            ack = "I'll follow them around" if kind == "follow" else "I'll stay right here"
        elif kind == "come":
            self._attached = True
            self._mode = "follow"
            self._order = {"kind": "once", "goal": "return_to_player"}
            ack = "I'm heading back to them"
        else:
            # An order comes first; when attached, she follows again after.
            self._mode = "follow" if self._attached else None
            self._order = order
            if kind == "collect":
                order["start"] = sum(_inv(status).get(i, 0) for i in order["items"])
                ack = f"I'm going to get {order['amount']} {order['label']}" + \
                      (" and bring them over" if order.get("deliver") else "")
                if order.get("deliver"):
                    order["give"] = {"item_name": DELIVER_ITEM[order["label"]], "count": order["amount"]}
            elif kind == "tool":
                ack = f"I'm going to make {'an' if order['label'][0] in 'aeiou' else 'a'} {order['label']}" + \
                      (" and hand it over" if order.get("deliver") else "")
                if order.get("deliver"):
                    order["give"] = {"item_name": order["item"], "count": 1}
            elif kind == "once" and order["goal"] == "give_item":
                g = order["give"]
                ack = f"Here, I'll bring over {g['count']} {g['item_name'].replace('_', ' ')}"
            elif kind == "house":
                ack = "I'm going to build a house"
            else:
                ack = f"On it: {_goal_desc(order['goal']).lower()}"
        if self._order:
            self._order.update(text=text, who=who, fails=0, until=time.time() + 20 * 60)
        self._tel["current_order"], self._tel["mode"] = self._order_text() or None, self._mode
        self._think(f"New order: {text}")
        if not talk:
            await self._narrate(f"{heard}. {ack}.", major=True)
        await self._push_status(status, force=True)

    async def _override_order(self, text: str, status: Dict) -> Optional[Dict[str, Any]]:
        """Ask the game model which goal carries out an order the parser
        didn't recognize. None if there's no model or it isn't an order."""
        if not self._llm_ok:
            return None
        options = {g: d["desc"] for g, d in FREE_GOALS.items()}
        options.update({t["goal"]: t["desc"] for t in CURRICULUM})
        prompt = (
            f"The user has given you a new direct order: '{text}'. You must prioritize "
            "this above all current goals.\n"
            f"Your inventory contains: {', '.join(f'{n}×{c}' for n, c in _inv(status).items()) or 'nothing'}. "
            "If the order is about something you already have, don't gather or craft it again.\n"
            "Goals:\n" + "\n".join(f"- {k}: {v}" for k, v in options.items()) + "\n- none: not an order"
        )
        loop = asyncio.get_running_loop()
        try:
            resp = await loop.run_in_executor(None, lambda: chat_sync(
                GOAL_MODEL, [{"role": "system", "content": OVERRIDE_SYSTEM},
                             {"role": "user", "content": prompt}],
                think=False, keep_alive=GOAL_KEEP_ALIVE, options={"temperature": 0.2, "num_ctx": 2048}))
            reply = (resp.content or "").strip()
        except Exception as e:
            self._record("warn", f"order model failed: {e}")
            return None
        m = re.search(r"\{.*\}", reply, re.S)
        try:
            goal = str(json.loads(m.group(0)).get("goal") or "").strip() if m else ""
        except json.JSONDecodeError:
            goal = ""
        if goal not in options:
            return None
        return {"kind": "house"} if goal == "build_house" else {"kind": "once", "goal": goal}

    def _order_step(self, status: Dict) -> Optional[str]:
        """The next goal for the current order, or None (and the order is
        cleared) once it's done or expired."""
        o, inv = self._order, _inv(status)
        if time.time() > o["until"]:
            self._record("warn", f"order expired: {o['text']}")
            self._order = None
            return None
        if o["kind"] == "once":
            return o["goal"]
        if o.get("delivering"):
            return "give_item"
        if o["kind"] == "house":
            if _house(status).get("complete"):
                self._finish_order_later("the house is built")
                return None
            if self._wanted.get("cobblestone"):
                return "gather_cobblestone"
            if self._wanted.get("oak_planks") or self._wanted.get("planks"):
                return "chop_trees"
            return "build_house"
        near = status.get("nearby") or {}
        if o["kind"] == "tool":
            # To hand one over she needs that exact tool; for herself, the
            # tier or better is enough.
            reached = (inv.get(o["item"], 0) > 0 if o.get("deliver")
                       else _tool_level(inv, o["tool"]) >= _TOOL_TIER[o["mat"]])
            what = f"made {'an' if o['label'][0] in 'aeiou' else 'a'} {o['label']}"
            step = None if reached else _craft_step(o["mat"], o["tool"], inv, near)
        else:
            reached = sum(inv.get(i, 0) for i in o["items"]) - o["start"] >= o["amount"]
            what = f"got {o['amount']} {o['label']}"
            step = None if reached else _collect_step(o["label"], inv, near)
        if reached:
            if o.get("deliver"):
                o["delivering"] = True       # one more step: hand it over
                return "give_item"
            self._finish_order_later(what)
            return None
        if step and not self._available(step, status):
            step = ORDER_ALTERNATIVE.get(step, step)
        return step

    def _finish_order_later(self, what: str) -> None:
        """Mark the order done; the brag goes out from the loop."""
        self._record("result", f"order done ({what}): {self._order['text']}")
        self._done_order = (self._order, what)
        self._order = None
        self._tel["current_order"] = None

    async def _order_result(self, goal: str, ok: bool, msg: str) -> None:
        o = self._order
        if not o:
            return
        if ok:
            o["fails"] = 0          # three failures in a row, not in total
            if o["kind"] == "once" or goal == "give_item":
                self._finish_order_later(msg if goal == "give_item" else _goal_desc(goal).lower())
        else:
            o["fails"] += 1
            if o["fails"] >= FAIL_STREAK:
                self._record("warn", f"giving up on the order: {o['text']}")
                self._order = None
                self._tel["current_order"] = None
                await self._narrate(f"I failed my order: \"{o['text']}\". I am giving up. "
                                    f"(Last problem: {msg[:140]})", major=True)
        await self._announce_done_order()

    async def _announce_done_order(self) -> None:
        if self._done_order:
            order, what = self._done_order
            self._done_order = None
            await self._narrate(f'Done with the order "{order["text"]}": {what}.', major=True)

    async def _hold_mode(self, status: Dict) -> bool:
        """Follow or stay, from an order. True if this step was used."""
        if self._mode == "stay":
            self._think("Staying put like I was told.")
            return True
        if self._mode == "follow":
            # An order, a goal from Lumi, or getting her items back after a
            # death (they despawn) comes before following.
            death = status.get("death") or {}
            bed = _bed_routine(_inv(status), status)
            if (self._order or self._override_goal
                    or (death.get("recoverable") and self._available("retrieve_items", status))
                    or (bed and self._available(bed, status))):
                return False
            owner = status.get("owner")
            if not owner:
                self._think("Waiting for my player to come back online.")
                return True
            d = _owner_distance(status)
            if d is None or d > 5:
                self._think(f"Following {owner['name']}.")
                r = await self._bot_action("return_to_player", {"range": 3})
                if not r.get("success") and "interrupted" not in r.get("message", ""):
                    self._record("warn", f"follow: {r.get('message', '')}")
            else:
                self._think(f"Sticking close to {owner['name']}.")
            return True
        return False

    def _order_text(self) -> str:
        o = self._order
        if not o:
            return {"follow": "following the player", "stay": "staying put"}.get(self._mode or "", "")
        if o["kind"] == "collect":
            return f"\"{o['text']}\" ({o['label']}, {o['amount']} wanted)"
        return f"\"{o['text']}\""

    def _should_return(self, status: Dict) -> Optional[str]:
        """A reason to go back to the player, or None. Survival has already
        run by now, so a creeper always comes first."""
        owner = status.get("owner")
        if not owner or not self._available("return_to_player", status):
            return None
        name, d = owner.get("name"), _owner_distance(status)
        if owner.get("called") and (d is None or d > 4):
            return f"{name} called me"
        leash = PROJECT_LEASH if time.time() < self._project_until else FAR_DIST
        if d is None:
            return f"{name} is out of sight"
        if d > leash:
            return f"{name} is {d:.0f} blocks away"
        return None

    def _update_band(self, status: Dict) -> bool:
        """Track near / roaming / far, and say so in the log on a change.
        Returns True on a change so the silent status goes out right away."""
        owner = status.get("owner")
        if not owner:
            band = None
        elif _owner_distance(status) is None:
            band = "out of sight"
        else:
            d = _owner_distance(status)
            band = "near" if d < NEAR_DIST else "roaming" if d <= FAR_DIST else "far"
        if band == self._band:
            return False
        if band and self._band:
            self._record("agent", f"{owner['name']}: {self._band} → {band}")
        self._band = band
        return True

    def _available(self, goal: str, status: Dict) -> bool:
        """Not resting after a success or a failure streak, and (for the
        hut) not already built tonight."""
        if time.time() < self._cooldown_until.get(goal, 0):
            return False
        if goal == "build_dirt_hut" and self._hut_this_night:
            return False
        return True

    def _random_pick(self, status: Dict, options: Dict[str, str], progress: Optional[str]) -> str:
        if not status.get("is_day", True):
            for night_goal in ("build_dirt_hut", "wait_out_night"):
                if night_goal in options:
                    return night_goal
        if (status.get("food") or 20) < EAT_BELOW_FOOD and "hunt_food" in options:
            return "hunt_food"
        if "craft_torches" in options and "branch_mine" in options:
            return "craft_torches"     # light before going underground
        if progress in options and random.random() < 0.7:
            return progress
        weights = {"chop_trees": 3, "explore": 2, "hunt_food": 1, "mine_coal": 2,
                   "cook_meat": 2, "build_dirt_hut": 0, "wait_out_night": 0,
                   "branch_mine": 3, "build_house": 4, "gather_cobblestone": 4}
        just_did = {g for g, _ in self._recent_goals[-2:]}
        names = [n for n in options if n not in just_did] or list(options)
        return random.choices(names, weights=[weights.get(n, 1) or 0.1 for n in names])[0]

    async def _llm_pick(self, status: Dict, options: Dict[str, str]) -> Optional[Tuple[str, str]]:
        inv = _inv(status)
        inv_text = ", ".join(f"{n}×{c}" for n, c in sorted(inv.items(), key=lambda kv: -kv[1])[:20]) or "empty"
        threats = ", ".join(f'{t["name"]} at {t["distance"]}' for t in status.get("threats") or []) or "none"
        is_day = status.get("is_day", True)
        prompt = (
            f"Current inventory: {inv_text} (logs: {_logs(inv)}).\n"
            f"Current time: {'Day' if is_day else 'Night'}. "
            f"Health {_fmt(status.get('health'))}/20, hunger {_fmt(status.get('food'))}/20. "
            f"Hostiles nearby: {threats}.\n"
            f"{_environment_text(status.get('environment'))}\n"
            f"{self._player_line(status)}"
            f"Recent goals (oldest first, with result): {self._recent_text(5)}.\n"
            + (f"Materials wanted for the house: "
               f"{', '.join(f'{k}×{v}' for k, v in self._wanted.items())}.\n" if self._wanted else "")
            + "Rules: Do not pick build_dirt_hut if it is Day. Do not pick chop_trees if "
            "you already have 10+ logs. Do not repeat a goal that just succeeded unless "
            "nothing else makes sense.\n\n"
            "Goals you can pick:\n"
            + "\n".join(f"- {k}: {v}" for k, v in options.items())
        )
        messages = [{"role": "system", "content": PICKER_SYSTEM},
                    {"role": "user", "content": prompt}]
        loop = asyncio.get_running_loop()
        try:
            resp = await loop.run_in_executor(None, lambda: chat_sync(
                GOAL_MODEL, messages, think=False, keep_alive=GOAL_KEEP_ALIVE,
                options={"temperature": 0.7, "num_ctx": 2048}))
            text = (resp.content or "").strip()
        except Exception as e:
            self._record("warn", f"goal picker LLM failed ({e}) — using random picks for 5 min")
            self._llm_ok = False
            asyncio.get_running_loop().call_later(300, self._reenable_llm)
            return None
        m = re.search(r"\{.*\}", text, re.S)
        try:
            data = json.loads(m.group(0)) if m else {}
        except json.JSONDecodeError:
            data = {}
        goal = str(data.get("goal") or "").strip()
        if goal not in options:
            # Accept a bare goal name anywhere in the reply.
            goal = next((k for k in options if k in text), "")
        if not goal:
            self._record("warn", f"goal picker gave no valid goal: {text[:120]}")
            return None
        return goal, str(data.get("reason") or "").strip()[:160]

    def _player_line(self, status: Dict) -> str:
        owner = status.get("owner")
        if not owner:
            return ""
        d = _owner_distance(status)
        where = f"{d:.0f} blocks away" if d is not None else "out of sight"
        return (f"You are playing near {owner['name']} ({where}). If you get too far "
                f"({FAR_DIST}+ blocks) or they call you, go back to them. Otherwise, do "
                "your own thing nearby.\n")

    def _reenable_llm(self) -> None:
        self._llm_ok = chat_sync is not None and GOAL_PICKER == "llm" and bool(GOAL_MODEL)

    async def _narrate(self, text: str, major: bool = False) -> None:
        """Push an event Lumi reacts to out loud. Each one costs a turn of
        Lumi's chat model, so by default only major moments are sent;
        MC_NARRATE=all adds the rest, rate-limited."""
        if NARRATE == "off" or (NARRATE != "all" and not major):
            return
        now = time.time()
        if not major and now - self._last_narration < NARRATE_COOLDOWN:
            return
        self._last_narration = now
        await self.send_context(f"[MINECRAFT EVENT] {text}", silent=False)

    async def _push_status(self, status: Dict, force: bool = False) -> None:
        """Silent standing context: what Lumi knows about her other half
        without being interrupted. Replaced every STATUS_EVERY seconds."""
        now = time.time()
        if not force and now - self._last_status_push < STATUS_EVERY:
            return
        self._last_status_push = now
        held = (status.get("held") or "nothing").replace("_", " ")
        threats = ", ".join(t["name"] for t in (status.get("threats") or [])[:3]) or "none"
        doing = self._tel.get("current_thought") or "idle"
        lines = [
            "Your other half is playing Minecraft in the background.",
            f"Status: health {_fmt(status.get('health'))}/20, hunger {_fmt(status.get('food'))}/20, "
            f"holding {held}, {'day' if status.get('is_day', True) else 'night'}, "
            f"hostiles nearby: {threats}.",
            f"Currently: {doing}",
        ]
        env = status.get("environment") or {}
        if env.get("in_cave") or env.get("hazards") or env.get("emergency") or env.get("oxygen", 20) < 20:
            lines.append(_environment_text(env))
        owner = status.get("owner")
        if owner:
            d = _owner_distance(status)
            if d is None:
                lines.append(f"{owner['name']} is out of sight, so you wandered off "
                             "(or they did). You're heading back.")
            elif d > FAR_DIST:
                lines.append(f"You wandered {d:.0f} blocks away from {owner['name']}.")
            elif d >= NEAR_DIST:
                lines.append(f"You're roaming {d:.0f} blocks from {owner['name']}, "
                             "keeping them in sight.")
            else:
                lines.append(f"You're right next to {owner['name']} ({d:.0f} blocks).")
        order = self._order_text()
        if order:
            lines.append(f"Your player's current order: {order}.")
        if _house(status).get("complete"):
            o = status["house"]["origin"]
            lines.append(f"You built a house at {o[0] + 3}, {o[1] + 1}, {o[2] + 3}.")
        if self._last_done:
            lines.append(f"Last finished: {self._last_done}.")
        await self.send_context("\n".join(lines), silent=True)

    def _autoplay_allowed(self) -> bool:
        """data/minecraft/autoplay.json, written by the dashboard switch.
        Checked every loop so switching off stops play even if the
        process is left running."""
        try:
            with open(AUTOPLAY_PATH, encoding="utf-8") as f:
                allowed = bool(json.load(f).get("minecraft", True))
        except Exception:
            allowed = True
        if allowed != self._autoplay_was:
            self._autoplay_was = allowed
            self._tel["loop_state"] = "running" if allowed else "autoplay off"
            self._think("Back to playing." if allowed else "Autoplay is off. Standing by.")
            self._record("agent", f"autoplay {'on' if allowed else 'off'}")
        return allowed

    def _remember_goal(self, goal: str, ok: bool) -> None:
        self._recent_goals.append((goal, ok))
        self._recent_goals = self._recent_goals[-10:]

    def _recent_text(self, n: int) -> str:
        return ", ".join(f"{g} ({'succeeded' if ok else 'failed'})"
                         for g, ok in self._recent_goals[-n:]) or "none"

    def _cooldown_text(self) -> str:
        now = time.time()
        return ", ".join(f"{g} ({int(t - now)} s)" for g, t in self._cooldown_until.items()
                         if t > now) or "nothing"

    # ─── Telemetry helpers ─────────────────────────────────────────

    def _record(self, category: str, message: str) -> None:
        entry = {
            "ts":       time.time(),
            "time":     time.strftime("%H:%M:%S"),
            "category": category,
            "message":  message,
        }
        print(f"[{entry['time']}] {category.upper()}: {message}")
        try:
            with open(EVENTS_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass
        if category == "error":
            errs = self._tel.setdefault("errors", [])
            errs.append({"time": entry["time"], "message": message})
            self._tel["errors"] = errs[-30:]

    def _think(self, thought: str) -> None:
        """What the agent is doing or deciding, in plain words, for the
        dashboard's thought box."""
        self._tel["current_thought"] = thought
        self._flush_telemetry()

    def _flush_telemetry(self) -> None:
        self._tel["skills_verified"] = self._progress
        self._tel["current_order"] = self._order_text() or None
        self._tel["mode"] = self._mode
        try:
            with open(TELEMETRY_PATH, "w", encoding="utf-8") as f:
                json.dump(self._tel, f, indent=2)
        except Exception:
            pass


# Foods worth eating; bot.js picks the most filling one.
_EDIBLE = ["bread", "apple", "baked_potato", "carrot", "potato", "beetroot", "melon_slice",
           "sweet_berries", "cookie", "pumpkin_pie", "golden_carrot", "mushroom_stew",
           "cooked_beef", "cooked_porkchop", "cooked_chicken", "cooked_mutton",
           "cooked_rabbit", "cooked_cod", "cooked_salmon", *_MEAT]


def _environment_text(env: Optional[Dict[str, Any]]) -> str:
    """Danger sense in one line, mirroring bot.js's scan_environment:
    "Environment: You are in a cave. Creeper at 4 blocks. Oxygen is 8/20.
    Pathfinding may be difficult." """
    if not env:
        return "Environment: unknown."
    parts = ["You are in a cave." if env.get("in_cave") else
             "You are under cover." if env.get("covered") else "You are outdoors."]
    parts.append("It's day." if env.get("is_day", True) else "It's night.")
    parts += [f"{h[0].upper()}{h[1:]}." for h in env.get("hazards") or []]
    if env.get("oxygen", 20) < 20:
        parts.append(f"Oxygen is {env['oxygen']}/20.")
    if env.get("in_cave"):
        parts.append("Pathfinding may be difficult.")
    if env.get("emergency"):
        parts.append(f"EMERGENCY: {env['emergency']}.")
    return "Environment: " + " ".join(parts)


def _fmt(v: Any) -> str:
    return "?" if v is None else f"{v:g}" if isinstance(v, (int, float)) else str(v)


def _missing_ollama_model(spec: Optional[str]) -> Optional[str]:
    """The model's name if it's an Ollama model that isn't pulled yet.
    Other providers, and an unreachable Ollama, count as available."""
    if not spec or parse_model_spec is None:
        return None
    provider, model = parse_model_spec(spec)
    if provider != "ollama" or not model:
        return None
    try:
        with urllib.request.urlopen(f"{_OLLAMA}/api/tags", timeout=3) as r:
            names = {m.get("name") for m in json.loads(r.read()).get("models", [])}
    except Exception:
        return None
    wanted = model if ":" in model else model + ":latest"
    return None if wanted in names else model


def _inv(status: Dict) -> Dict[str, int]:
    inv: Dict[str, int] = {}
    for i in status.get("inventory") or []:
        inv[i["name"]] = inv.get(i["name"], 0) + i["count"]
    return inv


def _all_goal_names() -> List[str]:
    return [t["goal"] for t in CURRICULUM] + list(FREE_GOALS) + list(TOOL_GOALS)


def _goal_desc(goal: str) -> str:
    if goal in FREE_GOALS:
        return FREE_GOALS[goal]["desc"]
    if goal in TOOL_GOALS:
        return TOOL_GOALS[goal]
    return next((t["desc"] for t in CURRICULUM if t["goal"] == goal), goal)


_TECH_REASON = {
    "collect_wood":        "need wood for a crafting table and tools",
    "craft_workbench":     "need a crafting table for tools",
    "craft_wood_pickaxe":  "need a pickaxe to mine stone",
    "mine_stone":          "need cobblestone for a stone pickaxe",
    "craft_stone_pickaxe": "upgrading to a stone pickaxe",
}


def _skill_code(goal: str, status: Dict) -> Optional[str]:
    if goal in FREE_GOALS:
        code: Optional[Code] = FREE_GOALS[goal].get("code")
        if code is None:
            return None      # an action goal, run as a plain bot.js action
        return _PRELUDE + (code(status) if callable(code) else code)
    return BUILTIN_SKILLS.get(goal)


def _goal_steps(goal: str, status: Dict) -> List[Step]:
    if goal in FREE_GOALS:
        steps = FREE_GOALS[goal].get("steps") or []
        return steps(status) if callable(steps) else steps
    return CURRICULUM_STEPS.get(goal, [])


# ─── Entry point ──────────────────────────────────────────────────────────────

async def _main() -> None:
    print("[mc-agent] Starting Lumi × Minecraft autonomous agent")
    print(f"[mc-agent] Bot control API: {BOT_API}")
    print(f"[mc-agent] Token: {TOKEN_PATH}")
    agent = MinecraftAgent()
    agent.start_loop()
    try:
        await agent.run()
    except KeyboardInterrupt:
        agent.stop()
        print("\n[mc-agent] Stopped")


if __name__ == "__main__":
    asyncio.run(_main())

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
    _GAME_MODEL, _OLLAMA = None, "http://localhost:11434"

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
EAT_BELOW_FOOD = 14   # eat when hunger drops below this and food is on hand
DEFEND_RANGE   = 6    # proactively fight hostiles this close (blocks)

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
  await mineBlock(bot, tree.name, n);
  const got = logCount() - before;
  if (got <= 0) throw new Error(`Couldn't get any ${tree.name}`);
  return `Chopped ${got} ${tree.name}`;
}
async function craftAndCheck(name) {
  await craftItem(bot, name, 1);
  if (!countOf(name)) throw new Error(`Couldn't craft a ${name}`);
  return `Crafted a ${name}`;
}
async function mineAndCheck(block, drops, count) {
  const have = () => drops.reduce((s, d) => s + countOf(d), 0);
  const before = have();
  await mineBlock(bot, block, count);
  const got = have() - before;
  if (got <= 0) throw new Error(`Mined no ${drops[0]}`);
  return `Got ${got} ${drops[0]}`;
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

# ─── Free-play goals ──────────────────────────────────────────────────────────
# Repeatable goals picked when the agent isn't pushing the curriculum. Each
# has a `when(inv, status)` gate, Voyager `code` (a string or a function of
# the status), and optional plain-action `steps` for when Voyager is missing.

_DIRS = [(1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1), (1, -1), (-1, 1)]
_MEAT = ["beef", "porkchop", "chicken", "mutton"]
_PICKAXES = ["wooden_pickaxe", "stone_pickaxe", "iron_pickaxe", "diamond_pickaxe", "netherite_pickaxe"]


def _explore_code(_status: Dict) -> str:
    dx, dz = random.choice(_DIRS)
    return (f'await exploreUntil(bot, new Vec3({dx}, 0, {dz}), 45, () => null);\n'
            f'const p = bot.entity.position;\n'
            f'return `Explored to ${{p.x.toFixed(0)}}, ${{p.y.toFixed(0)}}, ${{p.z.toFixed(0)}}`;')


def _explore_steps(status: Dict) -> List[Step]:
    p = status.get("position") or {}
    dx, dz = random.choice(_DIRS)
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

Code = Union[str, Callable[[Dict], str]]


def _has(inv: Dict[str, int], names: List[str]) -> bool:
    return any(inv.get(n, 0) > 0 for n in names)


# Each gate is checked in code, so the model is never offered a goal that
# makes no sense right now (a hut at noon, more trees with a full stack).
FREE_GOALS: Dict[str, Dict[str, Any]] = {
    "chop_trees":     {"desc": "Chop down some trees for wood",
                       "when": lambda inv, st: _logs(inv) < 10,
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
                       "when": lambda inv, st: not st.get("is_day", True),
                       "code": _DIRT_HUT},
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
                  "build_dirt_hut": 90, "hunt_food": 75, "mine_stone": 90}
# After success, don't offer a goal again for this long (s).
SUCCESS_COOLDOWN_S = {"build_dirt_hut": 600}
FAIL_STREAK = 3          # failures in a row before a goal is rested
FAIL_COOLDOWN_S = 300


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
            f'Health: {t["health"] or "?"}/20  Food: {t["food"] or "?"}/20',
            f'Goal: {t["current_goal"] or "none"} — {t["goal_description"] or ""}'
            + (f' (picked by {t["goal_source"]})' if t["goal_source"] else ""),
            f'Recent goals: {self._recent_text(5)}',
            f'Resting: {self._cooldown_text()}',
            f'Inventory: {inv}',
            f'Tech progress: {self._progress}/{len(CURRICULUM)}',
        ]
        return True, "\n".join(lines)

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
                    "last_update": time.strftime("%H:%M:%S"),
                    "loop_state":  "running",
                })
                if status.get("is_day", True):
                    self._hut_this_night = False
                self._progress = _progress_count(_inv(status), status.get("nearby") or {})
                self._flush_telemetry()
                await self._push_status(status)

                if not status.get("ready"):
                    self._think("Waiting to spawn in...")
                    continue

                # 2. Survive — fights and meals come before any goal
                if await self._survive(status):
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
                await self._push_status(status, force=True)

                # 4. Execute
                ok, msg = await self._execute_goal(goal_name, status)
                self._remember_goal(goal_name, ok)

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
                    if goal_name == "mine_diamond":
                        await self._narrate("I just found and mined diamonds!", major=True)
                    elif goal_name in _TIERS:
                        await self._narrate(f"I just finished a milestone: {desc.lower()}.")
                    self._tel["current_goal"] = None
                else:
                    n = self._failures[goal_name] = self._failures.get(goal_name, 0) + 1
                    self._record("error", f"FAIL {goal_name} (x{n}): {msg}")
                    self._think(f"That didn't work ({desc.lower()}): {msg[:120]}")
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

        inv = _inv(status)
        near = status.get("nearby") or {}

        # The basics are never left to the model.
        tech = _tech_goal(inv, near)
        if tech and self._available(tech, status):
            return tech, "tech tree", _TECH_REASON[tech]

        options: Dict[str, str] = {}
        progress = _progress_goal(inv, near)
        if progress and self._available(progress, status):
            options[progress] = f"{_goal_desc(progress)} (next step toward better tools)"
        for name, g in FREE_GOALS.items():
            try:
                if g["when"](inv, status) and self._available(name, status):
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
        if progress in options and random.random() < 0.7:
            return progress
        weights = {"chop_trees": 3, "explore": 2, "hunt_food": 1, "mine_coal": 2,
                   "cook_meat": 2, "build_dirt_hut": 0, "wait_out_night": 0}
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
            f"Recent goals (oldest first, with result): {self._recent_text(5)}.\n"
            "Rules: Do not pick build_dirt_hut if it is Day. Do not pick chop_trees if "
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
    return [t["goal"] for t in CURRICULUM] + list(FREE_GOALS)


def _goal_desc(goal: str) -> str:
    if goal in FREE_GOALS:
        return FREE_GOALS[goal]["desc"]
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
        code: Code = FREE_GOALS[goal]["code"]
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

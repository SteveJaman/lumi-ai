"""
Lumi × Minecraft — Autonomous Voyager-inspired agent.
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
if _ROOT in sys.path:
    sys.path.remove(_ROOT)
sys.path.insert(0, _ROOT)

from sdk import action
from plugin import game_vision
from plugin.base_game_agent import BaseGameAgent

try:
    from modules.llm_providers import chat_sync, parse_model_spec
    from constants import LLM_ENDPOINT as _OLLAMA
    from modules import model_router
    _GAME_MODEL = model_router.local_agent_model()
except Exception:
    chat_sync = parse_model_spec = model_router = None
    _GAME_MODEL, _OLLAMA = None, "http://127.0.0.1:11434"

from modules import minecraft_governor as governor
from modules import minecraft_social as social
from modules.minecraft_actions import render_actions_prompt
try:
    from constants import MINECRAFT_AGENT_CLOUD_TIMEOUT_S as _CLOUD_TIMEOUT_S
except Exception:
    _CLOUD_TIMEOUT_S = 45

# ─── Paths ────────────────────────────────────────────────────────────────────
DATA_ROOT      = os.path.join(_ROOT, "data")
MC_DIR         = os.path.join(DATA_ROOT, "minecraft")
TELEMETRY_PATH = os.path.join(MC_DIR, "telemetry.json")
EVENTS_PATH    = os.path.join(MC_DIR, "events.jsonl")
AUTOPLAY_PATH  = os.path.join(MC_DIR, "autoplay.json")
CONTROL_PATH   = os.path.join(MC_DIR, "agent_control.json")
GAMING_MODE_PATH = os.path.join(DATA_ROOT, "gaming_mode.json")
TOKEN_PATH     = os.path.join(DATA_ROOT, "memories", "sdk_token.txt")

BOT_API        = os.environ.get("BOT_API", "http://127.0.0.1:5057")
LOOP_INTERVAL  = float(os.environ.get("MC_LOOP_INTERVAL", "4"))
ACTION_TIMEOUT = float(os.environ.get("MC_ACTION_TIMEOUT", "135"))
GOAL_PICKER    = os.environ.get("MC_GOAL_PICKER", "llm").lower()
GOAL_MODEL     = os.environ.get("MC_GOAL_MODEL") or _GAME_MODEL
GOAL_KEEP_ALIVE = os.environ.get("MC_GOAL_KEEP_ALIVE", "30m")
NARRATE        = os.environ.get("MC_NARRATE", "major").lower()
NARRATE_COOLDOWN = float(os.environ.get("MC_NARRATE_COOLDOWN", "75"))
STATUS_EVERY   = float(os.environ.get("MC_STATUS_INTERVAL", "30"))
DEFAULT_MODE   = os.environ.get("MC_DEFAULT_MODE", "follow").lower()
EAT_BELOW_FOOD = 14
DEFEND_RANGE   = 6

NEAR_DIST      = 10
FAR_DIST       = 25
PROJECT_LEASH  = 64
PROJECT_GOALS  = {"branch_mine", "build_house", "mine_iron_ore", "mine_diamond"}
PROJECT_HOLD_S = 180

# ─── Tech tree & Goals ────────────────────────────────────────────────────────
_LOGS = ["oak_log", "birch_log", "spruce_log", "jungle_log", "acacia_log", "dark_oak_log", "mangrove_log"]
_STONE_MATS = ["cobblestone", "cobbled_deepslate", "blackstone"]
_PICKAXE_TIER = {"wooden_pickaxe": 1, "stone_pickaxe": 2, "iron_pickaxe": 3, "diamond_pickaxe": 4, "netherite_pickaxe": 5}

def _logs(inv: Dict[str, int]) -> int: return sum(inv.get(n, 0) for n in _LOGS)
def _wood(inv: Dict[str, int]) -> int: return _logs(inv) * 4 + sum(c for n, c in inv.items() if n.endswith("_planks"))
def _cobble(inv: Dict[str, int]) -> int: return sum(inv.get(n, 0) for n in _STONE_MATS)
def _pickaxe(inv: Dict[str, int]) -> int: return max((t for n, t in _PICKAXE_TIER.items() if inv.get(n)), default=0)
def _has_table(inv: Dict[str, int], near: Dict[str, bool]) -> bool: return bool(inv.get("crafting_table") or near.get("crafting_table"))
def _has_furnace(inv: Dict[str, int], near: Dict[str, bool]) -> bool: return bool(inv.get("furnace") or near.get("furnace"))

_TOOL_TIER = {"wooden": 1, "stone": 2, "iron": 3, "diamond": 4}
_TOOL_COST = {"pickaxe": (3, 2), "axe": (3, 2), "sword": (2, 1), "shovel": (1, 2), "hoe": (2, 2)}
_PICKAXE_GOAL = {"wooden": "craft_wood_pickaxe", "stone": "craft_stone_pickaxe", "iron": "craft_iron_pickaxe"}

def _tool_level(inv: Dict[str, int], tool: str) -> int:
    return max((t for m, t in _TOOL_TIER.items() if inv.get(f"{m}_{tool}")), default=0)

def _craft_goal(mat: str, tool: str) -> str:
    return _PICKAXE_GOAL.get(mat) if tool == "pickaxe" and mat in _PICKAXE_GOAL else f"craft_{mat}_{tool}"

def _craft_step(mat: str, tool: str, inv: Dict[str, int], near: Dict[str, bool]) -> Optional[str]:
    head, sticks = _TOOL_COST[tool]
    if mat == "stone":
        if _pickaxe(inv) < 1: return _tech_goal(inv, near)
        if _cobble(inv) < head: return "mine_stone"
    elif mat == "iron":
        if _pickaxe(inv) < 2: return _tech_goal(inv, near)
        ingots = inv.get("iron_ingot", 0)
        if ingots < head:
            if ingots + inv.get("raw_iron", 0) < head: return "mine_iron_ore"
            return "smelt_iron" if _has_furnace(inv, near) or _cobble(inv) >= 8 else "mine_stone"
    elif mat == "diamond":
        if _pickaxe(inv) < 3: return _tool_step(3, inv, near)
        if inv.get("diamond", 0) < head: return "branch_mine"
    wood = ((head if mat == "wooden" else 0) + (0 if inv.get("stick", 0) >= sticks else 2) + (0 if _has_table(inv, near) else 4))
    if _wood(inv) < wood: return "collect_wood"
    return _craft_goal(mat, tool)

_KIT = [("axe", "an axe chops trees much faster than bare hands"), ("sword", "a sword for fighting mobs")]

def _kit_goal(inv: Dict[str, int], near: Dict[str, bool]) -> Optional[Tuple[str, str]]:
    if _pickaxe(inv) < 2: return None
    for tool, why in _KIT:
        if _tool_level(inv, tool) < 2:
            step = _craft_step("stone", tool, inv, near)
            if step: return step, f"need a stone {tool}: {why}"
    return None

CURRICULUM: List[Dict[str, Any]] = [
    {"goal": "collect_wood",        "desc": "Collect logs", "done": lambda inv, near: _pickaxe(inv) >= 1 or _wood(inv) >= 12},
    {"goal": "craft_workbench",     "desc": "Craft a crafting table", "done": lambda inv, near: _has_table(inv, near) or _pickaxe(inv) >= 1},
    {"goal": "craft_wood_pickaxe",  "desc": "Craft a wooden pickaxe", "done": lambda inv, near: _pickaxe(inv) >= 1},
    {"goal": "mine_stone",          "desc": "Mine cobblestone", "done": lambda inv, near: _cobble(inv) >= 3 or _pickaxe(inv) >= 2},
    {"goal": "craft_stone_pickaxe", "desc": "Craft a stone pickaxe", "done": lambda inv, near: _pickaxe(inv) >= 2},
    {"goal": "craft_furnace",       "desc": "Craft a furnace", "done": lambda inv, near: _has_furnace(inv, near), "ready": lambda inv, near: _cobble(inv) >= 8},
    {"goal": "mine_iron_ore",       "desc": "Find and mine iron ore", "done": lambda inv, near: inv.get("raw_iron", 0) >= 3 or inv.get("iron_ingot", 0) >= 3 or _pickaxe(inv) >= 3, "ready": lambda inv, near: _pickaxe(inv) >= 2},
    {"goal": "smelt_iron",          "desc": "Smelt iron ingots", "done": lambda inv, near: inv.get("iron_ingot", 0) >= 3 or _pickaxe(inv) >= 3, "ready": lambda inv, near: inv.get("raw_iron", 0) >= 3 and (_has_furnace(inv, near) or _cobble(inv) >= 8)},
    {"goal": "craft_iron_pickaxe",  "desc": "Craft an iron pickaxe", "done": lambda inv, near: _pickaxe(inv) >= 3, "ready": lambda inv, near: inv.get("iron_ingot", 0) >= 3},
    {"goal": "mine_diamond",        "desc": "Find and mine diamonds", "done": lambda inv, near: inv.get("diamond", 0) >= 1 or _pickaxe(inv) >= 4, "ready": lambda inv, near: _pickaxe(inv) >= 3},
]
_TIERS = {t["goal"]: t for t in CURRICULUM}

_TECH_REASON = {
    "collect_wood": "I need wood", "craft_workbench": "I need a crafting table", "craft_wood_pickaxe": "a pickaxe lets me mine",
    "mine_stone": "I need cobblestone", "craft_stone_pickaxe": "a stone pickaxe mines faster",
}

def _tech_goal(inv: Dict[str, int], near: Dict[str, bool]) -> Optional[str]:
    pick = _pickaxe(inv)
    if pick >= 2: return None
    wood = _wood(inv)
    table = _has_table(inv, near)
    sticks_cost = 0 if inv.get("stick", 0) >= 2 else 2
    table_cost = 0 if table else 4
    if pick == 1:
        if _cobble(inv) < 3: return "mine_stone"
        return "collect_wood" if wood < sticks_cost + table_cost else "craft_stone_pickaxe"
    if wood < 3 + sticks_cost + table_cost: return "collect_wood"
    return "craft_workbench" if not table else "craft_wood_pickaxe"

def _progress_goal(inv: Dict[str, int], near: Dict[str, bool]) -> Optional[str]:
    for tier in CURRICULUM[5:]:
        if not tier["done"](inv, near) and tier["ready"](inv, near): return tier["goal"]
    if _pickaxe(inv) >= 2 and not _has_furnace(inv, near) and _cobble(inv) < 8: return "mine_stone"
    return None

def _progress_count(inv: Dict[str, int], near: Dict[str, bool]) -> int:
    return sum(1 for t in CURRICULUM if t["done"](inv, near))

# ─── Built-in Skills ──────────────────────────────────────────────────────────
_PRELUDE = """
const LOGS = """ + json.dumps(_LOGS) + """;
function countOf(name) { return bot.inventory.items().filter(i => i.name === name).reduce((s, i) => s + i.count, 0); }
function countWhere(pred) { return bot.inventory.items().filter(i => pred(i.name)).reduce((s, i) => s + i.count, 0); }
const logCount = () => countWhere(n => LOGS.includes(n));
const plankCount = () => countWhere(n => n.endsWith("_planks"));
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
  if (!(await exploreUntil(bot, direction, 60, look))) { throw new Error(`No ${blockName} found after exploring`); }
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
  let stopped = null;
  try { await mineBlock(bot, block, count); } catch (e) { stopped = e; }
  const got = have() - before;
  if (got <= 0) throw stopped || new Error(`Mined no ${drops[0]}`);
  return `Got ${got} ${drops[0]}` + (stopped ? ` (stopped early: ${stopped.message})` : "");
}
"""

BUILTIN_SKILLS: Dict[str, str] = {
    name: _PRELUDE + body for name, body in {
        "collect_wood": 'return await chopTrees(5);',
        "craft_workbench": 'await ensurePlanks(4);\nreturn await craftAndCheck("crafting_table");',
        "craft_wood_pickaxe": 'await ensureNear("crafting_table");\nawait ensureSticks(2);\nawait ensurePlanks(3);\nreturn await craftAndCheck("wooden_pickaxe");',
        "mine_stone": 'await findOrExplore("stone", new Vec3(1, -1, 1));\nreturn await mineAndCheck("stone", ["cobblestone", "cobbled_deepslate"], 8);',
        "craft_stone_pickaxe": 'await ensureNear("crafting_table");\nawait ensureSticks(2);\nreturn await craftAndCheck("stone_pickaxe");',
        "craft_furnace": 'await ensureNear("crafting_table");\nreturn await craftAndCheck("furnace");',
        "mine_iron_ore": 'await findOrExplore("iron_ore", new Vec3(1, -1, 1));\nreturn await mineAndCheck("iron_ore", ["raw_iron"], 3);',
        "smelt_iron": 'await ensureNear("furnace");\nlet fuel = "coal";\nif (!countOf("coal")) {\n  await ensurePlanks(4);\n  fuel = bot.inventory.items().find(i => i.name.endsWith("_planks")).name;\n}\nconst before = countOf("iron_ingot");\nawait smeltItem(bot, "raw_iron", fuel, Math.min(countOf("raw_iron"), 3));\nif (countOf("iron_ingot") <= before) throw new Error("Smelted no iron");\nreturn `Have ${countOf("iron_ingot")} iron ingots`;',
        "craft_iron_pickaxe": 'await ensureNear("crafting_table");\nawait ensureSticks(2);\nreturn await craftAndCheck("iron_pickaxe");',
        "mine_diamond": 'await findOrExplore("diamond_ore", new Vec3(1, -1, 0));\nreturn await mineAndCheck("diamond_ore", ["diamond"], 1);',
    }.items()
}

def _tool_skill(mat: str, tool: str) -> str:
    head, sticks = _TOOL_COST[tool]
    planks = f"await ensurePlanks({head});\n" if mat == "wooden" else ""
    return (_PRELUDE + 'await ensureNear("crafting_table");\n' + f"await ensureSticks({sticks});\n{planks}" + f'return await craftAndCheck("{mat}_{tool}");')

TOOL_GOALS: Dict[str, str] = {
    _craft_goal(m, t): f"Craft {'an' if m[0] in 'aeiou' else 'a'} {m} {t}"
    for m in _TOOL_TIER for t in _TOOL_COST if _craft_goal(m, t) not in _TIERS
}
BUILTIN_SKILLS.update({g: _tool_skill(*g[len("craft_"):].split("_", 1)) for g in TOOL_GOALS})

Step = Tuple[Any, ...]
CURRICULUM_STEPS: Dict[str, List[Step]] = {
    "collect_wood":        [("mine_block", {"name": "oak_log", "count": 5})],
    "craft_workbench":     [("craft_item", {"name": "oak_planks", "count": 1}), ("craft_item", {"name": "crafting_table", "count": 1})],
    "craft_wood_pickaxe":  [("place_block", {"name": "crafting_table"}, True), ("craft_item", {"name": "oak_planks", "count": 2}), ("craft_item", {"name": "stick", "count": 1}), ("craft_item", {"name": "wooden_pickaxe", "count": 1})],
    "mine_stone":          [("mine_block", {"name": "stone", "count": 8})],
    "craft_stone_pickaxe": [("craft_item", {"name": "stick", "count": 1}, True), ("craft_item", {"name": "stone_pickaxe", "count": 1})],
    "craft_furnace":       [("craft_item", {"name": "furnace", "count": 1})],
    "craft_iron_pickaxe":  [("craft_item", {"name": "iron_pickaxe", "count": 1})],
}
CURRICULUM_STEPS.update({g: [("craft_item", {"name": "stick", "count": 1}, True), ("craft_item", {"name": g[len("craft_"):], "count": 1})] for g in TOOL_GOALS})

# ─── Free-play goals ──────────────────────────────────────────────────────────
_DIRS = [(1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1), (1, -1), (-1, 1)]
_MEAT = ["beef", "porkchop", "chicken", "mutton"]
_PICKAXES = ["wooden_pickaxe", "stone_pickaxe", "iron_pickaxe", "diamond_pickaxe", "netherite_pickaxe"]
_EDIBLE = ["cooked_beef", "cooked_porkchop", "cooked_chicken", "cooked_mutton", "bread", "apple", "golden_apple", "carrot", "baked_potato", "cooked_salmon", "cooked_cod", "sweet_berries", "melon_slice", "pumpkin_pie"]

def _explore_dir(status: Dict) -> Tuple[int, int]:
    owner, me = status.get("owner") or {}, status.get("position") or {}
    op = owner.get("position")
    if op and me and (owner.get("distance") or 0) > 15:
        sign = lambda v: (v > 2) - (v < -2)
        d = (sign(op["x"] - me["x"]), sign(op["z"] - me["z"]))
        if d != (0, 0): return d
    return random.choice(_DIRS)

def _explore_code(status: Dict) -> str:
    dx, dz = _explore_dir(status)
    return (f'await exploreUntil(bot, new Vec3({dx}, 0, {dz}), 45, () => null);\n' + f'const p = bot.entity.position;\n' + f'return `Explored to ${{p.x.toFixed(0)}}, ${{p.y.toFixed(0)}}, ${{p.z.toFixed(0)}}`;')

def _explore_steps(status: Dict) -> List[Step]:
    p = status.get("position") or {}
    dx, dz = _explore_dir(status)
    return [("move_to", {"x": p.get("x", 0) + dx * 30, "y": p.get("y", 64), "z": p.get("z", 0) + dz * 30, "range": 6})]

_CHOP_TREES = "return await chopTrees(4);"
_HUNT_FOOD = f"""
const prey = {json.dumps(["cow", "pig", "sheep", "chicken"])};
const find = () => bot.nearestEntity(e => prey.includes(e.name) && bot.entity.position.distanceTo(e.position) < 48);
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
if (!countOf("coal")) {{ await ensurePlanks(4); fuel = bot.inventory.items().find(i => i.name.endsWith("_planks")).name; }}
await ensureNear("furnace");
await smeltItem(bot, raw, fuel, Math.min(countOf(raw), 6));
return `Cooked ${{raw}}`;
"""
_MINE_COAL = """
await findOrExplore("coal_ore", new Vec3(1, -1, -1));
await mineBlock(bot, "coal_ore", 5);
"""
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
    if (ref && ref.boundingBox === "block") { await bot.equip(item, "hand"); await bot.placeBlock(ref, new Vec3(-x, -y, -z)); return; }
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
_GET_BED = """
const WOOLS = mcData.itemsArray.filter(i => i.name.endsWith("_wool")).map(i => i.name);
const bestWool = () => WOOLS.map(n => [n, countOf(n)]).sort((a, b) => b[1] - a[1])[0];
const sheep = () => bot.nearestEntity(e => e.name === "sheep" && bot.entity.position.distanceTo(e.position) < 48);
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

def _has(inv: Dict[str, int], names: List[str]) -> bool: return any(inv.get(n, 0) > 0 for n in names)
def _owner_distance(st: Dict) -> Optional[float]:
    owner, me = st.get("owner") or {}, st.get("position") or {}
    op = owner.get("position")
    if not op or not me: return owner.get("distance")
    return ((op["x"] - me["x"]) ** 2 + (op["z"] - me["z"]) ** 2) ** 0.5

def _house(st: Dict) -> Dict: return st.get("house") or {}
def _house_center(st: Dict) -> Optional[Dict[str, float]]:
    o = _house(st).get("origin")
    return {"x": o[0] + 3, "y": o[1] + 1, "z": o[2] + 3} if o else None
def _away_from_home(st: Dict) -> bool:
    c, me = _house_center(st), st.get("position")
    if not c or not me: return False
    return max(abs(me["x"] - c["x"]), abs(me["z"] - c["z"])) > 3 or abs(me["y"] - c["y"]) > 2
def _at_surface(st: Dict) -> bool:
    op, me = (st.get("owner") or {}).get("position"), st.get("position")
    return not op or not me or abs(op["y"] - me["y"]) <= 8
def _house_material(inv: Dict[str, int]) -> str: return "cobblestone" if _cobble(inv) >= _wood(inv) else "planks"
def _bed(st: Dict) -> Dict: return st.get("bed") or {}
def _bed_handy(inv: Dict[str, int], st: Dict) -> bool: return bool(_bed(st).get("nearby") or any(n.endswith("_bed") for n in inv))
def _bed_routine(inv: Dict[str, int], st: Dict) -> Optional[str]:
    if not _bed_handy(inv, st): return None
    if st.get("can_sleep"): return "sleep_in_bed"
    return None if _bed(st).get("spawn_set") else "set_spawn"
def _wants(st: Dict, *items: str) -> bool: return any(st.get("wanted", {}).get(i) for i in items)

FREE_GOALS: Dict[str, Dict[str, Any]] = {
    "chop_trees":     {"desc": "Chop down some trees for wood", "when": lambda inv, st: _logs(inv) < 10 or _wants(st, "oak_planks", "planks"), "code": _CHOP_TREES, "steps": [("mine_block", {"name": "oak_log", "count": 4})]},
    "hunt_food":      {"desc": "Hunt an animal for food", "when": lambda inv, st: True, "code": _HUNT_FOOD, "steps": [("kill_mob", {"name": "cow"})]},
    "cook_meat":      {"desc": "Cook raw meat in a furnace", "when": lambda inv, st: _has(inv, _MEAT) and (inv.get("coal", 0) or _wood(inv) >= 4), "code": _COOK_MEAT},
    "explore":        {"desc": "Wander off and explore the area", "when": lambda inv, st: True, "code": _explore_code, "steps": _explore_steps},
    "mine_coal":      {"desc": "Find and mine coal for torches and fuel", "when": lambda inv, st: _has(inv, _PICKAXES), "code": _MINE_COAL},
    "build_dirt_hut": {"desc": "Build a dirt hut to hide in for the night", "when": lambda inv, st: not st.get("is_day", True) and not _house(st).get("complete"), "code": _DIRT_HUT},
    "go_home":        {"desc": "Go home to the house for the night", "when": lambda inv, st: (not st.get("is_day", True) and _house(st).get("complete") and _away_from_home(st)), "action": ("move_to", lambda st: dict(_house_center(st), range=1))},
    "build_house":    {"desc": "Build a proper 7x7 house", "when": lambda inv, st: (st.get("is_day", True) and not _house(st).get("complete") and max(_cobble(inv), _wood(inv)) >= 64 and _at_surface(st)), "action": ("build_house", lambda st: {"material": _house_material(_inv(st))})},
    "gather_cobblestone": {"desc": "Mine cobblestone for the house", "when": lambda inv, st: _wants(st, "cobblestone") and _pickaxe(inv) >= 1, "code": _GATHER_COBBLE},
    "branch_mine":    {"desc": "Dig a branch mine and look for ores", "when": lambda inv, st: _pickaxe(inv) >= 2, "action": ("mine_branch_tunnel", lambda st: {"depth": -54 if _pickaxe(_inv(st)) >= 3 else 12, "length": 24})},
    "craft_torches":  {"desc": "Craft torches", "when": lambda inv, st: inv.get("coal", 0) >= 1 and inv.get("torch", 0) < 16 and _wood(inv) >= 2, "code": _CRAFT_TORCHES},
    "organize_inventory": {"desc": "Put excess stuff away in a chest", "when": lambda inv, st: (st.get("free_slots") or 36) <= 6, "action": ("sort_inventory", {"place_if_missing": True})},
    "get_bed":        {"desc": "Make a bed (wool from three sheep)", "when": lambda inv, st: not _bed_handy(inv, st) and not _bed(st).get("remembered"), "code": _GET_BED},
    "sleep_in_bed":   {"desc": "Sleep in a bed for the night", "when": lambda inv, st: False, "action": ("use_bed", {"mode": "sleep"})},
    "set_spawn":      {"desc": "Set my respawn point at a bed", "when": lambda inv, st: False, "action": ("use_bed", {"mode": "spawn"})},
    "retrieve_items": {"desc": "Go back to where I died and pick up my items", "when": lambda inv, st: False, "action": ("retrieve_items", {})},
    "drop_item":      {"desc": "Drop items on the ground", "when": lambda inv, st: False},
    "give_item":      {"desc": "Hand an item over to my player", "when": lambda inv, st: False, "action": ("give_item", {})},
    "return_to_player": {"desc": "Head back to my player", "when": lambda inv, st: False, "action": ("return_to_player", {"range": 4})},
    "wait_out_night": {"desc": "Hide and wait for the sun to come up", "when": lambda inv, st: not st.get("is_day", True), "code": _WAIT_NIGHT},
}

PICKER_SYSTEM = (
    "You choose the next goal for a Minecraft survival player. Think like a "
    "streamer playing for fun: make progress, stay alive, and vary what you do. "
    "Prefer the tool-progress goal when one is listed, shelter at night, and "
    "food when hunger is low. Pick only from the listed goal names. Reply with "
    'JSON only: {"goal": "<one of the listed names>", "reason": "<one short sentence>"}'
)

GOAL_TIMEOUT_S = {"collect_wood": 120, "chop_trees": 120, "explore": 120, "build_dirt_hut": 120, "hunt_food": 120, "mine_stone": 120, "gather_cobblestone": 120}
STUCK_RE = re.compile(r"path|stuck|took to long|timed out|couldn'?t (reach|get to)|no .* found", re.I)
LOOK_EVERY_S = 180
LOOK_MEMORY_S = 300
VISION_EVERY_S = float(os.environ.get("MC_VISION_EVERY", "0"))
GLANCE_QUESTION = ("This is a first-person view in a blocky video game. In one or two sentences, say what you see: the terrain, blocks, animals or monsters, and anything in the way.")

REMEMBERED_TIERS = {"craft_iron_pickaxe": "Made my first iron pickaxe", "craft_furnace": "Built my first furnace"}
SUCCESS_COOLDOWN_S = {"build_dirt_hut": 600, "organize_inventory": 120}
FAIL_REST_S = {"set_spawn": 1800, "sleep_in_bed": 90}
FAIL_STREAK = 3
FAIL_COOLDOWN_S = 300

# ─── Orders ───────────────────────────────────────────────────────────────────
DROP_RE = re.compile(r"\b(?:drop|throw (?:away|out)|toss out|get rid of)\b\s+(.*)$")
GIVE_RE = re.compile(r"\b(?:give|hand|toss|pass|bring)\b(?:\s+(?:me|us|over|it))*\s+(.*)$")
GIVE_FILLER = re.compile(r"\b(the|a|an|some|your|my|of|please|pls|now|here|back|to me|over)\b")
TOOL_RE = re.compile(r"\b(wood(?:en)?|stone|cobble(?:stone)?|iron|diamond)\s+(pick(?:axe)?|axe|sword|shovel|spade|hoe)s?\b")
BARE_TOOL_RE = re.compile(r"\b(pick(?:axe)?|axe|sword|shovel|spade|hoe)s?\b")
TOOL_MAT = {"wood": "wooden", "wooden": "wooden", "stone": "stone", "cobble": "stone", "cobblestone": "stone", "iron": "iron", "diamond": "diamond"}
TOOL_KIND = {"pick": "pickaxe", "spade": "shovel"}
DELIVER_RE = re.compile(r"\b(?:get|bring|give|fetch|grab|make|craft|build)\s+me\b|\bfor me\b")

def _tool_order(text: str, deliver: bool) -> Optional[Dict[str, Any]]:
    m = TOOL_RE.search(text)
    if m:
        mat, tool = TOOL_MAT[m.group(1)], m.group(2)
    else:
        m = BARE_TOOL_RE.search(text)
        if not m: return None
        mat, tool = "stone", m.group(1)
    tool = TOOL_KIND.get(tool, tool)
    return {"kind": "tool", "mat": mat, "tool": tool, "item": f"{mat}_{tool}", "deliver": deliver, "label": f"{mat} {tool}"}

DELIVER_ITEM = {"diamonds": "diamond", "iron": "iron", "coal": "coal", "wood": "log", "cobblestone": "cobblestone"}
ORDER_MODES = [
    (r"\b(stop|quit|don'?t) follow", "resume"),
    (r"\b(go (play|have fun)|do your (own )?thing|you'?re free|carry on|resume|as you were|do whatever|back to (work|playing))\b", "resume"),
    (r"\bfollow( me)?\b|\bstick (with|close to) me\b|\bcome with me\b|\bstay close\b", "follow"),
    (r"\b(come (here|back)|over here|get over here|return to me|back to me|where are you)\b", "come"),
    (r"\b(stay( here| put)?|wait( here)?|stop|hold (on|still)|don'?t move|freeze|halt)\b", "stay"),
]
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
    (r"\b(build|make|construct)\b.*\b(house|home|base)\b|\bhouse\b", "build_house"),
    (r"\btorch", "craft_torches"),
    (r"\b(sort|organi[sz]e|tidy|chest|inventory)\b|\bput (stuff|things|it|everything) away\b", "organize_inventory"),
    (r"\bcook\b", "cook_meat"),
    (r"\b(food|hungry|hunt|meat|cows?|pigs?)\b", "hunt_food"),
    (r"\b(shelter|hut|hide)\b", "build_dirt_hut"),
    (r"\bfurnace\b", "craft_furnace"),
    (r"\b(branch ?mine|mine|mining|dig|ores?|caves?)\b", "branch_mine"),
    (r"\b(explore|look around|wander|scout)\b", "explore"),
]

def _parse_order(text: str) -> Optional[Dict[str, Any]]:
    t = text.lower()
    for pattern, mode in ORDER_MODES:
        if re.search(pattern, t): return {"kind": mode}
    deliver = bool(DELIVER_RE.search(t))
    drop = DROP_RE.search(t)
    if drop:
        rest = drop.group(1)
        n = re.search(r"\b(\d{1,4})\b", rest)
        item = " ".join(GIVE_FILLER.sub(" ", re.sub(r"\b\d{1,4}\b", " ", rest)).split())
        if item: return {"kind": "drop", "item": item, "count": int(n.group(1)) if n else None}
    give = GIVE_RE.search(t)
    if give:
        rest = give.group(1)
        n = re.search(r"\b(\d{1,3})\b", rest)
        item = " ".join(GIVE_FILLER.sub(" ", re.sub(r"\b\d{1,3}\b", " ", rest)).split())
        if item: return {"kind": "give", "item": item, "count": min(max(int(n.group(1)), 1), 64) if n else None}
    tool = _tool_order(t, deliver)
    if tool: return tool
    for pattern, label, items, default in ORDER_COLLECT:
        if re.search(pattern, t):
            n = re.search(r"\b(\d{1,3})\b", t)
            return {"kind": "collect", "label": label, "items": items, "deliver": deliver, "amount": min(max(int(n.group(1)), 1), 64) if n else default}
    for pattern, goal in ORDER_TASKS:
        if re.search(pattern, t): return {"kind": "house"} if goal == "build_house" else {"kind": "once", "goal": goal}
    return None

def _held_item(inv: Dict[str, int], term: str) -> Optional[str]:
    t = "_".join(term.lower().split())
    for cand in (t, t[:-1] if t.endswith("s") else t, t[:-2] if t.endswith("es") else t):
        if inv.get(cand): return cand
    word = t[:-1] if t.endswith("s") else t
    names = [n for n in inv if word in n]
    return max(names, key=lambda n: inv[n]) if names else None

def _give_fallback(item: str, deliver_count: Optional[int]) -> Optional[Dict[str, Any]]:
    tool = _tool_order(item, True)
    if tool: return tool
    for pattern, label, items, default in ORDER_COLLECT:
        if re.search(pattern, item):
            return {"kind": "collect", "label": label, "items": items, "deliver": True, "amount": deliver_count or default}
    return None

ORDER_ALTERNATIVE = {"mine_iron_ore": "branch_mine", "mine_coal": "branch_mine", "gather_cobblestone": "branch_mine", "mine_stone": "branch_mine", "chop_trees": "explore", "collect_wood": "explore"}

def _tool_step(tier: int, inv: Dict[str, int], near: Dict[str, bool]) -> Optional[str]:
    tech = _tech_goal(inv, near)
    if tech or tier <= 2: return tech
    return _progress_goal(inv, near) or "mine_iron_ore"

def _collect_step(label: str, inv: Dict[str, int], near: Dict[str, bool]) -> Optional[str]:
    pick, tech = _pickaxe(inv), _tech_goal(inv, near)
    if label == "wood": return "chop_trees"
    if label == "cobblestone": return "gather_cobblestone" if pick >= 1 else tech
    if label == "coal": return "mine_coal" if pick >= 1 else tech
    if label == "iron": return "mine_iron_ore" if pick >= 2 else tech
    if label == "diamonds":
        if pick < 2: return tech
        if pick < 3: return _progress_goal(inv, near) or "mine_iron_ore"
        return "branch_mine"
    return None

OVERRIDE_SYSTEM = (
    "You control a Minecraft player. Your player just gave you an order. "
    "Choose the one goal from the list that best carries it out. If the message "
    "isn't actually an order for you, choose \"none\". Reply with JSON only: "
    '{"goal": "<one of the listed names, or none>", "reason": "<one short sentence>"}'
)

FRONTIER_DIRECTIVE = (
    "You are playing Minecraft. Respond with exactly one action from the list, then a "
    "one-line reason. Do not plan more than one step ahead. If an action is refused, "
    "choose a different one. Use the JSON format above.")
REFUSAL_MEMORY_S = 300
REFUSED_REST_S = 60

class CloudModelError(RuntimeError):
    def __init__(self, label: str, error: str):
        self.label, self.error = label, error
        super().__init__(f"{label}: {error}")

# ─── Social Prompts ───────────────────────────────────────────────────────────
ACK_TIMEOUT_S = 6.0
ACK_MAX_WORDS = 9
SOCIAL_POLL_S = 1.5
ACK_SYSTEM = social.persona_prompt()
ACK_PROMPT = (
    '{who} just asked you in Minecraft: "{order}". You are already doing it. '
    "Reply with ONE short game-chat line, under 10 words, in your own voice "
    "(like \"Right behind you!\" or \"Ugh, fine, I'll chop the tree.\"). If the job "
    "is tedious and your patience is low, you may complain a little while agreeing. "
    "Only the line: no quotes, no emoji, no name prefix.")
_STOCK_OBSERVE = {"mob": "Heads up, something's out there.", "hurt": "Ow. I could use a break.", "hungry": "I'm getting hungry.", "night": "Night's here. Stay close."}
_STOCK_ACKS = {
    "follow":  ["Right behind you!", "Lead the way!", "Coming, coming!"],
    "come":    ["On my way!", "Coming over!", "Be right there!"],
    "stay":    ["I'll wait right here.", "Staying put!", "Okay, I'll hang here."],
    "resume":  ["Free time, yay!", "Okay, I'll do my own thing."],
    "collect": ["Leave it to me!", "On it, back soon!", "Coming right up!"],
    "tool":    ["One sec, crafting!", "Leave it to me!"],
    "house":   ["A house? Ooh, on it!", "Time to build!"],
}
_ROBOTIC = re.compile(r"\b(executing|action|command|bot|ai|assistant)\b", re.I)

def clean_ack(raw: str) -> Optional[str]:
    line = next((l for l in str(raw or "").splitlines() if l.strip()), "")
    line = re.sub(r"<[^>]+>", "", line)
    line = re.sub(r"^\s*(?:lumi|lumibot)\s*[:>-]\s*", "", line, flags=re.I)
    line = line.strip().strip("\"'`*").strip()
    line = "".join(ch for ch in line if ch.isprintable() and ord(ch) < 0x2000)
    words = line.split()
    if not words or len(words) > ACK_MAX_WORDS or _ROBOTIC.search(line): return None
    line = " ".join(words)[:80]
    return line if line[-1] in ".!?~" else line + "!"

def stock_ack(kind: str) -> str:
    return random.choice(_STOCK_ACKS.get(kind, ["On it!", "You got it!", "Okay, leave it to me!"]))

def _read_gaming_mode() -> bool:
    try:
        with open(GAMING_MODE_PATH, encoding="utf-8") as f: return bool(json.load(f).get("enabled", False))
    except (OSError, ValueError): return False

# ─── Missing Helpers ──────────────────────────────────────────────────────────
def _inv(status: Dict) -> Dict[str, int]:
    return {i["name"]: i["count"] for i in status.get("inventory") or []}

def _fmt(v: Optional[float]) -> str:
    return "?" if v is None else str(round(v, 1))

def _pos_text(st: Dict) -> str:
    p = st.get("position") or {}
    return f"{p.get('x', 0):.0f}, {p.get('y', 0):.0f}, {p.get('z', 0):.0f}"

def _environment_text(env: Optional[Dict]) -> str:
    if not env: return ""
    parts = [env.get("in_cave") and "You are in a cave." or "You are outdoors.", env.get("is_day") and "It's day." or "It's night."]
    for h in env.get("hazards") or []: parts.append(f"{h[0].upper()}{h[1:]}.")
    if env.get("oxygen", 20) < 20: parts.append(f"Oxygen is {env['oxygen']}/20.")
    if env.get("emergency"): parts.append(f"EMERGENCY: {env['emergency']}.")
    return " ".join(parts)

def _all_goal_names() -> List[str]:
    return list(BUILTIN_SKILLS.keys()) + list(FREE_GOALS.keys())

def _goal_desc(goal: str) -> str:
    return _TIERS.get(goal, {}).get("desc") or FREE_GOALS.get(goal, {}).get("desc") or goal

def _skill_code(goal: str, status: Dict) -> Optional[str]:
    spec = FREE_GOALS.get(goal, {}).get("code")
    if callable(spec): return spec(status)
    if spec: return spec
    return BUILTIN_SKILLS.get(goal)

def _wanders(code: str) -> bool:
    return "exploreUntil" in code

def _box_guard(code: str, status: Dict) -> str:
    try: return governor.wrap_explore(code, status.get("position") or {})
    except Exception: return code

def _goal_steps(goal: str, status: Dict) -> Optional[List[Step]]:
    steps = FREE_GOALS.get(goal, {}).get("steps")
    if callable(steps): return steps(status)
    return steps or CURRICULUM_STEPS.get(goal)

def _ordered(options: Dict[str, str]) -> Dict[str, str]:
    return options

def _missing_ollama_model(model: str) -> Optional[str]:
    try:
        import urllib.request, json
        with urllib.request.urlopen(f"{_OLLAMA}/api/tags", timeout=2) as r:
            tags = json.loads(r.read())
            models = [m["name"].split(":")[0] for m in tags.get("models", [])]
            if model.split(":")[0] not in models: return model
    except Exception: pass
    return None

# ─── Agent ────────────────────────────────────────────────────────────────────
class MinecraftAgent(BaseGameAgent):
    GAME_ID = "minecraft"
    GAME_NAME = "MinecraftAgent"
    EVENT_TAG = "MINECRAFT EVENT"

    def __init__(self) -> None:
        super().__init__(token_path=TOKEN_PATH, reconnect_delay=5.0)
        os.makedirs(MC_DIR, exist_ok=True)
        self._paused = False
        self._gaming = _read_gaming_mode()
        self._wake = asyncio.Event()
        self._override_goal: Optional[str] = None
        self._loop_task: Optional[asyncio.Task] = None
        self._recent_goals: List[Tuple[str, bool]] = []
        self._failures: Dict[str, int] = {}
        self._cooldown_until: Dict[str, float] = {}
        self._persona = social.load_persona()
        self._persona_text = social.persona_prompt(self._persona)
        self._social = social.SocialMemory()
        self._last_reply = 0.0
        self._last_reply_to: Dict[str, float] = {}
        self._last_observe = 0.0
        self._observed: Dict[str, float] = {}
        self._social_task: Optional[asyncio.Task] = None
        self._hut_this_night = False
        self._progress = 0
        self._wanted: Dict[str, int] = {}
        self._project_until = 0.0
        self._band: Optional[str] = None
        self._order: Optional[Dict[str, Any]] = None
        self._attached = DEFAULT_MODE != "free"
        self._mode: Optional[str] = "follow" if self._attached else None
        self._done_order: Optional[Tuple[Dict[str, Any], str]] = None
        self._last_narration = 0.0
        self._last_status_push = 0.0
        self._last_done: Optional[str] = None
        self._last_look: Optional[Tuple[float, str, str]] = None
        self._last_look_at = 0.0
        self._looking = False
        self._autoplay_was = True
        self._defend_backoff_until = 0.0
        self._llm_ok = chat_sync is not None and GOAL_PICKER == "llm" and bool(GOAL_MODEL)
        self._model_choice = "local"
        self._model_spec: Optional[str] = GOAL_MODEL
        self._cloud_pause: Optional[Dict[str, Any]] = None
        self._control_seq: Optional[int] = None
        self._placed: List[Tuple[int, int, int, float]] = []
        self._last_status: Dict[str, Any] = {}
        self._refusal: Optional[Tuple[float, str, str]] = None
        self._tel: Dict[str, Any] = {
            "bot_status": "unreachable", "lumi_sdk": "offline", "position": None, "health": None, "food": None,
            "inventory": [], "current_goal": None, "goal_description": None, "goal_source": None,
            "current_thought": "Starting up...", "current_order": None, "mode": None, "loop_state": "starting",
            "last_update": None, "skills_verified": 0, "errors": [], "agent_model": None, "cloud_pause": None, "last_action": None,
        }

    async def setup(self) -> None:
        self.register_action(action("mc_agent_status", "Get the Minecraft autonomous agent's current goal...").capabilities("read").build(), self._h_status)
        self.register_action(action("mc_agent_set_goal", "Tell the Minecraft autonomous agent what to do next.").param("goal", "string", description="Goal name", required=True).capabilities("write").build(), self._h_set_goal)
        self.register_action(action("mc_agent_pause", "Pause the Minecraft agent's autonomous skill loop.").capabilities("write").build(), self._h_pause)
        self.register_action(action("mc_agent_resume", "Resume the Minecraft agent's autonomous skill loop.").capabilities("write").build(), self._h_resume)
        self.register_action(action("mc_agent_look_around", "Look through your Minecraft body's eyes...").param("question", "string").capabilities("read").build(), self._h_look_around)
        await self.send_context("Minecraft autonomous agent is online...", silent=True)
        self._tel["lumi_sdk"] = "connected"
        self._record("sdk", "Connected to Lumi SDK")

    async def on_gaming_mode(self, enabled: bool, wake: bool) -> None:
        changed = enabled != self._gaming
        self._gaming = enabled
        if changed:
            self._record("agent", f"gaming mode {'on' if enabled else 'off'}")
            if enabled and not self._has_work():
                asyncio.create_task(self._bot_action("stop", {}))
        if wake or not enabled: self._wake.set()

    def _has_work(self) -> bool:
        return bool(self._order or self._override_goal or self._done_order)

    async def _gaming_sleep(self) -> None:
        if self._tel["loop_state"] != "gaming mode":
            self._tel["loop_state"] = "gaming mode"
            self._think("Gaming mode: resting until I get an order.")
            self._flush_telemetry()
            await self._bot_action("stop", {})
        await self._wake.wait()
        self._wake.clear()

    async def on_disconnect(self) -> None:
        self._tel["lumi_sdk"] = "offline"
        self._record("sdk", "Disconnected from Lumi SDK")

    async def loop(self) -> None:
        await self._skill_loop()

    async def run(self) -> None:
        warned = False
        while not self._stop:
            if await self._lumi_port_open():
                warned = False
                try: await self._connect_and_loop()
                except Exception as e: self._record("error", f"Lumi SDK connection error: {e}")
            elif not warned:
                print("[mc-agent] Lumi is not running (127.0.0.1:5056)...")
                self._tel["lumi_sdk"] = "offline"
                warned = True
            await asyncio.sleep(self.reconnect_delay)

    async def _lumi_port_open(self) -> bool:
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), timeout=1.0)
            writer.close()
            return True
        except Exception: return False

    async def _h_status(self, _args: Dict) -> tuple:
        t = self._tel
        inv = ", ".join(f'{i["name"]}×{i["count"]}' for i in t["inventory"][:8]) or "empty"
        pos = t["position"]
        pos_str = f'{pos["x"]}, {pos["y"]}, {pos["z"]}' if pos else "unknown"
        lines = [
            f'Loop: {t["loop_state"]}{" (paused)" if self._paused else ""}',
            f'Bot: {t["bot_status"]} at {pos_str}',
            f'Health: {t["health"] or "?"}/20 Food: {t["food"] or "?"}/20 Armor: {t.get("armor") or 0}/20',
            f'Hotbar: {self._hotbar_text()}',
            f'Goal: {t["current_goal"] or "none"} — {t["goal_description"] or ""}',
            f'Recent goals: {self._recent_text(5)}', f'Resting: {self._cooldown_text()}',
            f'Order: {self._order_text() or "none"}', f'Inventory: {inv}', f'Tech progress: {self._progress}/{len(CURRICULUM)}',
        ]
        return True, "\n".join(lines)

    def _hotbar_text(self) -> str:
        bar, held = self._tel.get("hotbar") or [], self._tel.get("held_slot")
        if not bar: return "unknown"
        return ", ".join(f"{i + 1}:{name or '-'}{' (held)' if i == held else ''}" for i, name in enumerate(bar))

    async def _h_set_goal(self, args: Dict) -> tuple:
        name = str(args.get("goal") or "").strip()
        if name not in _all_goal_names(): return False, f"Unknown goal '{name}'."
        self._override_goal = name
        self._record("agent", f"Goal overridden to: {name}")
        return True, f"Goal set to: {name}"

    async def _h_pause(self, _args: Dict) -> tuple:
        self._paused = True
        self._tel["loop_state"] = "paused"
        self._think("Paused.")
        return True, "Paused"

    async def _h_resume(self, _args: Dict) -> tuple:
        self._paused = False
        self._tel["loop_state"] = "running"
        self._record("agent", "Loop resumed")
        return True, "Resumed"

    async def _h_look_around(self, args: Dict) -> tuple:
        question = " ".join(str(args.get("question") or "").split())[:200]
        q = f"This is a first-person view in a blocky video game. {question} Answer in one or two sentences." if question else "This is a first-person view in a blocky video game. Describe what you see in one or two sentences."
        loop = asyncio.get_running_loop()
        answer, note = await loop.run_in_executor(None, game_vision.look, BOT_API, q)
        return (True, answer) if answer else (False, f"Couldn't see anything: {note}")

    async def _glance(self) -> None:
        loop = asyncio.get_running_loop()
        self._looking = True
        try: answer, note = await loop.run_in_executor(None, game_vision.look, BOT_API, GLANCE_QUESTION)
        except Exception as e: answer, note = "", str(e)
        finally: self._looking = False
        if not answer:
            self._record("vision", f"couldn't glance around: {note}")
            return
        self._last_look = (time.time(), answer, "glance")
        self._record("vision", f"glanced around: {answer}")

    async def _look_when_stuck(self, desc: str) -> None:
        loop = asyncio.get_running_loop()
        self._looking = True
        try: answer, note = await loop.run_in_executor(None, game_vision.look, BOT_API)
        except Exception as e: answer, note = "", str(e)
        finally: self._looking = False
        if not answer:
            self._record("vision", f"couldn't look around: {note}")
            return
        self._last_look = (time.time(), answer, "stuck")
        self._record("vision", f"stuck ({desc.lower()}), looked around: {answer}")
        self._think(f"I'm stuck. Looking around... {answer[:140]}")
        await self._narrate(f"I got stuck trying to {desc.lower()}, so I looked around. What I see: {answer}")

    async def _bot_get(self, path: str) -> Optional[Dict]:
        loop = asyncio.get_running_loop()
        try:
            def _fetch():
                with urllib.request.urlopen(f"{BOT_API}{path}", timeout=4) as r: return json.loads(r.read())
            return await loop.run_in_executor(None, _fetch)
        except urllib.error.URLError: return None
        except Exception as e:
            self._record("error", f"GET {path}: {e}")
            return None

    def _governor_state(self) -> Dict[str, Any]:
        st = self._last_status or {}
        owner = (st.get("owner") or {}).get("name")
        return {"position": st.get("position") or self._tel.get("position"), "inventory": _inv(st), "placed": self._placed, "players": [owner] if owner else [], "food": _EDIBLE}

    def _govern(self, action_name: str, args: Dict, intent: Optional[str]) -> Dict[str, Any]:
        verdict = governor.check(action_name, args, self._governor_state(), intent=intent)
        self._tel["last_action"] = {"name": action_name, "admitted": verdict["admit"], "reason": verdict["reason"], "goal": intent, "time": time.strftime("%H:%M:%S")}
        if not verdict["admit"]: self._record("governor", f"refused {action_name} ({intent or 'no goal'}): {verdict['reason']}")
        return verdict

    async def _bot_action(self, action_name: str, args: Dict, intent: Optional[str] = None) -> Dict:
        intent = intent or self._tel.get("current_goal")
        verdict = self._govern(action_name, args, intent)
        if not verdict["admit"]:
            self._flush_telemetry()
            return {"success": False, "message": governor.REFUSED_PREFIX + verdict["reason"]}
        result = await self._bot_action_raw(action_name, args)
        if result.get("success"): self._placed = governor.remember_placement(self._placed, action_name, args)
        return result

    async def _bot_action_raw(self, action_name: str, args: Dict) -> Dict:
        loop = asyncio.get_running_loop()
        try:
            def _post():
                body = json.dumps({"action": action_name, "args": args}).encode()
                req = urllib.request.Request(f"{BOT_API}/action", data=body, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=ACTION_TIMEOUT) as r: return json.loads(r.read())
            result = await loop.run_in_executor(None, _post)
            self._bot_offline_warned = False
            return result
        except (urllib.error.URLError, ConnectionError) as e:
            if not isinstance(e, urllib.error.HTTPError):
                if not getattr(self, "_bot_offline_warned", False):
                    self._bot_offline_warned = True
                    self._record("warn", "Minecraft bot is offline, cannot execute action")
                return {"success": False, "error": "offline", "message": "Minecraft bot is offline"}
            self._record("error", f"action '{action_name}': bot.js said {e.code}")
            return {"success": False, "message": f"bot.js said {e.code}"}
        except Exception as e:
            self._record("error", f"action '{action_name}': {e}")
            return {"success": False, "message": str(e)}

    async def _skill_loop(self) -> None:
        self._tel["loop_state"] = "running"
        if self._llm_ok:
            loop = asyncio.get_running_loop()
            missing = await loop.run_in_executor(None, _missing_ollama_model, GOAL_MODEL)
            if missing:
                self._llm_ok = False
                self._record("warn", f"game model {GOAL_MODEL} is not installed — picking goals at random. Run: ollama pull {missing}")
        self._record("agent", "Skill loop started" + (f" (goal picker: {GOAL_MODEL})" if self._llm_ok else " (goal picker: random)"))
        if self._social_task is None or self._social_task.done(): self._social_task = asyncio.create_task(self._social_loop())

        while True:
            try:
                if self._gaming and not self._has_work(): await self._gaming_sleep()
                else: await asyncio.sleep(LOOP_INTERVAL)
                if not self._autoplay_allowed() or self._paused: continue
                self._check_control()
                self._refresh_model_info()
                if self._cloud_pause:
                    if self._tel["loop_state"] != "paused_cloud_error":
                        self._tel["loop_state"] = "paused_cloud_error"
                        self._flush_telemetry()
                    continue

                status = await self._bot_get("/status")
                if not status:
                    self._tel["bot_status"] = "unreachable"
                    self._tel["loop_state"] = "waiting for bot"
                    self._think("Waiting for the game to start...")
                    continue
                self._last_status = status
                self._tel.update({
                    "bot_status":  "ready" if status.get("ready") else "spawning", "position": status.get("position"),
                    "health": status.get("health"), "food": status.get("food"), "inventory": status.get("inventory", []),
                    "armor": status.get("armor"), "held_slot": status.get("held_slot"),
                    "hotbar": [(s or {}).get("name") for s in (status.get("slots") or [])[:9]],
                    "last_update": time.strftime("%H:%M:%S"), "loop_state": "running",
                })
                if status.get("is_day", True): self._hut_this_night = False
                self._progress = _progress_count(_inv(status), status.get("nearby") or {})
                self._flush_telemetry()
                band_changed = self._update_band(status)
                await self._push_status(status, force=band_changed)
                if not status.get("ready"):
                    self._think("Waiting to spawn in...")
                    continue
                await self._take_directive(status)
                if VISION_EVERY_S > 0 and not self._looking and time.time() - self._last_look_at > VISION_EVERY_S:
                    self._last_look_at = time.time()
                    asyncio.create_task(self._glance())
                if status.get("sleeping"):
                    self._tel["current_goal"], self._tel["goal_description"] = "sleep_in_bed", "Sleeping"
                    self._think("Zzz. Sleeping until morning.")
                    continue
                if await self._survive(status): continue
                if await self._hold_mode(status): continue

                picked = await self._pick_goal(status)
                if self._cloud_pause: continue
                if picked is None:
                    self._think("Nothing I can do right now. Taking a breather.")
                    await asyncio.sleep(10)
                    continue
                goal_name, source, reason = picked
                desc = _goal_desc(goal_name)
                self._tel.update({"current_goal": goal_name, "goal_description": desc, "goal_source": source})
                self._think(desc + (f" — {reason}" if reason else ""))
                via = f" via {self._model_spec}" if source == "llm" else ""
                self._record("curriculum", f"Goal: {desc} ({source}{via}{': ' + reason if reason else ''})")
                await self._narrate(f"I'm starting something new: {desc.lower()}." + (f" Why: {reason}" if reason else ""))
                if goal_name in PROJECT_GOALS: self._project_until = time.time() + PROJECT_HOLD_S
                await self._push_status(status, force=True)

                ok, msg = await self._execute_goal(goal_name, status)
                if not ok and "interrupted by" in msg:
                    self._record("agent", f"{goal_name} {msg}")
                    self._tel["current_goal"] = None
                    continue
                self._remember_goal(goal_name, ok)
                refused = msg.startswith(governor.REFUSED_PREFIX)
                if refused:
                    self._refusal = (time.time(), goal_name, msg[len(governor.REFUSED_PREFIX):])
                    self._cooldown_until[goal_name] = time.time() + REFUSED_REST_S
                if source == "order":
                    if refused and self._order: self._order["fails"] = FAIL_STREAK - 1
                    await self._order_result(goal_name, ok, msg)

                if ok:
                    self._failures.pop(goal_name, None)
                    self._record("result", f"OK {goal_name}: {msg}")
                    self._think(f"Done: {desc.lower()}. What next?")
                    self._last_done = desc
                    if goal_name in SUCCESS_COOLDOWN_S: self._cooldown_until[goal_name] = time.time() + SUCCESS_COOLDOWN_S[goal_name]
                    if goal_name == "build_dirt_hut": self._hut_this_night = True
                    if goal_name == "build_house" and msg.startswith("Built a"):
                        self._wanted.clear()
                        await self._narrate("I just finished building a real house! Floor, walls, windows, a roof, the works.", major=True, event_kind="house")
                        await self.remember(f"Built a {_house_material(_inv(status))} house near {_pos_text(status)}.")
                    elif goal_name == "build_house": self._wanted.clear(); self._project_until = time.time() + PROJECT_HOLD_S
                    if goal_name == "return_to_player":
                        self._project_until = 0.0
                        await self._push_status(status, force=True)
                    if goal_name == "retrieve_items": await self._narrate(f"I went back to where I died and got my stuff back. {msg}", major=True)
                    if goal_name == "mine_diamond" or (goal_name == "branch_mine" and "diamond" in msg):
                        await self._narrate("I just found and mined diamonds!", major=True, event_kind="diamond")
                        await self.remember(f"Found and mined diamonds near {_pos_text(status)}.")
                    elif goal_name in _TIERS:
                        await self._narrate(f"I just finished a milestone: {desc.lower()}.", event_kind="milestone")
                        if goal_name in REMEMBERED_TIERS: await self.remember(f"{REMEMBERED_TIERS[goal_name]} ({_pos_text(status)}).")
                    self._tel["current_goal"] = None
                else:
                    n = self._failures[goal_name] = self._failures.get(goal_name, 0) + 1
                    self._record("error", f"FAIL {goal_name} (x{n}): {msg}")
                    if n >= 2 and STUCK_RE.search(msg) and time.time() - self._last_look_at > LOOK_EVERY_S:
                        self._last_look_at = time.time()
                        asyncio.create_task(self._look_when_stuck(desc))
                    missing = re.search(r"Missing materials: (\w+)×(\d+)", msg)
                    if missing:
                        self._wanted[missing.group(1)] = int(missing.group(2))
                        self._record("agent", f"house needs {missing.group(2)} more {missing.group(1)}")
                    self._think(f"That didn't work ({desc.lower()}): {msg[:120]}")
                    if goal_name in FAIL_REST_S: self._cooldown_until[goal_name] = time.time() + FAIL_REST_S[goal_name]
                    if n >= FAIL_STREAK:
                        self._failures.pop(goal_name, None)
                        self._cooldown_until[goal_name] = time.time() + FAIL_COOLDOWN_S
                        self._record("warn", f"{goal_name} failed {n} times in a row — resting it for {FAIL_COOLDOWN_S // 60} min")
                        await self._narrate(f"I keep failing at this: {desc.lower()}. The game says: {msg[:160]}")
                    await asyncio.sleep(5)
                self._flush_telemetry()
            except asyncio.CancelledError: break
            except Exception as e:
                self._record("error", f"Loop exception: {e}")
                await asyncio.sleep(5)
        self._tel["loop_state"] = "stopped"
        self._flush_telemetry()

    async def _survive(self, status: Dict) -> bool:
        emergency = status.get("emergency")
        if emergency:
            env = status.get("environment") or {}
            self._tel["current_goal"], self._tel["goal_description"] = "survive", emergency
            self._think(f"Swimming up for air! Oxygen {env.get('oxygen', '?')}/20." if emergency == "drowning" else "Lava! Getting away from it.")
            await asyncio.sleep(1)
            return True
        threats = [t for t in status.get("threats") or [] if t.get("distance", 99) <= DEFEND_RANGE and t.get("name") != "enderman"]
        if threats and not status.get("defending") and time.time() >= self._defend_backoff_until:
            self._tel["current_goal"], self._tel["goal_description"] = "defend", f"Fight off a {threats[0]['name']}"
            self._record("survival", f"{threats[0]['name']} at {threats[0]['distance']} blocks — defending")
            self._think(f"A {threats[0]['name'].replace('_', ' ')} is right there! Fighting it off.")
            r = await self._bot_action("defend", {})
            self._record("result" if r.get("success") else "error", f"defend: {r.get('message', '')}")
            if not r.get("success"): self._defend_backoff_until = time.time() + 60
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
        if goal == "drop_item":
            drop = dict((self._order or {}).get("drop") or {})
            if not drop: return False, "Nothing to drop"
            self._record("skill", f"Executing: drop_item ({drop})")
            r = await self._bot_action("drop_item", drop)
            return bool(r.get("success")), r.get("message", "")
        if goal == "give_item":
            give = dict((self._order or {}).get("give") or {})
            if not give: return False, "Nothing to give"
            self._record("skill", f"Executing: give_item ({give})")
            r = await self._bot_action("give_item", give)
            m = social.GIFT_RE.match(str(r.get("message") or "")) if r.get("success") else None
            if m:
                self._social.gift(m.group(3), m.group(2), int(m.group(1)))
                self._record("social", f"gave {m.group(1)} {m.group(2)} to {m.group(3)}")
            return bool(r.get("success")), r.get("message", "")
        spec = FREE_GOALS.get(goal, {}).get("action")
        if spec:
            name, args = spec
            self._record("skill", f"Executing: {goal} ({name})")
            r = await self._bot_action(name, args(status) if callable(args) else dict(args))
            return bool(r.get("success")), r.get("message", "")
        code = _skill_code(goal, status)
        if not code: return False, f"No skill code for '{goal}'"
        if _wanders(code): code = _box_guard(code, status)
        self._record("skill", f"Executing: {goal}")
        args = {"code": code, "description": goal}
        if goal in GOAL_TIMEOUT_S: args["timeout_ms"] = GOAL_TIMEOUT_S[goal] * 1000
        result = await self._bot_action("execute_skill", args)
        msg = result.get("message", "")
        if result.get("success") or "primitives not loaded" not in msg: return bool(result.get("success")), msg

        steps = _goal_steps(goal, status)
        if not steps: return False, f"{msg} (no mineflayer fallback for {goal})"
        self._record("skill", f"Voyager unavailable — running {goal} as {len(steps)} plain action(s)")
        out = []
        for name, step_args, *optional in steps:
            r = await self._bot_action(name, step_args)
            out.append(r.get("message", ""))
            if not r.get("success") and not optional: return False, " | ".join(out)
        return True, " | ".join(out)

    async def _pick_goal(self, status: Dict) -> Optional[Tuple[str, str, str]]:
        if self._override_goal:
            goal, self._override_goal = self._override_goal, None
            return goal, "Lumi", ""
        death = status.get("death") or {}
        if death.get("recoverable") and self._available("retrieve_items", status):
            return ("retrieve_items", "routine", f"I died at {death['x']:.0f}, {death['y']:.0f}, {death['z']:.0f} {death['age_s']} s ago; my items are still there")
        inv = _inv(status)
        near = status.get("nearby") or {}
        if self._order:
            step = self._order_step(status)
            if step: return step, "order", self._order["text"]
        await self._announce_done_order()
        bed = _bed_routine(inv, status)
        if bed and self._available(bed, status): return bed, "routine", ("it's night" if bed == "sleep_in_bed" else "so I respawn here instead of at world spawn")
        back = self._should_return(status)
        if back: return "return_to_player", "proximity", back
        tech = _tech_goal(inv, near)
        if tech and self._available(tech, status): return tech, "tech tree", _TECH_REASON[tech]
        kit = _kit_goal(inv, near)
        if kit and self._available(kit[0], status): return kit[0], "tech tree", kit[1]
        if (_pickaxe(inv) >= 2 and status.get("is_day", True) and FREE_GOALS["get_bed"]["when"](inv, status) and self._available("get_bed", status)):
            return "get_bed", "tech tree", "a bed to set my respawn point and sleep through the night"
        gate_status = dict(status, wanted=self._wanted)
        for routine in ("go_home", "organize_inventory"):
            if FREE_GOALS[routine]["when"](inv, gate_status) and self._available(routine, status): return routine, "routine", ""
        options: Dict[str, str] = {}
        progress = _progress_goal(inv, near)
        if progress and self._available(progress, status): options[progress] = f"{_goal_desc(progress)} (next step toward better tools)"
        for name, g in FREE_GOALS.items():
            try:
                if g["when"](inv, gate_status) and self._available(name, status): options[name] = g["desc"]
            except Exception: pass
        if not options: return None
        if self._model_ready():
            for _attempt in range(2):
                try: picked = await self._llm_pick(status, options)
                except CloudModelError as e:
                    self._pause_for_cloud(e)
                    return None
                if not picked: break
                verdict = self._preview_goal(picked[0], status)
                if verdict["admit"]: return picked[0], "llm", picked[1]
                self._refusal = (time.time(), picked[0], verdict["reason"])
                self._cooldown_until[picked[0]] = time.time() + REFUSED_REST_S
                options = {k: v for k, v in options.items() if k != picked[0]}
                if not options: return None
        return self._random_pick(status, options, progress), "random", ""

    def _preview_goal(self, goal: str, status: Dict) -> Dict[str, Any]:
        spec = FREE_GOALS.get(goal, {}).get("action")
        if not spec: return {"admit": True, "reason": ""}
        name, args = spec
        try: args = args(status) if callable(args) else dict(args)
        except Exception: return {"admit": True, "reason": ""}
        return self._govern(name, args, goal)

    def _model_ready(self) -> bool:
        if chat_sync is None or GOAL_PICKER != "llm": return False
        return self._model_choice != "local" or self._llm_ok

    def _refresh_model_info(self) -> None:
        if model_router is None: return
        try: choice = model_router.agent_model_choice("minecraft")
        except Exception: choice = "local"
        spec = GOAL_MODEL if choice == "local" else model_router.cloud_agent_model(choice)
        if choice != self._model_choice or spec != self._model_spec:
            self._record("model", f"goal picker model: {self._model_spec} → {spec} ({choice})")
            self._model_choice, self._model_spec = choice, spec
            self._cloud_pause = None
            self._tel["cloud_pause"] = None
            if choice == "local": self._reenable_llm()
        self._tel["agent_model"] = model_router.agent_model_info(choice, GOAL_MODEL)

    def _check_control(self) -> None:
        try:
            with open(CONTROL_PATH, encoding="utf-8") as f: seq = json.load(f).get("seq")
        except (OSError, ValueError, AttributeError): return
        if self._control_seq is None:
            self._control_seq = seq
            return
        if seq != self._control_seq:
            self._control_seq = seq
            if self._cloud_pause:
                self._record("agent", "resumed from the Games page — trying the cloud model once")
                self._think("Resuming. Trying my game brain again.")
            self._cloud_pause = None
            self._tel["cloud_pause"] = None
            self._tel["loop_state"] = "running"
            if self._model_choice == "local": self._reenable_llm()

    def _pause_for_cloud(self, e: "CloudModelError") -> None:
        self._cloud_pause = {"provider": e.label, "error": e.error[:300], "since": time.strftime("%H:%M:%S")}
        self._tel["cloud_pause"] = self._cloud_pause
        self._tel["loop_state"] = "paused_cloud_error"
        self._record("error", f"Minecraft agent paused: {e.label} — {e.error[:200]}")
        self._think(f"Minecraft agent paused: {e.error[:140]}" if "key not set" in e.error else f"Paused: {e.label} didn't answer ({e.error[:120]}). Press Resume on the Games page.")
        self._flush_telemetry()

    async def _ask_model(self, system: str, prompt: str, options: Dict[str, Any]) -> str:
        loop = asyncio.get_running_loop()
        if self._model_choice == "local":
            resp = await loop.run_in_executor(None, lambda: chat_sync(GOAL_MODEL, [{"role": "system", "content": system}, {"role": "user", "content": prompt}], think=False, keep_alive=GOAL_KEEP_ALIVE, options=options))
            return (resp.content or "").strip()
        label = model_router.AGENT_MODEL_LABELS.get(self._model_choice, self._model_choice)
        try: spec = model_router.agent_model_spec(model_override=self._model_choice)
        except model_router.CloudKeyMissing as e: raise CloudModelError(label, str(e))
        self._model_spec = spec
        print(f"[mc-agent] model call → {spec}")
        messages = [{"role": "system", "content": system + "\n" + FRONTIER_DIRECTIVE}, {"role": "user", "content": prompt}]
        opts = {"temperature": options.get("temperature", 0.3), "num_predict": 200}
        try:
            resp = await asyncio.wait_for(loop.run_in_executor(None, lambda: chat_sync(spec, messages, options=opts)), timeout=_CLOUD_TIMEOUT_S)
        except asyncio.TimeoutError: raise CloudModelError(label, f"timed out after {_CLOUD_TIMEOUT_S:g} s")
        except Exception as e: raise CloudModelError(label, str(e) or type(e).__name__)
        return (resp.content or "").strip()

    async def _take_directive(self, status: Dict) -> None:
        if not status.get("directive_pending"): return
        got = await self._bot_get("/directive?take=1")
        d = (got or {}).get("directive")
        text = re.sub(r"\s+", " ", str((d or {}).get("text") or "")).strip()[:200]
        if not text: return
        in_game = d.get("source") == "chat"
        talk = d.get("source") in ("talk", "lumi")
        who = (d.get("from") or (status.get("owner") or {}).get("name") or "my player") if in_game else None
        heard = (f'{who} told me in game: "{text}"' if in_game else f'I was asked to "{text}" in Minecraft' if talk else f'I got an order from the Games page: "{text}"')
        self._record("agent", f"order ({who or ('talking to Lumi' if talk else 'Games page')}): {text}")

        order = _parse_order(text)
        if order is None: order = await self._override_order(text, status)
        if order is None and self._cloud_pause:
            await self._bot_action("chat", {"message": "My game brain is paused right now, so I can't work that out: " + text[:60]})
            return
        if order is None:
            await self._bot_action("chat", {"message": f"Sorry, I don't know how to do that: {text[:60]}"})
            self._record("warn", f"didn't understand the order: {text}")
            if talk: await self._narrate(f"{heard}, but I don't know how to do that in the game.", major=True)
            return

        if order["kind"] == "drop":
            inv = _inv(status)
            item = _held_item(inv, order["item"])
            if not item:
                await self._bot_action("chat", {"message": f"I don't have any {order['item']}."})
                self._record("warn", f"can't drop {order['item']}: not in inventory")
                if talk: await self._narrate(f"{heard}, but I don't have any {order['item']}.", major=True)
                return
            count = min(order["count"], inv[item]) if order["count"] else inv[item]
            order = {"kind": "once", "goal": "drop_item", "drop": {"name": item, "count": count}}

        if order["kind"] == "give":
            inv = _inv(status)
            item = _held_item(inv, order["item"])
            if item:
                plural = order["item"].endswith("s") and not item.endswith("s")
                count = order["count"] or (inv[item] if plural else 1)
                order = {"kind": "once", "goal": "give_item", "give": {"item_name": item, "count": min(count, inv[item])}}
            else:
                fallback = _give_fallback(order["item"], order["count"])
                if fallback is None:
                    await self._bot_action("chat", {"message": f"I don't have any {order['item']}."})
                    self._record("warn", f"can't give {order['item']}: not in inventory")
                    if talk: await self._narrate(f"{heard}, but I don't have any {order['item']}.", major=True)
                    return
                order = fallback

        if order.get("goal") in ("sleep_in_bed", "set_spawn") and not _bed_handy(_inv(status), status):
            order = {"kind": "once", "goal": "get_bed"}
            await self._bot_action("chat", {"message": "I don't have a bed. I'll make one first."})
        elif order.get("goal") == "sleep_in_bed" and not status.get("can_sleep"):
            order = {"kind": "once", "goal": "set_spawn"}
            await self._bot_action("chat", {"message": "I can only sleep at night. I'll set my spawn at the bed."})

        kind = order["kind"]
        if kind == "resume":
            self._order, self._mode, self._attached = None, None, False
            ack = "I'm free to do my own thing again"
        elif kind in ("follow", "stay"):
            self._order, self._mode = None, kind
            if kind == "follow": self._attached = True
            ack = "I'll follow them around" if kind == "follow" else "I'll stay right here"
        elif kind == "come":
            self._attached = True
            self._mode = "follow"
            self._order = {"kind": "once", "goal": "return_to_player"}
            ack = "I'm heading back to them"
        else:
            self._mode = "follow" if self._attached else None
            self._order = order
            if kind == "collect":
                order["start"] = sum(_inv(status).get(i, 0) for i in order["items"])
                ack = f"I'm going to get {order['amount']} {order['label']}" + (" and bring them over" if order.get("deliver") else "")
                if order.get("deliver"): order["give"] = {"item_name": DELIVER_ITEM[order["label"]], "count": order["amount"]}
            elif kind == "tool":
                ack = f"I'm going to make {'an' if order['label'][0] in 'aeiou' else 'a'} {order['label']}" + (" and hand it over" if order.get("deliver") else "")
                if order.get("deliver"): order["give"] = {"item_name": order["item"], "count": 1}
            elif kind == "once" and order["goal"] == "give_item":
                g = order["give"]
                ack = f"Here, I'll bring over {g['count']} {g['item_name'].replace('_', ' ')}"
            elif kind == "once" and order["goal"] == "drop_item":
                g = order["drop"]
                ack = f"Dropping {g['count']} {g['name'].replace('_', ' ')}"
            elif kind == "house":
                ack = "I'm going to build a house"
            else:
                ack = f"On it: {_goal_desc(order['goal']).lower()}"
        
        if self._order:
            self._order.update(text=text, who=who, fails=0, until=time.time() + 20 * 60)
        self._tel["current_order"], self._tel["mode"] = self._order_text() or None, self._mode
        self._think(f"New order: {text}")
        
        task = asyncio.create_task(self._ack_in_game(text, kind, who))
        self._ack_tasks = getattr(self, "_ack_tasks", set())
        self._ack_tasks.add(task)
        task.add_done_callback(self._ack_tasks.discard)
        
        if not talk and not in_game:
            await self._narrate(f"{heard}. {ack}.", major=True)
        await self._push_status(status, force=True)

    async def _ack_in_game(self, text: str, kind: str, who: Optional[str]) -> None:
        if who: self._social.order(who)
        status = self._last_status or {}
        prompt = (social.nearby_players_block(status, self._social) + "\n" + ACK_PROMPT.format(who=who or "your player", order=text))
        line = clean_ack(await self._say_line(prompt) or "")
        await self._bot_action("chat", {"message": line or stock_ack(kind)})

    async def _say_line(self, prompt: str) -> Optional[str]:
        if not self._model_ready(): return None
        opts = {"temperature": 0.9, "num_predict": 32}
        if not self._cloud_pause:
            try: return await asyncio.wait_for(self._ask_model(self._persona_text, prompt, opts), timeout=ACK_TIMEOUT_S)
            except Exception as e:
                err = getattr(e, "error", None) or str(e) or type(e).__name__
                self._record("warn", f"chat line: {self._model_choice} model failed ({str(err)[:120]})")
        if self._model_choice != "local" and chat_sync is not None and GOAL_MODEL:
            loop = asyncio.get_running_loop()
            try:
                resp = await asyncio.wait_for(loop.run_in_executor(None, lambda: chat_sync(GOAL_MODEL, [{"role": "system", "content": self._persona_text}, {"role": "user", "content": prompt}], think=False, keep_alive=GOAL_KEEP_ALIVE, options=opts)), timeout=ACK_TIMEOUT_S)
                return (resp.content or "").strip()
            except Exception as e: self._record("warn", f"chat line: local model failed too ({type(e).__name__})")
        return None

    async def _social_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(SOCIAL_POLL_S)
                if not self._autoplay_allowed(): continue
                got = await self._bot_get("/chat?take=1")
                msgs = (got or {}).get("chat") or []
                status = None
                if msgs or time.time() - self._last_observe >= social.OBSERVE_EVERY_S:
                    status = await self._bot_get("/status")
                if not status or not status.get("ready"): continue
                near = [p["name"] for p in social.nearby(status)]
                for name in self._social.seen(near): self._record("social", f"{name} is nearby")
                for m in msgs[-5:]: await self._hear(m, status, near)
                if time.time() - self._last_observe >= social.OBSERVE_EVERY_S:
                    self._last_observe = time.time()
                    await self._maybe_observe(status, near)
            except asyncio.CancelledError: raise
            except Exception as e: self._record("error", f"social loop: {e}")

    async def _hear(self, msg: Dict, status: Dict, near: List[str]) -> None:
        name, text = str(msg.get("from") or ""), str(msg.get("text") or "")
        if not name or not text: return
        greeted = self._social.chat(name, text)
        if greeted: self._record("social", f"{name} said hi; remembering them this session")
        now = time.time()
        if not social.wants_reply(name, text, near=name in near, now=now, last_reply=self._last_reply, last_reply_to=self._last_reply_to): return
        doing = self._order_text() or self._tel.get("goal_description") or "hanging around"
        prompt = (social.nearby_players_block(status, self._social) + "\n" + f"You're busy with: {doing}.\n" + f'{name} just said in game chat: "{text}"\n' + ("They just said hi to you for the first time today.\n" if greeted else "") + "Reply in game chat with ONE short line, under 10 words, in your own voice. Only the line: no quotes, no emoji, no name prefix.")
        line = social.clean_line(await self._say_line(prompt) or "")
        if not line and greeted: line = f"Hey {name}."
        if not line: return
        self._last_reply = time.time()
        self._last_reply_to[name] = self._last_reply
        self._record("social", f"replied to {name}: {line}")
        await self._bot_action("chat", {"message": line})

    async def _maybe_observe(self, status: Dict, near: List[str]) -> None:
        if not near or status.get("busy") or status.get("sleeping") or self._order: return
        if self._gaming and not self._has_work(): return
        seen = social.observe(status)
        if not seen: return
        key, fact = seen
        if time.time() - self._observed.get(key, 0) < social.OBSERVE_REPEAT_S: return
        self._observed[key] = time.time()
        prompt = (social.nearby_players_block(status, self._social) + "\n" + f"You notice: {fact}.\n" + "Say it out loud in game chat to the players near you, ONE short line under 10 words, in your own voice. Only the line.")
        line = social.clean_line(await self._say_line(prompt) or "") or _STOCK_OBSERVE.get(key.split(":")[0])
        if line:
            self._record("social", f"remarked: {line}")
            self._last_reply = time.time()
            await self._bot_action("chat", {"message": line})

    async def _override_order(self, text: str, status: Dict) -> Optional[Dict[str, Any]]:
        if not self._model_ready(): return None
        options = {g: d["desc"] for g, d in FREE_GOALS.items()}
        options.update({t["goal"]: t["desc"] for t in CURRICULUM})
        prompt = (f"The user has given you a new direct order: '{text}'. You must prioritize this above all current goals.\n" + f"Your inventory contains: {', '.join(f'{n}×{c}' for n, c in _inv(status).items()) or 'nothing'}. If the order is about something you already have, don't gather or craft it again.\n" + "Goals:\n" + "\n".join(f"- {k}: {v}" for k, v in options.items()) + "\n- none: not an order")
        try: reply = await self._ask_model(OVERRIDE_SYSTEM, prompt, {"temperature": 0.2, "num_ctx": 2048})
        except CloudModelError as e:
            self._pause_for_cloud(e)
            return None
        except Exception as e:
            self._record("warn", f"order model failed: {e}")
            return None
        m = re.search(r"\{.*\}", reply, re.S)
        try: goal = str(json.loads(m.group(0)).get("goal") or "").strip() if m else ""
        except json.JSONDecodeError: goal = ""
        if goal not in options: return None
        return {"kind": "house"} if goal == "build_house" else {"kind": "once", "goal": goal}

    def _order_step(self, status: Dict) -> Optional[str]:
        o, inv = self._order, _inv(status)
        if time.time() > o["until"]:
            self._record("warn", f"order expired: {o['text']}")
            self._order = None
            return None
        if o["kind"] == "once": return o["goal"]
        if o.get("delivering"): return "give_item"
        if o["kind"] == "house":
            if _house(status).get("complete"):
                self._finish_order_later("the house is built")
                return None
            if self._wanted.get("cobblestone"): return "gather_cobblestone"
            if self._wanted.get("oak_planks") or self._wanted.get("planks"): return "chop_trees"
            return "build_house"
        near = status.get("nearby") or {}
        if o["kind"] == "tool":
            reached = (inv.get(o["item"], 0) > 0 if o.get("deliver") else _tool_level(inv, o["tool"]) >= _TOOL_TIER[o["mat"]])
            what = f"made {'an' if o['label'][0] in 'aeiou' else 'a'} {o['label']}"
            step = None if reached else _craft_step(o["mat"], o["tool"], inv, near)
        else:
            reached = sum(inv.get(i, 0) for i in o["items"]) - o["start"] >= o["amount"]
            what = f"got {o['amount']} {o['label']}"
            step = None if reached else _collect_step(o["label"], inv, near)
        if reached:
            if o.get("deliver"):
                o["delivering"] = True
                return "give_item"
            self._finish_order_later(what)
            return None
        if step and not self._available(step, status): step = ORDER_ALTERNATIVE.get(step, step)
        return step

    def _finish_order_later(self, what: str) -> None:
        self._record("result", f"order done ({what}): {self._order['text']}")
        self._done_order = (self._order, what)
        self._order = None
        self._tel["current_order"] = None

    async def _order_result(self, goal: str, ok: bool, msg: str) -> None:
        o = self._order
        if not o: return
        if ok:
            o["fails"] = 0
            if o["kind"] == "once" or goal == "give_item": self._finish_order_later(msg if goal == "give_item" else _goal_desc(goal).lower())
        else:
            o["fails"] += 1
            if o["fails"] >= FAIL_STREAK:
                self._record("warn", f"giving up on the order: {o['text']}")
                self._order = None
                self._tel["current_order"] = None
                await self._narrate(f"I failed my order: \"{o['text']}\". I am giving up. (Last problem: {msg[:140]})", major=True, event_kind="order_failed")
        await self._announce_done_order()

    async def _announce_done_order(self) -> None:
        if self._done_order:
            order, what = self._done_order
            self._done_order = None
            await self._narrate(f'Done with the order "{order["text"]}": {what}.', major=True, event_kind="order_done")

    async def _hold_mode(self, status: Dict) -> bool:
        if self._mode == "stay":
            self._think("Staying put like I was told.")
            return True
        if self._mode == "follow":
            death = status.get("death") or {}
            bed = _bed_routine(_inv(status), status)
            if (self._order or self._override_goal or (death.get("recoverable") and self._available("retrieve_items", status)) or (bed and self._available(bed, status))): return False
            owner = status.get("owner")
            if not owner:
                self._think("Waiting for my player to come back online.")
                return True
            d = _owner_distance(status)
            if d is None or d > 5:
                self._think(f"Following {owner['name']}.")
                r = await self._bot_action("return_to_player", {"range": 3})
                if not r.get("success") and "interrupted" not in r.get("message", ""): self._record("warn", f"follow: {r.get('message', '')}")
            else:
                self._think(f"Sticking close to {owner['name']}.")
            return True
        return False

    def _order_text(self) -> str:
        o = self._order
        if not o: return {"follow": "following the player", "stay": "staying put"}.get(self._mode or "", "")
        if o["kind"] == "collect": return f"\"{o['text']}\" ({o['label']}, {o['amount']} wanted)"
        return f"\"{o['text']}\""

    def _should_return(self, status: Dict) -> Optional[str]:
        owner = status.get("owner")
        if not owner or not self._available("return_to_player", status): return None
        name, d = owner.get("name"), _owner_distance(status)
        if owner.get("called") and (d is None or d > 4): return f"{name} called me"
        leash = PROJECT_LEASH if time.time() < self._project_until else FAR_DIST
        if d is None: return f"{name} is out of sight"
        if d > leash: return f"{name} is {d:.0f} blocks away"
        return None

    def _update_band(self, status: Dict) -> bool:
        owner = status.get("owner")
        if not owner: band = None
        elif _owner_distance(status) is None: band = "out of sight"
        else:
            d = _owner_distance(status)
            band = "near" if d < NEAR_DIST else "roaming" if d <= FAR_DIST else "far"
        if band == self._band: return False
        if band and self._band: self._record("agent", f"{owner['name']}: {self._band} → {band}")
        self._band = band
        return True

    def _available(self, goal: str, status: Dict) -> bool:
        if time.time() < self._cooldown_until.get(goal, 0): return False
        if goal == "build_dirt_hut" and self._hut_this_night: return False
        return True

    def _random_pick(self, status: Dict, options: Dict[str, str], progress: Optional[str]) -> str:
        if not status.get("is_day", True):
            for night_goal in ("build_dirt_hut", "wait_out_night"):
                if night_goal in options: return night_goal
        if (status.get("food") or 20) < EAT_BELOW_FOOD and "hunt_food" in options: return "hunt_food"
        if "craft_torches" in options and "branch_mine" in options: return "craft_torches"
        if progress in options and random.random() < 0.7: return progress
        weights = {"chop_trees": 3, "explore": 2, "hunt_food": 1, "mine_coal": 2, "cook_meat": 2, "build_dirt_hut": 0, "wait_out_night": 0, "branch_mine": 3, "build_house": 4, "gather_cobblestone": 4}
        just_did = {g for g, _ in self._recent_goals[-2:]}
        names = [n for n in options if n not in just_did] or list(options)
        return random.choices(names, weights=[weights.get(n, 1) or 0.1 for n in names])[0]

    async def _llm_pick(self, status: Dict, options: Dict[str, str]) -> Optional[Tuple[str, str]]:
        inv = _inv(status)
        inv_text = ", ".join(f"{n}×{c}" for n, c in sorted(inv.items(), key=lambda kv: -kv[1])[:20]) or "empty"
        threats = ", ".join(f'{t["name"]} at {t["distance"]}' for t in status.get("threats") or []) or "none"
        is_day = status.get("is_day", True)
        prompt = (f"Current inventory: {inv_text} (logs: {_logs(inv)}).\n" + f"Current time: {'Day' if is_day else 'Night'}. " + f"Health {_fmt(status.get('health'))}/20, hunger {_fmt(status.get('food'))}/20. " + f"Hostiles nearby: {threats}.\n" + f"{_environment_text(status.get('environment'))}\n" + f"{self._player_line(status)}" + social.nearby_players_block(status, self._social) + "\n" + (f"What you can see right now (your eyes can be wrong): {self._last_look[1]}\n" if self._last_look and time.time() - self._last_look[0] < LOOK_MEMORY_S else "") + f"Recent goals (oldest first, with result): {self._recent_text(5)}.\n" + (f"Materials wanted for the house: {', '.join(f'{k}×{v}' for k, v in self._wanted.items())}.\n" if self._wanted else "") + "Rules: Do not pick build_dirt_hut if it is Day. Do not pick chop_trees if you already have 10+ logs. Do not repeat a goal that just succeeded unless nothing else makes sense.\n" + self._refusal_text() + "\n" + render_actions_prompt(_ordered(options), detail=self._model_choice != "local"))
        try: text = await self._ask_model(PICKER_SYSTEM + "\n" + self._picker_personality(), prompt, {"temperature": 0.7, "num_ctx": 2048})
        except CloudModelError: raise
        except Exception as e:
            self._record("warn", f"goal picker LLM failed ({e}) — using random picks for 5 min")
            self._llm_ok = False
            asyncio.get_running_loop().call_later(300, self._reenable_llm)
            return None
        m = re.search(r"\{.*\}", text, re.S)
        try: data = json.loads(m.group(0)) if m else {}
        except json.JSONDecodeError: data = {}
        goal = str(data.get("goal") or "").strip()
        if goal not in options: goal = next((k for k in options if k in text), "")
        if not goal:
            self._record("warn", f"goal picker gave no valid goal: {text[:120]}")
            return None
        return goal, str(data.get("reason") or "").strip()[:160]

    def _picker_personality(self) -> str:
        return ("You are " + self._persona["name"] + ". Let your personality shape the pick (high chaos: sometimes the fun or unexpected option; low patience: avoid long grinds when something else works; high empathy: stay useful to the players nearby):\n" + "\n".join(social.trait_lines(self._persona["traits"])))

    def _refusal_text(self) -> str:
        r, self._refusal = self._refusal, None
        if not r or time.time() - r[0] > REFUSAL_MEMORY_S: return ""
        self._record("governor", f"told the picker: {r[1]} was refused ({r[2]})")
        return f"Observation: action refused: {r[2]} (goal {r[1]}). Choose a different goal.\n"

    def _player_line(self, status: Dict) -> str:
        owner = status.get("owner")
        if not owner: return ""
        d = _owner_distance(status)
        where = f"{d:.0f} blocks away" if d is not None else "out of sight"
        return (f"You are playing near {owner['name']} ({where}). If you get too far ({FAR_DIST}+ blocks) or they call you, go back to them. Otherwise, do your own thing nearby.\n")

    def _reenable_llm(self) -> None:
        self._llm_ok = chat_sync is not None and GOAL_PICKER == "llm" and bool(GOAL_MODEL)

    async def _narrate(self, text: str, major: bool = False, event_kind: Optional[str] = None) -> None:
        if NARRATE == "off" or (NARRATE != "all" and not major): return
        now = time.time()
        if now - self._last_narration < NARRATE_COOLDOWN: return
        self._last_narration = now
        if event_kind: await self.send_context(f"[{self.EVENT_TAG}] {text}", silent=False, event_kind=event_kind)
        else: await self.send_context(f"[{self.EVENT_TAG}] {text}", silent=False)

    async def _push_status(self, status: Dict, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_status_push < STATUS_EVERY: return
        self._last_status_push = now
        inv = _inv(status)
        line = (f"Status: Health {_fmt(status.get('health'))}/20, Food {_fmt(status.get('food'))}/20, " + f"Pos {_pos_text(status)}. Goal: {self._tel.get('current_goal') or 'none'}. " + f"Inventory: {', '.join(f'{k}×{v}' for k, v in list(inv.items())[:5])}...")
        await self.send_context(line, silent=True)

    async def _flush_telemetry(self) -> None:
        try:
            with open(TELEMETRY_PATH, "w", encoding="utf-8") as f: json.dump(self._tel, f)
        except Exception: pass

    def _record(self, category: str, message: str) -> None:
        entry = {"time": time.strftime("%H:%M:%S"), "category": category, "message": message}
        self._tel["errors"].append(entry)
        if len(self._tel["errors"]) > 50: self._tel["errors"].pop(0)
        try:
            with open(EVENTS_PATH, "a", encoding="utf-8") as f: f.write(json.dumps(entry) + "\n")
        except Exception: pass

    def _think(self, thought: str) -> None:
        self._tel["current_thought"] = thought
        self._flush_telemetry()

    def _remember_goal(self, goal: str, ok: bool) -> None:
        self._recent_goals.append((goal, ok))
        if len(self._recent_goals) > 10: self._recent_goals.pop(0)

    def _recent_text(self, n: int) -> str:
        return ", ".join(f"{g} ({'ok' if ok else 'fail'})" for g, ok in self._recent_goals[-n:]) or "none"

    def _cooldown_text(self) -> str:
        now = time.time()
        active = [f"{g} ({int(t - now)}s)" for g, t in self._cooldown_until.items() if t > now]
        return ", ".join(active) or "none"

    def _autoplay_allowed(self) -> bool:
        try:
            with open(AUTOPLAY_PATH, encoding="utf-8") as f: allowed = json.load(f).get("allowed", True)
        except (OSError, ValueError): allowed = True
        if allowed != self._autoplay_was:
            self._autoplay_was = allowed
            self._record("agent", "Autoplay " + ("resumed" if allowed else "paused"))
        return allowed

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    agent = MinecraftAgent()
    try: asyncio.run(agent.run())
    except KeyboardInterrupt: print("Stopped.")
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
    data/minecraft/skills.json     — verified skill library
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
SKILLS_PATH    = os.path.join(MC_DIR, "skills.json")
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

# ─── Voyager curriculum ───────────────────────────────────────────────────────
# Each tier fires once its required items are in inventory. Quantities are
# sized so each tier leaves enough material for the next ones.
CURRICULUM: List[Dict[str, Any]] = [
    {"goal": "collect_wood",        "requires": {},                                    "desc": "Collect oak logs"},
    {"goal": "craft_planks",        "requires": {"oak_log": 5},                        "desc": "Craft wooden planks"},
    {"goal": "craft_workbench",     "requires": {"oak_planks": 4},                     "desc": "Craft a crafting table"},
    {"goal": "craft_wood_pickaxe",  "requires": {"crafting_table": 1, "oak_planks": 7}, "desc": "Craft sticks and a wooden pickaxe"},
    {"goal": "mine_stone",          "requires": {"wooden_pickaxe": 1},                 "desc": "Mine cobblestone"},
    {"goal": "craft_stone_pickaxe", "requires": {"cobblestone": 3, "stick": 2},        "desc": "Craft a stone pickaxe"},
    {"goal": "craft_furnace",       "requires": {"cobblestone": 8},                    "desc": "Craft a furnace"},
    {"goal": "mine_iron_ore",       "requires": {"stone_pickaxe": 1},                  "desc": "Find and mine iron ore"},
    {"goal": "smelt_iron",          "requires": {"raw_iron": 3},                       "desc": "Smelt iron ingots"},
    {"goal": "craft_iron_pickaxe",  "requires": {"iron_ingot": 3, "stick": 2},         "desc": "Craft an iron pickaxe"},
    {"goal": "mine_diamond",        "requires": {"iron_pickaxe": 1},                   "desc": "Find and mine diamonds"},
]

# Helpers shared by the skills below. Everything else they call (mineBlock,
# craftItem, placeItem, smeltItem, killMob, exploreUntil) is a Voyager
# control primitive that bot.js puts in scope from plugin/voyager.
_PRELUDE = """
async function ensureNear(blockName) {
  const id = mcData.blocksByName[blockName].id;
  if (bot.findBlock({ matching: id, maxDistance: 32 })) return;
  if (!bot.inventory.items().find(i => i.name === blockName)) {
    await craftItem(bot, blockName, 1);
  }
  await placeItem(bot, blockName, bot.entity.position.offset(1, 0, 0));
}
async function findOrExplore(blockName, direction) {
  const id = mcData.blocksByName[blockName].id;
  const look = () => bot.findBlock({ matching: id, maxDistance: 32 });
  if (look()) return;
  if (!(await exploreUntil(bot, direction, 60, look))) {
    throw new Error(`No ${blockName} found after exploring`);
  }
}
function countOf(name) {
  return bot.inventory.items().filter(i => i.name === name).reduce((s, i) => s + i.count, 0);
}
"""

BUILTIN_SKILLS: Dict[str, str] = {
    name: _PRELUDE + body for name, body in {
        "collect_wood":        'await mineBlock(bot, "oak_log", 5);',
        "craft_planks":        'await craftItem(bot, "oak_planks", 5);',
        "craft_workbench":     'await craftItem(bot, "crafting_table", 1);',
        "craft_wood_pickaxe":  'await ensureNear("crafting_table");\n'
                               'await craftItem(bot, "stick", 2);\n'
                               'await craftItem(bot, "wooden_pickaxe", 1);',
        "mine_stone":          'await mineBlock(bot, "stone", 11);',
        "craft_stone_pickaxe": 'await ensureNear("crafting_table");\n'
                               'await craftItem(bot, "stone_pickaxe", 1);',
        "craft_furnace":       'await ensureNear("crafting_table");\n'
                               'await craftItem(bot, "furnace", 1);',
        "mine_iron_ore":       'await findOrExplore("iron_ore", new Vec3(1, -1, 1));\n'
                               'await mineBlock(bot, "iron_ore", 3);',
        "smelt_iron":          'await ensureNear("furnace");\n'
                               'await smeltItem(bot, "raw_iron", "oak_planks", 3);',
        "craft_iron_pickaxe":  'await ensureNear("crafting_table");\n'
                               'await craftItem(bot, "iron_pickaxe", 1);',
        "mine_diamond":        'await findOrExplore("diamond_ore", new Vec3(1, -1, 0));\n'
                               'await mineBlock(bot, "diamond_ore", 1);',
    }.items()
}

# Plain bot.js actions to run when Voyager's primitives aren't loaded. Those
# actions fall back to raw mineflayer inside bot.js. Tiers that need
# exploring or smelting have no fallback.
Step = Tuple[str, Dict[str, Any]]
CURRICULUM_STEPS: Dict[str, List[Step]] = {
    "collect_wood":        [("mine_block", {"name": "oak_log", "count": 5})],
    "craft_planks":        [("craft_item", {"name": "oak_planks", "count": 5})],
    "craft_workbench":     [("craft_item", {"name": "crafting_table", "count": 1})],
    "craft_wood_pickaxe":  [("place_block", {"name": "crafting_table"}),
                            ("craft_item", {"name": "stick", "count": 2}),
                            ("craft_item", {"name": "wooden_pickaxe", "count": 1})],
    "mine_stone":          [("mine_block", {"name": "stone", "count": 11})],
    "craft_stone_pickaxe": [("craft_item", {"name": "stone_pickaxe", "count": 1})],
    "craft_furnace":       [("craft_item", {"name": "furnace", "count": 1})],
    "craft_iron_pickaxe":  [("craft_item", {"name": "iron_pickaxe", "count": 1})],
}

# ─── Free-play goals ──────────────────────────────────────────────────────────
# Repeatable goals picked when the agent isn't pushing the curriculum. Each
# has a `when(inv, status)` gate, Voyager `code` (a string or a function of
# the status), and optional plain-action `steps` for when Voyager is missing.

_DIRS = [(1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1), (1, -1), (-1, 1)]
_LOGS = ["oak_log", "birch_log", "spruce_log", "jungle_log", "acacia_log", "dark_oak_log", "mangrove_log"]
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


_CHOP_TREES = f"""
const ids = {json.dumps(_LOGS)}.map(n => mcData.blocksByName[n]).filter(Boolean).map(b => b.id);
const look = () => bot.findBlock({{ matching: ids, maxDistance: 32 }});
if (!look()) await exploreUntil(bot, new Vec3(1, 0, 1), 60, look);
const tree = look();
if (!tree) throw new Error("No trees anywhere around here");
await mineBlock(bot, tree.name, 4);
return `Chopped ${{tree.name}}`;
"""

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
const fuel = countOf("coal") > 0 ? "coal" : "oak_planks";
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


FREE_GOALS: Dict[str, Dict[str, Any]] = {
    "chop_trees":     {"desc": "Chop down some trees for wood",
                       "when": lambda inv, st: True,
                       "code": _CHOP_TREES,
                       "steps": [("mine_block", {"name": "oak_log", "count": 4})]},
    "hunt_food":      {"desc": "Hunt an animal for food",
                       "when": lambda inv, st: True,
                       "code": _HUNT_FOOD,
                       "steps": [("kill_mob", {"name": "cow"})]},
    "cook_meat":      {"desc": "Cook raw meat in a furnace",
                       "when": lambda inv, st: _has(inv, _MEAT) and _has(inv, ["coal", "oak_planks"]),
                       "code": _COOK_MEAT},
    "explore":        {"desc": "Wander off and explore the area",
                       "when": lambda inv, st: True,
                       "code": _explore_code,
                       "steps": _explore_steps},
    "mine_coal":      {"desc": "Find and mine coal for torches and fuel",
                       "when": lambda inv, st: _has(inv, _PICKAXES),
                       "code": _MINE_COAL},
    "build_dirt_hut": {"desc": "Build a dirt hut to hide in",
                       "when": lambda inv, st: True,
                       "code": _DIRT_HUT},
    "wait_out_night": {"desc": "Hide and wait for the sun to come up",
                       "when": lambda inv, st: not st.get("is_day", True),
                       "code": _WAIT_NIGHT},
}

PICKER_SYSTEM = (
    "You choose the next goal for a Minecraft survival player. Think like a "
    "streamer playing for fun: make progress, stay alive, and vary what you do. "
    "Prefer shelter at night, food when hunger is low, and tools before deep "
    "mining. Do not repeat a goal that just failed. Reply with JSON only: "
    '{"goal": "<one of the listed names>", "reason": "<one short sentence>"}'
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
        self._skills: Dict[str, Any] = self._load_skills()
        self._paused = False
        self._override_goal: Optional[str] = None
        self._loop_task: Optional[asyncio.Task] = None
        self._recent_goals: List[str] = []           # last few goals, newest last
        self._failures: Dict[str, int] = {}          # consecutive failures per goal
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
            f'Recent goals: {", ".join(self._recent_goals[-5:]) or "none"}',
            f'Inventory: {inv}',
            f'Skills verified: {t["skills_verified"]}/{len(CURRICULUM)}',
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
                self._remember_goal(goal_name)

                # 5. Verify and record
                if ok:
                    self._failures.pop(goal_name, None)
                    self._record("result", f"OK {goal_name}: {msg}")
                    self._think(f"Done: {desc.lower()}. What next?")
                    self._last_done = desc
                    if goal_name in BUILTIN_SKILLS:
                        self._save_skill(goal_name, BUILTIN_SKILLS[goal_name])
                        if goal_name == "mine_diamond":
                            await self._narrate("I just found and mined diamonds!", major=True)
                        else:
                            await self._narrate(f"I just finished a milestone: {desc.lower()}.")
                    self._tel["current_goal"] = None
                else:
                    n = self._failures[goal_name] = self._failures.get(goal_name, 0) + 1
                    self._record("error", f"FAIL {goal_name} (x{n}): {msg}")
                    self._think(f"That didn't work ({desc.lower()}): {msg[:120]}")
                    if n == 3:
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
        code = self._get_skill_code(goal, status)
        if not code:
            return False, f"No skill code for '{goal}'"
        self._record("skill", f"Executing: {goal}")
        result = await self._bot_action("execute_skill", {"code": code, "description": goal})
        msg = result.get("message", "")
        if result.get("success") or "primitives not loaded" not in msg:
            return bool(result.get("success")), msg

        # Voyager is missing: run the goal as plain bot.js actions instead.
        steps = _goal_steps(goal, status)
        if not steps:
            return False, f"{msg} (no mineflayer fallback for {goal})"
        self._record("skill", f"Voyager unavailable — running {goal} as {len(steps)} plain action(s)")
        out = []
        for name, args in steps:
            r = await self._bot_action(name, args)
            out.append(r.get("message", ""))
            if not r.get("success"):
                return False, " | ".join(out)
        return True, " | ".join(out)

    # ─── Goal choice ───────────────────────────────────────────────

    async def _pick_goal(self, status: Dict) -> Optional[Tuple[str, str, str]]:
        """Return (goal, source, reason), or None if nothing can run."""
        if self._override_goal:
            goal, self._override_goal = self._override_goal, None
            return goal, "Lumi", ""

        inv = _inv(status)
        tier = self._pick_tier(status.get("inventory", []))
        options: Dict[str, str] = {}
        if tier and self._failures.get(tier["goal"], 0) < 3:
            options[tier["goal"]] = f'{tier["desc"]} (next step toward better tools)'
        for name, g in FREE_GOALS.items():
            try:
                if g["when"](inv, status):
                    options[name] = g["desc"]
            except Exception:
                pass
        # Let a goal that keeps failing rest unless it's the only option.
        rested = {k: v for k, v in options.items() if self._failures.get(k, 0) < 3}
        options = rested or options
        if not options:
            return None

        if self._llm_ok:
            picked = await self._llm_pick(status, options)
            if picked:
                return picked[0], "llm", picked[1]
        return self._random_pick(status, options, tier), "random", ""

    def _random_pick(self, status: Dict, options: Dict[str, str], tier: Optional[Dict]) -> str:
        if not status.get("is_day", True):
            if "build_dirt_hut" not in self._recent_goals[-6:] and "build_dirt_hut" in options:
                return "build_dirt_hut"
            if "wait_out_night" in options:
                return "wait_out_night"
        if (status.get("food") or 20) < EAT_BELOW_FOOD and "hunt_food" in options:
            return "hunt_food"
        if tier and tier["goal"] in options and random.random() < 0.7:
            return tier["goal"]
        weights = {"chop_trees": 3, "explore": 2, "hunt_food": 1, "mine_coal": 2,
                   "cook_meat": 2, "build_dirt_hut": 0.3, "wait_out_night": 0}
        names = [n for n in options if n not in self._recent_goals[-2:]] or list(options)
        return random.choices(names, weights=[weights.get(n, 1) for n in names])[0]

    async def _llm_pick(self, status: Dict, options: Dict[str, str]) -> Optional[Tuple[str, str]]:
        inv = ", ".join(f'{i["name"]}×{i["count"]}' for i in status.get("inventory", [])[:20]) or "empty"
        threats = ", ".join(f'{t["name"]} at {t["distance"]}' for t in status.get("threats") or []) or "none"
        failing = ", ".join(f"{k} (failed {v}x)" for k, v in self._failures.items() if v) or "none"
        prompt = (
            f"Health {status.get('health')}/20, hunger {status.get('food')}/20, "
            f"{'daytime' if status.get('is_day', True) else 'NIGHT'}.\n"
            f"Inventory: {inv}\nHostiles nearby: {threats}\n"
            f"Recent goals (oldest first): {', '.join(self._recent_goals[-5:]) or 'none'}\n"
            f"Failing goals: {failing}\n\nGoals you can pick:\n"
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

    def _remember_goal(self, goal: str) -> None:
        self._recent_goals.append(goal)
        self._recent_goals = self._recent_goals[-10:]

    # ─── Curriculum helpers ────────────────────────────────────────

    def _pick_tier(self, inventory: List[Dict]) -> Optional[Dict]:
        inv = {i["name"]: i["count"] for i in inventory}

        def ready(tier: Dict) -> bool:
            return all(inv.get(k, 0) >= v for k, v in tier["requires"].items())

        for idx, tier in enumerate(CURRICULUM):
            if self._skills.get(tier["goal"], {}).get("verified"):
                continue
            if ready(tier):
                return tier
            # The frontier tier lacks materials: redo the closest earlier tier
            # that can run (e.g. chop more logs) instead of stalling.
            return next((t for t in reversed(CURRICULUM[:idx]) if ready(t)), None)
        return None

    def _get_skill_code(self, goal: str, status: Dict) -> Optional[str]:
        if goal in FREE_GOALS:
            code: Code = FREE_GOALS[goal]["code"]
            return _PRELUDE + (code(status) if callable(code) else code)
        return BUILTIN_SKILLS.get(goal) or self._skills.get(goal, {}).get("code")

    def _save_skill(self, goal: str, code: str) -> None:
        self._skills[goal] = {"code": code, "verified": True, "ts": time.time()}
        try:
            with open(SKILLS_PATH, "w", encoding="utf-8") as f:
                json.dump(self._skills, f, indent=2)
        except Exception as e:
            self._record("error", f"Failed to save skill: {e}")

    def _load_skills(self) -> Dict:
        try:
            if os.path.exists(SKILLS_PATH):
                with open(SKILLS_PATH, encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

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
        self._tel["skills_verified"] = sum(
            1 for s in self._skills.values() if s.get("verified")
        )
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

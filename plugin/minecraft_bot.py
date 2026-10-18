"""
Lumi × Minecraft — Autonomous Voyager-inspired agent.

Runs independently of Lumi's web chat / agent mode. It manages its own
skill loop (assess → pick goal → execute skill → verify → save) and
connects to Lumi's SDK server only to register status/control actions
and push telemetry context.

The mineflayer bot (sdk/examples/minecraft/bot.js) is the execution
layer. This script talks to it through the local control API on
127.0.0.1:5057 — start bot.js before running this script.

Usage:
    python plugin/minecraft_bot.py

Dependencies: websockets (already in Lumi's requirements).
No other extra packages — HTTP calls use urllib from the stdlib.

Data written at runtime:
    data/minecraft/telemetry.json  — live bot state (read by mc_dashboard.py)
    data/minecraft/events.jsonl    — agent event log (read by mc_dashboard.py)
    data/minecraft/skills.json     — verified skill library
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

# ─── Path bootstrap ───────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from sdk import LumiClient, action

# ─── Paths ────────────────────────────────────────────────────────────────────
DATA_ROOT      = os.path.join(_ROOT, "data")
MC_DIR         = os.path.join(DATA_ROOT, "minecraft")
SKILLS_PATH    = os.path.join(MC_DIR, "skills.json")
TELEMETRY_PATH = os.path.join(MC_DIR, "telemetry.json")
EVENTS_PATH    = os.path.join(MC_DIR, "events.jsonl")
TOKEN_PATH     = os.path.join(DATA_ROOT, "memories", "sdk_token.txt")

BOT_API        = os.environ.get("BOT_API", "http://127.0.0.1:5057")
LOOP_INTERVAL  = float(os.environ.get("MC_LOOP_INTERVAL", "4"))  # seconds between steps
# Must exceed bot.js SKILL_TIMEOUT_MS (120 s) so bot.js reports the timeout itself.
ACTION_TIMEOUT = float(os.environ.get("MC_ACTION_TIMEOUT", "135"))

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
# craftItem, placeItem, smeltItem, exploreUntil) is a Voyager control
# primitive that bot.js puts in scope from plugin/voyager.
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
        self._tel: Dict[str, Any] = {
            "bot_status": "unreachable",
            "lumi_sdk": "offline",
            "position": None,
            "health": None,
            "food": None,
            "inventory": [],
            "current_goal": None,
            "goal_description": None,
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
                   "Override the Minecraft agent's current curriculum goal.")
            .param("goal", "string",
                   description="Goal name: " + ", ".join(t["goal"] for t in CURRICULUM),
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
            "Minecraft autonomous agent is online. "
            "It runs a Voyager-inspired skill loop: "
            "assess inventory → pick curriculum goal → execute JS skill → verify → save. "
            "Use mc_agent_status for live progress.",
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
            f'Goal: {t["current_goal"] or "none"} — {t["goal_description"] or ""}',
            f'Inventory: {inv}',
            f'Skills verified: {t["skills_verified"]}/{len(CURRICULUM)}',
        ]
        return True, "\n".join(lines)

    async def _h_set_goal(self, args: Dict) -> tuple:
        name = str(args.get("goal") or "").strip()
        if not any(t["goal"] == name for t in CURRICULUM):
            valid = [t["goal"] for t in CURRICULUM]
            return False, f"Unknown goal '{name}'. Valid: {valid}"
        self._override_goal = name
        self._record("agent", f"Goal overridden to: {name}")
        return True, f"Goal set to: {name}"

    async def _h_pause(self, _args: Dict) -> tuple:
        self._paused = True
        self._tel["loop_state"] = "paused"
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

    # ─── Voyager skill loop ────────────────────────────────────────

    async def _skill_loop(self) -> None:
        self._tel["loop_state"] = "running"
        self._record("agent", "Skill loop started")

        while True:
            try:
                await asyncio.sleep(LOOP_INTERVAL)
                if self._paused:
                    continue

                # 1. Assess — get bot status
                status = await self._bot_get("/status")
                if not status:
                    self._tel["bot_status"] = "unreachable"
                    self._tel["loop_state"] = "waiting for bot"
                    self._flush_telemetry()
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

                if not status.get("ready"):
                    continue

                # 2. Pick goal
                if self._override_goal:
                    goal_name = self._override_goal
                    self._override_goal = None
                else:
                    tier = self._pick_tier(status.get("inventory", []))
                    if tier is None:
                        # All curriculum complete
                        self._tel["current_goal"] = None
                        self._tel["goal_description"] = "All curriculum goals complete!"
                        self._flush_telemetry()
                        await asyncio.sleep(30)
                        continue
                    goal_name = tier["goal"]

                tier_obj = next((t for t in CURRICULUM if t["goal"] == goal_name), None)
                if tier_obj:
                    self._tel["current_goal"]      = goal_name
                    self._tel["goal_description"]  = tier_obj["desc"]
                    self._record("curriculum", f"Goal: {tier_obj['desc']}")

                # 3. Get skill code
                code = self._get_skill_code(goal_name)
                if not code:
                    self._record("warn", f"No skill code for '{goal_name}' — skipping")
                    self._tel["current_goal"] = None
                    continue

                # 4. Execute
                self._record("skill", f"Executing: {goal_name}")
                result = await self._bot_action("execute_skill", {
                    "code": code,
                    "description": goal_name,
                })

                msg = result.get("message", "")
                if result.get("success"):
                    self._record("result", f"OK {goal_name}: {msg}")
                    self._save_skill(goal_name, code)
                    self._tel["current_goal"] = None
                    self._tel["skills_verified"] = sum(
                        1 for s in self._skills.values() if s.get("verified")
                    )
                    await self.send_context(
                        f"[Minecraft] Completed: {goal_name}. {msg}",
                        silent=True,
                    )
                else:
                    self._record("error", f"FAIL {goal_name}: {msg}")
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

    def _get_skill_code(self, goal: str) -> Optional[str]:
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

    def _flush_telemetry(self) -> None:
        self._tel["skills_verified"] = sum(
            1 for s in self._skills.values() if s.get("verified")
        )
        try:
            with open(TELEMETRY_PATH, "w", encoding="utf-8") as f:
                json.dump(self._tel, f, indent=2)
        except Exception:
            pass


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

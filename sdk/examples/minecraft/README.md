# Lumi × Minecraft SDK

A [mineflayer](https://github.com/PrismarineJS/mineflayer) bot that connects to
Lumi's SDK server and exposes Minecraft controls as Lumi actions. Lumi can chat
in-game, navigate, mine, craft, smelt, place blocks, fight mobs, and run
[Voyager](https://github.com/MineDojo/Voyager)-style JavaScript skills, all
through her normal agent tool-use path. The bot also serves a local control API
that the autonomous agent ([`plugin/minecraft_bot.py`](../../../plugin/README.md))
and the diagnostics page use.

```
Lumi agent ─ sdk__minecraft_mine_block ─► SDK server (:5056) ─┐
                                                              ▼
plugin/minecraft_bot.py ─ POST /action ─────────────────► bot.js ─► mineflayer ─► Minecraft
plugin/mc_dashboard.py  ─ GET /status, /events, /config ─►   │      (Voyager primitives
                                                       (:5057)       in skill scope)
```

---

## Requirements

- **Node.js 18+**
- **A Minecraft world to join**: a server, or a single-player world opened to LAN
- **The Voyager submodule** at `plugin/voyager` (see Setup)
- **Lumi running** (`python main.py`) only if you want Lumi to call the actions.
  The bot joins and plays without it.
- `MinecraftBot` approved in `data/memories/sdk_allowlist.json` (pre-filled by this integration)

---

## Setup

```powershell
# from the project root
git submodule update --init plugin/voyager   # Voyager's control primitives
cd sdk/examples/minecraft
npm install
cp .env.example .env        # then edit .env with your server details
node bot.js
```

Expected output:

```
[voyager] loaded 11 control primitives
[mc] connecting to localhost:25565 as LumiBot
[sdk] connecting to ws://127.0.0.1:5056/ws
[ctrl] control API on 127.0.0.1:5057
[sdk] welcome — session=... protocol=1.0
[sdk] registered: minecraft_get_status
...
[sdk] registered: minecraft_kill_mob
[mc] bot spawned and ready
```

For a LAN world, the port changes every launch. Set it from the dashboard's
**Minecraft Server** panel (`python plugin/mc_dashboard.py`, then
`http://127.0.0.1:5002/`) instead of editing `.env`.

---

## Configuration (`.env`)

| Variable | Default | Description |
|---|---|---|
| `LUMI_HOST` | `127.0.0.1` | Lumi SDK server host |
| `LUMI_PORT` | `5056` | Lumi SDK server port |
| `LUMI_TOKEN_PATH` | `../../../data/memories/sdk_token.txt` | Path to the SDK auth token |
| `LUMI_SDK_TOKEN` | — | Token value directly (overrides `LUMI_TOKEN_PATH`) |
| `MC_HOST` | `localhost` | Minecraft server hostname or IP |
| `MC_PORT` | `25565` | Minecraft server port |
| `MC_USERNAME` | `LumiBot` | Bot account username |
| `MC_AUTH` | `offline` | `offline` or `microsoft` |
| `MC_VERSION` | *(auto)* | Pin a Minecraft version string, e.g. `1.20.1` |
| `BOT_CONTROL_PORT` | `5057` | Port for the local control API |
| `SKILL_TIMEOUT_MS` | `120000` | Time limit for one skill run |
| `VOYAGER_DIR` | `../../../plugin/voyager/voyager` | Where to load Voyager's primitives from |
| `EVENT_REACTIONS` | `true` | Push game events (damage, mobs, deaths, rare loot) for Lumi to react to out loud. `false` keeps only the silent status snapshots |
| `AUTO_DEFEND` | `true` | Fight back when a hostile mob hits the bot. See [Self-defense](#self-defense) |

`MC_HOST` and `MC_PORT` can also be changed while the bot runs, through
`POST /config` or the dashboard. That is how LAN worlds are handled.

---

## Allowlist

The SDK server uses a deny-by-default allowlist at
`data/memories/sdk_allowlist.json`. The `MinecraftBot` entry is pre-configured
with the capability ceiling needed for every action:

```json
{
  "MinecraftBot": {
    "approved": true,
    "capability_ceiling": ["read", "write", "dangerous"],
    "auto_approve_writes": false
  }
}
```

Set `auto_approve_writes: true` to skip the terminal `y/n` prompt for
`write`-tier actions (chat, move, look, attack, stop). The `dangerous`-tier
`minecraft_execute_skill` and `requires_confirm` `minecraft_dig` always prompt
regardless of this flag.

---

## Actions reference

All actions are registered as `sdk__<name>` in Lumi's tool list. The `sdk__`
prefix is added by the SDK server; you never type it directly.

### `minecraft_get_status`

**Tier:** `read` — runs without a prompt.

Returns the bot's position, time of day, health, hunger, held item, hostile
mobs within 16 blocks, and inventory as a formatted string. Also used as the
context snapshot pushed every 30 seconds.

**Parameters:** none

**Returns:**
```
[MinecraftBot] LumiBot on localhost:25565
Position: 128.0, 64.0, -32.0  Time: night
Health: 14/20  Hunger: 17/20  Holding: stone_sword
Nearby threats: zombie (4.2 blocks), skeleton (11.8 blocks)
Inventory: dirt×3, stone_sword×1
```

---

### `minecraft_chat`

**Tier:** `write`

Sends a chat message to the Minecraft server as the bot.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `message` | string | yes | The text to send in chat |

**Returns:** `Sent: <message>`

---

### `minecraft_move_to`

**Tier:** `write`

Navigates the bot to the given coordinates using `mineflayer-pathfinder`. The
bot pathfinds around obstacles. Returns when it arrives or if pathfinding fails.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `x` | number | yes | Target X coordinate |
| `y` | number | yes | Target Y coordinate |
| `z` | number | yes | Target Z coordinate |
| `range` | number | no | Stop within this many blocks (default `1`) |

**Returns:** `Reached (x, y, z)` or a pathfinding error message.

---

### `minecraft_look_at`

**Tier:** `write`

Rotates the bot's head to face the given block coordinates instantly.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `x` | number | yes | Target X coordinate |
| `y` | number | yes | Target Y coordinate |
| `z` | number | yes | Target Z coordinate |

**Returns:** `Looking at (x, y, z)`

---

### `minecraft_attack_nearest`

**Tier:** `write`

Finds and attacks the nearest mob. Without a filter it targets the nearest
hostile mob (zombie, skeleton, creeper, spider, enderman, witch, slime,
cave_spider, blaze, ghast, zombie_pigman, wither_skeleton, phantom, drowned,
husk, stray, pillager, vindicator, ravager, hoglin, zoglin, piglin_brute).

| Parameter | Type | Required | Description |
|---|---|---|---|
| `mob_type` | string | no | Filter by mob type name, e.g. `zombie`. Attacks nearest hostile if omitted. |

**Returns:** `Attacking zombie at distance 3.2` or a "not found" message.

---

### `minecraft_stop`

**Tier:** `write`

Clears the pathfinder goal, stopping any active movement immediately.

**Parameters:** none

**Returns:** `Stopped movement`

---

### `minecraft_dig`

**Tier:** `write` + `requires_confirm: true`

Breaks the block at integer x,y,z coordinates. Prompts for confirmation in
Lumi's terminal before executing.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `x` | integer | yes | Block X coordinate |
| `y` | integer | yes | Block Y coordinate |
| `z` | integer | yes | Block Z coordinate |

**Returns:** `Dug dirt at (x, y, z)` or an error if the target is air or
digging fails.

---

### `minecraft_execute_skill`

**Tier:** `dangerous` + `requires_confirm: true`

Runs async JavaScript the same way Voyager's own environment does: Voyager's
control primitives and your code are joined into one async function body.
Always prompts for confirmation in Lumi's terminal.

In scope:

| Name | What it is |
|---|---|
| `bot` | The live mineflayer bot, with Voyager's `inject()` applied (`bot.save`, `bot.inventoryUsed`) |
| `mcData` | `minecraft-data` for the server's version |
| `Vec3`, `Movements`, `goals`, `GoalNear`, `GoalBlock`, `GoalFollow`, … | `vec3` and `mineflayer-pathfinder` |
| `mineBlock`, `craftItem`, `smeltItem`, `placeItem`, `killMob`, `exploreUntil`, chest helpers | Voyager control primitives |

The skill times out after 120 s (`SKILL_TIMEOUT_MS`). The result Lumi sees is
the return value plus the last lines the bot said in chat, because the
primitives report progress and failures through `bot.chat` (for example
`No oak_log nearby, please explore first`).

When Lumi calls an action, the SDK server waits 30 s for the result, so long
skills report a timeout to Lumi while the bot keeps working. The autonomous
agent (`plugin/minecraft_bot.py`) waits the full 120 s.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `code` | string | yes | Async JavaScript code |
| `description` | string | no | Human-readable label (logged, not executed) |

#### Examples

**Wooden pickaxe from scratch:**
```js
await mineBlock(bot, "oak_log", 3);
await craftItem(bot, "oak_planks", 3);
await craftItem(bot, "crafting_table", 1);
await placeItem(bot, "crafting_table", bot.entity.position.offset(1, 0, 0));
await craftItem(bot, "stick", 1);
await craftItem(bot, "wooden_pickaxe", 1);
```

**Explore until iron shows up, then mine it:**
```js
const look = () => bot.findBlock({ matching: mcData.blocksByName.iron_ore.id, maxDistance: 32 });
await exploreUntil(bot, new Vec3(1, -1, 1), 60, look);
await mineBlock(bot, "iron_ore", 3);
```

**Follow the nearest player:**
```js
const player = bot.nearestEntity(e => e.type === "player");
if (!player) return "No player nearby";
bot.pathfinder.setGoal(new GoalFollow(player, 2), true);
return `Following ${player.username}`;
```

---

### Voyager primitive actions

**Tier:** `write`. Lumi asks `y/n` in its terminal unless the client's
`auto_approve_writes` is `true` in the allowlist.

Single-call wrappers around the primitives, so Lumi doesn't have to write
JavaScript for common tasks. Names must be Minecraft ids (`[a-z0-9_]`).
Counts are clamped to 1–64.

| Action | Parameters | Primitive |
|---|---|---|
| `minecraft_mine_block` | `name`, `count` | `mineBlock(bot, name, count)` |
| `minecraft_craft_item` | `name`, `count` | `craftItem(bot, name, count)`. 3×3 recipes need a crafting table within 32 blocks |
| `minecraft_smelt_item` | `name`, `fuel` (default `coal`), `count` | `smeltItem(bot, name, fuel, count)`. Needs a furnace within 32 blocks |
| `minecraft_place_item` | `name`, optional `x`,`y`,`z` | `placeItem(bot, name, pos)`. Defaults to the block beside the bot |
| `minecraft_kill_mob` | `name` | `killMob(bot, name, 60)` |

If the primitives aren't loaded, or a primitive throws (for any reason except
the 120 s timeout), `mine_block`, `craft_item`, `place_item` and `kill_mob`
retry with plain mineflayer: `collectBlock` for mining, `bot.recipesFor` +
`bot.craft` for crafting, `bot.placeBlock` for placing, and `mineflayer-pvp`
for fighting. `smelt_item` and `minecraft_execute_skill` have no fallback.

---

### Survival actions

**Tier:** `write`. Plain mineflayer, so they work without Voyager.

| Action | Parameters | What it does |
|---|---|---|
| `minecraft_defend` | — | Fights hostile mobs within 8 blocks one at a time until none are left (30 s limit). Equips the best sword or axe first |
| `minecraft_attack_entity` | optional `name` | Chases and fights one mob until it dies (30 s limit). Nearest hostile unless a mob id is given. Unlike `minecraft_attack_nearest`, which swings once |
| `minecraft_eat_food` | optional `name` | Eats the most filling food in the inventory. Skips rotten flesh, spider eyes, raw chicken and other risky food unless named |
| `minecraft_place_block` | `name`, optional `x`,`y`,`z` | Places a block against a solid neighbour of the target cell, beside the bot by default. Walks closer if the target is out of reach |
| `minecraft_drop_item` | `name`, optional `count` | Tosses items on the ground. All of that item unless `count` is given |

---

## Context and events

The bot pushes a status snapshot (`silent: true`) that Lumi sees in her prompt
between `[FROM SDK]` markers:

- On successful connect
- After every mutating action (chat, move, dig, etc.)
- Every 30 seconds while the bot is alive

This means Lumi always has current position, health, hunger, threats, and
inventory when she decides to call a tool.

It also pushes **events** (`silent: false`), each starting with
`[MINECRAFT EVENT]`. An event starts a turn, so Lumi reacts out loud in
character ("Ow! Who put a zombie here?"). This happens in both chat and agent
mode, and event turns never call tools.

| Event | When | Bot cooldown |
|---|---|---|
| Joined the world | First spawn on a server | — |
| Took damage | Health drops. Names the nearest hostile and warns at 6 HP or less | 20 s |
| Hostile nearby | A hostile mob comes within 10 blocks for the first time. Creepers get an extra warning | 25 s |
| Getting hungry | Hunger falls to 6 or below | 2 min |
| Killed a mob | A mob the bot was fighting dies | 10 s |
| Died | Death, with the mob that last hurt it if that was within 10 s | — |
| Rare pickup | Diamonds, emeralds, gold, ender pearls, golden apples and similar | 15 s per item |

Lumi's SDK server applies one more limit on top: it ignores events while Lumi
is thinking or speaking, and allows at most one event turn every 12 s per
client. Events it drops are logged, not queued. Set `EVENT_REACTIONS=false` to
turn events off.

## Self-defense

Once a second the bot scans for hostile mobs:

- **Idle** (no action running): it looks at any hostile within 5 blocks, and
  fights it with `mineflayer-pvp` if it was hit in the last 5 s. It doesn't
  look at endermen, because that would provoke them.
- **Hit while busy** (a skill or action is running): it fights back only once
  health is 12 or lower. The fight takes over the pathfinder, so the running
  skill usually fails and the agent retries it later.

Set `AUTO_DEFEND=false` to turn this off. Lumi or the agent can still call
`minecraft_defend` explicitly.

---

## Control API

`bot.js` serves plain HTTP on `127.0.0.1:BOT_CONTROL_PORT` (default `5057`).
It doesn't go through Lumi, so it works whether or not Lumi is running.

| Method | Path | Body / response |
|---|---|---|
| `GET` | `/status` | `{ready, username, server, position, health, food, inventory, threats, is_day, busy, defending}` |
| `GET` | `/events` | Last 100 entries of the bot's event log |
| `GET` | `/config` | `{host, port, username, auth}` |
| `POST` | `/config` | `{host, port}`. Updates the target server and reconnects if it changed |
| `POST` | `/action` | `{action, args}`, where `action` is an action name without the `minecraft_` prefix (e.g. `mine_block`). Returns `{success, message}` |

The API has no authentication and allows any origin (CORS `*`). See the
warning in [docs/security.md](../../../docs/security.md#minecraft-bot-control-api)
before running the bot on a machine where you browse untrusted sites.

---

## Voyager integration notes

[Voyager](https://github.com/MineDojo/Voyager) is a git submodule at
`plugin/voyager`. Its files are used in place, not copied:

| Voyager piece | Used? | How |
|---|---|---|
| `voyager/control_primitives/*.js` | Yes | Read by `bot.js` at startup and put in scope for every skill |
| `env/mineflayer/lib/skillLoader.js` (`inject`) | Yes | Applied to each new bot |
| Plugins: pathfinder, tool, collectblock, pvp | Yes | Installed from npm in this folder |
| `minecrafthawkeye` (bow aiming) | No | Only `shoot()` and `killMob` while holding a bow need it |
| `env/mineflayer/index.js` (Voyager's Node server) | No | `bot.js` replaces it, so only one bot joins the world |
| Python LLM agents (curriculum, action, critic) | No | They require the OpenAI API. `plugin/minecraft_bot.py` uses the same primitives with a fixed curriculum plus free-play goals, and asks Lumi's own model which goal to run |

The curriculum in `plugin/minecraft_bot.py` goes logs → planks →
crafting table → wooden pickaxe → stone → stone pickaxe → furnace → iron →
smelting → iron pickaxe → diamonds. Between tiers the agent also picks
free-play goals (chop trees, hunt, explore, build a dirt hut at night). See
[the agent's README](../../../plugin/README.md#minecraft-agent-minecraft_botpy).

---

## Troubleshooting

**Bot connects to Lumi but actions never fire.**
Agent mode must be ON (`!agent_on` in the terminal or the Agent chip in the web
chat). Also check the action names against the
[design notes in sdk/README.md](../../README.md#design-notes-for-plugin-authors):
names that overlap with built-in tools are skipped.

**`client 'MinecraftBot' is not approved` error.**
Check `data/memories/sdk_allowlist.json`. The entry must have `"approved": true`.
Restart `bot.js` after editing the file (the allowlist is read on connect).

**`Cannot find module 'ws'` (or another package).**
Run `npm install` in `sdk/examples/minecraft`.

**`[voyager] primitives not loaded`.**
The submodule is missing. Run `git submodule update --init plugin/voyager` from
the workspace root, or set `VOYAGER_DIR`.

**`[sdk] Lumi is not running (127.0.0.1:5056)`.**
Expected when `main.py` isn't running. The bot still joins and plays, and
attaches to Lumi when it starts.

**`ECONNREFUSED ...:25565` at startup.**
Nothing is listening on the configured Minecraft port. For a LAN world, set the
port shown by *Open to LAN* in the dashboard's Minecraft Server panel.

**Dashboard says "Cannot reach bot.js control API (:5057)".**
`bot.js` isn't running, or is an old process from before the control API
existed. Restart it and look for `[ctrl] control API on 127.0.0.1:5057`.

**Pathfinding never completes.**
The target may be surrounded by solid blocks or over water. Supply a `range`
parameter (e.g. `range: 3`) to let the bot stop short, or check that
`mineflayer-pathfinder` is installed (`npm install`).

**A skill fails with `No oak_log nearby, please explore first` (or similar).**
That message comes from a Voyager primitive through the bot's chat. Move the bot
closer to the resource, or add an `exploreUntil(...)` call before `mineBlock`.

**`minecraft_execute_skill` times out.**
`bot.js` stops a skill after `SKILL_TIMEOUT_MS` (120 s). When Lumi calls the
action, the SDK server gives up after 30 s and reports a timeout while the bot
keeps working. Split long jobs into several calls.

**Bot logs `[mc] disconnected` and reconnects every 10 seconds.**
The Minecraft server is refusing the connection. Check `MC_HOST`, `MC_PORT`,
`MC_AUTH`, and `MC_VERSION`. For Microsoft-authenticated servers set
`MC_AUTH=microsoft` and ensure the bot account owns the game.

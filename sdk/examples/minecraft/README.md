# Lumi × Minecraft SDK

A [mineflayer](https://github.com/PrismarineJS/mineflayer) bot that connects to
Lumi's SDK server and exposes Minecraft controls as Lumi actions. Lumi can chat
in-game, navigate, attack mobs, dig blocks, and execute
[Voyager](https://github.com/MineDojo/Voyager)-compatible async JavaScript
skills — all through her normal agent tool-use path.

```
Lumi agent
  └─ sdk__minecraft_move_to({x, y, z})
       └─ SDKServer (ws://127.0.0.1:5056)
            └─ bot.js
                 └─ mineflayer bot → Minecraft server
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

---

## Allowlist

The SDK server uses a deny-by-default allowlist at
`data/memories/sdk_allowlist.json`. The `MinecraftBot` entry is pre-configured
with the capability ceiling needed for all eight actions:

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

Returns the bot's current position, health, food level, and inventory as a
formatted string. Also used as the context snapshot pushed every 30 seconds.

**Parameters:** none

**Returns:**
```
[MinecraftBot] LumiBot on localhost:25565
Position: 128.0, 64.0, -32.0
Health: 20.0/20  Food: 20/20
Inventory: dirt×3, wood_sword×1
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

---

## Context

The bot pushes a status snapshot as `[EXTERNAL CONTEXT]` in Lumi's prompt:

- On successful connect
- After every mutating action (chat, move, dig, etc.)
- Every 30 seconds while the bot is alive

This means Lumi always has current position, health, food, and inventory when
she decides to call a tool.

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
| Python LLM agents (curriculum, action, critic) | No | They require the OpenAI API. `plugin/minecraft_bot.py` runs a fixed curriculum built on the same primitives instead |

The fixed curriculum in `plugin/minecraft_bot.py` goes logs → planks →
crafting table → wooden pickaxe → stone → stone pickaxe → furnace → iron →
smelting → iron pickaxe → diamonds. If a tier is missing materials, the agent
re-runs the closest earlier tier, for example chopping more logs, rather than
stalling.

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

**Pathfinding never completes.**
The target may be surrounded by solid blocks or over water. Supply a `range`
parameter (e.g. `range: 3`) to let the bot stop short, or check that
`mineflayer-pathfinder` is installed (`npm install`).

**`minecraft_execute_skill` times out.**
Skills that `await` forever (e.g. `GoalFollow` with no exit condition) hit the
30-second limit. Structure long-running skills with a loop limit or a
`Promise.race` timeout inside the code itself.

**Bot logs `[mc] disconnected` and reconnects every 10 seconds.**
The Minecraft server is refusing the connection. Check `MC_HOST`, `MC_PORT`,
`MC_AUTH`, and `MC_VERSION`. For Microsoft-authenticated servers set
`MC_AUTH=microsoft` and ensure the bot account owns the game.

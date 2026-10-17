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
- **A running Minecraft server** (local Paper/Vanilla or a public server)
- **Lumi running** with agent mode on — the SDK server binds on `127.0.0.1:5056`
- `MinecraftBot` approved in `data/memories/sdk_allowlist.json` (pre-filled by this integration)

---

## Setup

```powershell
# from the project root
cd sdk/examples/minecraft
npm install
cp .env.example .env        # then edit .env with your server details
node bot.js
```

Expected output:

```
[mc]  connecting to localhost:25565 as LumiBot
[sdk] connecting to ws://127.0.0.1:5056/ws
[sdk] welcome — session=... protocol=1.0
[sdk] registered: minecraft_get_status
[sdk] registered: minecraft_chat
[sdk] registered: minecraft_move_to
[sdk] registered: minecraft_look_at
[sdk] registered: minecraft_attack_nearest
[sdk] registered: minecraft_stop
[sdk] registered: minecraft_dig
[sdk] registered: minecraft_execute_skill
[mc]  bot spawned and ready
```

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

Executes a [Voyager](https://github.com/MineDojo/Voyager)-compatible async
JavaScript skill against the live mineflayer bot. Always prompts for
confirmation in Lumi's terminal.

The code runs as the body of:

```js
async function(bot, goals, Movements, Vec3) {
  // your code here
}
```

Four names are injected into scope:

| Name | What it is |
|---|---|
| `bot` | The live mineflayer bot instance |
| `goals` | `mineflayer-pathfinder` goals (GoalNear, GoalBlock, GoalFollow, …) |
| `Movements` | `mineflayer-pathfinder` Movements class |
| `Vec3` | `vec3` Vec3 constructor |

The skill has a 30-second timeout. Its return value (coerced to string) is what
Lumi sees as the tool result.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `code` | string | yes | Async JavaScript code |
| `description` | string | no | Human-readable label (logged, not executed) |

**Returns:** The skill's return value, or an error message on timeout or throw.

#### Voyager skill examples

**Collect wood:**
```js
const woodBlock = bot.findBlock({
  matching: (b) => b.name.includes("log"),
  maxDistance: 32,
});
if (!woodBlock) return "No wood found nearby";
await bot.pathfinder.goto(new goals.GoalNear(
  woodBlock.position.x, woodBlock.position.y, woodBlock.position.z, 1
));
await bot.dig(woodBlock);
return `Collected ${woodBlock.name}`;
```

**Follow nearest player:**
```js
const player = Object.values(bot.entities).find(
  e => e.type === "player" && e !== bot.entity
);
if (!player) return "No player nearby";
await bot.pathfinder.goto(new goals.GoalFollow(player, 2));
return `Following ${player.username}`;
```

**Craft a crafting table:**
```js
const plankTypes = ["oak_planks","spruce_planks","birch_planks","jungle_planks"];
const plank = plankTypes.find(n => bot.inventory.items().some(i => i.name === n));
if (!plank) return "No planks in inventory";
const craftingTable = bot.registry.itemsByName["crafting_table"];
await bot.craft(bot.registry.craftingRecipe(craftingTable.id), 1, null);
return "Crafted crafting table";
```

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

[Voyager](https://github.com/MineDojo/Voyager) is an LLM-powered agent that
generates and accumulates JavaScript skills for Minecraft. This bridge is
compatible with Voyager's skill format at the `minecraft_execute_skill` level:
Voyager writes async skill bodies that receive a `bot` argument, and
`minecraft_execute_skill` accepts exactly that shape.

To use Voyager's full curriculum and skill library alongside Lumi:

1. Run Voyager's Python process separately (it manages its own mineflayer
   connection and skill database).
2. Use `minecraft_execute_skill` to feed skills from Voyager's library directly
   to Lumi — paste the skill code as the `code` parameter.
3. Or implement a second SDK client in Python that wraps
   `voyager.env.step(skill_code)` and registers a single
   `voyager_run_skill` action for higher-level orchestration.

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

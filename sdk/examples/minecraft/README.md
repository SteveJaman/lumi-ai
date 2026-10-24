# Lumi × Minecraft SDK

The body of Lumi's Minecraft "other half": a
[mineflayer](https://github.com/PrismarineJS/mineflayer) bot that joins your
world as `LumiBot`. It walks, mines, crafts, smelts, builds, branch-mines,
fights, defends itself, hands you items, and runs
[Voyager](https://github.com/MineDojo/Voyager)-style JavaScript skills. It
connects to Lumi's SDK server, so Lumi can call its actions as tools, and it
pushes status and events she reacts to. A local control API serves the game
agent ([`plugin/minecraft_bot.py`](../../../plugin/README.md)), the order
inbox, and the diagnostics page.

**New here?** Start with [docs/minecraft.md](../../../docs/minecraft.md),
which explains the whole feature. This file is the reference for `bot.js`.
Normally Lumi starts `bot.js` for you from the Games page; the steps below
also work by hand.

```
Lumi agent ─ sdk__minecraft_* ─► SDK server (:5056) ─────┐
                                                         ▼
plugin/minecraft_bot.py ─ /status /action /directive ─► bot.js ─► mineflayer ─► Minecraft
plugin/mc_dashboard.py  ─ /status /events /config ────►   │  (:5057)
owner's chat "Lumi, …" ─────────────────────────────────► │  order inbox
                                                          ├─► live viewer :3000
                                                          └─► skin server :3001
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
npm start                   # node --max-old-space-size=4096 bot.js
```

Expected output:

```
[voyager] loaded 11 control primitives
[mc] connecting to localhost:25565 as LumiBot
[sdk] connecting to ws://127.0.0.1:5056/ws
[ctrl] control API on 127.0.0.1:5057
[skin] serving lumi_skin.png on http://127.0.0.1:3001/skins/LumiBot.png
[sdk] welcome — session=... protocol=1.0
[sdk] registered: minecraft_get_status
...
[sdk] registered: minecraft_kill_mob
[mc] bot spawned and ready
[viewer] live view on http://localhost:3000
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
| `SKILL_TIMEOUT_MS` | `120000` | Time limit for one skill run (the most a caller can ask for) |
| `PATH_THINK_MS` | `10000` | How long the pathfinder may plan one path. Mineflayer's default of 5000 caused "Took to long to decide path to goal!" in forests |
| `VOYAGER_DIR` | `../../../plugin/voyager/voyager` | Where to load Voyager's primitives from |
| `EVENT_REACTIONS` | `major` | Which game events Lumi reacts to out loud: `major` (deaths, boss kills, top-tier loot), `all` (also damage, mobs, hunger, kills, joining), or `off`. See [Context and events](#context-and-events) |
| `AUTO_DEFEND` | `true` | Fight back when a hostile mob hits the bot. See [Self-defense](#self-defense) |
| `MC_OWNER` | *(first other player online)* | The in-game name of the player Lumi plays near. See [Playing near you](#playing-near-you) |
| `HOUSE_PATH` | `../../../data/minecraft/house.json` | Where the house site and its progress are remembered |
| `VIEWER_PORT` | `3000` | Port for the first-person [live viewer](#live-viewer). `0` turns it off |
| `VIEWER_DISTANCE` | `4` | Chunks the live viewer sends around the bot, to each browser watching. Its main memory cost |
| `VIEWER_HEAP_LIMIT_MB` | `1536` | Pause the live viewer when the heap passes this. See [Memory](#memory) |
| `SKIN_PORT` | `3001` | Port for the [skin server](#lumis-skin). `0` turns it off |
| `SKIN_PATH` | `lumi_skin.png` next to `bot.js` | The skin PNG the skin server sends |

### Ports

| Port | Used by | Bound to |
|---|---|---|
| `3000` | Live viewer (`VIEWER_PORT`) | All interfaces. prismarine-viewer can't be told otherwise, so Windows may show a firewall prompt; allow it on private networks only, or deny it, since the dashboard on this PC still works |
| `3001` | Skin server (`SKIN_PORT`) | `127.0.0.1` |
| `5000` | Lumi's web UI | — |
| `5002` | Minecraft dashboard (`plugin/mc_dashboard.py`) | `127.0.0.1` |
| `5056` | Lumi SDK server | `127.0.0.1` |
| `5057` | `bot.js` control API (`BOT_CONTROL_PORT`) | `127.0.0.1` |

`MC_HOST` and `MC_PORT` can also be changed while the bot runs, through
`POST /config` or the dashboard. That is how LAN worlds are handled.

---

## Lumi's skin

The skin is `lumi_skin.png` in this folder: a standard 64×64 skin with classic
(4 px) arms, built from the reference sheet in
`assets/branding/lumi_png_samples/`. Edit it in any skin editor.

**Why the game doesn't show it automatically.** A vanilla LAN world runs in
offline mode. An offline-mode client never sends skin data to the server: the
login packet has no field for it. So the server tells every player that
`LumiBot` has the default skin. Minecraft also rejects skin URLs that aren't
on Mojang's domains. A fake profile from `bot.js` can't get around either
limit.

**What works:** the [CustomSkinLoader](https://modrinth.com/mod/customskinloader)
mod, installed in *your* Minecraft client (Fabric 1.19 build, alongside Fabric
API). It replaces skins on your screen, which is what viewers see when you
stream. Install it, launch the game once so it creates its folders, then do
one of the following:

- **Copy the file** (simplest). Copy `lumi_skin.png` to
  `%APPDATA%\.minecraft\CustomSkinLoader\LocalSkin\skins\LumiBot.png`. Use
  your `MC_USERNAME` as the file name if you changed it.
- **Load it from `bot.js`.** In `%APPDATA%\.minecraft\CustomSkinLoader\CustomSkinLoader.json`,
  add this entry to the top of `loadlist`. The skin then follows whatever is
  in `lumi_skin.png` (or `SKIN_PATH`) while `bot.js` runs:

  ```json
  {"name": "LumiBot", "type": "Legacy", "checkPNG": false,
   "skin": "http://127.0.0.1:3001/skins/{USERNAME}.png",
   "model": "default", "cape": "", "elytra": ""}
  ```

**Changing the skin.** Upload a new one with **Change skin…** on the Games
page in Lumi's web UI. Lumi checks that it's a real PNG of 64x64 (or 64x32)
pixels, at most 64 KB, writes it to `SKIN_PATH` (the previous one becomes
`lumi_skin.bak.png`), and the skin server serves it right away. With the
copy-the-file option above, copy it again.

The skin server only answers `/lumi_skin.png` and `/skins/<MC_USERNAME>.png`.
Other players on the LAN still see the default skin unless they install the
mod and the file too.

---

## Live viewer

When the bot spawns, `bot.js` starts
[prismarine-viewer](https://github.com/PrismarineJS/prismarine-viewer) in
first-person mode: a 3D view of what the bot sees, at
`http://localhost:3000`. The dashboard (`plugin/mc_dashboard.py`) embeds it in
its **Live View** panel.

The viewer is optional. If the package doesn't load (for example, `canvas`,
an optional native dependency, failed to install) or port 3000 is taken,
`bot.js` logs a warning and the bot plays normally without it. It stops on
disconnect and starts again after the bot reconnects and spawns.

### Memory

One run ended after 14 minutes with `JavaScript heap out of memory` at 4 GB.
The likely cause is the viewer: it serializes every chunk around the bot for
**each** connected browser, and socket.io keeps whatever a slow or background
tab hasn't read yet in memory, with no limit. Two viewers open (the Games page
and the old diagnostics page, say) double it.

- Every 15 s a watchdog checks the heap. Above `VIEWER_HEAP_LIMIT_MB` it
  pauses the viewer, which disconnects its browsers and frees their buffers,
  for 10 minutes. It restarts the viewer once the heap is below half the
  limit.
- `[mem] heap … MB, live viewer on/off` is logged every 5 minutes, and
  `/status` has `heap_mb`. If the heap keeps climbing with the viewer off,
  the leak is somewhere else; that line is what to look at.
- Lumi starts `bot.js` with `--max-old-space-size=4096`, and so does
  `npm start`. Node's default heap ceiling depends on the machine's RAM.
- `VIEWER_DISTANCE` defaults to 4 chunks, down from 6: about half the data
  per browser.

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
| `timeout_ms` | integer | no | Time limit, 5000–120000 ms. Default `SKILL_TIMEOUT_MS` |

When a skill fails or times out, `bot.js` stops everything it may have
started (pathfinding, block collection, digging, fighting, movement keys).
The skill's own code can't be cancelled, so without this a timed-out skill
would keep fighting the next one for the pathfinder.

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

### Companion actions

**Tier:** `write`. Plain mineflayer, so they work without Voyager. Each one
stops itself after about 105 s and reports its progress, so a call never
outlasts the agent's 135 s wait. Building and branch mining are resumable:
call again to continue.

| Action | Parameters | What it does |
|---|---|---|
| `minecraft_give_item` | `item_name`, optional `count` (default 1), `username` (default the owner) | Walks within about 2 blocks of the player (30 s limit), looks at them and tosses the items their way. `item_name` can be loose: `pickaxe` gives the best pickaxe, `diamonds` the diamonds, `stone pickaxe` that one |
| `minecraft_return_to_player` | optional `range` (default 4) | Follows the owner until within `range` blocks. The pathfinder digs through whatever is in the way. With no progress for 30 s, or the owner out of tracking range, it tries `/tp` (works only with cheats on in the LAN world), then gives up |
| `minecraft_mine_branch_tunnel` | optional `depth` (default 12), `length` (default 20, max 64) | Digs a 1-wide staircase down to `depth`, then a 1x2 tunnel. Mines any ore within 2 to 3 blocks it can harvest with the pickaxe it has (it never breaks diamond with a stone pickaxe), places a torch every 5 blocks if it has torches, fills holes in the floor, and stops at lava or water, at bedrock, or when attacked. Keeps the same direction between calls. In 1.18+ diamonds are most common around y=-54 |
| `minecraft_build_house` | optional `material`: `cobblestone` or `planks` | Builds the [7x7 house](#the-house) next to the bot, or continues the unfinished one. Fails with `Missing materials: cobblestone×65 (have 100, need 165 of 165)` when short |
| `minecraft_craft_chest` | optional `count` (1–8) | Crafts chests from planks or logs, placing a crafting table if needed |
| `minecraft_place_chest` | optional `x`,`y`,`z` | Places a chest from the inventory, next to the bot by default |
| `minecraft_deposit_items` | `item_name`, optional `count`, `x`,`y`,`z` | Puts items into the chest at `x`,`y`,`z`, or the nearest one within 16 blocks |
| `minecraft_sort_inventory` | optional `place_if_missing` | Puts excess bulk items into the nearest chest (crafting and placing one first with `place_if_missing`). Keeps tools, weapons, armor, food, torches and anything not listed, plus a working stock of 32 cobblestone, 32 dirt, 16 logs and 32 planks. Everything else on the bulk list (other stone, gravel, sand, saplings, mob drops like rotten flesh) goes in the chest |

#### The house

`house_blueprint.js` describes it as an ordered list of cells. The floor's
north-west corner goes 2 blocks east and 3 north of where the bot stands:

| Layer | Contents |
|---|---|
| Floor | 7x7 of the material, replacing the ground |
| Walls, 3 high | The 7x7 perimeter, with a 1x2 door gap in the middle of the front (north) wall and 1x1 windows in the middle of the other three walls at eye level |
| Roof | Flat, 7x7, placed from the edge inward |
| Extras | A door (crafted from spare planks if there are 6), glass in the windows if the bot has glass, two torches inside and one outside the door if it has torches |

Inside the walls is dug out first, so the house can sit against a slope.
That takes 165 blocks of material. While building, the pathfinder may not
dig, so it can't tunnel through walls it just placed. Cells it can't place
yet (a mob in the way, the bot standing there) are retried on the next call.
The site and whether it's finished are kept in `HOUSE_PATH`. A new site is
picked only when there's no house yet, the unfinished one is more than 64
blocks away, or the finished one is far away and `new_site` is passed.

---

## Playing near you

Lumi treats one player as hers, the **owner**: `MC_OWNER`, or the first other
player online. `GET /status` reports `owner: {name, distance, position,
called}`. `distance` is `null` when the owner is too far away for the server
to send their position.

- **Checking in.** When idle and no hostile is close, the bot glances at the
  owner when they move (within 16 blocks), and at least every 8 s.
- **Calling her.** A short chat message from the owner like "come here"
  or "follow me" sets `called` for 60 s, and the game agent brings her back.
  Longer messages count only if they mention Lumi, so "I'll come back later"
  doesn't summon her.
- **Giving orders.** A message from the owner that starts with a wake word
  (`Lumi,`, `Lumi:`, `!lumi`, or `Lumi ` and more words) is an order, not
  chat: it goes to the order inbox below, the bot answers `Got it. <order>.`,
  and it isn't passed to Lumi as chat. Everything else is ordinary chat,
  which Lumi sees in her context as before. What orders can say is in
  [the agent's README](../../../plugin/README.md#orders).

The agent decides when to return. See
[the agent's README](../../../plugin/README.md#playing-near-you).

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
character. This happens in both chat and agent mode, and event turns never
call tools. Every event costs a turn of Lumi's chat model, so by default
(`EVENT_REACTIONS=major`) only the big ones are sent; the rest go to the
bot's event log (`GET /events`) only.

| Event | When | Sent with `major` | Bot cooldown |
|---|---|---|---|
| Died | Death, with the mob that last hurt it if that was within 10 s | Yes | — |
| Boss died | An ender dragon, wither, elder guardian or warden dies within 32 blocks ("I killed it" if the bot was fighting it) | Yes | — |
| Top-tier loot | Diamonds, emeralds, ancient debris, netherite, enchanted golden apples, totems, elytra, nether stars | Yes | 15 s per item |
| Other rare pickup | Gold, ender pearls, golden apples, lapis, name tags, saddles | `all` only | 15 s per item |
| Joined the world | First spawn on a server | `all` only | — |
| Took damage | Health drops. Names the nearest hostile and warns at 6 HP or less | `all` only | 20 s |
| Hostile nearby | A hostile mob comes within 10 blocks for the first time. Creepers get an extra warning | `all` only | 25 s |
| Getting hungry | Hunger falls to 6 or below | `all` only | 2 min |
| Killed a mob | A mob the bot was fighting dies | `all` only | 10 s |

Lumi's SDK server applies one more limit on top: at most one event turn every
12 s per client. An event that arrives while Lumi is thinking or speaking is
held (the latest one per client) and delivered when she's free, or dropped if
she's still busy after 20 s. Drops are logged. Set `EVENT_REACTIONS=off` to
turn events off, or `all` for the full Neuro-style commentary.

## Running from Lumi's web UI

You don't have to start `bot.js` yourself. The **Minecraft AI** switch on the
Games page of Lumi's web UI starts it, together with the game agent, as
background processes. If a `bot.js` is already running (the control API on
`:5057` answers), Lumi uses it and leaves it running when you switch off.
See [the agent's README](../../../plugin/README.md#running-it-1).

## Seeing the game: `look_at_minecraft`

While a Minecraft client is connected, Lumi's agent mode gets a
`look_at_minecraft` tool. Ask "What is your other half doing?" and she
captures the Minecraft game window, has the screen vision model (`moondream`)
describe it, and answers using that plus the agent's current thought.

- The capture uses the window's own pixels, so it works while the game is
  behind other windows. It fails if the window is minimized.
- It captures **your** Minecraft client: your player's view, which shows
  LumiBot only if she's on screen. LumiBot's own first-person view is the
  [live viewer](#live-viewer).
- Screenshots are saved under `data/logs/images/minecraft_*.jpg`.
- In chat mode (agent mode off) Lumi answers the same question from the
  silent status line instead.

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
| `GET` | `/status` | `{ready, username, server, auth, version, position, health, food, armor, inventory, slots, held_slot, threats, is_day, busy, defending, held, nearby, free_slots, owner, house}`. While the bot isn't in a world it's just `{ready: false, username, server, auth}`. `inventory` is a flat list of stacks (what the agent reads); `slots` is the 36-slot layout for the Games page's HUD: 0–8 the hotbar, 9–35 the main inventory, each `{slot, name, display_name, count, durability?, max_durability?}` or `null`. `held_slot` is the selected hotbar slot (0–8), `armor` the armor bar's points (0–20). `nearby` says whether a crafting table and a furnace are within 32 blocks and a chest within 16. `owner` is described in [Playing near you](#playing-near-you), `house` is `{origin, material, complete}` or `null`. `heap_mb` is the bot process's heap |
| `GET` | `/events` | Last 100 entries of the bot's event log |
| `GET` | `/config` | `{host, port, username, auth, viewer_port, skin_port}`. `viewer_port` is `null` while the viewer isn't running |
| `POST` | `/config` | `{host, port}`. Updates the target server and reconnects if it changed |
| `POST` | `/action` | `{action, args}`, where `action` is an action name without the `minecraft_` prefix (e.g. `mine_block`). Returns `{success, message}` |
| `POST` | `/directive` | `{text, source?}`. Stores an order for the game agent (the latest one wins, up to 200 characters, control characters removed) and interrupts the running skill unless the bot is fighting. 422 for empty or non-string text |
| `GET` | `/directive` | `{directive}`: the pending order `{text, source, from, at}`, or `null`. `?take=1` also clears it (the game agent does this). `/status` has `directive_pending` |

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
| Python LLM agents (curriculum, action, critic) | No | They require the OpenAI API. `plugin/minecraft_bot.py` uses the same primitives with an inventory-based tech tree plus free-play goals, and asks a small local model (`GAME_AGENT_MODEL`) which goal to run |

The curriculum in `plugin/minecraft_bot.py` goes logs → planks →
crafting table → wooden pickaxe → stone → stone pickaxe → furnace → iron →
smelting → iron pickaxe → diamonds. Between tiers the agent also picks
free-play goals (chop trees, hunt, explore, build a dirt hut at night). See
[the agent's README](../../../plugin/README.md#minecraft-agent-minecraft_botpy).

---

## Troubleshooting

**`bot.js` crashed with `JavaScript heap out of memory`.**
See [Memory](#memory). Look for `[mem]` lines in `data/logs/minecraft/bot.log`
before the crash. If the heap grows with the viewer off, report it. To rule
the viewer out, set `VIEWER_PORT=0`. Close extra viewer tabs.

**`[viewer] live view disabled (Cannot find module 'canvas')`.**
`canvas` is an optional native dependency, so `npm install` carries on without
it if it can't be installed. Run `npm install canvas` in this folder and read
the error it prints. The bot works without the viewer.

**Live View panel says offline.**
The viewer starts only after the bot spawns in a world. Check the `bot.js`
log for `[viewer]` lines: another program on port 3000 (`VIEWER_PORT`) is the
usual cause.

**LumiBot still has the default skin.**
See [Lumi's skin](#lumis-skin): the skin needs the CustomSkinLoader mod in
your client. With the `loadlist` option, `bot.js` must be running when the
skin loads. Press F3+T in game to reload textures.

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

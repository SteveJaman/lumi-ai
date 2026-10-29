# Lumi plays Minecraft

Lumi has an "other half": a character in a Minecraft world that plays on its
own while Lumi keeps chatting. It gathers wood, works its way up to an iron
pickaxe, fights mobs, builds a house, branch-mines, and keeps you in sight like
a kid playing near a parent. You can give it orders from the web UI or in game
chat, and Lumi talks about what her other half is doing.

This page explains the whole feature. The per-folder READMEs have the
reference details:

| Read | For |
|---|---|
| [`../sdk/examples/minecraft/README.md`](../sdk/examples/minecraft/README.md) | `bot.js`: every action, the control API, events, self-defense, the skin, the live viewer, memory |
| [`../plugin/README.md`](../plugin/README.md#minecraft-agent-minecraft_botpy) | The game agent: its loop, the tech tree, free-play goals, orders, playing near you |
| [`../web/README.md`](../web/README.md) | The Games page |
| [security.md](security.md#minecraft-bot-control-api) | What the bot's local API exposes. Read before running it |

---

## Quick start

You need Node.js 18+, Minecraft Java Edition 1.19 (Fabric works), and Lumi
running on the same PC.

1. Once: fetch Voyager and the bot's packages, and pull the small game model.

   ```powershell
   git submodule update --init plugin/voyager
   cd sdk/examples/minecraft; npm install; cd ../../..
   ollama pull qwen2.5:3b
   ```

2. In Minecraft, open a world and choose **Open to LAN**.
3. Start Lumi: `python main.py`.
4. In Lumi's web UI on this PC, open **Games** and turn on **Minecraft AI**.
5. Optional, once: give her her own skin instead of Steve's. See
   [Lumi's skin](#lumis-skin).
6. Under **Server connection**, click your world. `bot.js` hears Minecraft's
   LAN announcement, so it shows up by name with its port. You can also type a
   host and port and press **Reconnect**. The choice is remembered for next
   time.

`LumiBot` joins your world within a few seconds. The Games page shows what
she's thinking, a first-person live view, and a live HUD with her hearts,
hunger, armor, hotbar and inventory. Try typing `Lumi, follow me` in Minecraft
chat.

To stop, turn the switch off. Lumi also stops both processes when she shuts
down, and starts them again on the next boot while the switch stays on.

---

## How it fits together

```
 ┌──────────────────────────── Lumi (python main.py) ─────────────────────────────┐
 │  web server :5000 ── Games page ── /api/game/* , /api/games/{game}/directive    │
 │       │                                                                        │
 │  game manager (modules/game_manager.py) ── starts, stops, watches ──┐          │
 │  prompter (chat model) ◄── SDK server :5056 ◄── status + events ────┼───┐      │
 └─────────────────────────────────────────────────────────────────────┼───┼──────┘
                                                                       │   │
          ┌────────────────────────────────────────────────────────────┘   │
          ▼                                                                │
 plugin/minecraft_bot.py  (the game agent: "MinecraftAgent")  ─────────────┤
   decides what to do each tick, picks goals with GAME_AGENT_MODEL         │
          │ HTTP :5057 (/status, /action, /directive)                      │
          ▼                                                                │
 sdk/examples/minecraft/bot.js  (the body: "MinecraftBot")  ───────────────┘
   mineflayer + Voyager primitives, self-defense, events, order inbox
          │ Minecraft protocol            ├── :3000 live viewer (prismarine-viewer)
          ▼                               └── :3001 skin file for CustomSkinLoader
   your Minecraft world
```

- **`bot.js` is the body.** It joins the world as `LumiBot` and does the
  physical work: walking, mining, crafting, building, fighting. It defends
  itself, reports events (a death, a boss kill, diamonds), takes orders from
  your chat, and serves a local control API on `127.0.0.1:5057`.
- **`plugin/minecraft_bot.py` is the mind.** Every 4 seconds it reads the
  bot's state and decides what to do next. It picks goals with its own small
  model (`GAME_AGENT_MODEL`), so Lumi's chat model is never kept busy.
- **The game manager** in Lumi starts both as background processes when you
  flip the switch. It sends their output to the Dashboard's Event Stream and
  to `data/logs/minecraft/`, and reports a crash with its last lines. On
  Windows the processes are tied to Lumi's, so they can't outlive her.
- **Lumi herself** (the chat model) hears about her other half through the
  SDK: a silent status line every 30 seconds, and spoken events for the big
  moments.

---

## How she decides what to do

Every tick the agent goes down this list and does the first thing that
applies:

1. **Take a new order**, if one arrived (see [Orders](#orders)).
2. **Survive.** If `bot.js` reports an emergency (drowning, or lava close by;
   see [Staying alive](#staying-alive)), do nothing else until it's over.
   Fight a hostile mob within 6 blocks (not endermen); eat when hunger is below
   14. `bot.js` also fights back on its own when hit.
3. **Hold a mode**: follow you closely (the default), or stay put. An
   order, a goal from Lumi or getting her items back after a death comes
   first, and the steps below run only once she's been told to do her own
   thing.
4. **Get her items back** if she died less than 5 minutes ago (dropped items
   despawn after that).
5. **Work on the current order.**
6. **Come back to you** if you called, are out of sight, or are more than 25
   blocks away (64 while on a long project). See
   [Playing near you](#playing-near-you).
7. **Tech tree basics.** Wood → crafting table → wooden pickaxe → cobblestone
   → stone pickaxe → stone axe → stone sword, decided in code. Once she has
   the axe, she chops with it. Progress is read from the inventory, any
   wood type works, and the language model never chooses these steps.
8. **Routines.** Go home at night once there's a finished house, and put
   things away in a chest when the inventory is nearly full.
9. **Free play.** The game model picks from whatever makes sense right now: the
   next tool tier (furnace, iron, smelting, iron pickaxe, diamonds), chopping
   trees, hunting, cooking, exploring, mining coal, branch mining, crafting
   torches, building the house, a dirt hut at night, or waiting out the night.
   Goals that make no sense are never offered: no hut in daylight, no more
   trees with 10+ logs, no house while down a mine. Without the model,
   a weighted random pick stands in.

Each skill checks its own result. A step that changed nothing counts as a
failure, while a step that got *some* of what it went for counts as progress.
A goal that fails three times in a row rests for 5 minutes. A goal that just
succeeded can have a cooldown too: the dirt hut runs once per night.

---

## Orders

Three ways to tell her what to do:

- **Talking to Lumi**, in the web chat or out loud (the chat's mic button or
  the local mic), addressed to her: "Lumi, follow me", "Lumi, can you make
  me a stone axe?", "chop some trees, Lumi". While **Minecraft AI** is on,
  the order goes to the game, your message gets a **⛏️ Sent to Minecraft**
  badge, and Lumi answers in a line ("Fine, following you."). While it's
  off, nothing is sent and she tells you to switch her game on. Anything
  else is ordinary conversation. Typing `!mc <order>` always counts as an
  order. See [Talking to her](#talking-to-her).
- **In Minecraft chat**, as the owner, start with a wake word: `Lumi,`, `Lumi:`,
  `!lumi`, or just `Lumi ` followed by the order. She answers `Got it. …` in
  game chat. The order goes straight to the game agent and doesn't become a
  turn for Lumi's chat model, so nothing interrupts the conversation.
- **On the Games page**, type into **AI directive / nudge** and press
  **Send command**.

By default she's attached to you: she follows you around (within about 5
blocks) and does what you tell her. An order interrupts the following, and
when it's done she comes back and follows again. She stops following only
when you say so ("do your own thing", "stop following"); "follow me" or
"come here" attaches her again. Set `MC_DEFAULT_MODE=free` for the old
behaviour, where she plays on her own near you.

A new order interrupts whatever she's doing, unless she's in the middle of a
fight. Common orders are understood without the model:

| You say | She does |
|---|---|
| `follow me` · `stay close` | Follows you (the default), and again after each order |
| `come here` · `come back` | Comes back and follows you again |
| `stay` · `wait here` · `stop` | Stays put (still defends herself) until the next order |
| `make a bed` · `set your spawn` · `go to sleep` | Makes a bed from sheep's wool, sets her respawn point at a bed, or sleeps (by day, "go to sleep" sets the respawn point instead) |
| `go play` · `do your thing` · `stop following` | Plays on her own near you, until you say `follow me` or `come here` |
| `get me 5 diamonds` · `get some wood` · `mine for iron` | Gets that many *more*. Upgrades her pickaxe first if she has to (diamonds need iron), and with "get **me**" hands them over at the end |
| `make a stone axe` · `craft me an iron sword` · `make an iron pickaxe` | Gets the materials, then crafts that tool: any pickaxe, axe, sword, shovel or hoe in wood, stone, iron or diamond. With "make **me**" or "craft me" she hands it over |
| `give me the pickaxe` · `bring me 5 diamonds` | Walks over and tosses it to you, getting it first if she doesn't have it |
| `build a house` | Builds the house, gathering what's missing |
| `go home` · `make torches` · `sort your stuff` · `go mining` · `explore` | That task, once |

When combined, the come-back, follow and stay kinds win: "forget the diamonds,
come back to me" brings her back. Anything else goes to the game model with
"The user has given you a new direct order: '…'. You must prioritize this above
all current goals." The model may also decide it isn't an order at all, and
then she says in chat that she doesn't know how. An order gives up after three
failures in a row or 20 minutes, and says so: "I failed my order: … I am
giving up."

The full table is in
[the agent's README](../plugin/README.md#orders).

---

### Talking to her

`modules/game_orders.py` decides, by rules alone (no model call, so no
delay), whether what you said is an order:

- `!mc <order>` or `!minecraft <order>` always is.
- Otherwise it has to be addressed to her: start with "Lumi" ("Lumi, …",
  "hey Lumi …") or end with ", Lumi". "Follow me" on its own, or "I told
  Lumi to collect wood", is conversation.
- After the wake word (and "in Minecraft", "can you", "please"), it has to
  be short (16 words at most), start with a command verb, and be something
  the agent's own order parser understands. The verbs: follow, come, stick
  with; collect, get, grab, mine, chop, find, hunt, kill, smelt, craft,
  make; give, hand, bring, drop, throw away; build, construct; stop, stay,
  wait, go play, do your thing. "Lumi, how do I make a stone axe?" doesn't
  start with a verb, so it's a question. Stance words count on their own or
  as a stance ("Lumi, stop", "Lumi, stay here"), so "Lumi, stop it lol" or
  "Lumi, wait, what?" stays conversation.

Then the gate, `game_manager.is_game_active("minecraft")`: the Games page
switch is on and the game agent is running (started by Lumi, or by hand and
writing its telemetry). If it is, the order is sent and her prompt for that
turn gets `[Game Order Dispatched: "…"]`, so she acknowledges it in a line
with the fast chat model, without agent mode or tools. If it isn't, nothing
is sent: the turn gets `[System Note: The user gave a Minecraft order, but
the game agent is offline.]` and she tells you to switch her game on. In
agent mode, `dispatch_game_command` refuses the same way.

Only the host can do this: messages from the host machine's web chat, its
mic and the terminal. A remote web user or someone in Discord voice can't
steer the game, same as the Games page. The order goes through the same
inbox as the Games page, marked as said to Lumi. The game agent doesn't say
its usual "I got an order…" line for it, because Lumi has already answered,
but it does speak up when it can't do it ("I don't have any golden
carrots", "I don't know how to do that").

## Staying alive

**Beds.** With a bed nearby or in her inventory, she sets her respawn point
at it by day, so she respawns next to you and not at world spawn. At night
she sleeps in it. If the game says monsters are nearby, she kills them first
and then lies down. She gets up in the morning, or early if she's hurt,
given an order, or you walk more than 32 blocks away. Without a bed, she
makes one after her stone tools: three sheep, three planks. See
[Beds](../sdk/examples/minecraft/README.md#beds).

Survival runs inside `bot.js`, so it reacts within half a second, and it
outranks everything: an emergency interrupts whatever skill is running (but
never a fight), and the agent waits it out.

| Danger | What she does |
|---|---|
| **Drowning**: head underwater and oxygen below 10/20 | Holds jump and looks up to swim straight up, digging through a block above her head if there is one, until she has air (oxygen 18+) |
| **Lava or fire**: standing in it, or lava within 2.5 blocks with open air next to it | Walks to the nearest standable spot within 6 blocks that has no lava within 3; with none found, backs straight away. Lava sealed behind stone doesn't count, so mining near it doesn't cause panics |
| **Falling** more than 3 blocks | Logs `[SURVIVAL] Falling!`. With a water bucket, places water just before landing ("MLG") and scoops it back up |

Her paths avoid trouble in the first place. She never plans a drop of more than
3 blocks, doesn't treat a fall into water as automatically safe, and avoids
lava, fire, magma, berry bushes, powder snow, campfires and cactus. Water is
expensive to path through, so she goes around it but can still swim when
that's the only way. She can still dig and pillar her way out of holes and
caves.

**Danger sense.** Every status the agent reads includes an environment check:
whether she's in a cave (rock overhead and little sky light), day or night,
oxygen, and hazards (hostile mobs within 8 blocks, lava within 6, being
underwater). The game model sees it as one line, and so does Lumi when
something's notable:

> Environment: You are in a cave. It's night. Creeper at 4 blocks. Oxygen is 8/20. Pathfinding may be difficult.

Lumi can also call `minecraft_scan_environment` herself in agent mode.

**After dying.** `bot.js` saves where she died in
`data/minecraft/death_location.json`. Within the next 5 minutes, before dropped
items despawn, the agent's first goal after respawning is
`retrieve_items`. She walks back, picks up every item near the spot, and
brags about it. If the spot is in lava, in another dimension, unreachable, or
too old, she forgets it and moves on.

---

## Playing near you

Lumi treats one player as hers: `MC_OWNER` in `sdk/examples/minecraft/.env`,
or the first other player online. By default she follows you (see
[Orders](#orders)). The table is for when you've told her to do her own
thing. Distances are horizontal, so mining straight below you still counts
as nearby.

She gets around like a player: she sprints to catch up (when hunger is 7 or
more) and walks the last few blocks. She swims after you across water. In
creative she flies to you when you're far or flying. With an elytra on, she
glides instead of taking fall damage, and uses fireworks to reach you. See
[Moving like a player](../sdk/examples/minecraft/README.md#moving-like-a-player).

| Distance | What she does |
|---|---|
| Under 10 blocks | Plays right next to you; glances at you when you move |
| 10–25 blocks | Roams on her own; exploring drifts back toward you |
| Over 25, or out of sight | Comes back |
| On a project (branch mine, house, ore hunting) | Leash stretches to 64 blocks for 3 minutes |
| You say "come here" / "follow me" in chat | Comes back (the call counts for a minute) |

If she's stuck for 30 seconds on the way back, she tries `/tp` (only works with
cheats on in the LAN world). Survival always comes first: a creeper gets dealt
with before she walks back.

---

## Building and mining

- **House.** A 7x7 house: a floor, 3-high walls, a door gap with a door if she
  can craft one, three glass windows if she has glass, a flat roof, and
  torches inside and out. It takes 165 blocks of cobblestone or planks. If she's
  short, the build stops with the exact shortfall, and she gathers it next.
  Building spreads over several runs, and the site is remembered in
  `data/minecraft/house.json`.
- **Branch mine.** A staircase down to y=12 (or y=-54 for diamonds once she has
  an iron pickaxe), then a 24-block tunnel. She mines every ore she can
  harvest, places a torch every 5 blocks, and stops at lava or water.
- **Chests.** When her inventory is nearly full she crafts and places a chest
  and puts away bulk items, keeping her tools, food, torches and a working
  stock of cobblestone, dirt, logs and planks.

---

## What Lumi says about it

| Channel | When | Lumi's reaction |
|---|---|---|
| Silent status | Every 30 s, and right away when her distance from you changes band | None. It updates her context, so if you ask what she's doing in Minecraft she answers from it |
| Body status | Every chat turn while bot.js is in a world, agent mode or not, game agent connected or not | None. A live `[BODY STATUS]` line read from bot.js that turn: what she's doing (fighting a zombie, escaping lava, asleep, or the current goal), health, hunger, position, what she's holding, day or night, hostiles nearby |
| Mood | Every game moment, spoken or not (deaths, hits, diamonds, kills, a house, orders done or failed) | None out loud: her emotional state moves a little (dying stings, diamonds feel good), clamped so a bad fight makes her grumpy, not unhinged, and it fades back on its own |
| Memory | A death, a finished house, diamonds, her first furnace and iron pickaxe | None. Stored as a long-term memory with the coordinates, so she remembers it in later sessions |
| Spoken event | Deaths (with what killed her), boss kills, diamonds and other top loot, a finished house, each new order from the Games page, an order done or abandoned, an order said to her that she can't do, you hitting her, you crouch-spamming at her | Lumi reacts in a short sentence, in her own voice ("[anxious] Oh, not now.") |

A reaction uses her persona prompt (the same one chat uses, from
`config/lumi.yaml`) plus a short instruction: react in one short sentence,
starting with one emotion tag, without repeating her last few lines. It skips
the rest of the chat prompt, uses the fast model when one is installed, and
usually takes about a second. If the model runs long, Lumi cuts the reply at a
clause, at about 20 words. The event itself isn't shown in the web chat, only
her line.

Spoken events are deliberately rare, because each one is a turn of Lumi's chat
model. `bot.js` sends the same kind of event at most 3 times in any minute, on
top of its per-kind cooldowns. If one arrives while she's mid-reply, it's delivered as soon as she
finishes, or dropped if she's still busy after 20 seconds. `EVENT_REACTIONS=all` (in `bot.js`'s `.env`) and `MC_NARRATE=all`
bring back the full play-by-play: every hit, every mob, every new goal.

**When she's stuck.** When a goal fails twice in a row with a path
problem ("took too long to decide path", "stuck", a timeout), the agent
screenshots the live viewer (first person) with Playwright and asks the
vision model (`qwen2.5vl` by default, `MC_VISION_MODEL`) "what is blocking
the way?". The answer goes into her status for 5 minutes, so Lumi can talk
about it, and is narrated when `MC_NARRATE=all`. It's a hint, not something
the agent acts on: a small vision model gets it wrong now and then. At most
every 3 minutes; it runs in the background and unloads the model after, but
loading it while the chat models are in VRAM can take a couple of minutes.
Lumi can also call `mc_agent_look_around` in agent mode.

**Her own eyes, all the time (opt-in).** `MC_VISION_EVERY=60` makes the
agent glance through the viewer every 60 s and put what it sees ("tall
grass and a cow ahead") into the goal picker's prompt and Lumi's status, so
decisions and reactions can come from what's on screen, mistakes included.
It's off by default: each look loads the vision model (about 6 GB) next to
Lumi's chat models, which on an 11 GB card pushes them out of VRAM and slows
her replies. Worth it with more VRAM, or a smaller vision model in
`MC_VISION_MODEL`.

**The game agent works on its own.** Lumi's chat and her other half are
separate: the game agent (`plugin/minecraft_bot.py`) runs whenever the
Games page's **Minecraft AI** switch is on, whatever agent mode is set to,
and orders reach it without agent mode (talking to her, in-game chat, the
Games page). Agent mode is only for multi-step work with tools. In agent
mode Lumi also has `dispatch_game_command(game, order_text)`: she hands the
order to the game agent and gets "Order dispatched" back at once, without
waiting for it to finish. If the rules already passed this turn's order on,
the tool says so instead of sending it twice.

In agent mode Lumi can also call `look_at_minecraft`: she captures the
Minecraft window, even when it's behind other windows, and a small vision model
(`moondream`) describes it. That shows *your* game client. Her own view is the
live viewer.

---

## The Games page

Over the live view, a small panel shows Lumi's mood (mood, energy,
frustration, curiosity, boredom and her current frame), and the card's
border flashes red when her body takes damage. Other registered games, if
there are any, get a switch each above the Minecraft card.

Everything on **Games → Minecraft AI**, refreshed every 2 seconds while the page
is open:

| Part | What it shows or does |
|---|---|
| Switch, thought, goal, order | On/off, what she's thinking and doing, and the current order |
| AI directive / nudge | Sends an [order](#orders) |
| Live viewer | A first-person 3D view of what `LumiBot` sees (also at `http://localhost:3000`) |
| Connection | "Connected as LumiBot (offline mode) · localhost:52518 · 1.19", or "Offline" with the reason |
| Bars | Hearts, hunger drumsticks and armor, drawn like the game's (ten icons, half icons for odd values; the armor bar only appears when she wears armor) |
| Inventory | The 27-slot inventory and the 9-slot hotbar, with the held slot highlighted, stack counts and durability bars |
| Item tooltip | Hover or focus a slot: display name, item id, count, durability |
| Drop menu | Click a slot: **Drop 1** or **Drop stack**. She tosses it where she's looking |
| Skin | A preview of her skin and **Change skin…** to upload a new one |
| Server connection | Host and port with **Reconnect**, and buttons for the LAN worlds `bot.js` has heard announced (the current one is marked). See [Switching worlds](#switching-worlds) |

Item icons are the textures bundled with the live viewer's package, served by
Lumi, so nothing is fetched from the internet. A few items have none (a chest,
for example) and show their name instead. When the bot isn't in a world, the
HUD says "Offline" and the inventory is cleared, so stale items never show.

The HUD only reads. The one thing it can change in the game is dropping items,
and Lumi passes only that action to the bot (`drop_item`), nothing else.

---

## Switching worlds

Minecraft opens a world to LAN on a new random port each time. You don't
have to edit anything:

- **Pick it from the list.** `bot.js` listens for Minecraft's "Open to LAN"
  announcements, the same broadcast the game's multiplayer screen uses (UDP
  multicast `224.0.2.60:4445`). Every world open on this network appears on
  the Games page by name, with its port. Click one to connect.
- **Or type it.** Enter a host and port and press **Reconnect**.

Before switching, `bot.js` asks the new address for its server status (a
protocol ping). If nothing answers, the current connection is left alone and
the page says why, for example "nothing is listening there. Open the world to
LAN (or check the port) and try again." If the world answers, the bot leaves
the old one, waits 2 seconds, and joins the new one with everything
reattached: pathfinder, viewer, events, and the SDK. The choice is saved in
`data/minecraft/server.json` and used at the next start, ahead of `MC_HOST`
and `MC_PORT` in `.env`. With Minecraft AI off, **Reconnect** just saves it.

---

## Seeing her

- **Live viewer.** A first-person 3D view of what `LumiBot` sees, at
  `http://localhost:3000` and on the Games page. It sends 4 chunks around her
  (`VIEWER_DISTANCE`), down from 6, to keep memory in check.
- **Skin.** See [Lumi's skin](#lumis-skin) below. The live viewer is
  first-person, so it never shows her body or skin.

### Lumi's skin

Out of the box, `LumiBot` looks like Steve. A vanilla LAN world runs in
offline mode, and an offline-mode bot has no way to send a skin to the server,
so every player sees a default one. The fix lives in **your** Minecraft
client: the CustomSkinLoader mod loads her skin from a file on your PC. It
also changes what viewers see when you stream.

One-time setup:

1. **Install CustomSkinLoader for Fabric 1.19.** Download the Fabric build for
   your Minecraft version from
   [Modrinth](https://modrinth.com/mod/customskinloader) and put the `.jar` in
   `%APPDATA%\.minecraft\mods`, next to Fabric API (which Fabric mods need
   anyway). Start Minecraft once with the Fabric profile.
2. **Run the setup script** from the workspace root:

   ```powershell
   python scripts/setup_skin.py --dry-run   # see what it will change
   python scripts/setup_skin.py
   ```

   It copies her skin (`sdk/examples/minecraft/lumi_skin.png`) to
   `%APPDATA%\.minecraft\CustomSkinLoader\LocalSkin\skins\LumiBot.png`
   (named after `MC_USERNAME`). It puts the mod's LocalSkin loader first in
   `CustomSkinLoader.json`, ahead of Mojang, which would otherwise return the
   skin of whoever owns that name on a real account; the old file is kept as
   `.bak`. It also clears the
   mod's cache so a cached Steve doesn't stick. It warns if the mod isn't in
   your mods folder.
3. **Reload.** Restart Minecraft, or press F3+T in game.

For a launcher instance (Prism, CurseForge, …) pass its folder:
`--minecraft-dir "D:\Prism\instances\1.19\.minecraft"`.

To change her skin later, upload one with **Change skin…** on the Games page
(a 64x64 or old-style 64x32 PNG; the previous one is kept as
`lumi_skin.bak.png`), then run `python scripts/setup_skin.py` again to copy it
to the mod. `--from-sheet` rebuilds the original skin from her reference sheet
in `assets/branding/`. That sheet is a character drawing, not a skin layout, so
it can't simply be cropped. Other players on the LAN need the mod and the file
too. More options, such as loading the skin straight from `bot.js`'s skin
server, are in [the bot's README](../sdk/examples/minecraft/README.md#lumis-skin).

---

## Performance

- **Models.** Goal picking uses `GAME_AGENT_MODEL` (`qwen2.5:3b`, about 2 GB),
  small enough to stay loaded next to Lumi's chat model on an 11 GB GPU. A
  bigger game model would push the chat model out of VRAM and slow her replies.
  If chat replies take 10+ seconds, Ollama is probably swapping models: setting
  `FAST_MODEL_NAME` to the same model as `MODEL_NAME` frees one slot.
- **Memory.** One session ended with `bot.js` out of memory at 4 GB. The live
  viewer is the likely cause: it sends every chunk to each open browser. A
  watchdog now pauses the viewer above 1.5 GB of heap and brings it back later,
  and `[mem] heap … MB` in `data/logs/minecraft/bot.log` shows the trend. Keep
  one viewer open (the Games page *or* the old `:5002` diagnostics page).

---

## Files and ports

| Path | What it is |
|---|---|
| `data/minecraft/autoplay.json` | The Games page switch. The agent checks it every tick |
| `data/minecraft/telemetry.json` | The agent's latest state, thought and order, read by the Games page |
| `data/minecraft/events.jsonl` | The agent's event log |
| `data/minecraft/house.json` | The house site and whether it's finished |
| `data/minecraft/server.json` | The world picked on the Games page (overrides `.env`) |
| `data/minecraft/death_location.json` | Where she last died, until her items are back or gone |
| `sdk/examples/minecraft/lumi_skin.png` | Her skin (`SKIN_PATH` overrides it). The upload keeps the previous one as `lumi_skin.bak.png` |
| `data/logs/minecraft/agent.log`, `bot.log` | Output of the two processes |

| Port | What | Reachable from |
|---|---|---|
| 5057 | `bot.js` control API | This PC only |
| 3000 | Live viewer | All interfaces (prismarine-viewer can't be told otherwise) |
| 3001 | Skin file | This PC only |
| 4445/udp | Listens for "Open to LAN" announcements (receives only) | Your local network |
| 5002 | Old diagnostics page (`python plugin/mc_dashboard.py`, optional) | This PC only |

None of these go through the Cloudflare tunnel.

---

## Safety

- The `bot.js` control API on `:5057` has **no authentication**. Anything on
  this PC, including a web page in your browser, could run code in `bot.js`
  or give her orders. Turn Minecraft AI off when you aren't using it. Details
  and the planned fix are in [security.md](security.md#minecraft-bot-control-api).
- The Games page routes that start processes and send orders are host-only:
  someone using Lumi through the tunnel can't reach them.
- In game, only the owner's wake-word messages are orders. Other players'
  chat is just chat.

---

## Limitations

- Mineflayer's pathfinder sometimes can't plan a path ("Took to long to decide
  path to goal!"). Such steps fail fast, keep whatever they got, and fall back
  to branch mining or exploring.
- The order parser knows common phrasings. Unusual ones depend on a 3B model's
  judgment.
- Diamond tools only when you order them. No enchanting, farming, the Nether,
  or bows.
- She swims at the surface at normal speed (no fast swimming), flies only in
  creative, and doesn't take off with an elytra from the ground.
- `/tp` for getting unstuck needs cheats on.
- Survival is best-effort: the water-bucket landing needs a bucket and good
  timing; a lava escape needs somewhere safe within 6 blocks; dropped items
  despawn after 5 minutes.

See [known-limitations.md](known-limitations.md#minecraft) and
[troubleshooting.md](troubleshooting.md#minecraft-bot-and-agent).

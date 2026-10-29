# Plugins

External apps that connect to Lumi through the [SDK](../sdk/README.md). None of them are imported by Lumi; each runs as its own process.

| Path | What it is |
|---|---|
| `plugin.py` | Reference plugin: screenshot and note actions. Start here when writing your own |
| `minecraft_bot.py` | Lumi's Minecraft game agent (SDK client `MinecraftAgent`). Started by Lumi from the Games page |
| `mc_dashboard.py` | Optional Minecraft diagnostics page at `http://127.0.0.1:5002/`. The Games page in Lumi's web UI covers day-to-day use |

For an overview of the Minecraft feature, start with
[`../docs/minecraft.md`](../docs/minecraft.md).
| `voyager/` | Git submodule of [MineDojo/Voyager](https://github.com/MineDojo/Voyager). Read-only; do not edit |

---

# Reference plugin (`plugin.py`)

`plugin.py` is the reference implementation for the Lumi External SDK. It is a small, working external app that connects to a running Lumi over the local websocket, registers two actions, and handles invocations when Lumi's model calls them. Read it, run it, then copy it as a starting point for your own plugin.

## Actions

| Action | Tier | What it does |
|---|---|---|
| `screenshot_primary_monitor` | `read` | Captures the primary monitor and saves the image. |
| `log_note` | `write` | Appends a timestamped note to a text file. |

In Lumi's tool list these appear as `sdk__screenshot_primary_monitor` and `sdk__log_note`.

## Requirements

- Lumi running, with the SDK module enabled.
- `mss` for screen capture:

```powershell
pip install mss
```

The SDK client library also needs `websockets>=12.0`. If it is not already installed, run `pip install "websockets>=12.0"`.

## Running it

From the workspace root:

```powershell
python plugin/plugin.py
```

Expected output once the plugin is approved (see the next section):

```text
[sdk] connecting to ws://127.0.0.1:5056/ws
[sdk] welcome — session=... protocol=1.0
[sdk] registered: screenshot_primary_monitor
[sdk] registered: log_note
```

The plugin authenticates with the token Lumi generates at `data/memories/sdk_token.txt`. Leave the process running; Lumi can only call the actions while the plugin is connected.

## Approving the plugin

The SDK is deny-by-default, so the plugin has to be approved before its actions register. Without an allowlist entry, neither `screenshot_primary_monitor` nor `log_note` registers.

1. Start Lumi, then run the plugin as above. It connects but registers nothing.
2. On this first connect, the SDK server adds a denied entry named `"Lumi Plugin"` (the plugin's `game` name) to `data/memories/sdk_allowlist.json`.
3. Open that file and edit the entry under `clients`:

```json
{
  "deny_by_default": true,
  "clients": {
    "Lumi Plugin": {
      "approved": true,
      "capability_ceiling": ["read", "write"],
      "auto_approve_writes": false
    }
  }
}
```

4. Stop the plugin (Ctrl+C) and run it again. Lumi does not need a restart, because the server reads the allowlist on every new connection. You should now see the `registered:` lines shown above.

The ceiling must cover the highest tier the plugin declares. `screenshot_primary_monitor` is `read` and `log_note` is `write`, so both tiers are listed. An action above the ceiling is refused. If an action is refused or keeps asking for confirmation, see [Troubleshooting](../docs/troubleshooting.md#security-and-refusals). For why the SDK works this way, see [Security](../docs/security.md).

## Triggering it

In the web chat, turn agent mode ON, then send:

```text
Take a screenshot.
```

Lumi calls `sdk__screenshot_primary_monitor`, the plugin captures the display, and Lumi replies in character. To exercise the other action, ask her to save a note, for example "Log a note: check the SDK docs tomorrow."

## Where output lands

| Path | Written by | Contents |
|---|---|---|
| `data/logs/screenshots/` | `screenshot_primary_monitor` | Captured screenshots |
| `data/notes.txt` | `log_note` | Timestamped notes, one per entry |

`data/logs/` is gitignored. `data/notes.txt` is not currently matched by `.gitignore`, so check `git status` before committing if you use `log_note`.

## Reading the code

`plugin.py` demonstrates the patterns most plugins need:

- **Subclassing `LumiClient`.** The plugin is one class on top of the SDK's base client. Connection, handshake, and reconnection come from the base class.
- **Registering actions in `setup()`.** Each action is built with the SDK's `action()` builder and bound to a handler method.
- **Returning `(success, message)` tuples.** Handlers report the outcome as a tuple. The message is what Lumi's model sees as the tool result, so write it as a plain sentence.
- **Pushing context.** The plugin sends context to Lumi after connecting, which Lumi injects into its prompt between `[FROM SDK]` markers.
- **Handling `on_lumi_message`.** The plugin receives Lumi's replies (the `speech_finished` broadcast) through this callback and prints them.

## Adapting it

Two things trip up most first plugins.

**Naming.** If a built-in tool or a shell command shares vocabulary with your action name, the model routes to the built-in and your action never fires. `screenshot_primary_monitor` works because nothing else in Lumi can capture a display. Three earlier candidate actions (`open_notepad`, `get_time_in`, `read_clipboard`) lost to built-ins. Before writing an action, check whether Lumi can already do it. The reasoning and the full table are in the [SDK design notes](../sdk/README.md#design-notes-for-plugin-authors).

**Capability tier.** Every action declares one of three tiers:

| Tier | Behavior |
|---|---|
| `read` | Auto-approved. |
| `write` | Gated on a `y/n` prompt in Lumi's terminal, unless the allowlist entry sets `auto_approve_writes: true`. |
| `dangerous` | Always gated on a `y/n` prompt in Lumi's terminal. |

Calling `requires_confirm()` on an action forces the `y/n` prompt regardless of its declared tier. The prompt appears in Lumi's terminal, not in the plugin. See [Capability tiers](../sdk/README.md#capability-tiers) for details.

---

# Minecraft agent (`minecraft_bot.py`)

Lumi's "other half": an autonomous Minecraft player that runs in the
background, separate from her chat. It is built on the same control
primitives as [Voyager](https://github.com/MineDojo/Voyager) and drives the
mineflayer bot in
[`../sdk/examples/minecraft/bot.js`](../sdk/examples/minecraft/README.md)
through that bot's local control API (`127.0.0.1:5057`).

It keeps Lumi's chat responsive in three ways:

- **Its own model.** Goals are picked by `GAME_AGENT_MODEL` (`qwen2.5:3b`),
  small enough to stay loaded next to Lumi's chat model instead of swapping
  it out of VRAM.
- **Silent status.** Every 30 s it replaces a short status line in Lumi's
  prompt (health, hunger, held item, what it's doing). Lumi reads it when
  she next talks, without being interrupted.
- **Spoken events only for big moments.** Only diamonds (from the agent)
  and deaths, boss kills and top-tier loot (from `bot.js`) make Lumi react
  out loud. Each one costs a turn of her chat model.

## Running it

From the web dashboard (recommended):

1. Start Lumi: `python main.py`.
2. Open the **Games** page in Lumi's web UI on the host machine and turn on
   the **Minecraft AI** switch.

Lumi starts `bot.js` (unless one is already running) and this agent as
background processes, with no extra terminals. The card shows the agent's
current thought, health, goal and current order, and the bot's first-person
live view. Its **AI directive / nudge** box gives her [orders](#orders). The
switch writes `data/minecraft/autoplay.json`. Switching it off stops both
processes, and Lumi restarts them on the next boot while it stays on.

Their output goes to the dashboard's Event Stream (`minecraft` filter) and
to `data/logs/minecraft/agent.log` and `bot.log`. If either process exits
unexpectedly, the card shows the exit code and its last lines, and the
Event Stream logs an error. On Windows the processes are tied to Lumi's,
so they never outlive it, even if Lumi crashes.

By hand, for debugging (from the workspace root, `bot.js` first):

```powershell
cd sdk/examples/minecraft; node bot.js     # terminal 1
python plugin/minecraft_bot.py             # terminal 2
python plugin/mc_dashboard.py              # optional diagnostics page
```

The agent needs only the standard library plus the SDK's `websockets`. Goal
picking also uses Lumi's `modules/llm_providers.py` when it can import it,
so run it from Lumi's Python environment.

## The loop

Every `MC_LOOP_INTERVAL` seconds the agent:

0. Checks `data/minecraft/autoplay.json`. While `"minecraft"` is `false` it stands by and does nothing else. A missing file counts as `true`, so running it by hand just works.
1. Reads the bot's position, health, hunger, inventory, nearby threats and time of day from `GET /status`, and every `MC_STATUS_INTERVAL` seconds pushes Lumi's silent status line.
2. **Survives.** If `bot.js` reports an emergency (drowning, lava), it does nothing else that step: survival overrides every goal. If a hostile other than an enderman is within 6 blocks, it calls `defend`. If hunger is below 14 and there is food, it calls `eat_food`. Either one uses up the step. Survival always comes before going back to the player: next, if the player called, is out of sight, or is past the leash, the goal is `return_to_player`. See [Playing near you](#playing-near-you).
3. **Picks a goal.** A goal set through `mc_agent_set_goal` comes first. Then the [tech tree](#tech-tree) decides, in code, until there's a stone pickaxe. After that the options are the next later tier that can run plus every free-play goal whose gate passes and that isn't resting. The game model (`MC_GOAL_MODEL`, default `GAME_AGENT_MODEL` from `constants.py`) picks one from the exact inventory, day or night, and the recent goals with their results. If the model isn't pulled (the agent checks at startup and logs the `ollama pull` command) or its call fails, a weighted random pick takes over: shelter at night, food when hungry, and the tool tier 70% of the time.
4. **Narrates, if it's big.** With the default `MC_NARRATE=major`, only mining diamonds becomes a spoken `[MINECRAFT EVENT]`. With `MC_NARRATE=all` it also announces each new goal, each finished curriculum tier and a goal failing three times in a row, at most one every `MC_NARRATE_COOLDOWN` seconds.
5. **Executes** the goal's skill through `POST /action` as `execute_skill`, with a per-goal time limit (`GOAL_TIMEOUT_S`: 60 s for chopping and exploring, 90 s for mining stone and the hut, 120 s otherwise) so a stuck pathfinder fails fast. If Voyager's primitives aren't loaded, it runs the goal's plain-action fallback instead (for example `mine_block` then `craft_item`), when the goal has one.
6. **Rests goals.** After `build_dirt_hut` succeeds it isn't offered again that night, nor for 10 minutes. A goal that fails three times in a row is rested for 5 minutes, and the agent does something else meanwhile.

All HTTP and LLM calls run in executor threads, so a two-minute skill never blocks the SDK websocket.

### Free-play goals

Defined in `FREE_GOALS`. They can repeat. Each gate is checked in code, so the
model is never offered something that makes no sense right now.

| Goal | Offered when | What it does |
|---|---|---|
| `chop_trees` | Fewer than 10 logs | Mines 4 logs of whatever tree is closest (any wood type), exploring if none are in sight |
| `hunt_food` | Always | Finds a cow, pig, sheep or chicken and kills it |
| `cook_meat` | Raw meat, and coal or wood | Smelts up to 6 raw meat, placing a furnace if needed |
| `explore` | Always | Explores in a random direction for 45 s |
| `mine_coal` | Any pickaxe | Explores until coal ore is in sight, mines 5 |
| `build_dirt_hut` | Night, and not built yet tonight | Digs 25 dirt and seals itself in a 3×3 dirt box |
| `wait_out_night` | Night | Waits up to 100 s for morning |
| `build_dirt_hut` | Night, no finished house, not built yet tonight | (as above) |
| `build_house` | Day, no finished house, 64+ cobblestone or wood on hand, roughly level with the player (not down a mine) | `build_house` in `bot.js`, with whichever material there's more of. A `Missing materials` failure puts the item on a wishlist |
| `gather_cobblestone` | The house wants cobblestone, any pickaxe | Mines 16 stone. (`chop_trees` is also offered past 10 logs while the house wants planks) |
| `branch_mine` | Stone pickaxe | `mine_branch_tunnel` in `bot.js`: down to y=12 for iron, or y=-54 for diamonds once there's an iron pickaxe, then a 24-block tunnel |
| `craft_torches` | Coal, fewer than 16 torches, some wood | Sticks if needed, then up to 4 torch recipes. The random picker does this before a branch mine |

**Routines** are decided in code before the model is asked, like the tech
tree:

| Routine | When | What it does |
|---|---|---|
| `retrieve_items` | She died less than about 5 minutes ago (`death.recoverable` in `/status`). Checked before orders and before following (a stay mode still comes first) | `retrieve_items` in `bot.js`: walks back and picks up her dropped items. A spoken event when she gets them |
| `sleep_in_bed` | Night (or a thunderstorm), with a bed within 32 blocks or in her inventory. A routine checked in code, before the leash and before following | `use_bed` in `bot.js`: places the bed if needed, fights monsters that are too close, then sleeps. While she's asleep the loop waits |
| `set_spawn` | By day, with a bed handy and no respawn point set at one yet. Same routine | `use_bed` with `mode: "spawn"`. After a failure (an old server), rested for 30 minutes |
| `get_bed` | No bed handy or remembered. In code after the stone kit, by day; also offered to the model | Kills sheep until there are 3 wool of one colour, then crafts that bed |
| `go_home` | Night, the house is finished, and the bot isn't in it | Walks to the middle of the house (then `wait_out_night` takes over) |
| `organize_inventory` | 6 or fewer free slots | `sort_inventory` in `bot.js`, crafting and placing a chest if none is within 16 blocks. Then rested for 2 minutes |

Spoken events from these goals: finishing the house, and finding diamonds in
a branch mine.

### Orders

You can tell Lumi's other half what to do, in two ways:

- **In game**, as the owner: start a chat message with `Lumi,`, `Lumi:`,
  `!lumi` or just `Lumi `, e.g. `Lumi, get me 5 diamonds`. `bot.js` answers
  `Got it. get me 5 diamonds.` in chat. A bare `Lumi` gets
  `Lumi here. What should I do?`.
- **From the web UI**: the Games page's directive box
  (`POST /api/games/minecraft/directive`, host only).

Both go to `bot.js`'s order inbox (`POST /directive`), which keeps the latest
one and **interrupts the running skill**, so the new order takes effect right
away. A fight already in progress isn't interrupted. The agent takes the order
on its next tick (`GET /directive?take=1`). An interrupted goal isn't counted
as a failure.

The order is parsed in code first. These are checked in order, so "Forget
diamonds, come back to me" means "come back":

| Kind | Example phrases | What happens |
|---|---|---|
| Resume | "go play", "do your thing", "stop following" | Clears any order or mode and detaches her: free play near you |
| Follow | "follow me", "stay close", "come with me" | Attaches her: follow mode, within about 5 blocks of you. The default (`MC_DEFAULT_MODE=follow`) |
| Come back | "come here", "come back", "where are you" | `return_to_player`, then follow mode (attaches her) |
| Stay | "stay", "wait here", "stop", "don't move" | Stay mode: does nothing but survive until told otherwise |
| Give | "give me the pickaxe", "bring me 5 diamonds", "hand over your sword" | If she has it: walks over and tosses it to you (`give_item`; a loose name like "pickaxe" means her best one, a plural like "the diamonds" means all of them). If not, and it's a pickaxe or something she can collect, she gets or makes it first and then hands it over. Otherwise she says she doesn't have any |
| Tool | "make a stone axe", "craft me an iron sword", "make an iron pickaxe", "…smelt it into an iron pickaxe" | Any pickaxe, axe, sword, shovel or hoe in wood, stone, iron or diamond (stone when no material is named: "craft an axe"). `_craft_step` gets what's missing first: wood for planks and sticks, a pickaxe to mine stone, iron mined and smelted (with a furnace, or 8 cobblestone to make one), diamonds from a branch mine with an iron pickaxe. Then she crafts it (`craft_<material>_<tool>`, or the tier goal for a pickaxe). For herself, that tier or better is enough; with "make **me**", "craft me" or "for me" she crafts that exact tool and hands it over. Checked before Collect, so "make a stone axe" is about the axe, not cobblestone |
| Collect | "get me 5 diamonds", "get some wood", "mine for iron", "coal", "cobblestone" | An objective: N more than she has now (defaults: 3 diamonds, 6 iron, 8 coal, 16 logs, 32 cobblestone). She works through the tech tree first when she needs a better pickaxe (diamonds need iron), then branch-mines, chops or mines until she has them. With "get **me**", "bring me" or "for me" she hands them over at the end |
| House | "build a house" | Builds the house, gathering what it's missing, until it's done |
| Task | "go home", "make torches", "sort your stuff", "hunt", "cook", "make a shelter", "make a furnace", "go mining", "explore" | That goal, once |
| Bed | "make a bed", "set your spawn", "go to sleep", "go to bed" | `get_bed`, `set_spawn` or `sleep_in_bed`. "Go to sleep" by day sets the respawn point instead (she says she can only sleep at night). With no bed, she makes one first, and the bed routine sleeps or sets the spawn after |

Anything else goes to the game model with the override prompt ("The user has
given you a new direct order: '…'. You must prioritize this above all current
goals."), which picks one goal or says it isn't an order. If it isn't
understood, she says so in chat and keeps her current order.

An order outranks free play and the leash, but survival still comes first.
A mining or chopping step that gets some of what it went for before failing
(an unreachable ore, a path that takes too long) counts as a success with what
it got, so progress is kept and isn't mistaken for failure. When an order's
next step is resting after 3 failures, the order tries an alternative: branch
mining instead of `mine_iron_ore`, `mine_coal` or mining stone, exploring
instead of chopping.

Orders give up after 3 failures in a row (any success resets the count) or
20 minutes, with "I failed my order: … I am giving up." and the last
problem. Lumi's chat model hears
about each order (a spoken event, so she acknowledges it: "Alright, I'll go
find some diamonds. Don't wander too far."), sees it in her silent status
line, and hears when it's done or given up.

### Playing near you

By default (`MC_DEFAULT_MODE=follow`) she's attached: she follows the
player, any order runs first, and she goes back to following when it's done
(`_attached` in the agent). An order, a goal from Lumi, or getting her items
back after a death comes before following. The rest of this section applies
once she's detached ("do your own thing", or `MC_DEFAULT_MODE=free`).

Detached, Lumi plays near one player, like a kid near a parent: she wanders
off to do her own thing but comes back when she's too far away. `bot.js` reports the
player (`MC_OWNER`, or the first other player online) in `GET /status`, and
the agent uses the **horizontal** distance, so mining straight below them
still counts as nearby.

| Distance | What happens |
|---|---|
| Under 10 blocks | Near: free play as usual |
| 10–25 blocks | Roaming: free play, and `explore` heads back toward the player once more than 15 blocks out |
| Over 25 blocks, or out of sight | `return_to_player`, unless on a project |
| Over 64 blocks during a project | `return_to_player` even on a project |
| The player called ("come here", "follow me", …) | `return_to_player` within 60 s of the message |

A **project** is `branch_mine`, `build_house`, `mine_iron_ore` or
`mine_diamond`. Starting one gives the long 64-block leash for 3 minutes, and
an unfinished house renews it. Survival still runs first, so a creeper gets
fought before she walks back.

The goal picker's prompt says where the player is, with the rule: "You are
playing near Anthony (18 blocks away). If you get too far (25+ blocks) or they
call you, go back to them. Otherwise, do your own thing nearby." Lumi's silent
status line says whether she's right next to them, roaming, or has wandered
off, and is sent right away whenever that changes. Lumi's own prompt tells her
she is independent but likes to keep the host in her sights, and to say what
she's doing when she wanders off.

### Tech tree

Defined in `CURRICULUM`. Progress is read from the inventory, plus a crafting
table or furnace placed within 32 blocks (from `GET /status`). It never
depends on which goal last succeeded, so 40 logs from `chop_trees` count as
wood, and any wood type works.

The first five tiers are decided in code (`_tech_goal`), in this order, and
the model never sees them:

| Tier | Chosen when | Skill |
|---|---|---|
| `collect_wood` | Not enough wood for what's next (a table, sticks and a pickaxe come to 9 planks) | Chops 5 logs of the nearest tree |
| `craft_workbench` | Enough wood, no crafting table in inventory or nearby | Planks from whichever logs there are most of, then a crafting table |
| `craft_wood_pickaxe` | Has a crafting table | Places the table on a free spot if needed, then sticks and a wooden pickaxe |
| `mine_stone` | Wooden pickaxe, fewer than 3 cobblestone | Explores until stone is in sight, mines 8 |
| `craft_stone_pickaxe` | Wooden pickaxe and 3 cobblestone | Sticks if needed, then a stone pickaxe |

Then, still in code (`_kit_goal`), the rest of a first stone kit, the way a
player does it: a stone axe, so trees come down fast (mining equips the best
tool for the block, so once she has an axe, she chops with it), then a stone
sword. Each one mines the cobblestone or chops the wood it's missing first.
An axe or sword of a better tier counts.

With a stone pickaxe, the next of these is offered to the model as the
"tool progress" option, as soon as its inputs are on hand:

| Tier | Ready when | Skill |
|---|---|---|
| `craft_furnace` | 8 cobblestone | Furnace at a crafting table |
| `mine_iron_ore` | Stone pickaxe | Explore until iron ore is in sight, mine 3 |
| `smelt_iron` | 3 raw iron, and a furnace or 8 cobblestone | Smelts with coal, or planks if there's no coal |
| `craft_iron_pickaxe` | 3 iron ingots | Iron pickaxe at a crafting table |
| `mine_diamond` | Iron pickaxe | Explore downward until diamond ore is in sight, mine 1 |

Every tier checks its own result and fails if nothing changed. So mining stone
with none in reach is a failure, and rests after three, instead of a
"success" that repeats forever. The dashboard's progress count is the number
of tiers already done according to the inventory.

## SDK actions

The agent connects as `MinecraftAgent`. The allowlist entry needs
`capability_ceiling: ["read", "write"]`.

| Action | Tier | What it does |
|---|---|---|
| `mc_agent_status` | `read` | Loop state, goal (and who picked it), recent goals, current order, position, health, hunger, armor, the hotbar (with the held slot), inventory, tech progress |
| `mc_agent_set_goal` | `write` | Run a specific curriculum tier or free-play goal next |
| `mc_agent_pause` | `write` | Pause the loop |
| `mc_agent_resume` | `write` | Resume the loop |
| `mc_agent_look_around` | `read` | Screenshot the live viewer and ask the vision model what's there (optionally a question). Can be wrong |

### Other games

`MinecraftAgent` is a `BaseGameAgent` ([`base_game_agent.py`](base_game_agent.py)):
the shared loop, the Games page switch (`autoplay.json` under the game's
id), and `narrate()`, `feel()` and `remember()` for talking to Lumi. A new
game is a subclass with `GAME_ID`, `GAME_NAME` and `tick()`, plus an entry
in `modules/game_manager.py`'s `GAME_REGISTRY` (or `register_game()`), and
the Games page lists it with a switch. [`game_agent_mock.py`](game_agent_mock.py)
is the smallest one: it plays nothing, and shows up when `LUMI_DEV_GAMES=mock`.
Its log goes to `data/logs/mock/agent.log`. Each agent connects as its own SDK
client, so each needs its own allowlist entry.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `BOT_API` | `http://127.0.0.1:5057` | `bot.js` control API |
| `MC_LOOP_INTERVAL` | `4` | Seconds between loop steps |
| `MC_ACTION_TIMEOUT` | `135` | Seconds to wait for a skill. Keep it above `bot.js`'s 120 s `SKILL_TIMEOUT_MS` |
| `MC_GOAL_PICKER` | `llm` | `llm` asks the game model to pick goals, `random` skips it |
| `MC_GOAL_MODEL` | `GAME_AGENT_MODEL` from `constants.py` | Model spec for goal picking, in the same format as Lumi's other model settings |
| `MC_GOAL_KEEP_ALIVE` | `30m` | How long Ollama keeps the game model loaded between picks |
| `MC_STATUS_INTERVAL` | `30` | Seconds between silent status pushes to Lumi |
| `MC_NARRATE` | `major` | Spoken events from the agent: `major` (diamonds), `all` (also goals, milestones, repeated failures), or `off` |
| `MC_NARRATE_COOLDOWN` | `75` | Minimum seconds between non-major narration events |
| `MC_VISION_EVERY` | `0` | Seconds between background glances through the live viewer, fed to the goal picker and Lumi's status. `0`: only when stuck. Costs VRAM, see [docs/minecraft.md](../docs/minecraft.md) |
| `MC_VISION_MODEL` | `VISION_MODEL_NAME` from `constants.py` (`qwen2.5vl:latest`) | Vision model for looking around when stuck (`game_vision.py`). moondream is faster but answers these frames with junk |
| `MC_DEFAULT_MODE` | `follow` | `follow`: she follows the player and goes back to following after each order, until told to do her own thing. `free`: she plays on her own near the player |

## Files it writes

| Path | Contents |
|---|---|
| `data/minecraft/telemetry.json` | Latest bot state, loop state and `current_thought`, read by the dashboard |
| `data/minecraft/events.jsonl` | Append-only event log |
| `data/minecraft/autoplay.json` | Written by the dashboard switch, read by the agent and by Lumi at boot |
| `data/logs/minecraft/agent.log`, `bot.log` | Output of the processes Lumi started |

All of these are gitignored.

---

# Minecraft dashboard (`mc_dashboard.py`)

A standalone page at `http://127.0.0.1:5002/` (`MC_DASH_PORT`), standard library only.

| Panel | Source |
|---|---|
| Lumi's current thought | `current_thought` in telemetry: what the agent is doing or deciding, in plain words |
| Live view | An iframe of `bot.js`'s first-person viewer, found through `GET 127.0.0.1:5057/config`. Shows "offline" until the bot spawns |
| Bot telemetry | `data/minecraft/telemetry.json` |
| Connection status | Telemetry, plus `GET 127.0.0.1:5057/status` from the browser |
| Live command relay | `data/minecraft/events.jsonl` merged with `GET 127.0.0.1:5057/events` |
| Minecraft server | `GET`/`POST 127.0.0.1:5057/config` |
| Error tracker | The last 30 errors in telemetry |

**LAN worlds.** Minecraft picks a new port each time you choose *Open to LAN*.
Enter it in the **Minecraft Server** panel and click **Reconnect**; `bot.js`
reconnects without a restart.

The page follows Lumi's mint and ice palette, with a light/dark toggle stored
in the browser.

---

# Voyager (`voyager/`)

A git submodule pinned to an upstream commit. After cloning Lumi, fetch it with:

```powershell
git submodule update --init plugin/voyager
```

`bot.js` loads Voyager's `voyager/control_primitives/*.js` and
`env/mineflayer/lib/skillLoader.js` from here at startup. Nothing else in the
submodule runs. Treat the folder as read-only: local edits would show as a
dirty submodule and be lost on the next update. What is and isn't used is
listed in the [Minecraft README](../sdk/examples/minecraft/README.md#voyager-integration-notes).

## See also

- [`../sdk/README.md`](../sdk/README.md) — SDK reference: wire protocol, client, action builder
- [`../sdk/examples/minecraft/README.md`](../sdk/examples/minecraft/README.md) — the Minecraft bot, its actions, and the Voyager integration
- [`../sdk/client.py`](../sdk/client.py) — `LumiClient` source
- [`../sdk/examples/system_stats.py`](../sdk/examples/system_stats.py) — second Python reference client
- [`../docs/architecture.md`](../docs/architecture.md) — where the SDK fits in Lumi's turn flow
- [`../docs/security.md`](../docs/security.md) — the SDK allowlist and capability ceiling
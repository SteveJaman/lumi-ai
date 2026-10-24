# Plugins

External apps that connect to Lumi through the [SDK](../sdk/README.md). None of them are imported by Lumi; each runs as its own process.

| Path | What it is |
|---|---|
| `plugin.py` | Reference plugin: screenshot and note actions. Start here when writing your own |
| `minecraft_bot.py` | Autonomous Minecraft agent (SDK client `MinecraftAgent`) |
| `mc_dashboard.py` | Minecraft diagnostics page at `http://127.0.0.1:5002/` |
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
2. Open the **Dashboard** page in Lumi's web UI on the host machine and turn
   on the **Minecraft AI** switch.

Lumi starts `bot.js` (unless one is already running) and this agent as
background processes, with no extra terminals. The card shows the agent's
current thought, health and goal, and the bot's first-person live view. The
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
2. **Survives.** If a hostile other than an enderman is within 6 blocks, it calls `defend`. If hunger is below 14 and there is food, it calls `eat_food`. Either one uses up the step.
3. **Picks a goal.** A goal set through `mc_agent_set_goal` comes first. Otherwise the options are the next curriculum tier plus every free-play goal that can run now. The game model (`MC_GOAL_MODEL`, default `GAME_AGENT_MODEL` from `constants.py`) picks one from a status summary and the recent goal history. If the model isn't pulled (the agent checks at startup and logs the `ollama pull` command) or its call fails, a weighted random pick takes over: shelter at night, food when hungry, and the curriculum tier 70% of the time.
4. **Narrates, if it's big.** With the default `MC_NARRATE=major`, only mining diamonds becomes a spoken `[MINECRAFT EVENT]`. With `MC_NARRATE=all` it also announces each new goal, each finished curriculum tier and a goal failing three times in a row, at most one every `MC_NARRATE_COOLDOWN` seconds.
5. **Executes** the goal's skill through `POST /action` as `execute_skill`. If Voyager's primitives aren't loaded, it runs the goal's plain-action fallback instead (for example `mine_block` then `craft_item`), when the goal has one.
6. On success, a curriculum tier is marked verified in `data/minecraft/skills.json`. A goal that fails three times in a row is left out of the options until it succeeds again, unless nothing else can run.

All HTTP and LLM calls run in executor threads, so a two-minute skill never blocks the SDK websocket.

### Free-play goals

Defined in `FREE_GOALS`. They can repeat and are never marked verified.

| Goal | Available | What it does |
|---|---|---|
| `chop_trees` | Always | Mines 4 logs of whatever tree is closest (any wood type), exploring if none are in sight |
| `hunt_food` | Always | Finds a cow, pig, sheep or chicken and kills it |
| `cook_meat` | Raw meat and coal or planks in inventory | Smelts up to 6 raw meat, placing a furnace if needed |
| `explore` | Always | Explores in a random direction for 45 s |
| `mine_coal` | Any pickaxe | Explores until coal ore is in sight, mines 5 |
| `build_dirt_hut` | Always | Digs 25 dirt and seals itself in a 3×3 dirt box. The random picker builds one at the start of each night |
| `wait_out_night` | Night | Waits up to 100 s for morning |

### Curriculum

Defined in `CURRICULUM`:

| Tier | Needs | Skill |
|---|---|---|
| `collect_wood` | — | `mineBlock(bot, "oak_log", 5)` |
| `craft_planks` | 5 oak logs | `craftItem(bot, "oak_planks", 5)` |
| `craft_workbench` | 4 planks | `craftItem(bot, "crafting_table", 1)` |
| `craft_wood_pickaxe` | crafting table, 7 planks | Place the table, craft sticks and a wooden pickaxe |
| `mine_stone` | wooden pickaxe | `mineBlock(bot, "stone", 11)` |
| `craft_stone_pickaxe` | 3 cobblestone, 2 sticks | Stone pickaxe at a crafting table |
| `craft_furnace` | 8 cobblestone | Furnace at a crafting table |
| `mine_iron_ore` | stone pickaxe | Explore until iron ore is in sight, mine 3 |
| `smelt_iron` | 3 raw iron | Place the furnace if needed, smelt with planks |
| `craft_iron_pickaxe` | 3 iron ingots, 2 sticks | Iron pickaxe at a crafting table |
| `mine_diamond` | iron pickaxe | Explore downward until diamond ore is in sight, mine 1 |

If the next tier lacks materials, for example because a previous run used them
up, the agent re-runs the closest earlier tier that can run instead of stopping.
The skills use oak, so start the bot near oak trees.

## SDK actions

The agent connects as `MinecraftAgent`. The allowlist entry needs
`capability_ceiling: ["read", "write"]`.

| Action | Tier | What it does |
|---|---|---|
| `mc_agent_status` | `read` | Loop state, goal (and who picked it), recent goals, position, health, inventory, curriculum progress |
| `mc_agent_set_goal` | `write` | Run a specific curriculum tier or free-play goal next |
| `mc_agent_pause` | `write` | Pause the loop |
| `mc_agent_resume` | `write` | Resume the loop |

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

## Files it writes

| Path | Contents |
|---|---|
| `data/minecraft/telemetry.json` | Latest bot state, loop state and `current_thought`, read by the dashboard |
| `data/minecraft/events.jsonl` | Append-only event log |
| `data/minecraft/skills.json` | Verified tiers. Delete it to restart the curriculum |
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
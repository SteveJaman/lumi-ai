# Reference Plugin

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

<!-- VERIFY: confirm plugin.py reads the token from data/memories/sdk_token.txt by default. -->

The plugin authenticates with the token Lumi generates at `data/memories/sdk_token.txt`. Leave the process running; Lumi can only call the actions while the plugin is connected.

## Approving the plugin

The SDK is deny-by-default, so the plugin has to be approved before its actions register. Without an allowlist entry, neither `screenshot_primary_monitor` nor `log_note` registers.

1. Start Lumi, then run the plugin as above. It connects but registers nothing.
2. On this first connect, the SDK server adds a denied entry for the plugin to `data/memories/sdk_allowlist.json`.
3. Open that file, find the entry, set `approved: true`, and add `capability_ceiling: ["read", "write"]`:

```json
"approved": true,
"capability_ceiling": ["read", "write"]
```

4. Restart Lumi, then run the plugin again. You should now see the `registered:` lines shown above.

<!-- VERIFY: confirm the entry's surrounding structure (key name, nesting) in data/memories/sdk_allowlist.json, and that Lumi must be restarted rather than reloading the file live. -->

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

Both paths sit under `data/`, which is covered by the `data/*` rule in `.gitignore`, so neither is committed.

## Reading the code

`plugin.py` demonstrates the patterns most plugins need:

- **Subclassing `LumiClient`.** The plugin is one class on top of the SDK's base client. Connection, handshake, and reconnection come from the base class.
- **Registering actions in `setup()`.** Each action is built with the SDK's `action()` builder and bound to a handler method.
- **Returning `(success, message)` tuples.** Handlers report the outcome as a tuple. The message is what Lumi's model sees as the tool result, so write it as a plain sentence.
- **Pushing context.** The plugin sends context to Lumi after connecting, which Lumi injects into its prompt as `[EXTERNAL CONTEXT]`.
- **Handling `on_lumi_message`.** The plugin receives Lumi's replies (the `speech_finished` broadcast) through this callback.

<!-- VERIFY: confirm the context push and on_lumi_message usage in plugin.py match the two bullets above. -->

## Adapting it

Two things trip up most first plugins.

**Naming.** If a built-in tool or a shell command shares vocabulary with your action name, the model routes to the built-in and your action never fires. `screenshot_primary_monitor` works because nothing else in Lumi can capture a display. Three earlier candidate actions (`open_notepad`, `get_time_in`, `read_clipboard`) lost to built-ins. Before writing an action, check whether Lumi can already do it. The reasoning and the full table are in the [SDK design notes](../sdk/README.md#design-notes-for-plugin-authors).

**Capability tier.** Every action declares one of three tiers:

| Tier | Behavior |
|---|---|
| `read` | Auto-approved. |
| `write` | Runs and is logged. |
| `dangerous` | Gated on a `y/n` prompt in Lumi's terminal. |

Calling `requires_confirm()` on an action forces the `y/n` prompt regardless of its declared tier. The prompt appears in Lumi's terminal, not in the plugin. See [Capability tiers](../sdk/README.md#capability-tiers) for details.

## See also

- [`../sdk/README.md`](../sdk/README.md) — SDK reference: wire protocol, client, action builder
- [`../sdk/client.py`](../sdk/client.py) — `LumiClient` source
- [`../sdk/examples/system_stats.py`](../sdk/examples/system_stats.py) — second Python reference client
- [`../docs/architecture.md`](../docs/architecture.md) — where the SDK fits in Lumi's turn flow
- [`../docs/security.md`](../docs/security.md) — the SDK allowlist and capability ceiling
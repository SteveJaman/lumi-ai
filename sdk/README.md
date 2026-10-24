# Lumi External SDK

The Lumi External SDK lets other applications plug into Lumi over a local websocket. An external app connects, registers its own actions (tools), pushes context, and receives invocations when Lumi decides to use one of those actions. The protocol is modeled on Vedal's Neuro SDK and shares its wire format, so the Neuro SDK documentation is a valid secondary reference.

Version 1.0. Python client library included; any language with a websocket client can speak the protocol.

## Quick start

With Lumi running and agent mode ON in the web chat:

```powershell
pip install mss
python plugin/plugin.py
```

Expected output:

```text
[sdk] connecting to ws://127.0.0.1:5056/ws
[sdk] welcome — session=... protocol=1.0
[sdk] registered: screenshot_primary_monitor
[sdk] registered: log_note
```

On the first run the actions are refused until you approve the client: set `"approved": true` for `"Lumi Plugin"` in `data/memories/sdk_allowlist.json` and restart the plugin (see [Allowlist](#allowlist)).

Then say "Take a screenshot." in the web chat. Lumi calls `sdk__screenshot_primary_monitor`, the plugin captures the display, and she replies in character.

## Contents

```text
sdk/
├── __init__.py        Public exports: ActionSpec, action, LumiClient
├── protocol.py        Wire protocol constants and message builders
├── actions.py         action() builder and param() JSON-schema helper
├── client.py          LumiClient base class for external apps
├── errors.py          SDK exceptions
├── server.py          SDKServer module (runs inside Lumi)
├── README.md          This file
└── examples/
    ├── system_stats.py          Python reference client (psutil stats)
    └── minecraft/
        ├── bot.js               Node.js mineflayer bot with Voyager skill support
        ├── package.json         npm dependencies
        ├── .env.example         Config template
        └── README.md            Setup, action reference, Voyager notes
```

Third-party developers need four files: `protocol.py`, `actions.py`, `client.py`, and `errors.py`. They are standalone and import nothing else from Lumi's internals. `server.py` runs inside Lumi and is not something a plugin imports.

## Architecture

```text
   Lumi process                                    External app
  ┌──────────────────────────┐                  ┌──────────────────────┐
  │  Prompter (agent turn)   │                  │  LumiClient subclass │
  │    tool list             │                  │    handlers          │
  │    + sdk__<name> schemas │                  │    registered actions│
  │            │             │                  │           │          │
  │            ▼             │   ws://127.0.0.1 │           ▼          │
  │  SDKServer (sdk/server)  │◄════════════════►│  websocket client    │
  │    token auth           │      :5056/ws    │                      │
  └──────────────────────────┘                  └──────────────────────┘
```

The server binds to `127.0.0.1:5056` only. The client connects, authenticates, and registers actions. On each agent turn, Lumi adds the registered actions to the model's tool list as `sdk__<name>`. When the model calls one, the server sends an `action` message to the client that registered it, waits for an `action/result`, and returns the result to the model. Context the client pushes is injected into Lumi's prompt between `[FROM SDK]` markers on every turn. A context message sent with `silent: false` is an event instead: it starts a turn so Lumi reacts to it out loud (see [context](#context)). Every Lumi reply is broadcast back to connected clients as `speech_finished`.

## Authentication

The server generates a token on first boot and stores it at `data/memories/sdk_token.txt`. On POSIX systems the file is created with 0600 permissions. All connected clients share this one token.

Pass the token to `LumiClient` either as a path or as a string:

```python
LumiClient(game="My App", token_path="data/memories/sdk_token.txt")
LumiClient(game="My App", token="the-token-string")
```

If the token is missing or does not match, the server sends an `error` (`invalid auth_token`) and closes the socket. A client that authenticates but is not approved in `data/memories/sdk_allowlist.json` stays connected and receives `speech_finished`, but every `actions/register` is refused. See [Allowlist](#allowlist).

## Allowlist

The SDK is deny-by-default. The first time a client connects, the server adds a denied entry for its `game` name to `data/memories/sdk_allowlist.json`. To approve it, edit the entry:

```json
{
  "deny_by_default": true,
  "clients": {
    "My App": {
      "approved": true,
      "capability_ceiling": ["read", "write"],
      "auto_approve_writes": false
    }
  }
}
```

The server reads the file on every new connection, so restarting the client is enough. Lumi does not need a restart, although the refusal message still says it does. `capability_ceiling` is the highest tier the client may register (see [Capability tiers](#capability-tiers)). `auto_approve_writes: true` skips the `y/n` prompt for `write` actions from this client.

## Wire protocol

Messages are plaintext JSON objects. Client messages carry the client's `game` name; server messages do not:

```json
{"command": "<name>", "game": "<app name>", "data": {}}
{"command": "<name>", "data": {}}
```

Commands are namespaced with `/` where they group (`actions/register`, `action/result`, `context/clear`). The constants live in `sdk/protocol.py` (`ClientCommand`, `ServerCommand`).

### Client to server

| Command | Purpose |
|---|---|
| `hello` | Open the session and present the token. Must be the first message, within 10 s |
| `actions/register` | Register one or more actions |
| `actions/unregister` | Drop actions this client registered |
| `action/result` | Return the result of an invocation |
| `context` | Push context text into Lumi's prompt (replaces this client's previous context) |
| `context/clear` | Remove context this client pushed |
| `pong` | Reply to the server's `ping` |
| `actions/force` | Stubbed: the server answers with an `error`. Do not rely on it |

### Server to client

| Command | Purpose |
|---|---|
| `startup` | Handshake accepted; carries the session id and protocol version |
| `actions/registered` | Acknowledge one registered action |
| `action` | Invoke a registered action |
| `speech_finished` | Lumi finished a reply |
| `ping` | Keepalive, every 30 s |
| `error` | Report a protocol, auth, or approval failure |

### hello and startup

```json
{"command": "hello", "game": "My App", "client_name": "My App",
 "client_version": "0.1.0", "auth_token": "..."}
```

The server answers with `startup`:

```json
{"command": "startup",
 "data": {"session": {"sessionId": "...", "characterId": "lumi",
                      "displayName": "Lumi Voidwright"},
          "protocolVersion": "1.0"}}
```

`LumiClient` registers its actions when `startup` arrives.

### actions/register

```json
{
  "command": "actions/register",
  "game": "My App",
  "data": {
    "actions": [
      {
        "name": "log_note",
        "description": "Save a timestamped note to the notes file.",
        "schema": {"type": "object",
                   "properties": {"text": {"type": "string"}},
                   "required": ["text"]},
        "capabilities": ["write"],
        "requires_confirm": false
      }
    ]
  }
}
```

`capabilities` and `requires_confirm` are Lumi extensions to Neuro's format; see [Capability tiers](#capability-tiers). The server acknowledges each accepted action separately:

```json
{"command": "actions/registered", "data": {"action_name": "log_note"}}
```

An action whose capabilities exceed the client's ceiling is refused with an `error` instead.

### action

```json
{"command": "action",
 "data": {"id": "a1b2c3", "name": "log_note",
          "data": "{\"text\": \"remember to buy milk\"}"}}
```

`data.data` is a JSON-encoded string, not an object. Parse it before use. This matches Neuro's convention. `LumiClient` does the parsing for you and hands your handler a dict.

### action/result

```json
{"command": "action/result", "game": "My App",
 "data": {"id": "a1b2c3", "success": true, "message": "Saved note."}}
```

`id` must echo the `id` from the `action` message. `message` is what Lumi's model sees as the tool result. The server waits 30 s for the result, then reports `action timed out` to the model.

### context

```json
{"command": "context", "game": "My App",
 "data": {"message": "Player is at 12 HP.", "silent": true}}
```

`silent` decides what the message is:

| `silent` | Meaning |
|---|---|
| `true` (default) | Standing context. Replaces this client's previous context and is shown to Lumi, as data, on every turn until replaced or cleared. |
| `false` | One-shot event. Starts a turn so Lumi reacts to it out loud, and leaves the standing context unchanged. |

Event turns are spoken reactions only. They run in conversational mode with no tools, even when agent mode is on, and the event text reaches the model as data between `[FROM SDK: <game>]` markers. If Lumi is already thinking or speaking, the server holds the event (the latest one per client) and starts the turn when she's free, or drops it after 20 s. An event less than 12 s after the same client's last event turn is dropped. Only approved clients can send events. To stop one client starting turns, set `"allow_reactions": false` on its allowlist entry.

### speech_finished

```json
{"command": "speech_finished",
 "data": {"message": "Screenshot taken.", "role": "assistant",
          "isFinal": true, "cancelled": false}}
```

`cancelled` is true when the user interrupted her (barge-in). `isFinal` marks the last chunk of a reply.

### context/clear, ping, pong, error

```json
{"command": "context/clear", "game": "My App"}
{"command": "ping"}
{"command": "pong", "game": "My App"}
{"command": "error", "data": {"message": "invalid auth_token"}}
```

## Building a client

A minimal plugin that registers one action. `setup()` runs after every successful handshake, and `run()` connects and reconnects until `stop()` is called:

```python
import asyncio
from datetime import datetime
from sdk import LumiClient, action


class NotesPlugin(LumiClient):
    async def setup(self):
        self.register_action(
            action("log_note", "Save a timestamped note to notes.txt.")
            .param("text", "string", description="The note to save", required=True)
            .capabilities("write")
            .build(),
            self.handle_log_note,
        )
        await self.send_context("Notes plugin is online.")

    async def handle_log_note(self, args):
        line = f"{datetime.now().isoformat(timespec='seconds')} {args['text']}\n"
        with open("notes.txt", "a", encoding="utf-8") as f:
            f.write(line)
        return "Saved note."


client = NotesPlugin(
    game="Notes",
    token_path="data/memories/sdk_token.txt",
)

if __name__ == "__main__":
    asyncio.run(client.run())
```

`LumiClient.__init__` takes: `game`, `host`, `port`, `token`, `token_path`, `reconnect_delay`, `on_lumi_message`, `on_connected`. `host` and `port` default to `127.0.0.1` and `5056`.

### Handler return shapes

The client library normalizes what a handler returns into an `action/result` (`LumiClient._format_result`).

| Handler returns | Sent as |
|---|---|
| `None` | `success: true`, empty message |
| `str` | `success: true`, message is the string |
| `bool` | `success` is the bool, empty message |
| `(bool, str)` | `success` and message from the tuple |
| `dict` with `success` and `message` | Used as given |

If a handler raises, the client sends `success: false` with the exception text as the message.

## Action builder

`sdk/actions.py` provides `action()` and `param()`. In `param(name, type_, *, description, required, enum, default)`, everything after the type is keyword-only. `capabilities()` takes the tiers as separate arguments, not a list.

```python
from sdk import action

spec = (
    action("set_volume", "Set the music volume for the player app.")
    .param("level", "integer", description="Volume from 0 to 100", required=True)
    .param("channel", "string", description="Which output to change",
           enum=["music", "voice"], default="music")
    .capabilities("write")
    .requires_confirm(False)
    .build()
)
```

The equivalent hand-written JSON Schema:

```json
{"type": "object",
 "properties": {
   "level":   {"type": "integer", "description": "Volume from 0 to 100"},
   "channel": {"type": "string", "description": "Which output to change",
               "enum": ["music", "voice"], "default": "music"}},
 "required": ["level"]}
```

Schemas are plain JSON Schema, compatible with both Ollama's tool interface and OpenAI function calling. Keep them flat: small models handle a few simple parameters far better than nested objects.

## Capability tiers

Each action declares `capabilities=["read" | "write" | "dangerous"]` and `requires_confirm=bool`.

| Tier | Behavior |
|---|---|
| `read` | Auto-approved. Runs without a prompt. |
| `write` | Gated on a `y/n` prompt, unless the client's allowlist entry has `auto_approve_writes: true`. |
| `dangerous` | Always gated on a `y/n` prompt. |

`requires_confirm=True` upgrades any action to the dangerous behavior regardless of its declared tier. A client can only register tiers that its allowlist `capability_ceiling` covers.

The prompt appears in Lumi's terminal, not in the client. The user answers there, the same way they confirm `write_file` or `run_command`. If the user declines, the action is never sent to your client. Lumi's model receives a declined result and can tell the user.

## Design notes for plugin authors

Small-model tool selection routes by token overlap. Built-in tools are in the schema list from turn 1, and shell commands share vocabulary with everyday requests. If your action's name or description overlaps with either, the model picks the built-in and your action never fires.

The reference plugin hit this three times before one action worked:

| Plugin action | Lost to | Why |
|---|---|---|
| `open_notepad` | `run_in_terminal` | Built-in opens any command including Notepad |
| `get_time_in(city)` | `web_search` | Built-in has a dedicated timezone handler |
| `read_clipboard` | `run_command clip` | Shell command shares query vocabulary |
| `screenshot_primary_monitor` | (nothing) | Unique capability |

Rules:

- A plugin action needs a capability that no built-in tool and no shell command covers. Before writing one, ask "can Lumi already do this with a built-in?" If yes, do not write the plugin.
- Name the action for the capability, not the operation. `screenshot_primary_monitor` beats `capture`. `log_note` beats `write_note`.
- Avoid any word that also appears in `run_command` examples.

## Errors

`LumiClient` does not raise on network or protocol problems. It prints them and keeps reconnecting, so a plugin survives Lumi restarts:

| Situation | What the client does |
|---|---|
| Lumi not running, port closed | Prints `[sdk] connection error`, retries after `reconnect_delay` |
| Token rejected | Prints `[sdk] server error: invalid auth_token`; the server closes the socket and the client retries |
| Client not approved, or an action above the ceiling | Prints `[sdk] server error: ...`; stays connected without those actions |
| Handler raises | Sends `success: false` with `handler raised: <message>` |
| Invocation takes more than 30 s | The server reports `action timed out` to Lumi; the client is not told |

Construction errors do raise: `ValueError` when neither `token` nor `token_path` is given, and `RuntimeError` when `websockets` is not installed.

`sdk/errors.py` defines `SDKError` and its subclasses `AuthError`, `ProtocolError`, `ActionError` and `ClientDisconnected`. Only the server raises one today (`AuthError`, internally, for a bad token). They are exported for plugins that want to raise them from their own code.

## Reconnection

`LumiClient` reconnects after a fixed delay, `reconnect_delay`, which defaults to 5 seconds.

Registered actions and their handlers survive a reconnect: the client re-registers them automatically. Context pushed with `context` does not survive. Re-push any context you still want Lumi to see, for example from the `on_connected` callback.

## Reference clients

- `examples/system_stats.py` — Python client that exposes system stats (uses `psutil`).
- `examples/minecraft/bot.js` — Node.js mineflayer bot, connecting as client
  `MinecraftBot`. Registers 13 actions: movement, chat, status, digging, and
  combat, plus five that wrap [Voyager](https://github.com/MineDojo/Voyager)'s
  control primitives (`minecraft_mine_block`, `minecraft_craft_item`,
  `minecraft_smelt_item`, `minecraft_place_item`, `minecraft_kill_mob`).
  `minecraft_execute_skill` runs Voyager-style JavaScript with those
  primitives in scope. See [`examples/minecraft/README.md`](examples/minecraft/README.md).
- [`../plugin/minecraft_bot.py`](../plugin/minecraft_bot.py) — Python client
  `MinecraftAgent`. Runs an autonomous skill loop against `bot.js` and
  registers status and control actions. It keeps playing when Lumi is not
  running. See [`../plugin/README.md`](../plugin/README.md).

## Debugging

**Client will not connect.** Confirm Lumi is running and the SDK server started. The server listens on `127.0.0.1:5056` only, so a client on another machine cannot reach it. Check that nothing else holds port 5056. Confirm the token file exists at `data/memories/sdk_token.txt` and matches what you pass to `LumiClient`.

**Actions register but never fire.** Agent mode must be ON. Then check the action name and description against [Design notes](#design-notes-for-plugin-authors): if a built-in tool or shell command covers the same request, the model routes there. Rename for the capability, or drop the action.

**Invocation times out.** The client is connected but the handler did not return a result in time. Keep handlers fast, and avoid blocking calls inside async handlers. Confirm the handler returns one of the shapes in [Handler return shapes](#handler-return-shapes). If the action is `dangerous` or `requires_confirm`, check Lumi's terminal, because it may be waiting on a `y/n` answer.

**Lumi narrates the result instead of calling the action.** The model described the outcome without emitting a tool call. This is usually a naming or description problem: the description should state the concrete capability in plain words, and the name should not overlap with built-ins. Shorten the description and retry.

## Protocol versioning

The protocol is at version 1.0 and follows semantic versioning: breaking wire changes bump the major version, additive changes bump the minor. The version is sent by the server in its welcome message during the handshake (`protocol=1.0`). Clients should check it and refuse to run against a major version they do not support.

## See also

- [`../plugin/plugin.py`](../plugin/plugin.py) — working reference plugin
- [`../docs/architecture.md`](../docs/architecture.md) — where the SDK fits in Lumi's turn flow
- [`protocol.py`](protocol.py) — message constants and builders
- [Neuro SDK](https://github.com/VedalAI/neuro-sdk) — the protocol this one is modeled on
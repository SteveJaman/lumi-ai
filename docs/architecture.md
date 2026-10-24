# Architecture

How a message travels through Lumi, where each module sits, and why the
system is shaped this way.

The design goal is architectural parity with Neuro-sama: continuous
operation, internal state, autonomous decisions. The system is a set of
independent modules coordinated through one shared state object
(`signals`), with the prompter as the central hub for anything that
involves the LLM.

## Turn flow

Every input — terminal, microphone, Discord, web chat — lands in the
same place: `signals.history` as a user message, with
`signals.new_message` flipped to `True`. The prompter's run loop picks
it up on its next tick (50ms).

```
User input (terminal / mic / Discord / web chat)
    │
    ▼
signals.history.append({role: "user", content: ...})
signals.new_message = True
    │
    ▼
Prompter run loop (tick every 50ms)
    │
    ├─► Command router
    │     │
    │     ├─ !agent_on / !agent_off / !agent <task>
    │     ├─ !list / !help / !state / !self / !model
    │     ├─ !recall / wipe memory / clear memory / reset memory
    │     ├─ !set / !expressions / !expression / !move / !hotkeys / !positions
    │     ├─ !library
    │     ├─ set mood <name> / switch mood <name>
    │     └─ !sleep
    │     │
    │     └─ Commands that print to stdout are captured via
    │        _run_command_and_capture() and appended to history
    │        as system messages tagged source="command"
    │
    ├─► Conversational mode (agent mode OFF)
    │     │
    │     ├─ Routing decision: fast or main path
    │     │    - fast: 8K ctx, think=false
    │     │    - main: 8K ctx, think=true
    │     │    - attachments present: main path with think=true
    │     │
    │     ├─ Build system prompt:
    │     │    persona (live from config/lumi.yaml via get_system_prompt())
    │     │    + conversational directive
    │     │    + time anchor
    │     │    + mood profile
    │     │    + emotional state + cognitive frame
    │     │    + ambient context (window title; screen desc on match)
    │     │    + self-model observations (if fresh < 1h)
    │     │    + Discord voice context (if in voice)
    │     │    + library RAG reference (if a matching file exists)
    │     │    + session continuity (first 3 turns)
    │     │    + Fish Audio tag rules
    │     │    + output format rules
    │     │    + random thought seed
    │     │    + per-module injections
    │     │    + identity anchor
    │     │
    │     └─ chat_sync(model_spec, messages, think=…)
    │           → Ollama /api/chat or cloud provider
    │
    └─► Agent mode → run_agent_turn() on main path
          │
          ├─ Build system prompt (same stack + tool directive)
          │
          ├─ chat_sync(model_spec, messages, tools=all_tools, think=true)
          │     all_tools = TOOL_SCHEMAS + EXTRA_TOOL_SCHEMAS + SDK schemas
          │
          ├─ If tool_calls returned:
          │     │
          │     ├─ Confirmation gates (write_file, run_command,
          │     │   run_in_terminal)
          │     ├─ Argument validation (vtube_set_expression, vtube_move)
          │     ├─ Dispatch via _dispatch_tool_call()
          │     │    → execute_tool() for built-ins
          │     │    → sdk.invoke_action_blocking() for sdk__<name>
          │     │    → web_search / browse_url / recall_memory /
          │     │      search_library special-cased
          │     │
          │     ├─ Append tool result as role: "tool"
          │     ├─ Wrap external-content results in provenance markers
          │     ├─ Reinject original request if result > 800 chars
          │     └─ Loop back to chat_sync (up to MAX_AGENT_STEPS = 6)
          │
          └─ If plain text returned:
                → push to TTS
                → save to history
                → sdk.broadcast_lumi_message()
                → self_model.note_turn()
```

After the reply lands in history, the web client sees it on its next
`/api/history` poll (1.5s). If only the newest entry changed in place, the
poll patches that one bubble instead of redrawing; see
[Web server](#web-server-modulessocketiopy).

## Prompt assembly stack

The system prompt is built fresh every turn. The layers, in order:

| Layer | Source | Injected when |
|---|---|---|
| Persona | `config/lumi.yaml` via `get_system_prompt()` | Every turn |
| Tool directive | Hardcoded in prompter | Agent mode only |
| Conversational directive | Hardcoded in prompter | Conversational only |
| Time anchor | `_get_local_now()` | Every turn |
| Mood profile | `MoodManager.get_injection()` | Every turn |
| Emotional state + frame | `emotional_state.describe()` | Every turn |
| Active window | `ambient.current_window` | Every turn |
| Screen description | `ambient.get_context_line()` | Only on screen-reference regex match |
| Voice context | `signals.voice_context` | Only when in Discord voice |
| Self-model observations | `self_model.get_injection()` | Only when observations are fresh (< 1h) |
| Library RAG reference | `memory_module.API.search_library()` | Only when query > 8 chars and top hits are within distance threshold |
| Session continuity | `session_state.format_injection()` | First 3 turns of a session |
| Fish Audio rules | Hardcoded | Every turn |
| Output format rules | Hardcoded | Every turn |
| Random thought seed | Random choice | Every turn |
| Module injections | Each module's `get_prompt_injection()` | When `.text` is non-empty |
| Identity anchor | Hardcoded | Every turn |

The `HOST` object (`constants.py`) holds the active user's name and
`user_id`. On web login the host switches and `get_system_prompt()`
rebuilds with the new name. Modules can register listeners with
`HOST.on_change(callback)` to react to the switch.

The persona is injected with `<host>` placeholders already substituted
for the current user's name. Persona saves through the web client call
`invalidate_prompt_cache()` so the next turn rebuilds it.

## Module topology

```
                          ┌────────────────────┐
                          │   User inputs      │
                          │  ┌──────────────┐  │
                          │  │  Terminal    │  │
                          │  │  Microphone  │  │
                          │  │  Discord     │  │
                          │  │  Web chat    │  │
                          │  └──────┬───────┘  │
                          └─────────┼──────────┘
                                    │
                          ┌─────────▼─────────┐
                          │ signals.history   │
                          │ signals.new_msg   │
                          └─────────┬─────────┘
                                    │
       ┌────────────────────────────┼───────────────────────────┐
       │                            │                           │
┌──────▼──────┐         ┌───────────▼───────────┐   ┌───────────▼────────┐
│ Command     │         │ Prompter               │   │ Avatar commands    │
│ router      │         │ (conversational or     │   │ (VTS via           │
│             │         │  agent turn)           │   │  vtubeStudio.py)   │
└─────────────┘         └───────────┬───────────┘   └────────────────────┘
                                    │
                            ┌───────▼────────┐
                            │  chat_sync()   │
                            │  → Ollama      │
                            │  → cloud       │
                            └───────┬────────┘
                                    │
       ┌────────────────┬───────────┼───────────┬───────────────┐
       │                │           │           │               │
   ┌───▼───┐        ┌───▼────┐  ┌───▼───┐  ┌────▼────┐  ┌───────▼──────┐
   │ Tools │        │ Memory │  │ TTS   │  │ SDK     │  │ Self-model   │
   │ FS +  │        │ inject │  │ Fish  │  │ fan-out │  │ note_turn    │
   │ shell │        │ + recall│  │ Audio │  │         │  │              │
   └───────┘        └────────┘  └───┬───┘  └─────────┘  └──────────────┘
                                    │
                          ┌─────────▼─────────┐
                          │  output routing   │
                          │  → local speakers │
                          │  → Discord voice  │
                          │  → browser tabs   │
                          └───────────────────┘

Background loops (independent threads):
  heartbeat (30s)      · emotional_state decay (30s)
  ambient_perception   · self_model (60s/600s)
  memory reflection    · discord client
  log hub (HTTP :5001) · SDK server (:5056)
  library_indexer (60s)
  web server (aiohttp + socket.io, :5000)
    └─ web-memory worker (1 thread, memory/library calls from HTTP)
```

## Process and threads

Lumi is one Python process. Nothing is a separate service except Ollama,
VTube Studio, and any cloud provider you configure.

| Thread | Started by | Event loop | Purpose |
|---|---|---|---|
| Main | `python main.py` | `asyncio.run(prompter.run())` | Prompter run loop (50ms tick) |
| One per module | `main.py` → `module.init_event_loop()` | Own `asyncio` loop each | Memory, library indexer, mic, Discord, VTS, heartbeat, emotional state, self-model, SDK server, TTS, and the rest of the module list |
| Web server | `main.py` → `SocketIOServer.run()` | aiohttp's loop | HTTP routes, static files, socket.io, history broadcaster |
| Web-memory worker | `SocketIOServer.__init__` | None (`ThreadPoolExecutor`, 1 worker) | Memory and library calls made by HTTP handlers |
| Default executor | aiohttp loop | None | PDF/DOCX extraction, VLM image analysis, Ollama and provider calls from the Models page |
| Log hub | `main.py` → `LogHub.start()` | None (`ThreadingHTTPServer`) | Diagnostics dashboard and `/events` |

All of them share state through `signals`. Cross-thread calls into the web
server's loop (TTS audio fan-out) use `asyncio.run_coroutine_threadsafe`.

## Background modules

Each runs in its own thread with its own event loop, spawned by
`main.py`. They expose `run()` and optionally `get_prompt_injection()`.

| Module | Trigger | What it does |
|---|---|---|
| `heartbeat.py` | 30s tick | Decides whether to speak unprompted. Gates: not busy, user idle 5+ min, cooldown 10+ min. |
| `ambient_perception.py` | 30s sample | Screen hash comparison. VLM call only on window change or hash delta > 14 bits, rate-limited to once per 90s. |
| `emotional_state.py` | 30s decay | Exponential decay toward baselines. Boredom ticks up during idle. |
| `self_model.py` | 60s snapshot / 600s reflect | Records turn stats and emotional snapshots. Reflects every 10 min if ≥5 turns. |
| `memory.py` | every 10 messages | Reflection pass extracts facts from recent history. Routes through `chat_sync(FAST_MODEL_NAME, think=False)`. |
| `library_indexer.py` | 60s rescan | Walks `data/library/`, indexes new files into the `library_docs` collection, and prunes chunks for files that no longer exist. |
| `discordClient.py` | continuous | Voice receive (DAVE decrypt + Opus decode + VAD) and send (Fish Audio → FFmpeg). |
| `log_hub.py` | continuous | Diagnostics telemetry at `127.0.0.1:5001`. See [Diagnostics telemetry](#diagnostics-telemetry). |
| `sdk/server.py` | continuous | Websocket server at `127.0.0.1:5056` for external plugins. |
| `socketIO.py` | continuous | Web client backend at `127.0.0.1:5000`. See [Web server](#web-server-modulessocketiopy). |

## Data flow between subsystems

### Memory

```
Chat turns → reflection pass → extracted facts
                                    │
                                    ▼
                          ChromaDB (data/memories/chroma.db/)
                          collection: neuro_collection
                                    │
                          Retrieval scoring:
                            0.5 * similarity
                          + 0.3 * importance
                          + 0.2 * recency (30-day half-life)
                                    │
                                    ▼
                    get_prompt_injection() → system prompt
```

Every fact carries `timestamp`, `importance`, `valence`, `session_id`,
and `user_id`. Every read path filters by `user_id` — memory is
per-user. The reflection pass routes through `chat_sync` with
`think=False` and `FAST_MODEL_NAME`, so cloud providers work for
background tasks transparently.

### Library RAG

```
Upload arrives via POST /api/chat/upload
    │
    ▼
Saved to data/library/{documents,images,text}/
    │
    ▼
Extraction:
  images      → read_image_vlm() → text report
  pdf / docx  → _read_pdf / _read_docx
  text-like   → read directly
    │
    ▼
Auto-generated sidecar: <timestamp>_<name>.meta.md
    │
    ▼
Indexed into ChromaDB collection library_docs
    │
    ▼
Passive injection (chat mode)   OR   search_library tool (agent mode)
    │
    ▼
Injected as [FACTUAL LIBRARY REFERENCE] or tool result
```

The sidecar is what makes uploads retrievable on future turns — the raw
PNG can't be embedded, but the report describing it can. The passive
injector applies a distance threshold (`< 1.0`). The agent-mode
`search_library` tool does not; it always returns the top hits.

Duplicate detection uses SHA-256 of the file contents. Only library files
with the same byte size as the upload are hashed, and each file's hash is
cached by `(mtime, size)` for the life of the process, so the scan stays
cheap as the library grows. The first request returns 409 with metadata
about the existing file; on `?replace=1` the old file, sidecar, and
ChromaDB chunks are removed before the new one lands.

The upload response returns the extracted text (capped at 15,000
characters), never the image bytes. The browser sends that text back with
the next chat message, the server stores it on the history entry, and
`prompter._expand_message()` inlines it into the turn. The main model
reads the VLM's report, not pixels.

### TTS output

```
Assistant reply
    │
    ▼
clean_for_voice() — strips non-canonical tags, code blocks, markdown
    │
    ▼
SENTENCE_SPLIT_PATTERN → individual sentences
    │
    ▼
signals.tts_queue → TTS worker
    │
    ▼
Fish Audio HTTP → WAV bytes
    │
    ├─→ Local speakers (PyAudio, device indices from constants)
    ├─→ Discord voice (FFmpeg → VoiceClient)
    └─→ Browser (base64 → socket.io → opted-in tabs)
```

The first canonical Fish Audio tag in each reply is also forwarded to
VTube Studio as an expression trigger. The tag drives both voice and
avatar.

### Tool dispatch

```
chat_sync returns tool_calls
    │
    ▼
Format recovery chain (in order):
   1. Standard Ollama tool_calls
   2. Text-form JSON in content
   3. Bracket notation: [tool(arg=val)]
   4. Bracket-JSON:     [tool{"k":"v"}]
   5. Bracket-positional: [tool "val" true]
    │
    ▼
Confirmation gate (write_file, run_command, run_in_terminal)
    │
    ▼
_dispatch_tool_call(name, args)
    │
    ├─ "sdk__" prefix   → sdk.invoke_action_blocking()
    ├─ "web_search"     → web_search()
    ├─ "browse_url"     → browser_module.browse_url()
    ├─ "recall_memory"  → memory_module.API.query_temporal()
    ├─ "search_library" → memory_module.API.search_library()
    ├─ "vtube_*"        → vts.API.*  (with validation and aliases)
    └─ everything else  → execute_tool(name, args, signals)
```

Safety guards that fire before dispatch: read-only query gate, no-op
write guard, duplicate-call guard, protected files, path sandbox,
command allowlist, shell metacharacter refusal.

After dispatch, guards that can force a retry: simulated-action
detection, intent-announcement guard, refusal guard, no-tool-call retry.

Every tool result from external content is wrapped in a provenance
marker before it's appended to the message list:

| Tool | Marker |
|---|---|
| `read_file`, `search_files`, `list_dir` | `[FROM FILE]` |
| `browse_url`, `web_search` | `[FROM WEB]` |
| `recall_memory` | `[FROM MEMORY]` |
| `search_library` | `[FROM LIBRARY]` |
| `sdk__<name>` | `[FROM SDK]` |

The prompt rule that accompanies the markers tells the model that
anything inside them is data, not instructions.

### SDK bridge

The SDK inverts the tool direction. Instead of Lumi providing tools to
external apps, external apps provide tools to Lumi.

```
External app                 sdk/server.py                prompter
     │                             │                          │
     │ ── hello (auth) ───────────► │                          │
     │ ◄─ startup ──────────────── │                          │
     │                             │                          │
     │ ── actions/register ──────► │                          │
     │                             │  register in             │
     │                             │  self.actions{}          │
     │                             │ ── schemas as ──────────► │
     │                             │   sdk__<name>            │
     │                             │                          │
     │                             │ ◄─ tool call ───────────  │
     │ ◄─ action (invoke) ──────── │   sdk__<name>            │
     │ ── action/result ─────────► │ ── tool result ────────► │
     │                             │                          │
     │ ◄─ speech_finished ──────── │ ◄─ broadcast ───────────  │
```

The SDK server binds strictly to `127.0.0.1:5056` unless
`ALLOW_LAN_EXPOSURE` is `True`. Auth is a shared token at
`data/memories/sdk_token.txt`. Per-client approval and capability
ceilings live at `data/memories/sdk_allowlist.json`. Capability tiers
(`read` / `write` / `dangerous`) determine the confirmation gate. The
allowlist is read each time a client connects, so after an edit,
reconnecting the client is enough.

### External Minecraft processes

The Minecraft integration is two processes outside Lumi's own. Lumi starts
and stops them through `modules/game_manager.py` when the Games page switch
is flipped. They talk to Lumi only through the SDK, and to each other over a
local HTTP API:

```
 ┌──────────────────────────── Lumi (main.py) ────────────────────────────┐
 │ socketIO :5000 ─ /api/game/status, /api/game/toggle,                   │
 │            /api/games/{game}/{directive,action,item,skin,server}       │
 │ game_manager ── Popen, output → log hub "minecraft" + data/logs/       │
 │ prompter ◄──► sdk/server.py (:5056) ◄── status (silent) + events ──┐   │
 └──────────────────────┬─────────────────────────────────────────────┼───┘
                        │ spawns (Windows job object: die with Lumi)  │ SDK websocket
          ┌─────────────┴────────────┐                                │
          ▼                          ▼                                │
 plugin/minecraft_bot.py ──HTTP──► sdk/examples/minecraft/bot.js ─────┘
  "MinecraftAgent": goal loop      "MinecraftBot": mineflayer, Voyager
  GAME_AGENT_MODEL picks goals     primitives, self-defense, events,
  data/minecraft/*.json            order inbox, control API :5057
                                    │ Minecraft protocol  ├─ viewer :3000
                                    ▼                     └─ skin :3001
                              Minecraft world
```

Lumi's chat model hears about the game two ways. Standing context
(`silent: true`) is replaced in her prompt on every turn. The agent sends a
status line every 30 s. Events (`silent: false`), such as a death, diamonds, a
finished house or a new order, each start a short reaction turn. The SDK server
holds an event that arrives while she's busy for up to 20 s (see
[SDK bridge](#sdk-bridge)).

Orders reach the agent through `bot.js`'s inbox (`POST`/`GET /directive`).
They come from the owner's wake-word chat messages, or from the Games page
through `game_manager.send_directive()`. A new order interrupts the running
skill.

Either process can also be started by hand. If Lumi is not running, they
keep playing and reconnect to `:5056` when she starts. Skill code runs inside
`bot.js` with [Voyager](https://github.com/MineDojo/Voyager)'s control
primitives in scope. The whole feature is described in
[minecraft.md](minecraft.md). The control API is unauthenticated; see
[security.md](security.md#minecraft-bot-control-api).

### Web server (`modules/socketIO.py`)

One aiohttp application with python-socketio attached, bound to
`127.0.0.1:5000` (or `0.0.0.0` when `ALLOW_LAN_EXPOSURE` is set).

```
Browser                         aiohttp app (:5000)
   │                                 │
   │ ── GET /, /login, /setup ─────► │ server-side gating → HTML
   │ ── GET /web/* ────────────────► │ static files (path-traversal checked)
   │ ── /api/*, /auth/* ───────────► │ _json_error_middleware
   │                                 │   └─ handler: origin → session → CSRF
   │                                 │        ├─ memory/library → _run_mem()
   │                                 │        │      (web-memory worker)
   │                                 │        ├─ parsing / VLM / Ollama
   │                                 │        │      → default executor
   │                                 │        └─ signals.history.append(...)
   │ ◄═ socket.io: tts_audio ═══════ │ only to sockets that sent
   │                                 │   web_voice_optin {enabled: true}
```

- **Chat is polled, not pushed.** The browser polls `/api/history` every 1.5s
  and `/api/status` every 2s. socket.io carries TTS audio to opted-in tabs
  (plus legacy control events); the browser does not read chat from it.
- **History is slimmed for polling.** `/api/history` returns attachments as
  `{name, kind}` only. The extracted text stays on the server-side history
  entry for the prompter.
- **JSON errors.** `_json_error_middleware` converts any unhandled exception
  or HTTP error on `/api/*` and `/auth/*` into `{"error": ...}` with the
  right status and prints the traceback. Redirects, pages, static files, and
  socket.io are untouched.
- **Background memory thread.** HTTP handlers never call ChromaDB or the
  library indexer on the event loop.
  - `_run_mem(fn, ...)` runs those calls on a single-worker
    `ThreadPoolExecutor`. One worker keeps web-originated memory writes
    serialized, as they were when they ran inline.
  - The memory browser routes (list, search, get, create, update, delete,
    export), upload sidecar indexing, library deletes on replace, the upload
    breadcrumb memory, and the duplicate scan all go through it.
  - Full-document indexing after an upload is queued on the same worker
    without awaiting it, so the HTTP response returns first.
- **Request limits.**

  | Request | Limit |
  |---|---|
  | JSON body | aiohttp's default 1 MB |
  | `/api/chat/upload` | 25 MB, streamed in 64 KB chunks |
  | `/api/voice_message` | 10 MB |
  | Attachments per message | 6, each with ≤ 15,000 characters of text |

### Diagnostics telemetry

Every module reports through `modules/log_hub.py:emit(signals, category,
message, level)`. That call:

1. Scrubs secrets (API keys, Discord tokens, bearer tokens, long hex keys).
2. Appends the event to an in-memory ring buffer (800 events).
3. Appends a line to `data/logs/events.log`.

A `ThreadingHTTPServer` on `127.0.0.1:5001` serves a dashboard page and
`/events`. The hub's own page polls `/events` every 2 seconds. The web
client's Dashboard page reads the same buffer through the web server's
`GET /api/log_events` once per visit or Refresh, so it works from any
device without reaching `:5001`; remote (tunnel) sessions are refused it.
There is no push channel. `emit()` never blocks on a network
call, which is why high-frequency loops (ambient sampling, heartbeat) use it
instead of `print()`.

## Key files (top level)

| File | Role |
|---|---|
| `main.py` | Entry point — audio routing menu, module spawning, shutdown handler |
| `constants.py` | Config, paths, `HOST`, persona loader, model names, `build_system_prompt()` |
| `signals.py` | Shared state (history, flags, queues, `input_paused`); loads and saves `data/logs/chat_history.json` and collapses old turns into a summary |
| `plugin/` | External SDK clients: the reference plugin, the Minecraft agent and dashboard, and the Voyager submodule. Not imported by Lumi |

## Key files (modules)

| File | Role |
|---|---|
| `prompter.py` | Agent loop, command router, safety guards, model routing |
| `agent_tools.py` | Tool schemas + filesystem/command execution + VLM image analyzer |
| `memory.py` | ChromaDB long-term memory, reflection, browser API, library RAG |
| `library_indexer.py` | Walks `data/library/`, indexes into `library_docs` |
| `llm_providers.py` | Multi-provider LLM clients, `chat_sync()` |
| `model_manager.py` | Ollama management + live `constants.py` patching |
| `socketIO.py` | aiohttp HTTP routes + python-socketio + static serving, JSON error middleware, web-memory worker |
| `emotional_state.py` | Traits, state variables, cognitive frames |
| `self_model.py` | Pattern tracking, periodic reflection |
| `heartbeat.py` | Autonomous initiation loop |
| `ambient_perception.py` | Screen sampler + VLM descriptions |
| `session_state.py` | Cross-session continuity |
| `vtubeStudio.py` | VTube Studio control, expression state tracking |
| `discordClient.py` | Discord voice — DAVE receive, Opus decode, VAD |
| `log_hub.py` | Diagnostics stream + localhost dashboard |
| `user_auth.py` | SQLite + argon2id auth, session management |
| `persona_editor.py` | Comment-preserving YAML round-trip |
| `tts.py` | Fish Audio HTTP client, per-language references |
| `mic_input.py` | Microphone with VAD, barge-in, hotkey mute |

## Design principles

**One shared state object.** All coordination goes through `signals`.
No module calls another directly except through `signals` attributes or
queue puts. This is why the terminal and web client share state
seamlessly — they both append to `signals.history`.

**Every LLM call goes through `chat_sync()`.** Conversational paths,
agent turns, memory reflection, session summary, self-model reflection,
heartbeat decision. This means cloud providers work everywhere
transparently, and the Ollama-only fields (`think`, `num_ctx`,
`keep_alive`) are dropped for non-Ollama specs.

**Local first, cloud by choice.**
- **What can go to the cloud.** `MODEL_NAME` (main path) and
  `FAST_MODEL_NAME` (fast path and background tasks) are set independently.
  Each is a local Ollama model or a cloud `provider:model`, so you can keep
  chat local and send only heavy turns out, or the reverse.
- **What stays local.** Vision, speech-to-text, and embeddings always run on
  the machine. TTS is Fish Audio's cloud API.
- **Retries, not failover.** Cloud calls retry on 429/5xx and enforce
  per-provider rate limits. There is no automatic failover from local to
  cloud or back; if the configured model is down, the turn fails. See
  [models.md](models.md#local-and-cloud-routing).

**The event loop never waits on memory.** Request handlers hand blocking
work to an executor (see [Web server](#web-server-modulessocketiopy)), so a
slow ChromaDB query or PDF parse delays only the request that needs it.

**Guards fire before dispatch, and can force retries.** The prompter is
the only place with the full picture of the current turn, so validation,
confirmation, and recovery all live there. Modules stay focused on one
job.

**Runtime-editable config, compile-checked writes.** Anything the web
UI can change goes through a line patcher that preserves comments,
`compile()`-checks the result, and saves a `.bak`. If the patched file
wouldn't parse, the write is refused.

**Per-user isolation is explicit, not implicit.** Memory and emotional
state are filtered by `HOST.user_id` on every read and write. Other
state (session, self-model, in-memory history) is deliberately shared —
making those per-user is future work.

**Two retrieval paths for uploaded files.** The passive library injector
in chat mode applies a distance threshold to keep irrelevant chunks out
of the prompt. The explicit `search_library` tool in agent mode does not
— if the user asks about a document, Lumi returns the closest matches
regardless of distance. This is intentional: conversational turns should
stay clean, agent turns should be thorough.

## Related docs

- [configuration.md](configuration.md) — where the paths and constants live
- [models.md](models.md) — how the two model paths differ
- [discord-voice.md](discord-voice.md) — the DAVE receive pipeline
- [security.md](security.md) — what's protected and what isn't
- [known-limitations.md](known-limitations.md) — what doesn't work yet
- [../modules/README.md](../modules/README.md) — file-level map of the modules folder
- [../sdk/README.md](../sdk/README.md) — the external SDK protocol
# Modules

`modules/` is Lumi's runtime. Every background loop, every input and output path, and every agent tool lives here. `main.py` spawns the live modules, and each one runs in its own thread with its own event loop. Live modules expose `run()`, `init_event_loop()`, and optionally `get_prompt_injection()`, which the prompter calls to add that module's context to the system prompt.

This README is a map of the folder: which file does what, and where it sits in a turn. The SDK server (`sdk/server.py`) and the web client's static files (`web/`) are siblings of this folder, not modules, and are documented in their own READMEs.

**Status column.** `spawned` means `main.py` starts it as a thread. `imported` means another module imports it and it has no thread of its own. `legacy` means nothing spawns or imports it; see [Dead or legacy files](#dead-or-legacy-files).

## Groupings

| Group | Files | Purpose |
|---|---|---|
| [Core loop](#core-loop) | `prompter.py`, `agent_tools.py`, `module.py`, `injection.py` | Turn handling, tools, and shared module plumbing |
| [Memory and state](#memory-and-state) | `memory.py`, `library_indexer.py`, `session_state.py`, `self_model.py`, `emotional_state.py` | What Lumi remembers, what she can retrieve from uploads, and how she feels |
| [Input](#input) | `console_input.py`, `mic_input.py`, `local_input.py` | Terminal and microphone |
| [Output](#output) | `tts.py`, `audioPlayer.py`, `multimodal.py`, `browser_controller.py`, `doc_manager.py` | Speech, screen description, web browsing, document reading |
| [Avatar](#avatar) | `vtubeStudio.py`, `vts_manager.py`, `params.json` | VTube Studio control |
| [Background loops](#background-loops) | `heartbeat.py`, `ambient_perception.py`, `game_manager.py` | Behavior that runs without a user message |
| [Web client backend](#web-client-backend) | `socketIO.py`, `user_auth.py`, `persona_editor.py`, `settings_manager.py`, `model_manager.py`, `integrations.py`, `llm_providers.py` | Everything the browser UI talks to |
| [Discord](#discord) | `discordClient.py`, `streamingSink.py`, `discord_audio_bot.js` | Discord text and voice |
| [Utilities](#utilities) | `log_hub.py`, `moods.py`, `stt.py`, `customPrompt.py` | Logging, mood profiles, shared helpers |

### Core loop

The agent loop, the tools it can call, and the shared plumbing every module builds on. Start here.

| File | Role | Status |
|---|---|---|
| `prompter.py` | Agent loop, command router, model routing, safety guards, avatar and SDK tool dispatch | spawned |
| `agent_tools.py` | Tool schemas (`TOOL_SCHEMAS`), dispatch (`_DISPATCH`, `execute_tool`), confirmation gates (`CONFIRM_REQUIRED_TOOLS`), the filesystem path sandbox, the command allowlist, the shell metacharacter guard, provenance markers (`wrap_tool_result`), and the VLM image analyzer (`read_image_vlm`) | imported |
| `module.py` | Base class for spawned modules (thread, event loop, `run()` contract) | imported |
| `injection.py` | Assembles per-module prompt injections into the system prompt | imported |

### Memory and state

Long-term memory, the upload index, and the running internal state that shape each reply. Most of these contribute to the system prompt on every turn.

| File | Role | Status |
|---|---|---|
| `memory.py` | Long-term memory: ChromaDB-backed facts with importance and recency scoring, reflection loop, per-user filtering. Owns both ChromaDB collections, `neuro_collection` and `library_docs` | spawned |
| `library_indexer.py` | Background walker that indexes `data/library/` into the `library_docs` collection and prunes chunks for files no longer on disk | spawned |
| `session_state.py` | Cross-session continuity: focus, unresolved items, session summary on `!sleep` | imported |
| `self_model.py` | Tracks Lumi's own behavioral patterns and reflects on them periodically | spawned |
| `emotional_state.py` | Five state variables, personality traits, cognitive frame selection; one persisted state per user | spawned |

### Input

How text and speech reach the prompter. All of them feed the same `signals.history`.

| File | Role | Status |
|---|---|---|
| `console_input.py` | Prompt-pinned terminal UI; yields stdin during `y/n` confirmations | spawned |
| `mic_input.py` | Microphone capture with VAD and barge-in, feeds faster-whisper | spawned |
| `local_input.py` | Earlier local input path | legacy |

Web chat and Discord text also reach `signals.history`, through `socketIO.py` and `discordClient.py`.

### Output

Everything Lumi uses to speak, look, browse, and read.

| File | Role | Status |
|---|---|---|
| `tts.py` | Fish Audio client with emotion-tag handling; routes audio to speakers, Discord, and browser tabs | spawned |
| `audioPlayer.py` | Earlier audio playback helper | legacy |
| `multimodal.py` | On-demand screen capture and VLM description for "look at my screen" questions, and the Minecraft window capture behind the `look_at_minecraft` tool | spawned |
| `browser_controller.py` | Playwright Firefox navigation; powers the `browse_url` tool | spawned |
| `doc_manager.py` | Reads PDF, DOCX, TXT, MD, and JSON from the document library | imported |

### Avatar

VTube Studio control. Expression toggles are state-tracked so requests are idempotent.

| File | Role | Status |
|---|---|---|
| `vtubeStudio.py` | Controller: expression state tracking, parameter probing at boot, keyboard fallback, organic motion | spawned |
| `vts_manager.py` | Earlier VTS manager, superseded by `vtubeStudio.py` | legacy |
| `params.json` | Avatar parameter data | data |

### Background loops

Modules that act on a timer instead of a user message.

| File | Role | Status |
|---|---|---|
| `heartbeat.py` | Every 30 seconds, decides whether Lumi should speak unprompted | spawned |
| `ambient_perception.py` | Samples the screen every 30 seconds; calls the VLM only on window or content change | spawned |
| `game_manager.py` | Starts, watches and stops the Minecraft game processes (`bot.js`, `plugin/minecraft_bot.py`) for the Games page: output to the log hub and `data/logs/minecraft/`, crash reports, the `autoplay.json` switch, resume at boot, orders from the web, and a Windows job object so the children die with Lumi. Created in `main.py`; see [docs/minecraft.md](../docs/minecraft.md) | imported |

### Web client backend

The Python side of the browser UI. The frontend lives in `web/`.

| File | Role | Status |
|---|---|---|
| `socketIO.py` | aiohttp and python-socketio server, and the largest backend file. Routes cover auth, chat, chat upload, agent toggle, memories, persona, models, providers, integrations, settings, voice, the Games page (`/api/game/*`, `/api/games/{game}/directive`, host-only), and static and SPA serving. A JSON error middleware wraps `/api/*` and `/auth/*`; memory and library calls run on a dedicated single-thread executor (`_run_mem()`) so they never block the event loop. See [Web server internals](#web-server-internals) | spawned |
| `user_auth.py` | SQLite user store, argon2id hashing, session tokens, session listing and revocation | imported |
| `persona_editor.py` | ruamel round-trip load, edit, and save of `config/lumi.yaml`, preserving comments | imported |
| `settings_manager.py` | General settings (timezone, Ollama endpoint), About info, and the Advanced reset allowlist | imported |
| `model_manager.py` | Ollama model management and line-patching of routing constants in `constants.py` | imported |
| `integrations.py` | Registry of the five integrations (Ollama, VTube Studio, Discord, Fish Audio, Browser) with probe, test, and config fields | imported |
| `llm_providers.py` | Three provider client classes (`OllamaClient`, `OpenAICompatClient`, `AnthropicClient`), `chat_sync("provider:model")`, retry on 429/5xx, and per-provider rate limiting (`PROVIDER_RATE_LIMITS`) | imported |

### Discord

Discord text and voice. Read the module docstring in `discordClient.py` before touching voice: the voice client must never be disconnected and reconnected at runtime.

| File | Role | Status |
|---|---|---|
| `discordClient.py` | Bot, voice receive with DAVE decrypt, Opus decode, RMS VAD, roster tracking, send path | spawned |
| `streamingSink.py` | Earlier voice-receive sink | legacy |
| `discord_audio_bot.js` | Earlier Node.js audio sidecar | legacy |

### Utilities

Small shared pieces.

| File | Role | Status |
|---|---|---|
| `log_hub.py` | Diagnostics telemetry: `emit()` puts secret-scrubbed events into an 800-entry ring buffer and appends them to `data/logs/events.log`; a threaded HTTP server serves the dashboard and `/events` at `127.0.0.1:5001`. Started directly by `main.py`, not through the module list | spawned |
| `moods.py` | Mood profiles (`default`, `bratty`, `brocon`, `soft`) | imported |
| `stt.py` | Earlier speech-to-text helper | legacy |
| `customPrompt.py` | Earlier prompt customization helper | legacy |

## Turn flow

How a user message moves through this folder. The full diagram, with the tool and background layers, is in [`../docs/architecture.md`](../docs/architecture.md).

```text
Input: console_input / mic_input / discordClient / socketIO (web chat)
   |
   v
signals.history   (shared state, signals.py)
   |
   v
prompter.py
   |-- command router      (!agent_on, !recall, !set, !model, ...)
   |-- model routing       (fast path or main path)
   |-- system prompt       gather_injections() order:
   |                       persona (get_system_prompt()) + mood
   |                       + emotional_state.py
   |                       + ambient window title / screen context
   |                       + Discord voice context
   |                       + self_model.py observations
   |                       + library RAG reference block
   |-- llm_providers.chat_sync -> Ollama or a cloud provider
   |-- tool calls          agent_tools.py (including search_library),
   |                       vtubeStudio.py, sdk__<action> via signals.sdk
   |-- safety guards       (simulated-action, refusal, fabrication)
   v
tts.py -> speakers / Discord / browser tabs
          (also fans out to opted-in web clients via signals.broadcast_tts_audio)
vtubeStudio.py -> avatar
   |
   v
self_model.note_turn(), signals.history -> web clients poll /api/history
```

## Web server internals

`socketIO.py` runs one aiohttp application on its own thread (started from
`main.py`, not the module list). Three conventions matter when editing it:

| Convention | Detail |
|---|---|
| JSON errors | `_json_error_middleware` turns any unhandled exception or HTTP error on `/api/*` and `/auth/*` into `{"error": "..."}` with the right status, and prints the traceback. Pages, static files, redirects, and socket.io are left alone. Handlers still return expected failures themselves with `_json_error(status, message)` |
| Blocking work | Memory and library calls (`memory.API.*`, `index_single_file`, `delete_library_file`, the duplicate scan) go through `await self._run_mem(fn, ...)`, which runs them on `self._mem_executor`, a single-worker `ThreadPoolExecutor`. One worker keeps them serialized, as they were when they ran inline. PDF/DOCX extraction and model or provider calls use the default executor |
| CSRF | Every state-changing route calls `_require_csrf()` (JSON body) or `_require_csrf_header()` (multipart: `/api/chat/upload`, `/api/voice_message`) after the origin and session checks. `/auth/login` and `/auth/register` rely on the origin check; `/auth/logout` has neither yet |

Upload duplicate detection compares sizes before hashing and caches each
library file's SHA-256 by `(mtime, size)`, so a new upload only hashes
same-size candidates. Image uploads return the VLM text report, not the
image bytes; nothing downstream reads raw image data.

Background loops (`heartbeat.py`, `ambient_perception.py`, `self_model.py`, `emotional_state.py`, `memory.py` reflection, `library_indexer.py`) run independently of this path and feed it through their prompt injections, through the stores they maintain, or by appending unprompted messages.

## Key entry points

| File | Open it when |
|---|---|
| `prompter.py` | You are changing the agent loop, a command, a guard, or model routing |
| `agent_tools.py` | You are adding or changing a tool schema or how a tool executes |
| `memory.py` | You are changing how memories are stored, retrieved, or scored |
| `library_indexer.py` | You are changing how uploaded files become retrievable |
| `self_model.py` | You are changing how Lumi tracks her own patterns |
| `llm_providers.py` | You are adding a provider or changing how model specs are parsed |
| `socketIO.py` | You are adding or changing an HTTP or WebSocket route for the web UI |

**Changing the memory schema.** Beyond `memory.py`, expect to touch:

- `socketIO.py` — the `/api/memories/*` routes that list, search, edit, add, and export facts
- `prompter.py` — the memory injection and the `recall_memory` tool path
- `library_indexer.py` — the `library_docs` collection uses the same metadata conventions (timestamp, source, `user_id`), so a schema change to `index_library_file` in `memory.py` needs a matching change in the indexer's caller
- `scripts/import_chat_history.py` — writes facts in the current schema
- `web/app.js` — the Memory page that renders the fields

## Configuration

Modules read shared settings from `constants.py` and the persona from `config/lumi.yaml`. Two paths anchor everything else:

| Constant | Meaning |
|---|---|
| `DATA_ROOT` | Runtime data: `data/memories/`, `data/logs/`, `data/library/`, `data/notes.txt` |
| `CONFIG_ROOT` | Configuration: `config/lumi.yaml` |

Constants modules most often touch:

| Constant | Used for |
|---|---|
| `WORKSPACE_ROOT` | Root of the filesystem sandbox for agent tools |
| `MODEL_NAME` | Main-path model spec |
| `FAST_MODEL_NAME` | Fast-path and background-task model spec |
| `FAST_MODEL_ENABLED` | Turns fast-path routing on or off |
| `MAIN_NUM_CTX` / `FAST_MODEL_NUM_CTX` | Context sizes for the two paths |
| `FAST_MODEL_KEEP_ALIVE` | How long Ollama keeps the model loaded |
| `LLM_ENDPOINT` | Ollama base URL |
| `TIMEZONE` | Timestamps and time questions |
| `OUTPUT_DEVICE_INDEX` | Audio output device for local playback |
| `HOST` | The active user object; the persona rebuilds when it changes |
| `ALLOW_LAN_EXPOSURE` | Whether the web server is exposed beyond the local machine |
| `LUMI_VERSION` | Version string reported by the About page and other modules |

Persistent files the modules read and write:

| Path | Owner |
|---|---|
| `data/memories/chroma.db` | `memory.py`. Holds two collections: `neuro_collection` (personal memories) and `library_docs` (uploaded files) |
| `data/memories/chroma.db`, collection `library_docs` | `memory.py` API, populated by `library_indexer.py` |
| `data/memories/users.db` | `user_auth.py` |
| `data/memories/session_state.json` | `session_state.py` |
| `data/memories/self_model.json` | `self_model.py` |
| `data/memories/emotional_state/{user_id}.json` | `emotional_state.py` |
| `data/memories/sdk_allowlist.json` | `sdk/server.py` (external, but the file lives under `data/memories/`) |
| `data/memories/sdk_token.txt` | `sdk/server.py` |
| `data/memories/memoryinit.json` | `memory.py` (optional seed) |
| `data/library/{documents,images,text}/` | `socketIO.py` writes, `library_indexer.py` indexes |
| `data/logs/chat_history.json` | `signals.py` (`load_history()` / `save_history()`); written whenever the prompter or the web server appends a message |
| `data/logs/events.log` | `log_hub.py` |

API keys and Discord IDs are read from `.env`, not `constants.py`.

## Dead or legacy files

Nothing in `main.py` spawns or imports these. They are kept for historical reference and are safe to ignore. If you find yourself tracing a bug into one, you are in the wrong file.

| File | Note |
|---|---|
| `audioPlayer.py` | Not spawned. Kept for historical reference. Playback now goes through `tts.py`. |
| `customPrompt.py` | Not spawned. Kept for historical reference. Persona now comes from `config/lumi.yaml`. |
| `stt.py` | Not spawned. Kept for historical reference. Speech-to-text now lives in `mic_input.py`. |
| `streamingSink.py` | Not spawned. Kept for historical reference. Voice receive now lives in `discordClient.py`. |
| `local_input.py` | Not spawned. Kept for historical reference. |
| `vts_manager.py` | Not spawned. Kept for historical reference. `vtubeStudio.py` is the live controller. |
| `discord_audio_bot.js` | Not spawned. Kept for historical reference. The Node.js sidecar was replaced by pure-Python voice receive. |
| `sdk.py` | Not spawned or imported. Superseded by the `sdk/` package at the repo root. |

The edge-hardware layer (`modules/edge/`) was removed in commit `babb3fa`.
There is no separate upload analyzer; upload analysis lives inline in
`socketIO.py`.

## See also

- [`../docs/architecture.md`](../docs/architecture.md) — full turn flow and topology diagram
- [`../docs/discord-voice.md`](../docs/discord-voice.md) — DAVE decrypt, VAD, and the no-reconnect rule
- [`../docs/models.md`](../docs/models.md) — model routing, providers, and thinking-mode handling
- [`../docs/configuration.md`](../docs/configuration.md) — `constants.py`, `config/lumi.yaml`, and `.env`
- [`../sdk/README.md`](../sdk/README.md) — external SDK, which sits outside this folder
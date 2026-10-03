# Modules

`modules/` is Lumi's runtime. Every background loop, every input and output path, and every agent tool lives here. `main.py` spawns the live modules, and each one runs in its own thread with its own event loop. Live modules expose `run()`, `init_event_loop()`, and optionally `get_prompt_injection()`, which the prompter calls to add that module's context to the system prompt.

This README is a map of the folder: which file does what, and where it sits in a turn. The SDK server (`sdk/server.py`) and the web client's static files (`web/`) are siblings of this folder, not modules, and are documented in their own READMEs.

<!-- VERIFY: the Status column throughout this file (spawned / imported / legacy) was inferred, not read from main.py. Confirm each row against main.py before publishing. -->

**Status column.** `spawned` means `main.py` starts it as a thread. `imported` means another module imports it and it has no thread of its own. `legacy` means nothing spawns or imports it; see [Dead or legacy files](#dead-or-legacy-files).

## Groupings

| Group | Files | Purpose |
|---|---|---|
| [Core loop](#core-loop) | `prompter.py`, `agent_tools.py`, `module.py`, `injection.py` | Turn handling, tools, and shared module plumbing |
| [Memory and state](#memory-and-state) | `memory.py`, `session_state.py`, `self_model.py`, `emotional_state.py` | What Lumi remembers and how she feels |
| [Input](#input) | `console_input.py`, `mic_input.py`, `local_input.py` | Terminal and microphone |
| [Output](#output) | `tts.py`, `audioPlayer.py`, `multimodal.py`, `doc_manager.py` | Speech, screen description, document reading |
| [Avatar](#avatar) | `vtubeStudio.py`, `vts_manager.py`, `params.json` | VTube Studio control |
| [Background loops](#background-loops) | `heartbeat.py`, `ambient_perception.py` | Behavior that runs without a user message |
| [Web client backend](#web-client-backend) | `socketIO.py`, `user_auth.py`, `persona_editor.py`, `settings_manager.py`, `model_manager.py`, `integrations.py`, `llm_providers.py` | Everything the browser UI talks to |
| [Discord](#discord) | `discordClient.py`, `streamingSink.py`, `discord_audio_bot.js` | Discord text and voice |
| [Edge optimization](#edge-optimization) | `edge/manager.py`, `edge/core/hardware_detector.py`, `edge/adapters/...` | Hardware abstraction layer for ARM/NPU edge deployment (Surface Pro 11) |
| [Utilities](#utilities) | `log_hub.py`, `moods.py`, `stt.py`, `customPrompt.py` | Logging, mood profiles, shared helpers |

### Core loop

The agent loop, the tools it can call, and the shared plumbing every module builds on. Start here.

| File | Role | Status |
|---|---|---|
| `prompter.py` | Agent loop, command router, model routing, safety guards, avatar and SDK tool dispatch | spawned |
| `agent_tools.py` | Tool schemas plus filesystem and command execution, including the path sandbox and confirmation gates | imported |
| `module.py` | Base class for spawned modules (thread, event loop, `run()` contract) | imported |
| `injection.py` | Assembles per-module prompt injections into the system prompt | imported |

<!-- VERIFY: roles of module.py and injection.py were inferred from the run()/init_event_loop()/get_prompt_injection() contract. -->

### Memory and state

Long-term memory and the running internal state that shape each reply. Each of these contributes to the system prompt on every turn.

| File | Role | Status |
|---|---|---|
| `memory.py` | Long-term memory: ChromaDB-backed facts with importance and recency scoring, reflection loop, per-user filtering | spawned |
| `session_state.py` | Cross-session continuity: focus, unresolved items, session summary on `!sleep` | imported |
| `self_model.py` | Tracks Lumi's own behavioral patterns and reflects on them periodically | spawned |
| `emotional_state.py` | Five state variables, personality traits, cognitive frame selection; one persisted state per user | spawned |

### Input

How text and speech reach the prompter. Both feed the same `signals.history`.

| File | Role | Status |
|---|---|---|
| `console_input.py` | Prompt-pinned terminal UI; yields stdin during `y/n` confirmations | spawned |
| `mic_input.py` | Microphone capture with VAD and barge-in, feeds faster-whisper | spawned |
| `local_input.py` | Earlier local input path | legacy |

### Output

Everything Lumi uses to speak, look, and read.

| File | Role | Status |
|---|---|---|
| `tts.py` | Fish Audio client with emotion-tag handling; routes audio to speakers, Discord, and browser tabs | spawned |
| `audioPlayer.py` | Earlier audio playback helper | legacy |
| `multimodal.py` | On-demand screen capture and VLM description for "look at my screen" questions | spawned |
| `doc_manager.py` | Reads PDF, DOCX, TXT, MD, and JSON from the document library | imported |

### Avatar

VTube Studio control. Expression toggles are state-tracked so requests are idempotent.

| File | Role | Status |
|---|---|---|
| `vtubeStudio.py` | Controller: expression state tracking, parameter probing, keyboard fallback, organic motion | spawned |
| `vts_manager.py` | Earlier VTS manager, superseded by `vtubeStudio.py` | legacy |
| `params.json` | Avatar parameter data used by the controller | data |

### Background loops

Modules that act on a timer instead of a user message.

| File | Role | Status |
|---|---|---|
| `heartbeat.py` | Every 30 seconds, decides whether Lumi should speak unprompted | spawned |
| `ambient_perception.py` | Samples the screen every 30 seconds; calls the VLM only on window or content change | spawned |

### Web client backend

The Python side of the browser UI. The frontend lives in `web/`.

| File | Role | Status |
|---|---|---|
| `socketIO.py` | aiohttp and python-socketio server: auth, chat, memory, persona, models, providers, integrations, settings, and voice routes | spawned |
| `user_auth.py` | SQLite user store, argon2id hashing, session tokens, session listing and revocation | imported |
| `persona_editor.py` | Comment-preserving load, edit, and save of `config/lumi.yaml` | imported |
| `settings_manager.py` | Timezone, Ollama endpoint, About info, and the Advanced reset allowlist | imported |
| `model_manager.py` | Ollama model management and hot-patching of routing constants in `constants.py` | imported |
| `integrations.py` | Registry of the five integrations with probe, test, and config fields | imported |
| `llm_providers.py` | Provider clients (Ollama, OpenAI-compatible, Anthropic) and `chat_sync("provider:model")` | imported |

### Discord

Discord text and voice. Read the module docstring in `discordClient.py` before touching voice: the voice client must never be disconnected and reconnected at runtime.

| File | Role | Status |
|---|---|---|
| `discordClient.py` | Bot, voice receive with DAVE decrypt, Opus decode, RMS VAD, roster tracking, send path | spawned |
| `streamingSink.py` | Earlier voice-receive sink | legacy |
| `discord_audio_bot.js` | Earlier Node.js audio sidecar | legacy |

### Edge optimization

Hardware abstraction layer (HAL) and ONNX-backed adapters for running on ARM edge devices (such as the Surface Pro 11) without heavy x86 dependencies or excessive battery drain.

| File | Role | Status |
|---|---|---|
| `edge/manager.py` | Coordinates ARM detection, configuration loading, and edge adapter routing | imported |
| `edge/core/hardware_detector.py` | Checks system architecture (`arm64`/`aarch64`) and resolves optimal DirectML/CPU execution providers | imported |
| `edge/adapters/onnx_embeddings.py` | ONNX-backed vector embeddings optimized for NPU/GPU offloading via DirectML | imported |
| `edge/adapters/light_whisper.py` | ONNX-backed Whisper STT adapter replacing heavy CTranslate2 dependencies on Windows ARM64 | imported |
| `edge/configs/edge_overrides.json` | Resource thresholds, context limits (2048 tokens), and background throttle profiles | data |

### Utilities

Small shared pieces.

| File | Role | Status |
|---|---|---|
| `log_hub.py` | Structured diagnostics stream and the dashboard at `127.0.0.1:5001` | spawned |
| `moods.py` | Mood profiles (`default`, `bratty`, `brocon`, `soft`) | imported |
| `stt.py` | Earlier speech-to-text helper | legacy |
| `customPrompt.py` | Earlier prompt customization helper | legacy |

<!-- VERIFY: the browser controller (Playwright, used by browse_url) is spawned by main.py but is not in the file list I was given. Add its file to the Output or Core loop group. -->

## Turn flow

How a user message moves through this folder. The full diagram, with the tool and background layers, is in [`../docs/architecture.md`](../docs/architecture.md).

```text
Input: console_input / mic_input / discordClient / socketIO
   |
   v
signals.history   (shared state, signals.py)
   |
   v
prompter.py
   |-- command router      (!agent_on, !recall, !set, !model, ...)
   |-- model routing       (fast path or main path)
   |-- system prompt       persona + memory.py + moods.py + emotional_state.py
   |                       + ambient_perception.py + self_model.py
   |                       + session_state.py + Discord roster
   |-- llm_providers.chat_sync -> Ollama or a cloud provider
   |-- tool calls          agent_tools.py, vtubeStudio.py, SDK actions
   |-- safety guards       (simulated-action, refusal, fabrication)
   v
tts.py -> speakers / Discord / browser tabs;  vtubeStudio.py -> avatar
   |
   v
self_model.note_turn(), signals.history -> web clients poll /api/history
```

Background loops (`heartbeat.py`, `ambient_perception.py`, `self_model.py`, `emotional_state.py`, `memory.py` reflection) run independently of this path and feed it through their prompt injections or by appending unprompted messages.

## Key entry points

| File | Open it when |
|---|---|
| `prompter.py` | You are changing the agent loop, a command, a guard, or model routing |
| `agent_tools.py` | You are adding or changing a tool schema or how a tool executes |
| `memory.py` | You are changing how memories are stored, retrieved, or scored |
| `llm_providers.py` | You are adding a provider or changing how model specs are parsed |
| `socketIO.py` | You are adding or changing an HTTP or WebSocket route for the web UI |

**Changing the memory schema.** Beyond `memory.py`, expect to touch:

- `socketIO.py` — the `/api/memories/*` routes that list, search, edit, add, and export facts
- `prompter.py` — the memory injection and the `recall_memory` tool path
- `scripts/import_chat_history.py` — writes facts in the current schema
- `web/app.js` — the Memory page that renders the fields

## Configuration

Modules read shared settings from `constants.py` and the persona from `config/lumi.yaml`. Two paths anchor everything else:

<!-- VERIFY: DATA_ROOT and CONFIG_ROOT semantics, and the constants list, against constants.py. -->

| Constant | Meaning |
|---|---|
| `DATA_ROOT` | Runtime data: `data/memories/`, `data/logs/`, `data/notes.txt` |
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

Persistent files the modules read and write:

| Path | Owner |
|---|---|
| `data/memories/chroma.db` | `memory.py` |
| `data/memories/users.db` | `user_auth.py` |
| `data/memories/session_state.json` | `session_state.py` |
| `data/memories/self_model.json` | `self_model.py` |
| `data/memories/emotional_state/{user_id}.json` | `emotional_state.py` |
| `data/logs/chat_history.json` | `prompter.py` |
| `data/logs/events.log` | `log_hub.py` |

API keys and Discord IDs are read from `.env`, not `constants.py`.

## Dead or legacy files

Nothing in `main.py` spawns or imports these. They are kept for historical reference and are safe to ignore. If you find yourself tracing a bug into one, you are in the wrong file.

<!-- VERIFY: each one-line description below was inferred from the filename. -->

| File | Note |
|---|---|
| `audioPlayer.py` | Not spawned. Kept for historical reference. Playback now goes through `tts.py`. |
| `customPrompt.py` | Not spawned. Kept for historical reference. Persona now comes from `config/lumi.yaml`. |
| `stt.py` | Not spawned. Kept for historical reference. Speech-to-text now lives in `mic_input.py`. |
| `streamingSink.py` | Not spawned. Kept for historical reference. Voice receive now lives in `discordClient.py`. |
| `local_input.py` | Not spawned. Kept for historical reference. |
| `vts_manager.py` | Not spawned. Kept for historical reference. `vtubeStudio.py` is the live controller. |
| `discord_audio_bot.js` | Not spawned. Kept for historical reference. The Node.js sidecar was replaced by pure-Python voice receive. |
| `sdk.py` (repo root) | Not spawned. Superseded by the `sdk/` package. |

## See also

- [`../docs/architecture.md`](../docs/architecture.md) — full turn flow and topology diagram
- [`../docs/discord-voice.md`](../docs/discord-voice.md) — DAVE decrypt, VAD, and the no-reconnect rule
- [`../docs/models.md`](../docs/models.md) — model routing, providers, and thinking-mode handling
- [`../docs/configuration.md`](../docs/configuration.md) — `constants.py`, `config/lumi.yaml`, and `.env`
- [`../sdk/README.md`](../sdk/README.md) — external SDK, which sits outside this folder
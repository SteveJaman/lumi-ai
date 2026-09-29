# Architecture

How a message travels through Lumi, where each module sits, and why the
system is shaped this way.

The design goal is architectural parity with Neuro-sama: continuous
operation, internal state, autonomous decisions. The system is a set of
independent modules coordinated through one shared state object
(`signals`), with the prompter as the central hub for anything that
involves the LLM.

## Turn flow

Every input — terminal, microphone, Discord, web chat — lands in the same
place: `signals.history` as a user message, with `signals.new_message`
flipped to `True`. The prompter's run loop picks it up on its next tick
(50ms).

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
    │     │    - main: 16K ctx, think=true
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
    │     │    + SDK [EXTERNAL CONTEXT] (if clients connected)
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
          │     │    → web_search / browse_url / recall_memory special-cased
          │     │
          │     ├─ Append tool result as role: "tool"
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
`/api/history` poll (1.5s).

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
┌──────▼──────┐         ┌───────────▼────────────┐   ┌──────────▼────────┐
│ Command     │         │ Prompter               │   │ Avatar commands   │
│ router      │         │ (conversational or     │   │ (VTS via          │
│             │         │  agent turn)           │   │  vtubeStudio.py)  │
└─────────────┘         └───────────────┬────────┘   └───────────────────┘
                                        │
                            ┌───────────▼────────────┐
                            │  chat_sync()           │
                            │  → Ollama (local)      │
                            │  → cloud providers     │
                            └───────────┬────────────┘
                                        │
       ┌────────────────┬───────────────┼────────────┬───────────────┐
       │                │               │            │               │
   ┌───▼───┐     ┌──────▼──────┐  ┌─────▼─────┐ ┌────▼────┐ ┌────────▼─────┐
   │ Tools │     │ Memory      │  │ TTS       │ │ SDK     │ │ Self-model   │
   │ FS +  │     │ injection   │  │ Fish      │ │ fan-out │ │ note_turn    │
   │ shell │     │ + recall    │  │ Audio     │ │         │ │              │
   └───────┘     └─────────────┘  └───────────┘ └─────────┘ └──────────────┘
                                        │
                          ┌─────────────┴─────────────┐
                          │  output routing           │
                          │  → local speakers         │
                          │  → Discord voice          │
                          │  → browser (TTS playback) │
                          └───────────────────────────┘

Background loops (independent threads):
  heartbeat (30s)      · emotional_state decay (30s)
  ambient_perception   · self_model (60s/600s)
  memory reflection    · discord client
  log hub              · SDK server
```

## Background modules

Each runs in its own thread with its own event loop, spawned by
`main.py`. They expose `run()` and optionally `get_prompt_injection()`.

| Module | Trigger | What it does |
|---|---|---|
| `heartbeat.py` | 30s tick | Decides whether to speak unprompted. Gates: not busy, user idle 5+ min, cooldown 10+ min. |
| `ambient_perception.py` | 30s sample | Screen hash comparison. VLM call only on window change or hash delta > 14 bits, rate-limited to once per 90s. |
| `emotional_state.py` | 30s decay | Exponential decay toward baselines. Boredom ticks up during idle. |
| `self_model.py` | 60s snapshot / 600s reflect | Records turn stats and emotional snapshots. Reflects every 10 min if ≥5 turns. |
| `memory.py` | every 10 messages | Reflection pass extracts facts from recent history. Routes through `chat_sync(FAST_MODEL_NAME)`. |
| `discordClient.py` | continuous | Voice receive (DAVE decrypt + Opus decode + VAD) and send (Fish Audio → FFmpeg). |
| `log_hub.py` | continuous | Structured diagnostics stream at `127.0.0.1:5001`. |
| `sdk/server.py` | continuous | Websocket server at `127.0.0.1:5056` for external plugins. |

## Data flow between subsystems

### Memory

```
Chat turns → reflection pass → extracted facts
                                    │
                                    ▼
                          ChromaDB (data/memories/chroma.db/)
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
per-user.

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
    ├─► Local speakers (PyAudio, device indices from constants)
    ├─► Discord voice (FFmpeg → VoiceClient)
    └─► Browser (base64 → socket.io → opted-in tabs)
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
    ├─ "sdk__" prefix  → sdk.invoke_action_blocking()
    ├─ "web_search"    → web_search()
    ├─ "browse_url"    → browser_module.browse_url()
    ├─ "recall_memory" → memory_module.API.query_temporal()
    ├─ "vtube_*"       → vts.API.*  (with validation and aliases)
    └─ everything else → execute_tool(name, args)
```

Safety guards that fire before dispatch: read-only query gate, no-op
write guard, duplicate-call guard, protected files, path sandbox.

After dispatch, guards that can force a retry: simulated-action
detection, intent-announcement guard, refusal guard, no-tool-call retry.

### SDK bridge

The SDK inverts the tool direction. Instead of Lumi providing tools to
external apps, external apps provide tools to Lumi.

```
External app                 sdk/server.py                prompter
     │                             │                          │
     │ ── hello (auth) ──────────► │                          │
     │ ◄─ startup ──────────────── │                          │
     │                             │                          │
     │ ── actions/register ──────► │                          │
     │                             │  register in             │
     │                             │  self.actions{}          │
     │                             │ ── schemas as ─────────► │
     │                             │   sdk__<name>            │
     │                             │                          │
     │                             │ ◄─ tool call ──────────  │
     │ ◄─ action (invoke) ──────── │   sdk__<name>            │
     │ ── action/result ─────────► │ ── tool result ────────► │
     │                             │                          │
     │ ◄─ speech_finished ──────── │ ◄─ broadcast ──────────  │
```

The server binds strictly to `127.0.0.1:5056`. Auth is a shared token
at `data/memories/sdk_token.txt`. Capability tiers (`read` / `write` /
`dangerous`) determine the confirmation gate.

## Key files (top level)

| File | Role |
|---|---|
| `main.py` | Entry point — audio routing menu, module spawning, shutdown handler |
| `constants.py` | Config, paths, `HOST`, persona loader, model names, `build_system_prompt()` |
| `signals.py` | Shared state (history, flags, queues, `input_paused`) |

## Key files (modules)

| File | Role |
|---|---|
| `prompter.py` | Agent loop, command router, safety guards, model routing |
| `agent_tools.py` | Tool schemas + filesystem/command execution |
| `memory.py` | ChromaDB long-term memory, reflection, browser API |
| `llm_providers.py` | Multi-provider LLM clients, `chat_sync()` |
| `model_manager.py` | Ollama management + live `constants.py` patching |
| `socketIO.py` | aiohttp HTTP routes + python-socketio + static serving |
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

## Related docs

- [configuration.md](configuration.md) — where the paths and constants live
- [models.md](models.md) — how the two model paths differ
- [discord-voice.md](discord-voice.md) — the DAVE receive pipeline
- [security.md](security.md) — what's protected and what isn't
- [known-limitations.md](known-limitations.md) — what doesn't work yet
- [../modules/README.md](../modules/README.md) — file-level map of the modules folder
- [../sdk/README.md](../sdk/README.md) — the external SDK protocol
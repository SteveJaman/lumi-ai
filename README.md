# Lumi AI Companion

A local-first, agentic AI companion running on consumer hardware. Lumi is a
persona-driven VTuber-style assistant with real tool use — she can read and
write files, run commands, search the web, control a browser, query her own
memory, verify her own work, track her own behavioral patterns, drive her own
Live2D avatar, and route casual chat through a faster model — all coordinated
through local Ollama.

Unlike scripted chatbots or bracket-tag "tool" syntax, Lumi uses **structured
tool calling** through Ollama's native `/api/chat` tool interface. When she
calls a tool, it actually runs. When she says she did something, you can check
the log and see that she did.

---

## Table of Contents

- [What She Actually Does](#what-she-actually-does)
  - [Real Agentic Tool Use](#real-agentic-tool-use)
  - [Multi-Step Task Execution](#multi-step-task-execution)
  - [Safety Layers](#safety-layers)
  - [Multi-Model Orchestration](#multi-model-orchestration)
  - [Persona & Voice](#persona--voice)
  - [Episodic Memory](#episodic-memory)
  - [Emotional State & Cognitive Frames](#emotional-state--cognitive-frames)
  - [Self-Model](#self-model)
  - [Autonomous Heartbeat](#autonomous-heartbeat)
  - [Ambient Screen Awareness](#ambient-screen-awareness)
  - [Avatar Control (VTube Studio)](#avatar-control-vtube-studio)
  - [Multimodal (Optional)](#multimodal-optional)
  - [Diagnostics Dashboard](#diagnostics-dashboard)
  - [Integrations](#integrations)
- [Terminal Commands](#terminal-commands)
- [Architecture](#architecture)
  - [Turn Flow](#turn-flow)
  - [Key Files](#key-files)
  - [Background Modules](#background-modules)
- [Requirements](#requirements)
  - [Recommended Models](#recommended-models)
- [Installation](#installation)
- [Running](#running)
  - [Agent Mode Example](#agent-mode-example)
  - [Avatar Control Example](#avatar-control-example)
  - [Memory Query Example](#memory-query-example)
  - [Self-Model Query Example](#self-model-query-example)
  - [Model Routing Query Example](#model-routing-query-example)
- [Voice Customization](#voice-customization)
- [Resource Optimization](#resource-optimization)
- [Known Limitations](#known-limitations)
- [Roadmap](#roadmap)
- [Credits](#credits)
- [Disclaimer](#disclaimer)

---

## What She Actually Does

### Real Agentic Tool Use

Lumi has eleven tools available at request time. She picks them herself based
on the task, executes them one at a time, reads the result, and decides what
to do next — up to `MAX_AGENT_STEPS = 6` per turn.

| Tool | Purpose |
|---|---|
| `read_file(path)` | Read a file inside `WORKSPACE_ROOT` |
| `write_file(path, content)` | Create/overwrite — **requires confirmation** |
| `list_dir(path)` | List a folder |
| `search_files(query, path)` | grep the workspace |
| `run_command(command)` | Run a shell command, capture stdout/stderr — **requires confirmation** |
| `run_in_terminal(command)` | Open a NEW visible terminal — **requires confirmation** |
| `web_search(query)` | Web search or live weather/time |
| `browse_url(url)` | Playwright-based browser navigation |
| `recall_memory(query, since)` | Query Lumi's own long-term memory for facts, past conversations, or events with timestamps |
| `vtube_set_expression(name, enabled)` | Turn an appearance toggle on her own VTube Studio avatar on/off — idempotent |
| `vtube_move(location)` | Move her avatar to a preset screen position |

Because `write_file`, `run_command`, and `run_in_terminal` are gated behind a
`y/n` prompt, nothing destructive ever runs without your explicit approval.

### Multi-Step Task Execution

Lumi understands dependency ordering. Ask her to *"write a script that divides
10 by 0, run it, and fix the error"* and she will:

1. `write_file` — create the buggy script
2. `run_command` — run it, see the `ZeroDivisionError`
3. `write_file` — produce a fix
4. `run_command` — re-run to verify the fix
5. Report back in character, having actually verified the fix

That's a self-correcting loop, not a single-tool assistant.

### Safety Layers

Multiple guards prevent the model from lying about having done work, writing
files it wasn't asked to write, or looping on the same failed command:

- **Simulated-action detection** — catches "I've updated the file!" prose that
  isn't backed by a real tool call. Also catches state-change narrations like
  "Hat removed." or "Ponytail_r engaged." when the user asked for an avatar
  change
- **Intent-announcement guard** — catches "now let's X" without an actual call
- **Refusal guard** — catches "I can't" and re-prompts with the original request
- **Read-only query gate** — refuses `write_file` when the user only asked to read
- **No-op write guard** — refuses writes identical to the current file
- **Duplicate-call guard** — skips repeat tool calls; clears the memory after a
  successful `write_file` so verification runs aren't blocked
- **Tool-call format recovery** — Lumi's model emits tool calls in several
  formats (`[tool(arg=val)]`, `[tool{"k":"v"}]`, `[tool "val"]`, `[tool "val" true]`).
  All are recovered by a chain of parsers before dispatch, so a hallucinated
  format still fires the real tool
- **Protected files** — `main.py`, `constants.py`, `.env`, `Modelfile` are
  never overwritable
- **Path sandbox** — every filesystem tool is confined to `WORKSPACE_ROOT`
- **Auto-backup** — every overwrite of an existing file saves a `.bak` first
- **Linux-command blocklist** — rejects `sudo`, `systemctl`, `ls`, `cat`, etc.
  on Windows before the confirm prompt even appears
- **Avatar state validation** — `vtube_set_expression` rejects unknown state
  names before dispatch and returns the valid list, so the model can retry
  with a correct name instead of looping on a hallucinated one

### Multi-Model Orchestration

Lumi runs a two-model architecture to keep casual conversation fast without
sacrificing agent quality:

- **Main model** (`lumi-coder`, 7B, 32K context): agent turns, complex
  queries, heartbeat decisions. Full quality, higher latency.
- **Fast model** (`qwen2.5:7b`, 8K context): short casual chat, memory
  reflection, session summarization. Same family as the main model, smaller
  context window, meaningfully faster.

**Routing heuristic** (first match wins):

| Condition | Route |
|---|---|
| Agent mode active | Main |
| Message > 280 chars | Main |
| Complexity markers (`analyze`, `debug`, `step by step`, `why does`, etc.) | Main |
| Time/date questions | Main |
| Everything else | Fast |

The routing label appears in the terminal on every turn — `Thinking
(conversational, fast)` or `Thinking (conversational, full)` — so you can see
which model handled each message. Use `!model` to check the current routing
state.

### Persona & Voice

- **Fish Audio S2** synthesis with inline `[emotion]` tags (`[sarcastic]`,
  `[sighing]`, `[excited]`, `[whispering]`, `[shouting]`, `[laughing]`, …)
  parsed as delivery instructions, not spoken text. Only canonical tags from
  Fish Audio's S2 documentation are used — invented tags like `[smug]` or
  `[deadpan]` are silently ignored by the engine, so the persona prompt
  explicitly bans them. A `_strip_non_canonical_tags()` pass in the TTS
  pipeline removes any invented tags before they reach audio synthesis or
  get echoed back into history.
- **MoodManager** with four profiles: `default`, `bratty`, `brocon`, `soft` —
  switchable at runtime with `set mood <name>`
- **faster-whisper** local STT with VAD-triggered **barge-in** (start talking,
  she stops mid-sentence). Runs on GPU with `float16` for ~0.8–1.2s
  end-to-end latency per utterance.
- **Language mirroring** — if you write or speak in Vietnamese, Spanish,
  Japanese, or another supported language, she replies in that language
- **Dual output routing** — local speakers, Discord voice, or both

### Episodic Memory

ChromaDB-backed long-term memory with **timestamped, importance-scored facts**.

- Every ~10 messages, a background reflection pass asks the LLM to extract
  facts from the recent conversation. Each fact is stored with:
  - `timestamp` (ISO, seconds precision)
  - `importance` (0.0–1.0, model-scored)
  - `valence` (one-word emotion like `frustrated` or `satisfied`, or `null`)
  - `session_id` (per-run, for grouping)
- Retrieval reweights results by a combined score:
  `0.50 * similarity + 0.30 * importance + 0.20 * recency`, with a 30-day
  half-life on the recency term. Old facts naturally fade; important recent
  ones surface.
- Two query surfaces:
  - `!recall <query>` — terminal command, sorted oldest-first, with
    importance markers (`★` for 0.85+, `·` for 0.6+) and age hints
    (`[6:16 PM]`, `[yesterday]`, `[2d ago]`)
  - `recall_memory(query, since)` — an agent tool Lumi can call on her own
    when you ask "do you remember…" or "when did we talk about…"
- Temporal hints (`yesterday`, `last week`, `3 days ago`) are parsed in both
  the injection path and the explicit query path.
- A migration pass runs on startup and repairs any legacy records with
  missing or malformed metadata. Idempotent — silent when there's nothing
  to fix.

### Emotional State & Cognitive Frames

Five runtime variables (`valence`, `arousal`, `frustration`, `curiosity`,
`boredom`) with baselines and exponential decay every 30 seconds. Eight
personality traits from `lumi.yaml` modulate how strongly events move the
state.

Six cognitive frames — `play`, `strategy`, `chaos`, `social`, `learning`,
`threat` — are selected contextually:

- High frustration → `threat`
- Vulnerable user language → `social`
- High arousal + positive valence → `chaos`
- Agent mode active → `strategy`
- High curiosity + low frustration → `learning`
- Default → `play`

Each frame carries a tone directive that gets injected alongside the state
description. The state and frame color her delivery without her ever naming
them.

### Self-Model

Lumi keeps a running model of her own patterns — separate from her memory of
you. Two files, one behavior:

- **Collection** (`modules/self_model.py`): snapshots emotional state every
  60 seconds, tracks per-turn stats (canonical tags used, cognitive frame,
  response length), and increments an unprompted-message count when the
  heartbeat fires.
- **Reflection** (every 10 minutes, minimum 5 turns): sends a structured
  summary of the collected data to `lumi-coder` and gets back 2–4 short
  observations about recent patterns. Empty list is a valid result.
  Observations are persisted across sessions to `memories/self_model.json`.
- **Injection**: a sparse self-awareness block injected into the prompt only
  when observations are fresh (< 1 hour old). Framed as internal awareness
  the model should not announce.

The reflection model is the full 7B — the 3B variant was tried first and
hallucinated numbers ("an hour" for a 5-minute session, "two unprompted
messages" when the count was 0). The 7B reports accurately.

Check current state any time with `!self`.

### Autonomous Heartbeat

A background loop fires every 30 seconds and decides whether Lumi should speak
without being prompted. Gates:

- Not busy (not thinking or speaking)
- User idle for 5+ minutes (30+ minutes late at night)
- 10+ minutes since her last unprompted message

When gates pass, she's shown the current time, idle duration, last topic,
active window title, current mood, and her recent unprompted messages (to
avoid repeating herself). She responds with either a short anchored message or
the literal token `SILENCE`. When in doubt, she leans toward speaking — a
companion who only talks when something is wrong is not good company.

### Ambient Screen Awareness

A background sampler captures the active screen every 30 seconds, computes an
8×8 average hash, and only calls the vision model (`moondream`) when:

1. The foreground window title changes, or
2. The screen hash changes by more than 14 bits **and** 90+ seconds have
   passed since the last VLM call

Descriptions are cached for 5 minutes. A two-tier injection strategy keeps
casual conversation clean:

- **`[Active window] <title>`** — injected on every turn. Cheap, always
  accurate, no VLM involvement.
- **`[Screen Context] <full description>`** — injected only when the user's
  message matches a screen-reference regex (`screen`, `monitor`, `looking at`,
  `what am I`, `can you see`, etc.).

**Cost profile:** idle on the same window → ~1 screenshot + 1 hash per tick,
no VLM. Active (window switches or big content changes) → one VLM call,
rate-limited to at most once per 90 seconds.

### Avatar Control (VTube Studio)

Lumi drives her own Live2D avatar through pyvts. Two paths exist and both are
wired:

**Passive path** — whenever her reply contains a canonical Fish Audio emotion
tag, the first one is forwarded to the VTS module as an expression trigger.
`[moved]` fires heart eyes, `[angry]` fires the shadow face, `[confused]`
fires spiral eyes. She doesn't have to think about it; her face just matches
what she's already saying.

**Deliberate path** — she has two agent tools and six terminal commands for
manually triggering expressions and moving her avatar.

**Expression state tracking.** Every hotkey on her model is a
`ToggleExpression` — firing the same key twice turns the effect back off.
Some hotkeys are named for the *action* (`remove_jacket`, `hide_ponytail_l`)
rather than the resulting *state* (jacket on, ponytail visible), which reads
backwards. An `EXPRESSION_STATE_MAP` in the VTS module normalizes this:
`vtube_set_expression("jacket", enabled=True)` is a state request, not a
toggle. The module checks its tracked state, fires the hotkey only if the
state needs to change, and no-ops when it's already correct. So "put on your
hat" is idempotent — asking twice doesn't toggle it off the second time.

**Keyboard fallback path.** VTS's `HotkeyTriggerRequest` API is silently
accepted but does not fire on some models' `ToggleExpression` hotkeys (a
gesture-gate quirk in the plugin). The module falls back to simulating the
model's bound key combo (Ctrl+3 for hat, Ctrl+8 for left ponytail, etc.),
focusing the VTS window first. Windows' foreground lock blocks
`SetForegroundWindow` from background processes, so an `AttachThreadInput`
workaround is used. The keypress itself is sequenced (modifier-down, small
sleep, key-tap, small sleep, modifier-up) so VTS's polling input loop catches
every event.

**Natural-language alias resolution.** Lumi sometimes reaches for the
natural word instead of the schema name — `dizzy` instead of `spiral_eyes`,
`shadow` instead of `darkened`, `ponytail_left` instead of `ponytail_l`. A
`_STATE_NAME_ALIASES` map in the prompter resolves these before validation,
so the natural phrasing works.

**Fabrication guard.** On longer requests ("reset everything, take off your
hat, put your jacket on, hide both ponytails, drop the controller..."), Lumi
sometimes narrates success in prose instead of firing each tool call.
Multiple pattern checks catch this and force a retry: completion phrases
("there we go", "all set"), state claims ("Hat removed.", "Ponytail_r
engaged.", "X mode engaged"), meta-claims ("everything reset"), and expression
names wrapped in brackets (`[spiral_eyes]`). When the guard fires she gets one
retry with an explicit "call the tool, don't narrate it" nudge.

**Known gap.** Multi-item requests of more than ~3 items still occasionally
fall through the guard. For batch operations use the terminal commands
(`!set hat off`, `!set jacket on`, etc.) — they're idempotent, always land,
and skip the LLM entirely.

### Multimodal (Optional)

`MultiModal` module captures your primary/secondary monitor on demand, asks a
local VLM (`moondream`) to describe it, and injects the description into the
prompt when she detects a "look at my screen" style question. Triggered by
text patterns, not a continuous loop.

### Diagnostics Dashboard

High-frequency loop output (ambient ticks, heartbeat decisions, memory
operations, history saves, model routing choices, VTS hotkey fires) is routed
to a structured log hub instead of the terminal. Live view at:

```
http://127.0.0.1:5001/
```

Filterable by category (`ambient`, `heartbeat`, `memory`, `emotional`, `mic`,
`system`, `self`, `vts`, `tts`), color-coded, auto-refreshing every 2 seconds.
The terminal is reserved for actual conversation, ambient VLM fires, and
unprompted speech.

Events also persist to `logs/events.log` for post-mortem review.

### Integrations

- **VTube Studio** — full avatar control. Expression toggles with state
  tracking, model positioning, mouth-open sync from TTS envelope, organic
  head sway and blink, hotkey firing via either the API or keyboard
  simulation (configurable), expression triggers on emotion tags. The model
  is probed at boot to discover its real parameter names.
- **Discord** — reads chat, joins voice, plays TTS into voice. Voice *receive*
  (transcription of users in the channel) is currently unstable due to
  Discord's DAVE end-to-end encryption; see Known Limitations.
- **Browser** — Playwright (Firefox) with a persistent profile
- **Document library** — PDF, DOCX, TXT, MD, JSON reading from `library/`

---

## Terminal Commands

Type `!list` at any time to see the full command card. Summary:

| Command | Effect |
|---|---|
| `!list` / `!help` | Show command reference |
| `!agent_on` | Enable agent mode for the session — tools become available |
| `!agent_off` | Disable agent mode — pure conversation only |
| `!agent` | Toggle agent mode |
| `!agent <task>` | One-shot agent turn, returns to current mode |
| `!state` | Show emotional state, traits, and active cognitive frame |
| `!recall <query>` | Search long-term memory for a topic, oldest-first |
| `!self` | Show current self-model observations and session stats |
| `!model` | Show current model routing state (main, fast, availability) |
| `!sleep` | Summarize session and save continuity state for next time |
| `!set <name> on\|off` | Set an avatar expression state idempotently |
| `!expressions` | List avatar expression states with ● / ○ markers |
| `!expression <name>` | Toggle a raw VTS hotkey directly (no state tracking) |
| `!move <location>` | Move avatar to a preset screen position |
| `!hotkeys` | List raw VTS hotkeys on the loaded model |
| `!positions` | List preset VTS model positions |
| `set mood <name>` | Switch mood profile (`default`, `bratty`, `brocon`, `soft`) |
| `wipe memory` | Clear all long-term ChromaDB memories |

**Agent mode is OFF by default.** At startup, every message goes through a
plain chat completion with no tools — Lumi can't invent tasks on idle banter.
Only `!agent_on` or `!agent <task>` opens the tool interface. The `!set`,
`!expression`, `!move`, `!hotkeys`, `!positions`, and `!expressions` commands
do NOT require agent mode — they pass through to VTS directly.

---

## Architecture

### Turn Flow

```
User input (terminal, mic, or Discord)
        │
        ▼
Command router (checks !agent_on/off, !list, !state, !recall, !self, !model,
                mood, memory wipes, !set, !expressions, !expression, !move,
                !hotkeys, !positions)
        │
        ├─── conversational mode
        │     ├─ Routing decision: fast or full model
        │     ├─ Build system prompt (persona + tool directive + memory + mood
        │     │  + emotional state + cognitive frame + ambient context
        │     │  + self-model observations + continuity)
        │     └─ POST to Ollama /api/chat (no tools)
        │
        ├─── agent mode → run_agent_turn() on main model:
        │     ├─ Build system prompt (same injection stack)
        │     ├─ Append history
        │     ├─ POST to Ollama /api/chat with tool schemas
        │     ├─ If tool_calls returned:
        │     │    ├─ Confirm gates for write_file / run_command / run_in_terminal
        │     │    ├─ Validate args for vtube_set_expression / vtube_move
        │     │    ├─ Execute tool via execute_tool() or _dispatch_tool_call
        │     │    ├─ Append result as `role: tool`
        │     │    └─ Loop back to Ollama (up to MAX_AGENT_STEPS)
        │     └─ If plain text returned: push to TTS, save to history
        │
        └─── avatar commands → VTube Studio module
              ├─ !set / vtube_set_expression → idempotent state change
              ├─ !expression / vtube_hotkey → raw toggle (no tracking)
              └─ !move / vtube_move → reposition model
        │
        ▼
Self-model note_turn() — records tags, frame, length for the next reflection
```

### Key Files

| File | Role |
|---|---|
| `main.py` | Entry point — audio routing menu, module spawning |
| `modules/prompter.py` | Agent loop, command router, detection guards, model routing, format enforcement, avatar tool dispatch |
| `modules/agent_tools.py` | Tool schemas + filesystem/command execution |
| `modules/vtubeStudio.py` | VTube Studio controller — expression state tracking, parameter probing, keyboard fallback, organic motion |
| `modules/memory.py` | ChromaDB long-term memory, reflection loop, temporal queries |
| `modules/emotional_state.py` | Traits, state variables, cognitive frame selection |
| `modules/self_model.py` | Pattern tracking, periodic reflection, sparse injection |
| `modules/heartbeat.py` | Autonomous initiation loop |
| `modules/ambient_perception.py` | Background screen sampler + VLM descriptions |
| `modules/session_state.py` | Cross-session continuity (focus, unresolved, running bits) |
| `modules/log_hub.py` | Structured diagnostics stream + localhost dashboard |
| `modules/console_input.py` | Prompt-pinned terminal UI |
| `modules/socketIO.py` | Web control panel backend (Socket.io on port 5000) |
| `modules/tts.py` | Fish Audio HTTP client with per-language reference support |
| `signals.py` | Shared state object (history, flags, queues) |
| `constants.py` | Config, workspace path, YAML persona loader, model names |
| `lumi.yaml` | Full persona definition — personality, lore, favorites, fears |

### Background Modules

Each module runs in its own thread with its own event loop, spawned by
`main.py`. Modules expose `run()`, `init_event_loop()`, and optionally
`get_prompt_injection()` for injecting context into the system prompt.

---

## Requirements

- **Python** 3.11+
- **Ollama** running locally with at least two tool-capable models (see below)
- **FFmpeg** on PATH (for Discord voice playback)
- **Playwright Firefox** (`playwright install firefox`)
- **PyYAML** (`pip install pyyaml`)
- **pyvts** (`pip install pyvts`) — for VTube Studio avatar control
- **keyboard** (`pip install keyboard`) — for VTS hotkey simulation
- **Conda** recommended for environment isolation

### Recommended Models

Two models: a main model for agent work and a fast model for casual chat.

**Main** — custom Modelfile built on Qwen2.5:7b:

```
FROM qwen2.5:7b
SYSTEM You are Lumi, a real person who streams under a fictional
       cosmic-AI persona. Short, dry, present.
PARAMETER num_ctx 32768
```

Build with `ollama create lumi-coder -f Modelfile`.

**Fast** — pull directly:

```bash
ollama pull qwen2.5:7b
```

Both are configured in `constants.py`:

```python
MODEL_NAME = "lumi-coder"
FAST_MODEL_NAME = "qwen2.5:7b"
FAST_MODEL_ENABLED = True
FAST_MODEL_NUM_CTX = 8192
FAST_MODEL_KEEP_ALIVE = "5m"
```

The general Qwen2.5:7b variant is preferred over Qwen2.5-Coder, which has a
training bias toward emitting code-as-text and produces more simulated-action
hallucinations.

**For lower-end hardware:** drop `num_ctx` to `16384` and consider
`qwen2.5:3b` as the fast model. The 3B variant works but has known issues with
format compliance and occasional fact mangling on injected context — see
Known Limitations.

**Disabling routing entirely:** set `FAST_MODEL_ENABLED = False` in
`constants.py`. All turns go through the main model. You lose the latency win
on casual chat; you gain one less model to load.

---

## Installation

1. **Clone and enter the workspace.**

   ```bash
   git clone <your-repo> Lumi-master
   cd Lumi-master
   ```

2. **Create and activate a Conda environment.**

   ```bash
   conda create -n lumi python=3.11
   conda activate lumi
   ```

3. **Install dependencies.**

   ```bash
   pip install -r requirements.txt
   playwright install firefox
   ```

4. **Create `.env`** at the workspace root:

   ```
   FISH_API_KEY=your_fish_audio_key
   FISH_REFERENCE_ID=your_voice_reference_id
   DISCORD_BOT_TOKEN=your_bot_token
   DISCORD_VOICE_CHANNEL_ID=000000000000000000
   DISCORD_CHANNEL_ID=000000000000000000
   ```

5. **Configure `constants.py`** — set `WORKSPACE_ROOT`, `LLM_ENDPOINT`,
   `TIMEZONE`, and `OUTPUT_DEVICE_INDEX` for your machine. Run
   `python modules/check_mics.py` to list available audio device indices.

6. **Edit `lumi.yaml`** — this is where Lumi's persona lives. Change the name,
   host, personality, lore, appearance, and dialogue to make her your own.

7. **Pull both models.**

   ```bash
   ollama pull qwen2.5:7b
   ollama create lumi-coder -f Modelfile
   ```

8. **VTube Studio (optional but recommended).** Install VTS, load a Live2D
   model, enable the API in VTS settings, then run `python main.py` once to
   trigger the plugin authentication prompt. If your model uses keyboard-bound
   hotkeys (like ariu), edit `HOTKEY_KEYBINDINGS` in `modules/vtubeStudio.py`
   to match the model's key combos. See the model's `.vtube.json` under the
   `Hotkeys` array for the actual bindings.

---

## Running

```bash
conda activate lumi
python main.py
```

You'll see the audio routing menu:

```
=======================================================
  Lumi Audio Routing Setup
=======================================================
1. Local PC Only
2. Discord Voice Channel Only
3. Both (PC + Discord)

Select audio routing (1/2/3):
```

Once initialized, talk to her in the terminal, over the mic (`\` to toggle
mute), or via Discord text/voice.

### Agent Mode Example

```
Anthony: !agent_on
Lumi: [agent mode: ON] Agent mode engaged. Tools are live, Anthony.

Anthony: write a script called fib.py that prints the first 10 fibonacci
         numbers, run it, and tell me the actual output

Lumi: Thinking (agent step 1/6)...
[Lumi wants to use tool: write_file({'path': 'fib.py', 'content': '...'})]
  -> Allow this? (y/n): y
  -> Created fib.py (... chars written).

Thinking (agent step 2/6)...
[Lumi wants to use tool: run_command({'command': 'python fib.py'})]
  -> Allow this? (y/n): y
  -> Exit code 0
     0
     1
     1
     2
     3
     5
     8
     13
     21
     34

[confident] There you go. The output is real — I actually ran it.
```

### Avatar Control Example

```
Anthony: !agent_on
Lumi: [agent mode: ON] Agent mode engaged. Tools are live, Anthony.

Anthony: Lumi, put on your hat.

Lumi: Thinking (agent step 1/6)...
[Lumi is using tool: vtube_set_expression(hat, enabled=True)]
  -> Set 'hat' to on.
Thinking (agent step 2/6)...
[sarcastic] There. Happy?

Anthony: !set hat on

[VTS] 'hat' is already on

Anthony: !expressions

Expression States
  ○ bag
  ○ controller
  ○ darkened
  ● hat
  ○ heart_eyes
  ● jacket
  ● ponytail_l
  ● ponytail_r
  ○ skirt
  ○ spiral_eyes

Anthony: Lumi, take off your hat.

Lumi: Thinking (agent step 1/6)...
[Lumi is using tool: vtube_set_expression(hat, enabled=False)]
  -> Set 'hat' to off.
Thinking (agent step 2/6)...
[soft tone] There. Better?
```

For batch operations, use the terminal commands directly — they bypass the
LLM and always land:

```
!set jacket off
!set hat off
!set bag off
!set skirt off
!set controller off
!set heart_eyes off
!set spiral_eyes off
!set darkened off
!set ponytail_l on
!set ponytail_r on
```

### Memory Query Example

```
Anthony: !recall perception

Memory recall: 'perception'
[6:16 PM] ★ Anthony is working on a perception feature for Lumi
[6:16 PM] · [tired] Anthony frequently zones out or gets distracted
[6:16 PM] Anthony prefers Python over other programming languages
[6:27 PM] ★ Anthony is working on a perception feature for Lumi
[yesterday] Anthony's main monitor is cluttered with development tools
```

The `★` marker means importance ≥ 0.85, `·` means ≥ 0.6.

### Self-Model Query Example

```
Anthony: !self

Self-Model
  · Sarcasm used more frequently — 3 out of 5 turns.
  · Shorter unprompted message count — only 0 today

  (last reflection: 3m ago)

Session Stats
  turns:        8
  unprompted:   0
  snapshots:    7
  top tags:     [sarcastic]×3, [laughing]×3, [sympathetic]×3
  frames:       play 100%
```

### Model Routing Query Example

```
Anthony: !model

Model Routing
  main:        lumi-coder
  fast:        qwen2.5:7b (available)
  routing:     ON
  agent turns: main
  chat:        fast (short/simple) / main (long/complex)
```

---

## Voice Customization

Lumi's voice comes from Fish Audio. The reference voice is set via
`FISH_REFERENCE_ID` in `.env`.

**Emotional delivery** works through inline tags in square brackets. The
persona prompt (`lumi.yaml`) tells the model which tags to use in which
situations:

- `[excited]`, `[delighted]` — happy, enthusiastic moments
- `[whispering]`, `[soft tone]` — quiet, unguarded, or handler mode
- `[sighing]`, `[chuckling]`, `[laughing]` — non-verbal beats
- `[shouting]`, `[screaming]` — loud, frustrated, or genuinely high-energy
- `[sarcastic]`, `[disdainful]`, `[confident]` — dry, superior, or unimpressed

Only canonical tags from Fish Audio's S2 documentation are supported. Invented
tags like `[smug]`, `[deadpan]`, `[pitch up]`, or `[low voice]` are silently
ignored by the engine. The persona file lists all 64+ approved tags organized
by category and forbids non-canonical ones. A `_strip_non_canonical_tags()`
pass in the TTS pipeline removes any invented tags before they reach audio
synthesis — so even if the model slips and emits `[rolling eyes]`, it never
gets spoken.

**These tags also drive Lumi's avatar.** The first canonical tag in each reply
is forwarded to the VTube Studio module as an expression trigger — `[moved]`
fires heart eyes, `[angry]` fires the shadow face, `[confused]` fires spiral
eyes. So her choice of delivery tag colors both voice and face at once.

**To change the voice:** record a 10–30 second clip of the voice you want,
upload it to Fish Audio, get a `reference_id`, and update `.env`. Voice quality
is heavily influenced by the reference clip — quiet room, consistent volume,
and varied intonation all matter.

**Pitch limitations:** Fish Audio's tag system lets you influence *delivery*
(tone, pace, emphasis) but not raw pitch. Per-word F0 modulation — the kind
you hear in Evil Neuro — requires a custom TTS pipeline with an explicit pitch
extractor and a neural vocoder. Not included. Attempting pitch-shift
post-processing with ffmpeg was tried and rejected — the reference voice is
already at the high end of natural range, and any meaningful shift reads as
artificial.

---

## Resource Optimization

Lumi is designed to run on a single consumer GPU, but idle resource use can be
trimmed significantly:

1. **Unload models between turns.** Both the main and fast model use
   `keep_alive` strings. Main model unloads after 30s of inactivity (idle GPU
   drops to ~30W). Fast model stays warm for 5 minutes during active chat,
   then unloads. Cost: ~3–5 second reload on the next message.

2. **Disable modules you're not using.** In `main.py`:
   - `MultiModal` — screen capture on demand; light when idle
   - `AmbientPerception` — samples every 30s; VLM calls rate-limited
   - `SelfModel` — very light when idle; reflection fires every 10 min
   - `DiscordClient` — loads a second Whisper model
   - `BrowserController` — holds a Playwright Firefox context
   - `VtubeStudio` — light when idle, but requires VTS to be running
   - `MicInput` — set to `False` if you mostly type at the terminal

3. **Disable fast-model routing** with `FAST_MODEL_ENABLED = False` if VRAM
   is tight. Everything routes to the main model. You lose latency wins on
   casual chat; you save ~4.7GB of VRAM headroom for other processes.

4. **Auto-detect remote sessions.** `MicInput` is disabled automatically when
   `SESSIONNAME` starts with `RDP-` in `constants.py`, so remote sessions boot
   without mic load.

5. **Reduce `num_ctx`.** From `32768` to `16384` saves ~1 GB VRAM and slightly
   reduces per-token inference cost on the main model.

---

## Known Limitations

- **Context window** — 32K tokens for main, 8K for fast. The system prompt +
  tool schemas + history + tool results + memory injection + ambient context
  + self-model + continuity block add up fast; very long sessions may need
  `main.py` restarted to clear history.
- **Local model variance** — 7B-class models occasionally invent things, drop
  tool-call formatting, or hallucinate action completion. The guards catch
  most of this, but it's a moving target across model updates. The fast model
  slot specifically was tested with `qwen2.5:3b` and found to produce format
  slips, non-canonical tag leaks, and occasional fact mangling — it was
  upgraded to `qwen2.5:7b` to resolve these.
- **Tool-call format variance** — Lumi's 7B model does not always emit tool
  calls in the standard JSON shape. It falls back to bracket forms like
  `[tool(arg=val)]`, `[tool{"k":"v"}]`, `[tool "val"]`, and
  `[tool "val" true]`. All are recovered by a chain of parsers, but the
  bracket-positional form occasionally needs an extra retry step when the
  user asks for many things at once.
- **VTS avatar multi-item fabrications** — On requests with more than ~3
  items ("reset everything, take off your hat, put your jacket on, hide both
  ponytails, drop the controller..."), Lumi sometimes narrates success in
  prose instead of firing each individual `vtube_set_expression` tool call.
  The fabrication guard catches most shapes but not all. Use `!set` terminal
  commands for batch operations — they bypass the LLM entirely and always
  land.
- **VTS state drift** — If you manually press a bound hotkey in VTS
  (Ctrl+3 for hat, etc.), the module's internal `_toggle_state` dict drifts
  from the actual model state. A subsequent `vtube_set_expression` call may
  no-op when it should fire. Fix: `!set <name> on` / `!set <name> off`
  re-syncs the tracked state idempotently. VTS does not expose a read API for
  current expression state, so a true auto-resync isn't possible.
- **VTS focus stealing** — The keyboard fallback path briefly focuses the
  VTS window to send the keypress (~250ms per hotkey). If a batch of hotkeys
  fires, focus returns to whatever you were doing after the last one. Any
  keystrokes you type during that window may land in VTS instead of your
  active application.
- **VTS parameter injection on physics-managed params** — Some model configs
  (`UseBreathing: true`, head-sway physics writing back to `ParamAngleX`)
  refuse direct API injection into managed parameters. The VTS module probes
  the model at boot to discover which params accept injection and which
  silently reject it; managed params are skipped.
- **Discord voice receive** — Discord's DAVE end-to-end encryption requires
  `discord-ext-voice-recv >= 0.5.2a`. Older versions connect but receive no
  audio. Voice receive is currently paused in this build; text works.
- **Voice pitch control** — Fish Audio's tag system influences delivery but
  not raw pitch. See [Voice Customization](#voice-customization).
- **Ambient VLM accuracy** — moondream is a small model. It reliably
  identifies general context (code editor open, browser page, terminal) but
  can misname specific applications ("code editor" for a browser window, for
  example). Descriptions are structurally correct even when not perfectly
  app-accurate.
- **Self-modification** — Lumi can edit `prompter.py` and other writable
  files in the workspace. Always read the `write_file` confirmation blob
  before confirming; a `.bak` is saved automatically.
- **Port conflicts** — the log hub listens on `127.0.0.1:5001` and the
  Socket.io control panel on `5000`. Both must be free at boot. Change
  `DEFAULT_PORT` in `modules/log_hub.py` if 5001 is occupied.

---

## Roadmap

Architectural phases inspired by Neuro-sama (Vedal987). The goal is not
intelligence parity — it's architectural parity. Neuro runs continuously, has
internal state, makes her own decisions, and acts without being prompted. This
project closes that gap in measurable steps.

| Phase | Description | Status |
|---|---|---|
| 1 | Autonomous heartbeat | ✅ Complete |
| 2 | Emotional state + traits + cognitive frames | ✅ Complete |
| 3 | Continuous ambient perception | ✅ Complete |
| 4 | Episodic memory (timestamps + importance) | ✅ Complete |
| 5 | Multi-model orchestration | ✅ Complete |
| 6 | Self-model / meta-cognition | ✅ Complete |
| 6.5 | Avatar control (VTube Studio) | ✅ Complete |
| 7 | Discord voice bridge (C# sidecar) | ⏳ In progress |
| 8 | External SDK (websocket plugin host) | ⏳ Planned |
| 9 | Security hardening | ⏳ Planned |
| 10 | Dynamic user identity | 🟡 Designed |

Every phase through 6.5 is shipped and stable. Phases 7–10 are documented in
the project roadmap and represent optional polish — nothing is broken, and the
existing architecture supports all of them cleanly.

Use her for a week before deciding which (if any) of the remaining items
matter. Real use drives the next round of tuning, not a checklist.

---

## Credits

Built as a personal project. Architecture inspired by Neuro-sama
(Vedal987). 

* **VTube Model:** [Arii / Ariu (Booth)](https://booth.pm/en/items/5879420)

Built using Ollama, ChromaDB, Faster-Whisper, Fish Audio, Playwright,
pyvts, and pyautogui.

---

## Disclaimer

Local, experimental AI companion project. The language model generates all
responses dynamically; behavior depends on the specific model and its prompt
adherence. This is a personal-use agent, not a production system. Use with
assumption of risk.
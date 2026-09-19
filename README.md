# Lumi AI Companion

A local-first, agentic AI companion running on consumer hardware. Lumi is a
persona-driven VTuber-style assistant with real tool use — she can read and
write files, run commands, search the web, control a browser, and verify her
own work — all coordinated through a local Ollama model.

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
  - [Persona & Voice](#persona--voice)
  - [Persistent Memory](#persistent-memory)
  - [Multimodal (Optional)](#multimodal-optional)
  - [Integrations](#integrations)
- [Terminal Commands](#terminal-commands)
- [Architecture](#architecture)
  - [Turn Flow](#turn-flow)
  - [Key Files](#key-files)
  - [Background Modules](#background-modules)
- [Requirements](#requirements)
  - [Recommended Model](#recommended-model)
- [Installation](#installation)
- [Running](#running)
  - [Agent Mode Example](#agent-mode-example)
- [Voice Customization](#voice-customization)
- [Resource Optimization](#resource-optimization)
- [Known Limitations](#known-limitations)
- [Credits](#credits)
- [Disclaimer](#disclaimer)

---

## What She Actually Does

### Real Agentic Tool Use

Lumi has eight tools available at request time. She picks them herself based on
the task, executes them one at a time, reads the result, and decides what to do
next — up to `MAX_AGENT_STEPS = 6` per turn.

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
  isn't backed by a real tool call
- **Intent-announcement guard** — catches "now let's X" without an actual call
- **Read-only query gate** — refuses `write_file` when the user only asked to read
- **No-op write guard** — refuses writes identical to the current file
- **Duplicate-call guard** — skips repeat tool calls; clears the memory after a
  successful `write_file` so verification runs aren't blocked
- **Protected files** — `main.py`, `constants.py`, `.env`, `Modelfile` are
  never overwritable
- **Path sandbox** — every filesystem tool is confined to `WORKSPACE_ROOT`
- **Auto-backup** — every overwrite of an existing file saves a `.bak` first
- **Linux-command blocklist** — rejects `sudo`, `systemctl`, `ls`, `cat`, etc.
  on Windows before the confirm prompt even appears

### Persona & Voice

- **Fish Audio S2** synthesis with inline `[emotion]` tags (`[smug]`,
  `[sighing]`, `[sarcastic]`, `[excited]`, …) parsed as delivery instructions,
  not spoken text
- **MoodManager** with four profiles: `default`, `bratty`, `brocon`, `soft` —
  switchable at runtime with `set mood <name>`
- **faster-whisper** local STT with VAD-triggered **barge-in** (start talking,
  she stops mid-sentence)
- **Language mirroring** — if you write or speak in Vietnamese, Spanish,
  Japanese, or another supported language, she replies in that language
- **Dual output routing** — local speakers, Discord voice, or both

### Persistent Memory

ChromaDB-backed long-term memory. Every ~10 messages, Lumi reflects on the
recent conversation and stores extracted facts; relevant memories are pulled
into the prompt based on semantic similarity to recent turns.

### Multimodal (Optional)

`MultiModal` module captures your primary/secondary monitor, asks a local VLM
(`moondream`) to describe it, and injects the description into the prompt when
she detects a "look at my screen" style question. Triggered on demand — no
continuous screenshot loop.

### Integrations

- **Discord** — reads chat, joins voice, plays TTS into voice. Voice *receive*
  (transcription of users in the channel) is currently unstable due to
  Discord's DAVE end-to-end encryption; see Known Limitations.
- **VTube Studio** — pyvts integration for avatar control, hotkeys, model
  positioning, microphone prop
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
| `set mood <name>` | Switch mood profile (`default`, `bratty`, `brocon`, `soft`) |
| `wipe memory` | Clear all long-term ChromaDB memories |
| `!mic_on` / `!mic_off` | Start/stop microphone listening |

**Agent mode is OFF by default.** At startup, every message goes through a
plain chat completion with no tools — Lumi can't invent tasks on idle banter.
Only `!agent_on` or `!agent <task>` opens the tool interface.

---

## Architecture

### Turn Flow

```
User input (terminal, mic, or Discord)
        │
        ▼
Command router (checks !agent_on/off, !list, mood, memory wipes)
        │
        ├─── conversational mode → single chat completion, no tools
        │
        └─── agent mode → run_agent_turn():
              ├─ Build system prompt (persona + tool directive + memory + mood)
              ├─ Append history
              ├─ POST to Ollama /api/chat with tool schemas
              ├─ If tool_calls returned:
              │    ├─ Confirm gates for write_file / run_command / run_in_terminal
              │    ├─ Execute tool via execute_tool() in agent_tools.py
              │    ├─ Append result as `role: tool`
              │    └─ Loop back to Ollama (up to MAX_AGENT_STEPS)
              └─ If plain text returned: push to TTS, save to history
```

### Key Files

| File | Role |
|---|---|
| `main.py` | Entry point — audio routing menu, module spawning |
| `modules/prompter.py` | The agent loop, command router, detection guards |
| `modules/agent_tools.py` | Tool schemas + filesystem/command execution |
| `modules/socketIO.py` | Web control panel backend (Socket.io server on port 5000) |
| `modules/tts.py` | Fish Audio HTTP client with per-language reference support |
| `signals.py` | Shared state object (history, flags, queues) |
| `constants.py` | Config, workspace path, YAML persona loader, auto-aging |
| `lumi.yaml` | Full persona definition — personality, lore, favorites, fears |

### Background Modules

Each module runs in its own thread with its own event loop, spawned by
`main.py`. Modules expose `run()`, `init_event_loop()`, and optionally
`get_prompt_injection()` for injecting context into the system prompt.

---

## Requirements

- **Python** 3.11+
- **Ollama** running locally with a tool-capable model (see below)
- **FFmpeg** on PATH (for Discord voice playback)
- **Playwright Firefox** (`playwright install firefox`)
- **Conda** recommended for environment isolation

### Recommended Model

The agent loop works best on a model that emits structured `tool_calls`
natively. Tested and recommended:

```
FROM qwen2.5:7b
SYSTEM You are Lumi, Anthony's deadpan, smug AI VTuber companion.
PARAMETER num_ctx 32768
```

Build with `ollama create lumi-coder -f Modelfile`. The general Qwen2.5:7b
variant is preferred over Qwen2.5-Coder, which has a training bias toward
emitting code-as-text and produces more simulated-action hallucinations.

**For lower-end hardware:** drop `num_ctx` to `16384` and consider
`qwen2.5:3b`. The agent loop still works, but persona depth and tool-call
reliability both degrade.

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

5. **Configure `constants.py`** — set `WORKSPACE_ROOT`, `LLM_ENDPOINT`, and
   `OUTPUT_DEVICE_INDEX` for your machine. Run `python modules/check_mics.py` to list
   available audio device indices.

6. **Edit `lumi.yaml`** — this is where Lumi's persona lives. Change the name,
   host, personality, lore, appearance, and dialogue to make her your own.

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

---

## Voice Customization

Lumi's voice comes from Fish Audio. The reference voice is set via
`FISH_REFERENCE_ID` in `.env`.

**Emotional delivery** works through inline tags in square brackets. The
persona prompt (`lumi.yaml`) tells the model which tags to use in which
situations:

- `[excited]`, `[pitch up]` — happy, enthusiastic moments
- `[whisper]`, `[low voice]` — soft, unguarded, or handler mode
- `[sighing]`, `[chuckling]` — non-verbal beats
- `[confident]`, `[disdainful]` — smug, superiority mode

**To change the voice:** record a 10–30 second clip of the voice you want,
upload it to Fish Audio, get a `reference_id`, and update `.env`. Voice quality
is heavily influenced by the reference clip — quiet room, consistent volume,
and varied intonation all matter.

**Pitch limitations:** Fish Audio's tag system lets you influence *delivery*
(tone, pace, emphasis) but not raw pitch. Per-word F0 modulation — the kind
you hear in Evil Neuro — requires a custom TTS pipeline with an explicit pitch
extractor and a neural vocoder. Not included. A lighter alternative is
post-processing TTS output with a pitch-shift library like SoX, but the effect
is coarser than what a trained vocoder produces.

---

## Resource Optimization

Lumi is designed to run on a single consumer GPU, but idle resource use can be
trimmed significantly:

1. **Unload the model between turns.** Add `"keep_alive": "30s"` to every
   Ollama `requests.post` payload. Model unloads 30 seconds after your last
   message; idle GPU drops from ~95W to ~30W. Cost: ~3–5 second reload on the
   next message.

2. **Disable modules you're not using.** In `main.py`:
   - `MultiModal` — screen capture on demand; light when idle
   - `DiscordClient` — loads a second Whisper model
   - `BrowserController` — holds a Playwright Firefox context
   - `MicInput` — set to `False` if you mostly type at the terminal

3. **Auto-detect remote sessions.** `MicInput` is disabled automatically when
   `SESSIONNAME` starts with `RDP-` in `constants.py`, so remote sessions boot
   without mic load.

4. **Reduce `num_ctx`.** From `32768` to `16384` saves ~1 GB VRAM and slightly
   reduces per-token inference cost.

---

## Known Limitations

- **Context window** — 32K tokens. The system prompt + tool schemas + history
  + tool results add up fast; very long sessions may need `main.py` restarted
  to clear history.
- **Local model variance** — 7B-class models occasionally invent things, drop
  tool-call formatting, or hallucinate action completion. The guards catch
  most of this, but it's a moving target across model updates.
- **Discord voice receive** — Discord's DAVE end-to-end encryption requires
  `discord-ext-voice-recv >= 0.5.2a`. Older versions connect but receive no
  audio. Voice receive is currently paused in this build; text works.
- **Voice pitch control** — Fish Audio's tag system influences delivery but
  not raw pitch. See [Voice Customization](#voice-customization).
- **Self-modification** — Lumi can edit `prompter.py` and other writable files
  in the workspace. Always read the `write_file` confirmation blob before
  confirming; a `.bak` is saved automatically.

---

## Disclaimer

Local, experimental AI companion project. The language model generates all
responses dynamically; behavior depends on the specific model and its prompt
adherence. This is a personal-use agent, not a production system. Use with
assumption of risk.
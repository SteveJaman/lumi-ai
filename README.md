# Lumi AI Companion

> Lumi now supports an **edge-optimized runtime mode** for ARM64 hardware (such
> as the Surface Pro 11), offloading vector embeddings and speech-to-text
> directly to the NPU/GPU via ONNX Runtime and DirectML. She also has a full
> external SDK. Third-party applications connect over a local websocket,
> register their own actions, push context, and receive invocations — the same
> pattern Vedal's Neuro SDK uses, with a Neuro-shaped wire protocol. She also
> speaks and hears in Discord voice channels with correct DAVE end-to-end
> encryption (pure Python, no sidecar), and her local web client at
> `127.0.0.1:5000` is a complete editing surface for persona, models,
> providers, integrations, and settings. Only Phase 9 (security hardening)
> remains on the roadmap.

A local-first, agentic AI companion running on consumer hardware. Lumi is a
persona-driven VTuber-style assistant with real tool use — she can read and
write files, run commands, search the web, control a browser, query her own
memory, verify her own work, track her own behavioral patterns, drive her own
Live2D avatar, and route casual chat through a faster path — all coordinated
through local Ollama.

Unlike scripted chatbots or bracket-tag "tool" syntax, Lumi uses **structured
tool calling** through Ollama's native `/api/chat` tool interface. When she
calls a tool, it actually runs. When she says she did something, you can check
the log and see that she did.

Phase 8 is complete: Lumi now has an **external SDK**. Other applications
connect to her over a local websocket, register their own tools, and receive
invocations when she decides to use them — see [`sdk/README.md`](sdk/README.md).

Phase 10 is complete: Lumi now has a full **local web client** at
`127.0.0.1:5000` — a first-run setup wizard, chat, dashboard, memory browser,
persona editor, model manager, cloud provider support (OpenAI, Anthropic,
Gemini, Groq, OpenRouter, and other OpenAI-compatible endpoints), an
integrations page, a settings page with password change and session
management, per-user memory and emotional-state isolation, and voice in the
browser. It's all served locally, so the default setup never leaves your
machine. The terminal remains a first-class developer tool; the two surfaces
share the same state and history.

---

## Table of Contents

- [What She Does](#what-she-does)
- [Documentation](#documentation)
- [Requirements](#requirements)
- [Installation](#installation)
- [Running](#running)
- [Roadmap](#roadmap)
- [Credits](#credits)
- [Disclaimer](#disclaimer)

---

## What She Does

- **Real agentic tool use.** Eleven built-in tools (file read/write, shell
  commands, web search, browser control, memory recall, avatar control) plus
  any actions registered by SDK plugins. She picks tools herself, chains up to
  six steps per turn, and reads each result before deciding what to do next.
  File writes and shell commands need your `y/n` approval. Agent mode is a
  plain on/off toggle (`!agent_on` / `!agent_off`). See
  [docs/architecture.md](docs/architecture.md) and
  [docs/security.md](docs/security.md).
- **Persona and voice.** Personality, lore, and rules live in `lumi.yaml` and
  can be edited live from the web client. Speech uses Fish Audio with inline
  emotion tags. See [docs/configuration.md](docs/configuration.md).
- **Memory and inner state.** Timestamped, importance-scored long-term memory
  in ChromaDB, an emotional state with cognitive frames, a self-model of her
  own patterns, and an autonomous heartbeat that lets her speak unprompted.
  See [docs/architecture.md](docs/architecture.md) and
  [modules/README.md](modules/README.md).
- **Screen awareness.** An ambient sampler and an on-demand multimodal path use
  a local vision model to describe what is on screen.
- **Local models, optional cloud.** Runs on local Ollama by default, with an
  optional fast path for casual chat and optional OpenAI, Anthropic, Gemini,
  Groq, and OpenRouter routing. See [docs/models.md](docs/models.md).
- **VTube Studio avatar.** Drives her own Live2D avatar through expressions,
  positions, and emotion-tag triggers. See [modules/README.md](modules/README.md).
- **Discord voice.** Joins voice channels, hears every speaker, and talks back
  with DAVE end-to-end encryption handled in pure Python. See
  [docs/discord-voice.md](docs/discord-voice.md).
- **Local web client.** Setup wizard, chat, dashboard, memory browser, persona
  editor, model manager, integrations, and settings at
  `http://127.0.0.1:5000/`. No framework, no build step. See
  [web/README.md](web/README.md).
- **External SDK.** Third-party apps connect over a local websocket, register
  their own actions, and push context. See [sdk/README.md](sdk/README.md).
- **ARM Edge Optimization.** Lightweight ONNX-backed adapters for STT and
  vector embeddings, utilizing DirectML and NPU acceleration on devices like
  the Surface Pro 11. See [modules/README.md](modules/README.md#edge-optimization).

### Architecture

The turn flow, component topology, key files, and background modules are
documented in [docs/architecture.md](docs/architecture.md). For a file-level
map of the backend, see [modules/README.md](modules/README.md).

---

## Documentation

[docs/README.md](docs/README.md) is the full index. The most common entry
points:

| If you need... | Read |
|---|---|
| Model routing, providers, and the model manager | [docs/models.md](docs/models.md) |
| Discord voice details and diagnostics | [docs/discord-voice.md](docs/discord-voice.md) |
| Safety layers and the threat model | [docs/security.md](docs/security.md) |
| What does not work yet | [docs/known-limitations.md](docs/known-limitations.md) |
| Diagnostics, FFmpeg problems, and memory backfill | [docs/troubleshooting.md](docs/troubleshooting.md) |
| What Lumi writes to `data/` | [data/README.md](data/README.md) |

---

## Requirements

- **Python** 3.11+ (Conda recommended for environment isolation; native ARM64
  builds supported for edge deployment)
- **ONNX Runtime** (`onnxruntime` or `onnxruntime-directml` for ARM/NPU
  hardware acceleration)
- **Ollama** running locally with a tool-capable model
- **FFmpeg** on PATH, for Discord voice playback. If voice send is silent, see
  [docs/troubleshooting.md](docs/troubleshooting.md)
- **Playwright Firefox** (`playwright install firefox`)

The remaining Python packages (`discord.py>=2.7`, `davey`, `chromadb`,
`aiohttp`, `python-socketio`, `argon2-cffi`, `pyvts`, `keyboard`, `pyyaml`,
`ruamel.yaml` (optional), `faster-whisper`, `websockets>=12.0`) install through
`pip` in the steps below. `psutil` and `mss` are only needed for the SDK
example clients.

For reproducible installs, use the pinned lock file instead of resolving fresh
versions: `pip install -r requirements.lock.txt`.

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
   pip install aiohttp python-socketio argon2-cffi chromadb
   playwright install firefox
   ```

   If `requirements.txt` already lists these, the second line is redundant.

4. **Create `.env`** at the workspace root:

   ```
   FISH_API_KEY=your_fish_audio_key
   FISH_REFERENCE_ID=your_voice_reference_id
   DISCORD_BOT_TOKEN=your_bot_token
   DISCORD_VOICE_CHANNEL_ID=000000000000000000
   DISCORD_CHANNEL_ID=000000000000000000
   ```

   Cloud provider keys are optional. Set them from the Models page or the
   setup wizard, or add them to `.env` yourself:

   ```
   # ---- Cloud LLM providers ----
   # OPENAI_API_KEY=
   # ANTHROPIC_API_KEY=
   # GEMINI_API_KEY=
   # GROQ_API_KEY=
   # OPENROUTER_API_KEY=
   # Optional base-URL overrides for self-hosted / proxy endpoints:
   # OPENAI_BASE_URL=
   # ANTHROPIC_BASE_URL=
   # GEMINI_BASE_URL=
   # GROQ_BASE_URL=
   # OPENROUTER_BASE_URL=
   ```

   The integration keys are also written by the Integrations page. To change
   her voice, upload a 10-30 second reference clip to Fish Audio and set
   `FISH_REFERENCE_ID` to the new `reference_id`.

5. **Configure `constants.py`.** Set `WORKSPACE_ROOT`, `LLM_ENDPOINT`,
   `TIMEZONE`, and `OUTPUT_DEVICE_INDEX` for your machine. Run
   `python modules/check_mics.py` to list audio device indices.

6. **Edit `lumi.yaml`.** This is where Lumi's persona lives: name, host,
   personality, lore, appearance, and dialogue.

7. **Pull and build the model.** The recommended base is Qwen3 8B.

   ```bash
   ollama pull qwen3:8b
   ollama create lumi-qwen3 -f Modelfile-qwen3
   ```

   Any tool-calling model works. For routing options, cloud providers, and
   lower-end hardware settings, see [docs/models.md](docs/models.md).

8. **VTube Studio (optional but recommended).** Install VTS, load a Live2D
   model, enable the API in VTS settings, then run `python main.py` once to
   trigger the plugin authentication prompt. If your model uses
   keyboard-bound hotkeys, edit `HOTKEY_KEYBINDINGS` in
   `modules/vtubeStudio.py` to match the model's key combos.

---

## Running

```bash
conda activate lumi
python main.py
```

You will be asked to choose audio routing (local PC, Discord voice channel, or
both). Once initialized, the terminal prints:

```
=======================================================
  Lumi AI Ready — Audio routed to: LOCAL
  Mic: active (press \ to toggle mute)
  Web client:  http://127.0.0.1:5000/
  Diagnostics: http://127.0.0.1:5001/
=======================================================
```

Three ways to interact:

- **Terminal.** Type at the prompt; `\` toggles mic mute.
- **Microphone.** Just talk; voice activity detection triggers recording.
- **Web client.** Open `http://127.0.0.1:5000/`. First visit goes to the setup
  wizard; after that you land on `/login`, then chat.

All three share the same history. A message typed at the terminal shows up in
the web chat within 1.5 seconds.

### Terminal commands

Type `!list` at any time for the full command card. The most useful ones:

| Command | Effect |
|---|---|
| `!agent_on` / `!agent_off` | Enable or disable agent mode (off by default) |
| `!agent <task>` | One-shot agent turn, returns to the current mode |
| `!state` | Show emotional state, traits, and active cognitive frame |
| `!recall <query>` | Search long-term memory for a topic |
| `!self` | Show self-model observations and session stats |
| `!model` | Show current model routing state |
| `!sleep` | Summarize the session and save continuity state |

### Agent mode example

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
  -> Exit code 0 ...

[confident] There you go. The output is real — I actually ran it.
```

The same turn can be driven from the web chat with the **Agent mode** chip.
Confirmations still happen in the terminal.

### Web client

Everything in the persona, model routing, provider keys, integrations, and
settings can be changed from the browser, including resets of chat history,
session state, self-model, and memory under **Settings, then Advanced**. See
[web/README.md](web/README.md) for the client's structure.

---

## Roadmap

Architectural phases inspired by Neuro-sama (Vedal987). The goal is not
intelligence parity — it's architectural parity. Neuro runs continuously, has
internal state, makes her own decisions, and acts without being prompted.

| Phase | Description | Status |
|---|---|---|
| 1 | Autonomous heartbeat | ✅ Complete |
| 2 | Emotional state + traits + cognitive frames | ✅ Complete |
| 3 | Continuous ambient perception | ✅ Complete |
| 4 | Episodic memory (timestamps + importance) | ✅ Complete |
| 5 | Multi-model orchestration | ✅ Complete |
| 6 | Self-model / meta-cognition | ✅ Complete |
| 6.5 | Avatar control (VTube Studio) | ✅ Complete |
| 7 | Discord voice bridge | ✅ Complete (pure Python, no sidecar) |
| 8 | External SDK (websocket plugin host) | ✅ Complete |
| 9 | Security hardening | ✅ Complete |
| 10 | Local web client | ✅ Complete |

**Phase 7** was completed in pure Python. The C# sidecar originally planned
turned out to be unnecessary once `davey` shipped a Python binding and
`discord.py` 2.7 integrated DAVE for the send path. Voice receive, speaker
attribution, group context, and the diagnostics commands are described under
[docs/discord-voice.md](docs/discord-voice.md).

**Phase 8** is complete. External applications connect to Lumi over a local
websocket on `127.0.0.1:5056`, register their own actions, push context, and
receive invocations, using a wire protocol modeled on Vedal's Neuro SDK. It was
demonstrated end to end with a working Python plugin that captures screenshots
and logs timestamped notes. See [sdk/README.md](sdk/README.md).

**Phase 10** is complete: the local web client, per-user memory and
emotional-state isolation, browser voice, and multi-provider LLM routing all
shipped. The remaining security hardening from that pass (10.17) is tracked
under Phase 9.

**Phase 9** is complete. Fourteen hardening items shipped: provenance
markers on external content, a deny-by-default SDK allowlist with per-client
capability ceilings, a command allowlist, a `realpath` file sandbox, protected
paths, secret scrubbing in logs, memory-poisoning guards, Discord rate
limiting, tool-call anomaly detection, a LAN exposure gate, and a pinned
dependency lock file. Encryption at rest is a BitLocker recommendation, not an
in-app feature. See [docs/security.md](docs/security.md) for the threat model
and incident playbook, and [docs/roadmap.md](docs/roadmap.md) for phase
status.

---

## Credits

Built as a personal project. Architecture inspired by Neuro-sama
(Vedal987).

* **VTube Model:** [Arii / Ariu (Booth)](https://booth.pm/en/items/5879420)
* **SDK protocol** modeled after Vedal's Neuro SDK (VedalAI/neuro-sdk).
* **Discord voice receive** would not be possible without `davey` by the
  Discord team and the AIRI project's TypeScript implementation, which served
  as the reference architecture.

Built using Ollama, ChromaDB, Faster-Whisper, Fish Audio, Playwright,
pyvts, aiohttp, python-socketio, ruamel.yaml, discord.py, davey, and
pyautogui.

---

## Disclaimer

Local, experimental AI companion project. The language model generates all
responses dynamically; behavior depends on the specific model and its prompt
adherence. This is a personal-use agent, not a production system. Use with
assumption of risk.
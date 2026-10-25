# Configuration

Three files control Lumi's behavior. Everything else is code.

| File | Purpose | Tracked in git? | Edited via |
|---|---|---|---|
| `.env` | Secrets and provider keys | No | Integrations page, Models → Providers, or by hand |
| `config/lumi.yaml` | Persona, traits, rules, dialogue | No (ship `.example`) | Persona page |
| `constants.py` | Paths, model names, runtime constants | Yes | Settings page, Models → Routing, or by hand |

`constants.py` also holds `DATA_ROOT`, `CONFIG_ROOT`, and `YAML_PATH`,
so every path in the codebase resolves through it. If a path ever looks
wrong, that's the first file to check.

## `.env`

Lives at the workspace root. Copy `.env.example` to start:

```powershell
Copy-Item .env.example .env
```

### Keys

```
# --- Text to speech (Fish Audio) ---
FISH_API_KEY=your_fish_audio_key
FISH_REFERENCE_ID=your_voice_reference_id

# --- Discord (optional) ---
DISCORD_BOT_TOKEN=your_bot_token
DISCORD_VOICE_CHANNEL_ID=000000000000000000
DISCORD_CHANNEL_ID=000000000000000000

# --- Cloud LLM providers (all optional) ---
# OPENAI_API_KEY=
# ANTHROPIC_API_KEY=
# GEMINI_API_KEY=
# GROQ_API_KEY=
# OPENROUTER_API_KEY=

# --- Base-URL overrides for self-hosted or proxy endpoints ---
# OPENAI_BASE_URL=
# ANTHROPIC_BASE_URL=
# GEMINI_BASE_URL=
# GROQ_BASE_URL=
# OPENROUTER_BASE_URL=

# --- Cloud throttling overrides (optional, per provider) ---
# GEMINI_RPM=5            requests per minute
# GEMINI_TPM=200000       tokens per minute
# GEMINI_RPD=100          requests per day (or "unlimited")
# GEMINI_RATE_LIMIT=off   disable throttling for this provider
# LLM_MAX_TOOL_CHARS=6000 cap on each tool result sent to a throttled
#                         OpenAI-compatible provider

# --- Remote access (see remote-access.md) ---
# LUMI_PUBLIC_ORIGINS=https://lumi.example.com   written by setup_tunnel.py
# CF_ACCESS_TEAM_DOMAIN=yourteam.cloudflareaccess.com
# CF_ACCESS_AUD=          Access application audience tag
#                         (both only read with TRUST_CLOUDFLARE_ACCESS)
```

`FISH_API_KEY` and `FISH_REFERENCE_ID` are required for voice output.
Everything else is optional. The remote-access keys, and the
`TUNNEL_PORT`, `REMOTE_ALLOW_REGISTRATION`, and `TRUST_CLOUDFLARE_ACCESS`
constants, are covered in [remote-access.md](remote-access.md).

The throttling keys work for any provider prefix (`OPENAI_`, `GROQ_`, …).
Only Gemini has built-in defaults; other providers are unthrottled unless
you set a value. Daily usage is tracked in `.rate_usage.json` next to
`.env`. See [models.md](models.md#local-and-cloud-routing).

### Writing rules

`.env` is written by an atomic, comment-preserving editor:

- Existing comments and key order survive every write
- On POSIX, the file is chmod'd to `0600` after every save
- Keys are masked (`mask_key()`) before ever reaching a UI response
- An empty string removes the key

The writer lives in `modules/llm_providers.py:write_env_var()`. Both
`integrations.py` and `llm_providers.py` use it.

### Rotating a key

1. Generate a new key at the provider's dashboard
2. Open the web client → Integrations (or Models → Providers)
3. Paste the new key, save, click **Test**
4. Delete the old key at the provider

If a key was ever committed to git, assume it's compromised even after
rotation. Purge history with `git filter-repo` before force-pushing.

## `config/lumi.yaml`

The persona file. Loaded by `constants.py:load_profile()` at import,
with `<host>` substituted for the current user's name at load time.

### Structure

| Section | What it controls |
|---|---|
| `name` | Character name |
| `greeting` | Fallback greeting |
| `personality` | Long-form personality description, values list |
| `traits` | Eight numeric traits, 0 to 1 — playfulness, intelligence, chaos, empathy, sarcasm, warmth, curiosity, patience. The shipped values lean Neuro-sama: playfulness 0.8, chaos 0.7, sarcasm 0.75, empathy 0.6 |
| `frames` | Cognitive frame descriptions — play, strategy, chaos, social, learning, threat |
| `identity` | Birthday, presented age, species, pronouns, self-description |
| `personality_modes` | Named modes (troll, unhinged, superiority, …) with descriptions and sample lines |
| `backstory` | Long-form narrative |
| `lore_notes` | List of concept/detail pairs |
| `appearance` | Visual description |
| `appearance_details` | Height, signature item, tells |
| `voice_and_speech` | Cadence, vocabulary, praise/affection style, Fish Audio tag rules |
| `relationship_to_<host>` | Relationship to the current user |
| `duo_dynamic` | "The Dev Dynamic" — how Lumi and the user work together |
| `love_language` | Primary love language and how it manifests |
| `favorites` | Music, food, drink, game, show, season, time of day, color, smell |
| `likes`, `dislikes` | String lists |
| `quirks`, `habits` | String lists |
| `daily_rhythm` | Morning, afternoon, evening, late night |
| `fears`, `insecurities` | String lists |
| `private_wishes`, `private_pride` | String lists |
| `things_she_is_bad_at` | String list |
| `signature_phrases` | Named catchphrases — vocabulary, not scripts |
| `recurring_bits` | Running jokes and habits |
| `example_dialogue` | Style reference — room-mate / tsundere |
| `example_dialogue_extra` | Vulnerability & continuity |
| `example_dialogue_duo` | Duo / partner-in-crime |
| `rules` | Behavioral rules injected as a numbered list |

### Editing

The web client's **Persona** page edits every section with form fields
plus a Raw YAML tab. Save / Revert / Reload buttons. **Preview Prompt**
renders the full system prompt from your draft without saving.

Saves are live. The cached system prompt (`_PROMPT_CACHE` in
`constants.py`) is invalidated and rebuilt on the next turn. No restart
needed.

### Comment preservation

Uses `ruamel.yaml` for round-trip. Install it:

```bash
pip install ruamel.yaml
```

Without it, the editor falls back to PyYAML and drops comments on save.
A yellow banner appears in the Persona page when this is the case.

### `<host>` substitution

The persona file uses `<host>` as a placeholder. At load time,
`load_profile(host_name)` reads the file, replaces every occurrence of
`<host>` with the actual name, and parses the result. This keeps the
persona per-user-agnostic — the same file serves any logged-in user.

If you edit the YAML directly, keep the `<host>` placeholders. Removing
them means switching users won't update the persona.

## `constants.py`

Values that never change at runtime are hardcoded. Values the user can
edit through the UI are patched in-place by `settings_manager` and
`model_manager`, both of which use a line-level regex patcher.

### Anchors

```python
WORKSPACE_ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_ROOT      = os.path.join(WORKSPACE_ROOT, "data")
CONFIG_ROOT    = os.path.join(WORKSPACE_ROOT, "config")
DOCS_ROOT      = os.path.join(WORKSPACE_ROOT, "docs")
YAML_PATH      = os.path.join(CONFIG_ROOT, "lumi.yaml")
```

`WORKSPACE_ROOT` self-locates from the file's own path, so the repo
runs from any clone location without editing.

### Model configuration

```python
MODEL_NAME        = "qwen3:8b"   # main path — think=true
FAST_MODEL_NAME   = "qwen3:8b"   # fast path — same weights, think=false
VISION_MODEL_NAME = "qwen2.5vl:latest"
GAME_AGENT_MODEL  = "qwen2.5:3b" # background game agents (Minecraft goal picking)

FAST_MODEL_ENABLED    = True
FAST_MODEL_NUM_CTX    = 8192
MAIN_NUM_CTX          = 8192
FAST_MODEL_KEEP_ALIVE = "5m"
```

Both paths point at the same weights, so Ollama keeps one copy in VRAM.
The fast path is differentiated by the `think` flag, not by the context
window. `GAME_AGENT_MODEL` is kept small on purpose: it must fit in VRAM
next to `MODEL_NAME`, or every goal pick would evict the chat model and slow
Lumi's replies. Pull it with `ollama pull qwen2.5:3b`; without it the game
agent picks goals at random. Changing `FAST_MODEL_NAME` to a cloud spec (e.g.
`groq:llama-3.3-70b-versatile`) moves all background work off-machine.

See [models.md](models.md) for the full routing story.

### LLM endpoint

```python
LLM_ENDPOINT = "http://localhost:11434"
```

Applies to every Ollama-routed call. Written by the Settings page.

### Timezone

```python
TIMEZONE = "America/New_York"
```

Used by the prompter's time anchor, the heartbeat's late-night gating,
and memory timestamps. Validated with `zoneinfo` before saving.

### Audio routing

```python
AUDIO_OUTPUT_MODE   = "both"        # "local" | "discord" | "both"
OUTPUT_DEVICE_INDEX = [24, 20]      # single int or list
```

Find device indices with:

```powershell
python scripts\check_outputs.py
```

The script prints every PyAudio output device with its index. Copy the
indices you want into `constants.py`.

For a single device:

```python
OUTPUT_DEVICE_INDEX = 24
```

For multiple (e.g. VB-Cable for VTube Studio + headset for your ears):

```python
OUTPUT_DEVICE_INDEX = [24, 20]
```

`scripts/check_mics.py` does the same for input devices.

### Network binding

```python
ALLOW_LAN_EXPOSURE = False
```

One gate for all three local services: the log hub (`:5001`), the web
client (`:5000`), and the SDK server (`:5056`). When `False`, each binds
strictly to `127.0.0.1`. When `True`, each binds to `0.0.0.0`.

Changing this value has no effect on already-running services. Restart
after toggling. Nothing warns you if you flip it — the exposure is
intentional and silent on the assumption that you meant it.

The Minecraft tools are separate processes that do not read this flag.
`plugin/mc_dashboard.py` (`:5002`), the `bot.js` control API (`:5057`) and
its skin server (`:3001`) always bind to `127.0.0.1`. The live viewer
(`:3000`, prismarine-viewer) binds to all interfaces and can't be configured
otherwise. Set `VIEWER_PORT=0` to turn it off.

### SDK server

```python
SDK_HOST    = "127.0.0.1"
SDK_PORT    = 5056
SDK_VERSION = "0.10.14"
```

The server binds strictly to `127.0.0.1` unless `ALLOW_LAN_EXPOSURE` is
`True`. Token lives at `data/memories/sdk_token.txt`, generated on first
boot. Per-client approval and capability ceilings live at
`data/memories/sdk_allowlist.json`. The allowlist is read each time a
client connects, so after editing it, restart the client. Lumi doesn't
need a restart.

### Minecraft tools

The Minecraft bot, agent and dashboard don't use `constants.py`. They are
configured with environment variables:

| Process | Where to set | Variables |
|---|---|---|
| `sdk/examples/minecraft/bot.js` | `sdk/examples/minecraft/.env` | Connection: `MC_HOST`, `MC_PORT`, `MC_USERNAME`, `MC_AUTH`, `MC_VERSION`, `LUMI_HOST`, `LUMI_PORT`, `LUMI_TOKEN_PATH`. Behavior: `MC_OWNER`, `EVENT_REACTIONS`, `AUTO_DEFEND`, `SKILL_TIMEOUT_MS`, `PATH_THINK_MS`, `LAN_DISCOVERY`. Ports and paths: `BOT_CONTROL_PORT`, `VIEWER_PORT`, `VIEWER_DISTANCE`, `VIEWER_HEAP_LIMIT_MB`, `SKIN_PORT`, `SKIN_PATH`, `HOUSE_PATH`, `VOYAGER_DIR` |
| `plugin/minecraft_bot.py` | Shell environment (Lumi passes its own when it starts the agent) | `BOT_API`, `MC_LOOP_INTERVAL`, `MC_ACTION_TIMEOUT`, `MC_GOAL_PICKER`, `MC_GOAL_MODEL`, `MC_GOAL_KEEP_ALIVE`, `MC_STATUS_INTERVAL`, `MC_NARRATE`, `MC_NARRATE_COOLDOWN` |
| `plugin/mc_dashboard.py` | Shell environment | `MC_DASH_PORT` |

The game agent's model is `GAME_AGENT_MODEL` in `constants.py` (see
[Model configuration](#model-configuration)); `MC_GOAL_MODEL` overrides it.
The Games page switch is stored in `data/minecraft/autoplay.json`, and the
world picked under **Server connection** in `data/minecraft/server.json`,
which overrides `MC_HOST` and `MC_PORT` at the next start.

Defaults and meanings are in [the Minecraft README](../sdk/examples/minecraft/README.md#configuration-env)
and [plugin/README.md](../plugin/README.md#configuration).

### Version

```python
LUMI_VERSION = "0.10.14"
```

Displayed in the web client's About tab.

### Host object

```python
HOST = _HostState()   # mutable, per-session
```

`HOST.name` and `HOST.user_id` update on web login. Modules register
listeners with `HOST.on_change(callback)` to react to user switches.
`HOST_NAME` remains as a backwards-compat proxy for any module still
reading it directly.

### Persona helpers

`build_system_prompt(host_name=None, profile_dict=None)` assembles the
full system prompt. Passing a `profile_dict` (from the persona editor's
live preview) uses that instead of reading from disk.

`get_system_prompt(host_name=None)` returns a cached prompt per host.
`invalidate_prompt_cache()` clears the cache after a persona save.

`save_persona_parsed(parsed_data)` and `save_persona_raw(raw_text)`
write through `modules.persona_editor`, then call `reload_profile()`.

### Constants file patching

Both `settings_manager.py` and `model_manager.py` patch top-level
assignments in `constants.py`:

- Only matches lines like `^NAME = ...` at column zero
- Preserves surrounding comments
- `compile()`-checks the patched file before writing
- Saves `constants.py.bak` on every write
- Rebinds live consumers so changes apply without restart

The patcher refuses to write if the result won't parse, or if the target
`NAME = ...` line isn't found.

## Reset procedures

### Full reset — nuke all runtime state

```powershell
# Stop Lumi first
Remove-Item data\* -Recurse -Force -Exclude README.md
```

Next boot recreates the folders it needs, generates a fresh SDK token,
and prompts VTS to re-authorize.

### Targeted resets

The web client's **Settings → Advanced** page has buttons for:

- Chat history (`data/logs/chat_history.json`)
- Session state (`data/memories/session_state.json`)
- Self-model (`data/memories/self_model.json`)
- Events log (`data/logs/events.log`)
- Long-term memory (current user's facts in `data/memories/chroma.db/`)

Each is confined to a server-side allowlist. The client cannot request
an arbitrary path. The memory reset wipes only the currently-logged-in
user's facts; a cross-user wipe needs the full reset above.

### Manual resets

| Goal | Command |
|---|---|
| Force VTS re-auth | `Remove-Item data\vtubeStudio_token.txt` |
| Reset Playwright state | `Remove-Item data\browser_profile -Recurse` |
| Rotate SDK token | `Remove-Item data\memories\sdk_token.txt` |
| Reset SDK approvals | `Remove-Item data\memories\sdk_allowlist.json` |
| Clear the uploaded library | `Remove-Item data\library -Recurse` |
| Force memory re-import from seed | Delete `data\memories\chroma.db\`, place `memoryinit.json` at `data\memories\memoryinit.json`, restart |
| Wipe user accounts | `Remove-Item data\memories\users.db` |
| Start a new Minecraft house | `Remove-Item data\minecraft\house.json` (turn Minecraft AI off first) |
| Stop Minecraft AI starting at boot | Turn it off on the Games page, or set `"minecraft": false` in `data\minecraft\autoplay.json` |

After any of those, restart Lumi — the affected module regenerates its
state on next boot.

## Where each config is read

| Config | Read by |
|---|---|
| `.env` | `integrations.py`, `llm_providers.py`, `tts.py`, `discordClient.py` |
| `config/lumi.yaml` | `constants.py:load_profile()` → prompt assembly |
| `constants.py` | Every module, transitively |
| `HOST` | Prompt assembly, memory filtering, emotional state per-user |

## Related docs

- [models.md](models.md) — model routing and cloud providers
- [architecture.md](architecture.md) — where each config value is used
- [data/README.md](../data/README.md) — what lives under `data/`
- [`config/lumi.yaml`](#configlumiyaml) — the persona file, documented above
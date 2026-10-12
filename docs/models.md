# Models

Lumi orchestrates several models for text, voice, hearing, and vision.
Everything routes through Ollama by default. Cloud providers are optional
and plug into the same interface.

## Large Language Model

**Model:** `qwen3:8b` via Ollama. Both `MODEL_NAME` and `FAST_MODEL_NAME`
in `constants.py` point at the same weights, so Ollama keeps exactly one
copy in VRAM.

There is no custom `lumi-qwen3` Modelfile. The persona is injected into
the system prompt at turn time from `config/lumi.yaml`, not baked into
the model. If you want a custom model with the persona embedded, write
your own Modelfile and point `MODEL_NAME` at it, but the default setup
does not need one.

### Two paths

Lumi routes each turn down one of two paths. Both use `qwen3:8b`. The
difference is the `think` flag and how the turn is classified.

| Path | `think` flag | Handles |
|---|---|---|
| Fast | `false` | Short casual chat |
| Full | `true` | Complex queries, agent turns, any turn with attachments |

Background tasks — memory reflection, session summary, self-model
reflection, heartbeat decision — all pass `think=False` and use
`FAST_MODEL_NAME`. They are structured extraction tasks, not reasoning.
Leaving reasoning on for them makes the model return empty `content`,
because it burns the entire `num_predict` budget on the internal
scratchpad.

The fast/full decision is made by `_should_use_fast_model()` in
`modules/prompter.py`. The first matching rule wins:

| Condition | Route |
|---|---|
| Agent mode active | Full |
| Message > 280 chars | Full |
| Complexity markers (`analyze`, `debug`, `step by step`, `why does`, …) | Full |
| Time / date questions | Full |
| Any attachment present | Full |
| Everything else | Fast |

The routing label prints on every turn — `Thinking (conversational,
fast)` or `Thinking (conversational, full)` — so you can see which path
handled each message. Use `!model` to check the current routing state.

### Qwen3 thinking mode

Qwen3 has an internal reasoning scratchpad that is on by default. It is
useful for complex queries and agent planning. It is pure overhead for
casual chat and actively breaks structured JSON extraction, because the
model spends the entire budget on the scratchpad and returns an empty
`content` field.

The flag is set through `chat_sync(..., think=True/False)`:

| Task | `think` |
|---|---|
| Fast conversational path | `false` |
| Full conversational path | `true` |
| Agent turns | `true` |
| Any turn with an attachment | `true` |
| Memory reflection | `false` |
| Session summary | `false` |
| Self-model reflection | `false` |
| Heartbeat decision | `false` |

## Multi-provider layer

Every LLM call in the codebase goes through `chat_sync()` in
`modules/llm_providers.py`. This is the single integration point. No
module calls Ollama directly.

```python
from modules.llm_providers import chat_sync

resp = chat_sync(
    "qwen3:8b",              # bare name → ollama
    messages,
    tools=tool_schemas,
    think=True,
    keep_alive="30m",
    options={"temperature": 0.5},
)
```

The function returns an `LLMResponse` with `.content` (str) and
`.tool_calls` (a list of `ToolCall` objects that normalize to the
Ollama tool-call shape). Call sites that used to read
`response["message"]["content"]` now read `response.content`.

### Model spec format

A model spec is either a bare name or `provider:model`. Bare names
default to Ollama.

| Spec | Parsed as |
|---|---|
| `qwen3:8b` | `("ollama", "qwen3:8b")` — the `:` is an Ollama tag |
| `lumi-qwen3` | `("ollama", "lumi-qwen3")` |
| `openai:gpt-4o-mini` | `("openai", "gpt-4o-mini")` |
| `groq:llama-3.3-70b-versatile` | `("groq", "llama-3.3-70b-versatile")` |

`parse_model_spec()` distinguishes an Ollama tag from a provider prefix
by checking the head against `PROVIDER_PRESETS`. If the head is not a
known provider, the whole string stays as an Ollama name. This is why
`qwen3:8b` does not get mistaken for a provider call.

### Supported providers

| Provider | Client class | Wire shape |
|---|---|---|
| Ollama | `OllamaClient` | `/api/chat` (native) |
| OpenAI, Gemini, Groq, OpenRouter, any OpenAI-compatible | `OpenAICompatClient` | `/v1/chat/completions` |
| Anthropic | `AnthropicClient` | `/v1/messages` |

Adding a provider is one dict entry in `PROVIDER_PRESETS`:

```python
"myprovider": {
    "label":        "My Provider",
    "kind":         "openai",   # or "anthropic" or "ollama"
    "base_url":     "https://api.myprovider.com/v1",
    "key_env":      "MYPROVIDER_API_KEY",
    "base_url_env": "MYPROVIDER_BASE_URL",
    "doc_url":      "https://myprovider.com/keys",
}
```

The Models page picks it up automatically. See
[development.md](development.md) for the full walkthrough.

API keys live in `.env`. The Models page writes them with a
comment-preserving editor that chmods the file to `0600` on POSIX and
never returns a key unmasked to the browser.

### Attachments and content blocks

Messages sent to `chat_sync()` may carry `content` as either a plain
string or a list of OpenAI-style content blocks. Each client handles
that shape natively:

| Client | Content-array handling |
|---|---|
| `OllamaClient` | Splits into `content` (text) and `images` (base64 payloads), matching Ollama's `/api/chat` shape |
| `OpenAICompatClient` | Passes the list through unchanged |
| `AnthropicClient` | Converts `image_url` blocks into Anthropic's `{type: "image", source: {…}}` shape |

In practice, the conversational path inlines attachment content as text
because the VLM has already run at upload time. The content-array shape
is kept for callers that want to pass raw images.

## Text-to-speech

**Model:** Fish Audio cloud API.

Lumi's voice comes from a Fish Audio reference clip selected by
`FISH_REFERENCE_ID` in `.env`. There is no local `voices/` directory.
The reference lives on Fish's servers, and only the reference ID is
stored locally. Audio is streamed in memory to local speakers, Discord
voice, and any browser tabs that have opted into voice playback.

Delivery is controlled by inline `[emotion]` tags in the LLM's reply.
Only canonical Fish Audio tags are passed through; invented tags are
stripped by `_strip_non_canonical_tags()` in `modules/prompter.py`. The
first canonical tag in each reply also drives the VTube Studio
expression, so voice and avatar stay in sync.

Per-language voice references are supported through `FISH_REFERENCE_IDS`
in `constants.py`. If a language code is not configured, the default
`FISH_REFERENCE_ID` is used. `_detect_language()` in `modules/tts.py`
picks the reference from the reply text.

## Speech-to-text

**Model:** `faster-whisper` (`base`, CPU, `int8`).

Used by two modules:

- `modules/mic_input.py` for the local microphone, with VAD and
  barge-in on the local input device.
- `modules/discordClient.py` for Discord voice receive. The Discord
  path downmixes incoming Opus frames to mono at 48 kHz, runs RMS VAD
  with hysteresis, resamples to 16 kHz, and passes the buffer to
  faster-whisper.

## Embeddings and memory

**Model:** ChromaDB's default embedding function. Under the hood this
is `all-MiniLM-L6-v2`, but the project never imports SentenceTransformers
directly — Chroma manages the embedding pipeline.

**Database:** `data/memories/chroma.db/`, a directory with two
collections:

| Collection | Contents |
|---|---|
| `neuro_collection` | Personal memories with `timestamp` / `importance` / `valence` / `session_id` / `user_id` |
| `library_docs` | Uploaded files and their auto-generated `.meta.md` sidecars |

Retrieval in `neuro_collection` uses a composite score:

```
0.50 * semantic_similarity
+ 0.30 * importance
+ 0.20 * recency (30-day half-life, exponential)
```

`library_docs` retrieval is a plain semantic query with no importance or
recency weighting — the content is static, so those terms do not apply.

## Vision

**Primary VLM:** `qwen2.5vl:latest` (`VISION_MODEL_NAME` in
`constants.py`).

Used by `read_image_vlm()` in `modules/agent_tools.py` to analyze
uploaded images. The function sends the image to Ollama's
`/api/generate` with a structured extraction prompt and returns a text
report with three sections: `TEXT IN IMAGE`, `DESCRIPTION`, `KEY
DETAILS`. That report is inlined into the main LLM's turn as if the user
had pasted it.

`read_image_vlm()` falls back to `moondream` when `qwen2.5vl` is not
pulled. The fallback is less accurate on dense text but still produces a
usable description.

**Secondary VLM:** `moondream`. Handles ambient screen descriptions in
`modules/ambient_perception.py` and on-demand screen capture in
`modules/multimodal.py`. Small, fast, and good enough for "is a code
editor open" — not for reading text off a screenshot.

## Model manager

`modules/model_manager.py` wraps the Ollama HTTP API:

| Endpoint | Used for |
|---|---|
| `/api/tags` | Listing installed models |
| `/api/ps` | Listing currently loaded models with VRAM usage |
| `/api/generate` | Load and unload via `keep_alive` |
| `/api/pull` | Streaming pull in a background thread |
| `/api/delete` | Removing a model |

Routing changes are written back to `constants.py` using the same
line-level patcher that `settings_manager.py` uses. The patcher:

- Matches only `^NAME = ...` at column zero
- Preserves surrounding comments
- `compile()`-checks the patched file before writing
- Saves `constants.py.bak` on every write
- Rebinds the value in every module that imported it, so changes apply
  on the next turn without a restart

The Models page in the web client is the UI for all of this: currently
loaded models, installed models, pull progress, provider keys, and the
routing form.

## Related docs

- [configuration.md](configuration.md) — where model config lives
- [architecture.md](architecture.md) — how routing decisions are made
- [development.md](development.md) — adding a provider
- [../modules/README.md](../modules/README.md) — `llm_providers.py` and `model_manager.py`
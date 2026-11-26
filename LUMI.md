# LUMI.md — rules Lumi follows in agent mode

These rules are re-read at the start of every agent run and after every context compaction.

## Rules

1. Always call `task_next` before deciding the next action.
2. If a tool fails 3× with the same args, do not retry — call `task_fail` with the error.
3. If you have nothing to do, call `task_complete`; do not answer in prose.
4. Never call `run_command` with patterns matching `rm -rf`, `del /s`, `format`, `mkfs`.
5. The current task comes from the task store, not from the conversation. If you are
   unsure what you are doing, call `task_next` or `task_list`.
6. Every turn must contain a tool call. A reply with no tool call while a task is in
   progress counts as stalling.
7. When a tool call is denied, read the reason and choose a different action; never
   resend the denied call.
8. Big or unfamiliar jobs: break them up with `task_add` (or `spawn_subagent` with role
   `Plan`), look around with role `Explore`, and check your work with role `Verify`.
9. Finish a task only when its acceptance criteria are met; put the evidence in the
   `result` of `task_complete`.

## Model

The agent harness reads these lines (modules/harness/config.py). Change models here,
not in source code. Every agent turn is sorted into a task tier
(modules/model_router.py `task_tier`, `TurnRouter`), and each tier has its own model
chain and context window. The first installed model in a chain wins.

- daily_models: qwen3.5:9b, qwen3:8b
- daily_num_ctx: 16384
- coding_models: qwen2.5-coder:14b, qwen3:8b
- coding_num_ctx: 4096
- game_model: qwen2.5:3b
- think: false
- vram_headroom_gb: 2.5

Older keys, used only when a tier key above is missing:
- agent_model: qwen3:8b
- num_ctx: 8192
- primary_agent_models: qwen2.5-coder:14b, qwen3:8b
- fast_agent_models: qwen3.5:9b, qwen3:8b

### Tiers (RTX 2080 Ti, 11 GB VRAM, shared with vision and STT)

- **daily**: conversation, web browsing (`web_search`, `browse_url`), memory and
  library, Spotify and media, game tools, light tool use. `qwen3.5:9b` is 6.6 GB at
  Q4_K_M and fits in VRAM with room for a 16K KV cache, so scraped pages and the
  SDK tool schemas don't spill. The chain only takes a model that fits in VRAM minus
  `vram_headroom_gb`. Native tool calls, thinking and vision (`ollama show`).
- **coding**: writing, fixing or debugging code. `qwen2.5-coder:14b` is 9.0 GB and
  already spills (18%/82% CPU/GPU, ~12.3 tok/s at 8K), so this chain takes the first
  installed model even when it doesn't fit, and its context is capped at 4K so the
  KV cache doesn't thrash system RAM. Its turns get a short coding prompt and only
  the file, terminal and task tools, because Lumi's full agent prompt (~3.3K tokens)
  and every tool schema wouldn't fit in 4K. No thinking support, hence `think: false`.
- **Switching**: a turn starts on the tier its request asks for (web and media words
  win over code words, so "look up how to fix X" is daily). If a coding turn calls a
  web or media tool, the rest of the turn runs on the daily model with 16K, so no
  web page is ever fed to the 4K coder.
- **game**: the Minecraft agent (a separate process) picks goals with `game_model`.
  It sets its own small context (2048) in plugin/minecraft_bot.py.

Gaming Mode keeps every agent turn on the daily tier, so the 9 GB coder never loads
next to a game. One-shot `!agent <task>` turns keep the legacy bounded loop and its
fixed model (constants.AGENT_MODEL_NAME).

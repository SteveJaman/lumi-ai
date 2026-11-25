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

The agent harness reads these lines (modules/harness/config.py). Change the model here,
not in source code.

- agent_model: qwen2.5-coder:14b
- num_ctx: 8192
- think: false
- primary_agent_models: qwen3.8:27b, qwen2.5-coder:14b, qwen3:8b
- fast_agent_models: qwen3.5:9b, qwen3:8b
- vram_headroom_gb: 2.5

Tiers (modules/model_router.py `agent_model_for_tier`): sonnet/opus agent turns and
`!auto` tasks use the first `primary_agent_models` entry that is installed in Ollama and
fits in VRAM minus `vram_headroom_gb`; haiku turns (and every turn in Gaming Mode) use
`fast_agent_models` the same way. Add `vram_gb: N` to override GPU detection. When no
chain entry is installed, `agent_model` above is used. On the 11 GB 2080 Ti both chains
resolve to qwen3:8b until a bigger card or smaller quant is available.

Selected 2026-11-22 from the fallback chain (qwen2.5-coder:14b → qwen3:14b →
qwen2.5-coder:7b → deepseek-coder:6.7b); the first candidate passed:
- `ollama run qwen2.5-coder:14b "reply with exactly the word OK and nothing else"` → `OK`
- Tool-call test (`get_time`, no args): emits a plausible JSON call
  `{"name": "get_time", "arguments": {}}` in the message text rather than as a structured
  `tool_calls` entry. The harness recovers text-form calls (`_recover_text_tool_calls`).
- 12.3 tok/s; Ollama reports 10 GB loaded, 18%/82% CPU/GPU at num_ctx 8192 (a partial
  spill on the 11 GB card). No thinking support, hence `think: false`.
- For comparison, qwen3:8b (already installed) emits structured tool calls at 31.9 tok/s
  and fits fully in VRAM; to use it set `agent_model: qwen3:8b` and remove `think: false`.

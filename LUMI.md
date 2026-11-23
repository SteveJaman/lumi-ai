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

TBD (set in Phase 2)

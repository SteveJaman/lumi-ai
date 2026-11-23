# Agent harness refactor — report

Branch `harness/autonomy-upgrade` (from `main` @ `c02c4fa`), 2026-11-22. Nothing was committed to `main`.

Lumi's agent mode used to be one 6-step loop that rebuilt its context from the last 10 chat
messages each turn. It is now a long-horizon harness in `modules/harness/`:
- a persistent task DAG
- a goal-driven loop with explicit termination reasons
- tool hooks
- tool-output truncation and archive-first compaction
- isolated Explore/Plan/Verify subagents
- declarative workflows

The full specification is in `CLAUDE.md`. Lumi's own rules and the model setting are in `LUMI.md`.

## 1. Model

**Chosen: `qwen2.5-coder:14b`**, recorded in `LUMI.md` under `## Model` (`agent_model`,
`num_ctx: 8192`, `think: false`). The harness reads the model from there; no model name is
hard-coded in harness source.

Fallback chain result: the first candidate passed, so qwen3:14b, qwen2.5-coder:7b and
deepseek-coder:6.7b were not pulled.

| Check | Result |
|---|---|
| `ollama pull qwen2.5-coder:14b` | installed, 9.0 GB |
| `ollama run … "reply with exactly the word OK and nothing else"` | `OK` |
| Tool-call test (`get_time`, no args) | `{"name": "get_time", "arguments": {}}` emitted as **message text**, not as a structured `tool_calls` entry. Passes the spec's "structured tool call (or a plausible JSON)" rule |
| Speed / memory | 12.3 tok/s; Ollama reports 10 GB loaded, **18%/82% CPU/GPU** at `num_ctx` 8192 (a partial spill on the 11 GB card with ~1.3 GB already used by the desktop) |

Caveats, with evidence:
- **Text-form tool calls.** Every tool call in the live runs came back as text, and the
  harness recovered all of them (`_recover_text_tool_calls`). The recovery layer is load-bearing
  for this model.
- **Partial CPU spill.** The model doesn't fully fit in VRAM, and it pushes the chat model,
  vision and STT off the GPU while it runs.
- **Comparison point.** `qwen3:8b` (already installed, never pulled for this) emitted structured
  tool calls in all three probes, at 31.9 tok/s, and fits fully in VRAM. See Next steps.

## 2. Commits, files and test counts

The full suite was run on a clean checkout of every commit (a git worktree with the same
`lumi` env). The only failure throughout is the pre-existing
`test_agent_tools.py::test_safe_path_rejects_absolute_outside_root`, which also fails on
`main` (`/etc/passwd` doesn't raise on Windows).

| # | Commit | Files changed | Suite (passed / failed) |
|---|---|---|---|
| — | baseline (`main` `c02c4fa`) | — | 829 / 1 |
| 1 | `16068e1` harness: scaffold — CLAUDE.md and LUMI.md | `.gitignore`, `CLAUDE.md`, `LUMI.md`, `data/tasks/baseline_tests.txt` | 829 / 1 |
| 2 | `a8fe936` harness: model config — record selected Ollama model | `LUMI.md` | 829 / 1 |
| 3 | `fd736f5` harness: mode-shape regression baseline | `tests/test_mode_shapes.py`, `tests/fixtures/mode_shapes_baseline.json` | 830 / 1 |
| 4 | `552d28b` harness: persistent task DAG — filesystem-backed store with DAG validation | `modules/harness/{__init__,config,tasks}.py`, `tests/test_harness_tasks.py` | 840 / 1 |
| 5 | `0e3932e` harness: goal-driven loop — remove 6-step cap, add text-only turn-end | `modules/harness/loop.py`, `modules/prompter.py`, `tests/test_harness_loop.py`, `tests/test_harness_prompter.py` | 857 / 1 |
| 6 | `3a4b326` harness: hook registry — PreToolUse/PostToolUse/PostToolUseFailure | `modules/harness/hooks.py`, `modules/harness/loop.py`, `modules/prompter.py`, `data/hooks/block_rm_rf.py`, `tests/test_harness_hooks.py` | 885 / 1 |
| 7 | `fe1bc0d` harness: context management — truncation and archive-first compaction | `modules/harness/context.py`, `modules/harness/loop.py`, `tests/test_harness_context.py` | 891 / 1 |
| 8 | `d109ef7` harness: subagents — Explore/Plan/Verify with isolated contexts | `modules/harness/subagents.py`, `modules/harness/loop.py`, `modules/prompter.py`, `tests/test_harness_subagents.py` | 909 / 1 |
| 9 | `6860935` harness: subagents — recover text-form tool calls (bug found in the live run) | `modules/harness/subagents.py`, `modules/prompter.py`, `tests/test_harness_subagents.py` | 910 / 1 |
| 10 | `2daa0b4` harness: workflow scripts — declarative multi-subagent orchestration | `modules/harness/workflows.py`, `modules/harness/loop.py`, `data/workflows/explore_plan_verify.json`, `tests/test_harness_workflows.py` | 914 / 1 |
| 11 | `fc6704e` harness: acceptance tests and docs | `tests/test_harness_acceptance.py`, `tests/harness_child.py`, `CLAUDE.md`, `docs/architecture.md` | 923 / 1 |

The first worktree run of commit 2 showed 6 errors and 3 skips in the Minecraft tests. The
cause was the worktree, not the code: it lacked the gitignored `data/memories/sdk_token.txt`
and `sdk/examples/minecraft/node_modules`. After adding both, the rerun gave 829 / 1, and every
count above comes from the fixed worktree.

## 3. Acceptance criteria

Scripted tests use a fake model that replays a fixed sequence of responses, so they are
deterministic. "Live" runs use `qwen2.5-coder:14b` through Lumi's own `chat_sync`, with
sandboxed in-memory tools (or real read-only file tools for Explore).

| # | Criterion | Result | Evidence |
|---|---|---|---|
| 1 | 20-step synthetic task completes; `MAX_AGENT_STEPS` no longer caps runs | **PASS** | `test_ac1_twenty_step_task_not_capped`: 20 steps > `MAX_AGENT_STEPS` (6), `task_complete`. **Live:** 20 model calls, `task_complete`, 16/16 notes correct, 69 s. Prompter path: `test_agent_mode_runs_harness_past_six_steps` (12 calls through `Prompter.run_agent_turn`) |
| 2 | Kill process mid-task; restart; agent resumes from the persisted DAG | **PASS** | `test_ac2_kill_and_resume`: a child process is hard-killed (`Popen.kill`) while blocked in a model call. The store then reads `{1: done, 2: in_progress, 3: pending, 4: pending}`. A new process given "continue" sees `#2 sub A` as its first task-state message and finishes all 4 tasks with `task_complete` |
| 3 | A `PreToolUse` hook denying `run_command` blocks the call in YOLO mode | **PASS** | `test_ac3_…`: `LUMI_AGENT_YOLO=1`, hook file in the hooks dir, real `Prompter._run_tool`. `execute_tool` was never called and no approval was asked. The denial reason reached the next model turn. Also: `block_rm_rf` blocks `rm -rf build` while `echo safe` is auto-approved, and without YOLO, approval is still requested |
| 4 | 90% context fill → compaction fires, archive file exists | **PASS** | `test_ac4_…`: history at ≈90% of 8192 tokens. `compactions ≥ 1`. `transcripts/ac4.jsonl` exists and holds the full uncompacted message. The model call itself was under 80% of the window |
| 5 | Explore: main context grows by ~1 message, not the subagent transcript | **PASS** | `test_ac5_…`: the subagent made 4 tool calls plus an answer. The main history gained exactly `[assistant tool-call, tool result]`, and the result is the subagent's final text only. **Live (after fix):** Explore ran `search_files` and the main agent answered `modules\harness\loop.py`. `subagents.log` row written; main history ended at 9 messages |
| 6 | 30-step synthetic task ends with `termination_reason = task_complete` | **PASS** | `test_ac6_thirty_step_task_complete`: 30 steps, `task_complete` |
| 7 | Two consecutive text-only turns → `model_stalled` | **PASS** | `test_ac7_…`: `model_stalled`, logged in `sessions.log`. `test_two_text_only_turns_stall` checks that exactly one "Continue with the next concrete action…" re-prompt sits between them |
| 8 | `run_command` emitting 50K chars → 4K in context; full output in `tool_outputs/` | **PASS** | `test_ac8_…`: 50,000-char output. The context holds 4,000 chars plus `[truncated; 46000 chars omitted]`. `tool_outputs/ac8/run_command_1.log` is byte-identical to the original. Also covered: last 200 lines kept (`test_run_command_keeps_last_200_lines`) |
| 9 | All baseline-passing tests still pass | **PASS** | Every commit: 1 failure, the pre-existing one; passed count only grew (829 → 923) |
| 10 | Chat-mode and plan-mode turns keep their message-history shapes | **PASS** | `tests/test_mode_shapes.py` compares against a snapshot captured from the pre-refactor `prompter.py` (commit 3, before any prompter change). It covers chat mode (`conversational_request`) and the legacy one-shot agent turn (message roles/keys, option keys, offered tool names). Green at every later commit |

### Criteria that are not fully verified

- **"Plan mode" is my interpretation.** Lumi has no plan mode, so the regression test uses the
  bounded one-shot agent turn (`!agent <task>`, or `LUMI_HARNESS=0`) as "plan mode", as
  proposed in Phase 0. If you meant something else, AC10 doesn't cover it.
- **AC2 and AC3 use scripted models, not live ones.** The kill/restart and YOLO-denial paths
  are model-independent, so they were tested deterministically.
- **No end-to-end run with the full persona prompt or the web/island approval UI.** The live
  runs used a short system prompt and sandboxed tools. The real `!agent_on` session has not
  been exercised with qwen2.5-coder:14b.

## 4. What changed, briefly

- `modules/harness/tasks.py`: `data/tasks/lumi_tasks.json`, atomic writes, validates on every
  write (cycles, unknown ids). Failing a task blocks its dependents. Tools: `task_list`,
  `task_add`, `task_next`, `task_complete`, `task_fail`.
- `modules/harness/loop.py`: `AgentHarness`.
  - The current task comes from the store in an ephemeral per-call message, never stored in
    history.
  - Termination reasons: `task_complete`, `task_failed`, `turn_complete`, `model_stalled`,
    `stagnant`, `safety_cap` (200), `time_budget` (2 h), `error`. Each is logged to
    `data/tasks/sessions.log`.
- `modules/harness/hooks.py`: `data/hooks/*.py` registry.
  - A PreToolUse denial wins over YOLO; a crashing PreToolUse hook blocks the call.
  - Failures are counted per tool+args, with a notice at 3×.
  - Ships `block_rm_rf.py`, which covers rm -rf, del /s, rmdir /s, format, mkfs,
    Remove-Item -Recurse, diskpart and more.
- `modules/harness/context.py`: 4K / 200-line truncation with full logs. Archive-first
  compaction at 80% of `num_ctx`, sized to land at about 60%, re-injecting the `LUMI.md` rules.
- `modules/harness/subagents.py`: Explore, Plan and Verify with role allowlists.
  - Verify's `run_command` is limited to an allowlist of read-only commands.
  - Logged to `data/tasks/subagents.log`.
- `modules/harness/workflows.py` plus `data/workflows/explore_plan_verify.json`.
- `modules/prompter.py`:
  - `run_agent_turn` hands session agent mode to `_run_harness_turn`; the legacy loop is
    unchanged.
  - Opt-in `LUMI_AGENT_YOLO`: skips approval only for `write_file`, `run_command` and
    `run_in_terminal`, and only during harness runs.
  - `_recover_text_tool_calls` shares the legacy text-call recovery chain.
- Not touched: `sdk/` protocol, `sdk/examples/minecraft/`, `plugin/minecraft_bot.py`, chat mode.

## 5. Recommended next steps

1. **Try `qwen3:8b` or `qwen3:14b` as `agent_model`.**
   - `qwen3:8b` gives structured tool calls, runs about 2.6× faster and fits in VRAM. Switching
     is one line in `LUMI.md` (remove `think: false`).
   - Then rerun `python scripts/harness_live_check.py synthetic` and `explore`, and keep whichever completes more
     live tasks.
2. **Run a real `!agent_on` session** with the persona prompt and the web approval UI. Watch
   the termination reasons in `data/tasks/sessions.log`.
3. **Add task-store controls** for stale work. Open tasks from a stopped run are resumed
   first, even if you've moved on.
   - Add `!tasks` (list) and `!tasks_clear` commands.
   - Or show all open tasks in the task-state message so the model can `task_fail` stale ones.
4. **Speed up `search_files`.** A workspace-wide search took about 100 s in the live Explore
   run because it walks `node_modules`, `third_party` and so on. Skip ignored folders.
5. **Wire the Mindcraft/Minecraft plugin through this harness:** goals as task-DAG entries,
   game actions behind PreToolUse hooks next to the existing governor, and a Verify step
   reading game state. Per the scope rule, ask before touching `plugin/minecraft_bot.py`.
6. Consider a model-written compaction summary (via a Plan-style subagent) once the model is
   reliable. The current summary is deterministic, which is safer for a 14B model but loses
   nuance.

| 12 | (this commit) harness: refactor report | `data/tasks/REFACTOR_REPORT.md`, `scripts/harness_live_check.py` | docs/script only; acceptance re-run on `fc6704e`: 10/10 passed |

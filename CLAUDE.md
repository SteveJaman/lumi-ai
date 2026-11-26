# CLAUDE.md — working on Lumi

Lumi is a local AI companion (voice, VTuber avatar, games, tools) written in Python.
`main.py` boots the modules; `modules/prompter.py` runs every model turn. This file is
what a Claude Code session needs to work on the repo cold. Read it fully before editing.

## Environment

- Windows 11, RTX 2080 Ti (11 GB VRAM, shared by Ollama, vision, STT and games).
- Python: the **`lumi` conda env**, Python 3.11. `python` on PATH is a *different*
  interpreter (3.12, system). Always call it explicitly:
  `C:/Users/anguy/anaconda3/envs/lumi/python.exe`
- Models are served by local Ollama at `http://127.0.0.1:11434` (not `localhost`).
- Run the full suite (≈4 min):
  `C:/Users/anguy/anaconda3/envs/lumi/python.exe -m pytest tests -q -p no:cacheprovider -rfE`
- Pre-harness baseline (recorded in `data/tasks/baseline_tests.txt`): **829 passed,
  1 failed**. The harness added `tests/test_harness_*.py` and `tests/test_mode_shapes.py`.
- Current count, after the harness merge: **923 passed, 1 failed**.
  The one known failure is `tests/test_agent_tools.py::test_safe_path_rejects_absolute_outside_root`
  (`/etc/passwd` doesn't raise on Windows). It is pre-existing; it does not block commits.

## Repo map (the parts that matter for the agent)

| Path | What it is |
|---|---|
| `modules/prompter.py` | Every turn. `run_conversational` (chat mode), `run_agent_turn` (agent mode), `_run_tool` (tool dispatch + approval gate), `_confirm` (HITL approval) |
| `modules/agent_tools.py` | Built-in tools (`read_file`, `write_file`, `list_dir`, `search_files`, `run_command`, `run_in_terminal`), `CONFIRM_REQUIRED_TOOLS`, `_safe_path` sandbox |
| `modules/llm_providers.py` | `chat_sync(model, messages, tools=…)` → `LLMResponse` for Ollama / OpenAI-compatible / Anthropic |
| `modules/hitl_server.py` | Approval UI bridge (web/island); terminal y/n fallback |
| `modules/harness/` | The autonomous agent harness (spec below) |
| `sdk/`, `modules/sdk.py` | External SDK (Neuro-SDK wire format). **Do not change the protocol** |
| `plugin/` | SDK client processes (reference plugin, Minecraft agent, Voyager submodule) |
| `data/memories/` | ChromaDB memories, users.db, session state (gitignored) |
| `data/tasks/`, `data/hooks/`, `data/workflows/` | Harness persistence, hooks, workflow scripts |
| `LUMI.md` | Lumi's own behavioral rules + the `## Model` config the harness reads |

## Operational rules (mandatory)

1. **Branch discipline (tiered).** Changes to the agent harness (`modules/harness/`,
   the harness paths in `modules/prompter.py`, `data/hooks/`, `data/workflows/`, `LUMI.md`)
   go on a branch and are merged once the suite is green. Everything else (docs, fixes,
   other modules) may be committed straight to `main`. Either way, run the full suite
   before committing.
2. **Test discipline (tiered).** While iterating, run *only* the specific test file you
   touched (e.g., `pytest tests/test_harness_tasks.py`). Run the full 4-minute suite
   **only once** right before a final commit. If any test that passes in
   `data/tasks/baseline_tests.txt` fails, stop and report before doing anything else.
3. **Scope discipline.** Do not touch `sdk/examples/minecraft/` or
   `plugin/minecraft_bot.py`. Ask first if a change seems to need them.
4. **Model discipline.** Model names and context sizes for the agent harness come
   from the `## Model` section of `LUMI.md` (parsed by `modules/harness/config.py`),
   never hard-coded in harness source. `constants.py` values are only the fallback.
5. **Code style.** Every new agent tool has a description (docstring) under two
   sentences and a JSON schema — the local model reads these to decide when to call it.
6. New persistence lives under `data/tasks/` and `data/hooks/` (and declarative
   workflows under `data/workflows/`) only.

## Claude Token-Saving & Routing Rules (For Claude Developer Only)

1. **Use `haiku` sub-agents for noisy work.** Whenever you need to search the codebase, read large test logs, or inspect `data/tasks/sessions.log`, spawn a sub-agent using the `haiku` model and have it return only a 3-bullet summary. Never dump raw log files into the main chat.
2. **Stay on `sonnet` for coding.** Do all file edits, bug fixes, and isolated `pytest` runs on `sonnet`. Only ask to escalate to `opus` if designing a brand-new module in `modules/harness/` or debugging a multi-file race condition.
3. **Never run the full test suite until the very end.** While iterating, run only the specific test file you touched. Run the full suite once right before committing.

## Harness specification

The agent mode used to be a 6-step, single-agent loop whose context was rebuilt from the
last 10 chat messages each turn. The harness replaces it with a long-horizon loop
architecturally like Claude Code. The local model is 14B-class and **will** misbehave:
answer in prose instead of calling a tool, repeat failed actions, forget the plan. Each
behavior below exists to counter one of those failure modes; none is optional.

Routing: session agent mode (`!agent_on`) runs the harness
(`Prompter._run_harness_turn`). One-shot agent turns (`!agent <task>`, a Minecraft
mention while the game is on) keep the bounded legacy loop in `run_agent_turn`; that
legacy path is the "plan mode" the shape regression test covers. `!agent <task>` stays
on the legacy bounded loop on purpose; do not route it through the harness.

Switches (environment, read at call time):
- `LUMI_HARNESS=0` — fall back to the legacy loop (kill switch; default is on).
- `LUMI_AGENT_YOLO=1` — skip the human approval prompt for `write_file`, `run_command`
  and `run_in_terminal` while the harness runs. `PreToolUse` hooks still run first and
  can deny. Email, calendar, finance, desktop and Claude Code tools always ask.
- `LUMI_HARNESS_MAX_STEPS`, `LUMI_HARNESS_TIME_BUDGET_S` — override the safety cap / budget.

Local-model compensation: qwen2.5-coder writes tool calls as JSON text, not structured
`tool_calls`. Both the main loop and subagents run `_recover_text_tool_calls`
(prompter.py) on text-only replies before treating them as text.

Open tasks persist across runs, but a new request comes first: it is added as a task
and every task still open from earlier runs is set to `parked`, so a stalled
"resume my spotify music" can't hijack "find a restaurant". Parked tasks aren't picked
by `task_next` and don't keep a run going. "continue" adds nothing: it un-parks them
(back to `pending`) and resumes.

### Change 1 — Persistent task DAG (`modules/harness/tasks.py`)
- Store: `data/tasks/lumi_tasks.json`, atomic writes (temp file + `os.replace`) under a lock.
- Task: `{id, description, status: pending|in_progress|done|failed|blocked|parked,
  depends_on: [ids], acceptance_criteria, result, attempts, last_error}`.
- Tools: `task_list`, `task_add`, `task_next`, `task_complete(id, result)`, `task_fail(id, reason)`.
- `task_next` returns the in-progress task if there is one (resume), else the first
  pending task whose dependencies are all `done`, and marks it `in_progress`.
- Every write validates the DAG: rejects cycles and unknown dependency ids. A task whose
  dependency failed becomes `blocked`.
- The loop reads the current task from the store, never from conversation context.

### Change 2 — Goal-driven loop (`modules/harness/loop.py`)
- `MAX_AGENT_STEPS` is no longer a limit (it remains only for the `LUMI_HARNESS=0` legacy path).
- Termination, checked every cycle:
  a. `task_next` is empty after a `task_complete`/`task_fail` → `task_complete`
     (`task_failed` if the last resolution was a failure).
  b. Text-only turn (zero tool calls, after text-form tool-call recovery): with a task
     `in_progress`, re-prompt once (`CONTINUE_PROMPT`): if the request is done, call
     `task_complete` with the message to the user in `result` (that is the reply the
     user sees); otherwise take the next concrete action or `task_fail`. Two
     consecutive text-only turns → `model_stalled`.
     Exception (b'): when a non-task tool already succeeded for the open task this
     run, a text reply finishes that task with the text as the reply (counted in
     `finished_by_reply`): at once if it reads as finished, else on the second text
     reply instead of stalling. A reply that promises more work ("let me…", "I'll…"),
     asks the user a question, or copies one of the harness's own status lines
     ("(I stopped: …)") gets the re-prompt first. When that text reply came right after a
     real work tool (not a task tool), the re-prompt is `TOOL_THEN_TEXT_PROMPT`, a system
     note: "Tool executed. If the task is done, you MUST call `task_complete`…".
     With no task in progress, a text turn ends the run → `turn_complete`.
  c. No progress in 5 consecutive cycles → `stagnant`. Progress is a mutating tool, or any
     other tool call that succeeded and isn't just looking (`spotify_play`,
     `click_button`…; reads, searches and "now playing" don't count; `is_progress`).
  d. `max_safety_steps = 200` → `safety_cap`.
  e. Wall-clock budget (default 2 h) → `time_budget`.
- Every termination is logged (session id, reason, steps, duration) to `data/tasks/sessions.log`.
- Task isolation: when `task_next` moves the run from one task to a different one, the
  transcript is archived and the window is cut to the system prompt, the `LUMI.md` rules
  and a brief of the new task, so an earlier task (and the chat history it came with)
  can't steer it. The run's first task keeps its chat context.
- Model calls: each may take `agent_timeout_s` (`LUMI.md`, default 300). A timeout is
  retried once on the same model; a second one ends the run (`error`) with a clear
  message. Never a smaller model.

### Change 3 — Hook registry (`modules/harness/hooks.py`)
- Events: `PreToolUse`, `PostToolUse`, `PostToolUseFailure`.
- Hooks are Python files in `data/hooks/*.py`. A file defines any of
  `pre_tool_use(event)`, `post_tool_use(event)`, `post_tool_use_failure(event)`.
  `event = {"tool", "args", "session_id", "result"?, "error"?}`.
- `PreToolUse` returning `{"deny": true, "reason": "..."}` blocks the call, even in YOLO
  mode; the reason goes back to the model as the tool result.
- `PostToolUseFailure`: the registry counts failures per tool+args (and, for a
  `run_command`/`run_in_terminal` refused as "not on the command allowlist", per
  program, so `ipconfig` and `ipconfig /all` count together). At 2 it injects
  "STOP TRYING THIS COMMAND. … You must use a different tool or tell the user you cannot
  do this", and a further identical call is denied before it runs (the stop message is
  the tool result).
- Hooks run outside model context (zero tokens). Shipped: `data/hooks/block_rm_rf.py`.

### Change 4 — Context management (`modules/harness/context.py`)
- Tool output is truncated before entering history: max 4,000 chars plus
  `\n[truncated; N chars omitted]`; `run_command` keeps only the last 200 lines first.
  The full output goes to `data/tasks/tool_outputs/<session>/<tool>_<n>.log`.
- Before each model call, history tokens are estimated (chars/4). At 80% of the model's
  context window (`num_ctx` from `LUMI.md`), older turns are compacted into a structured
  block: current task, decisions, open questions, last 3 tool outputs, `LUMI.md` rules.
- Archive first: the full transcript is appended to `data/tasks/transcripts/<session>.jsonl`
  before compacting. `LUMI.md` is re-injected after compaction.

### Change 5 — Subagents (`modules/harness/subagents.py`)
- `spawn_subagent(role, prompt, tools_allowed, timeout)`: fresh history, own tool
  allowlist, returns only the final response (one tool message in the parent).
- Roles: `Explore` (`read_file`, `search_files`, `list_dir`) → file map + snippets;
  `Plan` (`read_file`, `search_files`, `task_add`, `task_list`) → task DAG as JSON;
  `Verify` (`read_file`, read-only `run_command`) → `{"pass": bool, "evidence": str}`.
- "Read-only `run_command`" (`command_is_read_only` in `subagents.py`):
  - **No output redirection or substitution:** any `>`, `tee`, backtick or `$(` rejects the
    whole command.
  - **No mutating commands:** every segment of a compound command (split on `&&`, `||`,
    `;`, `|`) must start with an allowlisted read-only command:
    - git `status`, `diff`, `log`, `show`, `branch`, `rev-parse`, `ls-files`
    - `pytest`, or `python -m` with `pytest`, `pyflakes`, `py_compile` or `compileall`
    - `ls`, `dir`, `type`, `cat`, `head`, `tail`, `wc`, `find`, `findstr`, `grep`, `rg`,
      `where`, `which`, `echo`
    - pip `list`, `show`, `freeze`; `node --version`; npm `ls`, `test`
    - `Get-ChildItem`, `Get-Content`, `Select-String`, `Test-Path`
  - Anything else (installs, `git push`, deletes, running arbitrary scripts) is denied
    before dispatch.
- Every call is logged to `data/tasks/subagents.log` (role, prompt hash, duration, tokens).

### Change 6 — Workflow scripts (`modules/harness/workflows.py`)
- `data/workflows/*.json`: ordered subagent steps with `{{inputs.x}}` / `{{steps.id}}`
  bindings and optional `until` / `max_iterations` loops. Tool: `workflow_run(name, inputs)`.

### Scope constraints
- Changes 2–4 apply only to agent mode. Chat mode (and the legacy agent loop) keep their
  message-history shapes; `tests/test_mode_shapes.py` asserts this against a snapshot.
- Do not change the SDK plugin protocol. Do not auto-approve destructive `run_command`;
  hooks gate it. Do not remove the approval-gate UX; extend it.
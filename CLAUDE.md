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
- Run the suite (≈4 min):
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
2. **Test discipline.** After every commit, re-run the full suite. If any test that
   passes in `data/tasks/baseline_tests.txt` now fails, stop and report before doing
   anything else.
3. **Scope discipline.** Do not touch `sdk/examples/minecraft/` or
   `plugin/minecraft_bot.py`. Ask first if a change seems to need them.
4. **Model discipline.** Model names and context sizes for the agent harness come
   from the `## Model` section of `LUMI.md` (parsed by `modules/harness/config.py`),
   never hard-coded in harness source. `constants.py` values are only the fallback.
5. **Code style.** Every new agent tool has a description (docstring) under two
   sentences and a JSON schema — the local model reads these to decide when to call it.
6. New persistence lives under `data/tasks/` and `data/hooks/` (and declarative
   workflows under `data/workflows/`) only.

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

Open tasks persist across runs: a new request is added as a task, but an
`in_progress` task from an earlier (crashed or stopped) run is resumed first.
"continue" adds nothing and just resumes.

### Change 1 — Persistent task DAG (`modules/harness/tasks.py`)
- Store: `data/tasks/lumi_tasks.json`, atomic writes (temp file + `os.replace`) under a lock.
- Task: `{id, description, status: pending|in_progress|done|failed|blocked,
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
     With no task in progress, a text turn ends the run → `turn_complete`.
  c. No mutating tool call in 5 consecutive cycles → `stagnant`.
  d. `max_safety_steps = 200` → `safety_cap`.
  e. Wall-clock budget (default 2 h) → `time_budget`.
- Every termination is logged (session id, reason, steps, duration) to `data/tasks/sessions.log`.

### Change 3 — Hook registry (`modules/harness/hooks.py`)
- Events: `PreToolUse`, `PostToolUse`, `PostToolUseFailure`.
- Hooks are Python files in `data/hooks/*.py`. A file defines any of
  `pre_tool_use(event)`, `post_tool_use(event)`, `post_tool_use_failure(event)`.
  `event = {"tool", "args", "session_id", "result"?, "error"?}`.
- `PreToolUse` returning `{"deny": true, "reason": "..."}` blocks the call, even in YOLO
  mode; the reason goes back to the model as the tool result.
- `PostToolUseFailure`: the registry counts failures per tool+args; at 3 it injects
  "Tool X failed 3× with these args; try a different approach."
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

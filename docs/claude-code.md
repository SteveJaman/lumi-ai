# Claude Code as a tool for Lumi

In agent mode Lumi can hand a coding task to **your installed Claude Code**
and get the answer back. It runs on your own Claude login, so it uses your
plan instead of pay-per-token API billing, and Lumi never sees or copies your
login: she just starts the official `claude` program.

This is a tool (`ask_claude_code`), not a replacement for her chat model. Her
everyday models stay local (see [models.md](models.md)); Claude Code is for the
moments a real coder is worth using: writing or fixing a script, reviewing or
explaining code, planning a change.

## Setup

1. Install Claude Code (`npm install -g @anthropic-ai/claude-code`).
2. Run `claude` once in a terminal and sign in with your Claude account.
3. Turn on agent mode in Lumi (`!agent_on`). That's it. The tool is offered to
   her automatically while `claude` is found. To remove it, set
   `CLAUDE_CODE_ENABLED = False` in `constants.py`.

## What happens when she uses it

1. Lumi writes a task and picks a mode.
2. **You approve it.** The prompt shows the whole task, the folder, and the
   mode, through the same approval flow as `run_command` (the island, or the
   terminal). Both modes ask every time. A task is limited to 3,000
   characters so none of it is cut off in the prompt.
3. Claude Code runs headless in the project folder and Lumi receives its
   answer (up to 6,000 characters), wrapped as `[FROM CLAUDE CODE]` data.

| Mode | Tools Claude Code has | Use it for |
|---|---|---|
| `read_only` (default) | Read, Grep, Glob | Explaining, reviewing, planning |
| `edit` | Read, Grep, Glob, Edit, Write | Creating or changing files |

Neither mode has a shell or web access. Those tools are removed from the run,
not just discouraged.

## What it can't touch

Whatever Lumi's own file tools refuse, Claude Code is refused too, in both
modes, so delegating can't be used to get around those protections:

- **Never readable:** `.env`, `data/credentials/` (your sign-ins),
  `data/workspace/` (cached mail), `data/memories/`, `data/API/`.
- **Never editable (edit mode):** all of the above plus Lumi's own code
  (`modules/`, `sdk/`, `web/`, `main.py`, `constants.py`, `signals.py`) and her
  persona files.

To change Lumi's own code, use Claude Code yourself in your editor, as you do
now. The rules are built from `agent_tools.PROTECTED_*`, so the two lists can't
drift apart, and they were verified against the real CLI: a file planted in
the credentials folder could not be read, and writes into `modules/` and
`data/credentials/` were refused, while an ordinary file was created.

## Limits and safety details

- **Your plan, not an API key.** The child process has `ANTHROPIC_API_KEY`,
  `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_BASE_URL` removed from its
  environment, so a key in Lumi's `.env` can never quietly switch it to
  pay-per-token.
- **Usage counts against your plan.** Lumi is capped at 8 runs an hour, one at
  a time, 15 steps and 10 minutes per run.
- **Nothing in the task can run as a command.** The task goes in on stdin and
  `claude.exe` is started directly, never through `cmd.exe`.
- **Where your code goes.** The task and the files Claude Code reads are sent
  to Anthropic, as with any Claude Code use.
- **Prompt injection.** Text Lumi reads (a web page, an email) could try to
  talk her into delegating something. That is why every call is approved by
  you and why the protected paths are denied regardless.

## Troubleshooting

| You see | Fix |
|---|---|
| "Claude Code isn't installed" | Install it (step 1) and restart Lumi |
| "Claude Code isn't signed in" | Run `claude` in a terminal and log in |
| "ran out of steps" | Give her a smaller task |
| "already used 8 times this hour" | Wait, or do it in your editor |
| The tool never appears | Agent mode is off, `CLAUDE_CODE_ENABLED` is `False`, or `claude` isn't found |

The code is `modules/claude_code.py`; tests are `tests/test_claude_code.py`.

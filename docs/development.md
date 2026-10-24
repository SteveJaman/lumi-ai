# Development

How to set up a working environment, run tests, and extend Lumi.

## Getting set up

### Prerequisites

- Python 3.11+
- Conda (recommended) or venv
- Ollama running locally
- FFmpeg on PATH (for Discord voice)
- Playwright Firefox (for browser tools)
- Node.js 18+ (only for the Minecraft bot in `sdk/examples/minecraft/`)

### Clone and install

```powershell
git clone --recurse-submodules https://github.com/SteveJaman/lumi-ai.git Lumi-master
cd Lumi-master
# Already cloned without submodules? Run: git submodule update --init

conda create -n lumi python=3.11
conda activate lumi

pip install -r requirements.txt
pip install -r requirements-dev.txt     # pytest, ruff, mypy
playwright install firefox
```

### Configure

```powershell
Copy-Item .env.example .env
Copy-Item config\lumi.example.yaml config\lumi.yaml
```

Edit both. Minimum for a working instance:

- `FISH_API_KEY` and `FISH_REFERENCE_ID` in `.env` for voice
- `TIMEZONE` and `OUTPUT_DEVICE_INDEX` in `constants.py`

`WORKSPACE_ROOT` self-locates from `constants.py`, so you don't edit it.

Find your audio device indices:

```powershell
python scripts\check_outputs.py    # output devices
python scripts\check_mics.py       # input devices
```

### Pull the model

```powershell
ollama pull qwen3:8b
```

`MODEL_NAME` and `FAST_MODEL_NAME` in `constants.py` both point at
`qwen3:8b`. The fast path uses the same weights with a smaller context
window and thinking disabled. Any tool-calling model works.

For cloud providers (OpenAI, Anthropic, Gemini, Groq, OpenRouter), set
the key in `.env` or use the Models → Providers page. See
[models.md](models.md).

### Verify

```powershell
python scripts\check_system.py
```

This prints Python version, key package versions, GPU info, and whether
ports 5000/5001/5056 are free. It does not import `constants.py`, so it
runs before the app's own imports resolve — the first thing to run on a
new machine.

## Running

```powershell
python main.py
```

Pick an audio mode (1 for local, 2 for Discord, 3 for both). Once the
banner appears, four surfaces are live:

- **Terminal** — type directly, `\` toggles mic mute
- **Web client** — `http://127.0.0.1:5000/`
- **Diagnostics** — `http://127.0.0.1:5001/`
- **SDK server** — `ws://127.0.0.1:5056/ws`

All of them share `signals.history`. A message typed at the terminal
shows up in the web chat within 1.5 seconds.

## Project layout

```
Lumi-master/
├── main.py                  Entry point
├── constants.py             Config, paths, HOST, persona loader
├── signals.py               Shared state object
├── modules/                 Runtime — all background loops and I/O
├── web/                     Local HTML/CSS/JS client
├── sdk/                     External SDK (server + client library)
│   └── examples/minecraft/  Node.js mineflayer bot (npm install here)
├── plugin/                  SDK clients: reference plugin, Minecraft agent + dashboard
│   └── voyager/             MineDojo/Voyager submodule (read-only)
├── config/                  Editable config (.example files tracked)
├── docs/                    Deep documentation
├── scripts/                 Standalone utilities
├── tests/                   Pytest suite + standalone harnesses
├── assets/                  Branding, screenshots
└── data/                    Runtime state (gitignored except README)
```

See [architecture.md](architecture.md) for how the modules fit together.

## Test suite

Two kinds of tests. Run them differently.

### Pytest suite

```powershell
pytest tests\ -v
```

Runs offline, no external services. Covers pure logic only.

| File | What it tests |
|---|---|
| `tests/test_sdk_protocol.py` | Wire format builders, `ActionSpec` roundtrip |
| `tests/test_llm_providers.py` | `parse_model_spec`, `make_model_spec`, `mask_key` |
| `tests/test_agent_tools.py` | `_safe_path` sandbox — refuses escapes from `WORKSPACE_ROOT` |

### Standalone harnesses

Not pytest tests. Run them directly with `python`.

```powershell
python scripts\check_system.py       # machine pre-flight
python tests\test_debug.py           # whole-project health
python tests\test_all_features.py    # end-to-end feature smoke test
python tests\test_comprehensive.py   # integration & hardware
```

| Harness | Sections | Runs offline? |
|---|---|---|
| `scripts/check_system.py` | Python, folders, packages, ports, Ollama, FFmpeg, Playwright, GPU, `.env`, `lumi.yaml` | Yes (skips live services gracefully) |
| `tests/test_debug.py` | 14 sections — workspace, syntax, imports, constants, persona YAML, signals, agent tools, Ollama, storage, ports, config files, module shape, smoke tests, optional deps | Yes (Ollama check is a 2s ping) |
| `tests/test_all_features.py` | Every major subsystem, including the path sandbox, shell guards, provenance wrapping, and secret redaction; unconfigured optional services SKIP unless `LUMI_STRICT=1` | Yes |
| `tests/test_comprehensive.py` | Audio routing, database locks, library RAG, prompt assembly, tool dispatch parity | Partially — needs PyAudio, ChromaDB, Ollama for full coverage |

`tests/README.md` documents these plus `test_performance.py`,
`test_vtube_motion.py`, and the pytest suite.

### Checking the web client

The browser UI has no automated tests. To check layout and theming
without the full backend, serve `web/` from a throwaway aiohttp app that
returns canned JSON for `/auth/me`, `/api/status`, `/api/history`, and the
routes of the page under test, then drive it with Playwright (`channel="chrome"`
uses the installed Chrome). Screenshot light and dark at desktop width and at
≤ 760px, and watch the console for `pageerror` events. Socket.io 404s are
expected against a mock.

### What belongs in a test

Modules with **pure logic** — functions that take inputs and return
outputs without touching the network, filesystem, or global state. The
easy candidates:

- `sdk/protocol.py` — message builders
- `sdk/actions.py` — action/param builders
- `modules/llm_providers.py` — spec parsing, key masking
- `modules/agent_tools.py` — path sandbox

### What doesn't belong

Anything that needs a live service. Ollama, ChromaDB, Discord, and VTS
are not mocked. The way to test those is to run Lumi directly and
observe the behavior — or add integration tests that spin up a fake
service, which is out of scope for now.

### Adding a test

Create `tests/test_<module>.py`. Use pytest conventions:

```python
def test_something():
    assert compute(2, 3) == 5

def test_error_case():
    with pytest.raises(ValueError):
        compute(-1, 0)
```

Import from `modules.*` or `sdk.*` — the test runner adds the workspace
root to `sys.path` automatically.

## Linting and formatting

```powershell
ruff check .                  # lint
ruff format .                 # format (in place)
ruff check --fix .            # lint + auto-fix
```

Config lives in `pyproject.toml`. Line length is 88 (Black-compatible).
Rules: `E`, `F`, `W`, `I` (import order), `UP` (pyupgrade), `B`
(bugbear).

The formatter is not mandatory — you can write in your own style. If you
do run it, do so in a separate commit from any functional change so
reviewers can see what actually changed.

### Import order

Standard library, blank line, third-party, blank line, local. Ruff's
`I` rule enforces this. Groups:

```python
import os
import json
import asyncio

import requests
from dotenv import load_dotenv

from modules.module import Module
from constants import DATA_ROOT
```

## Type checking

```powershell
mypy modules/ sdk/ --ignore-missing-imports
```

Not enforced in CI. Used for spot-checking new code. Existing modules
have partial coverage — don't add `# type: ignore` comments to make
mypy happy on code you didn't write.

## Code conventions

### Paths

Never hardcode a filesystem path. Use the anchors in `constants.py`:

```python
from constants import DATA_ROOT, CONFIG_ROOT, WORKSPACE_ROOT

log_dir      = os.path.join(DATA_ROOT, "logs")
persona_path = os.path.join(CONFIG_ROOT, "lumi.yaml")
scripts_dir  = os.path.join(WORKSPACE_ROOT, "scripts")
```

`WORKSPACE_ROOT` self-locates from `constants.py`. Do not compute it
from `__file__` in other modules — the anchor exists for that reason.

**Never use relative paths** (`"logs/events.log"`) as the first argument
to `os.path.join`. They resolve against the current working directory,
which may not be the workspace root.

### Module contract

Every module in `modules/` subclasses `Module`:

```python
from modules.module import Module


class MyModule(Module):
    def __init__(self, signals, enabled=True):
        super().__init__(signals, enabled)
        # register on signals so other modules can find you
        signals.my_module = self

    async def run(self):
        while not self.signals.terminate:
            # do one unit of work
            await asyncio.sleep(1)

    def get_prompt_injection(self):
        # optional — return an Injection with text and priority
        return self.prompt_injection
```

`main.py` spawns each module on its own thread with its own event loop
via `init_event_loop()`, which calls `asyncio.run(self.run())`.

**Long-running or blocking work goes in `run_in_executor`** so the
event loop stays responsive. Network calls, file reads, and subprocess
invocations all qualify.

In `modules/socketIO.py` specifically:

- **Memory and library calls** (`memory.API.*`, `index_single_file`,
  anything touching ChromaDB) go through `await self._run_mem(fn, *args,
  **kwargs)`. That uses the server's single-worker memory executor, so
  web-originated memory work stays serialized.
- **Other blocking work** (parsing, VLM, Ollama or provider HTTP) uses
  `asyncio.get_running_loop().run_in_executor(None, ...)`.
- **Errors.** Return expected failures with `_json_error(status, message)`.
  Anything you don't catch is turned into a JSON 500 by
  `_json_error_middleware`, with the traceback printed to the console.
- **Order of checks** for a state-changing route: `_origin_ok()`, then
  `_current_user()`, then `_require_csrf()` (JSON) or
  `_require_csrf_header()` (multipart).

**Do not call another module directly.** Coordinate through `signals`:

```python
# Good — find the module via signals
memory = getattr(self.signals, "memory_module", None)

# Bad — import the module and instantiate
from modules.memory import Memory
```

The shared `signals` object is the bus. Direct calls create hidden
coupling and break the shutdown sequence.

### Errors

Catch specific exceptions, log via `hub_emit`, and keep the loop alive.
A crash in one module shouldn't take down the process.

```python
from modules.log_hub import emit as hub_emit

try:
    result = await do_work()
except requests.RequestException as e:
    hub_emit(self.signals, "my_category",
             f"request failed: {e}", level="warn")
    return
```

Use `level="warn"` for recoverable failures, `level="error"` for things
that need attention. `"info"` is the default.

### Logging

Three destinations, three purposes:

| Destination | Use for | Example |
|---|---|---|
| `print()` | Conversation output, one-shot diagnostics | `print(f"{GRAY}SESSION: saved{RESET}")` |
| `hub_emit()` | High-frequency loop output | Ambient skips, heartbeat decisions, memory ops |
| ANSI-colored `print()` | Occasional user-visible events | Startup banner, VLM fires |

The diagnostics dashboard at `127.0.0.1:5001` reads from `hub_emit`. If
you're inside a 30-second loop, use `hub_emit`. If you're inside a
user-triggered action, `print()` is fine. `hub_emit` scrubs secrets, keeps
the last 800 events in memory, and appends to `data/logs/events.log`; it
never blocks on the network.

ANSI color codes are defined at the top of `prompter.py`:

```python
MAGENTA = "\033[95m"
GRAY    = "\033[90m"
RESET   = "\033[0m"
GREEN   = "\033[92m"
YELLOW  = "\033[93m"
```

### Runtime-editable config

If a value should be editable from the web client, it goes in
`constants.py` as a top-level assignment. Both `settings_manager.py`
and `model_manager.py` patch those lines in place. The patcher:

- Only matches `^NAME = ...` at column zero
- Preserves surrounding comments
- `compile()`-checks the result before writing
- Saves `constants.py.bak` on every write

Never nest constants inside classes or functions if you want them
patchable.

## Adding new things

### Adding a module

1. Create `modules/my_module.py` with the `Module` contract above.
2. Register it on `signals` in `__init__` so others can find it.
3. Spawn it in `main.py`:

   ```python
   modules = [
       ...
       MyModule(signals, enabled=True),
   ]
   ```

4. If it writes files, anchor paths under `DATA_ROOT`.

### Adding an agent tool

1. Add a schema to `TOOL_SCHEMAS` (in `modules/agent_tools.py`) or
   `EXTRA_TOOL_SCHEMAS` (in `modules/prompter.py`) if it needs
   prompter-level context.
2. Add the implementation in `agent_tools.py` if it's simple, or a
   special case in `prompter._dispatch_tool_call()` if it needs other
   modules.
3. If it's destructive, add it to `CONFIRM_REQUIRED_TOOLS`.
4. Update the tool description in the agent directive inside
   `prompter.gather_injections()`. The model reads that list.
5. If the tool reads external content, add it to `PROVENANCE_MARKERS` in
   `agent_tools.py` so its result is wrapped in `[FROM X]` markers.

### Adding a cloud provider

1. Add a preset to `PROVIDER_PRESETS` in `modules/llm_providers.py`:

   ```python
   "myprovider": {
       "label":        "My Provider",
       "kind":         "openai",  # or "anthropic" or "ollama"
       "base_url":     "https://api.myprovider.com/v1",
       "key_env":      "MYPROVIDER_API_KEY",
       "base_url_env": "MYPROVIDER_BASE_URL",
       "doc_url":      "https://myprovider.com/keys",
   }
   ```

2. The Models → Providers page picks it up automatically. The routing
   dropdown lists its models as an optgroup once a key is saved.

3. Add the key name to `.env.example` as a commented line.

4. If the provider needs a custom model list, add an entry to
   `PROVIDER_MODEL_OVERRIDES`.

### Adding a web page

1. Add an entry to the `NAV` array in `web/app.js`.
2. Add a `case` in `render()`.
3. Write a `renderMyPage(main)` function that fills `#main-content`.
4. If the page needs data, add a `/api/my-thing` route in
   `modules/socketIO.py`. Follow the pattern of the existing routes —
   login-gated, CSRF on state-changing requests, blocking work off the
   event loop (see [Module contract](#module-contract)).
5. Use `openModal()` for dialogs and `escapeHtml()` for any server or user
   text inside an HTML string.
6. Check it at phone width (≤ 760px) and in both themes.

The SPA uses hash-based routing (`#/my-page`). No build step. The nav entry
becomes an `<a href="#/my-page">` automatically. See
[`../web/README.md`](../web/README.md) for the client's conventions.

### Adding an SDK action

Plugin authors write SDK actions. See
[`../sdk/README.md`](../sdk/README.md) and
[`../plugin/plugin.py`](../plugin/plugin.py).

If you're adding an SDK action **to Lumi itself** (rare), you're
probably actually adding an agent tool. SDK actions are for external
clients.

To add a Minecraft action, add a handler to `HANDLERS` in
`sdk/examples/minecraft/bot.js` and its spec to `registerActions()`.
The control API exposes it automatically at `POST /action`, under its
name without the `minecraft_` prefix. If it interpolates an item or
block name into skill code, validate the name first, as the existing
`badId()` handlers do. A long-running handler should stop itself after about
105 s (the agent waits 135 s) and check `interruptSeq` in its loop, so a new
order can interrupt it.

To give the game agent a new goal, add it to `FREE_GOALS` in
`plugin/minecraft_bot.py`, with either Voyager `code` or an `action`
(a `bot.js` action and its arguments), and a `when(inv, status)` gate that
only lets it run when it makes sense. A new order phrase goes in
`ORDER_MODES`, `ORDER_COLLECT` or `ORDER_TASKS`. The decision logic is plain
Python: test it by calling `_pick_goal()` with a crafted status dict (the
same shape as `bot.js`'s `GET /status`). Voyager skill code can be
compiled without a server by joining it with the primitives in an
`AsyncFunction`, the way `runVoyager()` does.

The house layout is `sdk/examples/minecraft/house_blueprint.js`, a pure
function you can run with plain `node`. Lumi's side of the feature is
`modules/game_manager.py` (processes, autoplay flag, orders from the web) and
the `/api/game*` routes in `modules/socketIO.py`.

### Working with the Voyager submodule

`plugin/voyager` is pinned to an upstream commit. Don't edit files
there: changes appear as a dirty submodule and are lost on update. To
move to a newer upstream commit:

```powershell
cd plugin/voyager; git fetch; git checkout <commit>; cd ../..
git add plugin/voyager
```

Then restart `bot.js` and check it prints
`[voyager] loaded 11 control primitives` (or the new count).

## Debugging

### Terminal output is cluttered

The log hub receives high-frequency events. Live view:

```
http://127.0.0.1:5001/
```

Filter by category. Categories in use: `ambient`, `heartbeat`, `memory`,
`emotional`, `mic`, `vts`, `discord`, `system`, `sdk`, `self`, `tts`.
`data/logs/events.log` persists the same events for post-mortem.

### A module isn't running

Check the boot log. `main.py` prints a banner listing loaded modules.
If a module is missing from `main.py`'s list, it's not spawned.

### A tool isn't firing

Add a `print()` to the tool handler:

```python
def my_tool(path):
    print(f"[debug] my_tool called with {path}")
    ...
```

If it never prints, the model isn't calling it. See
[`../sdk/README.md#design-notes-for-plugin-authors`](../sdk/README.md#design-notes-for-plugin-authors)
for why — small-model tool selection routes by token overlap.

### Uploads are not searchable

If a file is uploaded but Lumi can't reference it later, check the
`memory` category in the log hub for `upload:` and `library: indexed`
lines. If they're missing, `read_image_vlm()` or the sidecar write
failed. If they're present but retrieval fails, the passive library
injection's distance threshold (1.0) filtered the match — use agent
mode and the `search_library` tool instead.

### Tests pass locally, fail in CI

Rare but possible. CI runs on Linux, where path separators differ and
case sensitivity matters. Use `os.path.join`, never string concatenation
with `/` or `\`.

## Release checklist

Before pushing a version bump:

- [ ] `pytest tests/ -v` passes
- [ ] `python tests\test_all_features.py` passes
- [ ] `python tests\test_debug.py` passes with no errors
- [ ] `ruff check .` clean
- [ ] `python scripts\check_system.py` shows a healthy environment
- [ ] Boot Lumi and send one message end-to-end
- [ ] `LUMI_VERSION` in `constants.py` bumped
- [ ] `CHANGELOG.md` entry added
- [ ] Git tag matches the version

## Related docs

- [architecture.md](architecture.md) — the turn flow and module topology
- [configuration.md](configuration.md) — where config values live
- [roadmap.md](roadmap.md) — what's done and what's next
- [security.md](security.md) — threat model and incident response
- [`../modules/README.md`](../modules/README.md) — file-level map
- [`../sdk/README.md`](../sdk/README.md) — the external SDK
- [`../tests/README.md`](../tests/README.md) — test suite overview
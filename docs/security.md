# Security

Lumi is a local, personal-use agent that can read and write files, run
commands, browse the web, and accept plugins. This document covers the
threat model, the guards that shipped in Phase 9, and what to do when
something goes wrong. Phase 9 is complete; see
[roadmap.md](roadmap.md) for status and
[known-limitations.md](known-limitations.md) for what is still open.

## Threat model

Lumi runs on your machine and trusts you. It does not fully trust
anything she reads or anything that connects to her.

| Threat | Example | Main defense |
|---|---|---|
| Prompt injection | A web page or file tells her to run a command | Provenance markers, confirmation gates, command allowlist |
| Destructive tool use | Overwriting a source file or running a shell one-liner | `y/n` confirmation, protected paths, command allowlist |
| Path escape | `../../` reads outside `WORKSPACE_ROOT` | `realpath` sandbox |
| Rogue SDK client | A local plugin registers a dangerous action | Deny-by-default allowlist, capability ceiling |
| Memory poisoning | A page plants a false "fact" in long-term memory | Poisoning guards, importance cap |
| Secret leakage | An API key lands in a log or a screen description | Log scrubber, VLM output filter |
| Network exposure | The web client is reachable from the LAN | Loopback binding, `ALLOW_LAN_EXPOSURE` gate |
| Discord abuse | A channel member floods or injects instructions | Rate limit, injection filter |
| Runaway agent | Repeated or unusual tool calls | Anomaly detection |

Out of scope: an attacker with local admin or physical access to the
machine, and multi-user hosts. Per-user isolation is partial, so treat an
instance as single-user.

## Phase 9 guards

| Item | Guard | Where |
|---|---|---|
| 9.1 | Provenance markers wrap external content as `[FROM FILE]`, `[FROM WEB]`, `[FROM MEMORY]`, `[FROM SDK]` | `modules/agent_tools.py:wrap_tool_result`, `modules/prompter.py` |
| 9.2 | SDK deny-by-default allowlist | `data/memories/sdk_allowlist.json`, `sdk/server.py` |
| 9.3 | SDK per-client capability ceiling and confirm gate | `sdk/server.py:is_action_allowed`, `sdk/server.py:client_autoapproves_writes` |
| 9.4 | Command allowlist and shell metacharacter refusal | `modules/agent_tools.py` (`_COMMAND_ALLOWLIST`) |
| 9.5 | `realpath` sandbox on file access | `modules/agent_tools.py:_safe_path` |
| 9.6 | Secret scrubber in logs, VLM output filter | `modules/log_hub.py`, `modules/multimodal.py`, `modules/ambient_perception.py` |
| 9.7 | `ALLOW_LAN_EXPOSURE = False` gate | `constants.py` |
| 9.8 | Memory poisoning guards, `REFLECTION_IMPORTANCE_CAP = 0.7` | `modules/memory.py` (`_is_poisoned_fact()`) |
| 9.9 | Discord rate limit (10 messages per 60 s) and injection filter | `modules/discordClient.py` |
| 9.10 | `PROTECTED_PREFIXES`, `PROTECTED_BASENAMES`, read-only refusal counter | `modules/agent_tools.py`, `modules/prompter.py` |
| 9.11 | Tool call anomaly detection | `modules/self_model.py:note_tool_call`, `modules/prompter.py` |
| 9.12 | Encryption at rest: BitLocker recommendation only, nothing in-app | This document |
| 9.13 | Pinned dependencies | `requirements.lock.txt` |
| 9.14 | Incident playbook | This document |

<!-- VERIFY: confirm the symbol names in the Where column against the
code, especially `_COMMAND_ALLOWLIST` and `_is_poisoned_fact()`. -->

### Provenance markers

Content Lumi reads from files, the web, memory, or an SDK plugin is
wrapped in a marker so the model can tell it apart from your messages.
This reduces injection risk but does not eliminate it. A model can still
be talked into acting on marked text, which is why the confirmation gates
and allowlists below exist as independent layers.

### SDK controls

An SDK client can only run actions that appear in
`data/memories/sdk_allowlist.json`. Each client also has a capability
ceiling: an action cannot exceed the tier its client is allowed. Set
`auto_approve_writes: false` for a client to make its write actions ask
for confirmation in the terminal every time.

<!-- VERIFY: confirm the allowlist JSON schema and the
auto_approve_writes key name against sdk/server.py. -->

## Earlier layers

These predate Phase 9 and still apply.

| Layer | Behavior |
|---|---|
| Confirmation gate | `write_file`, `run_command`, and `run_in_terminal` require `y/n` in the terminal |
| Simulated-action detection | Catches "I updated the file" prose with no real tool call |
| Read-only query gate | Refuses `write_file` when the user only asked to read |
| No-op and duplicate guards | Refuses identical writes and repeated calls |
| Backups | `write_file` saves a `.bak` before overwriting |
| Web client | CSRF double-submit, WebSocket origin check, login rate limit of 5 per minute per user, loopback-only binding, no `innerHTML` |

## Operating safely

- **Keep agent mode off** when Lumi is reading material you do not
  trust. Agent mode is a plain toggle (`!agent_off`).
- **Read the `write_file` confirmation** before approving. A `.bak` is
  saved, but a bad write still costs you a restore.
- **Cloud providers see your prompts.** Any path routed to a cloud model
  sends its prompts (persona, injected memories, screen context) to that
  provider. Keep sensitive work on local Ollama.
- **Keep secrets out of git.** `data/*` is already ignored except its
  README. Confirm `.env` is ignored too, and rotate any key that has ever
  been committed.
- **Keep `constants.py` under version control.** The model manager and
  settings page rewrite it, saving a `.bak`.
- **Install from the lock file** for reproducible dependencies:

```powershell
pip install -r requirements.lock.txt
```

<!-- VERIFY: confirm .env is now in .gitignore; the older README said it
was not by default. -->

### Encryption at rest

Lumi does not encrypt `data/`. That folder holds long-term memory
(`data/memories/chroma.db/`), user accounts (`data/memories/users.db`),
and tokens. Turn on full-disk encryption for the drive that holds it.
On Windows that means BitLocker:

```powershell
manage-bde -status
```

## Incident response

Stop first, investigate second. Most steps below assume Lumi is stopped.

**Unexpected file change or command.**
1. Type `!agent_off`, then stop the process.
2. Check `data/logs/events.log` for the tool calls.
3. Restore changed files from their `.bak` copies or from git.
4. Read the last few external sources she touched for injected
   instructions.

**A secret leaked.**
1. Rotate the key at the provider first.
2. Update it on the Models page or in `.env`.
3. Search `data/logs/` for the old value and delete affected logs.
4. If it was committed, treat it as public and rotate regardless.

**Suspicious SDK client.**
1. Remove its entry from `data/memories/sdk_allowlist.json`.
2. Delete `data/memories/sdk_token.txt`; a fresh token is generated on
   the next boot.
3. Restart Lumi and reconnect only clients you trust.

**Poisoned memory.**
1. Find the fact in the Memory page and delete it.
2. If there are many, wipe memory under Settings, then Advanced.

**Unintended network exposure.**
1. Confirm `ALLOW_LAN_EXPOSURE = False` in `constants.py`.
2. Check that ports 5000, 5001, and 5056 listen on `127.0.0.1` only.
3. Change your password and end other sessions under Settings.

## Verifying the guards

```powershell
python tests/test_phase9.py
python tests/test_debug.py
```

`tests/test_phase9.py` runs 47 offline checks covering the Phase 9
guards. `tests/test_debug.py` is a 14-section health check. See
[../tests/README.md](../tests/README.md).

## Reporting a security issue

<!-- VERIFY: replace with your real contact or private-report process. -->
Report vulnerabilities privately to the repository owner instead of
opening a public issue.

## Related docs

- [architecture.md](architecture.md): where the guards sit in the turn flow
- [configuration.md](configuration.md): `constants.py` and `.env` settings
- [troubleshooting.md](troubleshooting.md): what to do when a guard blocks something
- [known-limitations.md](known-limitations.md): open gaps
- [roadmap.md](roadmap.md): phase status
- [../sdk/README.md](../sdk/README.md): SDK protocol and capability tiers
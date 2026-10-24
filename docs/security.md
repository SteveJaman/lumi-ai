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
| Drive-by request to a local service | A web page you visit POSTs code to the Minecraft bot's control API on `:5057` | **None yet.** See [Minecraft bot control API](#minecraft-bot-control-api) |
| Memory poisoning | A page plants a false "fact" in long-term memory | Poisoning guards, importance cap |
| Secret leakage | An API key lands in a log or a screen description | Log scrubber, VLM output filter |
| Network exposure | The web client is reachable from the LAN or internet | Loopback binding, `ALLOW_LAN_EXPOSURE` gate; tunnel traffic on its own port with a reduced tier, no remote sign-up, optional Cloudflare Access ([remote-access.md](remote-access.md)) |
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

### Provenance markers

Content Lumi reads from files, the web, memory, or an SDK plugin is
wrapped in a marker so the model can tell it apart from your messages.
This reduces injection risk but does not eliminate it. A model can still
be talked into acting on marked text, which is why the confirmation gates
and allowlists below exist as independent layers.

### SDK controls

A client can register actions only if its entry in
`data/memories/sdk_allowlist.json` has `approved: true`. Unknown clients
get a denied entry on first connect. Each client also has a
`capability_ceiling`: an action that declares a higher tier is refused at
registration and again before dispatch. `write` actions ask for `y/n`
confirmation in the terminal unless the client has
`auto_approve_writes: true`. `dangerous` actions, and any action with
`requires_confirm`, always ask. The file is read on every new connection.

### Minecraft bot control API

`sdk/examples/minecraft/bot.js` serves an HTTP control API on
`127.0.0.1:5057` for the game agent and the diagnostics page.
**It has no authentication and sends `Access-Control-Allow-Origin: *`.**
`POST /action` with `execute_skill` runs arbitrary JavaScript inside the
Node process, with your user's permissions. `POST /directive` gives the
agent an order (for example, to hand items to whoever is nearby). The SDK
allowlist and the `y/n` gate do not apply, because the request never passes
through Lumi.

So while `bot.js` is running, any local process, and potentially any web
page open in your browser, can run code on your machine. A plain-text
`POST` from a page is a "simple" cross-origin request with no preflight,
and browsers do not reliably block public sites from reaching
`127.0.0.1`.

Until the API requires a token:

- Run `bot.js` only while you are using it: turn **Minecraft AI** off on the
  Games page when you're done (Lumi stops the process).
- Don't browse untrusted sites in the same session.
- Keep `BOT_CONTROL_PORT` on loopback. `bot.js` binds to `127.0.0.1`
  and ignores `ALLOW_LAN_EXPOSURE`.

The planned fix: require the SDK token (`data/memories/sdk_token.txt`)
on every control request, drop the wildcard CORS header, and have the
dashboard on `:5002` proxy its requests server-side.

The Minecraft dashboard on `127.0.0.1:5002` is read-only apart from
forwarding the server address, and serves only Minecraft telemetry.

Other Minecraft surfaces:

- **Live viewer (`:3000`)** binds to all interfaces (prismarine-viewer offers
  no host setting) and shows the world around the bot to anyone on your
  network who opens it. It's read-only. Windows asks about the firewall; deny
  it on public networks, or set `VIEWER_PORT=0`.
- **Skin server (`:3001`)** is loopback-only and serves one PNG.
- **Games page routes** (`/api/game/status`, `/api/game/toggle`, and
  `/api/games/{game}/` `directive`, `action`, `item/{name}`, `skin`) start
  processes, send orders, drop items and replace the skin, so they are
  host-only: the remote tier refuses them (covered by
  `tests/test_remote_tiers.py`). The ones that change anything need a session
  and a CSRF token.
- **The HUD's drop action** goes through Lumi, not straight from the browser
  to `:5057`, and Lumi forwards only `drop_item` with a validated item id.
  `execute_skill` and every other bot action are refused.
- **Item textures** come from files bundled with prismarine-viewer. The name
  must match `[a-z0-9_]`, so a request can't walk out of that folder.
- **Skin uploads** must be real PNGs (checked by decoding them with Pillow) of
  64x64 or 64x32 pixels, at most 64 KB. They're written only to `SKIN_PATH`,
  and the previous file is kept.
- **In-game orders** are accepted only from the owner (`MC_OWNER`, or the
  first other player), and only with a wake word. Other players' chat is
  passed to Lumi as data, never as an order.
- **Spawned processes** run with your user's permissions. On Windows they're
  in a kill-on-close job object, so a crash of Lumi doesn't leave a bot
  logged into your world.

## Earlier layers

These predate Phase 9 and still apply.

| Layer | Behavior |
|---|---|
| Confirmation gate | `write_file`, `run_command`, and `run_in_terminal` require `y/n` in the terminal |
| Simulated-action detection | Catches "I updated the file" prose with no real tool call |
| Read-only query gate | Refuses `write_file` when the user only asked to read |
| No-op and duplicate guards | Refuses identical writes and repeated calls |
| Backups | `write_file` saves a `.bak` before overwriting |
| Web client | See [Web client](#web-client) below |

### Web client

| Control | Behavior |
|---|---|
| Binding | `127.0.0.1:5000` only, unless `ALLOW_LAN_EXPOSURE` is `True` |
| Session cookie | `lumi_session`, `HttpOnly`, `SameSite=Lax`, 30-day lifetime; argon2id password hashes in `data/memories/users.db` |
| Login throttle | 5 failed logins per username within 60 seconds blocks further attempts until the window passes (`user_auth.py:_rate_ok`) |
| Origin check | Every state-changing HTTP route and the socket.io handshake reject an `Origin` outside `http://127.0.0.1:5000` / `http://localhost:5000` and the `https://` hosts in `LUMI_PUBLIC_ORIGINS` |
| Remote tier | Requests on `TUNNEL_PORT` (5080), from non-loopback peers, or carrying Cloudflare/proxy headers are remote. They may only call the `/api/` routes in `_REMOTE_API_ALLOW` (chat, history, status, voice, uploads, model list), can't send chat commands, can't register, and get `Secure` cookies over HTTPS. See [remote-access.md](remote-access.md#what-remote-users-can-and-cannot-do) |
| Cloudflare Access | Off by default. With `TRUST_CLOUDFLARE_ACCESS`, only a JWT signed by your Access team signs a visitor in; the email header alone is ignored (`modules/cf_access.py`) |
| CSRF | Double-submit: the readable `lumi_csrf` cookie must match `X-CSRF-Token` (or a JSON `csrf` field), compared in constant time. Required on every POST/PUT/PATCH/DELETE under `/api/`, including the multipart routes `/api/chat/upload` and `/api/voice_message` |
| Request size | JSON bodies capped at aiohttp's 1 MB default; uploads 25 MB; voice clips 10 MB; both multipart routes stream and stop at the cap |
| Uploads | Extension allowlist, sanitized filenames, files written only under `data/library/{documents,images,text}/` |
| Static files | `/web/*` refuses `..`, absolute paths, and NUL bytes, and checks the resolved path stays inside `web/` |
| Error responses | API errors return `{"error": ...}`; tracebacks go to the console, not the browser |
| Rendering | Untrusted text is inserted with `textContent` or `escapeHtml()`. `innerHTML` is used for static templates only |

Known gaps, tracked in [known-limitations.md](known-limitations.md):
`/auth/logout` has no CSRF or origin check (worst case: a cross-site page
signs you out), and revoking a session does not disconnect a socket.io
connection that session already opened.

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

`.env` is in `.gitignore`. The rule matches any `.env`, including
`sdk/examples/minecraft/.env`.

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
   If the Minecraft tools are running, check 5002 and 5057 as well.
3. Change your password and end other sessions under Settings.

## Verifying the guards

```powershell
python tests/test_all_features.py
python tests/test_debug.py
pytest tests/test_agent_tools.py -v
```

`tests/test_all_features.py` exercises the path sandbox, shell guards,
provenance wrapping, and secret redaction. `tests/test_agent_tools.py`
covers the `realpath` sandbox under pytest. `tests/test_debug.py` is a
14-section health check. There is no dedicated harness for every Phase 9
item in the repo (an earlier `tests/test_phase9.py` was never committed),
so the remaining guards are verified by reading the code paths in the
table above. See [../tests/README.md](../tests/README.md).

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
- [remote-access.md](remote-access.md): tunnel, access tiers, Cloudflare Access
- [../sdk/README.md](../sdk/README.md): SDK protocol and capability tiers
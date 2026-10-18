# Troubleshooting

**Run `python tests\test_debug.py` first.** It's a 14-section whole-project
health check that localizes failures in about two seconds. For an
end-to-end feature pass, including the sandbox, shell, provenance, and
redaction guards, run `python tests\test_all_features.py`. For integration
and hardware checks, run `python tests\test_comprehensive.py`. For a
machine pre-flight before the app's own imports resolve, run
`python scripts\check_system.py`.

This guide covers common failure modes and diagnostic steps for Lumi's
core systems, particularly focusing on Discord voice integration, memory
locking, file uploads, and local dependencies.

## Security and refusals

Phase 9 added guards that stop Lumi from doing things on purpose. When one
fires, it looks like a refusal or a silent drop, not a crash. Each entry
below gives the symptom, the cause, the fix, and where to look in the Log
Hub at `http://127.0.0.1:5001/`. For the design behind these guards, see
[security.md](security.md).

The Log Hub categorizes events under `ambient`, `heartbeat`, `memory`,
`emotional`, `mic`, `vts`, `discord`, `system`, `sdk`, `self`, and `tts`.
Filter by the category a given guard logs under.

### Lumi refuses to write a file

**Symptom:** A `write_file` call is refused, or Lumi says she cannot edit the file.
**Cause:** The target matches `PROTECTED_PREFIXES` or `PROTECTED_BASENAMES` in `modules/agent_tools.py`. The path may also resolve outside `WORKSPACE_ROOT`, which `_safe_path` rejects, or you only asked her to read and the read-only gate refused the write. Repeated refusals are counted in `modules/prompter.py`.
**Resolution:**

1. Make the edit yourself outside Lumi. Protected files are meant to stay out of her reach.
2. If the file is inside the workspace and should be writable, adjust the protected lists in `modules/agent_tools.py`.
3. If the request was only to read, ask for the change explicitly.

**Where to check:** the `system` category in the Log Hub for the refusal reason.

### Lumi refuses to run a command

**Symptom:** A `run_command` call is refused with no confirmation prompt.
**Cause:** The command is not in `_COMMAND_ALLOWLIST`, or it contains shell metacharacters matched by `_SHELL_METACHAR` (for example chaining, pipes, or redirects). Both checks live in `modules/agent_tools.py`.
**Resolution:**

1. Ask for a single plain command instead of a chained one.
2. Run the command yourself in a terminal.
3. To permit a new command, add it to `_COMMAND_ALLOWLIST`. Add only commands you are comfortable with her running.

**Where to check:** the `system` category for the refused command string.

### Lumi acts on the contents of a file or page

**Symptom:** After reading a file, web page, or screen, Lumi tries a tool call you never asked for.
**Cause:** Prompt injection. The content contained instructions and the model followed them. Provenance markers (`[FROM FILE]`, `[FROM WEB]`, `[FROM MEMORY]`, `[FROM LIBRARY]`, `[FROM SDK]`) reduce this risk but do not remove it.
**Resolution:**

1. Answer `n` at the confirmation prompt.
2. Type `!agent_off` and stop the process if it keeps happening.
3. Follow the incident steps in [security.md](security.md).

**Where to check:** the `system` category for the sequence of tool calls. Tool call anomaly detection (`modules/self_model.py:note_tool_call`) also records unusual patterns.

### A memory was not stored

**Symptom:** You told Lumi a fact, but `!recall` does not find it.
**Cause:** `_is_poisoned_fact()` in `modules/memory.py` rejected it because it looked like an instruction rather than a fact. Facts from the background reflection pass are also limited by `REFLECTION_IMPORTANCE_CAP = 0.7`, so they never outrank facts you stated directly.
**Resolution:** Rephrase it as a plain statement about you or your world, for example "I prefer tea" rather than "Always answer with tea references". Then try `!recall` again.

**Where to check:** the `memory` category for a rejection line.

### A Discord message was dropped

**Symptom:** Lumi ignores a message in the Discord text channel.
**Cause:** Either the sender exceeded the rate limit of 10 messages per 60 seconds, or `_looks_like_injection()` in `modules/discordClient.py` flagged the text.
**Resolution:** Wait a minute and send again, or rephrase without instruction-style wording aimed at Lumi.

**Where to check:** the `discord` category in the Log Hub.

### A screen description was discarded

**Symptom:** Lumi ignores what is on screen, or the ambient sampler seems to skip frames.
**Cause:** The vision model's output failed `_vlm_output_safe()` in `modules/multimodal.py` or `modules/ambient_perception.py`, so the description was never injected into the prompt.
**Resolution:** Change what is on screen (for example close a window showing keys or instruction-like text) and ask again.

**Where to check:** the `ambient` or `system` category for a discarded-description line.

### An SDK action will not run

**Symptom:** A plugin connects and registers, but Lumi never calls its action, or the call is refused.
**Cause:** The SDK is deny-by-default. The client's entry in `data/memories/sdk_allowlist.json` has `approved: false` (the default the server writes on first connect), or the action declares a tier above that client's `capability_ceiling`. The allowlist approves clients, not individual actions. If the action never appears at all, see the routing notes in [../sdk/README.md](../sdk/README.md).
**Resolution:**

1. Review the plugin, then set `approved: true` for its `game` name in `data/memories/sdk_allowlist.json`.
2. Lower the action's declared tier, or raise the client's ceiling if you trust it.
3. Restart the plugin. Lumi doesn't need a restart.

The server reads the allowlist each time a client connects, so a plugin that was already connected keeps its old approval until it reconnects. The refusal message tells you to restart Lumi; restarting only the plugin is enough.

**Where to check:** the `sdk` category for the allowlist or capability refusal.

### An SDK action keeps asking for y/n

**Symptom:** Every call from a plugin stops for confirmation in Lumi's terminal.
**Cause:** `auto_approve_writes` is `false` for that client in `data/memories/sdk_allowlist.json`. The prompt appears in Lumi's terminal, not in the plugin.
**Resolution:** Answer the prompts, or set `auto_approve_writes` to `true` for a client you trust. Leave it `false` for anything you have not reviewed.

**Where to check:** the `sdk` category for the pending confirmation.

### An uploaded file was not indexed

**Symptom:** A file upload succeeded, but Lumi cannot reference it on later turns.
**Cause:** Either the sidecar write or `index_single_file()` failed, or the passive library injection filtered the match out on distance (the passive path uses a threshold of `< 1.0` on Chroma distance).
**Resolution:**

1. Check the Log Hub `memory` category for `upload:` and `library: indexed` lines. If they are missing, extraction or sidecar writing failed.
2. If the file is indexed but never retrieved, ask using words that appear in the file itself.
3. In agent mode, ask Lumi to call `search_library` explicitly — that tool returns the top matches regardless of distance.

**Where to check:** the `memory` category in the Log Hub.

### A duplicate upload is refused

**Symptom:** Uploading a file returns HTTP 409 with "File already exists."
**Cause:** A byte-identical file already exists in `data/library/`. The server compares SHA-256 hashes.
**Resolution:** On the web client, use the Replace button in the modal. The old file, its sidecar, and its ChromaDB chunks are removed before the new one lands.

**Where to check:** the `memory` category for `upload: duplicate detected`.

## Web client

### An API call fails with only "HTTP 500"

**Symptom:** A page shows an error with just a status code.
**Cause:** API routes always answer in JSON. The server's error middleware turns an unhandled exception into `{"error": "Internal error: <ExceptionType>"}` and prints the full traceback to the Lumi console.
**Resolution:** Read the traceback in the terminal running `main.py`. The browser only gets the exception type, by design.

### Sending a message fails with HTTP 413

**Symptom:** `Failed to send: HTTP 413` in the chat.
**Cause:** The JSON body exceeded aiohttp's 1 MB limit. Normal messages, including image attachments, stay far below it, because only the extracted text travels with the message (≤ 15,000 characters per attachment, at most 6 attachments). Builds before 2026-10-17 also sent each image's base64 and hit this limit with images over ~750 KB.
**Resolution:** Reload the tab so the current `app.js` is running.

### Voice playback plays every clip twice

**Symptom:** With Voice ON, each sentence plays twice or more.
**Cause:** More than one socket.io connection is open in the tab. The current client opens exactly one per page load.
**Resolution:** Reload the tab. If it persists, check that nothing other than `ensureVoiceSocket()` in `web/app.js` calls `io()`.

### Hold-to-talk does nothing, or fails

| Symptom | Cause | Fix |
|---|---|---|
| Button is greyed out | The browser has no `MediaRecorder` | Use a current Chrome, Edge, Firefox, or Safari |
| "Microphone permission denied." | Mic access blocked for `127.0.0.1:5000` | Allow the microphone in the browser's site settings |
| A quick tap does nothing | Clips under ~1 KB are dropped as accidental taps | Hold the button while speaking |
| `Voice message failed: CSRF token mismatch.` | Stale tab after signing in elsewhere | Reload the tab |
| `Voice message failed: Voice clip too large` | Clip over 10 MB | Record shorter clips |
| `Transcription failed: ...` | faster-whisper missing, or the clip couldn't be decoded | `pip install faster-whisper`; the Lumi console has the decoder error |

### The theme flashes or the accent colour looks wrong

**Symptom:** Dark-mode users see a light flash on load, or the light theme's accent turns mint or stays a mood colour.
**Cause:** `web/theme-init.js` isn't loading before `style.css`, or something wrote `--accent` inline on `<html>` without removing it.
**Resolution:** Confirm each HTML page has `<script src="/web/theme-init.js"></script>` in `<head>` before the stylesheet. Mood colours must go through `setAccentForMood()` / `resetMoodAccent()` in `web/app.js`. See [../web/README.md](../web/README.md).

### Memory page or uploads feel slow

**Symptom:** The Memory page or an upload takes a while, but the rest of the web client stays responsive.
**Cause:** Memory and library calls from the web server run one at a time on a dedicated background thread, so a long ChromaDB query or indexing job makes the next memory request wait its turn. Full-document indexing after an upload is queued on the same thread.
**Resolution:** Wait for the queued work to finish. The `memory` category in the Log Hub shows `upload:` and `library: indexed` lines as each job completes.

## Discord Voice Sending (Audio Output)

**Symptom:** Lumi joins the voice channel and generates responses, but no audio plays. The logs report clean completion with no errors.
**Cause:** `FFmpeg` is either missing from the system PATH or a broken installation (like a dead conda shim) is causing `discord.FFmpegPCMAudio` to silently no-op.
**Resolution:**

1. Verify FFmpeg is accessible by running `ffmpeg -version` in your terminal.
2. If it fails or points to a broken shim, install it cleanly via Windows Package Manager:
```powershell
winget install Gyan.FFmpeg

```


3. **Restart your terminal** and the Lumi process to ensure the updated PATH takes effect.
4. Run `!test_url` in Discord to verify FFmpeg is correctly routing to the voice client.

**Symptom:** Lumi is silent after joining a new guild or after an admin action.
**Cause:** Lingering server mute or deafen states silently drop outgoing audio.
**Resolution:** Use the `!voice_debug` command to check the `server_mute` and `server_deaf` flags. If they are set, the server admin must unmute the bot.

## Discord Voice Receiving (Audio Input)

**Symptom:** Lumi suddenly stops hearing users in the voice channel. No errors are printed to the terminal.
**Cause:** The DAVE End-to-End Encryption (E2EE) session has fallen out of sync with the audio sink's SSRC mapping. This almost always happens if the voice client attempted to reconnect at runtime.
**Diagnostic:** Run `!voice_stats` in the text channel. If `frames_in` is climbing rapidly but `decoded` is stuck at 0, the session is desynced.
**Resolution:** **Restart the Lumi process.** Do not attempt to add or trigger runtime reconnect logic, as it cannot recover a broken DAVE decryption state.

## Memory Database (ChromaDB)

**Symptom:** The console throws a `sqlite3.OperationalError: database is locked` on boot or when saving a memory.
**Cause:** ChromaDB uses a single-process lock for its persistent local database at `data/memories/chroma.db`. Both the `neuro_collection` (personal memory) and `library_docs` (uploaded files) share that lock. Another process is currently holding it.
**Resolution:**

1. Ensure you do not have multiple instances of `main.py` running simultaneously.
2. Ensure no standalone testing scripts (e.g., something in `scripts/` or `sdk/examples/`) are currently querying the ChromaDB directory while the main agent loop is active.

## Avatar & Visual Frontend

**Symptom:** The voice works, but the VTube Studio avatar is frozen or not lip-syncing.
**Cause:** The external websocket connection to VTube Studio dropped, or the application isn't running.
**Resolution:**

1. Ensure VTube Studio is open and the API/Websocket is enabled in the software settings.
2. Verify the authentication token inside `data/vtubeStudio_token.txt` is valid. If it expired or was revoked, delete the file and restart Lumi to trigger a new plugin authorization prompt in VTube Studio.

## Minecraft bot and agent

Three processes are involved: `sdk/examples/minecraft/bot.js` (the bot and
its control API on `:5057`), `plugin/minecraft_bot.py` (the autonomous
agent), and `plugin/mc_dashboard.py` (diagnostics on `:5002`). None of
them need Lumi to be running. The dashboard's relay and error panels are
the first place to look.

| Symptom | Cause | Resolution |
|---|---|---|
| `Cannot find module 'ws'` | npm packages not installed | `npm install` in `sdk/examples/minecraft` |
| `[voyager] primitives not loaded` | The Voyager submodule wasn't fetched | `git submodule update --init plugin/voyager` |
| `ECONNREFUSED ...:25565` repeating | No world on that port | Open the world to LAN and enter its port in the dashboard's Minecraft Server panel |
| `[sdk] Lumi is not running (127.0.0.1:5056)` | `main.py` isn't running | Expected. The bot plays anyway and attaches when Lumi starts |
| Dashboard: "Cannot reach bot.js control API" | `bot.js` stopped, or an old process without the API | Restart `bot.js`; look for `[ctrl] control API on 127.0.0.1:5057` |
| Agent stuck on `waiting for bot` | `bot.js` not running, or not spawned in a world | Start `bot.js` first, then check it printed `bot spawned and ready` |
| Skill fails with `No oak_log nearby, please explore first` | Voyager primitive feedback: the resource isn't within range | Move the bot near oak trees. The curriculum only uses oak |
| Lumi reports `action timed out` but the bot keeps working | SDK invocations time out after 30 s | Expected for long skills; see [known-limitations.md](known-limitations.md#minecraft) |
| Curriculum restarts from logs | `data/minecraft/skills.json` was deleted, or tiers are being redone for materials | Intended. Delete `skills.json` to force a full restart |

Full reference: [../sdk/examples/minecraft/README.md](../sdk/examples/minecraft/README.md#troubleshooting).

## Viewing Live Logs

If the terminal output is too sparse (due to the clean terminal design), all internal state transitions, Discord text messages, and STT transcripts are routed to the local Log Hub.

* **Access:** Open `http://127.0.0.1:5001/` in your browser.
* **Filter:** Use categories like `mic`, `discord`, or `system` to isolate the data stream. Background errors not printed to standard out will appear here.

## Related docs

- [security.md](security.md): the threat model, guard design, and incident response
- [known-limitations.md](known-limitations.md): open gaps and behavior that is not a bug
- [discord-voice.md](discord-voice.md): DAVE, VAD, and voice diagnostics
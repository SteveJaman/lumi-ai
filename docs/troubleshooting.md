# Troubleshooting

**Run `python tests/test_debug.py` first** (or `python tests/test_comperhension.py` for retrieval/comprehension verification). They run quickly and localize failures across Lumi's core systems.

This guide covers common failure modes and diagnostic steps for Lumi's core systems, particularly focusing on Discord voice integration, memory locking, and local dependencies.

## Security and refusals

Phase 9 added guards that stop Lumi from doing things on purpose. When one
fires, it looks like a refusal or a silent drop, not a crash. Each entry
below gives the symptom, the cause, the fix, and where to look in the Log Hub
at `http://127.0.0.1:5001/`. For the design behind these guards, see
[security.md](security.md).

<!-- VERIFY: Log Hub category names other than `discord` are guesses
(`system`, `memory`, `sdk`). Replace them with the categories the modules
actually log under. -->

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
**Cause:** Prompt injection. The content contained instructions and the model followed them. Provenance markers (`[FROM FILE]`, `[FROM WEB]`, `[FROM MEMORY]`, `[FROM SDK]`) reduce this risk but do not remove it.
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
**Cause:** The vision model's output failed `_vlm_output_safe()` in `modules/multimodal.py`, so the description was never injected into the prompt.
**Resolution:** Change what is on screen (for example close a window showing keys or instruction-like text) and ask again.

**Where to check:** the `system` category for a discarded-description line.

### An SDK action will not run

**Symptom:** A plugin connects and registers, but Lumi never calls its action, or the call is refused.
**Cause:** The SDK is deny-by-default. The action is missing from `data/memories/sdk_allowlist.json`, or it exceeds the capability ceiling set for that client. If the action never appears at all, see the routing notes in [../sdk/README.md](../sdk/README.md).
**Resolution:**

1. Review the plugin, then add its client and action to `data/memories/sdk_allowlist.json`.
2. Lower the action's declared tier, or raise the client's ceiling if you trust it.
3. Restart Lumi so the server picks up the change.

**Where to check:** the `sdk` category for the allowlist or capability refusal.

<!-- VERIFY: confirm whether the allowlist is reloaded live or needs a
restart, and the exact JSON keys for client and ceiling. -->

### An SDK action keeps asking for y/n

**Symptom:** Every call from a plugin stops for confirmation in Lumi's terminal.
**Cause:** `auto_approve_writes` is `false` for that client in `data/memories/sdk_allowlist.json`. The prompt appears in Lumi's terminal, not in the plugin.
**Resolution:** Answer the prompts, or set `auto_approve_writes` to `true` for a client you trust. Leave it `false` for anything you have not reviewed.

**Where to check:** the `sdk` category for the pending confirmation.

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
**Cause:** ChromaDB uses a single-process lock for its persistent local database at `data/memories/chroma.db`. Another process is currently holding the lock.
**Resolution:**

1. Ensure you do not have multiple instances of `main.py` running simultaneously.
2. Ensure no standalone testing scripts (e.g., something in `scripts/` or `sdk/examples/`) are currently querying the ChromaDB directory while the main agent loop is active.

## Avatar & Visual Frontend

**Symptom:** The voice works, but the VTube Studio avatar is frozen or not lip-syncing.
**Cause:** The external websocket connection to VTube Studio dropped, or the application isn't running.
**Resolution:**

1. Ensure VTube Studio is open and the API/Websocket is enabled in the software settings.
2. Verify the authentication token inside `data/vtubeStudio_token.txt` is valid. If it expired or was revoked, delete the file and restart Lumi to trigger a new plugin authorization prompt in VTube Studio.



## Viewing Live Logs

If the terminal output is too sparse (due to the clean terminal design), all internal state transitions, Discord text messages, and STT transcripts are routed to the local Log Hub.

* **Access:** Open `[http://127.0.0.1:5001/](http://127.0.0.1:5001/)` in your browser.
* **Filter:** Use categories like `mic`, `discord`, or `system` to isolate the data stream. Background errors not printed to standard out will appear here.

## Related docs

- [security.md](security.md): the threat model, guard design, and incident response
- [known-limitations.md](known-limitations.md): open gaps and behavior that is not a bug
- [discord-voice.md](discord-voice.md): DAVE, VAD, and voice diagnostics
# Troubleshooting

This guide covers common failure modes and diagnostic steps for Lumi's core systems, particularly focusing on Discord voice integration, memory locking, and local dependencies.

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
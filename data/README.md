# Lumi Runtime Data

`data/` holds everything Lumi writes at runtime: logs, memory, tokens, captures, and caches. **Only this README is tracked by git.** Everything else is excluded by a `data/*` rule in `.gitignore`, which is why the folder looks empty after a fresh clone. It is safe to delete the contents on a fresh clone, and safe to clear them if you want a factory reset.

## What lives here

The last column separates disposable files from destructive deletes. "Yes" means Lumi recreates or ignores it. Anything else names what you lose.

### Logs

| Path | Contents | Safe to delete? |
|---|---|---|
| `data/logs/chat_history.json` | Active conversation history, owned by `signals.py`. When a message added through `add_message()` (microphone and Discord input) pushes it past 25 entries, older turns collapse into one summary block and the last 10 stay verbatim. Only the last 30 entries are loaded on boot. Web chat attachments are stored as name, kind, and extracted text only; image bytes are never written here, and any left by older builds are stripped on load. | Yes (loses the backfill material used by `scripts/import_chat_history.py`) |
| `data/logs/events.log` | Diagnostics event stream from the log hub, secret-scrubbed, append-only | Yes |
| `data/logs/visual_memory.json` | Recent screen descriptions | Yes |
| `data/logs/images/` | Timestamped screen captures (JPEG) | Yes |
| `data/logs/screenshots/` | Plugin-captured screenshots (PNG) | Yes |

### Memories and credentials

| Path | Contents | Safe to delete? |
|---|---|---|
| `data/memories/chroma.db/` | ChromaDB vector store. A directory, not a file. Holds two collections: `neuro_collection` (personal memories) and `library_docs` (uploaded files and their sidecars). | Loses both |
| `data/memories/users.db` | SQLite user accounts and sessions | Loses accounts |
| `data/memories/emotional_state/` | Per-user emotional state JSON | Yes |
| `data/memories/self_model.json` | Self-model observations | Yes |
| `data/memories/session_state.json` | Session continuity summary | Yes |
| `data/memories/sdk_token.txt` | Auth token for the SDK server | Regenerated on boot |
| `data/memories/sdk_allowlist.json` | Per-client SDK approval list and capability ceilings | Yes (all clients revert to denied-by-default) |
| `data/memories/memoryinit.json` | Optional seed file, imported on first boot when the collection is empty | Yes |
| `data/vtubeStudio_token.txt` | VTube Studio plugin auth token | Requires re-authorizing VTS |

### Library and uploads

Uploads move through this pipeline: web chat → `data/library/` → ChromaDB `library_docs` collection.

| Path | Purpose |
|---|---|
| `data/library/documents/` | Uploaded PDF, DOCX, and DOC |
| `data/library/images/` | Uploaded images (PNG, JPG, GIF, WebP, BMP) |
| `data/library/text/` | Uploaded text-like files (TXT, MD, JSON, YAML, CSV, LOG) |
| `data/library/**/*.meta.md` | Auto-generated sidecar per upload: upload metadata plus extracted content (VLM report for images, preview for documents, full text for text-like files). This is what makes uploads retrievable by semantic query on later turns. |

- Filenames follow `<YYYYMMDD>_<HHMMSS>_<original_name>`. Collisions get a `_1`, `_2` suffix.
- Byte-identical uploads return HTTP 409. The user can choose Replace, which removes the old file, its sidecar, and its ChromaDB chunks first. The check only hashes library files of the same size as the upload, and caches each file's hash in memory for the life of the process.
- The `LibraryIndexer` rescans every 60 seconds, indexes new files, and prunes chunks for files no longer on disk.

Deleting `data/library/` loses your uploaded documents.

### Browser and audio

| Path | Contents | Safe to delete? |
|---|---|---|
| `data/browser_profile/` | Playwright Firefox profile (cookies, logins, bookmarks) | Yes (loses browser state) |
| `data/songs/` | Local audio for the legacy `audioPlayer` module, which `main.py` does not spawn | Yes |

### Miscellaneous

| Path | Contents | Safe to delete? |
|---|---|---|
| `data/outputs/tts_output.wav` | Debug TTS output | Yes |
| `data/notes.txt` | Plugin's timestamped notes. Not gitignored | Yes (loses your notes) |
| `data/test_tone.wav` | Diagnostics tone | Yes |
| `data/temp_discord_audio_*.wav` | Temporary Discord playback audio | Yes |

### Minecraft

Written by `plugin/minecraft_bot.py`. See [`../plugin/README.md`](../plugin/README.md#files-it-writes).

| Path | Contents | Safe to delete? |
|---|---|---|
| `data/minecraft/telemetry.json` | Latest bot and loop state, read by the dashboard on `:5002` | Yes, rewritten every loop step |
| `data/minecraft/events.jsonl` | Append-only agent event log | Yes |
| `data/minecraft/skills.json` | Curriculum tiers the agent has completed | Yes (the curriculum starts over) |

## Resetting

Prefer the web UI when it has a button for the job. Settings → Advanced exposes five reset targets:

| Goal | UI action |
|---|---|
| Wipe chat history | Settings → Advanced → Reset `history` |
| Wipe session continuity | Settings → Advanced → Reset `session` |
| Wipe self-model observations | Settings → Advanced → Reset `self_model` |
| Wipe the diagnostics log | Settings → Advanced → Reset `logs` |
| Wipe long-term memory | Settings → Advanced → Reset `memory` |

The UI `memory` reset wipes only the currently logged-in user's memories. A full cross-user wipe requires the full reset below.

**Full reset.** With Lumi stopped, run from the repo root:

```powershell
Remove-Item data\* -Recurse -Force -Exclude README.md
```

On the next boot Lumi recreates the folders it needs, generates a fresh SDK token, and prompts VTube Studio to re-authorize. You will also need to register a new account, since `users.db` is gone.

**Targeted resets.**

| Goal | How |
|---|---|
| Force VTube Studio re-auth | Delete `data/vtubeStudio_token.txt` |
| Reset Playwright state | Delete `data/browser_profile/` |
| Clear the uploaded library | Delete `data/library/`. Chunks are pruned on the next `LibraryIndexer` scan. |
| Force memory re-import from seed | Delete `data/memories/chroma.db/`, place `memoryinit.json` at `data/memories/memoryinit.json`, and restart |
| Reset all SDK client approvals | Delete `data/memories/sdk_allowlist.json` |
| Restart the Minecraft curriculum | Delete `data/minecraft/skills.json` |

## Gitignore

`.gitignore` excludes each runtime folder and file here by name (`data/memories/`, `data/logs/`, `data/library/*`, `data/minecraft/`, the VTS token, and so on), and `*.wav` and `*.log` everywhere. The SDK token, the VTS token, and the user database are all covered.

Two exceptions to know about:

- `data/notes.txt`, written by the reference plugin's `log_note`, is **not** ignored. Check `git status` before committing if you use it.
- `data/vtubeStudio_test_token.txt` is tracked in the repository.

When you add a new runtime path under `data/`, add it to `.gitignore` too.

## Backup

Two things are worth backing up: `data/memories/chroma.db/` (now holds both personal memory and the library RAG index) and `data/memories/users.db` (accounts). Everything else is regenerable or disposable.

Optionally, back up `data/library/` if you want to keep the raw uploads. ChromaDB chunks regenerate from the files as long as they are present on startup.

## See also

- `../docs/configuration.md`: where the paths are defined
- `../docs/architecture.md`: where the runtime state is used
- `../modules/README.md`: the modules that write here
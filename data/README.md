# Lumi Runtime Data

`data/` holds everything Lumi writes at runtime: logs, memory, tokens, captures, and caches. **Only this README is tracked by git.** Everything else is excluded by a `data/*` rule in `.gitignore`, which is why the folder looks empty after a fresh clone. It is safe to delete the contents on a fresh clone, and safe to clear them if you want a factory reset.

## What lives here

The last column separates disposable files from destructive deletes. "Yes" means Lumi recreates or ignores it. Anything else names what you lose.

### Logs

| Path | Contents | Safe to delete? |
|---|---|---|
| `data/logs/chat_history.json` | Active conversation history | Yes (loses backfill) |
| `data/logs/events.log` | Diagnostics event stream | Yes |
| `data/logs/visual_memory.json` | Recent screen descriptions | Yes |
| `data/logs/images/` | Timestamped screen captures (JPEG) | Yes |
| `data/logs/screenshots/` | Plugin-captured screenshots (PNG) | Yes |

### Memories and credentials

| Path | Contents | Safe to delete? |
|---|---|---|
| `data/memories/chroma.db/` | ChromaDB vector store (long-term memory). A directory, not a file. | Loses memory |
| `data/memories/users.db` | SQLite user accounts and sessions | Loses accounts |
| `data/memories/emotional_state/` | Per-user emotional state JSON | Yes |
| `data/memories/self_model.json` | Self-model observations | Yes |
| `data/memories/session_state.json` | Session continuity summary | Yes |
| `data/memories/sdk_token.txt` | Auth token for the SDK server | Regenerated on boot |
| `data/vtubeStudio_token.txt` | VTube Studio plugin auth token | Requires re-authorizing VTS |

### Models and assets

| Path | Contents | Safe to delete? |
|---|---|---|
| `data/models/` | XTTS model weights | Re-downloadable |
| `data/voices/` | Fish Audio reference clips | Re-upload |
| `data/songs/` | Local audio for the legacy player | Yes |
| `data/library/` | User documents for `doc_manager` | Yes (loses your documents) |
| `data/browser_profile/` | Playwright Firefox profile | Yes |

### Miscellaneous

| Path | Contents | Safe to delete? |
|---|---|---|
| `data/outputs/tts_output.wav` | Debug TTS output | Yes |
| `data/notes.txt` | Plugin's timestamped notes | Yes (loses your notes) |
| `data/test_tone.wav` | Diagnostics tone | Yes |
| `data/temp_discord_audio_*.wav` | Temporary Discord playback audio | Yes |

<!-- VERIFY: confirm the "loses your documents/notes" qualifiers on library/ and notes.txt; the brief lists both as plain "Yes". -->

## Resetting

Prefer the web UI when it has a button for the job. Go to **Settings, then Advanced** to wipe long-term memory or chat history without touching files.

<!-- VERIFY: confirm Settings -> Advanced exposes both memory and history resets, not just memory. -->

**Full reset.** With Lumi stopped, run from the repo root:

```powershell
Remove-Item data\* -Recurse -Force -Exclude README.md
```

On the next boot Lumi recreates the folders it needs, generates a fresh SDK token, and prompts VTube Studio to re-authorize. You will also need to register a new account, since `users.db` is gone.

**Targeted resets.**

| Goal | How |
|---|---|
| Wipe chat history | Settings, then Advanced |
| Wipe long-term memory | Settings, then Advanced |
| Force VTube Studio re-auth | Delete `data/vtubeStudio_token.txt` |
| Reset Playwright state | Delete `data/browser_profile/` |

## Gitignore

`data/*` is excluded from git with a single exception for `data/README.md`. Nothing written here is ever committed. The SDK token, the VTS token, and the user database all live here, so the same rule keeps them out of the repo.

## Backup

Two things are worth backing up: `data/memories/chroma.db/` (long-term memory) and `data/memories/users.db` (accounts). Everything else is regenerable or disposable.

## See also

- `../docs/configuration.md`: where the paths are defined
- `../docs/architecture.md`: where the runtime state is used
- `../modules/README.md`: the modules that write here
# Scripts

Standalone utilities for Lumi. `main.py` does not import or run any of them. Run them by hand when you need to inspect audio hardware, check your environment, backfill long-term memory from a chat log, or reset state. Run every command from the workspace root.

## Scripts

| Script | Purpose | Run |
|---|---|---|
| `check_mics.py` | Lists PyAudio input devices and their indices | `python scripts/check_mics.py` |
| `check_outputs.py` | Lists PyAudio output devices and their indices | `python scripts/check_outputs.py` |
| `check_system.py` | Prints Python version, key package versions, GPU info, and port availability | `python scripts/check_system.py` |
| `import_chat_history.py` | One-shot importer that backfills long-term memory from a chat log | `python scripts/import_chat_history.py --dry-run` |
| `setup_tunnel.py` | Exposes the web client through a Cloudflare Tunnel (`check`, `quick`, `setup`, `run`). Shares `modules/tunnel.py` with `main.py`'s `AUTO_START_TUNNEL`. See [../docs/remote-access.md](../docs/remote-access.md) | `python scripts/setup_tunnel.py check` |
| `setup_skin.py` | Installs Lumi's Minecraft skin for the CustomSkinLoader mod, so `LumiBot` stops showing up as Steve. See [setup_skin.py](#setup_skinpy) | `python scripts/setup_skin.py --dry-run` |
| `reset_lumi_history.ps1` | Clears chat history and session state | `.\scripts\reset_lumi_history.ps1` |

## setup_skin.py

Run it once after installing the CustomSkinLoader mod (Fabric 1.19 build) in
your Minecraft client. It needs Pillow, which Lumi already uses.

```powershell
python scripts/setup_skin.py --dry-run   # show what would change
python scripts/setup_skin.py
```

What it does, in order:

1. Takes the skin: `sdk/examples/minecraft/lumi_skin.png`, or `--source
   <png>`. It must be a real skin layout (64x64, 64x32, or a square HD skin);
   anything else is refused. `--from-sheet` rebuilds the original skin from
   her reference sheet in `assets/branding/`. The sheet is a character drawing,
   not a skin, so a crop of it wouldn't work.
2. Writes it to `sdk/examples/minecraft/lumi_skin.png` (served by `bot.js` on
   `:3001`; the previous one becomes `lumi_skin.bak.png`) and to
   `<.minecraft>/CustomSkinLoader/LocalSkin/skins/<MC_USERNAME>.png`.
3. Puts the LocalSkin loader first in `CustomSkinLoader.json`, keeping the
   other entries and settings and saving the old file as
   `CustomSkinLoader.json.bak`. If the file doesn't exist yet, it creates one
   with LocalSkin first.
4. Clears the mod's cache folders.

It changes nothing if the existing `CustomSkinLoader.json` isn't valid JSON,
and it warns, with the download link, if it can't find the mod in your mods
folder.

| Option | Use |
|---|---|
| `--dry-run` | Print what would change and change nothing |
| `--source <png>` | Install another skin |
| `--from-sheet` | Rebuild the skin from the reference sheet |
| `--minecraft-dir <dir>` | A launcher instance's `.minecraft` folder (default `%APPDATA%\.minecraft`) |
| `--username <name>` | The in-game name (default `MC_USERNAME` from `sdk/examples/minecraft/.env`) |

## import_chat_history.py

Backfills `data/memories/chroma.db` from `data/logs/chat_history.json`. Use it when the memory database is empty but you have earlier conversations saved.

**Close Lumi before running it.** ChromaDB holds an exclusive lock on `data/memories/chroma.db` while `main.py` is running, and the importer cannot open the database until Lumi exits. If the script fails on startup with a lock or database error, this is almost always the cause.

Always dry-run first. It shows the facts that would be stored without writing anything:

```powershell
python scripts/import_chat_history.py --dry-run
```

If the extracted facts look sensible, run it for real:

```powershell
python scripts/import_chat_history.py
```

| Flag | Effect |
|---|---|
| `--dry-run` | Show what would be stored. Writes nothing. |
| `--wipe` | Delete the entire memory collection before importing. Destructive. |
| `--chunk-size N` | Messages per reflection chunk. Default 12. |

The importer skips facts that already exist (exact text match), so running it twice does not create duplicates.

## check_mics.py and check_outputs.py

Both print PyAudio device indices. Put the index you want in `constants.py`, for example `OUTPUT_DEVICE_INDEX` for the speaker Lumi plays through.

Run them again after any change to your audio setup: installing VB-Cable, plugging in or unplugging a headset, or updating an audio driver. Device indices can shift when the device list changes, and a stale index sends audio to the wrong device or fails silently.

## check_system.py

Run this first when something is wrong. It prints:

- Python version
- Versions of the key packages Lumi depends on
- GPU information
- Whether ports 5000 (web client), 5001 (log hub), and 5056 (SDK server) are free

A port reported as in use usually means another Lumi process is still running. Close it before starting `main.py`.

## reset_lumi_history.ps1

PowerShell only. It gives Lumi a fresh start without touching what she has learned.

<!-- VERIFY: confirm exact files cleared, and whether Lumi must be closed first. -->

| Clears | Leaves alone |
|---|---|
| Chat history (`data/logs/chat_history.json`) | Long-term memories (`data/memories/chroma.db`) |
| Session state (`data/memories/session_state.json`) | Persona (`lumi.yaml`) |
| | Configuration |

To wipe long-term memory as well, use the web client's Settings, Advanced page, or `python scripts/import_chat_history.py --wipe` when re-importing.

## See also

- [`../docs/configuration.md`](../docs/configuration.md) — setting audio device indices in `constants.py`
- [`../docs/troubleshooting.md`](../docs/troubleshooting.md) — common failures and fixes
- [Installation](../README.md#installation) in the main README
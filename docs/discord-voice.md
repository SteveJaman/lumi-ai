# Discord Voice

Lumi joins Discord voice channels, hears every speaker, knows who's in
the room, and talks back — with Discord's DAVE end-to-end encryption
correctly decrypted in pure Python.

## Why this was hard

Discord enforces **DAVE E2EE** on all non-Stage voice channels. The
rollout completed 2026-03-02 — every voice channel now requires it.

The Python ecosystem was not ready:

- `discord.py` 2.7+ implements DAVE for the **send** path only.
- `discord-ext-voice-recv` predates enforcement and has no receive-side
  DAVE support at all.
- Existing workarounds used a C# sidecar process, which is a large
  maintenance burden for a hobby project.

Lumi bridges the gap in pure Python with a `DaveDecryptor` layer that:

1. Reads the `DaveSession` `discord.py` already negotiated on the
   connection.
2. Decrypts each received frame with the Python `davey` module — the
   same Rust implementation AIRI uses, with a maintained Python binding.
3. Feeds the decoded Opus to a per-SSRC decoder pool before VAD and
   faster-whisper.

No sidecar. No C#. One dependency (`davey`) and one extra layer.

## Receive pipeline

```
Opus frame (transport-decrypted by voice_recv)
  │
  ▼
DAVE MLS decrypt (davey.DaveSession.decrypt)
  │
  ▼
Opus decode (one decoder per SSRC)
  │
  ▼
Stereo → mono downmix at 48 kHz
  │
  ▼
RMS VAD with hysteresis
  │
  ▼
Buffer until 500 ms silence
  │
  ▼
Resample to 16 kHz
  │
  ▼
faster-whisper transcription
  │
  ▼
signals.add_message("user", "Name: text", source="discord_voice")
```

Each stage is a separate class so a failure in one doesn't cascade.
`DaveDecryptor` is lazy — it resolves the session on first decrypt
because at sink construction time the voice client isn't attached yet.

**One Opus decoder per SSRC.** Opus decoders carry inter-frame state,
so mixing two speakers through one decoder corrupts both. The pool
creates a decoder per SSRC on demand and clears on disconnect.

## Send path

```
Fish Audio WAV bytes
  │
  ▼
Write to data/temp_discord_audio_{0|1}.wav  (double-buffered)
  │
  ▼
discord.FFmpegPCMAudio
  │
  ▼
Discord VoiceClient
```

Temp files alternate between two slots so a new utterance doesn't
overwrite the file currently being played. Both are under `data/` and
gitignored.

The `after` callback on `vc.play()` fires with an error if playback
failed. A common failure mode is **FFmpeg silently no-op'ing** — the
callback reports clean completion while nothing reaches Discord. If
voice send doesn't work but the logs say it did, verify FFmpeg.

## VAD design

Speech detection runs at 48 kHz with RMS-based hysteresis:

| Parameter | Value | Why |
|---|---|---|
| `rms_start` | 0.020 | User has begun speaking |
| `rms_stop` | 0.010 | Hysteresis — must drop here to end |
| `min_speech_ms` | 250 | Discards utterances shorter than this |
| `silence_ms` | 500 | Half a second of quiet ends the buffer |
| `frame_duration_ms` | 20 | One Opus frame |

**Why RMS, not peak amplitude.** Peak-amplitude VAD is too spiky — a
single loud sample trips it, so breath sounds and background noise hold
the buffer open between words. RMS is smoother and matches the local
mic path's feel.

**Why hysteresis.** With a single threshold, the buffer opens and closes
on ambient noise around the boundary. Two thresholds (higher to start,
lower to stop) mean once speech begins, it takes genuine silence to end
it.

**Why 500 ms.** The old 800 ms window merged "Hello... Hello." into one
transcript. 500 ms matches typical conversational VAD (300–500 ms range)
and fires transcription half a second after you stop talking.

## Barge-in

When a user starts talking, Lumi stops mid-sentence. Same behavior as
the local mic path. The VAD fires on speech start, not on transcription
— so the interruption feels instant.

## Group awareness

`signals.voice_context` carries the channel roster, human count, and
mode:

```python
{
    "active": True,
    "guild_name": "Some Server",
    "guild_id": "12345...",
    "channel_name": "general-voice",
    "roster": [
        {"id": "...", "display_name": "Anthony", "is_bot": False},
        {"id": "...", "display_name": "Lumi", "is_bot": True},
    ],
    "human_count": 1,
    "mode": "solo",   # or "group" if human_count > 1
}
```

The prompter injects a `[Discord Voice Context]` block on every turn.
The persona's "GROUP AWARENESS" rule tells her she's in a room, not on a
call. When a message starts with `<Name>: `, that's who's talking.

Roster is refreshed via `on_voice_state_update` whenever anyone joins,
leaves, or moves in a guild the bot is connected to. The roster never
goes stale.

## Setup

### Required `.env`

```
DISCORD_BOT_TOKEN=your_bot_token
DISCORD_VOICE_CHANNEL_ID=000000000000000000
DISCORD_CHANNEL_ID=000000000000000000
```

`DISCORD_VOICE_CHANNEL_ID` is optional — if set, the bot auto-joins on
boot. Manual `!join` / `!leave` always work.

`DISCORD_CHANNEL_ID` is for the text channel where the bot reads chat
messages.

### Dependencies

```
discord.py>=2.7
davey
discord-ext-voice-recv
faster-whisper
```

Install:

```powershell
pip install "discord.py>=2.7" davey
```

The module's `_dave_preflight()` checks for `davey.DaveSession.decrypt`
and `davey.MediaType` before connecting. A missing or outdated `davey`
raises a clear error rather than failing silently.

### FFmpeg

FFmpeg must be on PATH. Verify:

```powershell
ffmpeg -version
```

A broken installation — commonly a dead conda shim shadowing a working
winget build — makes `FFmpegPCMAudio` silently no-op. No error, no
audio, and the `after` callback fires "cleanly." Verify FFmpeg **before**
debugging anything else.

If FFmpeg isn't installed:

```powershell
winget install Gyan.FFmpeg
```

Restart the terminal afterwards so the PATH update takes effect.

### First boot

On first voice connect, Discord asks the bot to authorize as a plugin.
The auth flow is handled by `discord.py` and `davey` transparently — no
user interaction needed.

## Operational rule: never reconnect at runtime

**Never disconnect and reconnect the voice client at runtime.**

A reconnect leaves the DAVE session out of sync with the sink's SSRC
mapping. The sink goes deaf silently — no error, no dropped frames, no
warnings. Lumi just stops hearing anyone.

If voice receive fails:

1. Check `!voice_stats` for the sink counters.
2. If `frames_in` is climbing but `decoded` is 0, the DAVE session is
   out of sync.
3. **Restart the process.** Do not add reconnect logic.

This rule is also documented in the module docstring of
`modules/discordClient.py`. No reconnect helpers exist in the codebase
and none should be added.

## Diagnostics

Discord bot commands:

| Command | Effect |
|---|---|
| `!join` | Joins the voice channel the caller is in |
| `!leave` | Leaves the current voice channel |
| `!ping` | Bot latency check |
| `!voice_debug` | Full voice state, roster, DAVE status, sink counters |
| `!voice_stats` | One-line snapshot |
| `!test_play` | 2-second 440 Hz tone that bypasses Fish Audio |
| `!test_url` | Streams a known-good MP3 from the internet |

### `!voice_stats` output

```
frames_in=1234  decoded=1230  dave_ok=1230  dave_drop=0  opus_fail=4  speaking=0
```

| Field | Meaning |
|---|---|
| `frames_in` | Opus frames received from the transport layer |
| `decoded` | Successfully DAVE-decrypted and Opus-decoded |
| `dave_ok` | Frames that passed DAVE decryption |
| `dave_drop` | Frames DAVE refused (key rotation, etc.) |
| `opus_fail` | Frames Opus couldn't decode |
| `speaking` | Users currently in a VAD-active state |

### Reading the numbers

- **`frames_in` climbing, `decoded` stuck at 0** — DAVE out of sync.
  Restart the process.
- **`frames_in` at 0** — sink not attached. Check boot log for
  `voice_recv` errors.
- **`dave_drop` growing during speech** — key rotation in progress.
  Normal for a few frames; not a problem unless it never recovers.
- **`opus_fail` climbing** — corrupted frames. Usually transient.

### `!test_play` vs `!test_url`

- `!test_play` generates a 440 Hz sine wave and plays it. Bypasses Fish
  Audio entirely. Tests the Discord send path only.
- `!test_url` streams a known-good MP3 from the internet. Tests FFmpeg
  and the Discord send path together.

If `!test_play` works but Lumi's voice doesn't, Fish Audio is the
problem. If neither works, FFmpeg is the problem.

## Server-mute auto-clear

If an admin muted the bot on the server side, that mute state persists
across reconnects and **silently drops all outgoing audio**. The client
calls `_clear_server_voice_flags()` on every connect and after every
`!join`.

If voice send doesn't work after joining a new guild, check the debug
output for `server_mute` and `server_deaf`.

## Clean terminal

STT transcripts, Discord text messages, and voice events all route to
the log hub dashboard under `mic` and `discord` categories instead of
printing to the terminal. The console stays reserved for real
conversation.

Live view at `http://127.0.0.1:5001/`, filter by category.

## Known limitations

### Frame drops on session boundaries

Discord voice drops **15–20% of frames** on session boundaries. This is
universal to DAVE — every frame that lands during a key rotation or a
speaker's join transition gets dropped by the receiving session. The
middle of each utterance always decodes cleanly.

Not fixable at the application layer. It's a property of the E2EE
protocol.

### No mid-utterance barge-in

Barge-in works between utterances, not during Lumi's own speech. If she's
mid-sentence and someone starts talking, the VAD fires and stops the TTS
playback, but the current sentence may cut off. Same as the local mic
path.

### Roster depends on Discord's state

If Discord's roster event is delayed (rare, but happens during gateway
hiccups), `signals.voice_context` lags for a few seconds. The next
`on_voice_state_update` fixes it.

### No reconnect

See the operational rule above. If voice receive fails, restart the
process.

## Files

| File | Role |
|---|---|
| `modules/discordClient.py` | The whole voice bridge |
| `modules/signals.py` | `voice_context` and `last_voice_speaker` live here |
| `modules/prompter.py` | Injects the voice context block |
| `config/lumi.yaml` | The "GROUP AWARENESS" persona rule |
| `data/temp_discord_audio_*.wav` | Temp playback files (gitignored) |
| `data/test_tone.wav` | Generated by `!test_play` (gitignored) |

## Related docs

- [architecture.md](architecture.md) — where Discord voice sits in the
  turn flow
- [configuration.md](configuration.md) — `.env` keys for Discord
- [known-limitations.md](known-limitations.md) — frame drop behavior
  and other cross-cutting issues
- [troubleshooting.md](troubleshooting.md) — silent voice receive and
  send debugging
- [`../modules/README.md`](../modules/README.md) — the modules folder
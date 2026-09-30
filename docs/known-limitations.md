# Known Limitations

Lumi is designed as a localized, single-process AI companion. While
highly capable, the architecture makes specific trade-offs regarding
external platform integrations, state management, and rendering.

**Note on security.** Prior to Phase 9 this file listed prompt
injection, command execution, SDK auth, and memory poisoning as known
gaps. All of those are now closed. See [security.md](security.md) for
the current threat model and the guards that back it. What remains
here is architectural, not defensive.

## Discord Voice Bridge (DAVE E2EE)

* **Frame drops on session boundaries:** Discord voice drops 15–20% of
  frames during DAVE session boundaries. Any frame arriving during a key
  rotation or a speaker's join transition is dropped by the receiving
  session. This is an inherent property of the E2EE protocol and cannot
  be fixed at the application layer.

* **No mid-utterance barge-in:** Barge-in works between utterances, not
  during Lumi's active speech generation. If a user speaks mid-sentence,
  the VAD fires and stops TTS playback, but the current audio buffer may
  cut off abruptly.

* **State-dependent roster events:** `signals.voice_context` relies on
  Discord's gateway. If Discord's `on_voice_state_update` event lags
  during gateway hiccups, the roster will be stale for a few seconds
  until the next update.

* **Strict no-reconnect rule:** Disconnecting and reconnecting the voice
  client at runtime leaves the DAVE session out of sync with the sink's
  SSRC mapping, causing the bot to silently go deaf. If voice receive
  fails, the entire process must be restarted. No runtime reconnect
  logic should be added to `modules/discordClient.py`.

## Visual & Avatar Rendering

* **External VTube Studio Dependency:** Unlike browser-native VTuber
  frameworks, Lumi does not contain a native WebGL/Three.js rendering
  engine. She relies entirely on an external VTube Studio instance for
  2D Live2D rendering, managed via `modules/vtubeStudio.py`. If VTube
  Studio crashes or the local websocket drops, visual lip-sync and
  emotional expressions will fail silently until the connection is
  restored.

* **VTS state drift on manual hotkey presses.** The expression state
  tracker in `modules/vtubeStudio.py` records each toggle flip it
  performs, but it cannot observe hotkeys the user presses directly in
  VTS. If you toggle an expression by hand, Lumi's idea of the current
  state is stale until the next `set_expression` call. Use
  `!expressions` to see what she thinks the state is.

## Audio & VAD Pipeline

* **FFmpeg silent no-ops:** The `discord.FFmpegPCMAudio` sink can
  silently fail if the system FFmpeg installation is broken (e.g., a
  dead conda shim shadowing a valid winget build). The `after` callback
  will report a clean completion even though no audio is actually
  transmitting to Discord.

## Memory & Local RAG

* **Single-process database lock:** The ChromaDB memory pipeline
  initializes as a persistent local client embedded directly in the
  `data/memories/chroma.db` directory. It is strictly locked to the
  active Python process. Running a second instance of Lumi
  simultaneously, or attempting to write memories from a standalone
  script while the main agent loop is running, will trigger SQLite
  database lock errors.

* **Memory reflection prompt lets through facts about Lumi herself.**
  The reflection pass occasionally extracts a fact whose subject is the
  assistant rather than the user ("Lumi is sarcastic"). The poisoning
  guards in Phase 9.8 reject imperative phrasing and URL patterns but do
  not yet reject self-referential facts outright. Low-impact — these
  facts are rarely retrieved — but a known quality issue.

## Session & History

* **History trim at 20 messages caps backfill depth.** The prompter
  trims `signals.history` to the last 20 messages. Long conversations
  lose older turns. The memory reflection pass captures facts before
  they're trimmed, so durable information survives, but verbatim context
  does not.

## SDK

* **`actions/force` is stubbed but not wired.** The wire protocol
  supports `actions/force` and both server and client can send it, but
  the prompter hook that bypasses normal tool selection is not built. A
  client sending it gets an error back.

* **The SDK client library is not pip-installable.** Third-party
  developers must copy `sdk/protocol.py`, `sdk/actions.py`,
  `sdk/client.py`, and `sdk/errors.py` into their project manually.
  Packaging as `lumi-sdk` is planned but not shipped.

## Model Behavior

* **Model pickiness on tool call format.** Small models occasionally
  emit tool calls as prose, as bracketed pseudo-calls, or as malformed
  JSON. The prompter has a multi-format recovery chain (see
  [architecture.md](architecture.md)) but the recovery isn't perfect.
  When the model refuses to emit a properly formatted call, the turn
  ends with a prose response instead of a tool result.

* **VTS multi-item fabrications.** If a user asks Lumi to change several
  appearance items at once, she may claim to have done all of them after
  only calling one tool. Low priority.
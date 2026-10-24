# Known Limitations

Lumi is designed as a localized, single-process AI companion. While
highly capable, the architecture makes specific trade-offs regarding
external platform integrations, state management, and rendering.

**Note on security.** Prior to Phase 9 this file listed prompt
injection, command execution, SDK auth, and memory poisoning as known
gaps. All of those are now closed. See [security.md](security.md) for
the current threat model and the guards that back it. One defensive gap
is open: the Minecraft bot's control API on `:5057` is unauthenticated
(see [Minecraft](#minecraft) below). Everything else here is
architectural.

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

* **Old turns collapse into a summary.** When `signals.add_message()`
  pushes history past 25 entries, `quantize_history()` replaces the older
  turns with one summary block and keeps the last 10 verbatim. Only
  microphone and Discord input go through `add_message()`; the terminal,
  web chat, and the prompter's replies append to `signals.history`
  directly and don't trigger the collapse themselves. On boot only the
  last 30 saved entries are loaded. Long conversations lose verbatim
  context; the memory reflection pass captures durable facts first.

## Web Client

* **Chat is polled, not pushed.** New messages appear on the next
  `/api/history` poll (up to 1.5 s late) and status on the next
  `/api/status` poll (up to 2 s). socket.io carries only TTS audio to the
  browser.

* **Only the last 100 messages are shown.** `/api/history` is capped at 100
  entries per request; there is no "load older" in the chat view.

* **Replies are not streamed.** The chat client can patch the newest
  bubble in place, but the backend writes each reply to history once, when
  it is complete.

* **Revoked sessions keep open sockets.** Revoking a session from
  Settings → Security stops its HTTP requests immediately, but a socket.io
  connection it already opened stays connected until the tab closes. That
  socket can still receive TTS audio and send the legacy control events.

* **Logout has no CSRF check.** `/auth/logout` doesn't verify origin or
  CSRF, so a cross-site page can sign you out. It cannot do anything else.

* **Chat messages are emitted twice on the socket.** `/api/chat/send` emits
  `web_message` and the history broadcaster emits it again. Harmless while
  the browser ignores chat on the socket; it has to be fixed before chat
  moves to socket.io.

* **Browser support for voice and motion.** Hold-to-talk needs
  `MediaRecorder` (the button is disabled without it). The smooth mood-accent
  fade needs CSS `@property`; older browsers switch colours instantly.

* **The Dashboard is a snapshot.** The web Dashboard page fetches the log
  hub's events once per visit or Refresh click. The log hub's own page at
  `127.0.0.1:5001` re-polls every 2 seconds and is the live view.

## SDK

* **`actions/force` is stubbed but not wired.** The wire protocol
  supports `actions/force` and both server and client can send it, but
  the prompter hook that bypasses normal tool selection is not built. A
  client sending it gets an error back.

* **The SDK client library is not pip-installable.** Third-party
  developers must copy `sdk/protocol.py`, `sdk/actions.py`,
  `sdk/client.py`, and `sdk/errors.py` into their project manually.
  Packaging as `lumi-sdk` is planned but not shipped.

* **Invocations time out after 30 s.** `INVOKE_TIMEOUT_SECONDS` in
  `sdk/server.py` is fixed. Lumi's model gets `action timed out` while
  the client may still be working.

## Minecraft

* **The bot's control API has no authentication.** Anything that can
  reach `127.0.0.1:5057`, including a web page in your browser, can make
  `bot.js` run arbitrary JavaScript. Run `bot.js` only while using it.
  Details and the planned fix are in
  [security.md](security.md#minecraft-bot-control-api).

* **No LLM-written skills.** Voyager's curriculum, action and critic
  agents are not used, because they need the OpenAI API. The game agent
  chooses among fixed, hand-written skills (the tech tree, free-play goals,
  house, branch mine). It does not learn new ones.

* **Pathfinding gives up on hard paths.** Mineflayer's pathfinder plans for at
  most `PATH_THINK_MS` (10 s) and then reports "Took to long to decide path to
  goal!", typically for ores behind solid rock or logs high in a canopy. Such
  steps keep whatever they collected, and the agent falls back to branch
  mining or exploring, but a specific block may stay out of reach.

* **Orders are parsed, not understood.** Common phrasings ("get me 5
  diamonds", "follow me", "give me the pickaxe") are handled in code;
  anything else depends on a 3B model's judgment. Diamond tools,
  enchanting, farming and the Nether aren't supported.

* **`/tp` needs cheats.** Getting unstuck on the way back to you, or
  returning when you're out of tracking range, uses `/tp`, which a LAN world
  only allows with cheats on.

* **`look_at_minecraft` shows your client.** It captures the Minecraft window
  on this PC, which is *your* view, and the small vision model embellishes.
  LumiBot's own view is the live viewer.

* **The live viewer is memory-hungry.** It serializes every chunk for each
  open browser. A watchdog pauses it above `VIEWER_HEAP_LIMIT_MB`; with
  several viewer tabs open it pauses often.

* **A custom skin needs a client mod.** A vanilla LAN world can't receive a
  skin from an offline-mode bot. CustomSkinLoader shows it on your screen
  only, and other players need the mod and the file too.

* **Some HUD items have no icon.** The HUD uses the textures bundled with
  prismarine-viewer. Blocks without a flat item icon fall back to a face
  texture, and a few (a chest, for example) show their name instead. The HUD
  refreshes every 2 seconds, so it lags the game slightly.

* **Long skills outrun Lumi's timeout.** Mining or exploring often
  takes longer than the SDK's 30 s, so Lumi reports a timeout while the
  bot finishes the job. The autonomous agent waits up to 135 s.

* **No bow combat.** `minecrafthawkeye` isn't installed, so `shoot()`
  and `killMob` with a bow in hand fail. Melee works.

* **LAN discovery needs the local network.** Worlds are found from
  Minecraft's UDP announcement on `224.0.2.60:4445`. If that port is taken or
  firewalled, or the world is on another network, type the host and port.

* **Survival is best-effort.** Drowning and lava are handled within half a
  second, but a lava escape needs a safe spot within 6 blocks, the water-bucket
  landing needs a bucket in the inventory and some luck with timing, and
  there's no handling for suffocation, starvation without food, or the void.
  Dropped items despawn 5 minutes after death, so a far-away death may be
  unrecoverable.

* **One bot per `bot.js`.** Each process controls one account, and two
  processes need different `BOT_CONTROL_PORT` values.

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
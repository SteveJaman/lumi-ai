# Known Limitations

Lumi is designed as a localized, single-process AI companion. While highly capable, the architecture makes specific trade-offs regarding external platform integrations, state management, and rendering.

## Discord Voice Bridge (DAVE E2EE)

* **Frame drops on session boundaries:** Discord voice drops 15–20% of frames during DAVE session boundaries. Any frame arriving during a key rotation or a speaker's join transition is dropped by the receiving session. This is an inherent property of the E2EE protocol and cannot be fixed at the application layer.
* **No mid-utterance barge-in:** Barge-in works between utterances, not during Lumi's active speech generation. If a user speaks mid-sentence, the VAD fires and stops TTS playback, but the current audio buffer may cut off abruptly.
* **State-dependent roster events:** `signals.voice_context` relies on Discord's gateway. If Discord's `on_voice_state_update` event lags during gateway hiccups, the roster will be stale for a few seconds until the next update.
* **Strict no-reconnect rule:** Disconnecting and reconnecting the voice client at runtime leaves the DAVE session out of sync with the sink's SSRC mapping, causing the bot to silently go deaf. If voice receive fails, the entire process must be restarted. No runtime reconnect logic should be added to `modules/discordClient.py`[cite: 10].

## Visual & Avatar Rendering

* **External VTube Studio Dependency:** Unlike browser-native VTuber frameworks, Lumi does not contain a native WebGL/Three.js rendering engine. She relies entirely on an external VTube Studio instance for 2D Live2D rendering, managed via `modules/vtubeStudio.py`[cite: 10]. If VTube Studio crashes or the local websocket drops, visual lip-sync and emotional expressions will fail silently until the connection is restored.

## Audio & VAD Pipeline

* **FFmpeg Silent No-Ops:** The `discord.FFmpegPCMAudio` sink can silently fail if the system FFmpeg installation is broken (e.g., a dead conda shim shadowing a valid winget build). The `after` callback will report a clean completion even though no audio is actually transmitting to Discord. 

## Memory & Local RAG

* **Single-Process Database Lock:** The ChromaDB memory pipeline initializes as a persistent local client embedded directly in the `memories/chroma.db` directory[cite: 10]. It is strictly locked to the active Python process. Running a second instance of Lumi simultaneously, or attempting to write memories from a standalone script while the main agent loop is running, will trigger SQLite database lock errors.
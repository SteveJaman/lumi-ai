================================================================
  LUMI — COMPANION ARCHITECTURE ROADMAP
  Goal: Move from reactive chatbot to autonomous companion
  Reference: Neuro-sama (Vedal987), Neuro SDK (VedalAI/neuro-sdk)
  Created:  2026-09-19
  Updated:  2026-10-12

  STATUS: All ten architectural phases are complete.
          The remaining work is optional polish.
  (Streaming pipeline excluded by request)
================================================================

The core difference between Lumi and Neuro is not intelligence.
It is ARCHITECTURE. Neuro runs continuously, has internal state,
makes its own decisions, and acts without being prompted.

Lumi waits. Neuro lives.

Every phase below has closed that gap. What's left is tuning —
iteration against real use, not new subsystems.

================================================================
  CHANGELOG
================================================================
  Reverse chronological. What shipped, when, and why.

  2026-10-12
    - File upload pipeline. Web chat accepts screenshots, PDFs,
      documents, and text files. Images go through the VLM
      (qwen2.5vl, moondream fallback) at upload time; documents
      are parsed directly. Every upload is written into
      data/library/{documents,images,text}/ with an auto-generated
      .meta.md sidecar that contains the extracted content. The
      sidecar is indexed into ChromaDB collection library_docs
      immediately, so uploads are retrievable on future turns
      via the passive library injection (chat mode) or the
      search_library tool (agent mode).
    - Duplicate detection. Byte-identical uploads return HTTP 409.
      The web client shows a Replace/Cancel modal. Replace deletes
      the old file, its sidecar, and its ChromaDB chunks before
      saving the new one.
    - Per-typed subfolder routing under data/library/. Filenames
      are <timestamp>_<original>; collisions get a _1, _2 suffix.
    - LibraryIndexer walker. Rescans every 60 seconds, indexes new
      files, prunes chunks for files deleted from disk.
    - Full documentation pass. Every file under docs/ was rewritten
      or patched against the current codebase. The stale XTTS-era
      models.md was replaced. The root README and per-folder
      READMEs are consistent with the code.
    - requirements.txt cleaned. Legacy-only packages (pydub,
      pynput, pyautogui) moved to a commented legacy block.
      spacy[ja] extra dropped — nothing imports it.

  2026-09-30
    - PHASE 9 COMPLETE. All 14 security hardening items addressed.
      HIGH: provenance markers on external content (9.1), deny-by-
      default SDK allowlist (9.2), per-client capability ceiling
      (9.3). MEDIUM: command allowlist (9.4), realpath sandbox
      (9.5), log scrubber + VLM output filter (9.6), LAN exposure
      gate (9.7), memory poisoning defense (9.8), Discord rate
      limit + injection filter (9.9), self-modification guard
      (9.10). LOW: tool-call anomaly detection (9.11), encryption-
      at-rest recommendation (9.12), dependency pinning (9.13),
      incident response playbook (9.14).
    - Two new test harnesses. tests/test_phase9.py (47 offline
      guard checks) and tests/test_debug.py (14-section project
      health check).
    - requirements.lock.txt generated with pip freeze.

  2026-09-29
    - PHASE 9 PARTIAL. realpath sandbox (9.5), ALLOW_LAN_EXPOSURE
      gate (9.7), PROTECTED_PREFIXES for self-modification (9.10).
    - PHASE 8 COMPLETE. External SDK end-to-end. Server on
      127.0.0.1:5056, client library with reconnect, Neuro-shaped
      wire protocol, reference plugin demonstrated with screenshot
      and note-logging actions.

  2026-09-28
    - PHASE 7 COMPLETE. Discord voice with DAVE end-to-end
      encryption solved in pure Python via the davey module.
      DaveDecryptor bridge, per-SSRC Opus decoder pool, RMS VAD
      with hysteresis, roster tracking, group context injection.
    - PHASE 10 COMPLETE. Local web client at 127.0.0.1:5000 with
      chat, dashboard, memory browser, persona editor, model
      manager, cloud provider support, integrations page, settings
      page, setup wizard, voice in browser, per-user memory
      isolation, per-user emotional state isolation.
    - Multi-provider LLM layer. chat_sync("provider:model") with
      Ollama, OpenAI, Anthropic, Gemini, Groq, OpenRouter, and
      any OpenAI-compatible endpoint.
    - constants.py line patcher with compile-check and .bak.
    - .env writer with 0600 perms and masked-key responses.

  2026-09-27
    - PHASE 10 MVP. Web client with auth, chat, dashboard, memory
      browser, agent mode toggle, and shared history with the
      terminal.
    - Memory reflection Qwen3 thinking-mode bug fixed
      (think=False on all structured-extraction tasks).

  2026-09-19
    - Initial roadmap. Phases 1-6.5 complete: heartbeat, emotional
      state, ambient perception, episodic memory, multi-model
      orchestration, self-model, VTube Studio avatar control.

================================================================
  WHAT LUMI IS
================================================================
  A snapshot of the current architecture.

  Core loop
    - Prompter drives every turn. Command router, model routing,
      safety guards, tool dispatch.
    - signals.history is the shared state object. Terminal, mic,
      Discord, and web chat all append to it; the prompter picks
      up new messages on its next 50ms tick.
    - Every LLM call routes through chat_sync() in
      modules/llm_providers.py. Conversational paths, agent turns,
      memory reflection, session summary, self-model reflection,
      heartbeat decision.

  Agent tools
    - Built-in: read_file, write_file, list_dir, search_files,
      run_command, run_in_terminal, web_search, browse_url,
      recall_memory, search_library, vtube_set_expression,
      vtube_move.
    - SDK actions: any action registered by an external websocket
      client, exposed to the model as sdk__<name>.
    - Confirmation gates: write_file, run_command, run_in_terminal.
    - Path sandbox, protected prefixes, protected basenames,
      command allowlist, shell metacharacter refusal.

  Memory
    - ChromaDB collection neuro_collection: personal facts with
      timestamp / importance / valence / session_id / user_id.
    - Retrieval reweights: 0.50 similarity + 0.30 importance +
      0.20 recency (30-day half-life).
    - ChromaDB collection library_docs: uploaded files and their
      sidecars.
    - Per-user isolation on every read and write.

  Inner state
    - EmotionalState: valence, arousal, frustration, curiosity,
      boredom. Trait vector from lumi.yaml. Decay every 30s.
      Per-user instances keyed by HOST.user_id.
    - Six cognitive frames: play, strategy, chaos, social,
      learning, threat.
    - Self-model: emotional snapshots every 60s, behavioral
      pattern reflection every 10 min.
    - Heartbeat: fires every 30s, decides whether to speak
      unprompted. Gates on idle, cooldown, busy, late-night.

  Perception
    - Ambient perception: screen sample every 30s, VLM call only
      on window change or hash delta.
    - MultiModal: on-demand screenshot + VLM description for
      "look at my screen" questions.
    - File upload: VLM analysis of uploaded images at intake
      time.

  Voice
    - Fish Audio cloud TTS with inline emotion tags.
    - faster-whisper STT for local mic and Discord voice.
    - Barge-in via VAD. Multi-language reference selection.
    - Output routing: local speakers, Discord voice, browser
      tabs.

  Discord
    - Text channel reading, voice channel join, DAVE E2EE
      decryption in pure Python.
    - Per-user rate limit (10 msg / 60s) and injection filter.
    - Group context injection into the prompt.

  Avatar
    - VTube Studio: expression state tracking, keyboard fallback
      for hotkeys that the API silently accepts, parameter probing,
      organic motion, mouth-open sync from TTS envelope.
    - Passive emotion → expression: the first canonical Fish Audio
      tag in each reply drives the avatar.

  Web client
    - 127.0.0.1:5000, no build step. Local HTML/CSS/JS.
    - Pages: chat, dashboard, memory, persona, models,
      integrations, settings, login, setup wizard.
    - Auth: SQLite, argon2id, sliding sessions, CSRF, origin
      check, login rate limit.
    - Voice in browser via socket.io (TTS playback only; chat is
      polling).

  External SDK
    - WebSocket server on 127.0.0.1:5056.
    - Token auth, per-client allowlist, capability tiers.
    - Client library and reference plugin.
    - Modeled after Neuro SDK's wire protocol.

  Diagnostics
    - Log hub with dashboard at 127.0.0.1:5001.
    - scripts/check_system.py: machine pre-flight.
    - tests/test_debug.py: whole-project health check.
    - tests/test_phase9.py: security guard verification.
    - tests/test_comprehensive.py: integration + hardware.

================================================================
  PHASES — FINAL STATUS
================================================================

  Phase  1     Heartbeat                           COMPLETE
  Phase  2     Emotional state + frames            COMPLETE
  Phase  3     Ambient perception                  COMPLETE
  Phase  4     Episodic memory                     COMPLETE
  Phase  5     Multi-model orchestration           COMPLETE
  Phase  6     Self-model                          COMPLETE
  Phase  6.5   VTube Studio avatar control         COMPLETE
  Phase  7     Discord voice bridge                COMPLETE
  Phase  8     External SDK                        COMPLETE
  Phase  9     Security hardening                  COMPLETE
  Phase 10     Local web client                    COMPLETE

  Optional, not started:
    Phase 7.6  Silero-VAD for local mic VAD
    Phase 7.7  True presence tracking
    Phase 8.6  SDK packaged as pip-installable library
    Phase 8.7  SDKs for TypeScript, C#, other languages

================================================================
  WHAT'S LEFT
================================================================
  Nothing blocking. Everything below is optional polish. Pick
  from this list based on what actually bothers you during use.

  Pending fixes (all optional)
    [ ] Signature phrases copied verbatim.
        Lumi occasionally pastes a signature_phrases line verbatim
        instead of paraphrasing. Fix: add a rule to lumi.yaml
        through the persona editor that signature_phrases describe
        a shape, not a script.

    [ ] Memory reflection prompt lets Lumi-facts through.
        The reflection pass occasionally extracts a fact about the
        assistant rather than the user. Low-impact — these facts
        are rarely retrieved. Fix: tighten REFLECTION_PROMPT in
        modules/memory.py to explicitly reject self-referential
        facts. The current prompt already has some HARD RULES for
        this; extend them.

    [ ] History trim at 20 messages caps backfill depth.
        The prompter trims signals.history to the last 20 turns.
        Older turns are lost for backfill purposes. Fix: raise the
        trim to 100 in modules/prompter.py.

    [ ] sdk/actions/force is stubbed but not wired.
        The wire protocol supports it and both client and server
        can send it, but the prompter hook that bypasses normal
        tool selection is not built. Fix: add a prompter hook in
        modules/prompter.py:run() that reads pending force requests
        from the SDK and skips the routing decision when one is
        present.

    [ ] SDK client is not pip-installable.
        Third-party developers must copy sdk/{protocol,actions,
        client,errors}.py into their project manually. Fix:
        extract those four files into a standalone lumi-sdk
        package and publish on PyPI.

  Known architectural limitations
    These are not bugs. They are documented behaviors.

    [!] VTS multi-item request fabrications.
        On requests with more than ~3 items, Lumi sometimes
        narrates success in prose instead of firing each tool
        call. Fabrication guard catches most shapes but not all.
        Workaround: use !set terminal commands for batch
        operations.

    [!] VTS state drift on manual hotkey presses.
        If you toggle an expression by hand in VTS, the module's
        internal state tracker goes stale. Workaround: !set
        <name> on/off corrects it idempotently.

    [!] Discord voice client fragility.
        Disconnecting and reconnecting the voice client at
        runtime leaves the DAVE session out of sync with the
        sink. The bot goes silently deaf. Rule: restart the
        process. Do not add reconnect logic.

    [!] Host name baked into legacy memory facts.
        Facts imported before the per-user migration carry the
        name "anthony" in their text. New facts carry the current
        HOST.user_id. Low-impact; the read path filters by
        user_id.

    [!] moondream description quality.
        The small VLM used for ambient perception is structurally
        correct but sometimes misnames applications. Cosmetic.

    [!] Duplicate facts across reflection passes.
        The reflection loop can extract the same fact twice from
        different windows. Cosmetic.

    [!] Ambient perception first-run delay.
        The first ambient description takes a few seconds to
        appear while the VLM warms up. Cosmetic.

    [!] Model pickiness on tool-call format.
        Small models occasionally emit tool calls as prose or as
        bracketed pseudo-calls. The prompter has a five-format
        recovery chain, but the recovery is not perfect. Known.

    [!] Log hub port conflicts.
        Ports 5000 (web client), 5001 (log hub), 5056 (SDK), and
        5055 (reserved for a future voice bridge) must all be
        free at boot. Change DEFAULT_PORT in modules/log_hub.py
        if 5001 is occupied.

================================================================
  RECOMMENDED NEXT STEPS
================================================================

  Step 1  ->  USE LUMI.
              Real use drives the next round of tuning. The
              architecture is done. What's left is iteration
              against actual behavior — how she sounds when she's
              bored, how often the heartbeat fires, whether the
              memory reflection quality is good enough.

  Step 2  ->  WHEN READY: SDK PACKAGING.
              Extract sdk/{protocol,actions,client,errors}.py
              into a standalone lumi-sdk package. Publish on
              PyPI. This is the largest remaining optional item
              and unblocks external developer adoption. See the
              "Pending fixes" section above for the file list.

  Step 3  ->  OPTIONAL POLISH.
              Pick from the Pending Fixes list based on what
              actually bothers you during use. Nothing there is
              blocking.

  Step 4  ->  IF YOU WANT A V2:
              - silero-vad for local mic VAD (7.6)
              - True presence tracking (7.7)
              - Wire actions/force into the prompter
              - SDKs for TypeScript and C# (8.7)
              - Fine-tune a base model on chat logs

================================================================
  WHAT VEDAL HAS THAT THIS WON'T FULLY CLOSE
================================================================

  - 100+ hours of fine-tuning on chat logs from real viewers
  - A larger base model
  - Years of iterative tuning on the heartbeat threshold
  - A team of collaborators
  - Actual viewer interaction data
  - His own C# Discord library (VedalAI) with native voice
    receive
  - A mature, published SDK with clients in 10+ languages

  The architecture is replicable. The tuning is not.
  You will get 70-80% of the feel with 100% of the
  architecture. The last 20% is data, time, and iteration.

================================================================
  WHAT YOU HAVE CLOSED
================================================================
  The full list of shipped subsystems.

  Autonomy
    ✅ Heartbeat — autonomous initiation with idle, cooldown,
      busy, and late-night gates
    ✅ Emotional state — five variables, trait vector, decay
    ✅ Cognitive frames — six context-aware delivery modes
    ✅ Session continuity across restarts
    ✅ Self-model — pattern tracking, periodic reflection,
      sparse injection

  Voice and presence
    ✅ Canonical Fish Audio emotion tags
    ✅ Non-canonical tag stripping
    ✅ Two-anchor time injection
    ✅ Neuro-adjacent speech cadence
    ✅ Conversational prose enforcement
    ✅ Passive emotion → expression (voice and avatar in sync)
    ✅ Discord voice receive — DAVE solved in pure Python
    ✅ Discord voice send — FFmpeg + server-mute auto-clear
    ✅ Multi-speaker attribution
    ✅ Group context injection
    ✅ Roster awareness
    ✅ Voice in browser

  Memory
    ✅ Episodic memory with timestamps + importance
    ✅ Weighted memory retrieval (similarity + importance +
      recency)
    ✅ Reflection pass reliability (think=False on structured
      extraction)
    ✅ Backfill importer from chat history
    ✅ "When did we..." temporal queries
    ✅ Per-user memory isolation
    ✅ Per-user emotional state isolation

  Perception
    ✅ Continuous screen perception (ambient sampler)
    ✅ On-demand multimodal screen capture
    ✅ File upload analysis via VLM at intake time
    ✅ Library RAG with auto-generated sidecars

  Agentic tool use
    ✅ Structured tool calling via Ollama /api/chat
    ✅ Multi-format tool-call recovery (5 formats)
    ✅ Command filtering, anchored retries
    ✅ Simulated-action detection, intent-announcement guard,
      refusal guard, no-tool-call retry
    ✅ Read-only query gate
    ✅ No-op write guard, duplicate-call guard
    ✅ Protected files and protected prefixes
    ✅ Path sandbox (realpath + normcase)
    ✅ Auto-backup on overwrite
    ✅ Linux-command blocklist
    ✅ Command allowlist
    ✅ Shell metacharacter refusal

  Avatar
    ✅ VTube Studio control
    ✅ Expression state tracking + idempotent toggle
    ✅ Keyboard fallback for VTS hotkey quirks
    ✅ Natural-language alias resolution
    ✅ Fabrication guard for narrated state changes
    ✅ Organic motion, blink scheduler, mouth-open sync

  Model orchestration
    ✅ Multi-model orchestration with VRAM deduplication
    ✅ Fast/full path routing
    ✅ Multi-provider LLM support via chat_sync
    ✅ Cloud providers: OpenAI, Anthropic, Gemini, Groq,
      OpenRouter, any OpenAI-compatible
    ✅ Qwen3 thinking-mode handling per task type

  Web client
    ✅ Auth (SQLite + argon2id + sliding sessions + CSRF +
      rate limit)
    ✅ Chat with polling, tag chips, agent-mode toggle
    ✅ Dashboard with live log hub event stream
    ✅ Memory browser with search, edit, delete, export
    ✅ Persona editor with form fields, raw YAML, preview
    ✅ Model manager (list, load, unload, pull, delete,
      routing)
    ✅ Cloud provider setup (API keys, base URL overrides,
      test connection)
    ✅ Integrations page (Ollama, VTS, Discord, Fish Audio,
      Browser)
    ✅ Settings page (General, Security, Advanced, About)
    ✅ Setup wizard
    ✅ File uploads with duplicate detection
    ✅ Voice in browser

  External SDK
    ✅ Server on 127.0.0.1:5056
    ✅ Client library with reconnect
    ✅ Neuro-shaped wire protocol
    ✅ Token auth
    ✅ Per-client allowlist
    ✅ Capability tiers (read / write / dangerous)
    ✅ Confirmation gate for write actions
    ✅ Reference plugin (screenshot + note logging)

  Security
    ✅ Prompt injection provenance markers
    ✅ SDK allowlist + capability policy
    ✅ Command execution sandboxing
    ✅ Filesystem realpath sandbox
    ✅ Log scrubber + VLM output filter
    ✅ Network exposure gate
    ✅ Memory poisoning defense
    ✅ Discord rate limit + injection filter
    ✅ Self-modification guard
    ✅ Tool-call anomaly detection
    ✅ Dependency pinning
    ✅ Incident response playbook

  Diagnostics
    ✅ Structured diagnostics with localhost dashboard
    ✅ Pre-flight system check (scripts/check_system.py)
    ✅ Project health harness (tests/test_debug.py)
    ✅ Phase 9 guard verification (tests/test_phase9.py)
    ✅ Integration harness (tests/test_comprehensive.py)
    ✅ Pytest suite for pure-logic modules

  Housekeeping
    ✅ .gitignore covering every runtime path
    ✅ Per-folder READMEs (modules, web, sdk, data, tests)
    ✅ Full docs/ folder with consistent sections
    ✅ .env writer with 0600 perms and masked-key responses
    ✅ constants.py line patcher with compile-check and .bak

================================================================
  WHAT YOU HAVE NOT CLOSED
================================================================
  All optional. Nothing here blocks any current feature.

    ❌ SDK packaging for third parties (Phase 8.6)
    ❌ SDKs in other languages (Phase 8.7)
    ❌ Silero-VAD (Phase 7.6 — optional)
    ❌ True presence tracking (Phase 7.7 — optional)
    ❌ Fine-tuning a base model on chat logs

================================================================
  END OF ROADMAP
================================================================
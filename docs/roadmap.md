================================================================
  LUMI — VEDAL REPLICATION ROADMAP
  Goal: Move from reactive chatbot to autonomous companion
  Reference: Neuro-sama (Vedal987), Neuro SDK (VedalAI/neuro-sdk)
  Created:  2026-09-19
  Updated:  2026-09-30 (PHASE 9 COMPLETE. All 14 items addressed.
                        Thirteen shipped in code, 9.12 documented
                        as a BitLocker recommendation. Two test
                        harnesses added: tests/test_phase9.py and
                        tests/test_debug.py.)
  Prev:     2026-09-29 (PHASE 9 PARTIAL. Three items landed and
                        verified: 9.5 realpath sandbox, 9.7 LAN
                        exposure gate, 9.10-protected-prefixes.)
  (Streaming pipeline excluded by request)
================================================================

The core difference between Lumi and Neuro is not intelligence.
It is ARCHITECTURE. Neuro runs continuously, has internal state,
makes its own decisions, and acts without being prompted.

Lumi waits. Neuro lives.

Every item below closes that gap. Ordered by dependency —
later items assume earlier ones are done.

================================================================
  PROGRESS SUMMARY
================================================================

  Phases fully complete:  1, 2, 3, 4, 5, 6, 6.5, 7, 8, 9, 10
  Phases remaining:       none

  Optional polish only:   7.6 (silero-vad), 7.7 (presence tracking),
                          8.6/8.7 (SDK packaging for third parties)

  Notable delta since last update (2026-09-30):

    - PHASE 9 COMPLETE. All 14 items addressed.

      HIGH priority (all shipped and verified via tests/test_phase9.py):

        9.1  Prompt injection defense
             - wrap_tool_result() in modules/agent_tools.py wraps
               every external-content tool result in [FROM FILE],
               [FROM WEB], [FROM MEMORY], [FROM SDK] markers.
             - PROVENANCE MARKERS rule added to the agent-mode
               prompt block in modules/prompter.py.
             - run_agent_turn wraps every tool result before
               appending it to the message list.

        9.2  SDK authentication and allowlist
             - Deny-by-default allowlist at
               data/memories/sdk_allowlist.json.
             - Unapproved clients connect but their action
               registrations are refused.
             - Auto-adds a denied template entry on first connect
               so the user has something to edit.
             - is_action_allowed() consulted by the prompter
               before dispatch.

        9.3  Capability categories
             - Per-client capability ceiling enforced at
               registration AND at dispatch time.
             - 'write' actions require interactive y/n unless
               the allowlist entry sets auto_approve_writes=true.
             - client_autoapproves_writes() consulted by the
               prompter.

      MEDIUM priority (all shipped):

        9.4  Command execution sandboxing
             - _COMMAND_ALLOWLIST gates run_command and
               run_in_terminal.
             - _SHELL_METACHAR refuses pipes, chains, redirects,
               and $() expansion.
             - _LINUX_ONLY_COMMAND returns a Windows hint.

        9.6  API key and token handling
             - _SECRET_PATTERNS in modules/log_hub.py redacts
               sk-*, AIza*, Discord tokens, bearer tokens, and
               long hex strings before write.
             - _vlm_output_safe() in modules/multimodal.py and
               modules/ambient_perception.py rejects key-shaped
               VLM output.
             - Memory reflection already rejects key-like hex.

        9.8  Memory poisoning defense
             - _is_poisoned_fact() in modules/memory.py rejects
               URLs, emails, imperative language, long hex
               strings, and out-of-workspace paths.
             - REFLECTION_IMPORTANCE_CAP = 0.7 caps model-extracted
               importance. Manual entries bypass the cap.
             - Fixed a duplication bug that was silently
               overwriting the cap with an un-capped second pass.

        9.9  Discord message safety
             - Per-user rate limit: 10 messages per 60s.
             - _looks_like_injection() rejects common jailbreak
               openers before they reach signals.history.

        9.10 Self-modification guard
             - PROTECTED_PREFIXES: modules/, sdk/, web/,
               data/memories/, data/API/.
             - PROTECTED_BASENAMES catches bare-filename writes
               that would create stray shadows at workspace root.
             - read_only_refusals counter bounds the retry loop
               when the model re-emits a refused write_file call.
             - Verified end-to-end.

      LOW priority:

        9.11 Tool call anomaly detection
             - note_tool_call() is a proper method on SelfModel
               (was nested inside __init__ and never ran).
             - Wired into run_agent_turn after every tool dispatch.
             - Fires a hub warning at 20 calls per session for
               run_command, run_in_terminal, and write_file.

        9.12 Encryption at rest
             - Documented in docs/security.md as a BitLocker /
               EFS recommendation. No in-app encryption.

        9.13 Dependency pinning
             - requirements.lock.txt generated with pip freeze.
             - README installation section references it.

        9.14 Incident response playbook
             - docs/security.md written. Four sections:
               prompt injection, SDK abuse, token leak, full
               system reset.

    - NEW TEST HARNESSES.

      tests/test_phase9.py — 47 offline checks covering every
        Phase 9 guard as a function AND as a source pattern.
        Runs in ~2 seconds. Does not start Lumi, open sockets,
        or call any LLM.

      tests/test_debug.py — 14-section whole-project health
        check. Workspace layout, syntax, imports, constants,
        persona YAML, signals, agent tools, Ollama connectivity,
        storage, ports, config files, module shape, smoke tests,
        optional deps. ~2 seconds. Used to localize a startup
        failure without hand-tracing imports.

    - HOUSEKEEPING.
      tests/README.md rewritten to cover both pytest suite and
        the standalone harnesses.

  Earlier deltas:
    2026-09-29 PM: Phase 9 partial (9.5, 9.7, 9.10-prefixes).
    2026-09-29 PM: Phase 8 complete. External SDK end-to-end.
    2026-09-28 PM3: Phase 7 complete (Discord voice with DAVE).
    2026-09-28 PM2: Phase 10 complete.
    2026-09-28 PM:  Phase 10.10, 10.12, 10.13, 10.14 shipped.
    2026-09-28 AM:  Provider, model manager, persona backends.
    2026-09-27:     Phase 10 MVP.
    2026-09-19:     Initial roadmap.

  Known outstanding (all optional polish):
    - Signature-phrases rule still not added to lumi.yaml.
    - Memory reflection prompt still lets through facts about
      Lumi herself.
    - History trim at 20 messages caps backfill depth.
    - ruamel.yaml may not be installed; persona editor falls back
      to PyYAML with a UI banner.
    - sdk/actions/force is stubbed but not wired to the prompter.
    - SDK client is not pip-installable yet (8.6).
    - SDKs for other languages not written (8.7).

================================================================
  PHASE 1 — THE HEARTBEAT (foundational)   ✅ COMPLETE
================================================================

[X] 1.1  Build the heartbeat task                       DONE
[X] 1.2  Give the heartbeat awareness of context        DONE
[X] 1.3  Tune the heartbeat's restraint                 DONE
[X] 1.4  Add an explicit idle-detection signal          DONE
[X] 1.5  Fix float('inf') overflow in gating            DONE

         Files:     modules/heartbeat.py
         Status:    Fires every 30s. Gates on idle, cooldown,
                    busy state, late-night thresholds. Routes
                    through chat_sync so cloud providers work.

================================================================
  PHASE 2 — COGNITIVE STATE MACHINE (emotional depth)   ✅ COMPLETE
================================================================

[X] 2.1  Define the trait vector in lumi.yaml           DONE
[X] 2.2  Define emotional state variables               DONE
[X] 2.3  Wire events to emotional deltas                DONE
[X] 2.4  Inject compressed state into the prompt        DONE
[X] 2.5  Add cognitive frames                           DONE

         Files:     lumi.yaml, modules/emotional_state.py,
                    prompter.py
         Status:    Eight traits, five state variables, six
                    frames. Per-user instances keyed by
                    HOST.user_id (Phase 10.16). HOST.on_change
                    swaps the active state on login.

================================================================
  PHASE 3 — CONTINUOUS PERCEPTION (ambient awareness)   ✅ COMPLETE
================================================================

[X] 3.1  Background screen sampler                      DONE
[X] 3.2  Feed active window title                       DONE
[X] 3.3  Change-detection logic                         DONE
[X] 3.4  Inject ambient context selectively             DONE

         Files:     modules/ambient_perception.py,
                    modules/prompter.py, modules/heartbeat.py
         Status:    Samples every 30s. 8×8 average hash +
                    hamming >14 triggers VLM. Two-tier
                    injection: window title always, screen
                    description only on screen-reference regex.

================================================================
  PHASE 4 — EPISODIC MEMORY (temporal depth)            ✅ COMPLETE
================================================================

[X] 4.1  Upgrade memory schema                          DONE
[X] 4.2  Weight retrieval by recency and importance     DONE
[X] 4.3  "When did we..." queries                       DONE
[X] 4.4  Reflection prompt crash fix                    DONE
[X] 4.5  Normalize import_json timestamps               DONE
[X] 4.6  Sanitize valence field                         DONE
[X] 4.7  Fix Qwen3 thinking-mode empty-content bug      DONE
[X] 4.8  Fix processed_count >= reset                    DONE
[X] 4.9  Backfill importer from chat_history.json       DONE

         Files:     modules/memory.py,
                    scripts/import_chat_history.py,
                    modules/prompter.py
         Status:    Every fact carries timestamp / importance
                    / valence / session_id / user_id.
                    Retrieval reweights by (0.5 sim + 0.3
                    importance + 0.2 recency). Reflection
                    routes through chat_sync.
         TODO:      Tighten reflection prompt — currently
                    lets through facts about Lumi herself.

================================================================
  PHASE 5 — MULTI-MODEL ORCHESTRATION                   ✅ COMPLETE
================================================================

[X] 5.1  Add a fast-path small model for chat           DONE
[X] 5.2  Route by task type                             DONE
[X] 5.3  Coordinate model loading                       DONE

         Files:     constants.py, prompter.py, memory.py,
                    session_state.py
         Status:    Both paths provider-aware via chat_sync.
                    Cloud specs work end to end.

================================================================
  PHASE 6 — SELF-MODEL                                  ✅ COMPLETE
================================================================

[X] 6.1  Track her own patterns                         DONE
[X] 6.2  Reflect on the self-model periodically         DONE
[X] 6.3  Ground identity in accumulated state           DONE

         Files:     modules/self_model.py, prompter.py,
                    modules/heartbeat.py
         Status:    Reflection routes through chat_sync.
                    Tool call anomaly detection wired
                    (Phase 9.11).

================================================================
  PHASE 6.5 — AVATAR CONTROL (VTube Studio)             ✅ COMPLETE
================================================================

[X] 6.5.1  VTS controller module                        DONE
[X] 6.5.2  Expression state tracking                    DONE
[X] 6.5.3  Keyboard fallback path                       DONE
[X] 6.5.4  Agent tools                                  DONE
[X] 6.5.5  Natural-language alias resolution            DONE
[X] 6.5.6  Tool-call format recovery                    DONE
[X] 6.5.7  Fabrication guard                            DONE
[X] 6.5.8  Passive emotion → expression path            DONE
[X] 6.5.9  Terminal commands                            DONE
[X] 6.5.10 Persona awareness                            DONE
[X] 6.5.11 Focus + timing reliability                   DONE
[X] 6.5.12 Multi-item fabrications                      KNOWN (low priority)

================================================================
  PHASE 7 — DISCORD VOICE BRIDGE                    ✅ COMPLETE
================================================================

[X] 7.1  C# sidecar — SUPERSEDED. Python davey module.
[X] 7.2  Python receiver module                         DONE
[X] 7.3  Multi-speaker attribution                      DONE
[X] 7.4  Persona — group context                        DONE
[X] 7.5  Roster awareness injection                     DONE
[ ] 7.6  VAD upgrade (silero-vad)                       OPTIONAL
[ ] 7.7  True presence tracking                         OPTIONAL

         Files:     modules/discordClient.py,
                    signals.py, modules/prompter.py,
                    lumi.yaml
         Status:    DAVE end-to-end encryption solved in
                    pure Python via davey + DaveDecryptor.
                    RMS VAD with hysteresis. Roster tracking
                    via on_voice_state_update. Group context
                    injected into the prompt.
                    Rate limit + injection filter added in
                    Phase 9.9.

================================================================
  PHASE 8 — EXTERNAL SDK                            ✅ COMPLETE
================================================================

Goal: External applications connect to Lumi over a local
websocket, register their own actions, push context, and
receive invocations. Lumi treats each registered action as a
tool her LLM can call, exactly like her built-in ones.
Protocol modeled after Vedal's Neuro SDK.

[X] 8.1  Protocol definition                             DONE
[X] 8.2  Server module                                   DONE
[X] 8.3  Prompter integration                            DONE
[X] 8.4  Reference client (Python)                       DONE
[X] 8.5  Fake-client test harness                        DONE
[ ] 8.6  Client library for external developers          OPTIONAL
[ ] 8.7  Official SDKs for other languages               NOT STARTED
[X] 8.8  Capability scoping                              DONE (9.3)

         Files:     sdk/__init__.py, sdk/protocol.py,
                    sdk/actions.py, sdk/client.py,
                    sdk/server.py, sdk/errors.py,
                    sdk/README.md,
                    sdk/examples/system_stats.py,
                    plugin/plugin.py,
                    modules/prompter.py,
                    main.py, constants.py
         Status:    Full SDK shipping. Server binds to
                    127.0.0.1:5056 with token auth. Client
                    library handles reconnect. Neuro-shaped
                    wire protocol. Demonstrated end-to-end
                    with a working plugin.
                    Per-client allowlist and capability
                    policy shipped in Phase 9.2/9.3.
         TODO:      Package client as pip-installable library
                    (8.6). Write SDKs for TypeScript, C# (8.7).
                    Wire actions/force to the prompter.

================================================================
  PHASE 9 — SECURITY HARDENING                        ✅ COMPLETE
================================================================

Goal: Close attack surfaces across the entire stack. Threat
model is local, not remote: malicious content Lumi reads,
malicious SDK plugin, accidental self-modification, other
local processes reaching the web client or SDK.

  In scope:   prompt injection via file/web/memory/SDK content;
              SDK hijacking; self-modification; API key
              exposure; command escalation; network exposure;
              CSRF and DNS-rebinding; session hijacking.
  Out of scope: kernel compromise, hardware attacks, physical
              access, supply-chain attacks on Ollama.

  Progress: 14 of 14 items addressed. Thirteen shipped in code
            and verified via tests/test_phase9.py. 9.12
            documented as a BitLocker recommendation.

[X] 9.1  Prompt injection defense                    HIGH  DONE
         Status: wrap_tool_result() in modules/agent_tools.py
                 wraps external-content tool results in
                 [FROM FILE] / [FROM WEB] / [FROM MEMORY] /
                 [FROM SDK] markers. Prompter injects the
                 PROVENANCE MARKERS rule and wraps results
                 before appending to the message list.
         Files:  modules/prompter.py, modules/agent_tools.py

[X] 9.2  SDK authentication and allowlist            HIGH  DONE
         Status: Deny-by-default allowlist at
                 data/memories/sdk_allowlist.json. Unapproved
                 clients connect but registrations are refused.
                 is_action_allowed() consulted by the prompter.
         Files:  sdk/server.py, modules/prompter.py,
                 data/memories/sdk_allowlist.json

[X] 9.3  Capability categories for actions           HIGH  DONE
         Status: Per-client capability ceiling enforced at
                 registration AND at dispatch. 'write' actions
                 require interactive confirmation unless the
                 allowlist entry sets auto_approve_writes=true.
         Files:  sdk/server.py, modules/prompter.py

[X] 9.4  Command execution sandboxing                MEDIUM  DONE
         Status: _COMMAND_ALLOWLIST gates run_command and
                 run_in_terminal. _SHELL_METACHAR refuses
                 pipes, chains, redirects, $() expansion.
                 _LINUX_ONLY_COMMAND returns a Windows hint.
         Files:  modules/agent_tools.py

[X] 9.5  Filesystem sandbox edge cases               MEDIUM  DONE
         Status: _safe_path uses os.path.realpath() plus
                 os.path.normcase() before the prefix check.
                 Verified: a junction pointing to System32 is
                 refused.
         Files:  modules/agent_tools.py

[X] 9.6  API key and token handling                  MEDIUM  DONE
         Status: _SECRET_PATTERNS in log_hub.py redacts
                 sk-*, AIza*, Discord tokens, bearer tokens,
                 and long hex strings before write.
                 _vlm_output_safe() in multimodal.py and
                 ambient_perception.py rejects key-shaped VLM
                 output. Memory reflection already rejects
                 key-like hex.
         Files:  modules/log_hub.py, modules/multimodal.py,
                 modules/ambient_perception.py,
                 modules/memory.py, .gitignore

[X] 9.7  Network exposure audit                      MEDIUM  DONE
         Status: ALLOW_LAN_EXPOSURE = False in constants.py.
                 All three services (log hub, socketIO, SDK)
                 read it at bind time. Default is 127.0.0.1.
         Files:  constants.py, modules/log_hub.py,
                 modules/socketIO.py, sdk/server.py

[X] 9.8  Memory poisoning defense                    MEDIUM  DONE
         Status: _is_poisoned_fact() rejects URLs, emails,
                 imperative language, long hex strings, and
                 out-of-workspace paths.
                 REFLECTION_IMPORTANCE_CAP = 0.7 caps model-
                 extracted importance. Manual entries bypass
                 the cap.
         Files:  modules/memory.py

[X] 9.9  Discord bot token and message safety        MEDIUM  DONE
         Status: Per-user rate limit: 10 messages per 60s.
                 _looks_like_injection() rejects jailbreak
                 openers before they reach signals.history.
         Files:  modules/discordClient.py

[X] 9.10 Self-modification guard                     HIGH  DONE
         Status: PROTECTED_PREFIXES: modules/, sdk/, web/,
                 data/memories/, data/API/.
                 PROTECTED_BASENAMES catches bare-filename
                 writes. read_only_refusals counter bounds
                 the retry loop. Verified end-to-end.
         Files:  modules/agent_tools.py, modules/prompter.py

[X] 9.11 Tool call anomaly detection                 LOW   DONE
         Status: note_tool_call() is a proper method on
                 SelfModel and is wired into run_agent_turn
                 after every tool dispatch. Fires a hub
                 warning at 20 calls per session for
                 run_command, run_in_terminal, write_file.
         Files:  modules/self_model.py, modules/prompter.py

[X] 9.12 Encryption of session state at rest         LOW   DONE
         Status: Documented in docs/security.md as a
                 BitLocker / EFS recommendation. No in-app
                 encryption — the cost/benefit doesn't justify
                 wrapping ChromaDB and SQLite in place.

[X] 9.13 Dependency and supply chain review          LOW   DONE
         Status: requirements.lock.txt generated with
                 pip freeze. README installation section
                 references it.
         Files:  requirements.lock.txt, README.md

[X] 9.14 Incident response playbook                  REQUIRED  DONE
         Status: docs/security.md written. Four sections:
                 prompt injection response, SDK abuse response,
                 token leak response, full system reset.
         Files:  docs/security.md

================================================================
  PHASE 10 — LOCAL WEB CLIENT                        ✅ COMPLETE
================================================================

All sub-phases 10.1-10.20 done. Full web client at
127.0.0.1:5000 with chat, dashboard, memory browser, persona
editor, model manager, cloud providers, integrations page,
settings page, setup wizard, voice in browser, per-user
memory isolation, and per-user emotional state isolation.

[X] 10.1–10.20 all shipped. See files under web/ and
    modules/{persona_editor,model_manager,settings_manager,
    integrations,llm_providers}.py.

================================================================
  TEST HARNESSES
================================================================

Three offline tools for localizing failures. None of them start
Lumi, open listening sockets, or call any LLM.

  scripts/check_system.py
    Pre-flight machine check: Python, folders, files, package
    versions, port availability, Ollama reachability, FFmpeg,
    Playwright Firefox binary, GPU/CUDA, .env presence, lumi.yaml.
    Does NOT import constants.py so it runs before the app's own
    imports resolve. First thing to run on a new machine.

  tests/test_debug.py
    Whole-project health check. 14 sections: workspace layout,
    Python syntax, imports, constants, persona YAML, signals,
    agent tools, Ollama connectivity, storage, ports, config
    files, module shape, smoke tests, optional deps. Runs in
    ~2 seconds. First thing to run when Lumi won't start.

  tests/test_phase9.py
    Phase 9 guard verification. 47 checks covering every
    security guard as a function AND as a source pattern.
    Run after any change under modules/ or sdk/.

  tests/README.md documents all three and the pytest suite.

================================================================
  PENDING FIXES
================================================================

All HIGH and MEDIUM items resolved. Remaining items are optional
polish and don't affect functionality.

[ ] Signature phrases copied verbatim                 OPTIONAL
    Fix:    Add rule via persona editor.

[ ] Memory reflection prompt lets Lumi-facts through  OPTIONAL
    Fix:    Tighten REFLECTION_PROMPT in memory.py.

[ ] History trim caps backfill depth                  OPTIONAL
    Fix:    Raise trim from 20 to 100.

[ ] actions/force not wired to prompter                OPTIONAL
    Fix:    Add a prompter hook that bypasses normal tool
            selection when an SDK client forces action names.

[ ] SDK client not pip-installable                     OPTIONAL
    Fix:    Extract sdk/{protocol,actions,client,errors}.py
            into a standalone lumi-sdk package.

================================================================
  KNOWN OUTSTANDING ISSUES
================================================================

All security-related issues closed. Remaining items are cosmetic
or architectural limitations, not bugs.

[!] VTS multi-item request fabrications                COSMETIC
[!] VTS state drift on manual hotkey presses           COSMETIC
[!] Voice client fragility (never reconnect at runtime) KNOWN
    Rule documented in discordClient.py docstring.
[!] actions/force is stubbed but not wired             KNOWN
[!] Host name baked into memory facts                  KNOWN
    Legacy backfill assigned to "anthony"; new facts
    carry the current HOST.user_id.
[!] moondream description quality                     COSMETIC
[!] Duplicate facts across reflection passes          COSMETIC
[!] Ambient perception first-run delay                COSMETIC
[!] Model pickiness on tool format                    KNOWN
[!] Log hub port conflicts risk                       KNOWN
    5001 (log hub), 5000 (web), 5056 (SDK), 5055 (voice
    bridge reserved)

================================================================
  RECOMMENDED NEXT STEPS
================================================================

  Phase 9 is complete. The remaining work is optional polish.

  Step 1  ->  USE LUMI.
              Real use drives the next round of tuning. The
              architecture is done. What's left is iteration
              against actual behavior.

  Step 2  ->  WHEN READY: SDK PACKAGING (8.6).
              Extract sdk/{protocol,actions,client,errors}.py
              into a standalone lumi-sdk package. Publish on
              PyPI. This is the largest remaining optional item
              and unblocks external developer adoption.

  Step 3  ->  OPTIONAL POLISH.
              Pick from the Pending Fixes list based on what
              actually bothers you during use. Nothing there
              is blocking.

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
  - His own C# Discord library (VedalAI) with native voice receive
  - A mature, published SDK with clients in 10+ languages

  The architecture is replicable. The tuning is not.
  You will get 70-80% of the feel with 100% of the
  architecture. The last 20% is data, time, and iteration.

  What you HAVE closed:
    ✅ Heartbeat, emotional state, cognitive frames, traits
    ✅ Canonical emotion-tag voice delivery
    ✅ Session continuity
    ✅ Command filtering, anchored retries, multi-format
      tool call recovery
    ✅ Clean shutdown across all subsystems
    ✅ Episodic memory with timestamps + importance
    ✅ Weighted memory retrieval
    ✅ Reflection pass reliability
    ✅ Backfill importer
    ✅ "When did we..." queries
    ✅ Continuous screen perception
    ✅ Structured diagnostics with dashboard
    ✅ Multi-model orchestration
    ✅ Self-model
    ✅ Neuro-adjacent speech cadence
    ✅ Conversational prose enforcement
    ✅ Two-anchor time injection
    ✅ Non-canonical tag stripping
    ✅ VTube Studio avatar control
    ✅ Expression state tracking + idempotent toggle
    ✅ Keyboard fallback for VTS hotkey quirks
    ✅ Natural-language alias resolution
    ✅ Fabrication guard for narrated state changes
    ✅ Passive emotion → expression
    ✅ VRAM deduplication
    ✅ Web client — all pages (Phase 10.1–10.20)
    ✅ Persona editor
    ✅ Model manager
    ✅ Integrations page
    ✅ Settings page
    ✅ Multi-provider LLM support
    ✅ Agent mode toggle
    ✅ Terminal + web input share one path
    ✅ Dedicated /login page
    ✅ Full setup wizard
    ✅ Per-user memory isolation
    ✅ Per-user emotional state isolation
    ✅ Voice in browser
    ✅ Cloud providers (OpenAI, Anthropic, Gemini, Groq,
      OpenRouter, any OpenAI-compatible)
    ✅ CSRF + origin + rate limit on auth
    ✅ constants.py line patcher with compile-check + .bak
    ✅ .env writer with 0600 perms + masked-key responses
    ✅ Discord voice receive — DAVE solved in pure Python
    ✅ Discord voice send — FFmpeg + server-mute auto-clear
    ✅ Multi-speaker attribution
    ✅ Group context injection
    ✅ Roster awareness
    ✅ External SDK — server, client library, protocol,
      capability tiers, reference plugin
    ✅ Screenshot + note-logging plugin demo
    ✅ Filesystem realpath sandbox (9.5)
    ✅ Network exposure gate with ALLOW_LAN_EXPOSURE (9.7)
    ✅ Protected prefixes for self-modification (9.10)
    ✅ .gitignore covering every runtime path
    ✅ Pre-flight system diagnostic script
    ✅ Prompt injection provenance markers (9.1)
    ✅ SDK per-client allowlist (9.2)
    ✅ SDK capability policy (9.3)
    ✅ Command execution sandboxing (9.4)
    ✅ Log scrubber + VLM output filter (9.6)
    ✅ Memory poisoning defense (9.8)
    ✅ Discord rate limit + injection filter (9.9)
    ✅ Tool call anomaly detection (9.11)
    ✅ Dependency pinning via requirements.lock.txt (9.13)
    ✅ Incident response playbook (9.14)
    ✅ Phase 9 verification harness (tests/test_phase9.py)
    ✅ General debug harness (tests/test_debug.py)

  What you have NOT closed (all optional):
    ❌ SDK packaging for third parties (Phase 8.6)
    ❌ SDKs in other languages (Phase 8.7)
    ❌ Silero-VAD (Phase 7.6 — optional)
    ❌ True presence tracking (Phase 7.7 — optional)
    ❌ Fine-tuning a base model on chat logs

================================================================
  END OF ROADMAP
================================================================
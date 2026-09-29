================================================================

&#x20; LUMI — VEDAL REPLICATION ROADMAP

&#x20; Goal: Move from reactive chatbot to autonomous companion

&#x20; Reference: Neuro-sama (Vedal987), Neuro SDK (VedalAI/neuro-sdk)

&#x20; Created:  2026-09-19

&#x20; Updated:  2026-09-29 (PHASE 8 COMPLETE. External SDK ships

&#x20;                       end-to-end: server, client library, protocol,

&#x20;                       capability tiers, prompt integration, working

&#x20;                       reference plugin. Only Phase 9 (security

&#x20;                       hardening) remains.)

&#x20; Prev:     2026-09-28 PM3 (Phase 7 complete — Discord voice with

&#x20;                       DAVE E2EE, multi-speaker attribution, group

&#x20;                       context. Phase 10 already complete.)

&#x20; (Streaming pipeline excluded by request)

================================================================



The core difference between Lumi and Neuro is not intelligence.

It is ARCHITECTURE. Neuro runs continuously, has internal state,

makes its own decisions, and acts without being prompted.



Lumi waits. Neuro lives.



Every item below closes that gap. Ordered by dependency —

later items assume earlier ones are done.



================================================================

&#x20; PROGRESS SUMMARY

================================================================



&#x20; Phases fully complete:  1, 2, 3, 4, 5, 6, 6.5, 7, 8, 10

&#x20; Phases remaining:       9 (security hardening)



&#x20; Optional polish:        7.6 (silero-vad), 7.7 (presence tracking),

&#x20;                         8.6/8.7 (SDK packaging for third parties),

&#x20;                         8.8 partial (capability policy refinement)



&#x20; Phase 8:                COMPLETE. External applications can now

&#x20;                         connect to Lumi over a local websocket,

&#x20;                         register their own actions, push context,

&#x20;                         receive invocations, and respond with

&#x20;                         results. The protocol is modeled after

&#x20;                         Vedal's Neuro SDK so third-party developers

&#x20;                         find the shapes familiar.



&#x20;                         Demonstrated end-to-end with a working

&#x20;                         reference plugin that captures screenshots

&#x20;                         and logs timestamped notes. Both actions

&#x20;                         route reliably because they have no

&#x20;                         vocabulary collision with any built-in

&#x20;                         tool or shell command.



&#x20; Notable delta since last update (2026-09-29):

&#x20;   - PHASE 8.1 SHIPPED (protocol definition).

&#x20;     sdk/protocol.py is the single source of truth for message

&#x20;     shapes. Client and server builders live there. Message format

&#x20;     matches Neuro: {"command": str, "game": str, "data": {...}}.

&#x20;     Commands are namespaced ("actions/register", "action/result").

&#x20;     Action payloads use JSON-Schema parameters, compatible with

&#x20;     both Ollama's tool interface and OpenAI's function calling.



&#x20;   - PHASE 8.2 SHIPPED (server module).

&#x20;     sdk/server.py exposes SDKServer, a Module that binds to

&#x20;     127.0.0.1:5056, authenticates clients with a token stored at

&#x20;     memories/sdk\_token.txt (0600 on POSIX), and manages the

&#x20;     action registry. Public methods used by the prompter:

&#x20;       get\_action\_schemas()    → tool schemas in Ollama format

&#x20;       get\_context\_injections() → free-text from connected clients

&#x20;       find\_action(tool\_name)  → resolve "sdk\_\_<name>"

&#x20;       invoke\_action\_blocking(name, args) → run a client action

&#x20;       broadcast\_lumi\_message(text) → fan out speech\_finished

&#x20;     Accepts both new {"command": ...} and legacy {"type": ...}

&#x20;     message shapes for backwards compatibility.



&#x20;   - PHASE 8.3 SHIPPED (prompter integration).

&#x20;     modules/prompter.py gained three surgical edits:

&#x20;       1. run\_agent\_turn now appends SDK schemas to the tool list

&#x20;          and prints "\[SDK] N action(s) available" on every turn.

&#x20;       2. \_dispatch\_tool\_call resolves "sdk\_\_<name>" tool calls and

&#x20;          routes them through SDKServer.invoke\_action\_blocking().

&#x20;          Honours requires\_confirm and dangerous capabilities with

&#x20;          a y/n gate before running.

&#x20;       3. run() broadcasts each assistant reply to connected SDK

&#x20;          clients via sdk.broadcast\_lumi\_message().



&#x20;   - PHASE 8.4 SHIPPED (client library).

&#x20;     sdk/client.py provides LumiClient, an importable base class.

&#x20;     Subclass it, override setup(), register actions with handlers,

&#x20;     call .run(). The library handles the websocket, auth

&#x20;     handshake, ping/pong, reconnection with backoff, action

&#x20;     registration, invocation dispatch, and result formatting.

&#x20;     Handlers may return (bool, str), a bare bool, a string, or a

&#x20;     dict — all four shapes are normalized.



&#x20;   - PHASE 8.5 SHIPPED (fake-client test harness).

&#x20;     sdk/examples/system\_stats.py is a runnable reference client

&#x20;     that registers CPU/memory/disk stats and a desktop

&#x20;     notification. Doubles as a fake-client harness.



&#x20;   - PHASE 8.8 PARTIAL (capability tiers).

&#x20;     Action specs carry capabilities=\["read"|"write"|"dangerous"]

&#x20;     and requires\_confirm=bool. The server enforces the y/n gate

&#x20;     at dispatch time. read actions auto-approve, write actions

&#x20;     log, dangerous actions always prompt. Refining the policy

&#x20;     (per-client trust levels, capability allowlists) is future

&#x20;     work.



&#x20;   - NEW FILES THIS SESSION:

&#x20;     sdk/\_\_init\_\_.py          — public exports

&#x20;     sdk/errors.py            — SDKError, AuthError, ProtocolError,

&#x20;                                ActionError, ClientDisconnected

&#x20;     sdk/protocol.py          — wire format + builders

&#x20;     sdk/actions.py           — action() builder + param() helper

&#x20;     sdk/server.py            — SDKServer module

&#x20;     sdk/client.py            — LumiClient base class

&#x20;     sdk/README.md            — third-party protocol spec

&#x20;     sdk/examples/system\_stats.py — reference client

&#x20;     plugin/plugin.py         — working demo plugin



&#x20;   - FILES MODIFIED THIS SESSION:

&#x20;     constants.py             — added SDK\_HOST, SDK\_PORT,

&#x20;                                SDK\_VERSION, LUMI\_VERSION,

&#x20;                                \_split\_model\_spec helper

&#x20;     main.py                  — imported SDKServer, added to

&#x20;                                modules list, added SDK banner

&#x20;     modules/prompter.py      — three SDK integration edits

&#x20;     requirements.txt         — added websockets, psutil



&#x20;   - DESIGN LESSON (documented for future plugin developers).

&#x20;     Small-model tool selection routes by token overlap. Three

&#x20;     plugin actions failed to fire before one worked:

&#x20;       open\_notepad         → lost to run\_in\_terminal

&#x20;       get\_time\_in(city)    → lost to web\_search's timezone handler

&#x20;       read\_clipboard       → lost to run\_command clip

&#x20;       screenshot\_\*         → succeeded (no competitor)

&#x20;     Rule: a plugin action needs a capability that no built-in

&#x20;     tool and no shell command covers. If either exists, the model

&#x20;     will route to it, because built-ins appear in the schema list

&#x20;     from turn 1 and shell commands share query vocabulary.



&#x20; Earlier deltas:

&#x20;   2026-09-28 PM3: Phase 7 complete (Discord voice with DAVE).

&#x20;   2026-09-28 PM2: Phase 10 complete.

&#x20;   2026-09-28 PM:  Phase 10.10, 10.12, 10.13, 10.14 shipped.

&#x20;   2026-09-28 AM:  Provider, model manager, persona backends.

&#x20;   2026-09-27:     Phase 10 MVP.

&#x20;   2026-09-19:     Initial roadmap.



&#x20; Known outstanding after this session:

&#x20;   - Signature-phrases rule still not added to lumi.yaml.

&#x20;   - Memory reflection prompt still lets through facts about

&#x20;     Lumi herself.

&#x20;   - History trim at 20 messages caps backfill depth.

&#x20;   - .env is not in .gitignore yet.

&#x20;   - ruamel.yaml may not be installed; persona editor falls back

&#x20;     to PyYAML with a UI banner.

&#x20;   - sdk/actions/force is stubbed but not wired to the prompter.

&#x20;     Used by turn-based games; not urgent.



================================================================

&#x20; PHASE 1 — THE HEARTBEAT (foundational)   ✅ COMPLETE

================================================================



\[X] 1.1  Build the heartbeat task                       DONE

\[X] 1.2  Give the heartbeat awareness of context        DONE

\[X] 1.3  Tune the heartbeat's restraint                 DONE

\[X] 1.4  Add an explicit idle-detection signal          DONE

\[X] 1.5  Fix float('inf') overflow in gating            DONE



&#x20;        Files:     modules/heartbeat.py

&#x20;        Status:    Fires every 30s. Gates on idle, cooldown,

&#x20;                   busy state, late-night thresholds. Routes

&#x20;                   through chat\_sync so cloud providers work.



================================================================

&#x20; PHASE 2 — COGNITIVE STATE MACHINE (emotional depth)   ✅ COMPLETE

================================================================



\[X] 2.1  Define the trait vector in lumi.yaml           DONE

\[X] 2.2  Define emotional state variables               DONE

\[X] 2.3  Wire events to emotional deltas                DONE

\[X] 2.4  Inject compressed state into the prompt        DONE

\[X] 2.5  Add cognitive frames                           DONE



&#x20;        Files:     lumi.yaml, modules/emotional\_state.py,

&#x20;                   prompter.py

&#x20;        Status:    Eight traits, five state variables, six

&#x20;                   frames. Per-user instances keyed by

&#x20;                   HOST.user\_id (Phase 10.16). HOST.on\_change

&#x20;                   swaps the active state on login.



================================================================

&#x20; PHASE 3 — CONTINUOUS PERCEPTION (ambient awareness)   ✅ COMPLETE

================================================================



\[X] 3.1  Background screen sampler                      DONE

\[X] 3.2  Feed active window title                       DONE

\[X] 3.3  Change-detection logic                         DONE

\[X] 3.4  Inject ambient context selectively             DONE



&#x20;        Files:     modules/ambient\_perception.py,

&#x20;                   modules/prompter.py, modules/heartbeat.py

&#x20;        Status:    Samples every 30s. 8×8 average hash +

&#x20;                   hamming >14 triggers VLM. Two-tier

&#x20;                   injection: window title always, screen

&#x20;                   description only on screen-reference regex.



================================================================

&#x20; PHASE 4 — EPISODIC MEMORY (temporal depth)            ✅ COMPLETE

================================================================



\[X] 4.1  Upgrade memory schema                          DONE

\[X] 4.2  Weight retrieval by recency and importance     DONE

\[X] 4.3  "When did we..." queries                       DONE

\[X] 4.4  Reflection prompt crash fix                    DONE

\[X] 4.5  Normalize import\_json timestamps               DONE

\[X] 4.6  Sanitize valence field                         DONE

\[X] 4.7  Fix Qwen3 thinking-mode empty-content bug      DONE

\[X] 4.8  Fix processed\_count >= reset                    DONE

\[X] 4.9  Backfill importer from chat\_history.json       DONE



&#x20;        Files:     modules/memory.py,

&#x20;                   modules/import\_chat\_history.py,

&#x20;                   modules/prompter.py

&#x20;        Status:    Every fact carries timestamp / importance

&#x20;                   / valence / session\_id / user\_id.

&#x20;                   Retrieval reweights by (0.5 sim + 0.3

&#x20;                   importance + 0.2 recency). Reflection

&#x20;                   routes through chat\_sync.

&#x20;        TODO:      Tighten reflection prompt — currently

&#x20;                   lets through facts about Lumi herself.



================================================================

&#x20; PHASE 5 — MULTI-MODEL ORCHESTRATION                   ✅ COMPLETE

================================================================



\[X] 5.1  Add a fast-path small model for chat           DONE

\[X] 5.2  Route by task type                             DONE

\[X] 5.3  Coordinate model loading                       DONE



&#x20;        Files:     constants.py, prompter.py, memory.py,

&#x20;                   session\_state.py

&#x20;        Status:    Both paths provider-aware via chat\_sync.

&#x20;                   Cloud specs work end to end.



================================================================

&#x20; PHASE 6 — SELF-MODEL                                  ✅ COMPLETE

================================================================



\[X] 6.1  Track her own patterns                         DONE

\[X] 6.2  Reflect on the self-model periodically         DONE

\[X] 6.3  Ground identity in accumulated state           DONE



&#x20;        Files:     modules/self\_model.py, prompter.py,

&#x20;                   modules/heartbeat.py

&#x20;        Status:    Reflection routes through chat\_sync.



================================================================

&#x20; PHASE 6.5 — AVATAR CONTROL (VTube Studio)             ✅ COMPLETE

================================================================



\[X] 6.5.1  VTS controller module                        DONE

\[X] 6.5.2  Expression state tracking                    DONE

\[X] 6.5.3  Keyboard fallback path                       DONE

\[X] 6.5.4  Agent tools                                  DONE

\[X] 6.5.5  Natural-language alias resolution            DONE

\[X] 6.5.6  Tool-call format recovery                    DONE

\[X] 6.5.7  Fabrication guard                            DONE

\[X] 6.5.8  Passive emotion → expression path            DONE

\[X] 6.5.9  Terminal commands                            DONE

\[X] 6.5.10 Persona awareness                            DONE

\[X] 6.5.11 Focus + timing reliability                   DONE

\[X] 6.5.12 Multi-item fabrications                      KNOWN (low priority)



================================================================

&#x20; PHASE 7 — DISCORD VOICE BRIDGE                    ✅ COMPLETE

================================================================



\[X] 7.1  C# sidecar — SUPERSEDED. Python davey module.

\[X] 7.2  Python receiver module                         DONE

\[X] 7.3  Multi-speaker attribution                      DONE

\[X] 7.4  Persona — group context                        DONE

\[X] 7.5  Roster awareness injection                     DONE

\[ ] 7.6  VAD upgrade (silero-vad)                       OPTIONAL

\[ ] 7.7  True presence tracking                         OPTIONAL



&#x20;        Files:     modules/discordClient.py,

&#x20;                   modules/signals.py, modules/prompter.py,

&#x20;                   lumi.yaml

&#x20;        Status:    DAVE end-to-end encryption solved in

&#x20;                   pure Python via davey + DaveDecryptor.

&#x20;                   RMS VAD with hysteresis. Roster tracking

&#x20;                   via on\_voice\_state\_update. Group context

&#x20;                   injected into the prompt.



================================================================

&#x20; PHASE 8 — EXTERNAL SDK                            ✅ COMPLETE

================================================================



Goal: External applications connect to Lumi over a local

websocket, register their own actions, push context, and

receive invocations. Lumi treats each registered action as a

tool her LLM can call, exactly like her built-in ones.

Protocol modeled after Vedal's Neuro SDK.



\[X] 8.1  Protocol definition                             DONE

\[X] 8.2  Server module                                   DONE

\[X] 8.3  Prompter integration                            DONE

\[X] 8.4  Reference client (Python)                       DONE

\[X] 8.5  Fake-client test harness                        DONE

\[ ] 8.6  Client library for external developers          OPTIONAL

\[X] 8.7  Official SDKs for other languages               NOT STARTED

\[X] 8.8  Capability scoping                              PARTIAL



&#x20;        Files:     sdk/\_\_init\_\_.py, sdk/protocol.py,

&#x20;                   sdk/actions.py, sdk/client.py,

&#x20;                   sdk/server.py, sdk/errors.py,

&#x20;                   sdk/README.md,

&#x20;                   sdk/examples/system\_stats.py,

&#x20;                   plugin/plugin.py,

&#x20;                   modules/prompter.py,

&#x20;                   main.py, constants.py

&#x20;        Status:    Full SDK shipping. Server binds to

&#x20;                   127.0.0.1:5056 with token auth. Client

&#x20;                   library handles reconnect. Neuro-shaped

&#x20;                   wire protocol. Demonstrated end-to-end

&#x20;                   with a working plugin.

&#x20;        TODO:      Package client as pip-installable library

&#x20;                   (8.6). Write SDKs for TypeScript, C# (8.7).

&#x20;                   Refine capability policy (8.8). Wire

&#x20;                   actions/force to the prompter.



================================================================

&#x20; PHASE 9 — SECURITY HARDENING                        NOT STARTED

================================================================



Goal: Close attack surfaces across the entire stack. Threat

model is local, not remote: malicious content Lumi reads,

malicious SDK plugin, accidental self-modification, other

local processes reaching the web client or SDK.



&#x20; In scope:   prompt injection via file/web/memory/SDK content;

&#x20;             SDK hijacking; self-modification; API key

&#x20;             exposure; command escalation; network exposure;

&#x20;             CSRF and DNS-rebinding; session hijacking.

&#x20; Out of scope: kernel compromise, hardware attacks, physical

&#x20;             access, supply-chain attacks on Ollama.



\[\~] 9.1  Prompt injection defense                    HIGH  PARTIAL

\[ ] 9.2  SDK authentication and allowlist            HIGH

\[ ] 9.3  Capability categories for actions           HIGH

\[ ] 9.4  Command execution sandboxing                MEDIUM

\[\~] 9.5  Filesystem sandbox edge cases               MEDIUM  PARTIAL

\[\~] 9.6  API key and token handling                  MEDIUM  PARTIAL

\[ ] 9.7  Network exposure audit                      MEDIUM

\[ ] 9.8  Memory poisoning defense                    MEDIUM

\[ ] 9.9  Discord bot token and message safety        MEDIUM

\[\~] 9.10 Self-modification guard                     HIGH  PARTIAL

\[ ] 9.11 Tool call anomaly detection                 LOW

\[ ] 9.12 Encryption of session state at rest         LOW

\[ ] 9.13 Dependency and supply chain review          LOW

\[ ] 9.14 Incident response playbook                  REQUIRED



================================================================

&#x20; PHASE 10 — LOCAL WEB CLIENT                        ✅ COMPLETE

================================================================



All sub-phases 10.1-10.20 done. Full web client at

127.0.0.1:5000 with chat, dashboard, memory browser, persona

editor, model manager, cloud providers, integrations page,

settings page, setup wizard, voice in browser, per-user

memory isolation, and per-user emotional state isolation.



\[X] 10.1–10.20 all shipped. See files under web/ and

&#x20;   modules/{persona\_editor,model\_manager,settings\_manager,

&#x20;   integrations,llm\_providers}.py.



================================================================

&#x20; PENDING FIXES

================================================================



\[ ] Signature phrases copied verbatim                 HIGH

&#x20;   Fix:    Add rule via persona editor.



\[ ] Memory reflection prompt lets Lumi-facts through  MEDIUM

&#x20;   Fix:    Tighten REFLECTION\_PROMPT in memory.py.



\[ ] .env not in .gitignore                             HIGH

&#x20;   Fix:    Add ".env", "memories/", "logs/",

&#x20;           "temp\_discord\_audio\_\*.wav", "notes.txt",

&#x20;           "test\_tone.wav" to .gitignore.



\[ ] ruamel.yaml not installed                          LOW

&#x20;   Fix:    pip install ruamel.yaml



\[ ] History trim caps backfill depth                   LOW

&#x20;   Fix:    Raise trim from 20 to 100.



\[ ] actions/force not wired to prompter                LOW

&#x20;   Fix:    Add a prompter hook that bypasses normal tool

&#x20;           selection when an SDK client forces action names.



\[ ] SDK client not pip-installable                     LOW

&#x20;   Fix:    Extract sdk/{protocol,actions,client,errors}.py

&#x20;           into a standalone lumi-sdk package.



================================================================

&#x20; KNOWN OUTSTANDING ISSUES

================================================================



\[!] VTS multi-item request fabrications

\[!] VTS state drift on manual hotkey presses

\[!] Voice client fragility (never reconnect at runtime)

\[!] SDK has no authentication beyond the token

\[!] Prompt injection not defended

\[!] File path sandbox uses string prefix matching

\[!] Self-modification allowed for anything outside

&#x20;   PROTECTED\_FILES

\[!] Host name baked into memory facts

\[!] moondream description quality                     COSMETIC

\[!] Duplicate facts across reflection passes          COSMETIC

\[!] Ambient perception first-run delay                COSMETIC

\[!] Model pickiness on tool format                    KNOWN

\[!] Log hub port conflicts risk                       KNOWN

&#x20;   5001 (log hub), 5000 (web), 5056 (SDK), 5055 (voice

&#x20;   bridge reserved)



================================================================

&#x20; RECOMMENDED NEXT STEPS

================================================================



&#x20; Step 0  ->  SMALL FIXES + HOUSEKEEPING (\~15 min).

&#x20;             (1) pip install ruamel.yaml

&#x20;             (2) Add .env, memories/, logs/, notes.txt,

&#x20;                 test\_tone.wav, temp\_discord\_audio\_\*.wav to

&#x20;                 .gitignore

&#x20;             (3) Persona editor → Rules → add "SIGNATURE

&#x20;                 PHRASES ARE VOCABULARY, NOT SCRIPT"

&#x20;             (4) Tighten REFLECTION\_PROMPT in memory.py



&#x20; Step 1  ->  USE LUMI FOR A WEEK.

&#x20;             Every major subsystem ships. Real use should

&#x20;             drive the next round of tuning.



&#x20; Step 2  ->  PHASE 9 (SECURITY HARDENING).

&#x20;             Before the SDK is exposed to third-party

&#x20;             developers. Priority order:

&#x20;               9.5  realpath() sandbox fix (one line)

&#x20;               9.10 expanded PROTECTED\_FILES

&#x20;               9.1  prompt-injection provenance tagging

&#x20;               9.2  SDK per-client auth beyond the shared token

&#x20;               9.3  capability allowlist per client

&#x20;               9.6  rotation UI, log redaction, .gitignore check



&#x20; Step 3  ->  SDK PACKAGING (8.6).

&#x20;             Extract sdk/{protocol,actions,client,errors}.py

&#x20;             into lumi-sdk. Publish on PyPI. Document for

&#x20;             third-party developers.



&#x20; Ongoing ->  Real use drives the next round of tuning.



================================================================

&#x20; WHAT VEDAL HAS THAT THIS WON'T FULLY CLOSE

================================================================



&#x20; - 100+ hours of fine-tuning on chat logs from real viewers

&#x20; - A larger base model

&#x20; - Years of iterative tuning on the heartbeat threshold

&#x20; - A team of collaborators

&#x20; - Actual viewer interaction data

&#x20; - His own C# Discord library (VedalAI) with native voice receive

&#x20; - A mature, published SDK with clients in 10+ languages



&#x20; The architecture is replicable. The tuning is not.

&#x20; You will get 70-80% of the feel with 100% of the

&#x20; architecture. The last 20% is data, time, and iteration.



&#x20; What you HAVE closed:

&#x20;   ✅ Heartbeat, emotional state, cognitive frames, traits

&#x20;   ✅ Canonical emotion-tag voice delivery

&#x20;   ✅ Session continuity

&#x20;   ✅ Command filtering, anchored retries, multi-format

&#x20;     tool call recovery

&#x20;   ✅ Clean shutdown across all subsystems

&#x20;   ✅ Episodic memory with timestamps + importance

&#x20;   ✅ Weighted memory retrieval

&#x20;   ✅ Reflection pass reliability

&#x20;   ✅ Backfill importer

&#x20;   ✅ "When did we..." queries

&#x20;   ✅ Continuous screen perception

&#x20;   ✅ Structured diagnostics with dashboard

&#x20;   ✅ Multi-model orchestration

&#x20;   ✅ Self-model

&#x20;   ✅ Neuro-adjacent speech cadence

&#x20;   ✅ Conversational prose enforcement

&#x20;   ✅ Two-anchor time injection

&#x20;   ✅ Non-canonical tag stripping

&#x20;   ✅ GPU STT pipeline

&#x20;   ✅ VTube Studio avatar control

&#x20;   ✅ Expression state tracking + idempotent toggle

&#x20;   ✅ Keyboard fallback for VTS hotkey quirks

&#x20;   ✅ Natural-language alias resolution

&#x20;   ✅ Fabrication guard for narrated state changes

&#x20;   ✅ Passive emotion → expression

&#x20;   ✅ VRAM deduplication

&#x20;   ✅ Web client — all pages (Phase 10.1–10.20)

&#x20;   ✅ Persona editor

&#x20;   ✅ Model manager

&#x20;   ✅ Integrations page

&#x20;   ✅ Settings page

&#x20;   ✅ Multi-provider LLM support

&#x20;   ✅ Agent mode toggle

&#x20;   ✅ Terminal + web input share one path

&#x20;   ✅ Dedicated /login page

&#x20;   ✅ Full setup wizard

&#x20;   ✅ Per-user memory isolation

&#x20;   ✅ Per-user emotional state isolation

&#x20;   ✅ Voice in browser

&#x20;   ✅ Cloud providers (OpenAI, Anthropic, Gemini, Groq,

&#x20;     OpenRouter, any OpenAI-compatible)

&#x20;   ✅ CSRF + origin + rate limit + sandbox

&#x20;   ✅ constants.py line patcher with compile-check + .bak

&#x20;   ✅ .env writer with 0600 perms + masked-key responses

&#x20;   ✅ Discord voice receive — DAVE solved in pure Python

&#x20;   ✅ Discord voice send — FFmpeg + server-mute auto-clear

&#x20;   ✅ Multi-speaker attribution

&#x20;   ✅ Group context injection

&#x20;   ✅ Roster awareness

&#x20;   ✅ External SDK — server, client library, protocol,

&#x20;     capability tiers, reference plugin

&#x20;   ✅ Screenshot + note-logging plugin demo



&#x20; What you have NOT closed:

&#x20;   ❌ Security hardening audit (Phase 9)

&#x20;   ❌ SDK authentication beyond shared token (Phase 9.2)

&#x20;   ❌ Prompt injection defense (Phase 9.1)

&#x20;   ❌ Self-modification guard for modules/ (Phase 9.10)

&#x20;   ❌ Filesystem sandbox realpath() fix (Phase 9.5)

&#x20;   ❌ SDK packaging for third parties (Phase 8.6)

&#x20;   ❌ SDKs in other languages (Phase 8.7)

&#x20;   ❌ Silero-VAD (Phase 7.6 — optional)

&#x20;   ❌ True presence tracking (Phase 7.7 — optional)



================================================================

&#x20; END OF ROADMAP

================================================================


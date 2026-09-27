# Lumi — Current Status

Last updated: 2026-09-27

## Phases complete

1, 2, 3, 4, 5, 6, 6.5 — all shipped and stable.

## Phases remaining

- 7 — Discord voice bridge — designed, not started
- 8 — External SDK — designed, not started
- 9 — Security hardening — designed, not started
- 10 — Local web client — designed, expanded, not started

## Next phase

Phase 10.1 — Identity refactor.

Prerequisite for the Phase 10 login flow. Replaces the
HOST_NAME constant with a mutable HOST object, replaces the
SYSTEM_PROMPT f-string with build_system_prompt(host), and
templates lumi.yaml with a <host> placeholder.

See LUMI-TODO.txt for the full checklist.

## Recently resolved

- constants.py NameError — RESOLVED
- Stale chat history contamination — RESOLVED
- Signature phrase rule in lumi.yaml — already present
- multimodal.py false-positive triggers — patched
- heartbeat.py busy gate — patched

## Notes for the reader

Discord voice bridge (Phase 7) is designed but explicitly
deferred. Do NOT report it as in-progress. It has not been
started.

Do NOT claim any constants.py NameError exists. It is resolved.
# Tests

Two kinds of test live here:

1. **Pytest suite** — small, targeted unit tests. Run under `pytest`.
2. **Standalone harnesses** — larger diagnostic scripts you run
   directly with `python`. They print their own pass/fail summary and
   exit non-zero on failure, but they are not pytest-collected.

The pytest suite needs no external services. The harnesses skip or
bypass services that are not running; `test_comprehensive.py` and
`test_vtube_motion.py` need the real hardware or app for full coverage.

## What's covered

### Pytest suite

| File | What it tests |
|---|---|
| `test_sdk_protocol.py` | Wire format builders, `ActionSpec` roundtrip |
| `test_llm_providers.py` | `parse_model_spec`, `make_model_spec`, `mask_key` |
| `test_agent_tools.py` | `_safe_path` sandbox — refuses escapes from `WORKSPACE_ROOT` |

### Standalone harnesses

| File | What it tests |
|---|---|
| `test_debug.py` | Whole-project health check: workspace layout, Python syntax, imports, constants, persona YAML, signals, agent tool schemas, Ollama connectivity, storage writability, ports, config files, module shape, smoke tests, optional deps |
| `test_all_features.py` | **End-to-end feature smoke test.** Walks every major subsystem — environment, core + optional deps, module imports, constants, ChromaDB round-trip, Ollama connectivity, agent tool schema, path sandbox, shell guards, provenance wrapping, secret redaction, web assets, SDK files/token/allowlist, ffmpeg, VTube Studio, Discord token, prompter surface. Prints a color-coded PASS/FAIL/SKIP summary. Optional subsystems (Discord, VTS, cloud providers) report SKIP rather than FAIL when unconfigured — set `LUMI_STRICT=1` to force them into failures. |
| `test_comprehensive.py` | Integration and hardware checks: audio output routing, database locks and integrity, library indexer and ChromaDB collection, prompt assembly and persona resolution, agent tool dispatch mapping |
| `test_performance.py` | Benchmarks critical path latency, memory retrieval scaling, and serialization efficiency across core modules |
| `test_vtube_motion.py` | Interactive VTube Studio motion check. Opens its own pyvts connection (separate plugin name and token) and drives each motion channel through a scripted sequence so you can confirm range by eye. Needs VTS running |

There is no dedicated Phase 9 security harness in the repo. The guards
that `test_all_features.py` covers (path sandbox, shell guards,
provenance wrapping, secret redaction) are the automated check; the
rest are verified by reading the code paths listed in
[`../docs/security.md`](../docs/security.md). The earlier edge-hardware
harness (`test_edge.py`) was removed with the edge layer in commit
`babb3fa`.

## Run

### Pytest suite

```powershell
pytest tests/
pytest tests/test_agent_tools.py -v
```

### Standalone harnesses

```powershell
python tests/test_all_features.py
python tests/test_debug.py
python tests/test_comprehensive.py
python tests/test_performance.py
python tests/test_vtube_motion.py   # VTS must be running
```

### Strict mode

`test_all_features.py` treats unconfigured optional subsystems
(Discord, VTube Studio, cloud LLM providers, Ollama) as **SKIP**, not
**FAIL**, so the suite runs clean on a fresh checkout with no external
services. When you want a hard gate — e.g. in CI or before a release —
set:

```powershell
$env:LUMI_STRICT="1"
python tests/test_all_features.py
```

Under strict mode, any SKIP becomes a non-zero exit. Use this only on
a machine where Discord, VTS, and Ollama are actually running.

### What "SKIP" means

A SKIP from `test_all_features.py` always carries a reason in the
output, e.g. `(Ollama not reachable at http://127.0.0.1:11434)` or
`(needed for Discord integration)`. If a check you *know* should pass
is skipping, the harness's function-name lookup for that check didn't
match your code — check the reason text and grep the harness for the
name it tried, then update the harness or the module.

## Interpreting failures

All standalone harnesses exit `0` on success and `1` on failure, so
they compose cleanly with shell chaining:

```powershell
python tests/test_all_features.py; if ($LASTEXITCODE -eq 0) { python tests/test_debug.py }
```

`test_all_features.py` prints a final block listing every failure by
name with its exception detail, so you can jump straight to the
offending subsystem without scrolling the per-section output.
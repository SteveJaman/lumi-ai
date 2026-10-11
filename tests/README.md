# Tests

Two kinds of test live here:

1. **Pytest suite** — small, targeted unit tests. Run under `pytest`.
2. **Standalone harnesses** — larger diagnostic scripts you run
   directly with `python`. They print their own pass/fail summary and
   exit non-zero on failure, but they are not pytest-collected.

Neither kind needs external services. Ollama, ChromaDB, Discord, and
VTube Studio are all mocked or bypassed.

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
| `test_phase9.py` | Every Phase 9 security guard in isolation: command allowlist (9.4), filesystem sandbox (9.5), log scrubber and VLM filter (9.6), LAN gate (9.7), memory poisoning (9.8), Discord safety (9.9), self-modification guard (9.10), tool anomaly detection (9.11), provenance markers (9.1), SDK allowlist and capability policy (9.2/9.3), playbook presence (9.14) |
| `test_debug.py` | Whole-project health check: workspace layout, Python syntax, imports, constants, persona YAML, signals, agent tool schemas, Ollama connectivity, storage writability, ports, config files, module shape, smoke tests, optional deps |
| `test_all_features.py` | **End-to-end feature smoke test.** Walks every major subsystem — environment, core + optional deps, module imports, constants, ChromaDB round-trip, Ollama connectivity, agent tool schema, path sandbox, shell guards, provenance wrapping, secret redaction, web assets, SDK files/token/allowlist, ffmpeg, VTube Studio, Discord token, prompter surface. Prints a color-coded PASS/FAIL/SKIP summary. Optional subsystems (Discord, VTS, cloud providers) report SKIP rather than FAIL when unconfigured — set `LUMI_STRICT=1` to force them into failures. |
| `test_edge.py` | Verifies `lumi-edge` hardware detection, execution provider resolution (CPU vs DirectML), and edge adapter initialization for ARM/Surface Pro 11 deployment |
| `test_performance.py` | Benchmarks critical path latency, memory retrieval scaling, and serialization efficiency across core modules |

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
python tests/test_phase9.py
python tests/test_edge.py
python tests/test_performance.py
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
python tests/test_all_features.py; if ($LASTEXITCODE -eq 0) { python tests/test_phase9.py }
```

`test_all_features.py` prints a final block listing every failure by
name with its exception detail, so you can jump straight to the
offending subsystem without scrolling the per-section output.
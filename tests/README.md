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
| `test_edge.py` | Verifies `lumi-edge` hardware detection, execution provider resolution (CPU vs DirectML), and edge adapter initialization for ARM/Surface Pro 11 deployment |

## Run

### Pytest suite

```powershell
pytest tests/
pytest tests/test_agent_tools.py -v
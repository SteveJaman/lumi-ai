# Tests

Pytest suite. No external services required — Ollama, ChromaDB,
Discord, and VTube Studio are all mocked or bypassed.

## What's covered

| File | What it tests |
|---|---|
| `test_sdk_protocol.py` | Wire format builders, `ActionSpec` roundtrip |
| `test_llm_providers.py` | `parse_model_spec`, `make_model_spec`, `mask_key` |
| `test_agent_tools.py` | `_safe_path` sandbox — refuses escapes from `WORKSPACE_ROOT` |

## Run

```powershell
pytest tests/
pytest tests/test_agent_tools.py -v
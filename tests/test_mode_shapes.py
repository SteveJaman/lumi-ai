"""Regression guard for the agent-harness refactor (see CLAUDE.md, scope constraints).

Chat mode, and agent mode with the harness switched off (LUMI_HARNESS=0, the legacy
"plan" path that runs one bounded agent turn), must send the model the same
message-history shape as before the refactor. The shape is the list of
(role, sorted keys, content type) for every message, plus the option keys and, for
the agent turn, the tool names offered.

The snapshot in tests/fixtures/mode_shapes_baseline.json was captured from the
pre-refactor prompter. To re-capture deliberately:
    python tests/test_mode_shapes.py --write
"""

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

SNAPSHOT = Path(__file__).resolve().parent / "fixtures" / "mode_shapes_baseline.json"

HISTORY = [
    {"role": "system", "content": "boot note"},
    {"role": "user", "content": "hello there"},
    {"role": "assistant", "content": "[sighing] Hi."},
    {"role": "system", "content": "!help output", "source": "command"},
    {"role": "user", "content": "look at this",
     "attachments": [{"name": "notes.txt", "kind": "text", "text": "abc"}]},
    {"role": "tool", "content": "earlier tool output"},
    {"role": "user", "content": "please list the files in modules"},
]


def _shape(messages):
    return [[m.get("role"), sorted(m.keys()), type(m.get("content")).__name__]
            for m in messages]


def _make_prompter(monkeypatch):
    from types import SimpleNamespace
    from modules import prompter as P
    from modules.llm_providers import LLMResponse

    p = P.Prompter.__new__(P.Prompter)
    p.signals = SimpleNamespace(history=[dict(m) for m in HISTORY])
    p.modules = []
    p.mood_manager = P.MoodManager("default")
    p.agent_mode_enabled = False
    p._turns_since_start = 0
    p._session_saved_this_run = False
    p.gather_injections = lambda *a, **k: "INJECTIONS"
    p._minecraft_connected = lambda: False
    p._minecraft_enabled = lambda: False
    p._everyday_ctx = lambda *a, **k: None

    monkeypatch.setattr(P.model_router, "route_model", lambda *a, **k: "sonnet")
    monkeypatch.setattr(P.hybrid_mode, "model_for", lambda tier, m: m)
    monkeypatch.setattr(P.hybrid_mode, "claude_code_allowed", lambda: False)
    monkeypatch.setattr(P.agent_tracker, "update", lambda *a, **k: None)
    monkeypatch.setattr(P.gaming_mode, "keep_alive", lambda v: v)
    monkeypatch.setattr(P, "everyday_tool_schemas", lambda ctx: [])
    monkeypatch.setattr(P.claude_code, "available", lambda: False)

    calls = []

    def fake_chat_sync(model, messages, tools=None, **kw):
        calls.append({"messages": [dict(m) for m in messages],
                      "tools": [t["function"]["name"] for t in (tools or [])],
                      "kw": sorted(kw.keys()),
                      "options": sorted((kw.get("options") or {}).keys())})
        return LLMResponse(content="Here you go.")

    monkeypatch.setattr(P, "chat_sync", fake_chat_sync)
    return p, calls


def capture(monkeypatch):
    p, calls = _make_prompter(monkeypatch)
    req = p.conversational_request("please list the files in modules", tier="sonnet")
    chat = {"messages": _shape(req["messages"]), "options": sorted(req["options"].keys()),
            "keys": sorted(req.keys())}

    monkeypatch.setenv("LUMI_HARNESS", "0")
    p.agent_mode_enabled = True
    asyncio.run(p.run_agent_turn("please list the files in modules"))
    first = calls[0]
    agent = {"messages": _shape(first["messages"]), "options": first["options"],
             "kw": first["kw"], "tools": first["tools"]}
    return {"chat": chat, "agent_legacy": agent}


def test_chat_and_legacy_agent_shapes_unchanged(monkeypatch):
    assert SNAPSHOT.exists(), "baseline snapshot missing; see module docstring"
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    got = capture(monkeypatch)
    assert got["chat"] == expected["chat"]
    assert got["agent_legacy"] == expected["agent_legacy"]


if __name__ == "__main__" and "--write" in sys.argv:
    mp = pytest.MonkeyPatch()
    try:
        data = capture(mp)
    finally:
        mp.undo()
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {SNAPSHOT}")

"""Context management (modules/harness/context.py) and its use in the loop."""

import asyncio
import json
import os

from modules.harness.context import (
    Compactor, ToolOutputStore, estimate_tokens, MAX_TOOL_CHARS, SUMMARY_TAG, RULES_TAG,
)
from modules.harness.loop import AgentHarness
from modules.harness.tasks import TaskStore
from modules.llm_providers import LLMResponse, ToolCall


def test_short_output_untouched(tmp_path):
    s = ToolOutputStore("sess", str(tmp_path))
    assert s.truncate("read_file", "hello") == ("hello", None)
    assert not os.path.exists(tmp_path / "sess")


def test_long_output_truncated_and_saved(tmp_path):
    s = ToolOutputStore("sess", str(tmp_path))
    big = "x" * 50_000
    text, path = s.truncate("read_file", big)
    assert text.startswith("x" * MAX_TOOL_CHARS)
    assert "\n[truncated; 46000 chars omitted]" in text
    assert len(text) < MAX_TOOL_CHARS + 200
    assert open(path, encoding="utf-8").read() == big
    assert os.path.basename(path) == "read_file_1.log"


def test_run_command_keeps_last_200_lines(tmp_path):
    s = ToolOutputStore("sess", str(tmp_path))
    out = "\n".join(f"line {i}" for i in range(1000))
    text, path = s.truncate("run_command", out)
    body = text.split("\n[truncated;")[0]
    lines = body.splitlines()
    assert len(lines) == 200 and lines[-1] == "line 999" and lines[0] == "line 800"
    assert open(path, encoding="utf-8").read() == out


def test_run_command_50k_chars_capped_at_4k(tmp_path):
    s = ToolOutputStore("sess", str(tmp_path))
    out = "\n".join("y" * 499 for _ in range(100))      # 100 lines, 50K chars
    text, path = s.truncate("run_command", out)
    body = text.split("\n[truncated;")[0]
    assert len(body) == MAX_TOOL_CHARS
    assert body.endswith("y")                          # the tail is kept
    assert os.path.getsize(path) >= 49_000


def test_compaction_archives_first_and_keeps_rules(tmp_path):
    c = Compactor("sess", num_ctx=1000, transcripts_dir=str(tmp_path), rules="1. Always call task_next.")
    msgs = [{"role": "system", "content": "SYSTEM PROMPT"}]
    for i in range(30):
        msgs.append({"role": "assistant", "content": f"step {i}?",
                     "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": f"f{i}"}}}]})
        msgs.append({"role": "tool", "name": "read_file", "content": "z" * 400})
    assert c.needs_compaction(msgs)
    out = c.compact(msgs, current_task={"id": 3, "description": "fix the bug", "acceptance_criteria": "tests pass"})
    archived = (tmp_path / "sess.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(archived[0])["type"] == "archive"
    assert len(archived) == 1 + len(msgs)              # the full transcript, before compaction
    assert out[0]["content"] == "SYSTEM PROMPT"
    assert out[1]["content"].startswith(SUMMARY_TAG)
    assert "#3 fix the bug" in out[1]["content"] and "Open questions" in out[1]["content"]
    assert "Last 3 tool outputs" in out[1]["content"]
    assert out[2]["content"].startswith(RULES_TAG) and "task_next" in out[2]["content"]
    assert estimate_tokens(out) < c.limit()
    assert out[3]["role"] != "tool"                    # no orphaned tool result


def tc(name, **a):
    return LLMResponse(tool_calls=[ToolCall(name=name, arguments=a)])


def test_loop_truncates_tool_results_and_compacts(tmp_path):
    store = TaskStore(str(tmp_path / "t.json"))
    seq = [tc("task_next")] + [tc("run_command", command=f"build {i}") for i in range(8)] + \
          [tc("task_complete", id=1, result="ok")]
    seen = []

    def chat(messages, tools):
        seen.append([dict(m) for m in messages])
        return seq.pop(0)

    h = AgentHarness(chat, lambda n, a: "\n".join("w" * 499 for _ in range(100)),
                     store=store, tasks_dir=str(tmp_path), rules="1. rule", num_ctx=4096,
                     mutating_tools={"run_command"}, session_id="s1")
    res = asyncio.run(h.run("x", [{"role": "system", "content": "S"}]))
    assert res.termination_reason == "task_complete"
    tool_msgs = [m for msgs in seen for m in msgs if m.get("role") == "tool" and m.get("name") == "run_command"]
    assert tool_msgs and all(len(m["content"]) < MAX_TOOL_CHARS + 300 for m in tool_msgs)
    assert len(os.listdir(tmp_path / "tool_outputs" / "s1")) == 8
    assert res.compactions >= 1
    assert (tmp_path / "transcripts" / "s1.jsonl").exists()
    # every call stays under the window, and rules are present after compaction
    assert max(estimate_tokens(m) for m in seen) < 4096
    assert any(m.get("content", "").startswith(RULES_TAG) for m in seen[-1])

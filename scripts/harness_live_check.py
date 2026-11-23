"""Live check of the agent harness with the model in LUMI.md (## Model).

usage: python scripts/harness_live_check.py synthetic|explore

synthetic: ~20-step task over in-memory note tools. explore: an Explore subagent over the
repo with real read-only tools. Writes only to a temp dir; prints a RESULT json line.
"""
import asyncio
import json
import os
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.chdir(REPO)

from modules.harness import config  # noqa: E402
from modules.harness.hooks import HookRegistry  # noqa: E402
from modules.harness.loop import AgentHarness  # noqa: E402
from modules.harness.subagents import SubagentRunner  # noqa: E402
from modules.harness.tasks import TaskStore, TASK_TOOL_SCHEMAS  # noqa: E402
from modules.llm_providers import chat_sync  # noqa: E402
from modules.agent_tools import TOOL_SCHEMAS, execute_tool  # noqa: E402
from modules.prompter import _recover_text_tool_calls, _result_looks_like_failure  # noqa: E402

MODEL = config.agent_model()
OPTS = {"temperature": 0.2, "num_ctx": config.num_ctx()}
THINK = config.think()
scenario = sys.argv[1]
tmp = tempfile.mkdtemp(prefix="lumi_live_")
calls = {"n": 0, "secs": 0.0}


def chat(messages, tools):
    t = time.time()
    r = chat_sync(MODEL, messages, tools=tools, think=THINK, options=OPTS, keep_alive="10m")
    calls["n"] += 1
    calls["secs"] += time.time() - t
    names = [tc.name for tc in r.tool_calls]
    print(f"  model call {calls['n']}: {time.time()-t:.1f}s tools={names} text={r.content[:90]!r}", flush=True)
    return r


NOTE_SCHEMAS = [
    {"type": "function", "function": {"name": "note_write", "description": "Save a value under a key in the notebook.",
     "parameters": {"type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
                    "required": ["key", "value"]}}},
    {"type": "function", "function": {"name": "note_read", "description": "Read the value saved under a key.",
     "parameters": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}}},
]
notes = {}


def dispatch(name, args):
    if name == "note_write":
        notes[str(args.get("key"))] = str(args.get("value"))
        return f"saved {args.get('key')}"
    if name == "note_read":
        k = str(args.get("key"))
        return notes.get(k, f"Error: no note {k}")
    if name in ("read_file", "list_dir", "search_files"):
        return execute_tool(name, args)
    return f"Error: tool {name} is not available in this test"


SYSTEM = ("You are Lumi in agent mode. You act only through tool calls. Work through the task store: "
          "call task_next, do the work with tools, then call task_complete with evidence.")

if scenario == "synthetic":
    goal = ("Use note_write to save the square of every whole number from 1 to 16, one call per number, "
            "key 'sq<N>' and value the square (e.g. key sq3 value 9). Then call note_read for sq4 and sq16 "
            "to check them, then call task_complete with the two values you read.")
    h = AgentHarness(chat, dispatch, tools=NOTE_SCHEMAS, store=TaskStore(os.path.join(tmp, "t.json")),
                     tasks_dir=tmp, hooks=HookRegistry(config.HOOKS_DIR), recover_fn=_recover_text_tool_calls,
                     failure_fn=_result_looks_like_failure, mutating_tools={"note_write"},
                     time_budget_s=1500)
    res = asyncio.run(h.run(goal, [{"role": "system", "content": SYSTEM}, {"role": "user", "content": goal}]))
    out = {"scenario": scenario, "model": MODEL, "termination_reason": res.termination_reason,
           "steps": res.steps, "duration_s": round(res.duration_s, 1), "tool_calls": res.tool_calls,
           "notes_written": len(notes), "correct": sum(1 for n in range(1, 17) if notes.get(f"sq{n}") == str(n * n)),
           "final_text": res.final_text[:300]}
elif scenario == "explore":
    goal = ("Find where the agent harness decides to stop running (its termination reasons). "
            "Delegate the search to an Explore subagent, then call task_complete with the file name.")
    store = TaskStore(os.path.join(tmp, "t.json"))
    runner = SubagentRunner(chat, dispatch, TOOL_SCHEMAS + TASK_TOOL_SCHEMAS, store, tasks_dir=tmp,
                            failure_fn=_result_looks_like_failure, max_steps=8,
                            recover_fn=_recover_text_tool_calls)
    h = AgentHarness(chat, dispatch, tools=[], store=store, tasks_dir=tmp, hooks=HookRegistry(config.HOOKS_DIR),
                     recover_fn=_recover_text_tool_calls, failure_fn=_result_looks_like_failure,
                     subagents=runner, time_budget_s=900)
    res = asyncio.run(h.run(goal, [{"role": "system", "content": SYSTEM}, {"role": "user", "content": goal}]))
    sub_log = open(os.path.join(tmp, "subagents.log"), encoding="utf-8").read() if os.path.exists(
        os.path.join(tmp, "subagents.log")) else ""
    out = {"scenario": scenario, "model": MODEL, "termination_reason": res.termination_reason,
           "steps": res.steps, "duration_s": round(res.duration_s, 1), "subagent_log": sub_log.strip()[:600],
           "main_history_len": len(h.messages), "final_text": res.final_text[:300]}
out["model_calls"] = calls["n"]
out["avg_call_s"] = round(calls["secs"] / max(1, calls["n"]), 1)
out["tasks_dir"] = tmp
print("RESULT " + json.dumps(out))

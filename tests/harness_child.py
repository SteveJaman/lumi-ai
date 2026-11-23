"""Child process for the kill/restart acceptance test (tests/test_harness_acceptance.py).

usage: python tests/harness_child.py <tasks_dir> start|resume <ready_file>

start:  seeds a goal, adds 3 subtasks, finishes the root task, starts subtask #2,
        writes <ready_file>, then hangs inside a model call until it is killed.
resume: a fresh process over the same task store; says "continue" and finishes
        whatever is open. Prints a JSON line with the result.
"""

import asyncio
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from modules.harness.hooks import HookRegistry  # noqa: E402
from modules.harness.loop import AgentHarness  # noqa: E402
from modules.harness.tasks import TaskStore  # noqa: E402
from modules.llm_providers import LLMResponse, ToolCall  # noqa: E402


def tc(name, **a):
    return LLMResponse(tool_calls=[ToolCall(name=name, arguments=a)])


def main():
    tasks_dir, mode, ready = sys.argv[1], sys.argv[2], sys.argv[3]
    store = TaskStore(os.path.join(tasks_dir, "lumi_tasks.json"))
    seen_state = []

    if mode == "start":
        script = [tc("task_add", description="sub A", depends_on=[1]),
                  tc("task_add", description="sub B", depends_on=[2]),
                  tc("task_add", description="sub C", depends_on=[3]),
                  tc("task_next"), tc("task_complete", id=1, result="planned"),
                  tc("task_next"), tc("write_file", path="a.txt", content="half")]

        def chat(messages, tools):
            if script:
                return script.pop(0)
            with open(ready, "w") as f:
                f.write(str(os.getpid()))
            time.sleep(600)                       # "thinking" when the process is killed
    else:
        def chat(messages, tools):
            seen_state.append(messages[-1]["content"])
            cur = store.current() or store.peek_next()
            if cur is None:
                return LLMResponse(content="nothing left")
            if store.current() is None:
                return tc("task_next")
            return tc("task_complete", id=cur["id"], result=f"finished #{cur['id']}")

    h = AgentHarness(chat, lambda n, a: "ok", store=store, tasks_dir=tasks_dir, rules="r",
                     hooks=HookRegistry(os.path.join(tasks_dir, "nohooks")),
                     mutating_tools={"write_file"}, session_id=f"child-{mode}")
    res = asyncio.run(h.run("build the thing" if mode == "start" else "continue",
                            [{"role": "system", "content": "S"}]))
    print(json.dumps({"termination_reason": res.termination_reason, "steps": res.steps,
                      "first_state": seen_state[0] if seen_state else "",
                      "statuses": {t["id"]: t["status"] for t in store.list()}}))


if __name__ == "__main__":
    main()

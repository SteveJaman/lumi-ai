"""Declarative multi-subagent workflows (data/workflows/*.json).

A workflow is an ordered list of subagent steps:

    {
      "name": "explore_plan_verify",
      "description": "...",
      "inputs": ["goal"],
      "steps": [
        {"id": "explore", "role": "Explore", "prompt": "Find files for {{inputs.goal}}"},
        {"id": "plan", "role": "Plan", "prompt": "Goal: {{inputs.goal}}\\nFiles:\\n{{steps.explore}}"},
        {"id": "verify", "role": "Verify", "prompt": "Is {{inputs.goal}} done?",
         "until": {"field": "pass", "equals": true}, "max_iterations": 3}
      ]
    }

Bindings: {{inputs.<name>}}, {{steps.<id>}} (the step's last output) and
{{steps.<id>.<field>}} (a field of a JSON output). `until` repeats a step until
the condition holds or `max_iterations` is reached; conditions are
{"field": f, "equals": v} on a JSON output, or {"contains": "text"}.
"""

import json
import os
import re

from modules.harness import config

MAX_ITERATIONS_CAP = 10
_BINDING = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")

WORKFLOW_RUN_SCHEMA = {
    "type": "function",
    "function": {
        "name": "workflow_run",
        "description": "Run a saved multi-subagent workflow from data/workflows by name and get its outputs.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Workflow name (file name without .json)."},
                "inputs": {"type": "object", "description": "Values for the workflow's inputs, e.g. {\"goal\": \"...\"}."},
            },
            "required": ["name"],
        },
    },
}


class WorkflowError(ValueError):
    pass


def _json(text):
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else None
    except (TypeError, ValueError):
        m = re.search(r"\{.*\}", str(text or ""), re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except ValueError:
                return None
    return None


def list_workflows(wf_dir=None):
    d = wf_dir or config.WORKFLOWS_DIR
    if not os.path.isdir(d):
        return []
    return sorted(f[:-5] for f in os.listdir(d) if f.endswith(".json"))


def load(name, wf_dir=None):
    d = wf_dir or config.WORKFLOWS_DIR
    if not re.fullmatch(r"[\w-]+", str(name or "")):
        raise WorkflowError(f"invalid workflow name {name!r}")
    path = os.path.join(d, f"{name}.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            wf = json.load(f)
    except FileNotFoundError:
        raise WorkflowError(f"no workflow {name!r}; available: {', '.join(list_workflows(d)) or 'none'}")
    except ValueError as e:
        raise WorkflowError(f"workflow {name!r} is not valid JSON: {e}")
    validate(wf)
    return wf


def validate(wf):
    steps = wf.get("steps")
    if not isinstance(steps, list) or not steps:
        raise WorkflowError("a workflow needs a non-empty 'steps' list")
    seen = set()
    for s in steps:
        sid = s.get("id")
        if not sid or sid in seen:
            raise WorkflowError(f"every step needs a unique id (got {sid!r})")
        if not s.get("role") or not s.get("prompt"):
            raise WorkflowError(f"step {sid} needs 'role' and 'prompt'")
        for ref in _BINDING.findall(s["prompt"]):
            parts = ref.split(".")
            if parts[0] == "steps" and (len(parts) < 2 or parts[1] not in seen | {sid}):
                raise WorkflowError(f"step {sid} references {ref} before it runs")
            if parts[0] not in ("steps", "inputs"):
                raise WorkflowError(f"step {sid} has unknown binding {{{{{ref}}}}}")
        seen.add(sid)


def render(template, inputs, outputs):
    def sub(m):
        parts = m.group(1).split(".")
        if parts[0] == "inputs":
            return str(inputs.get(parts[1], "")) if len(parts) > 1 else json.dumps(inputs)
        out = outputs.get(parts[1], "")
        if len(parts) == 2:
            return str(out)
        obj = _json(out) or {}
        v = obj.get(parts[2], "")
        return v if isinstance(v, str) else json.dumps(v)
    return _BINDING.sub(sub, template)


def condition_met(cond, output):
    if not cond:
        return True
    if "contains" in cond:
        return str(cond["contains"]) in str(output)
    if "field" in cond:
        obj = _json(output) or {}
        return obj.get(cond["field"]) == cond.get("equals", True)
    return True


async def run(name, inputs, spawn, wf_dir=None):
    """Run workflow `name`. `spawn(role, prompt, tools_allowed, timeout)` is
    SubagentRunner.spawn. Returns a result dict."""
    wf = load(name, wf_dir)
    inputs = dict(inputs or {})
    missing = [i for i in wf.get("inputs", []) if i not in inputs]
    if missing:
        raise WorkflowError(f"workflow {name} needs inputs: {', '.join(missing)}")
    outputs, iterations, status = {}, {}, "ok"
    for step in wf["steps"]:
        max_it = min(int(step.get("max_iterations") or 1), MAX_ITERATIONS_CAP)
        if step.get("until") and max_it < 2:
            max_it = 3
        out = ""
        for i in range(max_it):
            prompt = render(step["prompt"], inputs, outputs)
            if i:
                prompt += f"\n\nPrevious attempt (iteration {i}) output:\n{out}"
            out = await spawn(step["role"], prompt, step.get("tools_allowed"), step.get("timeout") or 300)
            outputs[step["id"]] = out
            iterations[step["id"]] = i + 1
            if condition_met(step.get("until"), out):
                break
        else:
            if step.get("until"):
                status = f"until_not_met:{step['id']}"
                if step.get("stop_on_fail", True):
                    break
    return {"workflow": name, "status": status, "outputs": outputs, "iterations": iterations}


async def tool(args, spawn, wf_dir=None):
    """workflow_run tool entry point; returns JSON text."""
    try:
        res = await run(args.get("name"), args.get("inputs") or {}, spawn, wf_dir)
    except WorkflowError as e:
        return f"Error: {e}"
    return json.dumps(res, ensure_ascii=False)

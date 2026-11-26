"""Hook registry (modules/harness/hooks.py) and the shipped block_rm_rf hook."""

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

from modules.harness.hooks import HookRegistry, PRE_TOOL_USE
from modules.harness.loop import AgentHarness
from modules.harness.tasks import TaskStore
from modules.llm_providers import LLMResponse, ToolCall

ROOT = Path(__file__).resolve().parent.parent
SHIPPED_HOOKS = ROOT / "data" / "hooks"


def _load_block_rm_rf():
    spec = importlib.util.spec_from_file_location("block_rm_rf", SHIPPED_HOOKS / "block_rm_rf.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("cmd", [
    "rm -rf /", "rm -fr build", "sudo rm -r -f x", "del /s /q C:\\temp", "DEL /S *.py",
    "rmdir /s /q data", "format c:", "mkfs.ext4 /dev/sda1", "Remove-Item -Recurse -Force x",
    "echo hi && rm -rf ~", "diskpart",
])
def test_block_rm_rf_denies(cmd):
    out = _load_block_rm_rf().pre_tool_use({"tool": "run_command", "args": {"command": cmd}})
    assert out and out["deny"] is True and "block_rm_rf" in out["reason"]


@pytest.mark.parametrize("cmd", [
    "rm file.txt", "dir /s", "python -m pytest -q", "git status", "echo format is fine",
    "ls -la", "del old.log", "python format_code.py",
])
def test_block_rm_rf_allows(cmd):
    assert _load_block_rm_rf().pre_tool_use({"tool": "run_command", "args": {"command": cmd}}) is None


def test_registry_loads_shipped_hooks():
    reg = HookRegistry(str(SHIPPED_HOOKS))
    assert "block_rm_rf.py:pre_tool_use" in reg.names(PRE_TOOL_USE)
    allowed, reason = reg.pre_tool_use("run_command", {"command": "rm -rf /"})
    assert not allowed and "rm -rf" in reason
    assert reg.pre_tool_use("read_file", {"path": "x"}) == (True, "")


def test_registry_loads_by_filename_and_skips_underscore(tmp_path):
    (tmp_path / "deny_all.py").write_text(
        "def pre_tool_use(e):\n    return {'deny': True, 'reason': 'nope ' + e['tool']}\n", encoding="utf-8")
    (tmp_path / "_disabled.py").write_text("raise SystemExit('should not load')\n", encoding="utf-8")
    (tmp_path / "broken.py").write_text("this is not python\n", encoding="utf-8")
    reg = HookRegistry(str(tmp_path))
    assert reg.pre_tool_use("x", {}) == (False, "nope x")
    assert any("broken.py" in e for e in reg.load_errors)


def test_crashing_pre_hook_fails_closed(tmp_path):
    reg = HookRegistry(str(tmp_path))
    reg.register(PRE_TOOL_USE, lambda e: 1 / 0, name="bad")
    allowed, reason = reg.pre_tool_use("run_command", {"command": "ls"})
    assert not allowed and "crashed" in reason


def test_two_identical_failures_say_stop_and_the_third_never_runs(tmp_path):
    reg = HookRegistry(str(tmp_path))
    args = {"command": "python missing.py"}
    assert reg.post_tool_use_failure("run_command", args, "Error 1") == []
    msgs = reg.post_tool_use_failure("run_command", args, "Error 2")
    assert len(msgs) == 1 and msgs[0].startswith("STOP TRYING THIS COMMAND.") and "failed 2x" in msgs[0]
    assert "different tool or tell the user you cannot do this" in msgs[0]
    allowed, reason = reg.pre_tool_use("run_command", args)              # a third try is refused
    assert not allowed and reason.startswith("STOP TRYING THIS COMMAND.")
    # different args count separately, and aren't blocked
    assert reg.post_tool_use_failure("run_command", {"command": "other"}, "e") == []
    assert reg.pre_tool_use("run_command", {"command": "other"})[0]


def test_a_program_off_the_allowlist_is_blocked_whatever_its_flags(tmp_path):
    reg = HookRegistry(str(tmp_path))
    refusal = "Error: 'ipconfig' is not on the command allowlist. Allowed: ..."
    reg.post_tool_use_failure("run_command", {"command": "ipconfig"}, refusal)
    msgs = reg.post_tool_use_failure("run_command", {"command": "ipconfig /all"}, refusal)
    assert msgs and msgs[0].startswith("STOP TRYING THIS COMMAND.")
    assert not reg.pre_tool_use("run_command", {"command": "IPCONFIG /release"})[0]
    assert reg.pre_tool_use("run_command", {"command": "git status"})[0]
    # an ordinary failure (not an allowlist refusal) doesn't block the program
    reg.post_tool_use_failure("run_command", {"command": "python a.py"}, "Exit code 1")
    reg.post_tool_use_failure("run_command", {"command": "python b.py"}, "Exit code 1")
    assert reg.pre_tool_use("run_command", {"command": "python c.py"})[0]


def _harness(tmp_path, items, dispatch, hooks, **kw):
    store = TaskStore(str(tmp_path / "t.json"))
    seq = list(items)
    seen = []

    def chat(messages, tools):
        seen.append([dict(m) for m in messages])
        return seq.pop(0) if seq else LLMResponse(content="done")

    h = AgentHarness(chat, dispatch, store=store, hooks=hooks, tasks_dir=str(tmp_path), rules="r",
                     mutating_tools={"run_command"}, **kw)
    return h, seen


def tc(name, **a):
    return LLMResponse(tool_calls=[ToolCall(name=name, arguments=a)])


def test_pre_hook_denial_blocks_run_command_in_yolo_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("LUMI_AGENT_YOLO", "1")
    ran = []
    reg = HookRegistry(str(tmp_path))
    reg.register(PRE_TOOL_USE, lambda e: {"deny": True, "reason": "run_command is off today"}
                 if e["tool"] == "run_command" else None, name="deny_run_command")
    h, seen = _harness(tmp_path, [tc("task_next"), tc("run_command", command="echo hi"),
                                  tc("task_complete", id=1, result="r")],
                       lambda n, a: ran.append(n) or "ok", reg)
    res = asyncio.run(h.run("x", [{"role": "system", "content": "S"}]))
    assert "run_command" not in ran
    denial = [m for m in seen[2] if m.get("role") == "tool" and m.get("name") == "run_command"]
    assert denial and "run_command is off today" in denial[0]["content"]
    assert res.termination_reason == "task_complete"


def test_two_failures_inject_stop_into_next_turn_and_block_the_third(tmp_path):
    reg = HookRegistry(str(tmp_path))
    ran = []

    def dispatch(n, a):
        ran.append(a.get("command"))
        return "Error: exit 1"
    items = [tc("task_next")] + [tc("run_command", command="python bad.py")] * 3 + [tc("task_fail", id=1, reason="x")]
    h, seen = _harness(tmp_path, items, dispatch, reg)
    asyncio.run(h.run("x", [{"role": "system", "content": "S"}]))
    stop = [m for m in seen[3] if "STOP TRYING THIS COMMAND" in str(m.get("content"))]
    assert stop, "the stop notice should be in the turn after the second failure"
    assert not any("STOP TRYING" in str(m.get("content")) for m in seen[2])
    assert ran == ["python bad.py", "python bad.py"]                    # the third never ran
    denied = [m for m in seen[4] if m.get("role") == "tool" and str(m.get("content")).startswith("Denied: STOP")]
    assert denied


# ---- through the real prompter approval path ----
def _prompter_run(tmp_path, monkeypatch, items, yolo, hook_src=None):
    from modules import prompter as P
    from modules.harness import config
    from tests.test_mode_shapes import _make_prompter
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    (hooks_dir / "block_rm_rf.py").write_text((SHIPPED_HOOKS / "block_rm_rf.py").read_text(encoding="utf-8"),
                                              encoding="utf-8")
    if hook_src:
        (hooks_dir / "deny_run_command.py").write_text(hook_src, encoding="utf-8")
    monkeypatch.setattr(config, "TASKS_DIR", str(tmp_path / "tasks"))
    monkeypatch.setattr(config, "HOOKS_DIR", str(hooks_dir))
    monkeypatch.setenv("LUMI_HARNESS", "1")
    if yolo:
        monkeypatch.setenv("LUMI_AGENT_YOLO", "1")
    else:
        monkeypatch.delenv("LUMI_AGENT_YOLO", raising=False)
    p, _ = _make_prompter(monkeypatch)
    p.agent_mode_enabled = True
    executed, confirmed = [], []
    monkeypatch.setattr(P, "execute_tool", lambda name, args, signals=None: executed.append((name, args)) or "Exit code 0\nok")

    async def fake_confirm(name, args, *a, **k):
        confirmed.append(name)
        return True

    p._confirm = fake_confirm
    seq = list(items)
    seen = []

    def fake_chat_sync(model, messages, tools=None, **kw):
        seen.append([dict(m) for m in messages])
        return seq.pop(0) if seq else LLMResponse(content="done")

    monkeypatch.setattr(P, "chat_sync", fake_chat_sync)
    reply = asyncio.run(p.run_agent_turn("do it"))
    return reply, executed, confirmed, seen


DENY_RUN_COMMAND = ("def pre_tool_use(e):\n"
                    "    if e['tool'] == 'run_command':\n"
                    "        return {'deny': True, 'reason': 'run_command is disabled by policy'}\n")


def test_prompter_yolo_hook_denies_run_command(tmp_path, monkeypatch):
    items = [tc("task_next"), tc("run_command", command="echo hi"), tc("task_fail", id=1, reason="denied")]
    reply, executed, confirmed, seen = _prompter_run(tmp_path, monkeypatch, items, yolo=True,
                                                     hook_src=DENY_RUN_COMMAND)
    assert executed == [] and confirmed == []
    assert any("run_command is disabled by policy" in str(m.get("content")) for m in seen[2])


def test_prompter_yolo_blocks_rm_rf_but_auto_approves_safe_command(tmp_path, monkeypatch):
    items = [tc("task_next"), tc("run_command", command="rm -rf build"),
             tc("run_command", command="echo safe"), tc("task_complete", id=1, result="ok")]
    reply, executed, confirmed, seen = _prompter_run(tmp_path, monkeypatch, items, yolo=True)
    assert executed == [("run_command", {"command": "echo safe"})]
    assert confirmed == []                      # YOLO: no human prompt for the safe one


def test_prompter_without_yolo_still_asks_for_approval(tmp_path, monkeypatch):
    items = [tc("task_next"), tc("run_command", command="echo safe"), tc("task_complete", id=1, result="ok")]
    reply, executed, confirmed, seen = _prompter_run(tmp_path, monkeypatch, items, yolo=False)
    assert confirmed == ["run_command"] and executed == [("run_command", {"command": "echo safe"})]

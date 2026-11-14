"""Claude Code as a Lumi tool: how it is launched (no shell, your login not an
API key, task on stdin), what it may touch, and that every call is approved.
A fake process stands in for claude.exe."""

import asyncio
import json
import os
import subprocess
import threading
import types

import pytest

from modules import agent_tools, claude_code as cc
from modules.prompter import Prompter


def ok_json(result="Here is the summary.", turns=3, **extra):
    return json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": result, "num_turns": turns, **extra})


class FakeProc:
    def __init__(self, out="", err="", code=0, sleep=None):
        self.out, self.err, self.returncode, self.pid, self.sleep = out, err, code, 999, sleep
        self.stdin_seen = None
    def communicate(self, input=None, timeout=None):
        if self.sleep is not None and not self.stdin_seen:
            self.stdin_seen = input
            raise subprocess.TimeoutExpired("claude", timeout)
        self.stdin_seen = input
        return self.out, self.err


@pytest.fixture
def fake(monkeypatch):
    state = types.SimpleNamespace(procs=[], cmds=[], kws=[], next=FakeProc(ok_json()))
    monkeypatch.setattr(cc, "find_cli", lambda: "C:/fake/claude.exe")
    monkeypatch.setattr(cc, "_kill_tree", lambda p: setattr(p, "killed", True))
    monkeypatch.setattr(cc, "_runs", cc.deque())

    def popen(cmd, **kw):
        state.cmds.append(cmd); state.kws.append(kw)
        p = state.next
        state.procs.append(p)
        return p
    monkeypatch.setattr(cc.subprocess, "Popen", popen)
    return state


# ── finding the program ───────────────────────────────────────────

def test_the_real_exe_is_found_behind_the_npm_shim_never_the_cmd_file(tmp_path, monkeypatch):
    npm = tmp_path / "npm"
    exe = npm / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    (npm / "claude.cmd").write_text("@echo off")
    monkeypatch.setattr(cc.shutil, "which", lambda name: str(npm / "claude.cmd"))
    assert cc.find_cli() == str(exe)
    exe.unlink()
    assert cc.find_cli() is None                                   # a .cmd shim is refused, not used


def test_not_installed_means_not_available(monkeypatch):
    monkeypatch.setattr(cc.shutil, "which", lambda name: None)
    assert cc.find_cli() is None and cc.available() is False


def test_the_off_switch_wins(monkeypatch):
    monkeypatch.setattr(cc, "find_cli", lambda: "C:/x/claude.exe")
    monkeypatch.setattr(cc.constants, "CLAUDE_CODE_ENABLED", False)
    assert cc.available() is False
    monkeypatch.setattr(cc.constants, "CLAUDE_CODE_ENABLED", True)
    assert cc.available() is True


# ── how it is launched ────────────────────────────────────────────

def args_after(cmd, flag):
    i = cmd.index(flag) + 1
    out = []
    while i < len(cmd) and not cmd[i].startswith("--"):
        out.append(cmd[i]); i += 1
    return out


def test_read_only_has_no_shell_no_web_and_no_edit_tools():
    cmd = cc.build_command("claude.exe", "read_only")
    tools = args_after(cmd, "--tools")[0].split(",")
    assert tools == ["Read", "Grep", "Glob"]
    assert not {"Bash", "WebFetch", "WebSearch", "Edit", "Write"} & set(tools)
    assert "-p" in cmd and cmd[cmd.index("--output-format") + 1] == "json" and "--no-session-persistence" in cmd
    assert cmd[cmd.index("--max-turns") + 1] == str(cc.MAX_TURNS)


def test_edit_mode_adds_only_file_editing_and_only_it_accepts_edits():
    cmd = cc.build_command("claude.exe", "edit")
    tools = args_after(cmd, "--tools")[0].split(",")
    assert tools == ["Read", "Grep", "Glob", "Edit", "Write"] and "Bash" not in tools
    assert cmd[cmd.index("--permission-mode") + 1] == "acceptEdits"
    assert "--permission-mode" not in cc.build_command("claude.exe", "read_only")     # read-only never accepts anything


def test_secrets_are_unreadable_in_both_modes_and_lumis_own_code_is_uneditable():
    ro = args_after(cc.build_command("claude.exe", "read_only"), "--disallowedTools")
    ed = args_after(cc.build_command("claude.exe", "edit"), "--disallowedTools")
    for must in ("Read(./.env)", "Read(./data/credentials/**)", "Read(./data/workspace/**)", "Read(./data/memories/**)"):
        assert must in ro and must in ed
    for must in ("Edit(./modules/**)", "Edit(./sdk/**)", "Edit(./web/**)", "Edit(./main.py)", "Edit(./constants.py)",
                 "Edit(./.env)", "Edit(./data/credentials/**)"):
        assert must in ed
    assert not any(r.startswith("Edit(") for r in ro)               # nothing to deny: it can't edit at all


def test_the_deny_list_follows_lumis_own_protections():
    ed = args_after(cc.build_command("claude.exe", "edit"), "--disallowedTools")
    for prefix in agent_tools.PROTECTED_PREFIXES:
        assert f"Edit(./{prefix.rstrip('/')}/**)" in ed
    for f in agent_tools.PROTECTED_FILES:
        assert f"Edit(./{f})" in ed


def test_an_api_key_can_never_switch_it_to_pay_per_token(monkeypatch):
    for k, v in {"ANTHROPIC_API_KEY": "sk-ant-x", "ANTHROPIC_AUTH_TOKEN": "t", "ANTHROPIC_BASE_URL": "http://proxy",
                 "CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "x", "CLAUDE_CODE_SSE_PORT": "1", "KEEP_ME": "yes"}.items():
        monkeypatch.setenv(k, v)
    env = cc.child_env()
    assert "KEEP_ME" in env and "PATH" in env
    for gone in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT"):
        assert gone not in env
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-x"             # Lumi's own environment is untouched


def test_the_task_goes_on_stdin_never_on_the_command_line(fake):
    nasty = 'fix it & del /q *.* | "quoted" ; $(whoami) %PATH%'
    cc.run(nasty)
    assert fake.procs[0].stdin_seen == nasty
    assert all(nasty not in part and "del /q" not in part for part in fake.cmds[0])
    assert fake.cmds[0][0] == "C:/fake/claude.exe"                    # started directly, not via cmd.exe
    assert fake.kws[0].get("shell") in (None, False)
    assert fake.kws[0]["cwd"] == cc.WORKSPACE_ROOT
    assert "ANTHROPIC_API_KEY" not in fake.kws[0]["env"]


# ── results and errors ────────────────────────────────────────────

def test_a_good_answer_comes_back_with_the_step_count(fake):
    assert cc.run("explain main.py") == "Here is the summary.\n\n(Claude Code, 3 steps)"


def test_long_answers_are_cut_off(fake):
    fake.next = FakeProc(ok_json("x" * 20000))
    out = cc.run("t")
    assert len(out) < cc.MAX_OUTPUT_CHARS + 100 and "cut off" in out


@pytest.mark.parametrize("payload,fragment", [
    (json.dumps({"is_error": True, "subtype": "error_max_turns", "result": ""}), "ran out of steps"),
    (json.dumps({"is_error": True, "subtype": "success", "result": "Invalid API key · Please run /login"}), "isn't signed in"),
    (json.dumps({"is_error": False, "subtype": "success", "result": ""}), "said nothing"),
    ("not json at all", "usable answer"),
    ("", "usable answer"),
])
def test_failures_are_explained_not_raised_raw(fake, payload, fragment):
    fake.next = FakeProc(payload, err="boom")
    with pytest.raises(cc.ClaudeCodeError, match=fragment):
        cc.run("t")


def test_a_run_that_takes_too_long_is_killed(fake):
    fake.next = FakeProc(sleep=True)
    with pytest.raises(cc.ClaudeCodeError, match="took longer"):
        cc.run("t", timeout=1)
    assert getattr(fake.procs[0], "killed", False) is True


def test_bad_tasks_never_start_a_process(fake):
    with pytest.raises(cc.ClaudeCodeError):
        cc.run("   ")
    with pytest.raises(cc.ClaudeCodeError, match="too long"):
        cc.run("x" * (cc.MAX_TASK_CHARS + 1))
    assert fake.cmds == []


def test_unknown_modes_fall_back_to_read_only(fake):
    cc.run("t", mode="root-access")
    assert args_after(fake.cmds[0], "--tools") == ["Read,Grep,Glob"]


def test_usage_is_capped_per_hour(fake):
    for _ in range(cc.MAX_RUNS_PER_HOUR):
        cc.run("t")
    with pytest.raises(cc.ClaudeCodeError, match="this hour"):
        cc.run("t")


def test_only_one_run_at_a_time(fake):
    gate, started = threading.Event(), threading.Event()
    class Slow(FakeProc):
        def communicate(self, input=None, timeout=None):
            started.set(); gate.wait(5)
            return super().communicate(input, timeout)
    fake.next = Slow(ok_json())
    t = threading.Thread(target=lambda: cc.run("first"))
    t.start()
    assert started.wait(3)
    with pytest.raises(cc.ClaudeCodeError, match="(?i)one task at a time"):
        cc.run("second")
    gate.set(); t.join(5)
    assert len(fake.cmds) == 1


def test_not_installed_gives_instructions(monkeypatch):
    monkeypatch.setattr(cc, "find_cli", lambda: None)
    with pytest.raises(cc.ClaudeCodeError, match="npm install"):
        cc.run("t")


# ── approval and prompt wiring ────────────────────────────────────

models = []


def make_prompter(approve, monkeypatch, ran):
    models.clear()
    p = Prompter.__new__(Prompter)
    p.signals = types.SimpleNamespace(workspace_hub=None, sdk=None)
    p.agent_mode_enabled = True
    p.modules = []
    asked, agents = [], []

    async def confirm(name, arguments, timeout=120, agent="main"):
        asked.append((name, arguments))
        agents.append(agent)
        return approve
    p._confirm = confirm
    monkeypatch.setattr(cc, "available", lambda: True)
    monkeypatch.setattr(cc, "run", lambda task, mode, model=None: ran.append((task, mode)) or models.append(model) or "done")
    p._agents_asked = agents
    return p, asked


def dispatch(p, args):
    return asyncio.run(p._dispatch_tool_call(cc.TOOL_NAME, args))


def test_a_declined_task_never_runs(monkeypatch):
    ran = []
    p, asked = make_prompter(False, monkeypatch, ran)
    assert "declined" in dispatch(p, {"task": "refactor everything", "mode": "edit"})
    assert ran == [] and asked[0][0] == cc.TOOL_NAME


def test_every_call_is_approved_with_the_whole_task_visible_even_read_only(monkeypatch):
    ran = []
    p, asked = make_prompter(True, monkeypatch, ran)
    task = "Look at the persona editor and explain how saving works. " * 20
    assert dispatch(p, {"task": task}) == "done"
    assert ran == [(task.strip(), "read_only")]                      # read_only is the default
    name, preview = asked[0]
    assert preview["task"] == task.strip() and preview["mode"] == "read_only" and "read-only" in preview["action"]
    assert len(json.dumps(preview, indent=2)) < 4000                 # the prompt shows ~4000 characters: nothing is hidden


def test_edit_mode_is_announced_in_the_prompt(monkeypatch):
    ran = []
    p, asked = make_prompter(True, monkeypatch, ran)
    dispatch(p, {"task": "write fib.py", "mode": "edit"})
    assert "may edit files" in asked[0][1]["action"] and ran[0][1] == "edit"


def test_an_oversized_task_is_refused_before_asking_anyone(monkeypatch):
    ran = []
    p, asked = make_prompter(True, monkeypatch, ran)
    out = dispatch(p, {"task": "x" * (cc.MAX_TASK_CHARS + 1)})
    assert "too long" in out and asked == [] and ran == []


def test_unavailable_is_reported_politely(monkeypatch):
    ran = []
    p, asked = make_prompter(True, monkeypatch, ran)
    monkeypatch.setattr(cc, "available", lambda: False)
    assert "isn't available" in dispatch(p, {"task": "t"}) and asked == []


def test_its_answers_are_wrapped_as_data():
    assert agent_tools.wrap_tool_result(cc.TOOL_NAME, "hi").startswith("[FROM CLAUDE CODE]")


def test_the_model_is_told_about_the_marker_and_the_tool():
    import inspect
    from modules import prompter
    src = inspect.getsource(prompter)
    assert "[FROM CLAUDE CODE] ... [END FROM CLAUDE CODE]" in src and "ask_claude_code hands a real coding task" in src
    assert cc.TOOL_SCHEMA["function"]["name"] == cc.TOOL_NAME


def test_office_approval_and_the_routed_claude_model(monkeypatch):
    ran = []
    p, asked = make_prompter(True, monkeypatch, ran)
    p._route_tier = "opus"
    dispatch(p, {"task": "find the race condition in the reconnect loop"})
    assert p._agents_asked == ["office"]                             # the Office card waits, not Main
    assert models == ["opus"] and asked[0][1]["model"] == "opus"     # the tier picks Claude Code's model


def test_the_routed_model_becomes_a_model_flag():
    cmd = cc.build_command("claude.exe", "read_only", "opus")
    assert cmd[cmd.index("--model") + 1] == "opus"
    assert "--model" not in cc.build_command("claude.exe", "read_only", None)
    assert "--model" not in cc.build_command("claude.exe", "read_only", "gpt-9")      # only Claude's own aliases


def test_a_model_the_plan_lacks_falls_back_to_the_default(fake):
    calls = []
    real_execute = cc._execute
    def fake_execute(exe, task, mode, timeout, model=None):
        calls.append(model)
        if model:
            raise cc.ClaudeCodeError("Claude Code reported a problem: model opus is not available on your plan")
        return "ok"
    import pytest as _p
    mp = _p.MonkeyPatch()
    mp.setattr(cc, "_execute", fake_execute)
    try:
        out = cc.run("t", "read_only", "opus")
    finally:
        mp.undo()
    assert calls == ["opus", None] and out.startswith("ok") and "wasn't available" in out


def test_the_office_card_follows_the_run(monkeypatch):
    from modules import agent_tracker
    monkeypatch.setattr(agent_tracker, "tracker", agent_tracker.AgentTracker())
    seen = []
    agent_tracker.register(lambda snap: seen.append(dict(snap["office"])))
    ran = []
    p, _ = make_prompter(True, monkeypatch, ran)
    p._route_tier = "sonnet"
    dispatch(p, {"task": "explain the reconnect loop"})
    assert any(s["status"] == "executing" and "Claude Code" in s["task"] and s["model"] == "sonnet" for s in seen)
    assert agent_tracker.get("office")["status"] == "idle"

    def boom(task, mode, model=None):
        raise RuntimeError("claude crashed")
    monkeypatch.setattr(cc, "run", boom)
    with pytest.raises(RuntimeError):
        dispatch(p, {"task": "explain it again"})
    assert agent_tracker.get("office")["status"] == "error"          # stays on show until the next run


def test_claude_code_never_runs_while_agent_mode_is_off(monkeypatch):
    ran = []
    p, asked = make_prompter(True, monkeypatch, ran)
    p.agent_mode_enabled = False
    out = dispatch(p, {"task": "explain the reconnect loop"})
    assert "agent mode" in out and ran == [] and asked == []        # no approval prompt, no Claude call
    p.agent_mode_enabled = True
    assert dispatch(p, {"task": "explain the reconnect loop"}) == "done"


def test_the_tool_isnt_even_offered_while_agent_mode_is_off():
    import inspect
    from modules import prompter
    src = inspect.getsource(prompter.Prompter)
    assert "self.agent_mode_enabled and claude_code.available()" in src

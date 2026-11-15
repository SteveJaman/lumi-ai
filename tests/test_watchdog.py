"""Proactive Watchdog (modules/watchdog_agent.py): thresholds, crash vs.
quit, hangs, Lumi's spoken offer and the approval-gated restart."""

import asyncio
import types

import pytest

from modules import agent_tracker, watchdog_agent as wa


def make(windows, alive=lambda pid: False, wer=lambda pid: False, logged=lambda exe: False, hung=lambda h: False):
    sig = types.SimpleNamespace(history=[], new_message=False, terminate=False)
    state = {"wins": windows}
    wd = wa.Watchdog(sig, enabled=True, games=["Stellar Blade"], probes={
        "windows": lambda: state["wins"],
        "is_hung": hung, "wer": wer, "crash_logged": logged, "alive": alive,
        "proc_info": lambda pid: (r"C:\Games\SB\SB.exe", [r"C:\Games\SB\SB.exe", "-dx12"], r"C:\Games\SB"),
        "top": lambda kind: ("chrome.exe", 41.0),
    })
    return wd, sig, state


GAME = [(101, "Stellar Blade", 4242)]


def test_strike_counter_needs_three_in_a_row_and_rearms():
    c = wa.StrikeCounter(95, strikes=3, cooldown_s=0)
    assert [c.check(v, t) for t, v in enumerate([96, 97, 50, 96, 96, 96, 99, 99])] == \
        [False, False, False, False, False, True, False, False]
    c.check(80, 100)
    assert [c.check(99, 100 + i) for i in range(3)] == [False, False, True]


def test_strike_counter_cooldown():
    c = wa.StrikeCounter(90, strikes=1, cooldown_s=1000)
    assert c.check(95, 0)                  # fires
    assert not c.check(50, 1)              # dip re-arms...
    assert not c.check(95, 2)              # ...but the cooldown still holds
    assert c.check(95, 1001)               # cooldown over: fires again
    assert not c.check(95, 2500)           # still high: no repeat without a dip


def test_resource_event_text_matches_the_spec(monkeypatch):
    wd, sig, _ = make(GAME)
    import psutil
    monkeypatch.setattr(psutil, "cpu_percent", lambda interval=None: 10.0)
    monkeypatch.setattr(psutil, "virtual_memory", lambda: types.SimpleNamespace(percent=97.0))
    fired = [wd.check_resources(now=i * 60) for i in range(3)]
    assert fired == [[], [], ["RAM"]]
    assert sig.history[-1]["content"] == "[WATCHDOG EVENT] High RAM usage detected. chrome.exe is using 41%."
    assert sig.history[-1]["source"] == "watchdog_event" and sig.new_message
    assert agent_tracker.get("system")["visual_state"] == "alarm"
    agent_tracker.update("system", "idle")


def test_crash_with_evidence_is_reported_and_offers_a_restart():
    wd, sig, state = make(GAME, wer=lambda pid: pid == 4242)
    assert wd.check_games() == []                       # running: remembers exe + args
    state["wins"] = []
    assert wd.check_games() == ["crash:Stellar Blade"]
    ev = sig.history[-1]
    assert ev["content"] == "[WATCHDOG EVENT] Game crashed. Stellar Blade closed unexpectedly."
    assert ev["say"] == "[worried] Hey, your game just crashed. Want me to try and restart it?"
    assert ev["offer_restart"] is True
    assert wd.restart_preview()["arguments"] == ["-dx12"]
    assert agent_tracker.get("system")["status"] == "error"
    agent_tracker.update("system", "idle")


def test_quitting_normally_is_not_a_crash():
    wd, sig, state = make(GAME)
    wd.check_games()
    state["wins"] = []
    assert wd.check_games() == [] and sig.history == []


def test_event_log_entry_counts_as_a_crash():
    wd, sig, state = make(GAME, logged=lambda exe: exe == "SB.exe")
    wd.check_games()
    state["wins"] = []
    assert wd.check_games() == ["crash:Stellar Blade"]
    agent_tracker.update("system", "idle")


def test_hang_is_reported_once_after_two_checks():
    wd, sig, state = make(GAME, hung=lambda h: True, alive=lambda pid: True)
    assert wd.check_games() == []
    assert wd.check_games() == ["hung:Stellar Blade"]
    assert wd.check_games() == []                        # not repeated
    assert "stopped responding" in sig.history[-1]["say"]
    assert wd.restart_preview()["note"]                  # will close the frozen one first
    agent_tracker.update("system", "idle")


def test_window_gone_but_process_alive_is_not_a_crash():
    wd, sig, state = make(GAME, alive=lambda pid: True, wer=lambda pid: True)
    wd.check_games()
    state["wins"] = []
    assert wd.check_games() == []


@pytest.mark.parametrize("text,yes", [("yes", True), ("Yeah!", True), ("sure", True), ("please do", True),
                                      ("restart it", True), ("no", False), ("yes but later", False), ("", False)])
def test_affirmative(text, yes):
    assert wa.is_affirmative(text) is yes


# ─── through the prompter ─────────────────────────────────────────────

def make_prompter(sig, approve):
    from modules.prompter import Prompter
    p = Prompter.__new__(Prompter)
    p.signals = sig
    p.modules = []
    p.said, p.asked = [], []
    p._push_to_tts = p.said.append
    p._extract_and_apply_emotions = lambda text: None

    async def confirm(name, args, timeout=120, agent="main"):
        p.asked.append((name, agent, args))
        return approve
    p._confirm = confirm
    return p


@pytest.mark.parametrize("approve", [True, False])
def test_lumi_speaks_the_offer_and_yes_restarts_after_approval(monkeypatch, approve):
    wd, sig, state = make(GAME, wer=lambda pid: True)
    wd.check_games()
    state["wins"] = []
    wd.check_games()
    launched = []
    monkeypatch.setattr(wa.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(wa.subprocess, "Popen", lambda args, cwd=None, creationflags=0: launched.append((args, cwd)))
    p = make_prompter(sig, approve)

    assert asyncio.run(p._handle_watchdog_turn(sig.history[-1], sig.history[-1]["content"]))
    assert p.said == ["[worried] Hey, your game just crashed. Want me to try and restart it?"]

    sig.history.append({"role": "user", "content": "yes"})
    assert asyncio.run(p._handle_watchdog_turn(sig.history[-1], "yes"))
    assert p.asked[0][0] == "restart_crashed_game" and p.asked[0][1] == "system"
    if approve:
        assert launched == [([r"C:\Games\SB\SB.exe", "-dx12"], r"C:\Games\SB")]
        assert "Restarted Stellar Blade" in p.said[-1]
        assert agent_tracker.get("system")["status"] == "idle"
    else:
        assert launched == [] and "leave it closed" in p.said[-1]
    agent_tracker.update("system", "idle")


def test_yes_to_something_else_does_not_restart():
    wd, sig, state = make(GAME, wer=lambda pid: True)
    wd.check_games()
    state["wins"] = []
    wd.check_games()
    p = make_prompter(sig, True)
    asyncio.run(p._handle_watchdog_turn(sig.history[-1], ""))
    sig.history.append({"role": "assistant", "content": "Want to play a game?"})      # she moved on
    sig.history.append({"role": "user", "content": "yes"})
    assert asyncio.run(p._handle_watchdog_turn(sig.history[-1], "yes")) is False
    assert p.asked == []
    agent_tracker.update("system", "idle")

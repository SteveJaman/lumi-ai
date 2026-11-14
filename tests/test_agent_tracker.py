"""Agent tracker: validation, change detection, callbacks, settle."""

import threading

import pytest

from modules.agent_tracker import AGENTS, AgentTracker


@pytest.fixture
def t():
    tr = AgentTracker()
    tr.seen = []
    tr.register(tr.seen.append)
    return tr


def test_starts_with_three_idle_agents(t):
    snap = t.snapshot()
    assert tuple(snap) == AGENTS == ("main", "minecraft", "office")
    assert all(a["status"] == "idle" and a["progress"] == 0 and a["model"] is None for a in snap.values())


def test_update_calls_back_with_the_whole_snapshot(t):
    assert t.update("main", "thinking", task="Generating response", progress=10, model="haiku", model_name="dolphin3:8b")
    snap = t.seen[-1]
    assert snap["main"] == {**snap["main"], "status": "thinking", "task": "Generating response", "progress": 10,
                            "model": "haiku", "model_name": "dolphin3:8b"}
    assert set(snap) == set(AGENTS)


def test_nothing_changed_means_no_callback(t):
    t.update("office", "executing", task="x", progress=50)
    n = len(t.seen)
    assert t.update("office", "executing", task="x", progress=50) is False
    assert len(t.seen) == n


def test_unset_fields_keep_their_value_but_idle_clears_task_and_progress(t):
    t.update("main", "thinking", task="Planning", progress=20, model="opus")
    t.update("main", "executing")
    assert t.get("main")["task"] == "Planning" and t.get("main")["progress"] == 20
    t.update("main", "idle")
    m = t.get("main")
    assert m["task"] == "" and m["progress"] == 0 and m["model"] == "opus"      # last tier stays on the badge


@pytest.mark.parametrize("value, want", [(-5, 0), (150, 100), ("42", 42), ("x", 0), (33.9, 33)])
def test_progress_is_clamped(t, value, want):
    t.update("main", "executing", progress=value)
    assert t.get("main")["progress"] == want


def test_long_or_messy_tasks_are_tidied(t):
    t.update("main", "thinking", task="a\n\n  b " + "c" * 500)
    assert t.get("main")["task"].startswith("a b c") and len(t.get("main")["task"]) <= 140


@pytest.mark.parametrize("args", [("boss", "idle"), ("main", "sleeping"), ("main", "thinking", None, None, "gpt-4")])
def test_bad_values_are_refused(t, args):
    with pytest.raises(ValueError):
        t.update(*args)


def test_a_failing_callback_doesnt_break_updates_or_others(t):
    t.register(lambda s: 1 / 0)
    later = []
    t.register(later.append)
    assert t.update("minecraft", "executing", task="Mining")
    assert later and t.seen


def test_settle_goes_idle_but_keeps_errors_on_show(t):
    t.update("office", "executing", task="Claude Code")
    t.settle("office")
    assert t.get("office")["status"] == "idle"
    t.update("office", "error", task="boom")
    t.settle("office")
    assert t.get("office")["status"] == "error"


def test_unregister_stops_callbacks(t):
    got = []
    t.register(got.append)
    t.unregister(got.append)
    t.unregister(got.append)                                    # twice is fine
    t.update("main", "thinking")
    assert got == []


def test_snapshots_are_copies(t):
    snap = t.snapshot()
    snap["main"]["status"] = "error"
    assert t.get("main")["status"] == "idle"


def test_threads_can_update_at_once(t):
    def work(i):
        for k in range(50):
            t.update(AGENTS[i % 3], "executing", progress=k)
    threads = [threading.Thread(target=work, args=(i,)) for i in range(6)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert all(a["status"] == "executing" for a in t.snapshot().values())

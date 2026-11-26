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
    assert tuple(snap) == AGENTS == ("lead", "research", "build", "operate")
    assert all(a["status"] == "idle" and a["progress"] == 0 and a["model"] is None for a in snap.values())


def test_update_calls_back_with_the_whole_snapshot(t):
    assert t.update("lead", "thinking", task="Generating response", progress=10, model="haiku", model_name="dolphin3:8b")
    snap = t.seen[-1]
    assert snap["lead"] == {**snap["lead"], "status": "thinking", "task": "Generating response", "progress": 10,
                            "model": "haiku", "model_name": "dolphin3:8b"}
    assert set(snap) == set(AGENTS)


def test_nothing_changed_means_no_callback(t):
    t.update("build", "executing", task="x", progress=50)
    n = len(t.seen)
    assert t.update("build", "executing", task="x", progress=50) is False
    assert len(t.seen) == n


def test_unset_fields_keep_their_value_but_idle_clears_task_and_progress(t):
    t.update("lead", "thinking", task="Planning", progress=20, model="opus")
    t.update("lead", "executing")
    assert t.get("lead")["task"] == "Planning" and t.get("lead")["progress"] == 20
    t.update("lead", "idle")
    m = t.get("lead")
    assert m["task"] == "" and m["progress"] == 0 and m["model"] == "opus"      # last tier stays on the badge


@pytest.mark.parametrize("value, want", [(-5, 0), (150, 100), ("42", 42), ("x", 0), (33.9, 33)])
def test_progress_is_clamped(t, value, want):
    t.update("lead", "executing", progress=value)
    assert t.get("lead")["progress"] == want


def test_long_or_messy_tasks_are_tidied(t):
    t.update("lead", "thinking", task="a\n\n  b " + "c" * 500)
    assert t.get("lead")["task"].startswith("a b c") and len(t.get("lead")["task"]) <= 140


@pytest.mark.parametrize("args", [("boss", "idle"), ("lead", "sleeping"), ("lead", "thinking", None, None, "gpt-4")])
def test_bad_values_are_refused(t, args):
    with pytest.raises(ValueError):
        t.update(*args)


def test_a_failing_callback_doesnt_break_updates_or_others(t):
    t.register(lambda s: 1 / 0)
    later = []
    t.register(later.append)
    assert t.update("operate", "executing", task="Minecraft: mining")
    assert later and t.seen


def test_settle_goes_idle_but_keeps_errors_on_show(t):
    t.update("build", "executing", task="Claude Code")
    t.settle("build")
    assert t.get("build")["status"] == "idle"
    t.update("build", "error", task="boom")
    t.settle("build")
    assert t.get("build")["status"] == "error"


def test_unregister_stops_callbacks(t):
    got = []
    t.register(got.append)
    t.unregister(got.append)
    t.unregister(got.append)                                    # twice is fine
    t.update("lead", "thinking")
    assert got == []


def test_snapshots_are_copies(t):
    snap = t.snapshot()
    snap["lead"]["status"] = "error"
    assert t.get("lead")["status"] == "idle"


def test_threads_can_update_at_once(t):
    def work(i):
        for k in range(50):
            t.update(AGENTS[i % len(AGENTS)], "executing", progress=k)
    threads = [threading.Thread(target=work, args=(i,)) for i in range(2 * len(AGENTS))]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert all(a["status"] == "executing" for a in t.snapshot().values())


def test_agents_report_station_and_visual_state(t):
    snap = t.snapshot()
    assert {a: s["station"] for a, s in snap.items()} == {
        "lead": "desk", "research": "bookshelf", "build": "workbench", "operate": "server_rack"}
    assert snap["lead"]["visual_state"] == "sitting"
    t.update("lead", "thinking")
    t.update("research", "executing")
    t.update("operate", "executing")
    snap = t.snapshot()
    assert (snap["lead"]["visual_state"], snap["research"]["visual_state"],
            snap["operate"]["visual_state"]) == ("typing", "reading", "blinking")
    t.update("operate", "waiting_approval")
    assert t.get("operate")["visual_state"] == "waving"
    t.update("operate", "error", task="Game crashed")
    assert t.get("operate")["visual_state"] == "alarm"
    t.update("operate", "idle")
    assert t.get("operate")["visual_state"] == "standing"


def test_older_names_still_land_on_the_right_card(t):
    from modules.agent_tracker import LEGACY
    assert LEGACY == {"main": "lead", "office": "build", "minecraft": "operate", "system": "operate"}
    t.update("office", "executing", task="Claude Code: tests")
    assert t.get("build")["task"] == "Claude Code: tests" and t.get("office") == t.get("build")
    t.update("system", "executing", task="Triaging your inbox")
    assert t.get("operate")["status"] == "executing"
    assert set(t.snapshot()) == set(AGENTS)                     # no extra cards appear


@pytest.mark.parametrize("tool,agent", [
    ("web_search", "research"), ("browse_url", "research"), ("recall_memory", "research"),
    ("read_file", "research"), ("search_files", "research"),
    ("write_file", "build"), ("run_command", "build"), ("run_in_terminal", "build"),
    ("workflow_run", "build"), ("mcp__github__create_issue", "build"),
    ("dispatch_game_command", "operate"), ("vtube_move", "operate"), ("sdk__minecraft__mine", "operate"),
    ("task_add", "lead"), ("spawn_subagent", "lead"), ("something_new", "lead"),
])
def test_tools_light_up_whoever_does_that_work(tool, agent):
    from modules.agent_tracker import agent_for_tool
    assert agent_for_tool(tool) == agent

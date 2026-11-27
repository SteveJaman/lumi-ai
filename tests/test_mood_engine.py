import datetime
import json

import pytest

from modules import mood_engine as me


def _engine(tmp_path, t=1_000_000.0):
    clock = {"t": t}
    eng = me.MoodEngine(path=str(tmp_path / "mood_state.json"), clock=lambda: clock["t"])
    return eng, clock


def test_resting_values_on_first_run(tmp_path):
    eng, _ = _engine(tmp_path)
    assert eng.state["smugness"] == me.RESTING["smugness"]
    assert eng.state["interruption_streak"] == 0


def test_barge_in_raises_annoyance_and_the_streak(tmp_path):
    eng, clock = _engine(tmp_path)
    before = eng.state["annoyance"]
    eng.event("barge_in", clock["t"])
    eng.event("barge_in", clock["t"])
    assert eng.state["annoyance"] > before
    assert eng.state["interruption_streak"] == 2


def test_a_finished_turn_clears_the_streak(tmp_path):
    eng, clock = _engine(tmp_path)
    eng.event("barge_in", clock["t"])
    eng.event("turn_finished", clock["t"])
    assert eng.state["interruption_streak"] == 0


def test_values_stay_between_0_and_100(tmp_path):
    eng, clock = _engine(tmp_path)
    for _ in range(50):
        eng.event("task_done", clock["t"])
    assert 0 <= eng.state["smugness"] <= 100
    eng.state["energy"] = 0
    eng.event("late_night", clock["t"])
    assert eng.state["energy"] == 0.0


def test_unknown_events_are_refused(tmp_path):
    eng, clock = _engine(tmp_path)
    with pytest.raises(ValueError):
        eng.event("cosmic_ray", clock["t"])


def test_the_mood_drifts_back_toward_resting_over_time(tmp_path):
    eng, clock = _engine(tmp_path)
    eng.state["annoyance"] = 90.0
    eng.state["updated_at"] = clock["t"]
    clock["t"] += 4 * 3600                              # four hours later
    eng.drift(clock["t"])
    assert me.RESTING["annoyance"] < eng.state["annoyance"] < 90.0


def test_state_survives_a_restart(tmp_path):
    eng, clock = _engine(tmp_path)
    eng.event("barge_in", clock["t"])
    eng.save()
    again, _ = _engine(tmp_path, t=clock["t"])
    assert again.state["interruption_streak"] == 1
    assert json.load(open(eng.path, encoding="utf-8"))["interruption_streak"] == 1


def test_a_corrupt_file_falls_back_to_resting(tmp_path):
    p = tmp_path / "mood_state.json"
    p.write_text("{not json", encoding="utf-8")
    eng = me.MoodEngine(path=str(p))
    assert eng.state["smugness"] == me.RESTING["smugness"]


def test_late_night_only_between_1_and_5(tmp_path):
    eng, clock = _engine(tmp_path)
    at = lambda h: datetime.datetime(2026, 11, 26, h, 30).timestamp()
    assert eng.late_night(at(2)) is True
    assert eng.late_night(at(12)) is False
    assert eng.late_night(at(5)) is False

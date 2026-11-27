import pytest

from modules import curiosity, mood_engine


@pytest.fixture
def mood(tmp_path, monkeypatch):
    eng = mood_engine.MoodEngine(path=str(tmp_path / "mood.json"))
    monkeypatch.setattr(mood_engine, "_shared", eng)
    return eng


def test_resting_mood_adds_no_cue(mood):
    assert mood_engine.cue() == ""


def test_annoyance_or_a_long_interruption_streak_sharpens_her(mood):
    mood.state["annoyance"] = 70.0
    assert "annoyed" in mood_engine.cue()
    mood.state["annoyance"] = 10.0
    mood.state["interruption_streak"] = 3
    assert "annoyed" in mood_engine.cue()


def test_only_the_strongest_feelings_are_named(mood):
    mood.state.update(annoyance=10.0, smugness=90.0, energy=10.0, curiosity=90.0)
    cue = mood_engine.cue()
    assert "smug" in cue and "low on energy" in cue and "curious" in cue
    assert cue.count("\n") <= 4


def test_a_discovery_is_handed_over_once(tmp_path, monkeypatch):
    store = curiosity.CuriosityStore(str(tmp_path / "c.db"))
    store.add("chess", "openings matter early", "https://x.example", 1.0)
    monkeypatch.setattr(curiosity, "_shared_store", store)
    first = curiosity.pending_cue()
    assert "chess" in first and "openings matter early" in first
    assert curiosity.pending_cue() == ""

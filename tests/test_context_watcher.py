from modules import context_watcher as cw


def test_titles_are_cleaned_and_capped():
    assert cw.sanitise("a\x00b\nc") == "a b c"
    assert len(cw.sanitise("x" * 500)) == cw.MAX_TITLE


def test_gaming_wins_over_everything():
    assert cw.scene_for("Visual Studio Code", hour=14, gaming=True) == "Gaming Mode"


def test_late_night_is_chill():
    assert cw.scene_for("Visual Studio Code", hour=2) == "Late Night Chill"
    assert cw.scene_for("Visual Studio Code", hour=5) == "Focus Mode"


def test_work_windows_are_focus_mode_and_others_have_no_preset():
    assert cw.scene_for("main.py - Visual Studio Code", hour=14) == "Focus Mode"
    assert cw.scene_for("YouTube - Chrome", hour=14) is None


def test_off_windows_return_an_empty_title(monkeypatch):
    monkeypatch.setattr(cw.sys, "platform", "linux")
    assert cw.foreground_title() == ""

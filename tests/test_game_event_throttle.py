import types

from modules import gaming_mode, prompter as pr
from modules import proactive


class _Fake:
    def __init__(self, dnd=False):
        self.signals = types.SimpleNamespace(do_not_disturb=dnd)


def test_one_game_reaction_per_interval(monkeypatch):
    monkeypatch.setattr(gaming_mode, "is_enabled", lambda: False)
    f = _Fake()
    assert pr.Prompter._game_event_allowed(f, now=1000.0) is True
    assert pr.Prompter._game_event_allowed(f, now=1010.0) is False
    assert pr.Prompter._game_event_allowed(f, now=1000.0 + proactive.EVENT_INTERVAL_S) is True


def test_no_game_reactions_in_gaming_mode_or_do_not_disturb(monkeypatch):
    monkeypatch.setattr(gaming_mode, "is_enabled", lambda: True)
    assert pr.Prompter._game_event_allowed(_Fake(), now=5000.0) is False
    monkeypatch.setattr(gaming_mode, "is_enabled", lambda: False)
    assert pr.Prompter._game_event_allowed(_Fake(dnd=True), now=6000.0) is False

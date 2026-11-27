import types

from modules import prompter as pr
from modules import proactive


class _Fake:
    def __init__(self, dnd=False):
        self.signals = types.SimpleNamespace(do_not_disturb=dnd)


def test_one_game_reaction_per_interval():
    f = _Fake()
    assert pr.Prompter._game_event_allowed(f, now=1000.0) is True
    assert pr.Prompter._game_event_allowed(f, now=1010.0) is False
    assert pr.Prompter._game_event_allowed(f, now=1000.0 + proactive.EVENT_INTERVAL_S) is True


def test_game_reactions_continue_in_gaming_mode(monkeypatch):
    from modules import gaming_mode
    monkeypatch.setattr(gaming_mode, "is_enabled", lambda: True)
    assert pr.Prompter._game_event_allowed(_Fake(), now=5000.0) is True


def test_no_game_reactions_in_do_not_disturb():
    assert pr.Prompter._game_event_allowed(_Fake(dnd=True), now=6000.0) is False

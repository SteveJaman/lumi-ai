import types

from modules import heartbeat as hb
from modules import proactive


def _hb(monkeypatch, gaming=False, dnd=False, since_last=10_000.0):
    sig = types.SimpleNamespace(do_not_disturb=dnd, heartbeat_paused_until=0,
                                last_message_time=0.0, history=[], AI_thinking=False)
    h = hb.Heartbeat(sig, enabled=False)
    monkeypatch.setattr(hb.gaming_mode, "is_enabled", lambda: gaming)
    monkeypatch.setattr(h, "_is_busy", lambda: False)
    monkeypatch.setattr(h, "_is_user_active", lambda: False)
    monkeypatch.setattr(h, "_seconds_since_user_message", lambda: 10_000.0)
    monkeypatch.setattr(h, "_seconds_since_our_last_utterance", lambda: since_last)
    return h


def test_the_gap_between_unprompted_remarks_is_fifteen_minutes():
    assert hb.MIN_GAP_BETWEEN_UTTERANCES == proactive.IDLE_INTERVAL_S == 15 * 60


def test_no_unprompted_speech_in_gaming_mode(monkeypatch):
    ok, why = _hb(monkeypatch, gaming=True)._should_consider_acting()
    assert ok is False and "gaming" in why


def test_no_unprompted_speech_in_do_not_disturb(monkeypatch):
    ok, why = _hb(monkeypatch, dnd=True)._should_consider_acting()
    assert ok is False and "do not disturb" in why


def test_an_idle_quiet_moment_is_eligible(monkeypatch):
    ok, _ = _hb(monkeypatch)._should_consider_acting()
    assert ok is True


def test_a_recent_remark_is_still_in_cooldown(monkeypatch):
    ok, why = _hb(monkeypatch, since_last=60.0)._should_consider_acting()
    assert ok is False and "cooldown" in why

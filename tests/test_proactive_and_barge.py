from modules import barge_gate as bg
from modules import proactive as pa


def test_no_idle_remark_within_fifteen_minutes():
    g = pa.ProactiveGate()
    assert g.allowed(now=0.0) is True
    g.fired(now=0.0)
    assert g.allowed(now=pa.IDLE_INTERVAL_S - 1) is False
    assert g.allowed(now=pa.IDLE_INTERVAL_S) is True


def test_no_remarks_in_gaming_mode_or_do_not_disturb():
    g = pa.ProactiveGate()
    assert g.allowed(now=0.0, gaming=True) is False
    assert g.allowed(now=0.0, dnd=True) is False
    assert g.allowed(now=0.0) is True


def test_event_commentary_is_throttled_per_kind():
    t = pa.EventThrottle(interval_s=90)
    assert t.allowed("game_death", now=0.0) is True
    assert t.allowed("game_death", now=10.0) is False
    assert t.allowed("track_change", now=10.0) is True        # a different kind isn't held back
    assert t.allowed("game_death", now=95.0) is True


def test_event_commentary_is_suppressed_in_gaming_mode():
    t = pa.EventThrottle()
    assert t.allowed("game_death", now=0.0, gaming=True) is False


def test_her_own_voice_through_the_mic_is_not_a_barge_in():
    # She's talking at level 0.5; the mic hears about 0.3 of it.
    assert bg.is_barge_in(mic_level=0.3, playback_level=0.5, speaking=True) is False


def test_you_talking_over_her_is_a_barge_in():
    assert bg.is_barge_in(mic_level=0.8, playback_level=0.5, speaking=True) is True


def test_silence_and_background_hiss_are_never_a_barge_in():
    assert bg.is_barge_in(mic_level=0.01, playback_level=0.0, speaking=True) is False
    assert bg.is_barge_in(mic_level=0.01, playback_level=0.0, speaking=False) is False


def test_when_she_is_quiet_any_voice_counts():
    assert bg.is_barge_in(mic_level=0.1, playback_level=0.0, speaking=False) is True

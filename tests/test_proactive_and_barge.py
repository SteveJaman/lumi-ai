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

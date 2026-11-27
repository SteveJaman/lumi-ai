import math

import pytest

from modules import avatar_touch as at
from modules import vts_motion


def test_a_head_pat_is_embarrassed_and_she_may_speak():
    r = at.on_avatar_interact("head", "pat", 100.0, gate=at.TouchGate())
    assert r.emotion == "embarrassed" and r.flinch == {"head_tilt": 0.5} and r.say is True


def test_every_mapped_emotion_is_one_the_engine_knows():
    for rule in list(at.RULES.values()) + [at.DRAG]:
        if rule.emotion is not None:
            assert rule.emotion in vts_motion.EMOTION_VAD, rule.emotion


def test_a_drag_leans_her_and_never_makes_her_speak():
    gate = at.TouchGate()
    for i, zone in enumerate(at.ZONES):
        r = at.on_avatar_interact(zone, "drag", 10.0 + i * 5, gate=gate)
        assert r.flinch == {"lean": 0.3} and r.say is False and r.emotion is None


def test_unknown_zone_or_action_is_ignored():
    gate = at.TouchGate()
    assert at.on_avatar_interact("tail", "pat", 0.0, gate=gate) is None
    assert at.on_avatar_interact("head", "lick", 0.0, gate=gate) is None


def test_an_unmapped_pair_does_not_use_up_the_zone_cooldown():
    gate = at.TouchGate()
    assert at.on_avatar_interact("ear", "pat", 0.0, gate=gate) is None       # no rule for ear pats
    assert at.on_avatar_interact("ear", "poke", 0.1, gate=gate) is not None  # the zone is still free


def test_a_zone_cools_down_then_reacts_again():
    gate = at.TouchGate()
    assert at.on_avatar_interact("head", "pat", 0.0, gate=gate) is not None
    assert at.on_avatar_interact("head", "poke", 1.0, gate=gate) is None      # within 2 s
    assert at.on_avatar_interact("head", "poke", 2.5, gate=gate) is not None


def test_speech_is_rate_limited_across_zones():
    gate = at.TouchGate()
    first = at.on_avatar_interact("head", "pat", 0.0, gate=gate)
    second = at.on_avatar_interact("body", "poke", 5.0, gate=gate)
    assert first.say is True and second.say is False                          # still reacts, doesn't talk
    third = at.on_avatar_interact("body", "poke", at.SPEECH_COOLDOWN_S + 6, gate=gate)
    assert third.say is True


def test_the_flinch_decays_smoothly_to_nothing():
    imp = at.Impulse()
    imp.start_at(1.0, now=0.0)
    values = [imp.value(t / 10) for t in range(0, 30)]
    assert values[0] == pytest.approx(1.0)
    assert all(a >= b for a, b in zip(values, values[1:]))                    # never bounces back
    assert imp.value(3.0) < 0.001
    assert imp.value(0.35) == pytest.approx(math.exp(-1))                    # the time constant

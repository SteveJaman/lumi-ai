"""VTS motion engine (modules/vts_motion.py): no VTube Studio needed."""

import math
import random

import numpy as np
import pytest

from modules import vts_motion as vm


def frames(fn, t0, t1, dt=1 / 60):
    t = t0
    out = []
    while t <= t1 + 1e-9:
        out.append((t, fn(t)))
        t += dt
    return out


# ─── noise & head ─────────────────────────────────────────────────────

def test_perlin_is_bounded_smooth_and_seeded():
    p = vm.Perlin1D(7)
    xs = [i * 0.01 for i in range(5000)]
    vals = [p(x) for x in xs]
    assert all(-1.0 <= v <= 1.0 for v in vals)
    assert max(abs(a - b) for a, b in zip(vals, vals[1:])) < 0.05      # no jumps
    assert p(3.0) == pytest.approx(0.0) and p(3.37) == vm.Perlin1D(7)(3.37)


def test_head_sway_stays_in_range_and_never_repeats():
    h = vm.HeadSway(seed=1)
    samples = [h.sample(t * 0.05) for t in range(4000)]
    for axis, amp in enumerate(vm.HeadSway.AMPLITUDE):
        vals = [s[axis] for s in samples]
        assert max(abs(v) for v in vals) <= amp * 1.15 + 0.5
        assert np.std(vals) > amp * 0.1                                 # it actually moves
    assert h.sample(10.0) != h.sample(10.0 + 2 * math.pi / (0.05 * math.tau))


# ─── eyes ─────────────────────────────────────────────────────────────

def blink_durations(sched, seconds=600, dt=1 / 240, **kw):
    """Lengths of every closed-ish stretch (openness < 1)."""
    durs, start = [], None
    t = 0.0
    while t < seconds:
        v = sched.value(t, **kw)
        if v < 1.0 and start is None:
            start = t
        elif v >= 1.0 and start is not None:
            durs.append(t - start)
            start = None
        t += dt
    return durs


def test_blinks_include_micro_and_normal():
    durs = blink_durations(vm.BlinkScheduler(random.Random(3)))
    micro = [d for d in durs if d <= 0.06]
    normal = [d for d in durs if 0.2 <= d <= 0.26]
    assert micro and normal and len(micro) + len(normal) == len(durs)
    assert 60 < len(durs) < 400                                          # ~ every 2.5-6.5 s, plus doubles


def test_sleepy_blinks_are_slow():
    durs = blink_durations(vm.BlinkScheduler(random.Random(3)), sleepy=True)
    assert durs and min(durs) > 0.45


def test_gaze_modes():
    g = vm.GazeController(random.Random(1))
    ys = [g.value(t / 60, "thinking")[1] for t in range(60 * 20)]
    assert np.mean(ys[120:]) > 0.45                                      # thinking looks up
    cam = [g.value(20 + t / 60, "camera") for t in range(60 * 5)]
    assert all(abs(x) < 0.05 and abs(y) < 0.05 for x, y in cam[30:])    # locked on the viewer
    speak = [g.value(30 + t / 60, "speaking") for t in range(60 * 60)]
    off = [x for x, _ in speak if abs(x) > 0.3]
    assert 0 < len(off) < len(speak) * 0.4                              # mostly at camera, some glances


def test_reading_gaze_sweeps_left_to_right():
    g = vm.GazeController(random.Random(2))
    xs = [g.value(t / 120, "reading")[0] for t in range(120 * 3)]
    rises = sum(1 for a, b in zip(xs, xs[1:]) if b > a + 1e-3)
    falls = sum(1 for a, b in zip(xs, xs[1:]) if b < a - 1e-3)
    assert rises > falls * 2 and max(xs) > 0.35 and min(xs) < -0.35


# ─── breath ───────────────────────────────────────────────────────────

def test_breath_exhales_while_talking_and_inhales_in_pauses():
    b = vm.Breath()
    for _ in range(120):
        b.update(1 / 60, speaking=False)
    start = b.value
    for _ in range(60 * 4):
        b.update(1 / 60, speaking=True, intensity=0.8)
    assert b.value < start - 0.3 or b.value <= 0.06                    # long sentence: breathing out
    low = b.value
    for _ in range(40):                                                 # a pause mid-speech
        b.update(1 / 60, speaking=True, intensity=0.0)
    assert b.value > low + 0.4


def test_sleepy_breathing_is_slower():
    def cycles(sleepy):
        b, crossings, prev = vm.Breath(), 0, None
        for _ in range(60 * 60):
            v = b.update(1 / 60, speaking=False, sleepy=sleepy) - 0.5
            if prev is not None and prev < 0 <= v:
                crossings += 1
            prev = v
        return crossings
    assert cycles(True) < cycles(False) * 0.7


# ─── emotion ──────────────────────────────────────────────────────────

def test_emotion_ramps_holds_for_the_sentence_then_decays():
    e = vm.EmotionState()
    e.trigger("sad", {"smile": -0.4, "bly": -0.4}, now=0.0)
    assert e.update(0.0, False)["smile"] == pytest.approx(0.0)
    assert e.update(0.15, False)["smile"] == pytest.approx(-0.4 * 0.75)  # ease-out halfway
    assert e.update(0.3, True)["smile"] == pytest.approx(-0.4)
    assert e.update(9.0, True)["smile"] == pytest.approx(-0.4)           # still talking: held
    e.update(9.5, False)                                                  # sentence over
    assert e.update(12.0, False)["smile"] == pytest.approx(-0.2, abs=0.02)
    assert e.update(14.6, False)["smile"] == 0.0 and e.tag is None
    assert e.vad == (0.0, 0.0, 0.0)


def test_new_emotion_blends_from_the_current_face():
    e = vm.EmotionState()
    e.trigger("happy", {"smile": 0.7}, 0.0)
    e.update(1.0, True)
    e.trigger("sad", {"smile": -0.4}, 1.0)
    mid = e.update(1.1, True)["smile"]
    assert -0.4 < mid < 0.7                                              # no snap
    assert e.vad[0] > -0.7


def test_emotion_without_speech_releases_after_a_wait():
    e = vm.EmotionState()
    e.trigger("happy", {"smile": 0.7}, 0.0)
    e.update(5.0, False)
    assert e.output["smile"] == pytest.approx(0.7)
    e.update(6.0, False)
    assert e.update(8.5, False)["smile"] < 0.7


# ─── lip-sync ─────────────────────────────────────────────────────────

# Textbook female formants, independent of the module's tuning table.
TEXTBOOK = {"A": (850, 1500), "E": (560, 2150), "I": (340, 2600), "O": (600, 1000), "U": (380, 900)}
# U and O differ mostly in F1, which high voices blur; both are round lips.
ACCEPT = {"A": {"A"}, "E": {"E"}, "I": {"I"}, "O": {"O"}, "U": {"U", "O"}}


def vowel_audio(vowel, rate=24000, seconds=0.5, f0=210.0):
    """Harmonics of f0 shaped by two formant resonances."""
    f1, f2 = TEXTBOOK[vowel]
    t = np.arange(int(rate * seconds)) / rate
    out = np.zeros_like(t)
    for h in range(1, int(4000 / f0)):
        f = h * f0
        gain = math.exp(-((f - f1) / 140.0) ** 2) + 0.8 * math.exp(-((f - f2) / 200.0) ** 2) + 0.02
        out += gain * np.sin(2 * math.pi * f * t)
    return (out / np.abs(out).max() * 12000).astype(np.float32)


@pytest.mark.parametrize("rate", [16000, 24000, 44100, 48000])
@pytest.mark.parametrize("f0", [170, 210, 260])
@pytest.mark.parametrize("vowel", list(TEXTBOOK))
def test_vowels_are_recognized_from_audio(vowel, f0, rate):
    f1, f2 = vm.estimate_formants(vowel_audio(vowel, rate, f0=f0)[:1024], rate)
    got = vm.classify_vowel(f1, f2)
    assert got in ACCEPT[vowel]
    assert (vm.VOWEL_FORM[got] < -0.3) == (vowel in "OU")     # round lips never confused with open/wide


def test_visemes_map_vowels_to_mouth_shapes():
    rate = 24000
    audio = np.concatenate([vowel_audio("E", rate), np.zeros(rate // 4, np.float32), vowel_audio("O", rate)])
    vis = vm.analyze_visemes(audio, rate)
    assert len(vis) == math.ceil(len(audio) / 1024)
    e_forms = [f for o, f in vis[2:10]]
    o_forms = [f for o, f in vis[-8:-2]]
    assert min(e_forms) > 0.5 and max(o_forms) < -0.5                   # wide smile vs round lips
    assert all(0.0 <= o <= 1.0 for o, _ in vis)
    gap = vis[len(vowel_audio("E", rate)) // 1024 + 2]
    assert gap[0] < 0.05                                                  # silence: closed


def test_mouth_settles_to_neutral_in_200ms():
    m = vm.MouthDriver()
    m.set_target(0.9, 0.7)
    for _ in range(30):
        m.update(1 / 60, speaking=True)
    assert m.open > 0.85 and m.form > 0.6
    vals = []
    for _ in range(15):                                                   # 250 ms
        vals.append(m.update(1 / 60, speaking=False))
    assert vals[2][0] > 0.6                                               # not snapped shut
    assert all(a[0] >= b[0] for a, b in zip(vals, vals[1:]))             # monotonic
    assert vals[11] == (0.0, 0.0)                                         # done by 200 ms


# ─── agent embodiment ─────────────────────────────────────────────────

def snap(**agents):
    base = {a: {"status": "idle", "task": "", "updated": 0.0} for a in ("lead", "research", "build", "operate")}
    for a, v in agents.items():
        base[a].update(v)
    return base


@pytest.mark.parametrize("agents,pose,reading", [
    ({}, "idle", False),
    ({"lead": {"status": "thinking"}}, "thinking", False),
    ({"research": {"status": "executing", "task": "Running web_search"}}, "focused", True),
    ({"operate": {"status": "executing", "task": "Triaging your inbox"}}, "focused", True),
    ({"operate": {"status": "executing", "task": "Rescheduling an event"}}, "focused", False),
    ({"lead": {"status": "thinking"}, "operate": {"status": "waiting_approval"}}, "approval", False),
    ({"build": {"status": "error", "updated": 99.0}}, "error", False),
    ({"build": {"status": "error", "updated": 50.0}}, "idle", False),         # old error: don't stay shocked
    ({"operate": {"status": "executing", "task": "Minecraft: mining"}}, "idle", False),  # her other half
])
def test_pose_from_tracker_snapshot(agents, pose, reading):
    assert vm.pose_from_snapshot(snap(**agents), now=100.0) == (pose, reading)


def test_pose_changes_ease_in():
    p = vm.PoseBlender()
    p.update(1 / 60, "focused")
    assert 0 > p.brows[0] > -0.25 and p.eye_open < 1.0
    for _ in range(60):
        p.update(1 / 60, "focused")
    assert p.brows[0] == pytest.approx(-0.25, abs=0.01) and p.eye_open == pytest.approx(0.72, abs=0.01)


# ─── idle context ─────────────────────────────────────────────────────

def test_yawns_only_late_at_night_after_five_idle_minutes():
    y = vm.YawnScheduler(random.Random(4))
    assert all(y.update(t, hour=14, idle_s=9999)[0] == 0.0 for t in range(0, 600))
    assert all(y.update(t, hour=1, idle_s=100)[0] == 0.0 for t in range(0, 600))
    starts, peak = 0, 0.0
    t = 0.0
    while t < 1200:
        amt, started = y.update(t, hour=1, idle_s=400)
        starts += started
        peak = max(peak, amt)
        t += 0.05
    assert y.sleepy and peak == 1.0 and 2 <= starts <= 7


def test_frame_limiter_caps_at_60fps():
    lim = vm.FrameLimiter(60)
    sent = sum(lim.ready(i / 1000) for i in range(1000))                  # polled at 1 kHz for 1 s
    assert 58 <= sent <= 61


def test_allowlist_filter():
    assert vm.filter_allowed({"FaceAngleX": 1, "Bogus": 2}, {"FaceAngleX"}) == {"FaceAngleX": 1}
    assert vm.filter_allowed({"Bogus": 2}, None) == {"Bogus": 2}

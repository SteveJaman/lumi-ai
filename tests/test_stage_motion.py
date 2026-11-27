import math
import types

from modules import stage_motion as sm
from modules import vts_motion as vm


def _signals(speaking=False):
    return types.SimpleNamespace(AI_speaking=speaking, last_message_time=0.0, history=[])


def test_frames_use_only_cubism_parameter_names_and_finite_values():
    s = sm.StageMotion(_signals())
    frame = s.frame(now=100.0)
    known = set(sm.CUBISM_HEAD.values()) | set(sm.CUBISM_EXTRA.values()) | {
        sm.CUBISM_MOUTH, sm.CUBISM_SMILE, sm.CUBISM_BREATH}
    assert frame and set(frame) <= known
    assert all(math.isfinite(v) for v in frame.values())
    assert "ParamAngleX" in frame and "ParamEyeLOpen" in frame


def test_the_stage_never_touches_vts_state():
    sig = _signals()
    sm.StageMotion(sig).frame(now=1.0)
    assert not hasattr(sig, "vts_state")                 # VtubeStudio's constructor would have set it


def test_the_face_moves_between_frames():
    s = sm.StageMotion(_signals())
    a = s.frame(now=10.0)
    b = s.frame(now=12.5)
    assert any(abs(a[k] - b[k]) > 1e-6 for k in a if k in b)


def test_speaking_is_read_from_the_signals():
    s = sm.StageMotion(_signals(speaking=True))
    s.frame(now=1.0)
    assert s._is_idle is False


def test_a_touch_applies_its_emotion_to_the_motion_engine():
    s = sm.StageMotion(_signals())
    r = s.touch("head", "poke", now=50.0)
    assert r is not None and r.emotion == "surprised"
    s._emotion.update(50.0 + 1.0, speaking=False)          # the ramp in
    assert s._emotion.intensity > 0.0


def test_touch_rules_and_cooldown_come_from_avatar_touch():
    s = sm.StageMotion(_signals())
    assert s.touch("tail", "poke", now=1.0) is None
    assert s.touch("head", "poke", now=1.0) is not None
    assert s.touch("head", "poke", now=1.5) is None          # zone cooldown


def test_the_frame_is_built_from_the_real_compute_frame():
    assert sm.StageMotion.compute_frame is sm.VtubeStudio.compute_frame
    assert vm  # the engine module is the same one VTS uses

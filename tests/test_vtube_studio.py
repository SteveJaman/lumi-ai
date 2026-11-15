"""modules/vtubeStudio.py against a fake VTS socket: session gating,
allowlist, rate limit, reconnect, agent poses and emotion hotkeys."""

import asyncio
import queue
import time
import types

import pytest

import modules.vtubeStudio as vs
from modules import vts_motion as vm


INPUTS = ["FaceAngleX", "FaceAngleY", "FaceAngleZ", "EyeOpenLeft", "EyeOpenRight",
          "EyeRightX", "EyeRightY", "MouthOpen", "MouthSmile", "Brows"]


class FakeVTS:
    """Just enough of pyvts.vts for the module."""

    def __init__(self, inputs=INPUTS, expressions=(), hotkeys=()):
        self.inputs = list(inputs)
        self.custom = []
        self.expressions = list(expressions)
        self.hotkeys = list(hotkeys)
        self.sent = []
        self.connected = 0
        self.fail_next = False
        self.vts_request = types.SimpleNamespace(
            BaseRequest=lambda t, data=None, request_id="x": {"messageType": t, "data": data or {}},
            requestHotKeyList=lambda: {"messageType": "HotkeysInCurrentModelRequest", "data": {}},
        )

    async def connect(self): self.connected = 1
    async def close(self): self.connected = 0
    async def request_authenticate_token(self): pass
    async def request_authenticate(self): pass
    def get_connection_status(self): return self.connected

    async def request(self, req):
        if self.fail_next or not self.connected:
            self.fail_next = False
            self.connected = 0
            raise ConnectionError("socket closed")
        self.sent.append(req)
        t, d = req["messageType"], req["data"]
        if t == "InputParameterListRequest":
            return {"data": {"modelName": "ariu", "defaultParameters": [{"name": n} for n in self.inputs],
                             "customParameters": [{"name": n} for n in self.custom]}}
        if t == "ParameterCreationRequest":
            self.custom.append(d["parameterName"])
            return {"data": {}}
        if t == "InjectParameterDataRequest":
            bad = [p["id"] for p in d["parameterValues"] if p["id"] not in self.inputs + self.custom]
            if bad:
                return {"messageType": "APIError", "data": {"message": f"unknown {bad}"}}
            return {"data": {}}
        if t == "ExpressionStateRequest":
            return {"data": {"expressions": [{"file": f} for f in self.expressions]}}
        if t == "HotkeysInCurrentModelRequest":
            return {"data": {"availableHotkeys": [{"name": n, "hotkeyID": f"id-{n}", "type": "ToggleExpression"}
                                                  for n in self.hotkeys]}}
        if t == "ItemListRequest":
            return {"data": {"availableItemFiles": []}}
        return {"data": {}}

    def of(self, t):
        return [r["data"] for r in self.sent if r["messageType"] == t]


def make(**fake_kw):
    signals = types.SimpleNamespace(terminate=False, AI_speaking=False, sio_queue=queue.SimpleQueue(),
                                    last_message_time=0.0)
    v = vs.VtubeStudio(signals, enabled=True)
    v.vts = FakeVTS(**fake_kw)
    return v


def run(coro):
    return asyncio.run(coro)


def test_establish_learns_the_model_and_creates_the_breath_input():
    v = make()
    assert run(v._establish()) and v.session == "ready" and v.is_ready()
    assert "LumiBreath" in v._allowed and v._breath_name == "LumiBreath"
    assert v._head_names == {"x": "FaceAngleX", "y": "FaceAngleY", "z": "FaceAngleZ",
                             "el": "EyeOpenLeft", "er": "EyeOpenRight"}
    assert v._mouth_name == "MouthOpen" and v._smile_name == "MouthSmile"
    # Probing never tried names outside the allowlist (no API errors).
    probed = {p["id"] for d in v.vts.of("InjectParameterDataRequest") for p in d["parameterValues"]}
    assert probed <= set(v._allowed)


def test_frames_only_carry_allowlisted_params_and_go_out_in_one_batch():
    v = make()
    run(v._establish())
    v.vts.sent.clear()
    frame = v.compute_frame(time.time(), speaking=False)
    frame["ParamAngleX"] = 3.0                                   # a Live2D name: not injectable
    assert run(v._send_frame(frame, time.time() + 1))
    injects = v.vts.of("InjectParameterDataRequest")
    assert len(injects) == 1                                     # one request per frame
    ids = {p["id"] for p in injects[0]["parameterValues"]}
    assert "ParamAngleX" not in ids and {"FaceAngleX", "MouthOpen", "Brows", "LumiBreath"} <= ids


def test_nothing_is_sent_unless_the_session_is_ready():
    v = make()
    run(v._establish())
    v.session = "reconnecting"
    v.vts.sent.clear()
    assert not run(v._send_frame({"FaceAngleX": 1.0}, time.time()))
    assert v.vts.sent == []


def test_parameter_frames_are_capped_at_60_per_second():
    v = make()
    run(v._establish())
    v.vts.sent.clear()
    t0 = time.time() + 10
    sent = sum(run(v._send_frame({"FaceAngleX": 1.0}, t0 + i / 500)) for i in range(500))   # 1 s at 500 Hz
    assert 50 <= sent <= 60          # never over the cap; polling granularity can only lower it


def test_api_injections_join_the_next_frame():
    v = make()
    run(v._establish())
    run(v.inject_parameters([{"id": "MouthSmile", "value": 0.9}]))
    assert v.vts.of("InjectParameterDataRequest")[-1]["parameterValues"][0]["value"] == 0.0  # not sent on its own
    assert v.compute_frame(time.time(), False)["MouthSmile"] == 0.9
    assert "MouthSmile" not in v._overrides or v.compute_frame(time.time() + 2, False)["MouthSmile"] != 0.9


def test_a_dropped_socket_starts_a_reconnect(monkeypatch):
    v = make()
    run(v._establish())
    v.vts.fail_next = True
    assert run(v.send_request({"messageType": "X", "data": {}})) == {}
    assert v.session == "reconnecting" and v.signals.vts_ready is False
    monkeypatch.setattr(vs, "RECONNECT_BACKOFF_S", (0,))
    run(v._reconnect())
    assert v.session == "ready" and v.signals.vts_state == "ready"


def tracker(**agents):
    snap = {a: {"status": "idle", "task": "", "updated": time.time()} for a in ("main", "minecraft", "office", "system")}
    for a, s in agents.items():
        snap[a].update(s)
    return snap


def test_agent_states_drive_the_face():
    v = make()
    run(v._establish())
    now = time.time()
    v._agent_snapshot = tracker(main={"status": "thinking"})
    for i in range(60):
        f = v.compute_frame(now + i / 60, speaking=False)
    assert v._frame_pose == "thinking" and v._gaze._mode == "thinking"
    v._agent_snapshot = tracker(system={"status": "waiting_approval"})
    for i in range(60):
        f = v.compute_frame(now + 1 + i / 60, speaking=False)
    assert v._frame_pose == "approval" and v._gaze._mode == "camera"
    assert abs(f["EyeRightX"]) < 0.1                              # looking straight at you
    v._agent_snapshot = tracker(main={"status": "executing", "task": "Running web_search"})
    v.compute_frame(now + 2, speaking=False)
    assert v._gaze._mode == "reading"


def test_pose_expressions_switch_on_and_off_only_what_lumi_set():
    v = make(expressions=["Pondering.exp3.json", "Hat.exp3.json"])
    run(v._establish())
    assert v._pose_targets == {"thinking": ("expression", "Pondering.exp3.json")}
    t = time.time()
    run(v._update_pose_expression("thinking", t))
    run(v._update_pose_expression("thinking", t + 0.5))         # held past the debounce
    run(v._update_pose_expression("idle", t + 1))
    run(v._update_pose_expression("idle", t + 1.5))
    acts = v.vts.of("ExpressionActivationRequest")
    assert acts == [{"expressionFile": "Pondering.exp3.json", "active": True},
                    {"expressionFile": "Pondering.exp3.json", "active": False}]


def test_pose_flicker_does_not_spam_expressions():
    v = make(expressions=["pondering.exp3.json"])
    run(v._establish())
    t = time.time()
    for i in range(20):                                          # thinking/idle every 100 ms
        run(v._update_pose_expression("thinking" if i % 2 else "idle", t + i * 0.1))
    assert v.vts.of("ExpressionActivationRequest") == []


def test_emotion_hotkey_toggles_back_off_after_decay(monkeypatch):
    v = make()
    run(v._establish())
    fired = []

    async def trig(name):
        fired.append(name)
    monkeypatch.setattr(v, "trigger_expression", trig)
    run(v.set_emotion("confused"))
    assert fired == ["spiral_eyes"] and v._emotion.tag == "confused"
    v._emotion.update(time.time() + 7, False)                     # release (no speech came)
    v._emotion.update(time.time() + 13, False)                    # fully decayed
    run(v._sync_emotion_hotkey())
    assert fired == ["spiral_eyes", "spiral_eyes"] and v._emotion_hotkey_on is None


def test_emotion_ramps_into_the_frame():
    v = make()
    run(v._establish())
    now = time.time()
    run(v.set_emotion("happy"))
    t0 = v._emotion._t0
    early = v.compute_frame(t0 + 0.05, speaking=False)["MouthSmile"]
    late = v.compute_frame(t0 + 0.5, speaking=False)["MouthSmile"]
    assert early < late and late == pytest.approx(0.7, abs=0.05)


def test_lip_sync_drives_open_and_form_then_settles():
    v = make()
    run(v._establish())
    now = time.time()
    v.API.set_mouth_shape(0.9, -0.8)                             # an "O"
    for i in range(20):
        f = v.compute_frame(now + i / 60, speaking=True)
    assert f["MouthOpen"] > 0.8 and f["MouthSmile"] < -0.4
    for i in range(1, 14):
        f = v.compute_frame(now + 20 / 60 + i / 60, speaking=False)
    assert f["MouthOpen"] == 0.0


ARIU_EXPRESSIONS = ["exp_dizzy_eyes.exp3.json", "exp_heart_eyes.exp3.json", "exp_shadow_face.exp3.json",
                    "exp_hat.exp3.json"]


def test_error_pose_uses_the_models_dizzy_eyes_expression():
    v = make(expressions=ARIU_EXPRESSIONS, hotkeys=["spiral_eyes", "hat"])
    run(v._establish())
    assert v._pose_targets == {"error": ("expression", "exp_dizzy_eyes.exp3.json")}


def test_emotion_looks_switch_by_expression_not_keystrokes(monkeypatch):
    v = make(expressions=ARIU_EXPRESSIONS)
    run(v._establish())
    monkeypatch.setattr(v, "_trigger_via_keyboard", lambda name: pytest.fail("used the keyboard"))
    run(v.set_emotion("grateful"))                               # heart_eyes
    run(v.set_emotion("angry"))                                  # darkened replaces it
    acts = v.vts.of("ExpressionActivationRequest")
    assert acts == [{"expressionFile": "exp_heart_eyes.exp3.json", "active": True},
                    {"expressionFile": "exp_heart_eyes.exp3.json", "active": False},
                    {"expressionFile": "exp_shadow_face.exp3.json", "active": True}]


def test_shared_expression_stays_on_until_both_owners_let_go():
    v = make(expressions=ARIU_EXPRESSIONS)
    run(v._establish())
    t = time.time()
    run(v.set_emotion("confused"))                               # dizzy eyes, owner: emotion
    run(v._update_pose_expression("error", t))
    run(v._update_pose_expression("error", t + 0.5))             # dizzy eyes, owner: pose too
    run(v._update_pose_expression("idle", t + 1))
    run(v._update_pose_expression("idle", t + 1.5))              # pose lets go: still confused
    dizzy = [a for a in v.vts.of("ExpressionActivationRequest") if "dizzy" in a["expressionFile"]]
    assert dizzy == [{"expressionFile": "exp_dizzy_eyes.exp3.json", "active": True}]
    v._emotion.update(time.time() + 7, False)
    v._emotion.update(time.time() + 13, False)
    run(v._sync_emotion_hotkey())                                # emotion decays: now off
    dizzy = [a for a in v.vts.of("ExpressionActivationRequest") if "dizzy" in a["expressionFile"]]
    assert dizzy[-1] == {"expressionFile": "exp_dizzy_eyes.exp3.json", "active": False}


def test_reconnect_reasserts_a_wanted_emotion_look(monkeypatch):
    v = make(expressions=ARIU_EXPRESSIONS)
    run(v._establish())
    run(v.set_emotion("grateful"))
    v.vts.sent.clear()
    v.vts.fail_next = True
    run(v.send_request({"messageType": "X", "data": {}}))
    monkeypatch.setattr(vs, "RECONNECT_BACKOFF_S", (0,))
    run(v._reconnect())
    assert {"expressionFile": "exp_heart_eyes.exp3.json", "active": True} in v.vts.of("ExpressionActivationRequest")

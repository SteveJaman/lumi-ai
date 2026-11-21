"""
VTube Studio controller: Lumi's embodied digital twin.

Drives the Live2D model via the VTS plugin API using pyvts. What the face
does each frame is decided by modules/vts_motion.py (pure, unit-tested);
this module owns the connection, discovers what the model supports, and
sends the result.

Responsibilities:
  - Connect + authenticate; reconnect with backoff if VTS drops
  - Discover the model's input parameters (allowlist) and probe names
  - Embody the agent pipeline (modules/agent_tracker.py): pondering while
    Main thinks, looking straight at you while an approval waits, focused
    while a tool runs, shocked on an error, eyes darting while she reads
  - Natural lip-sync: loudness drives MouthOpen, vowel formants (FFT/LPC)
    drive MouthForm; the mouth settles to neutral over 200 ms
  - Organic motion: Perlin + sine head sway, mode-aware gaze, micro- and
    double-blinks, speech-synced breathing
  - Continuous emotion: tags ramp in over 300 ms, hold for the sentence,
    decay over 5 s (valence / arousal / dominance shape the motion too)
  - Contextual idle: after midnight and idle 5 min she yawns and breathes
    slower
  - State-aware expression API ("put on hat" / "take off jacket")

CRITICAL — VTS Input names vs Live2D param names
------------------------------------------------
InjectParameterDataRequest only reaches the model through VTS *Input*
parameters (default inputs like FaceAngleX, MouthOpen, Brows, plus custom
ones created by plugins), which the model maps onto its Live2D params in
its ParameterSettings (ariu.vtube.json). Injecting a Live2D name such as
ParamAngleX either errors or is silently dropped.

So:
  * At startup the module asks VTS for its input list
    (InputParameterListRequest) and never sends anything outside it (Task 7
    allowlist). If the list can't be fetched, nothing is filtered (the old
    behaviour).
  * Candidate lists still put VTS Input names FIRST.
  * Breathing goes to a custom input, "LumiBreath", which the module creates
    on connect. To make her breathe, map LumiBreath -> ParamBreath once in
    VTS (Model settings -> add a parameter row). Without the mapping the
    value is simply unused.

The VTS Inputs on ariu (from ariu.vtube.json):
    FaceAngleX/Y/Z    -> head angles + body angles (shared)
    EyeOpenLeft/Right -> ParamEyeLOpen / ParamEyeROpen
    EyeRightX/Y       -> ParamEyeBallX / Y (X inverted in the model mapping)
    MouthOpen         -> ParamMouthOpenY
    MouthSmile        -> ParamMouthForm  (emotion smile + vowel shape)
    Brows             -> all four brow params (shared)

The body slots resolve to FaceAngleX/Y/Z, the SAME inputs as the head; at
startup they're dropped so two values don't fight over one input. The body
still moves through VTS's own FaceAngle -> BodyAngle transform.

Session state, rate limit (Task 7)
----------------------------------
self.session is "disconnected" -> "connecting" -> "ready" (or
"reconnecting"). Parameters are only ever sent while "ready", so nothing
stale goes out mid-reconnect. Every frame is ONE batched injection (one
request per mode), capped at MAX_PARAM_FPS (60). Ad-hoc injections from the
API are merged into the next frame instead of being sent on their own.

Agent poses (Task 1)
--------------------
The tracker callback stores the latest snapshot; each frame maps it to a
pose (vts_motion.pose_from_snapshot). A pose is always shown through
parameters (brows, lids, head, gaze), so it works on any model. If the
model ALSO has a matching expression (POSE_EXPRESSION_CANDIDATES, e.g.
"pondering.exp3.json") it's switched on with ExpressionActivationRequest,
or a matching hotkey is fired over the API. Only expressions/hotkeys Lumi
turned on for a pose are turned off again on idle, never outfit toggles.
The keyboard hotkey path is never used for poses: it steals window focus.

Hotkey firing paths (emotion and outfit hotkeys)
------------------------------------------------
1. API path — HotkeyTriggerRequest. Broken on ariu for ToggleExpression
   hotkeys (VTS accepts, does nothing). Kept for models where it works.
2. Keyboard path — simulate the bound key combo after focusing VTS.
   Works reliably. USE_KEYBOARD_HOTKEYS_ONLY=True always uses it.

Expression state tracking
-------------------------
Every outfit hotkey on ariu is a ToggleExpression. EXPRESSION_STATE_MAP
maps user-facing state names (jacket, ponytail_l) to (hotkey, sense) so
"put on your jacket" works even though the hotkey is named remove_jacket.
"""

import asyncio
import datetime
import math
import os
import queue
import time
import random
import pyvts
import logging
import ctypes

from modules.module import Module
from modules import agent_tracker
from modules import vts_motion as vm
from constants import VTUBE_MODEL_POSITIONS, VTUBE_MIC_POSITION, DATA_ROOT
from modules.log_hub import emit as hub_emit


logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("websockets.legacy").setLevel(logging.WARNING)
logging.getLogger("websockets.legacy.protocol").setLevel(logging.WARNING)


_VTS_TOKEN_PATH = os.path.join(DATA_ROOT, "vtubeStudio_token.txt")

VTS_CLOSED_MSG = "VTube Studio is closed. Avatar features disabled."


def vts_refused(e):
    """True when connecting failed because nothing listens on the VTS port:
    VTube Studio closed or its API switched off (WinError 1225 / 10061,
    ECONNREFUSED), however the websocket library wraps it."""
    seen = set()
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if isinstance(e, ConnectionRefusedError):
            return True
        if isinstance(e, OSError) and (getattr(e, "winerror", None) in (1225, 10061)
                                       or getattr(e, "errno", None) in (111, 10061)):
            return True
        if "refused" in str(e).lower():
            return True
        e = e.__cause__ or e.__context__
    return False


# ===============================================================
# HOTKEY ALIASES
# ===============================================================
HOTKEY_ALIASES = {}


# ===============================================================
# KEYBOARD FALLBACK HOTKEYS
# ===============================================================
HOTKEY_KEYBINDINGS = {
    "jk_bag":           "ctrl+1",
    "spiral_eyes":      "ctrl+2",
    "hat":              "ctrl+3",
    "controller":       "ctrl+4",
    "heart_eyes":       "ctrl+5",
    "remove_jacket":    "ctrl+6",
    "skirt":            "ctrl+7",
    "hide_ponytail_l":  "ctrl+8",
    "hide_ponytail_r":  "ctrl+9",
    "darkened":         "ctrl+q",
}

USE_KEYBOARD_HOTKEYS_ONLY = True
VTS_WINDOW_TITLE = "VTube Studio"


# ===============================================================
# EXPRESSION STATE MAP
# ===============================================================
EXPRESSION_STATE_MAP = {
    "hat":             ("hat",                +1),
    "jacket":          ("remove_jacket",      -1),
    "bag":             ("jk_bag",             +1),
    "skirt":           ("skirt",              +1),
    "ponytail_l":      ("hide_ponytail_l",    -1),
    "ponytail_r":      ("hide_ponytail_r",    -1),
    "controller":      ("controller",         +1),
    "heart_eyes":      ("heart_eyes",         +1),
    "spiral_eyes":     ("spiral_eyes",        +1),
    "darkened":        ("darkened",           +1),
}


# ===============================================================
# WINDOW FOCUS HELPER
# ===============================================================
def _focus_window_by_title(title_substring):
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        target_hwnd = [None]
        EnumWindowsProc = ctypes.WINFUNCTYPE(
            ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p
        )

        def _cb(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if title_substring.lower() in buf.value.lower():
                target_hwnd[0] = hwnd
                return False
            return True

        user32.EnumWindows(EnumWindowsProc(_cb), 0)
        hwnd = target_hwnd[0]
        if not hwnd:
            return False

        SW_RESTORE = 9
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)

        fg_hwnd = user32.GetForegroundWindow()
        fg_thread = user32.GetWindowThreadProcessId(fg_hwnd, None)
        this_thread = kernel32.GetCurrentThreadId()

        user32.AttachThreadInput(fg_thread, this_thread, True)
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
        user32.AttachThreadInput(fg_thread, this_thread, False)

        import time as _t
        _t.sleep(0.05)
        return user32.GetForegroundWindow() == hwnd
    except Exception:
        return False


# ===============================================================
# EMOTION → HOTKEY MAPPING (passive path)
# ===============================================================
EMOTION_HOTKEY_MAP = {
    "confused":     "spiral_eyes",
    "uncertain":    "spiral_eyes",
    "doubtful":     "spiral_eyes",
    "surprised":    "spiral_eyes",
    "moved":        "heart_eyes",
    "empathetic":   "heart_eyes",
    "grateful":     "heart_eyes",
    "hopeful":      "heart_eyes",
    "optimistic":   "heart_eyes",
    "angry":        "darkened",
    "frustrated":   "darkened",
    "disdainful":   "darkened",
    "contemptuous": "darkened",
}


# ===============================================================
# EMOTION → STICKY PARAMETER EXPRESSIONS
# ===============================================================
PARAMETER_EXPRESSION_MAP = {
    "happy":        [("smile",  0.7)],
    "delighted":    [("smile",  0.9)],
    "excited":      [("smile",  0.8)],
    "amused":       [("smile",  0.5)],
    "laughing":     [("smile",  0.8)],
    "chuckling":    [("smile",  0.5)],
    "satisfied":    [("smile",  0.4)],
    "proud":        [("smile",  0.6)],
    "confident":    [("smile",  0.4)],
    "relaxed":      [("smile",  0.3)],
    "soft tone":    [("smile",  0.3)],
    "embarrassed":  [("smile",  0.2)],
    "sad":          [("smile", -0.4)],
    "unhappy":      [("smile", -0.3)],
    "disappointed": [("smile", -0.4)],
    "upset":        [("smile", -0.5)],
    "depressed":    [("smile", -0.5)],
    "regretful":    [("smile", -0.3)],
    "guilty":       [("smile", -0.2)],
    "indifferent":  [("smile", -0.1)],
    "bored":        [("smile", -0.2)],
    "sarcastic":    [("smile", -0.6)],
    "whispering":   [("smile", -0.3)],
}


# ===============================================================
# EMOTION → EYEBROW BASELINE
# ===============================================================
BROW_EXPRESSION_MAP = {
    "happy":        ( 0.15,  0.15,  0.10,  0.10),
    "delighted":    ( 0.35,  0.35,  0.20,  0.20),
    "excited":      ( 0.45,  0.45,  0.10,  0.10),
    "amused":       ( 0.20,  0.15,  0.15,  0.05),
    "laughing":     ( 0.40,  0.40,  0.15,  0.15),
    "chuckling":    ( 0.25,  0.25,  0.10,  0.10),
    "satisfied":    ( 0.10,  0.10,  0.05,  0.05),
    "proud":        ( 0.10,  0.10,  0.15,  0.15),
    "confident":    ( 0.05,  0.05,  0.10,  0.10),
    "relaxed":      ( 0.00,  0.00,  0.00,  0.00),
    "soft tone":    ( 0.05,  0.05,  0.05,  0.05),
    "embarrassed":  ( 0.30,  0.25,  0.20,  0.15),
    "hopeful":      ( 0.25,  0.25,  0.15,  0.15),
    "optimistic":   ( 0.25,  0.25,  0.15,  0.15),
    "grateful":     ( 0.15,  0.15,  0.10,  0.10),
    "surprised":    ( 0.75,  0.75,  0.25,  0.25),
    "confused":     (-0.15,  0.35, -0.20,  0.10),
    "uncertain":    (-0.05,  0.30, -0.10,  0.05),
    "doubtful":     (-0.20,  0.30, -0.25,  0.05),
    "sad":          (-0.40, -0.40,  0.35,  0.35),
    "unhappy":      (-0.35, -0.35,  0.25,  0.25),
    "disappointed": (-0.30, -0.30,  0.20,  0.20),
    "upset":        (-0.50, -0.50,  0.40,  0.40),
    "depressed":    (-0.45, -0.45,  0.30,  0.30),
    "regretful":    (-0.30, -0.30,  0.20,  0.20),
    "guilty":       (-0.25, -0.25,  0.15,  0.15),
    "angry":        (-0.55, -0.55, -0.65, -0.65),
    "frustrated":   (-0.40, -0.40, -0.55, -0.55),
    "disdainful":   ( 0.10, -0.10, -0.20, -0.05),
    "contemptuous": ( 0.05, -0.15, -0.15, -0.05),
    "sarcastic":    ( 0.10, -0.05, -0.10,  0.00),
    "indifferent":  ( 0.00,  0.00,  0.00,  0.00),
    "bored":        (-0.15, -0.15, -0.10, -0.10),
    "empathetic":   (-0.10, -0.10,  0.20,  0.20),
    "moved":        (-0.05, -0.05,  0.15,  0.15),
    "concerned":    (-0.20, -0.20,  0.25,  0.25),
    "whispering":   (-0.05, -0.05,  0.05,  0.05),
}


# ===============================================================
# PARAMETER NAME CANDIDATES
# ---------------------------------------------------------------
# ORDER IS LOAD-BEARING. VTS Input names FIRST, Live2D param names
# as fallback for models that expose injectable params directly.
# See module docstring for the full explanation.
# ===============================================================
HEAD_NAME_CANDIDATES = {
    "x":   ["FaceAngleX",   "ParamAngleX",   "AngleX"],
    "y":   ["FaceAngleY",   "ParamAngleY",   "AngleY"],
    "z":   ["FaceAngleZ",   "ParamAngleZ",   "AngleZ"],
    "el":  ["EyeOpenLeft",  "ParamEyeLOpen"],
    "er":  ["EyeOpenRight", "ParamEyeROpen"],
}

EXTRA_NAME_CANDIDATES = {
    # Gaze — EyeRightX/Y are VTS Inputs. Note that the model's own
    # mapping inverts EyeRightX → ParamEyeBallX, but we just send
    # -1..1 and let the model handle the direction.
    "ex":  ["EyeRightX", "EyeLeftX", "ParamEyeBallX"],
    "ey":  ["EyeRightY", "EyeLeftY", "ParamEyeBallY"],

    # Body — same VTS Inputs as the head. The startup "body shares
    # head's input" check drops these slots so we don't stomp head
    # motion every frame.
    "bx":  ["FaceAngleX", "ParamBodyAngleX", "BodyAngleX"],
    "by":  ["FaceAngleY", "ParamBodyAngleY", "BodyAngleY"],
    "bz":  ["FaceAngleZ", "ParamBodyAngleZ", "BodyAngleZ"],
    "bw":  ["ParamBodyAngleZ2"],

    # Brows — all four route through the single "Brows" VTS Input.
    # _compute_brows dedupes them into one write per frame.
    "bly": ["Brows", "ParamBrowLY"],
    "bry": ["Brows", "ParamBrowRY"],
    "blf": ["Brows", "ParamBrowLForm"],
    "brf": ["Brows", "ParamBrowRForm"],

    # Arms — pure physics outputs on ariu. No VTS Input exists for
    # them, so verification will reject these on that model. Kept in
    # the candidate list for other models that might expose them.
    "la1": ["Param"],    "la2": ["Param3"],
    "la3": ["Param2"],   "la4": ["Param4"],
    "ra1": ["Param5"],   "ra2": ["Param7"],
    "ra3": ["Param6"],   "ra4": ["Param8"],
}

MOUTH_NAME_CANDIDATES = ["MouthOpen", "ParamMouthOpenY"]
SMILE_NAME_CANDIDATES = ["MouthSmile", "ParamMouthForm", "MouthForm"]


# ===============================================================
# VTS INPUT FALLBACKS
# ---------------------------------------------------------------
# Historically this let the controller retry with a VTS Input name
# after a direct Live2D param was rejected. With the candidate
# reorder above, the VTS Input names are now tried FIRST, so this
# path only fires on models that expose some params as direct
# injectables and others only through shared inputs.
#
# Kept as a defensive layer — harmless if unused.
# ===============================================================
VTS_INPUT_FALLBACK = {
    "bx":  "FaceAngleX",
    "by":  "FaceAngleY",
    "bz":  "FaceAngleZ",
    "bly": "Brows",
    "bry": "Brows",
    "blf": "Brows",
    "brf": "Brows",
}

# Range lookup for VTS Input names whose expected range differs from
# the Live2D param they drive. _clamp checks this first, then falls
# back to PARAM_RANGES.
VTS_INPUT_RANGES = {
    "FaceAngleX": (-30.0, 30.0),
    "FaceAngleY": (-30.0, 30.0),
    "FaceAngleZ": (-30.0, 30.0),
    "Brows":      (0.0, 1.0),
}


# ===============================================================
# TUNING
# ===============================================================
EXPRESSION_DURATION = 6.0


PARAM_RANGES = {
    # Mouth — both VTS Input names and Live2D params
    "MouthOpen":       (0.0, 1.0),
    "ParamMouthOpenY": (0.0, 2.1),
    "MouthSmile":      (-1.0, 1.0),
    "MouthForm":       (-1.0, 1.0),
    "ParamMouthForm":  (-1.0, 1.0),

    # Head — VTS Input names
    "FaceAngleX":      (-30.0, 30.0),
    "FaceAngleY":      (-30.0, 30.0),
    "FaceAngleZ":      (-30.0, 30.0),
    # Head — Live2D param fallbacks
    "ParamAngleX":     (-30.0, 30.0),
    "ParamAngleY":     (-30.0, 30.0),
    "ParamAngleZ":     (-30.0, 30.0),

    # Eyes — VTS Input names
    "EyeOpenLeft":     (0.0, 1.0),
    "EyeOpenRight":    (0.0, 1.0),
    "EyeRightX":       (-1.0, 1.0),
    "EyeRightY":       (-1.0, 1.0),
    "EyeLeftX":        (-1.0, 1.0),
    "EyeLeftY":        (-1.0, 1.0),
    # Eyes — Live2D param fallbacks
    "ParamEyeLOpen":   (0.0, 1.0),
    "ParamEyeROpen":   (0.0, 1.0),
    "EyeOpenLeft":     (0.0, 1.0),
    "EyeOpenRight":    (0.0, 1.0),
    "EyeBallX":        (-1.0, 1.0),
    "EyeBallY":        (-1.0, 1.0),
    "ParamEyeBallX":   (-1.0, 1.0),
    "ParamEyeBallY":   (-1.0, 1.0),

    # Body — fallback names only (VTS Input names already listed above)
    "ParamBodyAngleX":  (-10.0, 10.0),
    "ParamBodyAngleY":  (-10.0, 10.0),
    "ParamBodyAngleZ":  (-10.0, 10.0),
    "ParamBodyAngleZ2": (-10.0, 10.0),
    "BodyAngleX":       (-10.0, 10.0),
    "BodyAngleY":       (-10.0, 10.0),
    "BodyAngleZ":       (-10.0, 10.0),

    # Brows — fallback names
    "ParamBrowLY":      (-1.0, 1.0),
    "ParamBrowRY":      (-1.0, 1.0),
    "ParamBrowLForm":   (-1.0, 1.0),
    "ParamBrowRForm":   (-1.0, 1.0),

    # Arms
    "Param":  (-3.0, 3.0), "Param2": (-3.0, 3.0),
    "Param3": (-3.0, 3.0), "Param4": (-3.0, 3.0),
    "Param5": (-3.0, 3.0), "Param6": (-3.0, 3.0),
    "Param7": (-3.0, 3.0), "Param8": (-3.0, 3.0),

    # Breath
    "Breath":          (0.0, 1.0),
    "ParamBreath":     (0.0, 1.0),
}

# ===============================================================
# BREATH (custom VTS input; see module docstring)
# ===============================================================
BREATH_INPUT = "LumiBreath"
BREATH_NAME_CANDIDATES = [BREATH_INPUT, "Breath", "ParamBreath"]
CUSTOM_INPUTS = {
    BREATH_INPUT: {"explanation": "Lumi's breathing (0 = out, 1 = in). Map it to ParamBreath.",
                   "min": 0.0, "max": 1.0, "defaultValue": 0.5},
}
PARAM_RANGES[BREATH_INPUT] = (0.0, 1.0)


# ===============================================================
# AGENT POSES -> optional model expressions / hotkeys
# ---------------------------------------------------------------
# Matched case-insensitively against expression file names (without
# .exp3.json) first, then hotkey names. Missing ones are fine: the pose
# is always shown through parameters as well.
# ===============================================================
POSE_EXPRESSION_CANDIDATES = {
    "thinking": ["pondering", "thinking", "think", "hand_on_chin"],
    "approval": ["look_at_camera", "impatient", "attention", "waiting"],
    "focused":  ["focused", "focus", "serious", "concentrate"],
    "error":    ["shocked", "dizzy", "dizzy_eyes", "spiral_eyes", "panic"],
    "sleepy":   ["yawn", "yawning", "sleepy"],
}

# Emotion hotkeys -> the expression file they toggle. When the model has
# the file, it's switched on/off explicitly (ExpressionActivationRequest)
# instead of toggled by simulated keystrokes: deterministic, no focus
# stealing. Names match with or without an "exp_" prefix.
EMOTION_EXPRESSION_CANDIDATES = {
    "spiral_eyes": ["spiral_eyes", "dizzy_eyes", "dizzy"],
    "heart_eyes":  ["heart_eyes", "love_eyes"],
    "darkened":    ["darkened", "shadow_face", "shadow"],
}
POSE_DEBOUNCE_S = 0.4      # a pose must hold this long before expressions switch

# Face channels for the yawn emotion (brows up a little, lids heavy).
YAWN_CHANNELS = {"bly": 0.25, "bry": 0.25, "blf": 0.1, "brf": 0.1, "eye_open": -0.4}


# ===============================================================
# TUNING
# ===============================================================
MOTION_HZ = 60              # frame computation rate
MAX_PARAM_FPS = 60          # hard cap on injection frames sent to VTS
OVERRIDE_HOLD_S = 1.0       # API-injected values stay in the frame this long
RECONNECT_BACKOFF_S = (2, 4, 8, 15, 30)


# ===============================================================
# ARM CHOREOGRAPHER
# ===============================================================
class _ArmChoreographer:
    def __init__(self):
        rnd = random.uniform
        tau = math.tau
        self._l_phase = rnd(0, tau)
        self._r_phase = rnd(0, tau) + 0.4
        self._speech_env = 0.0
        self._lean = 0.0
        self._lean_current = 0.0
        self._wobble_phase = rnd(0, tau)

    def set_emotion_lean(self, value):
        self._lean = max(-1.0, min(1.0, float(value)))

    def apply_emotion(self, tag):
        self._lean = self._emotion_lean_for_tag(tag)

    @staticmethod
    def _emotion_lean_for_tag(tag):
        pos = {
            "happy": 0.35, "delighted": 0.55, "excited": 0.75,
            "amused": 0.30, "laughing": 0.80, "chuckling": 0.50,
            "satisfied": 0.15, "proud": 0.30, "confident": 0.15,
            "hopeful": 0.30, "optimistic": 0.30, "grateful": 0.20,
            "surprised": 0.40, "moved": 0.15, "empathetic": 0.10,
        }
        neg = {
            "sad": -0.50, "unhappy": -0.40, "disappointed": -0.45,
            "upset": -0.40, "depressed": -0.60, "regretful": -0.35,
            "guilty": -0.30, "bored": -0.30, "embarrassed": -0.30,
            "shy": -0.40, "indifferent": -0.15,
            "angry": -0.20, "frustrated": -0.25,
        }
        if tag in pos:
            return pos[tag]
        if tag in neg:
            return neg[tag]
        return 0.0

    def deltas(self, t, speaking, mouth_level, head_x, head_z):
        self._lean_current += (self._lean - self._lean_current) * 0.05
        l_sway = math.sin(t * 0.13 * math.tau + self._l_phase)
        r_sway = math.sin(t * 0.13 * math.tau + self._r_phase)
        if speaking:
            target = mouth_level * 0.8
            if target > self._speech_env:
                self._speech_env += (target - self._speech_env) * 0.55
            else:
                self._speech_env += (target - self._speech_env) * 0.20
        else:
            self._speech_env *= 0.85
        wobble = math.sin(t * 3.7 * math.tau + self._wobble_phase) * self._speech_env
        l_weight = 1.0 - max(0.0, head_z / 20.0) * 0.3
        r_weight = 1.0 - max(0.0, -head_z / 20.0) * 0.3
        sway_scale = [1.0, 0.7, 0.5, 0.35]
        lean_scale = [0.5, 0.6, 0.8, 1.0]
        base_l = self._lean_current * 0.9
        base_r = self._lean_current * 0.9
        d_l, d_r = [], []
        for i in range(4):
            s_l = (l_sway * 0.12 * l_weight * sway_scale[i]
                   + base_l * lean_scale[i]
                   + wobble * 0.06 * sway_scale[i])
            s_r = (r_sway * 0.12 * r_weight * sway_scale[i]
                   + base_r * lean_scale[i]
                   + wobble * 0.06 * sway_scale[i])
            s_l += max(0.0, -head_x / 30.0) * 0.06
            s_r += max(0.0,  head_x / 30.0) * 0.06
            d_l.append(s_l)
            d_r.append(s_r)
        return d_l, d_r


# ===============================================================
# MODULE
# ===============================================================
class VtubeStudio(Module):
    def __init__(self, signals, enabled=True):
        super().__init__(signals, enabled)
        self.queue = queue.SimpleQueue()
        self.API = self.API(self)

        self.item_list = []
        self.prop_instance_ids = {}
        self._hotkey_cache = {}
        self._hotkey_types = {}
        self._toggle_state = {}

        # Motion engine (modules/vts_motion.py).
        rng = random.Random()
        self._sway = vm.HeadSway(seed=rng.randrange(1 << 30))
        self._blink = vm.BlinkScheduler(rng)
        self._gaze = vm.GazeController(rng)
        self._breath = vm.Breath()
        self._emotion = vm.EmotionState()
        self._mouth = vm.MouthDriver()
        self._pose = vm.PoseBlender()
        self._yawn = vm.YawnScheduler(rng)
        self._limiter = vm.FrameLimiter(MAX_PARAM_FPS)
        self._arms = _ArmChoreographer()

        self._session_start = time.time()
        self._last_motion_tick = 0.0
        self._last_frame_t = time.time()
        self._body = [0.0, 0.0, 0.0]
        self._brow_current = [0.0, 0.0, 0.0, 0.0]
        self._overrides = {}                 # param id -> (value, expires_at)

        self._head_names = None
        self._extra_names = None
        self._mouth_name = None
        self._smile_name = None
        self._breath_name = None

        # Per-param mode lookup, populated by verification at startup.
        self._param_modes = {}
        # VTS input parameters the loaded model accepts (None = unknown).
        self._allowed = None

        # Session state (Task 7): only "ready" sends parameters.
        self.session = "disconnected"
        self._reconnect_attempt = 0

        # Agent embodiment (Task 1).
        self._agent_snapshot = {}
        self._tracking = None
        self._pose_name = "idle"
        self._pose_candidate = ("idle", 0.0)
        self._pose_targets = {}              # pose -> ("expression", file) | ("hotkey", id)
        self._pose_active = None             # (pose, kind, ref) Lumi switched on
        self._expressions = {}               # lower stem -> file
        self._expr_owners = {}               # file -> {"pose", "emotion"} that want it on

        # Emotion hotkeys (e.g. spiral_eyes) toggled on for the current emotion.
        self._emotion_hotkey_on = None

        plugin_info = {
            "plugin_name": "Lumi VTS Plugin",
            "developer": "Lumi AI",
            "authentication_token_path": _VTS_TOKEN_PATH,
        }
        self.vts = pyvts.vts(plugin_info=plugin_info)

        self._last_interaction_time = time.time()
        self._is_idle = False
        self._idle_start_time = 0.0

        if not enabled:
            self.signals.vts_state = "skipped"

    # ---------------------------------------------------------------
    # Connection / session
    # ---------------------------------------------------------------
    def is_ready(self):
        if self.session != "ready":
            return False
        try:
            return self.vts.get_connection_status() == 1
        except Exception:
            return False

    def _mark_lost(self, why):
        if self.session in ("ready", "connecting"):
            hub_emit(self.signals, "vts", f"connection lost: {why}", level="warn")
            print(f"\033[93mVTube Studio: connection lost ({why}); will reconnect\033[0m")
        self.session = "reconnecting"
        self.signals.vts_state = "loading"
        self.signals.vts_detail = "reconnecting"
        self.signals.vts_ready = False

    async def connect_and_auth(self):
        self.session = "connecting"
        try:
            await self.vts.connect()
        except Exception as e:
            self.session = "disconnected"
            self.signals.vts_state = "error"
            if vts_refused(e):
                # VTS is closed or its API is off: say so once, no Windows error code.
                self.signals.vts_detail = "VTube Studio is closed"
                if not getattr(self, "_closed_logged", False):
                    self._closed_logged = True
                    print(f"\033[93m{VTS_CLOSED_MSG}\033[0m")
                    hub_emit(self.signals, "vts", VTS_CLOSED_MSG, level="warn")
                return False
            print(f"\033[91mVTube Studio: connection failed — {e}\033[0m")
            hub_emit(self.signals, "vts", f"connection failed: {e}", level="error")
            self.signals.vts_detail = str(e)[:60]
            return False
        self._closed_logged = False
        try:
            await self.vts.request_authenticate_token()
            await self.vts.request_authenticate()
        except Exception as e:
            print(f"\033[91mVTube Studio: auth failed — {e}\033[0m")
            hub_emit(self.signals, "vts", f"auth failed: {e}", level="error")
            try:
                await self.vts.close()
            except Exception:
                pass
            self.session = "disconnected"
            return False
        hub_emit(self.signals, "vts", "connected and authenticated")
        return True

    async def _establish(self):
        """Connect, authenticate and learn the model. Ends in "ready"."""
        if not await self.connect_and_auth():
            return False
        try:
            self.item_list = await self.get_item_list()
        except Exception:
            self.item_list = []
        await self._ensure_custom_inputs()
        await self._fetch_allowlist()
        await self.load_hotkey_cache()
        await self.detect_parameter_names()
        await self._load_expressions()
        self._resolve_pose_targets()
        # After a reconnect (VTS may have restarted): the pose is re-applied
        # from scratch, and an emotion look that's still wanted is re-sent.
        self._pose_active = None
        self._pose_name, self._pose_candidate = "idle", ("idle", 0.0)
        for owners in self._expr_owners.values():
            owners.discard("pose")
        self.session = "ready"
        for f, owners in self._expr_owners.items():
            if owners:
                await self.send_request(self.vts.vts_request.BaseRequest(
                    "ExpressionActivationRequest", {"expressionFile": f, "active": True}))
        self._reconnect_attempt = 0
        self.signals.vts_state = "ready"
        self.signals.vts_ready = True
        return True

    async def _reconnect(self):
        delay = RECONNECT_BACKOFF_S[min(self._reconnect_attempt, len(RECONNECT_BACKOFF_S) - 1)]
        self._reconnect_attempt += 1
        try:
            await self.vts.close()
        except Exception:
            pass
        await asyncio.sleep(delay)
        if self.signals.terminate:
            return
        self.session = "connecting"
        if await self._establish():
            print("\033[92mVTube Studio: reconnected\033[0m")
            hub_emit(self.signals, "vts", "reconnected")
        else:
            self.session = "reconnecting"
            self.signals.vts_state = "loading"
            self.signals.vts_detail = f"reconnecting (retry in {RECONNECT_BACKOFF_S[min(self._reconnect_attempt, len(RECONNECT_BACKOFF_S) - 1)]}s)"

    async def load_hotkey_cache(self):
        try:
            hotkeys = await self.get_hotkeys()
            self._hotkey_cache = {
                hk["name"].lower(): hk["hotkeyID"]
                for hk in hotkeys
                if hk.get("name") and hk.get("hotkeyID")
            }
            self._hotkey_types = {
                hk["name"].lower(): hk.get("type", "")
                for hk in hotkeys if hk.get("name")
            }
            print(f"\033[90mVTube Studio: cached {len(self._hotkey_cache)} hotkeys\033[0m")
            names = sorted(self._hotkey_cache.keys())
            hub_emit(self.signals, "vts", f"cached {len(names)} hotkeys: {names}")
            self.signals.vts_detail = f"{len(names)} hotkeys"
            unmapped = []
            for tag, hotkey in EMOTION_HOTKEY_MAP.items():
                resolved = self._resolve_hotkey(hotkey)
                if resolved.lower() not in self._hotkey_cache:
                    unmapped.append(f"{tag}→{hotkey}")
            if unmapped:
                hub_emit(self.signals, "vts", f"missing hotkeys for: {unmapped}", level="warn")
        except Exception as e:
            print(f"\033[90mVTube Studio: hotkey cache failed — {e}\033[0m")
            hub_emit(self.signals, "vts", f"hotkey cache failed: {e}", level="warn")

    # ---------------------------------------------------------------
    # Allowlist, custom inputs, expressions
    # ---------------------------------------------------------------
    async def _fetch_allowlist(self):
        """Every VTS input parameter (default + custom). Injection is only
        possible into these, so anything else is filtered out of frames."""
        try:
            resp = await self.vts.request(
                self.vts.vts_request.BaseRequest("InputParameterListRequest"))
            data = (resp or {}).get("data") or {}
            names = {p.get("name") for p in
                     (data.get("defaultParameters") or []) + (data.get("customParameters") or [])
                     if p.get("name")}
            self._allowed = names or None
            hub_emit(self.signals, "vts",
                     f"model '{data.get('modelName', '?')}': {len(names)} input parameters allowed")
        except Exception as e:
            self._allowed = None
            hub_emit(self.signals, "vts", f"input parameter list failed ({e}); not filtering", level="warn")

    async def _ensure_custom_inputs(self):
        for name, spec in CUSTOM_INPUTS.items():
            try:
                resp = await self.vts.request(self.vts.vts_request.BaseRequest(
                    "ParameterCreationRequest", {"parameterName": name, **spec}))
                if (resp or {}).get("messageType") == "APIError":
                    hub_emit(self.signals, "vts",
                             f"couldn't create input {name}: {resp.get('data', {}).get('message')}",
                             level="warn")
            except Exception as e:
                hub_emit(self.signals, "vts", f"couldn't create input {name}: {e}", level="warn")

    async def _load_expressions(self):
        self._expressions = {}
        try:
            resp = await self.vts.request(self.vts.vts_request.BaseRequest(
                "ExpressionStateRequest", {"details": False}))
            for ex in ((resp or {}).get("data") or {}).get("expressions") or []:
                f = ex.get("file") or ""
                if f:
                    stem = f.lower().replace(".exp3.json", "")
                    self._expressions[stem] = f
        except Exception as e:
            hub_emit(self.signals, "vts", f"expression list failed: {e}", level="warn")

    def _find_expression(self, names):
        """Expression file matching any name, with or without "exp_"."""
        for n in names:
            for stem in (n, f"exp_{n}"):
                if stem in self._expressions:
                    return self._expressions[stem]
        return None

    def _resolve_pose_targets(self):
        """For each pose, an expression file or a hotkey the model has."""
        self._pose_targets = {}
        for pose, names in POSE_EXPRESSION_CANDIDATES.items():
            f = self._find_expression(names)
            if f:
                self._pose_targets[pose] = ("expression", f)
                continue
            for n in names:
                if n in self._hotkey_cache:
                    self._pose_targets[pose] = ("hotkey", self._hotkey_cache[n])
                    break
        found = {p: f"{k}:{r}" for p, (k, r) in self._pose_targets.items()}
        missing = [p for p in POSE_EXPRESSION_CANDIDATES if p not in self._pose_targets]
        hub_emit(self.signals, "vts",
                 f"agent poses — model expressions: {found or 'none'}; "
                 f"parameter-only: {missing or 'none'}")

    async def _set_pose_target(self, pose):
        """Switch the model expression / hotkey to match a pose. Only
        undoes what Lumi itself switched on."""
        target = self._pose_targets.get(pose)
        if self._pose_active and (self._pose_active[0] != pose or target is None):
            _, kind, ref = self._pose_active
            await self._activate_target(kind, ref, False)
            self._pose_active = None
        if target and not self._pose_active:
            kind, ref = target
            if await self._activate_target(kind, ref, True):
                self._pose_active = (pose, kind, ref)

    async def _own_expression(self, file, owner, on):
        """Reference-counted expression switching: a file shared by a pose
        and an emotion (e.g. dizzy eyes) only goes off when neither wants it."""
        owners = self._expr_owners.setdefault(file, set())
        was_on = bool(owners)
        (owners.add if on else owners.discard)(owner)
        if bool(owners) == was_on:
            return True
        resp = await self.send_request(self.vts.vts_request.BaseRequest(
            "ExpressionActivationRequest", {"expressionFile": file, "active": bool(owners)}))
        return bool(resp)

    async def _release_expressions(self):
        """On shutdown: switch off every expression Lumi switched on, so
        the avatar isn't left with dizzy eyes."""
        for f, owners in list(self._expr_owners.items()):
            if owners and self.is_ready():
                owners.clear()
                await self.send_request(self.vts.vts_request.BaseRequest(
                    "ExpressionActivationRequest", {"expressionFile": f, "active": False}))
        if self._pose_active and self._pose_active[1] == "hotkey" and self.is_ready():
            await self._activate_target("hotkey", self._pose_active[2], False)
        self._pose_active = None

    async def _activate_target(self, kind, ref, on):
        try:
            if kind == "expression":
                return await self._own_expression(ref, "pose", on)
            else:
                # Hotkeys toggle: firing again undoes it. API path only.
                req = self.vts.vts_request.BaseRequest("HotkeyTriggerRequest", {"hotkeyID": ref})
            resp = await self.send_request(req)
            return bool(resp)
        except Exception:
            return False

    # ---------------------------------------------------------------
    # Parameter discovery + verification
    # ---------------------------------------------------------------
    def _maybe_allowed(self, name):
        return self._allowed is None or name in self._allowed

    async def _test_injection(self, param_id, value, mode):
        """Return (ok, error_message_or_None)."""
        if not self._maybe_allowed(param_id):
            return False, "not an input parameter of this model"
        try:
            request = self.vts.vts_request.BaseRequest(
                "InjectParameterDataRequest",
                {
                    "faceFound": False,
                    "mode": mode,
                    "parameterValues": [
                        {"id": param_id, "value": self._clamp(param_id, value), "weight": 1.0}
                    ],
                },
            )
            response = await self.vts.request(request)
            if not response:
                return False, "no response"
            if response.get("messageType") == "APIError":
                msg = response.get("data", {}).get("message", "unknown")
                return False, f"APIError: {msg}"
            return True, None
        except Exception as e:
            return False, f"exception: {e}"

    async def _verify_param(self, param_id):
        """Return (mode_or_None, error_string_or_None)."""
        ok, err_set = await self._test_injection(param_id, 0.0, "set")
        if ok:
            return "set", None
        ok, err_add = await self._test_injection(param_id, 0.05, "add")
        if ok:
            return "add", None
        return None, f"set→[{err_set}] add→[{err_add}]"

    async def _probe_candidates(self, candidates):
        """First candidate the model has (allowlist) and VTS accepts."""
        for name in candidates:
            if not self._maybe_allowed(name):
                continue
            ok, _ = await self._test_injection(name, 0.0, "set")
            if ok:
                return name
        return None

    async def detect_parameter_names(self):
        hub_emit(self.signals, "vts", "probing parameter names…")
        self._param_modes = {}

        self._head_names = {}
        for slot, candidates in HEAD_NAME_CANDIDATES.items():
            name = await self._probe_candidates(candidates)
            if name:
                self._head_names[slot] = name

        self._extra_names = {}
        for slot, candidates in EXTRA_NAME_CANDIDATES.items():
            name = await self._probe_candidates(candidates)
            if name:
                self._extra_names[slot] = name

        self._mouth_name = await self._probe_candidates(MOUTH_NAME_CANDIDATES)
        self._smile_name = await self._probe_candidates(SMILE_NAME_CANDIDATES)
        self._breath_name = await self._probe_candidates(BREATH_NAME_CANDIDATES)

        hub_emit(self.signals, "vts", "verifying injection modes…")
        failures = {}

        for slot in list(self._head_names):
            name = self._head_names[slot]
            mode, err = await self._verify_param(name)
            if mode:
                self._param_modes[name] = mode
            else:
                failures[f"head.{slot}"] = f"{name} | {err}"
                del self._head_names[slot]

        for slot in list(self._extra_names):
            name = self._extra_names[slot]
            mode, err = await self._verify_param(name)
            if mode:
                self._param_modes[name] = mode
                continue
            fallback = VTS_INPUT_FALLBACK.get(slot)
            if fallback and fallback != name:
                fb_mode, fb_err = await self._verify_param(fallback)
                if fb_mode:
                    self._extra_names[slot] = fallback
                    self._param_modes[fallback] = fb_mode
                    continue
                failures[f"extra.{slot}"] = f"{name} | {err} || fallback {fallback} | {fb_err}"
            else:
                failures[f"extra.{slot}"] = f"{name} | {err}"
            del self._extra_names[slot]

        for attr in ("_mouth_name", "_smile_name", "_breath_name"):
            name = getattr(self, attr)
            if not name:
                continue
            mode, err = await self._verify_param(name)
            if mode:
                self._param_modes[name] = mode
            else:
                failures[attr.strip("_")] = f"{name} | {err}"
                setattr(self, attr, None)

        ext = self._extra_names
        if (ext.get("bx") == "FaceAngleX" and ext.get("by") == "FaceAngleY"
                and ext.get("bz") == "FaceAngleZ"):
            for s in ("bx", "by", "bz"):
                ext.pop(s, None)

        hub_emit(self.signals, "vts",
                 f"detected — head:{self._head_names} extra:{self._extra_names} "
                 f"mouth:{self._mouth_name} smile:{self._smile_name} breath:{self._breath_name}")
        if failures:
            hub_emit(self.signals, "vts", f"unusable params: {failures}", level="warn")
        if len(self._head_names) < 5:
            hub_emit(self.signals, "vts",
                     "WARNING — head detection incomplete. Head motion limited.", level="warn")
        have = {
            "body": all(k in ext for k in ("bx", "by", "bz")),
            "brows": all(k in ext for k in ("bly", "bry")),
            "gaze": "ex" in ext and "ey" in ext,
            "arms": all(s in ext for s in ("la1", "la2", "la3", "la4", "ra1", "ra2", "ra3", "ra4")),
            "breath": bool(self._breath_name),
        }
        hub_emit(self.signals, "vts", f"motion subsystems — {have}")

    def _resolve_hotkey(self, name):
        if not name:
            return name
        return HOTKEY_ALIASES.get(name.lower(), name)

    # ---------------------------------------------------------------
    # Parameter injection (one batched, rate-limited frame)
    # ---------------------------------------------------------------
    async def inject_parameters(self, parameters, mode="set"):
        """Queue ad-hoc values into the next frame. They're held for
        OVERRIDE_HOLD_S so callers don't have to resend every frame.
        Nothing is sent on its own: the 60 FPS cap covers everything."""
        if not parameters:
            return
        until = time.time() + OVERRIDE_HOLD_S
        for p in parameters:
            pid = p.get("id")
            if not pid:
                continue
            if p.get("_mode"):
                self._param_modes[pid] = p["_mode"]
            self._overrides[pid] = (float(p.get("value", 0.0)), until)

    async def _send_frame(self, frame, now):
        """Send one frame: READY only, allowlisted params only, <= 60 FPS."""
        if not frame or not self.is_ready():
            return False
        if not self._limiter.ready(now):
            return False
        frame = vm.filter_allowed(frame, self._allowed)
        by_mode = {}
        for pid, value in frame.items():
            by_mode.setdefault(self._param_modes.get(pid, "set"), []).append(
                {"id": pid, "value": self._clamp(pid, value), "weight": 1.0})
        for m, values in by_mode.items():
            await self.send_request(self.vts.vts_request.BaseRequest(
                "InjectParameterDataRequest",
                {"faceFound": False, "mode": m, "parameterValues": values}))
        return True

    def _clamp(self, param_id, value):
        if param_id in VTS_INPUT_RANGES:
            lo, hi = VTS_INPUT_RANGES[param_id]
        else:
            lo, hi = PARAM_RANGES.get(param_id, (-1.0, 1.0))
        return max(lo, min(hi, float(value)))

    # ---------------------------------------------------------------
    # Mouth (called by tts.py, from its playback thread)
    # ---------------------------------------------------------------
    def set_mouth_open(self, value):
        self._mouth.set_target(value, self._mouth._t_form)

    def set_mouth_shape(self, open_, form=0.0):
        self._mouth.set_target(open_, form)

    # ---------------------------------------------------------------
    # Agent tracker (any thread)
    # ---------------------------------------------------------------
    def _on_agent_update(self, snapshot):
        self._agent_snapshot = snapshot

    # ---------------------------------------------------------------
    # Keyboard hotkey path
    # ---------------------------------------------------------------
    def _trigger_via_keyboard(self, name):
        combo = HOTKEY_KEYBINDINGS.get((name or "").lower())
        if not combo:
            hub_emit(self.signals, "vts", f"no keybinding for hotkey '{name}'", level="warn")
            return False
        try:
            import keyboard
        except ImportError:
            hub_emit(self.signals, "vts",
                     "keyboard library not installed — cannot simulate hotkeys", level="error")
            return False
        focused = _focus_window_by_title(VTS_WINDOW_TITLE)
        if not focused:
            hub_emit(self.signals, "vts", f"could not focus VTS window for '{name}'", level="warn")
            return False
        time.sleep(0.15)
        try:
            parts = combo.lower().split("+")
            key = parts[-1]
            modifiers = parts[:-1]
            for mod in modifiers:
                keyboard.press(mod)
                time.sleep(0.02)
            time.sleep(0.05)
            keyboard.press(key)
            time.sleep(0.05)
            keyboard.release(key)
            time.sleep(0.02)
            for mod in reversed(modifiers):
                keyboard.release(mod)
                time.sleep(0.02)
            hub_emit(self.signals, "vts", f"keyboard hotkey '{name}' → {combo} (sequenced)")
            return True
        except Exception as e:
            hub_emit(self.signals, "vts", f"keyboard send failed for '{name}': {e}", level="warn")
            return False

    # ---------------------------------------------------------------
    # State-aware expression API
    # ---------------------------------------------------------------
    def set_expression_state(self, state_name, enabled):
        key = (state_name or "").strip().lower()
        if key not in EXPRESSION_STATE_MAP:
            return False, False, f"unknown state name '{state_name}'"
        hotkey_name, sense = EXPRESSION_STATE_MAP[key]
        requested_hotkey_state = enabled if sense == +1 else (not enabled)
        current = self._toggle_state.get(hotkey_name, False)
        if current == requested_hotkey_state:
            return False, enabled, None
        self._trigger_via_keyboard(hotkey_name)
        self._toggle_state[hotkey_name] = requested_hotkey_state
        return True, enabled, None

    def list_expression_states(self):
        result = {}
        for state_name, (hotkey_name, sense) in EXPRESSION_STATE_MAP.items():
            raw = self._toggle_state.get(hotkey_name, False)
            result[state_name] = raw if sense == +1 else (not raw)
        return result

    def list_expression_names(self):
        return list(EXPRESSION_STATE_MAP.keys())

    def resync_expression_states(self, states):
        for state_name, is_on in states.items():
            key = (state_name or "").strip().lower()
            if key not in EXPRESSION_STATE_MAP:
                continue
            hotkey_name, sense = EXPRESSION_STATE_MAP[key]
            raw_state = bool(is_on) if sense == +1 else (not bool(is_on))
            self._toggle_state[hotkey_name] = raw_state

    # ---------------------------------------------------------------
    # Brows and arms
    # ---------------------------------------------------------------
    def _compute_brows(self, t, dt, targets):
        """targets: (L-y, R-y, L-form, R-form) in -1..1. Smoothed, then
        deduped per input (on ariu all four go to the single "Brows")."""
        ext = self._extra_names or {}
        brow_slots = ("bly", "bry", "blf", "brf")
        if not any(s in ext for s in brow_slots):
            return {}
        for i in range(4):
            self._brow_current[i] = vm.approach(self._brow_current[i], targets[i], dt, 0.1)
        sums, counts = {}, {}
        for slot, val in zip(brow_slots, self._brow_current):
            if slot not in ext:
                continue
            pid = ext[slot]
            if pid == "Brows":                       # that input is 0..1
                val = (val + 1.0) * 0.5
            sums[pid] = sums.get(pid, 0.0) + val
            counts[pid] = counts.get(pid, 0) + 1
        return {pid: sums[pid] / counts[pid] for pid in sums}

    def _compute_arms(self, t, speaking, mouth_level, head_x, head_z):
        ext = self._extra_names or {}
        slots = ("la1", "la2", "la3", "la4", "ra1", "ra2", "ra3", "ra4")
        if not all(s in ext for s in slots):
            return {}
        d_l, d_r = self._arms.deltas(t, speaking, mouth_level, head_x, head_z)
        out = {}
        for slot, delta in zip(slots, d_l + d_r):
            out[ext[slot]] = delta
        return out

    # ---------------------------------------------------------------
    # Motion frame
    # ---------------------------------------------------------------
    def _gaze_mode(self, pose, reading, speaking):
        if pose in ("approval", "error"):
            return "camera"
        if speaking:
            return "speaking"
        if reading:
            return "reading"
        if pose == "thinking":
            return "thinking"
        if pose == "focused":
            return "attentive"
        return "idle" if self._is_idle else "attentive"

    def compute_frame(self, now, speaking):
        """Everything the face does this instant, as {param id: value}.
        Pure apart from advancing the motion state; no I/O."""
        t = now - self._session_start
        dt = max(0.0, min(0.1, now - self._last_frame_t))
        self._last_frame_t = now

        # ---- what the pipeline is doing ----
        pose, reading = vm.pose_from_snapshot(self._agent_snapshot, now)
        if speaking and pose in ("thinking", "focused"):
            pose = "idle"                            # talking beats pondering
        busy = speaking or pose != "idle" or reading

        # ---- idle / sleepy ----
        if busy:
            self._last_interaction_time = now
            self._is_idle = False
        elif now - self._last_interaction_time > 5.0:
            if not self._is_idle:
                self._is_idle = True
                self._idle_start_time = now
        last_msg = float(getattr(self.signals, "last_message_time", 0) or 0)
        idle_s = now - max(self._last_interaction_time, last_msg)
        yawn, yawn_started = self._yawn.update(t, datetime.datetime.now().hour, idle_s)
        sleepy = self._yawn.sleepy
        if yawn_started:
            self._emotion.trigger("yawning", YAWN_CHANNELS, now)
            hub_emit(self.signals, "vts", "late and idle: [yawning]")

        # ---- emotion + pose ----
        em = self._emotion.update(now, speaking)
        valence, arousal, dominance = self._emotion.vad
        pb = self._pose.update(dt, "idle" if pose == "idle" and sleepy else pose)

        # ---- head ----
        amount = pb.sway * (0.85 if speaking else 1.0) * (1.0 + 0.3 * max(0.0, arousal))
        if self._is_idle:
            amount *= max(0.4, 1.0 - (now - self._idle_start_time) * 0.03)
        if sleepy:
            amount *= 0.6
        hx, hy, hz = self._sway.sample(t, amount, micro=0.5 if sleepy else 1.0)
        hy += pb.head_y + dominance * 1.5 + yawn * 8.0 - (2.0 if sleepy else 0.0)
        hz += pb.head_z

        # ---- eyes ----
        blink = self._blink.value(t, rate=1.0 + 0.4 * max(0.0, arousal) + (0.15 if speaking else 0.0),
                                  sleepy=sleepy)
        lids = (0.55 if self._is_idle else 0.75) * pb.eye_open + em["eye_open"] * 0.3
        if sleepy:
            lids *= 0.8
        lids *= 1.0 - 0.75 * yawn
        eye_open = max(0.0, min(1.0, lids)) * blink
        gx, gy = self._gaze.value(t, self._gaze_mode(pose, reading, speaking), lively=arousal > 0.45)
        if yawn:
            gy += 0.4 * yawn

        # ---- mouth: visemes + emotion smile + yawn ----
        mo, mf = self._mouth.update(dt, speaking)
        mouth_open = max(mo, 0.95 * yawn)
        smile = em["smile"]
        form = (smile * 0.6 if speaking else smile) + mf * 0.7 - 0.3 * yawn

        # ---- brows ----
        idle_y = math.sin(t * 0.17 * math.tau) * 0.03
        speech_y = mo * 0.08 if speaking else 0.0
        brows = [pb.brows[0] + em["bly"] + idle_y + speech_y + 0.2 * yawn,
                 pb.brows[1] + em["bry"] + idle_y + speech_y + 0.2 * yawn,
                 pb.brows[2] + em["blf"], pb.brows[3] + em["brf"]]

        # ---- breath ----
        breath = self._breath.update(dt, speaking, mo, sleepy)

        # ---- assemble ----
        frame = {}
        names = self._head_names or {}
        for slot, val in (("x", hx), ("y", hy), ("z", hz), ("el", eye_open), ("er", eye_open)):
            if slot in names:
                frame[names[slot]] = val
        ext = self._extra_names or {}
        lag = [hx * 0.22, hy * 0.15, hz * 0.25]
        self._body = [vm.approach(b, l, dt, 0.25) for b, l in zip(self._body, lag)]
        for slot, val in zip(("bx", "by", "bz"), self._body):
            if slot in ext:
                frame[ext[slot]] = val
        if "ex" in ext and "ey" in ext:
            frame[ext["ex"]] = gx
            frame[ext["ey"]] = max(-1.0, min(1.0, gy))
        frame.update(self._compute_brows(t, dt, brows))
        frame.update(self._compute_arms(t, speaking, mo, hx, hz))
        if self._mouth_name:
            frame[self._mouth_name] = mouth_open
        if self._smile_name:
            frame[self._smile_name] = max(-1.0, min(1.0, form))
        if self._breath_name:
            frame[self._breath_name] = breath

        for pid in [p for p, (_, until) in self._overrides.items() if until < now]:
            del self._overrides[pid]
        for pid, (val, _) in self._overrides.items():
            frame[pid] = val

        self._frame_pose = pose
        return frame

    async def _update_pose_expression(self, pose, now):
        """Switch model expressions once a pose has held POSE_DEBOUNCE_S
        (agent turns flicker between thinking and executing)."""
        if pose != self._pose_candidate[0]:
            self._pose_candidate = (pose, now)
            return
        if pose != self._pose_name and now - self._pose_candidate[1] >= POSE_DEBOUNCE_S:
            self._pose_name = pose
            hub_emit(self.signals, "vts", f"pose → {pose}")
            await self._set_pose_target(pose)

    async def _emotion_hotkey(self, hotkey, on):
        """Switch an emotion's extra look (spiral_eyes, heart_eyes,
        darkened) on or off: through its expression file when the model has
        one, otherwise by toggling the hotkey (keyboard path)."""
        f = self._find_expression(EMOTION_EXPRESSION_CANDIDATES.get(hotkey, [hotkey]))
        if f:
            await self._own_expression(f, "emotion", on)
        else:
            await self.trigger_expression(hotkey)

    async def _sync_emotion_hotkey(self):
        """Switch the emotion's look off again once the emotion has decayed."""
        if self._emotion_hotkey_on and self._emotion.tag is None:
            hk, self._emotion_hotkey_on = self._emotion_hotkey_on, None
            await self._emotion_hotkey(hk, False)

    async def _tick_motion(self, speaking):
        now = time.time()
        frame = self.compute_frame(now, speaking)
        await self._update_pose_expression(getattr(self, "_frame_pose", "idle"), now)
        await self._sync_emotion_hotkey()
        await self._send_frame(frame, now)

    # ---------------------------------------------------------------
    # Raw hotkey trigger
    # ---------------------------------------------------------------
    async def trigger_expression(self, hotkey_name):
        if not hotkey_name:
            return
        resolved = self._resolve_hotkey(hotkey_name)
        lookup_key = resolved.lower()

        if USE_KEYBOARD_HOTKEYS_ONLY:
            # Blocking key presses (with sleeps) run off the event loop.
            ok = await asyncio.get_running_loop().run_in_executor(None, self._trigger_via_keyboard, lookup_key)
            if ok:
                self._toggle_state[lookup_key] = not self._toggle_state.get(lookup_key, False)
            else:
                hub_emit(self.signals, "vts", f"hotkey '{lookup_key}' failed (keyboard path)", level="warn")
            return

        hotkey_id = self._hotkey_cache.get(lookup_key)
        if not hotkey_id:
            try:
                for hk in await self.get_hotkeys():
                    if hk.get("name", "").lower() == lookup_key:
                        hotkey_id = hk.get("hotkeyID")
                        self._hotkey_cache[lookup_key] = hotkey_id
                        break
            except Exception:
                pass
        if not hotkey_id:
            if self._trigger_via_keyboard(lookup_key):
                self._toggle_state[lookup_key] = not self._toggle_state.get(lookup_key, False)
                return
            hub_emit(self.signals, "vts", f"hotkey '{resolved}' NOT FOUND", level="warn")
            return

        response = await self.send_request(self.vts.vts_request.BaseRequest(
            "HotkeyTriggerRequest", {"hotkeyID": hotkey_id}))
        if not response:
            if self._trigger_via_keyboard(lookup_key):
                self._toggle_state[lookup_key] = not self._toggle_state.get(lookup_key, False)
                return
            hub_emit(self.signals, "vts", f"API + keyboard both failed for '{resolved}'", level="warn")
            return
        self._toggle_state[lookup_key] = not self._toggle_state.get(lookup_key, False)
        hub_emit(self.signals, "vts", f"fired '{resolved}' (API)")

    # ---------------------------------------------------------------
    # Emotion routing (continuous state, Task 4)
    # ---------------------------------------------------------------
    @staticmethod
    def emotion_channels(tag):
        """Face channel targets for a tag: smile, the four brows, lids."""
        ch = {}
        for role, value in PARAMETER_EXPRESSION_MAP.get(tag, []):
            if role == "smile":
                ch["smile"] = value
        brow = BROW_EXPRESSION_MAP.get(tag)
        if brow:
            ch.update(zip(("bly", "bry", "blf", "brf"), brow))
        arousal = vm.EMOTION_VAD.get(tag, (0.0, 0.0, 0.0))[1]
        ch["eye_open"] = 0.25 * arousal                 # excited: wide; bored: heavy
        return ch

    async def set_emotion(self, emotion_tag):
        tag = (emotion_tag or "").lower().strip()
        if not tag:
            return
        known = (tag in PARAMETER_EXPRESSION_MAP or tag in BROW_EXPRESSION_MAP
                 or tag in vm.EMOTION_VAD or tag in EMOTION_HOTKEY_MAP)
        if not known:
            hub_emit(self.signals, "vts", f"tag [{tag}] has no mapping (skipped)")
            return
        ch = self.emotion_channels(tag)
        self._emotion.trigger(tag, ch, time.time())
        self._arms.apply_emotion(tag)
        hub_emit(self.signals, "vts",
                 f"tag [{tag}] → {', '.join(f'{k}={v:+.2f}' for k, v in ch.items())} "
                 f"VAD={vm.EMOTION_VAD.get(tag, (0, 0, 0))}")

        hotkey = EMOTION_HOTKEY_MAP.get(tag)
        if hotkey != self._emotion_hotkey_on:
            if self._emotion_hotkey_on:                 # a different emotion took over
                await self._emotion_hotkey(self._emotion_hotkey_on, False)
                self._emotion_hotkey_on = None
            if hotkey:
                hub_emit(self.signals, "vts", f"tag [{tag}] → '{hotkey}'")
                await self._emotion_hotkey(hotkey, True)
                self._emotion_hotkey_on = hotkey

    async def clear_expression(self):
        self._emotion.clear(time.time())
        self._arms.set_emotion_lean(0.0)
        hub_emit(self.signals, "vts", "expression released (decaying to neutral)")

    # ---------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------
    async def get_hotkeys(self):
        response = await self.send_request(self.vts.vts_request.requestHotKeyList())
        return (response or {}).get("data", {}).get("availableHotkeys", [])

    async def send_hotkey(self, hotkey):
        await self.send_request(self.vts.vts_request.requestTriggerHotKey(hotkey))

    async def get_item_list(self):
        request = self.vts.vts_request.BaseRequest(
            "ItemListRequest",
            {
                "includeAvailableSpots": False,
                "includeItemInstancesInScene": False,
                "includeAvailableItemFiles": True,
            },
        )
        response = (await self.send_request(request)).get("data") or {}
        return [item["fileName"] for item in response.get("availableItemFiles", [])]

    async def spawn_microphone(self):
        if "microphone_red (@7MDigital).png" not in self.item_list:
            self.signals.sio_queue.put(("error", "Microphone item not found in Vtube Studio"))
            return
        request = self.vts.vts_request.BaseRequest(
            "ItemLoadRequest",
            {
                "fileName": "microphone_red (@7MDigital).png",
                "positionX": VTUBE_MIC_POSITION["x"],
                "positionY": VTUBE_MIC_POSITION["y"] - 1,
                "size": VTUBE_MIC_POSITION["size"],
                "rotation": VTUBE_MIC_POSITION["rotation"],
                "fadeTime": 0,
                "order": 4,
                "failIfOrderTaken": False,
            },
        )
        resp = await self.send_request(request)
        if not resp:
            return
        self.prop_instance_ids["microphone"] = resp["data"]["instanceID"]
        await self.send_request(self.vts.vts_request.BaseRequest(
            "ItemMoveRequest",
            {"itemsToMove": [{
                "itemInstanceID": self.prop_instance_ids["microphone"],
                "timeInSeconds": 0.6, "fadeMode": "zip",
                "positionX": VTUBE_MIC_POSITION["x"], "positionY": VTUBE_MIC_POSITION["y"],
            }]},
        ))

    async def despawn_microphone(self):
        if "microphone" in self.prop_instance_ids:
            await self.send_request(self.vts.vts_request.BaseRequest(
                "ItemMoveRequest",
                {"itemsToMove": [{
                    "itemInstanceID": self.prop_instance_ids["microphone"],
                    "timeInSeconds": 0.6, "fadeMode": "easeBoth",
                    "positionX": VTUBE_MIC_POSITION["x"], "positionY": VTUBE_MIC_POSITION["y"] - 1,
                }]},
            ))
        await asyncio.sleep(0.6)
        await self.send_request(self.vts.vts_request.BaseRequest(
            "ItemUnloadRequest", {"fileNames": ["microphone_red (@7MDigital).png"]}))

    async def move_model(self, mode):
        if mode not in VTUBE_MODEL_POSITIONS:
            self.signals.sio_queue.put(("error", "Invalid model location: " + mode))
            return
        await self.send_request(self.vts.vts_request.BaseRequest(
            "MoveModelRequest",
            {
                "timeInSeconds": 0.8,
                "valuesAreRelativeToModel": False,
                "positionX": VTUBE_MODEL_POSITIONS[mode]["x"],
                "positionY": VTUBE_MODEL_POSITIONS[mode]["y"],
                "rotation":  VTUBE_MODEL_POSITIONS[mode]["rotation"],
                "size":      VTUBE_MODEL_POSITIONS[mode]["size"],
            },
        ))

    async def send_request(self, request):
        """Send one request. Returns {} on an API error or a dropped
        connection (which also starts a reconnect)."""
        try:
            response = await self.vts.request(request)
        except Exception as e:                      # socket closed, VTS quit, etc.
            self._mark_lost(f"{type(e).__name__}: {e}"[:80])
            return {}
        if response.get("messageType") == "APIError":
            msg = response.get("data", {}).get("message", "unknown")
            hub_emit(self.signals, "vts", f"API error: {msg}", level="warn")
            return {}
        return response

    # ---------------------------------------------------------------
    # Main run loop
    # ---------------------------------------------------------------
    async def run(self):
        if not self.enabled:
            return
        print("\033[90mVTube Studio: connecting...\033[0m")
        if not await self._establish():
            if not getattr(self, "_closed_logged", False):    # closed VTS already said so in one line
                print("\033[91mVTube Studio: disabling module — connection failed\033[0m")
            self.enabled = False
            return
        print("\033[92mVTube Studio: connected and authenticated\033[0m")

        self._agent_snapshot = agent_tracker.snapshot()
        self._tracking = self._on_agent_update
        agent_tracker.register(self._tracking)
        self._session_start = time.time()
        self._last_frame_t = time.time()
        motion_interval = 1.0 / MOTION_HZ

        try:
            while not self.signals.terminate:
                if self.session != "ready" or not self.is_ready():
                    if self.session == "ready":
                        self._mark_lost("socket closed")
                    await self._reconnect()
                    continue
                if not self.enabled:
                    await asyncio.sleep(0.1)
                    continue
                try:
                    if not self.queue.empty():
                        await self._handle_action(self.queue.get())
                except Exception as e:
                    print(f"\033[91mVTube Studio action error: {e}\033[0m")
                now = time.time()
                if now - self._last_motion_tick >= motion_interval:
                    speaking = getattr(self.signals, "AI_speaking", False)
                    try:
                        await self._tick_motion(speaking)
                    except Exception as e:
                        print(f"\033[91mVTube motion error: {e}\033[0m")
                    self._last_motion_tick = now
                await asyncio.sleep(0.004)
        finally:
            agent_tracker.unregister(self._tracking)
            await self._release_expressions()
            self.session = "disconnected"
            try:
                await self.vts.close()
            except Exception:
                pass
            print("\033[90mVTube Studio: disconnected\033[0m")

    async def _handle_action(self, action):
        a = action.action
        d = action.data
        if a == "get_hotkeys":
            self.signals.sio_queue.put(("get_hotkeys", await self.get_hotkeys()))
        elif a == "send_hotkey":
            await self.send_hotkey(d)
        elif a == "trigger_expression":
            await self.trigger_expression(d)
        elif a == "set_expression":
            state_name = d.get("name", "")
            enabled = bool(d.get("enabled", True))
            changed, actual, err = self.set_expression_state(state_name, enabled)
            if err:
                hub_emit(self.signals, "vts", f"set_expression '{state_name}' rejected: {err}", level="warn")
            else:
                hub_emit(self.signals, "vts",
                         f"set_expression '{state_name}' → {'on' if actual else 'off'}"
                         f"{' (fired)' if changed else ' (already there)'}")
        elif a == "set_emotion":
            await self.set_emotion(d)
        elif a == "clear_expression":
            await self.clear_expression()
        elif a == "inject_parameters":
            await self.inject_parameters(d)
        elif a == "move_model":
            await self.move_model(d)
        elif a == "spawn_microphone":
            await self.spawn_microphone()
        elif a == "despawn_microphone":
            await self.despawn_microphone()
        elif a == "resync_expression_states":
            self.resync_expression_states(d or {})
            hub_emit(self.signals, "vts", "expression states resynced")
        else:
            print(f"\033[90mVTube Studio: unknown action '{a}'\033[0m")

    # ---------------------------------------------------------------
    # Action container + external API
    # ---------------------------------------------------------------
    class Action:
        def __init__(self, action, data):
            self.action = action
            self.data = data

    class API:
        def __init__(self, outer):
            self.outer = outer

        def set_movement_status(self, status):
            self.outer.enabled = status
            self.outer.signals.sio_queue.put(("movement_status", status))
            if status:
                while not self.outer.queue.empty():
                    self.outer.queue.get()

        def get_movement_status(self):
            return self.outer.enabled

        def get_hotkeys(self):
            if not self.outer.enabled:
                self.outer.signals.sio_queue.put(("error", "Vtube Studio Module is disabled"))
                return
            self.outer.queue.put(self.outer.Action("get_hotkeys", None))

        def send_hotkey(self, hotkey):
            if not self.outer.enabled:
                return
            self.outer.queue.put(self.outer.Action("send_hotkey", hotkey))

        def trigger_hotkey(self, name):
            if not self.outer.enabled:
                return
            self.outer.queue.put(self.outer.Action("trigger_expression", name))

        def set_emotion(self, emotion_tag):
            if not self.outer.enabled:
                return
            self.outer.queue.put(self.outer.Action("set_emotion", emotion_tag))

        def clear_expression(self):
            if not self.outer.enabled:
                return
            self.outer.queue.put(self.outer.Action("clear_expression", None))

        def set_expression(self, name, enabled):
            if not self.outer.enabled:
                return
            self.outer.queue.put(self.outer.Action("set_expression", {"name": name, "enabled": bool(enabled)}))

        def list_expression_states(self):
            return self.outer.list_expression_states()

        def list_expression_names(self):
            return self.outer.list_expression_names()

        def resync_expression_states(self, states):
            if not self.outer.enabled:
                return
            self.outer.queue.put(self.outer.Action("resync_expression_states", states or {}))

        def move_model(self, mode):
            if not self.outer.enabled:
                return
            self.outer.queue.put(self.outer.Action("move_model", mode))

        def list_positions(self):
            return list(VTUBE_MODEL_POSITIONS.keys())

        def list_hotkeys(self):
            return sorted(self.outer._hotkey_cache.keys())

        def trigger_prop(self, prop_action):
            if not self.outer.enabled:
                return
            self.outer.queue.put(self.outer.Action(prop_action, None))

        def set_mouth_open(self, value):
            if not self.outer.enabled:
                return
            self.outer.set_mouth_open(value)

        def set_mouth_shape(self, open_, form=0.0):
            """Lip-sync from tts.py: mouth openness 0..1 and vowel shape -1..1."""
            if not self.outer.enabled:
                return
            self.outer.set_mouth_shape(open_, form)

        def inject_parameters(self, parameters, mode="set"):
            if not self.outer.enabled:
                return
            self.outer.queue.put(self.outer.Action("inject_parameters", parameters))

        def session_state(self):
            return self.outer.session

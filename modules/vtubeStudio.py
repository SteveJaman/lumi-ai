"""
VTube Studio controller.

Drives the Live2D model via the VTS plugin API using pyvts.

Responsibilities:
  - Connect + authenticate on startup
  - Probe the loaded model to discover its real parameter names
  - Drive continuous organic motion (head, blink, breath, brows, gaze)
  - Drive mouth-open from TTS volume envelope (via tts.py)
  - Fire hotkey-based expressions on emotion tags from prompter.py
  - Expose a state-aware expression API so Lumi can request
    "put on hat" / "take off jacket" and get deterministic results

CRITICAL — VTS Input names vs Live2D param names
------------------------------------------------
VTS accepts InjectParameterDataRequest payloads addressed to Live2D
parameter names (like ParamAngleX) and returns a clean success
response — then silently drops the value on the floor. No APIError,
no readback, no warning. Only injections into VTS *Input* names
(the "tracking parameters" in the model's ParameterSettings block,
field "Input" in ariu.vtube.json) actually reach the model.

Consequence: every candidate list below puts VTS Input names FIRST
and Live2D parameter names second. The probe walks each list and
takes the first name VTS accepts; if a Live2D param name sits at
the front, the probe stops there and the whole channel silently
does nothing.

The VTS Inputs on ariu (from ariu.vtube.json) are:
    FaceAngleX/Y/Z    → head angles + body angles (shared)
    EyeOpenLeft       → ParamEyeLOpen
    EyeOpenRight      → ParamEyeROpen
    EyeRightX         → ParamEyeBallX   (X is inverted in the model mapping)
    EyeRightY         → ParamEyeBallY
    MouthOpen         → ParamMouthOpenY
    MouthSmile        → ParamMouthForm
    Brows             → all four brow params (shared)

The body candidate slots (bx/by/bz) resolve to FaceAngleX/Y/Z, which
are the SAME inputs as head.x/y/z. At startup we detect this and
drop the body slots — otherwise every frame would inject two different
values to the same input and the last write would win. The body still
moves: VTS's own FaceAngleX → ParamBodyAngleX transform runs underneath
and produces a ~1/3-scale body lean from every head movement.

Motion architecture (v6)
------------------------
  Head      : 3-octave sine sway, dampened slightly during speech
  Body      : via head (shared VTS Input, see above)
  Eyes      : discrete saccades, biased to center when speaking, wider when idle
  Brows     : sticky emotion baseline + idle noise + speech emphasis,
              deduped to a single "Brows" write per frame
  Arms      : disabled (no VTS Input exists for arm params on ariu)
  Blink     : scheduled, occasionally double-blinks, relaxed to 85% when idle

Mode dispatch
-------------
Each verified param stores its working mode ("set" or "add") in
self._param_modes. inject_parameters() looks up the mode per param,
so a single batch can mix modes. A "_mode" key on a param dict
overrides the lookup.

Expression state tracking
-------------------------
Every hotkey on ariu is a ToggleExpression — firing it flips the
current state. Some are named for the ACTION (remove_jacket,
hide_ponytail_l) rather than the resulting STATE (jacket on,
ponytail visible), which reads backwards when a user says "put on
your jacket".

EXPRESSION_STATE_MAP normalizes that. Keys are the user-facing state
names Lumi sees; values are (hotkey_name, sense) where sense = +1 if
the hotkey's on-state matches the state name, -1 if inverted.

Hotkey firing paths
-------------------
1. API path — HotkeyTriggerRequest over the websocket. Broken on
   ariu for ToggleExpression hotkeys (VTS silently accepts, does
   nothing). Kept for models where it works.
2. Keyboard path — simulate the bound key combo after focusing VTS.
   Works reliably. Set USE_KEYBOARD_HOTKEYS_ONLY=True to always use it.
"""

import asyncio
import math
import os
import queue
import time
import random
import pyvts
import logging
import ctypes

from modules.module import Module
from constants import VTUBE_MODEL_POSITIONS, VTUBE_MIC_POSITION, DATA_ROOT
from modules.log_hub import emit as hub_emit


logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("websockets.legacy").setLevel(logging.WARNING)
logging.getLogger("websockets.legacy.protocol").setLevel(logging.WARNING)


_VTS_TOKEN_PATH = os.path.join(DATA_ROOT, "vtubeStudio_token.txt")


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
MOTION_HZ = 30


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
# ORGANIC MOTION
# ===============================================================
class _OrganicMotion:
    def __init__(self):
        rnd = random.uniform
        tau = math.tau
        self._hx = [(rnd(0, tau), 0.05), (rnd(0, tau), 0.11), (rnd(0, tau), 0.19)]
        self._hy = [(rnd(0, tau), 0.04), (rnd(0, tau), 0.09), (rnd(0, tau), 0.23)]
        self._hz = [(rnd(0, tau), 0.06), (rnd(0, tau), 0.13), (rnd(0, tau), 0.27)]
        self._bx = [(rnd(0, tau), 0.021), (rnd(0, tau), 0.037)]
        self._by = [(rnd(0, tau), 0.018), (rnd(0, tau), 0.033)]
        self._bz = [(rnd(0, tau), 0.024), (rnd(0, tau), 0.041)]
        self._bw = [(rnd(0, tau), 0.016), (rnd(0, tau), 0.029)]
        self._head_hist = []
        self._LAG_S = 0.18

    @staticmethod
    def _sum(terms, t):
        return sum(math.sin(t * f * math.tau + p) for p, f in terms) / len(terms)

    def head(self, t):
        hx = self._sum(self._hx, t) * 6.5
        hy = self._sum(self._hy, t) * 5.5
        hz = self._sum(self._hz, t) * 3.5
        return hx, hy, hz

    def tick(self, t, hx, hy, hz):
        self._head_hist.append((t, hx, hy, hz))
        if len(self._head_hist) > 90:
            self._head_hist.pop(0)

    def _lagged_head(self, t):
        cutoff = t - self._LAG_S
        while len(self._head_hist) > 1 and self._head_hist[0][0] < cutoff - 0.5:
            self._head_hist.pop(0)
        for entry in self._head_hist:
            if entry[0] >= cutoff:
                return entry[1], entry[2], entry[3]
        if self._head_hist:
            return self._head_hist[0][1:]
        return 0.0, 0.0, 0.0

    def body(self, t):
        lhx, lhy, lhz = self._lagged_head(t)
        bx = self._sum(self._bx, t) * 3.0 + lhx * 0.22
        by = self._sum(self._by, t) * 2.5 + lhy * 0.15
        bz = self._sum(self._bz, t) * 2.5 + lhz * 0.25
        bw = self._sum(self._bw, t) * 2.0
        return bx, by, bz, bw


# ===============================================================
# SACCADE SCHEDULER
# ===============================================================
class _SaccadeScheduler:
    SNAP_S = 0.08
    SOFT_S = 0.45

    def __init__(self):
        self._cur_x = 0.0
        self._cur_y = 0.0
        self._tgt_x = 0.0
        self._tgt_y = 0.0
        self._start_x = 0.0
        self._start_y = 0.0
        self._start_at = 0.0
        self._dur = self.SNAP_S
        self._next_at = 0.0

    def _pick_new_target(self, t, gaze_bias_x, gaze_bias_y, lively, idle):
        self._start_x = self._cur_x
        self._start_y = self._cur_y
        self._start_at = t
        r = random.random()
        
        if idle:
            # Bored / drifting attention: slower, wider drifts
            if r < 0.4:
                mag_x, mag_y = 0.30, 0.20
                self._dur = self.SOFT_S * 1.5
                hold = random.uniform(1.0, 3.5)
            else:
                mag_x, mag_y = 0.85, 0.70
                self._dur = self.SOFT_S * 2.5
                hold = random.uniform(2.5, 6.0)
        else:
            # Active attention: quick, focused snaps
            if r < 0.45:
                mag_x, mag_y = 0.12, 0.10
                self._dur = self.SNAP_S
                hold = random.uniform(0.35, 1.40)
            elif r < 0.85:
                mag_x, mag_y = 0.45, 0.35
                self._dur = self.SNAP_S
                hold = random.uniform(0.45, 1.60)
            else:
                mag_x, mag_y = 0.85, 0.60
                self._dur = self.SOFT_S
                hold = random.uniform(0.60, 2.20)
                
        self._tgt_x = max(-1.0, min(1.0, random.uniform(-mag_x, mag_x) + gaze_bias_x))
        self._tgt_y = max(-1.0, min(1.0, random.uniform(-mag_y, mag_y) + gaze_bias_y))
        
        if lively and not idle:
            hold *= 0.7
        self._next_at = t + hold

    def value(self, t, gaze_bias_x=0.0, gaze_bias_y=0.0, lively=False, idle=False):
        if self._next_at == 0.0 or t >= self._next_at:
            self._pick_new_target(t, gaze_bias_x, gaze_bias_y, lively, idle)
        elapsed = t - self._start_at
        k = min(1.0, elapsed / self._dur) if self._dur > 0 else 1.0
        e = 1.0 - (1.0 - k) ** 3
        self._cur_x = self._start_x + (self._tgt_x - self._start_x) * e
        self._cur_y = self._start_y + (self._tgt_y - self._start_y) * e
        return self._cur_x, self._cur_y


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
# BLINK SCHEDULER
# ===============================================================
class _BlinkScheduler:
    CLOSE_S = 0.07
    HOLD_S = 0.05
    OPEN_S = 0.11
    TOTAL = CLOSE_S + HOLD_S + OPEN_S

    def __init__(self):
        self._next_at = 0.0
        self._started_at = -1.0
        self._double_pending = False

    def value(self, t):
        if self._next_at == 0.0:
            self._next_at = t + random.uniform(2.5, 5.5)
        if t >= self._next_at:
            self._started_at = t
            if self._double_pending:
                self._next_at = t + self.TOTAL + random.uniform(0.10, 0.20)
                self._double_pending = False
            else:
                self._next_at = t + random.uniform(2.5, 6.5)
                if random.random() < 0.18:
                    self._double_pending = True
        elapsed = t - self._started_at
        if elapsed < 0 or elapsed > self.TOTAL:
            return 1.0
        if elapsed < self.CLOSE_S:
            return 1.0 - (elapsed / self.CLOSE_S)
        elif elapsed < self.CLOSE_S + self.HOLD_S:
            return 0.0
        else:
            return (elapsed - self.CLOSE_S - self.HOLD_S) / self.OPEN_S


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
        self._toggle_state = {}

        self._motion = _OrganicMotion()
        self._blink = _BlinkScheduler()
        self._saccades = _SaccadeScheduler()
        self._arms = _ArmChoreographer()

        self._session_start = time.time()
        self._last_motion_tick = 0.0
        self._last_head_tick = 0.0
        self._last_extra_tick = 0.0

        self._sticky_params = {}
        self._current_mouth_open = 0.0
        self._target_mouth_open = 0.0

        self._head_names = None
        self._extra_names = None
        self._mouth_name = None
        self._smile_name = None

        # Per-param mode lookup, populated by verification at startup.
        # Maps param_id → "set" | "add".
        self._param_modes = {}

        self._brow_baseline = [0.0, 0.0, 0.0, 0.0]
        self._brow_baseline_expires = 0.0
        self._brow_current = [0.0, 0.0, 0.0, 0.0]
        self._is_lively = False

        plugin_info = {
            "plugin_name": "Lumi VTS Plugin",
            "developer": "Lumi AI",
            "authentication_token_path": _VTS_TOKEN_PATH,
        }
        self.vts = pyvts.vts(plugin_info=plugin_info)

        # Tracking variables for idle/bored states
        self._last_interaction_time = time.time()
        self._is_idle = False
        self._idle_start_time = 0.0
        
        if not enabled:
            self.signals.vts_state = "skipped"

    # ---------------------------------------------------------------
    # Connection / Authentication
    # ---------------------------------------------------------------
    async def connect_and_auth(self):
        try:
            await self.vts.connect()
        except Exception as e:
            print(f"\033[91mVTube Studio: connection failed — {e}\033[0m")
            hub_emit(self.signals, "vts",
                     f"connection failed: {e}", level="error")
            self.signals.vts_state = "error"
            self.signals.vts_detail = str(e)[:60]
            return False
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
            return False
        hub_emit(self.signals, "vts", "connected and authenticated")
        self.signals.vts_state = "ready"
        return True

    async def load_hotkey_cache(self):
        try:
            hotkeys = await self.get_hotkeys()
            self._hotkey_cache = {
                hk["name"].lower(): hk["hotkeyID"]
                for hk in hotkeys
                if hk.get("name") and hk.get("hotkeyID")
            }
            print(
                f"\033[90mVTube Studio: cached {len(self._hotkey_cache)} "
                f"hotkeys\033[0m"
            )
            names = sorted(self._hotkey_cache.keys())
            hub_emit(self.signals, "vts",
                     f"cached {len(names)} hotkeys: {names}")
            self.signals.vts_detail = f"{len(names)} hotkeys"
            unmapped = []
            for tag, hotkey in EMOTION_HOTKEY_MAP.items():
                resolved = self._resolve_hotkey(hotkey)
                if resolved.lower() not in self._hotkey_cache:
                    unmapped.append(f"{tag}→{hotkey}")
            if unmapped:
                hub_emit(self.signals, "vts",
                         f"missing hotkeys for: {unmapped}", level="warn")
        except Exception as e:
            print(f"\033[90mVTube Studio: hotkey cache failed — {e}\033[0m")
            hub_emit(self.signals, "vts",
                     f"hotkey cache failed: {e}", level="warn")

    # ---------------------------------------------------------------
    # Parameter discovery + verification
    # ---------------------------------------------------------------
    async def _test_injection(self, param_id, value, mode):
        """Return (ok, error_message_or_None)."""
        try:
            request = self.vts.vts_request.BaseRequest(
                "InjectParameterDataRequest",
                {
                    "faceFound": False,
                    "mode": mode,
                    "parameterValues": [
                        {
                            "id": param_id,
                            "value": self._clamp(param_id, value),
                            "weight": 1.0,
                        }
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
        """Try each candidate name blindly; return first that VTS accepts."""
        for name in candidates:
            ok, _ = await self._test_injection(name, 0.0, "set")
            if ok:
                return name
        return None

    async def detect_parameter_names(self):
        hub_emit(self.signals, "vts", "probing parameter names blindly…")

        # ---- Head ----
        self._head_names = {}
        for slot, candidates in HEAD_NAME_CANDIDATES.items():
            name = await self._probe_candidates(candidates)
            if name:
                self._head_names[slot] = name

        # ---- Extra ----
        self._extra_names = {}
        for slot, candidates in EXTRA_NAME_CANDIDATES.items():
            name = await self._probe_candidates(candidates)
            if name:
                self._extra_names[slot] = name

        # ---- Mouth & Smile ----
        self._mouth_name = await self._probe_candidates(MOUTH_NAME_CANDIDATES)
        self._smile_name = await self._probe_candidates(SMILE_NAME_CANDIDATES)

        # ---- Verify injectability of every resolved param ----
        hub_emit(self.signals, "vts", "verifying injection modes…")
        failures = {}

        for slot in list((self._head_names or {}).keys()):
            name = self._head_names[slot]
            mode, err = await self._verify_param(name)
            if mode:
                self._param_modes[name] = mode
            else:
                failures[f"head.{slot}"] = f"{name} | {err}"
                del self._head_names[slot]

        for slot in list((self._extra_names or {}).keys()):
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

        if self._mouth_name:
            mode, err = await self._verify_param(self._mouth_name)
            if mode:
                self._param_modes[self._mouth_name] = mode
            else:
                failures["mouth"] = f"{self._mouth_name} | {err}"
                self._mouth_name = None
                
        if self._smile_name:
            mode, err = await self._verify_param(self._smile_name)
            if mode:
                self._param_modes[self._smile_name] = mode
            else:
                failures["smile"] = f"{self._smile_name} | {err}"
                self._smile_name = None

        ext = self._extra_names or {}
        if (ext.get("bx") == "FaceAngleX" and ext.get("by") == "FaceAngleY" and ext.get("bz") == "FaceAngleZ"):
            self._extra_names.pop("bx", None)
            self._extra_names.pop("by", None)
            self._extra_names.pop("bz", None)

        hub_emit(
            self.signals, "vts",
            f"detected — head:{self._head_names} extra:{self._extra_names} mouth:{self._mouth_name} smile:{self._smile_name}"
        )

        if not self._head_names or len(self._head_names) < 5:
            hub_emit(
                self.signals, "vts",
                "WARNING — head detection incomplete. Head motion disabled.",
                level="warn"
            )

        ext = self._extra_names or {}
        have_body = all(k in ext for k in ("bx", "by", "bz"))
        have_brows = all(k in ext for k in ("bly", "bry"))
        have_gaze = "ex" in ext and "ey" in ext
        have_arms = all(
            s in ext for s in ("la1", "la2", "la3", "la4",
                               "ra1", "ra2", "ra3", "ra4")
        )
        hub_emit(
            self.signals, "vts",
            f"motion subsystems — body:{have_body} brows:{have_brows} "
            f"gaze:{have_gaze} arms:{have_arms}"
        )
        if self._extra_names:
            modes_summary = {
                s: self._param_modes.get(n, "?")
                for s, n in self._extra_names.items()
            }
            hub_emit(self.signals, "vts",
                     f"extra param modes: {modes_summary}")

    def _resolve_hotkey(self, name):
        if not name:
            return name
        return HOTKEY_ALIASES.get(name.lower(), name)

    def _resolve_expression_target(self, role):
        if role == "smile":
            return self._smile_name
        if role == "mouth":
            return self._mouth_name
        return None

    # ---------------------------------------------------------------
    # Parameter injection
    # ---------------------------------------------------------------
    async def inject_parameters(self, parameters, mode="set"):
        """Inject a batch of params. Per-param mode resolution:
          1. explicit "_mode" key on the param dict
          2. self._param_modes lookup by param id
          3. the batch default `mode` argument

        NOTE: this MUTATES the dicts by popping "_mode". Build fresh
        lists per call if you plan to reuse them.
        """
        if not parameters:
            return
        by_mode = {}
        for p in parameters:
            m = p.pop("_mode", None)
            if m is None:
                m = self._param_modes.get(p["id"], mode)
            by_mode.setdefault(m, []).append(p)

        for m, group in by_mode.items():
            request = self.vts.vts_request.BaseRequest(
                "InjectParameterDataRequest",
                {
                    "faceFound": False,
                    "mode": m,
                    "parameterValues": [
                        {
                            "id": p["id"],
                            "value": self._clamp(p["id"], p["value"]),
                            "weight": 1.0,
                        }
                        for p in group
                    ],
                },
            )
            await self.send_request(request)

    def _clamp(self, param_id, value):
        if param_id in VTS_INPUT_RANGES:
            lo, hi = VTS_INPUT_RANGES[param_id]
        else:
            lo, hi = PARAM_RANGES.get(param_id, (-1.0, 1.0))
        return max(lo, min(hi, float(value)))

    # ---------------------------------------------------------------
    # Mouth (called by tts.py)
    # ---------------------------------------------------------------
    def set_mouth_open(self, value):
        self._target_mouth_open = max(0.0, min(1.0, float(value)))

    # ---------------------------------------------------------------
    # Keyboard hotkey path
    # ---------------------------------------------------------------
    def _trigger_via_keyboard(self, name):
        combo = HOTKEY_KEYBINDINGS.get((name or "").lower())
        if not combo:
            hub_emit(self.signals, "vts",
                     f"no keybinding for hotkey '{name}'", level="warn")
            return False
        try:
            import keyboard
        except ImportError:
            hub_emit(self.signals, "vts",
                     "keyboard library not installed — cannot simulate hotkeys",
                     level="error")
            return False
        focused = _focus_window_by_title(VTS_WINDOW_TITLE)
        if not focused:
            hub_emit(self.signals, "vts",
                     f"could not focus VTS window for '{name}'", level="warn")
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
            hub_emit(self.signals, "vts",
                     f"keyboard hotkey '{name}' → {combo} (sequenced)")
            return True
        except Exception as e:
            hub_emit(self.signals, "vts",
                     f"keyboard send failed for '{name}': {e}", level="warn")
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
    # Brow computation
    # ---------------------------------------------------------------
    def _compute_brows(self, t, speaking):
        ext = self._extra_names or {}
        brow_slots = ("bly", "bry", "blf", "brf")
        if not any(s in ext for s in brow_slots):
            return []

        now = time.time()
        if now >= self._brow_baseline_expires:
            self._brow_baseline = [0.0, 0.0, 0.0, 0.0]

        idle_y = math.sin(t * 0.17 * math.tau) * 0.03
        speech_y = (self._current_mouth_open * 0.08) if speaking else 0.0

        targets = [
            self._brow_baseline[0] + idle_y + speech_y,
            self._brow_baseline[1] + idle_y + speech_y,
            self._brow_baseline[2],
            self._brow_baseline[3],
        ]
        for i in range(4):
            self._brow_current[i] += (targets[i] - self._brow_current[i]) * 0.15

        # Dedupe by target id — on ariu all four slots point to "Brows",
        # so we send exactly one write per frame.
        by_id = {}
        by_id_count = {}
        for slot, val in zip(brow_slots, self._brow_current):
            if slot not in ext:
                continue
            pid = ext[slot]
            # "Brows" input expects 0..1; direct param names expect -1..1.
            if pid == "Brows":
                val = (val + 1.0) * 0.5
            by_id[pid] = by_id.get(pid, 0.0) + val
            by_id_count[pid] = by_id_count.get(pid, 0) + 1

        return [
            {"id": pid, "value": by_id[pid] / by_id_count[pid]}
            for pid in by_id
        ]

    # ---------------------------------------------------------------
    # Arm computation
    # ---------------------------------------------------------------
    def _compute_arms(self, t, speaking, head_x, head_z):
        ext = self._extra_names or {}
        slots = ("la1", "la2", "la3", "la4", "ra1", "ra2", "ra3", "ra4")
        if not all(s in ext for s in slots):
            return []
        d_l, d_r = self._arms.deltas(
            t, speaking, self._current_mouth_open, head_x, head_z
        )
        out = []
        for slot, delta in zip(("la1", "la2", "la3", "la4"), d_l):
            out.append({"id": ext[slot], "value": delta})
        for slot, delta in zip(("ra1", "ra2", "ra3", "ra4"), d_r):
            out.append({"id": ext[slot], "value": delta})
        return out

    # ---------------------------------------------------------------
    # Motion tick
    # ---------------------------------------------------------------
    async def _tick_motion(self, speaking):
        now = time.time()
        t = now - self._session_start

        # ---- IDLE TIMER ----
        if speaking:
            self._last_interaction_time = now
            if self._is_idle:
                self._is_idle = False
        else:
            if now - self._last_interaction_time > 5.0:
                if not self._is_idle:
                    self._is_idle = True
                    self._idle_start_time = now
            else:
                self._is_idle = False

        # ---- MOUTH ----
        if speaking:
            delta = self._target_mouth_open - self._current_mouth_open
            self._current_mouth_open += delta * 0.35
        else:
            self._current_mouth_open *= 0.75
            if self._current_mouth_open < 0.01:
                self._current_mouth_open = 0.0
        if self._mouth_name:
            try:
                await self.inject_parameters(
                    [{"id": self._mouth_name,
                      "value": self._current_mouth_open}]
                )
            except Exception:
                pass

        # ---- HEAD + BLINK (30 Hz) ----
        head_x = head_z = 0.0
        if now - self._last_head_tick >= 0.033 and self._head_names:
            hx, hy, hz = self._motion.head(t)
            
            # --- IDLE & SPEECH MODIFIERS ---
            if self._is_idle:
                # Gradually relax head sway the longer she's idle (bottoms out at 40% movement)
                idle_duration = now - self._idle_start_time
                relax_factor = max(0.4, 1.0 - (idle_duration * 0.03)) 
                hx *= relax_factor
                hy *= relax_factor
                hz *= relax_factor
            elif speaking:
                # Just scale down the regular sway slightly while talking, no jittery bobbing!
                hx *= 0.85
                hy *= 0.85
                hz *= 0.85
                
            self._motion.tick(t, hx, hy, hz)
            
            # --- BLINK & EYE RELAXATION ---
            eye_open = self._blink.value(now)
            if self._is_idle:
                # Relax her eyelids to 55% openness when idle so she doesn't look surprised
                eye_open *= 0.55 
            else:
                # Keep her eyelids slightly relaxed at 75% when she is talking/active
                eye_open *= 0.75

            params = []
            for slot, val in (("x", hx), ("y", hy), ("z", hz),
                              ("el", eye_open), ("er", eye_open)):
                if slot in self._head_names:
                    params.append({"id": self._head_names[slot], "value": val})
            if params:
                try:
                    await self.inject_parameters(params)
                except Exception:
                    pass
            head_x, head_z = hx, hz
            self._last_head_tick = now
        else:
            head_x, _, head_z = self._motion.head(t)

        # ---- BODY + GAZE + BROWS + ARMS + STICKY (20 Hz) ----
        if now - self._last_extra_tick >= 0.05:
            extra = []
            ext = self._extra_names or {}

            bx, by, bz, bw = self._motion.body(t)
            for slot, val in (("bx", bx), ("by", by), ("bz", bz), ("bw", bw)):
                if slot in ext:
                    extra.append({"id": ext[slot], "value": val})

            if "ex" in ext and "ey" in ext:
                bias_x = 0.0
                bias_y = 0.05 if speaking else 0.0
                gx, gy = self._saccades.value(
                    t, bias_x, bias_y, lively=self._is_lively, idle=self._is_idle
                )
                extra.append({"id": ext["ex"], "value": gx})
                extra.append({"id": ext["ey"], "value": gy})

            extra.extend(self._compute_brows(t, speaking))
            extra.extend(self._compute_arms(t, speaking, head_x, head_z))

            expired = []
            for name, (value, expire_at) in self._sticky_params.items():
                if now >= expire_at:
                    expired.append(name)
                else:
                    extra.append({"id": name, "value": value})
            for name in expired:
                del self._sticky_params[name]

            if extra:
                try:
                    await self.inject_parameters(extra)
                except Exception:
                    pass
            self._last_extra_tick = now

    # ---------------------------------------------------------------
    # Raw hotkey trigger
    # ---------------------------------------------------------------
    async def trigger_expression(self, hotkey_name):
        if not hotkey_name:
            return
        resolved = self._resolve_hotkey(hotkey_name)
        lookup_key = resolved.lower()

        if USE_KEYBOARD_HOTKEYS_ONLY:
            ok = self._trigger_via_keyboard(lookup_key)
            if ok:
                self._toggle_state[lookup_key] = not self._toggle_state.get(
                    lookup_key, False
                )
            else:
                hub_emit(self.signals, "vts",
                         f"hotkey '{lookup_key}' failed (keyboard path)",
                         level="warn")
            return

        hotkey_id = self._hotkey_cache.get(lookup_key)
        if not hotkey_id:
            try:
                hotkeys = await self.get_hotkeys()
                for hk in hotkeys:
                    if hk.get("name", "").lower() == lookup_key:
                        hotkey_id = hk.get("hotkeyID")
                        self._hotkey_cache[lookup_key] = hotkey_id
                        break
            except Exception:
                pass
        if not hotkey_id:
            if self._trigger_via_keyboard(lookup_key):
                self._toggle_state[lookup_key] = not self._toggle_state.get(
                    lookup_key, False
                )
                return
            hub_emit(self.signals, "vts",
                     f"hotkey '{resolved}' NOT FOUND", level="warn")
            return

        request = self.vts.vts_request.BaseRequest(
            "HotkeyTriggerRequest", {"hotkeyID": hotkey_id},
        )
        response = await self.send_request(request)
        if not response or response.get("messageType") == "APIError":
            if self._trigger_via_keyboard(lookup_key):
                self._toggle_state[lookup_key] = not self._toggle_state.get(
                    lookup_key, False
                )
                return
            hub_emit(self.signals, "vts",
                     f"API + keyboard both failed for '{resolved}'",
                     level="warn")
            return
        self._toggle_state[lookup_key] = not self._toggle_state.get(
            lookup_key, False
        )
        hub_emit(self.signals, "vts", f"fired '{resolved}' (API)")

    # ---------------------------------------------------------------
    # Emotion routing
    # ---------------------------------------------------------------
    async def set_emotion(self, emotion_tag):
        tag = (emotion_tag or "").lower().strip()
        if not tag:
            return
        brow = BROW_EXPRESSION_MAP.get(tag)
        if brow is not None:
            self._brow_baseline = list(brow)
            self._brow_baseline_expires = time.time() + EXPRESSION_DURATION
            self._is_lively = tag in {
                "excited", "surprised", "delighted",
                "angry", "laughing", "chuckling",
            }
            hub_emit(self.signals, "vts",
                     f"tag [{tag}] → brows {brow} lively={self._is_lively}")
        self._arms.apply_emotion(tag)

        hotkey = EMOTION_HOTKEY_MAP.get(tag)
        if hotkey:
            hub_emit(self.signals, "vts",
                     f"tag [{tag}] → hotkey '{hotkey}'")
            await self.trigger_expression(hotkey)
            return

        roles = PARAMETER_EXPRESSION_MAP.get(tag)
        if roles:
            now = time.time()
            applied = []
            for role, value in roles:
                actual = self._resolve_expression_target(role)
                if not actual:
                    continue
                self._sticky_params[actual] = (value, now + EXPRESSION_DURATION)
                applied.append(f"{role}={value}")
            if applied:
                hub_emit(self.signals, "vts",
                         f"tag [{tag}] → {applied}")
            else:
                hub_emit(self.signals, "vts",
                         f"tag [{tag}] had no resolvable target",
                         level="warn")
            return

        if brow is None:
            hub_emit(self.signals, "vts",
                     f"tag [{tag}] has no mapping (skipped)")

    async def clear_expression(self):
        self._sticky_params.clear()
        self._brow_baseline = [0.0, 0.0, 0.0, 0.0]
        self._brow_baseline_expires = 0.0
        self._is_lively = False
        self._arms.set_emotion_lean(0.0)
        hub_emit(self.signals, "vts", "expression cleared")

    # ---------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------
    async def get_hotkeys(self):
        response = await self.send_request(
            self.vts.vts_request.requestHotKeyList()
        )
        return response.get("data", {}).get("availableHotkeys", [])

    async def send_hotkey(self, hotkey):
        request = self.vts.vts_request.requestTriggerHotKey(hotkey)
        await self.send_request(request)

    async def get_item_list(self):
        request = self.vts.vts_request.BaseRequest(
            "ItemListRequest",
            {
                "includeAvailableSpots": False,
                "includeItemInstancesInScene": False,
                "includeAvailableItemFiles": True,
            },
        )
        response = (await self.send_request(request))["data"]
        return [item["fileName"] for item in response["availableItemFiles"]]

    async def spawn_microphone(self):
        if "microphone_red (@7MDigital).png" not in self.item_list:
            self.signals.sio_queue.put(
                ("error", "Microphone item not found in Vtube Studio")
            )
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
        self.prop_instance_ids["microphone"] = (
            await self.send_request(request)
        )["data"]["instanceID"]
        request = self.vts.vts_request.BaseRequest(
            "ItemMoveRequest",
            {
                "itemsToMove": [
                    {
                        "itemInstanceID": self.prop_instance_ids["microphone"],
                        "timeInSeconds": 0.6,
                        "fadeMode": "zip",
                        "positionX": VTUBE_MIC_POSITION["x"],
                        "positionY": VTUBE_MIC_POSITION["y"],
                    }
                ]
            },
        )
        await self.send_request(request)

    async def despawn_microphone(self):
        if "microphone" in self.prop_instance_ids:
            request = self.vts.vts_request.BaseRequest(
                "ItemMoveRequest",
                {
                    "itemsToMove": [
                        {
                            "itemInstanceID": self.prop_instance_ids["microphone"],
                            "timeInSeconds": 0.6,
                            "fadeMode": "easeBoth",
                            "positionX": VTUBE_MIC_POSITION["x"],
                            "positionY": VTUBE_MIC_POSITION["y"] - 1,
                        }
                    ]
                },
            )
            await self.send_request(request)
        await asyncio.sleep(0.6)
        request = self.vts.vts_request.BaseRequest(
            "ItemUnloadRequest",
            {"fileNames": ["microphone_red (@7MDigital).png"]},
        )
        await self.send_request(request)

    async def move_model(self, mode):
        if mode not in VTUBE_MODEL_POSITIONS:
            self.signals.sio_queue.put(
                ("error", "Invalid model location: " + mode)
            )
            return
        request = self.vts.vts_request.BaseRequest(
            "MoveModelRequest",
            {
                "timeInSeconds": 0.8,
                "valuesAreRelativeToModel": False,
                "positionX": VTUBE_MODEL_POSITIONS[mode]["x"],
                "positionY": VTUBE_MODEL_POSITIONS[mode]["y"],
                "rotation":  VTUBE_MODEL_POSITIONS[mode]["rotation"],
                "size":      VTUBE_MODEL_POSITIONS[mode]["size"],
            },
        )
        await self.send_request(request)

    async def send_request(self, request):
        response = await self.vts.request(request)
        if response.get("messageType") == "APIError":
            msg = response.get("data", {}).get("message", "unknown")
            self.signals.sio_queue.put(
                ("error", "Vtube Studio API Error: " + msg)
            )
            return {}
        return response

    # ---------------------------------------------------------------
    # Main run loop
    # ---------------------------------------------------------------
    async def run(self):
        if not self.enabled:
            return
        print("\033[90mVTube Studio: connecting...\033[0m")
        if not await self.connect_and_auth():
            print("\033[91mVTube Studio: disabling module — "
                  "connection failed\033[0m")
            self.enabled = False
            return
        print("\033[92mVTube Studio: connected and authenticated\033[0m")
        self.item_list = await self.get_item_list()
        await self.load_hotkey_cache()
        await self.detect_parameter_names()

        self.signals.vts_ready = True
        self._session_start = time.time()
        motion_interval = 1.0 / MOTION_HZ

        while not self.signals.terminate:
            if not self.enabled:
                await asyncio.sleep(0.1)
                continue
            try:
                if not self.queue.empty():
                    action = self.queue.get()
                    await self._handle_action(action)
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
            await asyncio.sleep(0.005)

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
                hub_emit(self.signals, "vts",
                         f"set_expression '{state_name}' rejected: {err}",
                         level="warn")
            else:
                hub_emit(self.signals, "vts",
                         f"set_expression '{state_name}' → "
                         f"{'on' if actual else 'off'}"
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
                self.outer.signals.sio_queue.put(
                    ("error", "Vtube Studio Module is disabled")
                )
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
            self.outer.queue.put(self.outer.Action(
                "set_expression",
                {"name": name, "enabled": bool(enabled)},
            ))

        def list_expression_states(self):
            return self.outer.list_expression_states()

        def list_expression_names(self):
            return self.outer.list_expression_names()

        def resync_expression_states(self, states):
            if not self.outer.enabled:
                return
            self.outer.queue.put(self.outer.Action(
                "resync_expression_states", states or {}
            ))

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

        def inject_parameters(self, parameters, mode="set"):
            if not self.outer.enabled:
                return
            self.outer.queue.put(
                self.outer.Action("inject_parameters", parameters)
            )
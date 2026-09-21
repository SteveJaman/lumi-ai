"""
VTube Studio controller.

Drives the Live2D model via the VTS plugin API using pyvts.

Responsibilities:
  - Connect + authenticate on startup
  - Probe the loaded model to discover its real parameter names
  - Drive continuous organic motion (head sway, body lean, blink, breath)
  - Drive mouth-open from TTS volume envelope (via tts.py)
  - Fire hotkey-based expressions on emotion tags from prompter.py
  - Expose a state-aware expression API so Lumi can request
    "put on hat" / "take off jacket" and get deterministic results

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

set_expression_state() consults the map, checks the current toggle
state, and fires the key only if a change is needed. Idempotent.

Hotkey firing paths
-------------------
1. API path — HotkeyTriggerRequest over the websocket. Broken on
   ariu for ToggleExpression hotkeys (VTS silently accepts, does
   nothing). Kept for models where it works.
2. Keyboard path — simulate the bound key combo after focusing VTS.
   Works reliably. Set USE_KEYBOARD_HOTKEYS_ONLY=True to always use
   it.
"""

import asyncio
import math
import queue
import time
import random
import pyvts
import logging
import ctypes

from modules.module import Module
from constants import VTUBE_MODEL_POSITIONS, VTUBE_MIC_POSITION
from modules.log_hub import emit as hub_emit


logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("websockets.legacy").setLevel(logging.WARNING)
logging.getLogger("websockets.legacy.protocol").setLevel(logging.WARNING)


# ===============================================================
# HOTKEY ALIASES
# ===============================================================
HOTKEY_ALIASES = {}


# ===============================================================
# KEYBOARD FALLBACK HOTKEYS
# ---------------------------------------------------------------
# Matches the "Triggers" block for each hotkey in ariu.vtube.json.
# If you swap models, update this map.
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

# If True, skip the VTS API hotkey call entirely and always use
# keyboard simulation.
USE_KEYBOARD_HOTKEYS_ONLY = True

# Window title substring to focus before sending keys.
VTS_WINDOW_TITLE = "VTube Studio"


# ===============================================================
# EXPRESSION STATE MAP
# ---------------------------------------------------------------
# Maps the STATE name Lumi uses to the underlying toggle hotkey.
# sense = +1 means "hotkey on" == "state on"
# sense = -1 means "hotkey on" == "state OFF" (inverted naming)
# ===============================================================
EXPRESSION_STATE_MAP = {
    # STATE NAME       →  (hotkey_name,        sense)
    "hat":             ("hat",                +1),  # on = hat is worn
    "jacket":          ("remove_jacket",      -1),  # on = jacket is OFF
    "bag":             ("jk_bag",             +1),
    "skirt":           ("skirt",              +1),
    "ponytail_l":      ("hide_ponytail_l",    -1),  # on = ponytail hidden
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
    """Bring a window whose title contains `title_substring` to the
    foreground. Uses AttachThreadInput to bypass the Windows foreground
    lock. Verifies the focus actually moved before returning."""
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

        # Verify — sometimes even AttachThreadInput silently fails.
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
# PARAMETER NAME CANDIDATES
# ===============================================================
HEAD_NAME_CANDIDATES = {
    "x":   ["FaceAngleX",    "ParamAngleX", "AngleX"],
    "y":   ["FaceAngleY",    "ParamAngleY", "AngleY"],
    "z":   ["FaceAngleZ",    "ParamAngleZ", "AngleZ"],
    "el":  ["EyeOpenLeft",   "ParamEyeLOpen", "EyeLOpen"],
    "er":  ["EyeOpenRight",  "ParamEyeROpen", "EyeROpen"],
}

EXTRA_NAME_CANDIDATES = {
    "ex":  ["EyeRightX", "EyeLeftX", "ParamEyeBallX", "EyeBallX"],
    "ey":  ["EyeRightY", "EyeLeftY", "ParamEyeBallY", "EyeBallY"],
    # "br" removed — VTS auto-drives ParamBreath when UseBreathing:true.
}

MOUTH_NAME_CANDIDATES = ["MouthOpen", "ParamMouthOpenY"]
SMILE_NAME_CANDIDATES = ["MouthSmile", "ParamMouthForm", "MouthForm"]


# ===============================================================
# TUNING
# ===============================================================
EXPRESSION_DURATION = 6.0
MOTION_HZ = 30


PARAM_RANGES = {
    "MouthOpen":       (0.0, 1.0),
    "ParamMouthOpenY": (0.0, 2.1),
    "MouthSmile":      (-1.0, 1.0),
    "MouthForm":       (-1.0, 1.0),
    "ParamMouthForm":  (-1.0, 1.0),

    "FaceAngleX":      (-30.0, 30.0),
    "FaceAngleY":      (-30.0, 30.0),
    "FaceAngleZ":      (-30.0, 30.0),
    "ParamAngleX":     (-30.0, 30.0),
    "ParamAngleY":     (-30.0, 30.0),
    "ParamAngleZ":     (-30.0, 30.0),

    "EyeOpenLeft":     (0.0, 1.0),
    "EyeOpenRight":    (0.0, 1.0),
    "ParamEyeLOpen":   (0.0, 1.0),
    "ParamEyeROpen":   (0.0, 1.0),
    "EyeLeftX":        (-1.0, 1.0),
    "EyeLeftY":        (-1.0, 1.0),
    "EyeRightX":       (-1.0, 1.0),
    "EyeRightY":       (-1.0, 1.0),
    "EyeBallX":        (-1.0, 1.0),
    "EyeBallY":        (-1.0, 1.0),
    "ParamEyeBallX":   (-1.0, 1.0),
    "ParamEyeBallY":   (-1.0, 1.0),

    "Breath":          (0.0, 1.0),
    "ParamBreath":     (0.0, 1.0),
}


# ===============================================================
# ORGANIC MOTION
# ===============================================================
class _OrganicMotion:
    def __init__(self):
        self._phases = {
            "hx": [random.uniform(0, math.tau) for _ in range(3)],
            "hy": [random.uniform(0, math.tau) for _ in range(3)],
            "hz": [random.uniform(0, math.tau) for _ in range(3)],
            "ex": [random.uniform(0, math.tau) for _ in range(2)],
            "ey": [random.uniform(0, math.tau) for _ in range(2)],
        }
        self._freqs = {
            "hx": [0.05, 0.11, 0.19],
            "hy": [0.04, 0.09, 0.23],
            "hz": [0.06, 0.13, 0.27],
            "ex": [0.17, 0.31],
            "ey": [0.13, 0.29],
        }

    def _combine(self, axis, t):
        phases = self._phases[axis]
        freqs = self._freqs[axis]
        s = sum(
            math.sin(t * f * math.tau + p)
            for f, p in zip(freqs, phases)
        )
        return s / len(freqs)

    def head(self, t):
        return (
            self._combine("hx", t) * 6.0,
            self._combine("hy", t) * 5.0,
            self._combine("hz", t) * 3.0,
        )

    def eyes(self, t):
        return (
            self._combine("ex", t) * 0.5,
            self._combine("ey", t) * 0.5,
        )


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

        # Expression toggle state — keyed by raw hotkey name, value
        # is True when that hotkey's toggle is currently ON. Populated
        # by set_expression_state(); can go stale if the user presses
        # the key manually in VTS.
        self._toggle_state = {}

        # Motion + blink
        self._motion = _OrganicMotion()
        self._blink = _BlinkScheduler()
        self._session_start = time.time()
        self._last_motion_tick = 0.0
        self._last_head_tick = 0.0
        self._last_extra_tick = 0.0

        # Sticky expressions (parameter-driven, not hotkey-driven)
        self._sticky_params = {}

        # Mouth
        self._current_mouth_open = 0.0
        self._target_mouth_open = 0.0

        # Detected parameter names
        self._head_names = None
        self._extra_names = None
        self._mouth_name = None
        self._smile_name = None

        plugin_info = {
            "plugin_name": "Lumi VTS Plugin",
            "developer": "Lumi AI",
            "authentication_token_path": "./vtubeStudio_token.txt",
        }
        self.vts = pyvts.vts(plugin_info=plugin_info)

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
            return False

        try:
            await self.vts.request_authenticate_token()
            await self.vts.request_authenticate()
        except Exception as e:
            print(f"\033[91mVTube Studio: auth failed — {e}\033[0m")
            hub_emit(self.signals, "vts",
                     f"auth failed: {e}", level="error")
            try:
                await self.vts.close()
            except Exception:
                pass
            return False

        hub_emit(self.signals, "vts", "connected and authenticated")
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
    # Parameter name detection
    # ---------------------------------------------------------------
    async def _fetch_live2d_params(self):
        try:
            resp = await self.vts.request({
                "apiName": "VTubeStudioPublicAPI",
                "apiVersion": "1.0",
                "requestID": "LumiParamList",
                "messageType": "Live2DParameterListRequest",
                "data": {},
            })
        except Exception as e:
            hub_emit(self.signals, "vts",
                     f"Live2DParameterListRequest failed: {e}",
                     level="warn")
            return set()

        if not resp or resp.get("messageType") == "APIError":
            return set()

        names = set()
        for p in resp.get("data", {}).get("parameters", []):
            n = p.get("name")
            if n:
                names.add(n)
        return names

    async def _try_inject_raw(self, parameters):
        try:
            request = self.vts.vts_request.BaseRequest(
                "InjectParameterDataRequest",
                {
                    "faceFound": False,
                    "mode": "set",
                    "parameterValues": [
                        {
                            "id": p["id"],
                            "value": self._clamp(p["id"], p["value"]),
                            "weight": 1.0,
                        }
                        for p in parameters
                    ],
                },
            )
            response = await self.vts.request(request)
            if not response:
                return False
            if response.get("messageType") == "APIError":
                return False
            return True
        except Exception:
            return False

    async def _probe_names(self, candidates_dict, available_names=None,
                           required_slots=None):
        result = {}
        if required_slots is None:
            required_slots = set(candidates_dict.keys())

        for slot, names in candidates_dict.items():
            ordered = names
            if available_names:
                preferred = [n for n in names if n in available_names]
                rest = [n for n in names if n not in available_names]
                ordered = preferred + rest if preferred else rest

            for name in ordered:
                if await self._try_inject_raw([{"id": name, "value": 0.0}]):
                    result[slot] = name
                    break

            if slot not in result and slot in required_slots:
                hub_emit(
                    self.signals, "vts",
                    f"no working name for slot '{slot}' "
                    f"(tried: {names})",
                    level="warn"
                )
        return result

    async def detect_parameter_names(self):
        hub_emit(self.signals, "vts", "probing parameter names…")

        available = await self._fetch_live2d_params()
        if available:
            related = sorted(
                n for n in available
                if "Body" in n or "Angle" in n or "Breath" in n
                or "Eye" in n or "Mouth" in n
            )
            hub_emit(
                self.signals, "vts",
                f"model exposes {len(available)} Live2D params — "
                f"relevant: {related[:20]}"
            )
        else:
            hub_emit(self.signals, "vts",
                     "could not read model param list — probing blind",
                     level="warn")

        self._head_names  = await self._probe_names(
            HEAD_NAME_CANDIDATES, available,
            required_slots={"x", "y", "z", "el", "er"},
        )
        self._extra_names = await self._probe_names(
            EXTRA_NAME_CANDIDATES, available,
            required_slots={"ex", "ey"},
        )

        for name in MOUTH_NAME_CANDIDATES:
            if await self._try_inject_raw([{"id": name, "value": 0.0}]):
                self._mouth_name = name
                break

        for name in SMILE_NAME_CANDIDATES:
            if await self._try_inject_raw([{"id": name, "value": 0.0}]):
                self._smile_name = name
                break

        hub_emit(
            self.signals, "vts",
            f"detected — head:{self._head_names} "
            f"extra:{self._extra_names} mouth:{self._mouth_name} "
            f"smile:{self._smile_name}"
        )

        if not self._head_names or len(self._head_names) < 5:
            hub_emit(
                self.signals, "vts",
                "WARNING — head detection incomplete. Head motion disabled.",
                level="warn"
            )

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
        if not parameters:
            return

        request = self.vts.vts_request.BaseRequest(
            "InjectParameterDataRequest",
            {
                "faceFound": False,
                "mode": mode,
                "parameterValues": [
                    {
                        "id": p["id"],
                        "value": self._clamp(p["id"], p["value"]),
                        "weight": 1.0,
                    }
                    for p in parameters
                ],
            },
        )
        await self.send_request(request)

    def _clamp(self, param_id, value):
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
                     f"no keybinding for hotkey '{name}'",
                     level="warn")
            return False
        try:
            import keyboard
        except ImportError:
            hub_emit(self.signals, "vts",
                     "keyboard library not installed — "
                     "cannot simulate hotkeys", level="error")
            return False

        focused = _focus_window_by_title(VTS_WINDOW_TITLE)
        if not focused:
            hub_emit(self.signals, "vts",
                     f"could not focus VTS window for '{name}'",
                     level="warn")
            return False

        # Give Windows a beat to finish routing input to the newly
        # focused window. Too short and the first key press is dropped.
        time.sleep(0.15)

        try:
            parts = combo.lower().split("+")
            key = parts[-1]
            modifiers = parts[:-1]

            # Press all modifiers, small gap, tap the key, small gap,
            # release modifiers in reverse. This mimics a real
            # keypress sequence closely enough that even polling
            # input loops catch it.
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
                     f"keyboard send failed for '{name}': {e}",
                     level="warn")
            return False

    # ---------------------------------------------------------------
    # State-aware expression API
    # ---------------------------------------------------------------
    def set_expression_state(self, state_name, enabled):
        """Set a state to on/off. Idempotent — no-op if already there.

        Args:
            state_name: user-facing state name from EXPRESSION_STATE_MAP
            enabled: True to turn on, False to turn off

        Returns:
            (changed, actual_state, error_message)
            error_message is None on success.
        """
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
        """Return {state_name: bool} for every state in the map."""
        result = {}
        for state_name, (hotkey_name, sense) in EXPRESSION_STATE_MAP.items():
            raw = self._toggle_state.get(hotkey_name, False)
            result[state_name] = raw if sense == +1 else (not raw)
        return result

    def list_expression_names(self):
        return list(EXPRESSION_STATE_MAP.keys())

    def resync_expression_states(self, states):
        """Manually override the internal toggle state. Use this after
        changing expressions by hand in VTS to keep the module in sync.
        `states` is a dict of {state_name: bool}."""
        for state_name, is_on in states.items():
            key = (state_name or "").strip().lower()
            if key not in EXPRESSION_STATE_MAP:
                continue
            hotkey_name, sense = EXPRESSION_STATE_MAP[key]
            raw_state = bool(is_on) if sense == +1 else (not bool(is_on))
            self._toggle_state[hotkey_name] = raw_state

    # ---------------------------------------------------------------
    # Motion tick
    # ---------------------------------------------------------------
    async def _tick_motion(self, speaking):
        now = time.time()
        t = now - self._session_start

        # ---- 1. MOUTH ----
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

        # ---- 2. HEAD + BLINK ----
        if now - self._last_head_tick >= 0.05 and self._head_names:
            hx, hy, hz = self._motion.head(t)
            if speaking:
                hx *= 0.4
                hy *= 0.4
                hz *= 0.4
            eye_open = self._blink.value(now)

            params = []
            if "x" in self._head_names:
                params.append({"id": self._head_names["x"], "value": hx})
            if "y" in self._head_names:
                params.append({"id": self._head_names["y"], "value": hy})
            if "z" in self._head_names:
                params.append({"id": self._head_names["z"], "value": hz})
            if "el" in self._head_names:
                params.append({"id": self._head_names["el"], "value": eye_open})
            if "er" in self._head_names:
                params.append({"id": self._head_names["er"], "value": eye_open})

            if params:
                try:
                    await self.inject_parameters(params)
                except Exception:
                    pass

            self._last_head_tick = now

        # ---- 3. EXTRA + STICKY EXPRESSIONS ----
        if now - self._last_extra_tick >= 0.2:
            extra = []

            if self._extra_names:
                if "ex" in self._extra_names:
                    ex, _ = self._motion.eyes(t)
                    extra.append({"id": self._extra_names["ex"], "value": ex})
                if "ey" in self._extra_names:
                    _, ey = self._motion.eyes(t)
                    extra.append({"id": self._extra_names["ey"], "value": ey})

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
    # Raw hotkey trigger (kept for emotion tags and legacy API)
    # ---------------------------------------------------------------
    async def trigger_expression(self, hotkey_name):
        """Fire a raw hotkey. Used by the passive emotion path and by
        the raw API — bypasses state tracking on purpose, because
        emotion tags don't care about current state."""
        if not hotkey_name:
            return

        resolved = self._resolve_hotkey(hotkey_name)
        lookup_key = resolved.lower()

        if USE_KEYBOARD_HOTKEYS_ONLY:
            ok = self._trigger_via_keyboard(lookup_key)
            if ok:
                # Flip the tracked state so subsequent set_expression
                # calls stay in sync.
                self._toggle_state[lookup_key] = not self._toggle_state.get(lookup_key, False)
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
                self._toggle_state[lookup_key] = not self._toggle_state.get(lookup_key, False)
                return
            hub_emit(self.signals, "vts",
                     f"hotkey '{resolved}' NOT FOUND "
                     f"(from alias '{hotkey_name}')",
                     level="warn")
            return

        request = self.vts.vts_request.BaseRequest(
            "HotkeyTriggerRequest", {"hotkeyID": hotkey_id},
        )
        response = await self.send_request(request)

        if not response or response.get("messageType") == "APIError":
            if self._trigger_via_keyboard(lookup_key):
                self._toggle_state[lookup_key] = not self._toggle_state.get(lookup_key, False)
                return
            hub_emit(self.signals, "vts",
                     f"API + keyboard both failed for '{resolved}'",
                     level="warn")
            return

        self._toggle_state[lookup_key] = not self._toggle_state.get(lookup_key, False)
        hub_emit(self.signals, "vts", f"fired '{resolved}' (API)")

    async def set_emotion(self, emotion_tag):
        tag = (emotion_tag or "").lower()
        if not tag:
            return

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

        hub_emit(self.signals, "vts",
                 f"tag [{tag}] has no mapping (skipped)")

    async def clear_expression(self):
        self._sticky_params.clear()
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
            print(
                "\033[91mVTube Studio: disabling module — connection "
                "failed\033[0m"
            )
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
                except Exception:
                    pass
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

        # --- Lifecycle ---
        def set_movement_status(self, status):
            self.outer.enabled = status
            self.outer.signals.sio_queue.put(("movement_status", status))
            if status:
                while not self.outer.queue.empty():
                    self.outer.queue.get()

        def get_movement_status(self):
            return self.outer.enabled

        # --- Raw hotkeys (emotion path + legacy) ---
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
            """Raw toggle trigger. Does NOT track state — prefer
            set_expression for user-facing calls."""
            if not self.outer.enabled:
                return
            self.outer.queue.put(
                self.outer.Action("trigger_expression", name)
            )

        def set_emotion(self, emotion_tag):
            if not self.outer.enabled:
                return
            self.outer.queue.put(
                self.outer.Action("set_emotion", emotion_tag)
            )

        def clear_expression(self):
            if not self.outer.enabled:
                return
            self.outer.queue.put(
                self.outer.Action("clear_expression", None)
            )

        # --- State-aware expressions ---
        def set_expression(self, name, enabled):
            """Idempotent state change. If already in the requested state,
            the module no-ops."""
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

        # --- Model position ---
        def move_model(self, mode):
            if not self.outer.enabled:
                return
            self.outer.queue.put(self.outer.Action("move_model", mode))

        def list_positions(self):
            return list(VTUBE_MODEL_POSITIONS.keys())

        def list_hotkeys(self):
            return sorted(self.outer._hotkey_cache.keys())

        # --- Props ---
        def trigger_prop(self, prop_action):
            if not self.outer.enabled:
                return
            self.outer.queue.put(self.outer.Action(prop_action, None))

        # --- Mouth (used by tts.py) ---
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
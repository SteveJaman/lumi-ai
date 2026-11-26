"""
Motion engine for Lumi's VTube Studio avatar: everything that decides
*what* the face does, with no VTS connection. modules/vtubeStudio.py owns
the socket and calls into this every frame. Pure Python (numpy only for
lip-sync analysis), deterministic with a seed, and unit-tested in
tests/test_vts_motion.py.

Layers, bottom to top (each frame):

    HeadSway        layered sines + Perlin fBm, plus fast low-amplitude
                    micro-movements: never quite repeats
    GazeController  modes: idle drift, attentive saccades, speaking
                    (camera with occasional off-screen glances), thinking
                    (drifts up), camera (fourth-wall lock), reading (fast
                    left-to-right sweeps, line by line)
    BlinkScheduler  normal blinks, 50 ms micro-blinks, double-blinks,
                    slower and heavier when sleepy
    Breath          idle cycle; exhales through long sentences, inhales
                    quickly in pauses; slower when sleepy
    EmotionState    continuous emotion (VAD + face channels): ramps in over
                    300 ms, holds for the sentence, decays over 5 s
    MouthDriver     viseme targets from TTS; settles to neutral over 200 ms
    PoseBlender     agent-pipeline poses (thinking / approval / focused /
                    error / idle) blended smoothly as param offsets
    YawnScheduler   past midnight and idle 5 min: occasional yawns
    FrameLimiter    caps parameter updates (60 FPS)
"""

from __future__ import annotations

import math
import random
import re
import time
from typing import Dict, Iterable, List, Optional, Tuple

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:          # lip-sync falls back to volume only
    np = None
    HAS_NUMPY = False


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def smoothstep(k: float) -> float:
    k = _clamp(k, 0.0, 1.0)
    return k * k * (3.0 - 2.0 * k)


def approach(cur: float, target: float, dt: float, tau: float) -> float:
    """Frame-rate independent exponential smoothing (time constant tau)."""
    if tau <= 0:
        return target
    return cur + (target - cur) * (1.0 - math.exp(-dt / tau))


# ─── noise ────────────────────────────────────────────────────────────

class Perlin1D:
    """Classic 1-D gradient noise. Output in [-1, 1], smooth (C2)."""

    def __init__(self, seed: Optional[int] = None):
        rng = random.Random(seed)
        self._grad = [rng.uniform(-1.0, 1.0) for _ in range(256)]

    def __call__(self, x: float) -> float:
        i = math.floor(x)
        f = x - i
        g0 = self._grad[i & 255]
        g1 = self._grad[(i + 1) & 255]
        u = f * f * f * (f * (f * 6.0 - 15.0) + 10.0)
        n0, n1 = g0 * f, g1 * (f - 1.0)
        return 2.0 * (n0 + u * (n1 - n0))

    def fbm(self, x: float, octaves: int = 3) -> float:
        total, amp, norm, freq = 0.0, 1.0, 0.0, 1.0
        for _ in range(octaves):
            total += amp * self(x * freq)
            norm += amp
            amp *= 0.5
            freq *= 2.0
        return total / norm


# ─── head ─────────────────────────────────────────────────────────────

class HeadSway:
    """Head angles in degrees: a slow layered-sine base, Perlin fBm for
    unpredictable drift, and quick micro-movements on top."""

    AMPLITUDE = (6.5, 5.5, 3.5)          # x (turn), y (nod), z (tilt)

    def __init__(self, seed: Optional[int] = None):
        rng = random.Random(seed)
        tau = math.tau
        self._sines = [
            [(rng.uniform(0, tau), f) for f in (0.05, 0.11, 0.19)],
            [(rng.uniform(0, tau), f) for f in (0.04, 0.09, 0.23)],
            [(rng.uniform(0, tau), f) for f in (0.06, 0.13, 0.27)],
        ]
        self._noise = [Perlin1D(rng.randrange(1 << 30)) for _ in range(3)]
        self._micro = [Perlin1D(rng.randrange(1 << 30)) for _ in range(3)]
        self._offset = rng.uniform(0, 1000)

    def sample(self, t: float, amount: float = 1.0, micro: float = 1.0) -> Tuple[float, float, float]:
        out = []
        for axis in range(3):
            sines = sum(math.sin(t * f * math.tau + p) for p, f in self._sines[axis]) / 3.0
            drift = self._noise[axis].fbm((t + self._offset) * 0.22, 3)
            jitter = self._micro[axis]((t + self._offset) * 1.7) * 0.35     # degrees
            out.append((sines * 0.55 + drift * 0.6) * self.AMPLITUDE[axis] * amount + jitter * micro)
        return out[0], out[1], out[2]


# ─── eyes ─────────────────────────────────────────────────────────────

class BlinkScheduler:
    """Eye openness multiplier, 1 = open. Normal blinks (~230 ms),
    micro-blinks (50 ms) and double-blinks."""

    NORMAL = (0.07, 0.05, 0.11)          # close, hold, open
    MICRO = (0.02, 0.01, 0.02)           # 50 ms total
    MICRO_P = 0.22
    DOUBLE_P = 0.18

    def __init__(self, rng: Optional[random.Random] = None):
        self._rng = rng or random.Random()
        self._next_at: Optional[float] = None
        self._start = -10.0
        self._phases = self.NORMAL
        self._double_pending = False

    def _schedule(self, t: float, rate: float) -> None:
        self._next_at = t + self._rng.uniform(2.5, 6.5) / max(0.2, rate)

    def value(self, t: float, rate: float = 1.0, sleepy: bool = False) -> float:
        if self._next_at is None:
            self._schedule(t, rate)
        if t >= self._next_at:
            self._start = t
            if self._double_pending:
                self._phases = tuple(p * 2.2 for p in self.NORMAL) if sleepy else self.NORMAL
                self._double_pending = False
                self._schedule(t, rate)
            else:
                self._phases = self.MICRO if self._rng.random() < self.MICRO_P and not sleepy else self.NORMAL
                if sleepy:
                    self._phases = tuple(p * 2.2 for p in self.NORMAL)
                if self._rng.random() < self.DOUBLE_P and self._phases is not self.MICRO:
                    self._double_pending = True
                    self._next_at = t + sum(self._phases) + self._rng.uniform(0.08, 0.18)
                else:
                    self._schedule(t, rate)
        close, hold, opening = self._phases
        e = t - self._start
        if e < 0 or e > close + hold + opening:
            return 1.0
        if e < close:
            return 1.0 - e / close
        if e < close + hold:
            return 0.0
        return (e - close - hold) / opening


GAZE_MODES = ("idle", "attentive", "speaking", "thinking", "camera", "reading")


class GazeController:
    """Eye direction in [-1, 1] (x right, y up). Each mode picks targets
    differently; moves are eased saccades (fast) or drifts (slow)."""

    SNAP_S = 0.07
    SOFT_S = 0.45

    def __init__(self, rng: Optional[random.Random] = None):
        self._rng = rng or random.Random()
        self.x = self.y = 0.0
        self._sx = self._sy = self._tx = self._ty = 0.0
        self._start = 0.0
        self._dur = self.SNAP_S
        self._next_at: Optional[float] = None
        self._mode = "idle"
        self._read_col = 0
        self._read_line = 0
        self._glancing = False

    def _move(self, t: float, x: float, y: float, dur: float, hold: float) -> None:
        self._sx, self._sy = self.x, self.y
        self._tx, self._ty = _clamp(x, -1.0, 1.0), _clamp(y, -1.0, 1.0)
        self._start, self._dur = t, dur
        self._next_at = t + max(dur, hold)

    def _pick(self, t: float, lively: bool) -> None:
        r, u = self._rng.random(), self._rng.uniform
        m = self._mode
        if m == "camera":
            self._move(t, u(-0.03, 0.03), u(-0.02, 0.03), self.SNAP_S, u(0.6, 1.6))
        elif m == "speaking":
            if self._glancing or r > 0.22:
                # Back to the camera, with tiny conversational saccades.
                self._glancing = False
                self._move(t, u(-0.08, 0.08), 0.04 + u(-0.05, 0.05), self.SNAP_S, u(0.5, 1.8))
            else:
                # A glance off-screen while gathering a thought.
                self._glancing = True
                self._move(t, self._rng.choice((-1, 1)) * u(0.35, 0.6), u(-0.1, 0.3), self.SNAP_S * 1.5, u(0.4, 1.2))
        elif m == "thinking":
            self._move(t, u(-0.45, 0.45), u(0.5, 0.85), self.SOFT_S * 2.2, u(1.0, 2.6))
        elif m == "reading":
            cols = 5
            if self._read_col >= cols:
                self._read_col = 0
                self._read_line = (self._read_line + 1) % 6
                # Return sweep to the start of the next line.
                self._move(t, -0.45, 0.15 - self._read_line * 0.08, self.SNAP_S, 0.12)
            else:
                x = -0.45 + 0.9 * self._read_col / (cols - 1)
                self._read_col += 1
                self._move(t, x + u(-0.04, 0.04), 0.15 - self._read_line * 0.08, 0.04, u(0.11, 0.2))
        elif m == "attentive":
            if r < 0.45:
                mx, my, dur, hold = 0.12, 0.10, self.SNAP_S, u(0.35, 1.4)
            elif r < 0.85:
                mx, my, dur, hold = 0.45, 0.35, self.SNAP_S, u(0.45, 1.6)
            else:
                mx, my, dur, hold = 0.85, 0.6, self.SOFT_S, u(0.6, 2.2)
            self._move(t, u(-mx, mx), u(-my, my), dur, hold * (0.7 if lively else 1.0))
        else:  # idle: bored, slow drifting attention
            if r < 0.4:
                self._move(t, u(-0.3, 0.3), u(-0.2, 0.2), self.SOFT_S * 1.5, u(1.0, 3.5))
            else:
                self._move(t, u(-0.85, 0.85), u(-0.7, 0.7), self.SOFT_S * 2.5, u(2.5, 6.0))

    def value(self, t: float, mode: str = "idle", lively: bool = False) -> Tuple[float, float]:
        mode = mode if mode in GAZE_MODES else "idle"
        if mode != self._mode:
            self._mode = mode
            self._glancing = False
            self._read_col = 0
            self._next_at = None              # react at once to a new mode
        if self._next_at is None or t >= self._next_at:
            self._pick(t, lively)
        k = (t - self._start) / self._dur if self._dur > 0 else 1.0
        e = 1.0 - (1.0 - _clamp(k, 0.0, 1.0)) ** 3
        self.x = self._sx + (self._tx - self._sx) * e
        self.y = self._sy + (self._ty - self._sy) * e
        return self.x, self.y


# ─── breathing ────────────────────────────────────────────────────────

class Breath:
    """Breath in [0, 1] (1 = full inhale). Idle: a calm cycle. Speaking:
    a slow exhale that runs faster for louder speech. A pause in speech
    (or its end) triggers a quick inhale, then the idle cycle resumes."""

    IDLE_HZ = 0.25
    SLEEPY_HZ = 0.13
    PAUSE_S = 0.3            # quiet this long mid-speech counts as a pause

    def __init__(self):
        self.value = 0.5
        self._phase = 0.0
        self._mode = "idle"      # idle | exhale | inhale
        self._quiet = 0.0

    def update(self, dt: float, speaking: bool, intensity: float = 0.0, sleepy: bool = False) -> float:
        dt = _clamp(dt, 0.0, 0.25)
        if speaking:
            self._quiet = self._quiet + dt if intensity < 0.05 else 0.0
            if self._mode == "inhale" and self.value < 0.95:
                pass                                   # finish the breath in
            elif self._quiet >= self.PAUSE_S:
                self._mode = "inhale"
            else:
                self._mode = "exhale"
        elif self._mode == "exhale":
            self._mode = "inhale"

        if self._mode == "exhale":
            self.value = max(0.05, self.value - dt * (0.05 + 0.12 * _clamp(intensity, 0.0, 1.0)))
        elif self._mode == "inhale":
            self.value = approach(self.value, 1.0, dt, 0.22)
            if self.value > 0.97:
                self._mode = "exhale" if speaking and self._quiet < self.PAUSE_S else "idle"
                self._phase = math.pi / 2               # continue the cycle from the top
        else:
            hz = self.SLEEPY_HZ if sleepy else self.IDLE_HZ
            self._phase = (self._phase + dt * hz * math.tau) % math.tau
            self.value = approach(self.value, 0.5 + 0.5 * math.sin(self._phase), dt, 0.3)
        return self.value


# ─── emotion ──────────────────────────────────────────────────────────

# Valence, arousal, dominance in [-1, 1] (Russell / Mehrabian style).
EMOTION_VAD: Dict[str, Tuple[float, float, float]] = {
    "happy": (0.8, 0.5, 0.4), "delighted": (0.9, 0.7, 0.5), "excited": (0.8, 0.9, 0.5),
    "amused": (0.6, 0.4, 0.3), "laughing": (0.9, 0.8, 0.4), "chuckling": (0.6, 0.4, 0.3),
    "satisfied": (0.6, 0.1, 0.4), "proud": (0.7, 0.4, 0.8), "confident": (0.5, 0.3, 0.8),
    "relaxed": (0.5, -0.5, 0.3), "calm": (0.4, -0.6, 0.3), "grateful": (0.7, 0.2, 0.0),
    "hopeful": (0.5, 0.3, 0.1), "optimistic": (0.6, 0.4, 0.3), "moved": (0.5, 0.3, -0.2),
    "empathetic": (0.3, 0.0, -0.1), "sympathetic": (0.3, 0.0, -0.1), "compassionate": (0.4, 0.0, 0.0),
    "surprised": (0.2, 0.8, -0.1), "curious": (0.3, 0.4, 0.1), "embarrassed": (-0.2, 0.5, -0.6),
    "nervous": (-0.4, 0.6, -0.6), "anxious": (-0.5, 0.6, -0.6), "worried": (-0.5, 0.4, -0.5),
    "scared": (-0.7, 0.8, -0.8), "confused": (-0.2, 0.3, -0.4), "uncertain": (-0.2, 0.1, -0.4),
    "doubtful": (-0.3, 0.1, -0.2), "sad": (-0.7, -0.3, -0.4), "unhappy": (-0.6, -0.2, -0.3),
    "disappointed": (-0.6, -0.2, -0.3), "upset": (-0.7, 0.4, -0.3), "depressed": (-0.8, -0.6, -0.6),
    "regretful": (-0.5, -0.2, -0.4), "guilty": (-0.5, 0.1, -0.5), "lonely": (-0.6, -0.3, -0.5),
    "angry": (-0.7, 0.8, 0.6), "frustrated": (-0.6, 0.6, 0.2), "disgusted": (-0.7, 0.4, 0.3),
    "disdainful": (-0.4, 0.1, 0.6), "contemptuous": (-0.5, 0.2, 0.7), "sarcastic": (-0.1, 0.3, 0.5),
    "indifferent": (0.0, -0.4, 0.2), "bored": (-0.3, -0.7, -0.1), "whispering": (0.0, -0.4, -0.2),
    "soft tone": (0.3, -0.3, 0.0), "determined": (0.3, 0.6, 0.7), "yawning": (0.0, -0.8, -0.2),
}

FACE_CHANNELS = ("smile", "bly", "bry", "blf", "brf", "eye_open")


class EmotionState:
    """A continuous emotion instead of on/off snapping. trigger() starts a
    300 ms ramp from whatever the face shows now to the new target. The
    target holds while she's speaking that sentence (or MAX_WAIT_S if no
    speech follows), then decays to neutral over 5 s."""

    RAMP_S = 0.3
    DECAY_S = 5.0
    MIN_HOLD_S = 1.5
    MAX_WAIT_S = 6.0         # tag arrived but no speech came: release anyway

    def __init__(self):
        self.tag: Optional[str] = None
        self._from: Dict[str, float] = {}
        self._to: Dict[str, float] = {}
        self._vad_from = (0.0, 0.0, 0.0)
        self._vad_to = (0.0, 0.0, 0.0)
        self._t0 = 0.0
        self._spoke = False
        self._release_at: Optional[float] = None
        self.output: Dict[str, float] = {c: 0.0 for c in FACE_CHANNELS}
        self.vad = (0.0, 0.0, 0.0)
        self.intensity = 0.0

    def trigger(self, tag: str, channels: Dict[str, float], now: float,
                vad: Optional[Tuple[float, float, float]] = None) -> None:
        self.tag = tag
        self._from = dict(self.output)
        self._to = {c: float(channels.get(c, 0.0)) for c in FACE_CHANNELS}
        self._vad_from = self.vad
        self._vad_to = vad if vad is not None else EMOTION_VAD.get(tag, (0.0, 0.0, 0.0))
        self._t0 = now
        self._spoke = False
        self._release_at = None

    def clear(self, now: float) -> None:
        """Let go now: decay from the current face to neutral."""
        if self.tag is not None and self._release_at is None:
            self._release_at = now

    def update(self, now: float, speaking: bool) -> Dict[str, float]:
        if self.tag is None:
            return self.output
        held = now - self._t0
        if speaking:
            self._spoke = True
            self._release_at = None
        elif self._release_at is None and held >= self.MIN_HOLD_S and (self._spoke or held >= self.MAX_WAIT_S):
            self._release_at = now

        k = 1.0 - (1.0 - _clamp(held / self.RAMP_S, 0.0, 1.0)) ** 2          # ease-out ramp
        decay = 1.0
        if self._release_at is not None:
            decay = 1.0 - smoothstep((now - self._release_at) / self.DECAY_S)
        for c in FACE_CHANNELS:
            v = self._from.get(c, 0.0) + (self._to[c] - self._from.get(c, 0.0)) * k
            self.output[c] = v * decay
        self.vad = tuple((a + (b - a) * k) * decay for a, b in zip(self._vad_from, self._vad_to))
        self.intensity = k * decay
        if decay <= 0.0:
            self.tag = None
            self.output = {c: 0.0 for c in FACE_CHANNELS}
            self.vad = (0.0, 0.0, 0.0)
            self.intensity = 0.0
        return self.output


# ─── mouth / lip-sync ─────────────────────────────────────────────────

# Rough formant centroids (F1, F2) in Hz for a female voice.
VOWEL_FORMANTS = {"A": (850, 1500), "E": (560, 2150), "I": (340, 2600), "O": (600, 1000), "U": (420, 900)}
# Mouth shape per vowel: form (+ wide smile / - rounded) and how open.
VOWEL_FORM = {"A": 0.15, "E": 0.75, "I": 0.55, "O": -0.65, "U": -0.85}
VOWEL_OPEN = {"A": 1.0, "E": 0.7, "I": 0.45, "O": 0.8, "U": 0.5}


def classify_vowel(f1: float, f2: float) -> str:
    """Nearest vowel in log-formant space."""
    best, best_d = "A", float("inf")
    lf1, lf2 = math.log(max(f1, 1.0)), math.log(max(f2, 1.0))
    for v, (c1, c2) in VOWEL_FORMANTS.items():
        d = (lf1 - math.log(c1)) ** 2 + (lf2 - math.log(c2)) ** 2
        if d < best_d:
            best, best_d = v, d
    return best


def _lpc(x, order: int):
    """LPC coefficients [1, a1..ap] by autocorrelation + Levinson-Durbin."""
    r = np.correlate(x, x, mode="full")[len(x) - 1:len(x) + order]
    if r[0] <= 0:
        return None
    r = r.astype(np.float64)
    r[0] *= 1.0 + 1e-6                     # white-noise correction: keeps it stable
    a = np.zeros(order + 1)
    a[0], err = 1.0, r[0]
    for i in range(1, order + 1):
        k = -(r[i] + np.dot(a[1:i], r[i - 1:0:-1])) / err
        a[1:i] = a[1:i] + k * a[i - 1:0:-1]
        a[i] = k
        err *= 1.0 - k * k
        if err <= 0:
            return None
    return a


def estimate_formants(frame, rate: int) -> Tuple[float, float]:
    """(F1, F2) of one audio frame by linear prediction: downsample to
    ~11 kHz (formants of interest are below 3.5 kHz), pre-emphasise, window,
    fit an all-pole vocal-tract model, and read resonances from its roots.
    The FFT-peak approach merges F1/F2 for back vowels (O, U); LPC doesn't."""
    x = np.asarray(frame, dtype=np.float64)
    q = max(1, int(rate // 11000))
    if q > 1:
        x = np.convolve(x, np.ones(q) / q, mode="same")[::q]      # crude anti-alias + decimate
    fs = rate / q
    x = np.append(x[0], x[1:] - 0.63 * x[:-1]) * np.hamming(len(x))
    a = _lpc(x, order=int(2 + fs // 1000))              # rule of thumb: 2 + fs in kHz
    if a is None:
        return 500.0, 1500.0
    roots = [z for z in np.roots(a) if z.imag > 0]
    found = []
    for z in roots:
        f = math.atan2(z.imag, z.real) * fs / math.tau
        bw = -math.log(max(abs(z), 1e-9)) * fs / math.pi
        if 200.0 < f < 3500.0 and bw < 500.0:
            found.append(f)
    found.sort()
    if len(found) < 2:
        return (found[0] if found else 500.0), 1500.0
    return found[0], found[1]


def analyze_visemes(samples, rate: int, chunk: int = 1024) -> List[Tuple[float, float]]:
    """Per playback chunk: (mouth_open 0..1, mouth_form -1..1).

    open is the loudness envelope (normalized RMS with a gamma so soft
    syllables still move) scaled by the vowel's openness; form is the
    vowel's shape (E/I wide, O/U round). Quiet chunks (consonants, gaps)
    keep the previous vowel's shape so the lips don't flicker."""
    if samples is None or len(samples) == 0:
        return []
    if not HAS_NUMPY:
        out = []
        for i in range(0, len(samples), chunk):
            c = samples[i:i + chunk]
            out.append((sum(float(s) * float(s) for s in c) / max(1, len(c))) ** 0.5)
        peak = max(out) or 1.0
        return [(min(1.0, (r / peak) ** 0.6), 0.0) for r in out]

    x = np.asarray(samples, dtype=np.float32)
    starts = range(0, len(x), chunk)
    rms = np.array([float(np.sqrt(np.mean(x[i:i + chunk] ** 2))) if len(x[i:i + chunk]) else 0.0
                    for i in starts])
    peak = float(rms.max()) or 1.0
    out: List[Tuple[float, float]] = []
    vowel = None
    for i, r in zip(starts, rms):
        level = min(1.0, (r / peak) ** 0.6)
        frame = x[i:i + chunk]
        if r > 0.18 * peak and len(frame) >= 256:
            vowel = classify_vowel(*estimate_formants(frame, rate))
        if vowel is None:
            out.append((level, 0.0))
        else:
            out.append((min(1.0, level * VOWEL_OPEN[vowel]), VOWEL_FORM[vowel]))
    return out


class MouthDriver:
    """Smooths the TTS viseme stream and, when speech stops, eases the
    mouth back to neutral over SETTLE_S instead of snapping shut."""

    SETTLE_S = 0.2

    def __init__(self):
        self.open = 0.0
        self.form = 0.0
        self._t_open = 0.0
        self._t_form = 0.0
        self._settle_from: Optional[Tuple[float, float]] = None
        self._settle_t = 0.0

    def set_target(self, open_: float, form: float = 0.0) -> None:
        self._t_open = _clamp(float(open_), 0.0, 1.0)
        self._t_form = _clamp(float(form), -1.0, 1.0)

    def update(self, dt: float, speaking: bool) -> Tuple[float, float]:
        if speaking:
            self._settle_from = None
            tau = 0.03 if self._t_open > self.open else 0.06       # fast attack, softer release
            self.open = approach(self.open, self._t_open, dt, tau)
            self.form = approach(self.form, self._t_form, dt, 0.08)
            return self.open, self.form
        if self._settle_from is None:
            self._settle_from, self._settle_t = (self.open, self.form), 0.0
        self._settle_t += dt
        k = 1.0 - smoothstep(self._settle_t / self.SETTLE_S)
        self.open = self._settle_from[0] * k
        self.form = self._settle_from[1] * k
        return self.open, self.form


# ─── agent embodiment ─────────────────────────────────────────────────

POSES = ("idle", "thinking", "approval", "focused", "error")

# Param offsets per pose. Brows are (L-y, R-y, L-form, R-form), -1..1.
POSE_PROFILES: Dict[str, Dict] = {
    "idle":     {"head_y": 0.0, "head_z": 0.0, "brows": (0, 0, 0, 0), "eye_open": 1.0, "sway": 1.0},
    # Pondering: chin up, head tilted, brows raised, eyes drifting up.
    "thinking": {"head_y": 5.0, "head_z": 6.0, "brows": (0.25, 0.35, 0.05, 0.1), "eye_open": 0.9, "sway": 0.6},
    # Fourth wall: square to the camera, eyes open, brows up expectantly.
    "approval": {"head_y": 1.0, "head_z": 0.0, "brows": (0.35, 0.35, 0.15, 0.15), "eye_open": 1.15, "sway": 0.35},
    # Focused: slight frown, narrowed eyes, head a touch down.
    "focused":  {"head_y": -3.0, "head_z": 0.0, "brows": (-0.25, -0.25, -0.35, -0.35), "eye_open": 0.72, "sway": 0.7},
    # Shocked: brows high, eyes wide.
    "error":    {"head_y": 2.0, "head_z": -4.0, "brows": (0.7, 0.7, 0.25, 0.25), "eye_open": 1.3, "sway": 1.3},
}

# Gaze mode per pose (speaking overrides thinking/focused gaze).
POSE_GAZE = {"idle": "idle", "thinking": "thinking", "approval": "camera", "focused": "attentive", "error": "camera"}

READING_RE = re.compile(r"web_search|browse_url|research|search_files|read_file|read_window|"
                        r"run_command|run_in_terminal|claude code|triag|reading|searching", re.I)
ERROR_SHOW_S = 8.0           # errors stay on the tracker card; the face only flinches for a while
POSE_AGENTS = ("lead", "research", "build", "operate")
GAME_TASK = "Minecraft: "      # her game agent runs on Operate; it's her other half and doesn't drive this face


def pose_from_snapshot(snap: Dict[str, Dict], now: Optional[float] = None) -> Tuple[str, bool]:
    """Agent-tracker snapshot -> (pose, reading). Any approval wins (she
    looks at you), then a fresh error, then Lead thinking, then any agent
    executing. reading is True while a search, page read or command runs."""
    now = time.time() if now is None else now
    snap = snap or {}
    agents = {a: s for a, s in ((a, snap.get(a) or {}) for a in snap)
              if not str(s.get("task") or "").startswith(GAME_TASK)}
    if any(s.get("status") == "waiting_approval" for s in agents.values()):
        pose = "approval"
    elif any(a in POSE_AGENTS and s.get("status") == "error" and now - float(s.get("updated") or 0) < ERROR_SHOW_S
             for a, s in agents.items()):
        pose = "error"
    elif (agents.get("lead") or {}).get("status") == "thinking":
        pose = "thinking"
    elif any(a in POSE_AGENTS and s.get("status") == "executing" for a, s in agents.items()):
        pose = "focused"
    else:
        pose = "idle"
    reading = any(a in POSE_AGENTS and s.get("status") == "executing" and READING_RE.search(s.get("task") or "")
                  for a, s in agents.items())
    return pose, reading


class PoseBlender:
    """Blends pose offsets so a state change eases in over ~0.4 s."""

    TAU = 0.18

    def __init__(self):
        idle = POSE_PROFILES["idle"]
        self.head_y = idle["head_y"]
        self.head_z = idle["head_z"]
        self.brows = list(idle["brows"])
        self.eye_open = idle["eye_open"]
        self.sway = idle["sway"]

    def update(self, dt: float, pose: str) -> "PoseBlender":
        p = POSE_PROFILES.get(pose, POSE_PROFILES["idle"])
        self.head_y = approach(self.head_y, p["head_y"], dt, self.TAU)
        self.head_z = approach(self.head_z, p["head_z"], dt, self.TAU)
        self.brows = [approach(c, t, dt, self.TAU) for c, t in zip(self.brows, p["brows"])]
        self.eye_open = approach(self.eye_open, p["eye_open"], dt, self.TAU)
        self.sway = approach(self.sway, p["sway"], dt, self.TAU)
        return self


# ─── contextual idle ──────────────────────────────────────────────────

class YawnScheduler:
    """Past midnight (00:00-05:00 local) and idle for 5 minutes: she gets
    sleepy (slower breathing, heavier lids) and yawns every few minutes."""

    IDLE_S = 300.0
    UP_S, HOLD_S, DOWN_S = 1.2, 0.9, 1.1

    def __init__(self, rng: Optional[random.Random] = None):
        self._rng = rng or random.Random()
        self._next_at: Optional[float] = None
        self._start = -100.0
        self.sleepy = False

    @staticmethod
    def is_late(hour: int) -> bool:
        return 0 <= hour < 5

    def update(self, t: float, hour: int, idle_s: float) -> Tuple[float, bool]:
        """Returns (yawn amount 0..1, started_this_frame)."""
        self.sleepy = self.is_late(hour) and idle_s >= self.IDLE_S
        started = False
        if not self.sleepy:
            self._next_at = None
        else:
            if self._next_at is None:
                self._next_at = t + self._rng.uniform(2.0, 20.0)
            if t >= self._next_at:
                self._start = t
                self._next_at = t + self._rng.uniform(180.0, 420.0)
                started = True
        e = t - self._start
        total = self.UP_S + self.HOLD_S + self.DOWN_S
        if e < 0 or e > total:
            return 0.0, started
        if e < self.UP_S:
            return smoothstep(e / self.UP_S), started
        if e < self.UP_S + self.HOLD_S:
            return 1.0, started
        return 1.0 - smoothstep((e - self.UP_S - self.HOLD_S) / self.DOWN_S), started


# ─── safety ───────────────────────────────────────────────────────────

class FrameLimiter:
    """At most max_fps parameter frames per second."""

    def __init__(self, max_fps: float = 60.0):
        self.min_dt = 1.0 / max_fps
        self._last = -1e9

    def ready(self, now: float) -> bool:
        if now - self._last >= self.min_dt - 1e-4:
            self._last = now
            return True
        return False


def filter_allowed(frame: Dict[str, float], allowed: Optional[Iterable[str]]) -> Dict[str, float]:
    """Drop params the loaded model doesn't have. allowed=None means the
    list couldn't be fetched: send everything (old behaviour)."""
    if allowed is None:
        return dict(frame)
    allowed = set(allowed)
    return {k: v for k, v in frame.items() if k in allowed}

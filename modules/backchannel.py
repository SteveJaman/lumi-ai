"""
Backchannels: Lumi's short "mm-hm"s while you're still talking.

Clips
  BACKCHANNEL_PHRASES are spoken once per voice with the normal TTS
  (TTS.fetch_audio: Fish Audio, or Kokoro when VOICE_ENGINE is local_fast)
  and cached as data/backchannels/<voice_id>/<slug>-<hash>.wav. The hash is
  of voice_id + phrase, so a clip is only made again when it's missing.

When (decide(), the one function to tune)
  The mic's audio callback (RealtimeSTT on_recorded_chunk, the same stream
  and recording that arm barge-in in mic_input.py) feeds on_chunk(). A clip
  plays only if all of these hold:
    - you've been talking without a pause for BACKCHANNEL_MIN_SPEECH_MS
    - BACKCHANNEL_MIN_GAP_MS since the last backchannel and since she last spoke
    - fewer than BACKCHANNEL_MAX_PER_UTTERANCE so far in this utterance
    - she isn't writing a reply, fetching speech or speaking
    - the partial transcript (RealtimeSTT realtime transcription) isn't
      venting: one strongly negative word and the rest of that utterance
      gets no "mm-hm". With partials on, nothing fires before the first
      one, so an opening "I'm so frustrated..." is heard first
  Heuristic only: no classifier.

Playback
  TTS.play_audio(..., volume=BACKCHANNEL_VOLUME, backchannel=True): the same
  output path and devices as her voice, local speakers only. It
  doesn't set AI_speaking, so it neither arms nor is stopped by barge-in.
  Every client hears about it: backchannel_spoken {"phrase", "at"} over the
  existing socket.io connection (signals.broadcast_event).
"""

from __future__ import annotations

import array
import hashlib
import io
import os
import random
import re
import threading
import time
import wave
from typing import Any, Dict, Optional, Tuple

import constants
from modules.log_hub import emit as hub_emit

BACKCHANNEL_PHRASES = ["Mm-hm.", "Right.", "Yeah.", "Uh-huh.", "Got it.", "Okay."]
CACHE_ROOT = os.path.join(constants.DATA_ROOT, "backchannels")

# Talking vs pause, from the loudness of each mic chunk (16-bit mono).
SPEECH_RMS = 350          # louder than this counts as talking
PAUSE_RESET_MS = 350      # this much quiet ends a "without a pause" run

NEGATIVE_WORDS = (
    "frustrated", "frustrating", "angry", "furious", "pissed", "upset", "hate", "hated",
    "sick of", "tired of", "fed up", "can't stand", "cannot stand", "annoyed", "annoying",
    "awful", "terrible", "horrible", "worst", "miserable", "depressed", "depressing",
    "crying", "cried", "stressed", "overwhelmed", "anxious", "panicking", "scared",
    "lonely", "hurts", "heartbroken", "passed away", "died", "devastated", "ruined",
)
_NEG = re.compile(r"\b(?:" + "|".join(re.escape(w) for w in NEGATIVE_WORDS) + r")\b", re.I)


def _cfg(name, default):
    return getattr(constants, name, default)


def is_venting(text: str) -> bool:
    """Strongly negative? (No emotional-state helper scores text, so: keywords.)"""
    return bool(_NEG.search(text or ""))


def voice_id() -> str:
    if _cfg("VOICE_ENGINE", "fish") == "local_fast":
        return re.sub(r"[^A-Za-z0-9_-]", "_", f"kokoro-{_cfg('LOCAL_VOICE', 'default')}")
    ref = os.getenv("FISH_REFERENCE_ID") or "default"
    return re.sub(r"[^A-Za-z0-9_-]", "_", ref)[:64]


def clip_path(phrase: str, vid: Optional[str] = None) -> str:
    vid = vid or voice_id()
    slug = re.sub(r"[^a-z0-9]+", "-", phrase.lower()).strip("-")
    digest = hashlib.sha1(f"{vid}\n{phrase}".encode("utf-8")).hexdigest()[:10]
    return os.path.join(CACHE_ROOT, vid, f"{slug}-{digest}.wav")


def _valid_wav(data: bytes) -> bool:
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            return w.getnframes() > 0
    except Exception:
        return False


def _rms(chunk: bytes) -> float:
    if not chunk:
        return 0.0
    samples = array.array("h")
    samples.frombytes(chunk[: len(chunk) - len(chunk) % 2])
    if not samples:
        return 0.0
    step = max(1, len(samples) // 256)            # a sample of the chunk is plenty
    picked = samples[::step]
    return (sum(s * s for s in picked) / len(picked)) ** 0.5


class Backchannel:
    def __init__(self, signals, tts=None):
        self.signals = signals
        self._tts = tts
        self._lock = threading.Lock()
        self.clips: Dict[str, bytes] = {}
        self.expect_partials = False       # mic_input sets it when realtime transcription is on
        self.last_fired = 0.0
        self._reset_utterance()
        signals.backchannel = self

    @property
    def tts(self):
        return self._tts or getattr(self.signals, "tts_instance", None)

    # ── clips ──────────────────────────────────────────────────
    def ensure_clips(self, tts=None) -> int:
        """Load the cached clips for this voice, making any that are missing.
        Returns how many are ready. Blocking (network for missing ones)."""
        tts = tts or self.tts
        vid = voice_id()
        made = 0
        for phrase in BACKCHANNEL_PHRASES:
            path = clip_path(phrase, vid)
            data = None
            if os.path.exists(path):
                with open(path, "rb") as f:
                    data = f.read()
                if not _valid_wav(data):
                    data = None
            if data is None and tts is not None and hasattr(tts, "fetch_audio"):
                try:
                    data = tts.fetch_audio(phrase)
                except Exception:
                    data = None
                if data and _valid_wav(data):
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    tmp = path + ".tmp"
                    with open(tmp, "wb") as f:
                        f.write(data)
                    os.replace(tmp, path)
                    made += 1
                else:
                    data = None
            if data:
                self.clips[phrase] = data
        if made:
            hub_emit(self.signals, "voice", f"backchannels: made {made} clip(s) for voice {vid}")
        return len(self.clips)

    def warm_up_async(self) -> None:
        """Load/make the clips in the background once TTS exists."""
        def go():
            for _ in range(120):
                if self.tts is not None or getattr(self.signals, "terminate", False):
                    break
                time.sleep(0.5)
            try:
                n = self.ensure_clips()
                hub_emit(self.signals, "voice", f"backchannels ready ({n}/{len(BACKCHANNEL_PHRASES)} clips)")
            except Exception as e:
                hub_emit(self.signals, "voice", f"backchannel clips failed: {e}", level="warn")
        threading.Thread(target=go, daemon=True, name="BackchannelClips").start()

    # ── utterance tracking (mic thread) ────────────────────────
    def _reset_utterance(self) -> None:
        self.count = 0
        self.run_start: Optional[float] = None
        self.last_voiced: Optional[float] = None
        self.partial = ""
        self.vented = False

    def on_utterance_start(self) -> None:
        with self._lock:
            self._reset_utterance()

    def on_utterance_end(self) -> None:
        with self._lock:
            self._reset_utterance()

    def on_partial(self, text: str) -> None:
        with self._lock:
            self.partial = text or ""
            if is_venting(self.partial):
                self.vented = True

    def _lumi_busy(self) -> bool:
        s = self.signals
        if getattr(s, "AI_thinking", False) or getattr(s, "AI_speaking", False):
            return True
        tts = self.tts
        if tts is not None:
            if getattr(tts, "is_playing", False) or getattr(tts, "_fetching", False):
                return True
            q = getattr(tts, "audio_queue", None)
            if q is not None and not q.empty():
                return True
        tq = getattr(s, "tts_queue", None)
        return bool(tq is not None and not tq.empty())

    def snapshot(self, now: Optional[float] = None) -> Dict[str, Any]:
        """The numbers decide() looks at."""
        now = time.time() if now is None else now
        her_last = max(self.last_fired, getattr(self.tts, "last_audio_end", 0.0) or 0.0)
        return {
            "speech_ms": 0 if self.run_start is None else int((now - self.run_start) * 1000),
            "since_last_ms": float("inf") if not her_last else int((now - her_last) * 1000),
            "count": self.count,
            "lumi_busy": self._lumi_busy(),
            "vented": self.vented or is_venting(self.partial),
            "awaiting_partial": self.expect_partials and not self.partial,
            "partial": self.partial,
            "enabled": bool(_cfg("BACKCHANNEL_ENABLED", True)),
            "have_clips": bool(self.clips),
        }

    def on_chunk(self, chunk: bytes, now: Optional[float] = None) -> Optional[str]:
        """Called for every recorded mic chunk. Returns the phrase if one fired."""
        now = time.time() if now is None else now
        with self._lock:
            if _rms(chunk) >= SPEECH_RMS:
                if self.run_start is None or (self.last_voiced and (now - self.last_voiced) * 1000 > PAUSE_RESET_MS):
                    self.run_start = now
                self.last_voiced = now
            elif self.last_voiced and (now - self.last_voiced) * 1000 > PAUSE_RESET_MS:
                self.run_start = None
            ok, _why = decide(self.snapshot(now))
            if not ok:
                return None
            phrase = random.choice(list(self.clips))
            self.count += 1
            self.last_fired = now
            self.run_start = now            # the next one needs another full run of talking
        self._play(phrase, now)
        return phrase

    def _play(self, phrase: str, at: float) -> None:
        data = self.clips.get(phrase)
        tts = self.tts
        if data and tts is not None:
            threading.Thread(
                target=tts.play_audio, args=(phrase, data),
                kwargs={"volume": float(_cfg("BACKCHANNEL_VOLUME", 0.6)), "backchannel": True},
                daemon=True, name="Backchannel").start()
        payload = {"phrase": phrase, "at": at}
        send = getattr(self.signals, "broadcast_event", None)
        if send is not None:
            try:
                send("backchannel_spoken", payload)
            except Exception:
                pass
        hub_emit(self.signals, "voice", f"backchannel: {phrase}")


def decide(signal: Dict[str, Any]) -> Tuple[bool, str]:
    """Should she backchannel right now? (True, "ok") or (False, why).
    Every rule lives here, so this is the one place to tune."""
    if not signal.get("enabled", True):
        return False, "disabled"
    if not signal.get("have_clips", True):
        return False, "no clips"
    if signal["speech_ms"] < _cfg("BACKCHANNEL_MIN_SPEECH_MS", 900):
        return False, "not talking long enough"
    if signal["since_last_ms"] < _cfg("BACKCHANNEL_MIN_GAP_MS", 4000):
        return False, "too soon"
    if signal["count"] >= _cfg("BACKCHANNEL_MAX_PER_UTTERANCE", 2):
        return False, "enough this utterance"
    if signal["lumi_busy"]:
        return False, "she's replying"
    if signal.get("awaiting_partial"):
        return False, "nothing transcribed yet"
    if signal.get("vented"):
        return False, "venting"
    return True, "ok"


async def should_backchannel(signal: Dict[str, Any]) -> bool:
    return decide(signal)[0]

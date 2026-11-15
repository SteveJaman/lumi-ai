"""
Full-duplex-style turn taking on top of the cascaded voice pipeline
(RealtimeSTT -> LLM -> Fish Audio). Pure logic, no audio I/O: mic_input.py
feeds it the mic chunks and partial transcripts it already gets, and tts.py
tells it how loud she is right now.

1. Smart end of turn: end_of_turn_silence(partial) is how much silence ends
   your turn. A finished sentence ends after TURN_SILENCE_FAST; one that
   trails off ("and...", "um", a comma) waits TURN_SILENCE_SLOW. mic_input
   sets it on the recorder live (RealtimeSTT reads post_speech_silence_duration
   on every loop). Replaces the fixed 2.2 s.

2. Echo guard (EchoGuard): with speakers, the mic hears her. A mic chunk
   only counts as *your* voice while she's talking if it's clearly louder
   than the echo of what she's playing (a learned mic/playback ratio times
   ECHO_MARGIN). And a transcript that matches what she just said is her own
   echo, not a turn (is_echo). This is echo *suppression*, not acoustic echo
   cancellation: with headphones it simply never triggers.

3. Barge-in that knows a backchannel (BargeIn): starting to talk while she
   speaks or writes no longer stops her at once. It stops her once you've
   talked for BARGE_IN_CONFIRM_MS and the words aren't just "yeah / mm-hm /
   right", right away on a word like "wait" or "stop", and always after
   BARGE_IN_FORCE_MS. If all you said was a backchannel, she carries on and
   it isn't sent as a message. BARGE_IN_CONFIRM_MS = 0 is the old instant
   barge-in.
"""

from __future__ import annotations

import difflib
import re
import threading
import time
from collections import deque
from typing import Deque, Iterable, Optional, Tuple

import constants

SPEECH_RMS = 350              # 16-bit mic RMS that counts as talking at all
LEVEL_WINDOW_S = 0.35         # her playback level is the max over this window (echo arrives late)
ECHO_RECENT_S = 30            # transcripts are compared with what she said in this window
ECHO_MATCH = 0.6              # ...and dropped at this similarity


def _cfg(name, default):
    return getattr(constants, name, default)


def _words(text: str):
    return re.sub(r"[^a-z0-9' ]+", " ", (text or "").lower().replace("-", "")).split()


# ── 1. end of turn ─────────────────────────────────────────────
CONTINUATION_WORDS = frozenset("""
and but or so because cause cuz um uh er erm like the a an to of with for if then that which
my your his her our their is was were are am i we you they about in on at when while just also
maybe well actually basically gonna wanna
""".split())


def end_of_turn_silence(partial: str) -> float:
    """Seconds of silence that end the user's turn, given what they've said."""
    fast = float(_cfg("TURN_SILENCE_FAST", 0.6))
    default = float(_cfg("TURN_SILENCE_DEFAULT", 1.0))
    slow = float(_cfg("TURN_SILENCE_SLOW", 1.8))
    text = (partial or "").strip()
    if not text:
        return default
    words = _words(text)
    if not words:
        return default
    if text.endswith((",", "...", "…", "-", "—")) or words[-1] in CONTINUATION_WORDS:
        return slow                              # trailing off: they're not done
    if text[-1] in ".?!" and len(words) >= 3:
        return fast                              # a finished sentence
    return default


# ── word classes ───────────────────────────────────────────────
BACKCHANNEL_WORDS = frozenset("""
yeah yea ya yes yep yup mm mmm mhm mmhm mmhmm hm hmm uh uhhuh uhuh huh right ok okay sure true
nice cool wow oh ah aha haha hah lol totally exactly really yeah yeah got it i see gotcha
""".split())
INTERRUPT_PHRASES = ("wait", "stop", "hold on", "hang on", "actually", "hey",
                     "excuse me", "listen", "shut up", "enough", "lumi")
_INTERRUPT = re.compile(r"\b(?:" + "|".join(re.escape(p) for p in INTERRUPT_PHRASES) + r")\b", re.I)


def is_backchannel(text: str) -> bool:
    """Only listener noises ("yeah", "mm-hm", "oh wow", "got it"), at most 4 words."""
    words = _words(text)
    return 0 < len(words) <= 4 and all(w in BACKCHANNEL_WORDS for w in words)


def has_interrupt_word(text: str) -> bool:
    return bool(_INTERRUPT.search(text or ""))


# ── 2. echo ────────────────────────────────────────────────────
class EchoGuard:
    """Learns how loud her voice is in the mic (mic RMS / playback RMS) and
    only counts louder-than-echo chunks as the user's voice while she plays."""

    def __init__(self):
        self.gain = 0.0
        self._lock = threading.Lock()

    def observe(self, mic_rms: float, play_rms: float, user_active: bool) -> None:
        """While she plays and nobody is (known to be) talking over her."""
        if user_active or play_rms < 200:
            return
        ratio = mic_rms / play_rms
        with self._lock:
            if self.gain == 0.0:
                self.gain = ratio
            elif ratio < self.gain:
                self.gain = 0.7 * self.gain + 0.3 * ratio       # quiet room: drop fast
            elif ratio < self.gain * 3:
                self.gain = 0.97 * self.gain + 0.03 * ratio     # louder: rise slowly, ignore spikes

    def threshold(self, play_rms: float) -> float:
        if not _cfg("ECHO_GUARD_ENABLED", True) or play_rms <= 0:
            return SPEECH_RMS
        return max(SPEECH_RMS, float(_cfg("ECHO_MARGIN", 2.0)) * self.gain * play_rms)

    def is_user_voice(self, mic_rms: float, play_rms: float) -> bool:
        return mic_rms >= self.threshold(play_rms)


def is_echo(transcript: str, recent: Iterable[Tuple[float, str]], now: Optional[float] = None) -> bool:
    """Is this transcript just her own recent words picked up by the mic?"""
    if not _cfg("ECHO_GUARD_ENABLED", True):
        return False
    now = time.time() if now is None else now
    said = [re.sub(r"\[[^\]]*\]", " ", t) for ts, t in recent if now - ts <= ECHO_RECENT_S]
    if not said or len(_words(transcript)) < 3:
        return False
    heard = " ".join(_words(transcript))
    joined = " ".join(_words(" ".join(said)))
    if heard and heard in joined:
        return True
    candidates = [" ".join(_words(s)) for s in said]
    candidates.append(" ".join(_words(" ".join(said[-2:]))))
    return any(difflib.SequenceMatcher(None, heard, c).ratio() >= ECHO_MATCH for c in candidates if c)


# ── 3. barge-in ────────────────────────────────────────────────
class BargeIn:
    """Pending -> confirmed (she's stopped) or resolved at the final transcript."""

    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        self.pending = False
        self.confirmed = False
        self.mode = ""                 # "speaking" | "generating"
        self.voiced_ms = 0.0
        self.partial = ""
        self.armed_at = 0.0

    def arm(self, mode: str, now: Optional[float] = None) -> None:
        with self._lock:
            self.reset()
            self.pending = True
            self.mode = mode
            self.armed_at = time.time() if now is None else now

    def _should_confirm(self) -> bool:
        if has_interrupt_word(self.partial):
            return True
        if self.voiced_ms >= float(_cfg("BARGE_IN_FORCE_MS", 1500)):
            return True
        return (self.voiced_ms >= float(_cfg("BARGE_IN_CONFIRM_MS", 500))
                and bool(self.partial) and not is_backchannel(self.partial))

    def _confirm_if_ready(self) -> bool:
        if self.pending and not self.confirmed and self._should_confirm():
            self.confirmed = True
            return True
        return False

    def on_chunk(self, user_voice: bool, chunk_ms: float) -> bool:
        """True exactly once: when this chunk confirms the barge-in."""
        with self._lock:
            if not self.pending or self.confirmed:
                return False
            if user_voice:
                self.voiced_ms += chunk_ms
            return self._confirm_if_ready()

    def on_partial(self, text: str) -> bool:
        with self._lock:
            if not self.pending or self.confirmed:
                return False
            self.partial = text or ""
            return self._confirm_if_ready()

    def resolve_final(self, text: str) -> Optional[str]:
        """At the final transcript of a pending (unconfirmed) barge-in:
        "drop" (a backchannel; she keeps going), "interrupt" (real words:
        stop her now and take the turn), or None (nothing pending)."""
        with self._lock:
            if not self.pending or self.confirmed:
                return None
            return "drop" if is_backchannel(text) else "interrupt"


class PlaybackLevel:
    """Her output loudness over the last moments (tts.py writes, the echo guard reads)."""

    def __init__(self):
        self._levels: Deque[Tuple[float, float]] = deque(maxlen=64)
        self._lock = threading.Lock()

    def note(self, rms: float, now: Optional[float] = None) -> None:
        with self._lock:
            self._levels.append((time.time() if now is None else now, float(rms)))

    def current(self, now: Optional[float] = None) -> float:
        now = time.time() if now is None else now
        with self._lock:
            recent = [r for t, r in self._levels if now - t <= LEVEL_WINDOW_S]
        return max(recent) if recent else 0.0

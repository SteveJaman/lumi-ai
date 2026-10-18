"""
Live subtitles for OBS.

TTS calls show(text, duration) the moment a line starts playing. The
line is split into short chunks, and each chunk is shown for a share of
the audio duration proportional to its length, so the text tracks the
voice without word-level timestamps. After the last chunk the text is
held briefly, then cleared.

Each chunk goes two places:
  - OBS_SUBTITLE_FILE, for an OBS Text source with "Read from file"
    on. Works with no websocket setup at all.
  - OBS_SUBTITLE_SOURCE over obs-websocket, if configured. Updates
    instantly instead of on OBS's file poll.

A newer show() or a clear() (barge-in) cancels whatever is running.
"""

import os
import re
import threading

try:
    from constants import (OBS_SUBTITLES_ENABLED, OBS_SUBTITLE_FILE,
                           OBS_SUBTITLE_MAX_CHARS, OBS_SUBTITLE_HOLD_SECONDS)
except ImportError:
    OBS_SUBTITLES_ENABLED = False
    OBS_SUBTITLE_FILE = ""
    OBS_SUBTITLE_MAX_CHARS = 90
    OBS_SUBTITLE_HOLD_SECONDS = 2.5

# Fish Audio delivery tags ([sighing], [whispering], …) are spoken
# cues, not words; *actions* are dropped before TTS (tts.fetch_audio).
# Viewers shouldn't see either.
_TAG = re.compile(r"\[[^\]]{1,40}\]")
_ACTION = re.compile(r"\*.*?\*")
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")


def clean_subtitle_text(text):
    text = _ACTION.sub("", _TAG.sub("", text or ""))
    return re.sub(r"\s+", " ", text).strip()


def chunk_subtitle(text, max_chars=OBS_SUBTITLE_MAX_CHARS):
    """Split into chunks of at most max_chars, preferring sentence
    boundaries, then word boundaries. A single word longer than
    max_chars becomes its own chunk."""
    chunks = []
    for sentence in _SENTENCE_END.split(text):
        current = ""
        for word in sentence.split():
            candidate = f"{current} {word}" if current else word
            if len(candidate) <= max_chars or not current:
                current = candidate
            else:
                chunks.append(current)
                current = word
        if current:
            chunks.append(current)

    # Merge short sentences ("Oh. It's you.") so the screen doesn't
    # flicker through one-word chunks.
    merged = []
    for c in chunks:
        if merged and len(merged[-1]) + 1 + len(c) <= max_chars:
            merged[-1] = f"{merged[-1]} {c}"
        else:
            merged.append(c)
    return merged


class SubtitleWriter:

    def __init__(self, obs=None, path=OBS_SUBTITLE_FILE,
                 enabled=OBS_SUBTITLES_ENABLED,
                 hold_seconds=OBS_SUBTITLE_HOLD_SECONDS):
        self.obs = obs
        self.path = path
        self.enabled = bool(enabled and path)
        self.hold_seconds = hold_seconds
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        if self.enabled:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            self._write("")

    def show(self, text, duration):
        if not self.enabled:
            return
        chunks = chunk_subtitle(clean_subtitle_text(text))
        if not chunks:
            return
        with self._lock:
            self._cancel.set()
            self._cancel = cancel = threading.Event()
        threading.Thread(
            target=self._play, args=(chunks, max(duration, 0.5), cancel),
            daemon=True, name="subtitles",
        ).start()

    def clear(self):
        if not self.enabled:
            return
        with self._lock:
            self._cancel.set()
        self._emit("")

    def _play(self, chunks, duration, cancel):
        total = sum(len(c) for c in chunks)
        for chunk in chunks:
            if cancel.is_set():
                return
            self._emit(chunk)
            if cancel.wait(duration * len(chunk) / total):
                return
        if not cancel.wait(self.hold_seconds):
            self._emit("")

    def _emit(self, text):
        self._write(text)
        if self.obs is not None:
            self.obs.set_subtitle(text)

    def _write(self, text):
        # Plain overwrite, not write-then-rename: on Windows the rename
        # fails while OBS has the file open for its poll.
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError as e:
            print(f"\033[90mSUBTITLES: write failed — {e}\033[0m")

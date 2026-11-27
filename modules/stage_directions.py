"""
Stage directions in Lumi's reply stream (Phase 4).

The model may write two things besides what she says:

    <thought>what she's thinking</thought>     shown in the Inner Thoughts bubble, never spoken
    [smug]  [panicked]  [pout]  [excited]      emotion tags for the avatar, never spoken

StageSplitter takes the reply in chunks, as it streams, and returns only what is
safe to say aloud and to show, plus the thoughts and emotions it found. A tag split
across two chunks ("<tho" then "ught>") is held back until it's complete, so the
TTS queue never hears a stage direction.

    s = StageSplitter()
    out = s.feed(chunk)     # Out(spoken, thoughts, emotions)
    end = s.flush()         # at the end of the stream
    split(text)             # the same, all at once
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List

EMOTIONS = frozenset({"smug", "panicked", "pout", "excited", "scheming", "deadpan", "blush"})
_OPEN = "<thought>"
_CLOSE = "</thought>"
_EMO = re.compile(r"\[([a-zA-Z]+)\]")
_PARTIAL_EMO = re.compile(r"\[[a-zA-Z]*")


@dataclass
class Out:
    spoken: str = ""
    thoughts: List[str] = field(default_factory=list)
    emotions: List[str] = field(default_factory=list)


def _partial_suffix(buf: str, tag: str) -> int:
    """Length of the longest end of buf that could be the start of tag (case-insensitive)."""
    low = buf.lower()
    for k in range(min(len(tag) - 1, len(buf)), 0, -1):
        if low.endswith(tag[:k].lower()):
            return k
    return 0


class StageSplitter:
    def __init__(self):
        self._buf = ""
        self._in_thought = False
        self._thought = ""

    def feed(self, chunk: str) -> Out:
        self._buf += chunk or ""
        out = Out()
        buf = self._buf
        while True:
            if self._in_thought:
                i = buf.lower().find(_CLOSE)
                if i < 0:
                    keep = _partial_suffix(buf, _CLOSE)
                    self._thought += buf[:len(buf) - keep]
                    buf = buf[len(buf) - keep:]
                    break
                self._thought += buf[:i]
                out.thoughts.append(self._thought.strip())
                self._thought = ""
                buf = buf[i + len(_CLOSE):]
                self._in_thought = False
                continue

            low = buf.lower()
            pos_open = low.find(_OPEN)
            pos_br = buf.find("[")
            hits = [p for p in (pos_open, pos_br) if p >= 0]
            if not hits:
                keep = _partial_suffix(buf, _OPEN)
                out.spoken += buf[:len(buf) - keep]
                buf = buf[len(buf) - keep:]
                break
            pos = min(hits)
            out.spoken += buf[:pos]
            buf = buf[pos:]
            if buf.lower().startswith(_OPEN):
                buf = buf[len(_OPEN):]
                self._in_thought = True
                continue
            m = _EMO.match(buf)
            if m:
                if m.group(1).lower() in EMOTIONS:
                    out.emotions.append(m.group(1).lower())
                    buf = buf[m.end():]
                else:                                   # "[note]" is just text
                    out.spoken += "["
                    buf = buf[1:]
                continue
            if _PARTIAL_EMO.fullmatch(buf):             # "[sm": wait for the rest
                break
            out.spoken += "["
            buf = buf[1:]
        self._buf = buf
        return out

    def flush(self) -> Out:
        """End of the stream: whatever is left. An unclosed thought counts as a thought."""
        out = Out()
        if self._in_thought:
            text = (self._thought + self._buf).strip()
            if text:
                out.thoughts.append(text)
            self._thought, self._buf, self._in_thought = "", "", False
        else:
            out.spoken = self._buf                      # a lone "[" or "<tho" at the end is still text
            self._buf = ""
        return out


def split(text: str) -> Out:
    s = StageSplitter()
    a = s.feed(text)
    b = s.flush()
    return Out(a.spoken + b.spoken, a.thoughts + b.thoughts, a.emotions + b.emotions)

"""
A silent slur filter for what Lumi says and shows.

Slurs (racial, ethnic, homophobic, transphobic, ableist) are removed from her
words before TTS and before they reach the chat and history. Ordinary
swearing is left alone; her persona allows it. Nothing is substituted and
nothing announces it: the word is just gone, so she never says "filtered"
or talks about a filter.
"""

from __future__ import annotations

import re

# Stems, matched as whole words with common plural/variant endings.
_SLURS = [
    r"n[i1]gg(?:a|ah|as|az|er|ers|uh|uhs|3r|3rs)",
    r"chinks?",
    r"spics?",
    r"kikes?",
    r"wetbacks?",
    r"gooks?",
    r"ragheads?",
    r"towelheads?",
    r"zipperheads?",
    r"beaners?",
    r"pakis?",
    r"coons?",
    r"faggots?",
    r"fags?",
    r"dykes?",
    r"trann(?:y|ies)",
    r"retard(?:s|ed)?",
]
_SLUR_RE = re.compile(r"\b(?:" + "|".join(_SLURS) + r")\b", re.I)


def strip_slurs(text: str) -> str:
    """text without slurs, spacing tidied. Everything else is unchanged."""
    if not text or not _SLUR_RE.search(text):
        return text
    out = _SLUR_RE.sub("", text)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\s+([,.!?;:])", r"\1", out)
    return out.strip()

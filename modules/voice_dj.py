"""
Voice DJ: music requests in plain words, typed or spoken.

    parse(text) -> Intent | None     what the message asks the music to do
    handle(text) -> str | None       do it and return Lumi's short reply (None: not a music request)

Two stages, both free:
  1. Exact phrases (regex): "skip", "pause the music", "volume 30", "turn it
     up", "like this song", "shuffle on". No model call.
  2. Anything that only *might* be about music ("queue some chill
     instrumental music", "put on something upbeat") goes to the local router
     model (constants.ROUTER_MODEL, else GAME_AGENT_MODEL: qwen3.5:9b), which answers in
     JSON and says "none" unless the message clearly asks to play or queue
     music. "Let's play a game" stays a normal chat turn.
Skipped in Gaming Mode for stage 2, so the router model isn't loaded mid-game.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional

import requests

import constants

MODEL_TIMEOUT_S = 6.0
COLD_TIMEOUT_S = 30.0         # the model isn't loaded yet: a song request may wait for it
KINDS = ("track", "playlist", "album", "artist", "show")


@dataclass
class Intent:
    action: str                 # play_query | queue_query | next | previous | pause | play | volume | like | shuffle
    query: str = ""
    kind: str = "track"
    value: object = None


_END = r"[\s.!?]*$"
_EXACT = [
    (re.compile(r"^(skip|next)(\s+(this|the))?(\s+(song|track|one))?" + _END, re.I), "next", None),
    (re.compile(r"^(go back|previous|back)(\s+(song|track))?|^play the (last|previous) (song|track)" + _END, re.I),
     "previous", None),
    (re.compile(r"^(pause|stop)(\s+(the|my))?(\s+(music|song|spotify|track))?" + _END, re.I), "pause", None),
    (re.compile(r"^(resume|unpause|continue)(\s+(the|my))?(\s+(music|song|spotify))?" + _END
                + r"|^play(\s+(the|my))?\s+(music|song|spotify)" + _END, re.I), "play", None),
    (re.compile(r"^(turn (it|the music|the volume) up|louder|volume up)" + _END, re.I), "volume", "+10"),
    (re.compile(r"^(turn (it|the music|the volume) down|quieter|volume down|lower the (music|volume))" + _END, re.I),
     "volume", "-10"),
    (re.compile(r"^(i (really )?(like|love) this (song|track)|like this (song|track)|save this (song|track))" + _END, re.I),
     "like", None),
    (re.compile(r"^(turn )?shuffle (on|off)" + _END, re.I), "shuffle", None),
]
_VOLUME = re.compile(r"^(set (the )?)?volume( to)? (\d{1,3})( ?%| percent)?" + _END, re.I)
_MAYBE = re.compile(r"\b(play|queue|put on|add|music|song|songs|track|playlist|album|spotify|podcast|listen)\b", re.I)

_SYSTEM = (
    "You turn a message into a music command for a media player. Reply with JSON only:\n"
    '{"action": "play" | "queue" | "none", "query": "what to search for", "kind": "track" | "playlist" | "album" | "artist" | "show"}\n'
    'Use "none" unless the message clearly asks to play or queue music, a song, an artist, a playlist, an album or a podcast. '
    'Games, videos, sports and pretend play are "none".\n'
    'Examples: "queue chill instrumental music" -> {"action":"queue","query":"chill instrumental","kind":"playlist"}\n'
    '"play bohemian rhapsody" -> {"action":"play","query":"bohemian rhapsody","kind":"track"}\n'
    '"put on some lofi" -> {"action":"play","query":"lofi","kind":"playlist"}\n'
    '"play some taylor swift" -> {"action":"play","query":"taylor swift","kind":"artist"}\n'
    '"let\'s play a game" -> {"action":"none"}\n'
    '"what song is this" -> {"action":"none"}'
)


def _ask_model(text: str) -> dict:
    model = getattr(constants, "ROUTER_MODEL", None) or getattr(constants, "GAME_AGENT_MODEL", "qwen3.5:9b")
    try:
        from modules.model_router import is_loaded
        timeout = MODEL_TIMEOUT_S if is_loaded(model) else COLD_TIMEOUT_S
    except Exception:
        timeout = MODEL_TIMEOUT_S
    r = requests.post(f"{constants.LLM_ENDPOINT.rstrip('/')}/api/chat", timeout=timeout, json={
        "model": model, "stream": False, "format": "json", "keep_alive": "10m",
        "think": False,                            # qwen3.5 thinks by default: 60 tokens would go there
        "messages": [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": text[:300]}],
        "options": {"temperature": 0, "num_predict": 60}})
    r.raise_for_status()
    return json.loads(((r.json() or {}).get("message") or {}).get("content") or "{}")


def parse(text: str, ask=None) -> Optional[Intent]:
    t = " ".join(str(text or "").split())
    if not t or len(t) > 200:
        return None
    for rx, action, value in _EXACT:
        m = rx.match(t)
        if m:
            if action == "shuffle":
                value = m.group(2).lower() == "on"
            return Intent(action, value=value)
    m = _VOLUME.match(t)
    if m:
        return Intent("volume", value=str(min(100, int(m.group(4)))))
    if not _MAYBE.search(t) or getattr(constants, "GAMING_MODE", False):
        return None
    try:
        data = (ask or _ask_model)(t)
    except Exception:
        return None                                    # Ollama down: it's just a chat message
    action = str((data or {}).get("action") or "none").lower()
    query = " ".join(str((data or {}).get("query") or "").split())[:120]
    if action not in ("play", "queue") or not query:
        return None
    kind = str((data or {}).get("kind") or "track").lower()
    return Intent(f"{action}_query", query=query, kind=kind if kind in KINDS else "track")


def _describe(row: dict) -> str:
    sub = f" by {row['subtitle']}" if row.get("subtitle") and row.get("kind") in ("track", "album") else ""
    return f"“{row.get('title', '')}”{sub}"


def handle(text: str, bridge=None, ask=None) -> Optional[str]:
    """Run a music request. Returns Lumi's reply, or None when it isn't one."""
    intent = parse(text, ask=ask)
    if intent is None:
        return None
    from modules import media_bridge
    b = bridge or media_bridge.bridge()
    try:
        if intent.action in ("play_query", "queue_query"):
            from modules import spotify
            if not spotify.connected():
                return "I need Spotify connected for that. You can connect it on the Integrations page."
            kinds = ("track",) if intent.action == "queue_query" and intent.kind == "track" else (intent.kind,)
            if intent.action == "queue_query" and intent.kind != "track":
                kinds = ("track",)                             # only songs can go in a queue
            rows = spotify.search(intent.query, kinds=kinds, limit=1)
            if not rows:
                return f"I couldn't find anything on Spotify for “{intent.query}”."
            row = rows[0]
            if intent.action == "queue_query":
                b.command("queue_uri", row["uri"])
                return f"Queued {_describe(row)}."
            b.command("play_uri", row["uri"])
            return f"Playing {_describe(row)}."
        if intent.action == "shuffle":
            cur = b.state(watch=False)
            if cur.get("shuffle") is not None and bool(cur.get("shuffle")) == bool(intent.value):
                return f"Shuffle is already {'on' if intent.value else 'off'}."
        msg, st = b.command(intent.action, intent.value)
        return {
            "next": "Skipping.", "previous": "Going back a song.", "pause": "Paused.", "play": "Resuming.",
            "like": (f"Saved “{st.get('title', '')}” to your Liked Songs." if st.get("liked")
                     else "Removed it from your Liked Songs."),
            "shuffle": "Shuffle on." if st.get("shuffle") else "Shuffle off.",
        }.get(intent.action) or msg or "Done."
    except Exception as e:
        return f"I couldn't do that: {e}"

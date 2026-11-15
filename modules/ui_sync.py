"""
One place that tells every open UI (web app tabs over socket.io, Lumi Islands over
the approval server's WebSocket) that shared state changed:

    theme_update            {"theme": "sakura"}
    hybrid_mode_update      hybrid_mode.status()
    knowledge_ingest_start  {"id", "file", "size", "source"}
    knowledge_ingest_finish {"id", "file", "ok", "chunks", "error", "source"}

broadcast() is thread-safe and never raises: a UI that isn't running just misses it,
and reads the current state when it connects (GET /api/ui/state, island "ui_state").
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

_web: Optional[Callable[[str, Dict[str, Any]], None]] = None


def set_web_broadcaster(fn: Callable[[str, Dict[str, Any]], None]) -> None:
    """socketIO.py registers its thread-safe emit-to-everyone here."""
    global _web
    _web = fn


def broadcast(event: str, payload: Dict[str, Any]) -> None:
    try:
        from modules import hitl_server
        hitl_server.broadcast_threadsafe({"type": event, **payload})
    except Exception:
        pass
    if _web is not None:
        try:
            _web(event, payload)
        except Exception:
            pass

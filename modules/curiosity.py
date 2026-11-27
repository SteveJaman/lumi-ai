"""
Idle curiosity (Phase 5): while you're away, Lumi may read up on something and keep one
or two notes for later. Strictly read-only: web search and page reads, never a form,
never a login, never a POST.

    should_run(idle_s, gaming, idle_minutes=10) -> bool   idle long enough and not gaming
    safe_url(url) -> bool                                  https only, no private or local hosts
    pick_topic(pool, rng) -> str | None                    a seed from interests, memory or curated feeds
    CuriosityStore(path)                                   discoveries, tagged source=idle_curiosity,
                                                           surfaced=0 until Lumi mentions them
    run_once(store, pool, search, now, ...) -> int         one pass, with the search injected

The search function is passed in (the web_search module in production), so this file never
reaches the network by itself, and the tests need none.
"""

from __future__ import annotations

import ipaddress
import os
import random
import sqlite3
import threading
import time
from typing import Callable, Iterable, List, Optional
from urllib.parse import urlparse

from constants import DATA_ROOT

IDLE_CURIOSITY_MINUTES = 10
PATH = os.path.join(DATA_ROOT, "curiosity.db")
MAX_NOTES_PER_RUN = 2
_BLOCKED_HOSTS = {"localhost", "localhost.localdomain"}


def should_run(idle_s: float, gaming: bool, idle_minutes: float = IDLE_CURIOSITY_MINUTES) -> bool:
    return (not gaming) and float(idle_s) >= float(idle_minutes) * 60.0


def safe_url(url: str) -> bool:
    try:
        u = urlparse(str(url or ""))
    except ValueError:
        return False
    if u.scheme != "https" or not u.hostname:
        return False
    host = u.hostname.lower()
    if host in _BLOCKED_HOSTS or host.endswith(".local") or host.endswith(".internal"):
        return False
    try:
        ip = ipaddress.ip_address(host)
        return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast)
    except ValueError:
        return True                       # a name, not a literal address


def pick_topic(pool: Iterable[str], rng: Optional[random.Random] = None) -> Optional[str]:
    items = [str(p).strip() for p in pool if str(p).strip()]
    if not items:
        return None
    return (rng or random).choice(items)


class CuriosityStore:
    def __init__(self, path: str = PATH):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS discoveries ("
            " id INTEGER PRIMARY KEY, topic TEXT, note TEXT, url TEXT,"
            " source TEXT NOT NULL DEFAULT 'idle_curiosity', surfaced INTEGER NOT NULL DEFAULT 0,"
            " timestamp REAL NOT NULL)")
        self._conn.commit()

    def add(self, topic: str, note: str, url: str, now: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO discoveries (topic, note, url, source, surfaced, timestamp)"
                " VALUES (?, ?, ?, 'idle_curiosity', 0, ?)", (topic, note[:600], url[:300], now))
            self._conn.commit()

    def unsurfaced(self, limit: int = 5) -> List[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, topic, note, url, timestamp FROM discoveries WHERE surfaced = 0"
                " ORDER BY timestamp DESC LIMIT ?", (int(limit),)).fetchall()
        return [{"id": r[0], "topic": r[1], "note": r[2], "url": r[3], "timestamp": r[4]} for r in rows]

    def mark_surfaced(self, ids: Iterable[int]) -> None:
        with self._lock:
            self._conn.executemany("UPDATE discoveries SET surfaced = 1 WHERE id = ?", [(int(i),) for i in ids])
            self._conn.commit()


def run_once(store: CuriosityStore, pool: Iterable[str], search: Callable[[str], List[dict]],
             now: Optional[float] = None, rng: Optional[random.Random] = None) -> int:
    """One pass: pick a topic, search, keep up to MAX_NOTES_PER_RUN safe results. Returns how many were kept."""
    topic = pick_topic(pool, rng)
    if not topic:
        return 0
    now = time.time() if now is None else now
    kept = 0
    for hit in search(topic) or []:
        url = str(hit.get("href") or hit.get("url") or "")          # web_search gives href/body
        note = str(hit.get("body") or hit.get("snippet") or hit.get("title") or "").strip()
        if not note or not safe_url(url):
            continue
        store.add(topic, note, url, now)
        kept += 1
        if kept >= MAX_NOTES_PER_RUN:
            break
    return kept

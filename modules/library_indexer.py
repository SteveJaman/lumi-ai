"""
Library indexer — RAG over user-supplied documents.

Scans data/library/ for PDF, TXT, MD, DOCX, and JSON files. On startup
and every RESCAN_INTERVAL seconds, checks each file's
(path, mtime, size) fingerprint. New or changed files are chunked and
handed to the Memory module, which stores them in a dedicated
ChromaDB collection named `library_docs`.

The Memory module owns the collection. This module just walks the
folder and hands chunks over.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from typing import List, Optional, Tuple

from modules.module import Module
from modules.log_hub import emit as hub_emit
from constants import DATA_ROOT


# ─── TUNING ─────────────────────────────────────────────────────
LIBRARY_DIR = os.path.join(DATA_ROOT, "library")
SUPPORTED_EXTS = {".pdf", ".txt", ".md", ".docx", ".json"}
CHUNK_SIZE = 800
CHUNK_OVERLAP = 100
RESCAN_INTERVAL = 60                 # seconds between rescans
MAX_FILE_BYTES = 50 * 1024 * 1024    # skip files over 50 MB


def _fingerprint(path: str) -> str:
    """Stable hash of (path, mtime, size). Changes when the file
    changes; stays the same otherwise."""
    try:
        st = os.stat(path)
    except OSError:
        return ""
    raw = f"{os.path.abspath(path)}|{st.st_mtime_ns}|{st.st_size}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _chunk_text(text: str, size: int = CHUNK_SIZE,
                overlap: int = CHUNK_OVERLAP) -> List[str]:
    """Slide a window over the text with overlap. Breaks on whitespace
    near the boundary when possible so chunks don't split mid-word."""
    if not text:
        return []
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []

    chunks: List[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:
            window = text[max(0, end - 40):end]
            for sep in ("\n\n", ". ", "\n", " "):
                idx = window.rfind(sep)
                if idx != -1:
                    end = max(0, end - 40) + idx + len(sep)
                    break
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= n:
            break
        start = max(0, end - overlap)
        if start >= end:
            start = end
    return chunks


class LibraryIndexer(Module):
    """Background indexer. Spawned by main.py like any other module."""

    def __init__(self, signals, enabled: bool = True,
                 rescan_interval: int = RESCAN_INTERVAL):
        super().__init__(signals, enabled)
        self.rescan_interval = rescan_interval
        self._known: dict = {}   # path -> fingerprint

    # ─── File extraction ────────────────────────────────────
    def _extract_text(self, path: str) -> Optional[str]:
        ext = os.path.splitext(path)[1].lower()

        try:
            if ext == ".pdf":
                from modules.agent_tools import _read_pdf
                return _read_pdf(path)
            if ext in (".docx", ".doc"):
                from modules.agent_tools import _read_docx
                return _read_docx(path)
            if ext in (".txt", ".md", ".json"):
                with open(path, "r", encoding="utf-8",
                          errors="replace") as f:
                    return f.read()
        except Exception as e:
            hub_emit(self.signals, "memory",
                     f"library: failed to read {os.path.basename(path)} — "
                     f"{type(e).__name__}: {e}", level="warn")
            return None

        hub_emit(self.signals, "memory",
                 f"library: unsupported extension {ext} for "
                 f"{os.path.basename(path)}", level="warn")
        return None

    # ─── Indexing ────────────────────────────────────────────
    def _scan_once(self) -> Tuple[int, int]:
        if not os.path.isdir(LIBRARY_DIR):
            try:
                os.makedirs(LIBRARY_DIR, exist_ok=True)
            except Exception:
                return (0, 0)

        mem = getattr(self.signals, "memory_module", None)
        if mem is None or not hasattr(mem, "API"):
            return (0, 0)

        indexed = 0
        skipped = 0

        for name in sorted(os.listdir(LIBRARY_DIR)):
            path = os.path.join(LIBRARY_DIR, name)
            if not os.path.isfile(path):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in SUPPORTED_EXTS:
                continue

            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if size > MAX_FILE_BYTES:
                hub_emit(self.signals, "memory",
                         f"library: skipping {name} ({size} bytes — "
                         f"over {MAX_FILE_BYTES} limit)", level="warn")
                continue
            if size == 0:
                continue

            fp = _fingerprint(path)
            if not fp:
                continue
            if self._known.get(path) == fp:
                skipped += 1
                continue

            text = self._extract_text(path)
            if not text:
                continue

            chunks = _chunk_text(text)
            if not chunks:
                continue

            try:
                mem.API.index_library_file(
                    filename=name,
                    file_path=path,
                    fingerprint=fp,
                    chunks=chunks,
                )
                self._known[path] = fp
                indexed += 1
                hub_emit(self.signals, "memory",
                         f"library: indexed '{name}' "
                         f"({len(chunks)} chunks, {len(text)} chars)")
            except Exception as e:
                hub_emit(self.signals, "memory",
                         f"library: index failed for {name} — "
                         f"{type(e).__name__}: {e}", level="error")

        return (indexed, skipped)

    # ─── Module contract ────────────────────────────────────
    async def run(self):
        if not self.enabled:
            return

        loop = asyncio.get_running_loop()

        try:
            indexed, skipped = await loop.run_in_executor(
                None, self._scan_once
            )
            hub_emit(self.signals, "memory",
                     f"library: startup scan — {indexed} indexed, "
                     f"{skipped} unchanged")
        except Exception as e:
            hub_emit(self.signals, "memory",
                     f"library: startup scan failed: {e}",
                     level="error")

        while not self.signals.terminate:
            await asyncio.sleep(self.rescan_interval)
            if self.signals.terminate:
                break
            try:
                await loop.run_in_executor(None, self._scan_once)
            except Exception as e:
                hub_emit(self.signals, "memory",
                         f"library: rescan failed: {e}", level="warn")
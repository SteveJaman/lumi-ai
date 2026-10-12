"""
Library indexer — RAG over user-supplied documents.

Scans data/library/ recursively. Files are organized into subfolders
by type (documents/, images/, text/), but the indexer doesn't care
about the folder layout — it just walks every file it can find and
chunks the ones it understands.

The ChromaDB `source` field for each chunk is the file's path
relative to the library root, e.g. "documents/report.pdf". That
keeps citations unambiguous even when two files share a basename.

Deletion handling: every scan compares the current filesystem state
against the last scan. Files that used to be known but no longer
exist get their chunks removed from ChromaDB in the same pass.
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
SUPPORTED_EXTS = {".pdf", ".txt", ".md", ".docx", ".doc", ".json"}
CHUNK_SIZE = 800
CHUNK_OVERLAP = 100
RESCAN_INTERVAL = 60
MAX_FILE_BYTES = 50 * 1024 * 1024


def _fingerprint(path: str) -> str:
    try:
        st = os.stat(path)
    except OSError:
        return ""
    raw = f"{os.path.abspath(path)}|{st.st_mtime_ns}|{st.st_size}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _chunk_text(text: str, size: int = CHUNK_SIZE,
                overlap: int = CHUNK_OVERLAP) -> List[str]:
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

def index_single_file(signals, path):
    """Immediately index one file into the library collection.

    Used by the upload handler so a freshly uploaded file becomes
    searchable on the very next turn instead of waiting up to
    RESCAN_INTERVAL seconds for the next background scan.

    Safe to call redundantly — the walker will later find the same
    file and re-index it, but `index_library_file` upserts by
    deterministic IDs so there's no duplication.

    Returns True on success.
    """
    mem = getattr(signals, "memory_module", None)
    if mem is None or not hasattr(mem, "API"):
        return False
    if not os.path.isfile(path):
        return False

    ext = os.path.splitext(path)[1].lower()
    if ext not in SUPPORTED_EXTS:
        return False

    try:
        size = os.path.getsize(path)
    except OSError:
        return False
    if size == 0 or size > MAX_FILE_BYTES:
        return False

    # Extract text using the same branch the walker uses.
    try:
        if ext == ".pdf":
            from modules.agent_tools import _read_pdf
            text = _read_pdf(path)
        elif ext in (".docx", ".doc"):
            from modules.agent_tools import _read_docx
            text = _read_docx(path)
        elif ext in (".txt", ".md", ".json"):
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        else:
            return False
    except Exception as e:
        hub_emit(signals, "memory",
                 f"library: extract failed for {path} — {e}",
                 level="warn")
        return False

    if not text:
        return False

    chunks = _chunk_text(text)
    if not chunks:
        return False

    fp = _fingerprint(path)
    if not fp:
        return False

    source = os.path.relpath(path, LIBRARY_DIR).replace("\\", "/")
    try:
        mem.API.index_library_file(
            filename=source,
            file_path=path,
            fingerprint=fp,
            chunks=chunks,
        )
        return True
    except Exception as e:
        hub_emit(signals, "memory",
                 f"library: single-file index failed for {source} — {e}",
                 level="warn")
        return False

class LibraryIndexer(Module):
    """Background indexer. Spawned by main.py like any other module."""

    def __init__(self, signals, enabled: bool = True,
                 rescan_interval: int = RESCAN_INTERVAL):
        super().__init__(signals, enabled)
        self.rescan_interval = rescan_interval
        # path (absolute) -> fingerprint
        self._known: dict = {}

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
            hub_emit(
                self.signals, "memory",
                f"library: failed to read {os.path.basename(path)} — "
                f"{type(e).__name__}: {e}", level="warn",
            )
            return None
        return None

    # ─── Scan ──────────────────────────────────────────────
    def _walk_library(self) -> List[Tuple[str, str]]:
        """Return (absolute_path, source_rel_path) for every file in
        the library tree. Skips dotfiles and dotfolders."""
        out = []
        if not os.path.isdir(LIBRARY_DIR):
            return out
        for root, dirs, files in os.walk(LIBRARY_DIR):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for name in files:
                if name.startswith("."):
                    continue
                path = os.path.join(root, name)
                source = os.path.relpath(
                    path, LIBRARY_DIR
                ).replace("\\", "/")
                out.append((path, source))
        return out

    def _scan_once(self) -> Tuple[int, int, int]:
        try:
            os.makedirs(LIBRARY_DIR, exist_ok=True)
        except Exception:
            return (0, 0, 0)

        mem = getattr(self.signals, "memory_module", None)
        if mem is None or not hasattr(mem, "API"):
            return (0, 0, 0)

        indexed = 0
        skipped = 0
        removed = 0

        current_paths = set()
        current_pairs = self._walk_library()

        for path, source in current_pairs:
            current_paths.add(path)

            ext = os.path.splitext(path)[1].lower()
            if ext not in SUPPORTED_EXTS:
                continue

            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if size > MAX_FILE_BYTES or size == 0:
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
                    filename=source,
                    file_path=path,
                    fingerprint=fp,
                    chunks=chunks,
                )
                self._known[path] = fp
                indexed += 1
                hub_emit(
                    self.signals, "memory",
                    f"library: indexed '{source}' "
                    f"({len(chunks)} chunks, {len(text)} chars)",
                )
            except Exception as e:
                hub_emit(
                    self.signals, "memory",
                    f"library: index failed for {source} — "
                    f"{type(e).__name__}: {e}", level="error",
                )

        # ─── Cleanup: remove chunks for files that no longer exist ───
        stale = [p for p in self._known if p not in current_paths]
        for path in stale:
            try:
                source = os.path.relpath(
                    path, LIBRARY_DIR
                ).replace("\\", "/")
                if hasattr(mem.API, "delete_library_file"):
                    mem.API.delete_library_file(source)
                del self._known[path]
                removed += 1
                hub_emit(
                    self.signals, "memory",
                    f"library: removed chunks for deleted file "
                    f"'{source}'",
                )
            except Exception:
                # Keep the entry so we retry next scan
                pass

        return (indexed, skipped, removed)

    # ─── Module contract ────────────────────────────────────
    async def run(self):
        if not self.enabled:
            return

        loop = asyncio.get_running_loop()

        try:
            indexed, skipped, removed = await loop.run_in_executor(
                None, self._scan_once,
            )
            hub_emit(
                self.signals, "memory",
                f"library: startup scan — {indexed} indexed, "
                f"{skipped} unchanged, {removed} removed",
            )
        except Exception as e:
            hub_emit(
                self.signals, "memory",
                f"library: startup scan failed: {e}", level="error",
            )

        while not self.signals.terminate:
            await asyncio.sleep(self.rescan_interval)
            if self.signals.terminate:
                break
            try:
                await loop.run_in_executor(None, self._scan_once)
            except Exception as e:
                hub_emit(
                    self.signals, "memory",
                    f"library: rescan failed: {e}", level="warn",
                )
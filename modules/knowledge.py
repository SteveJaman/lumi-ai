"""
Drag-and-drop knowledge base: drop a file on the web app or an island and Lumi
learns it.

    web     POST /api/knowledge/upload       multipart, one or more files (host only)
    island  knowledge_upload_begin / _chunk / _end over its WebSocket: the file's bytes in
            base64 chunks (the socket caps messages at 64 KB), so it works the same on the
            desktop and on a Surface paired over Tailscale, where a desktop path would mean
            nothing

Each file is saved under data/library/ (documents/ for PDF and Word, text/ for text,
Markdown, JSON and code), then indexed into the library RAG right away
(library_indexer.index_single_file) so search_library finds it on the next turn.
Every UI hears knowledge_ingest_start / _finish (ui_sync), and when something was
learned Lumi says so out loud ("I just learned something new: ...").
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import secrets
import shutil
import threading
import time
from typing import Any, Dict, List, Optional

from modules import ui_sync
from modules.library_indexer import LIBRARY_DIR, SUPPORTED_EXTS, index_single_file
from modules.log_hub import emit as hub_emit

MAX_BYTES = 25 * 1024 * 1024
MAX_FILES = 10
DOC_EXTS = {".pdf", ".docx", ".doc"}
INCOMING = os.path.join(LIBRARY_DIR, ".incoming")
UPLOAD_TTL_S = 600


class KnowledgeError(ValueError):
    pass


def allowed(name: str) -> bool:
    return os.path.splitext(name or "")[1].lower() in SUPPORTED_EXTS


def safe_name(name: str) -> str:
    base = os.path.basename(str(name or "").replace("\\", "/"))
    stem, ext = os.path.splitext(base)
    stem = re.sub(r"[^\w .\-()]+", "_", stem).strip(" .")[:80] or "file"
    return stem + ext.lower()


def destination(name: str) -> str:
    """A free path in data/library/<documents|text>/ for this file name."""
    name = safe_name(name)
    folder = os.path.join(LIBRARY_DIR, "documents" if os.path.splitext(name)[1] in DOC_EXTS else "text")
    os.makedirs(folder, exist_ok=True)
    stem, ext = os.path.splitext(name)
    path, n = os.path.join(folder, name), 2
    while os.path.exists(path):
        path = os.path.join(folder, f"{stem} ({n}){ext}")
        n += 1
    return path


def check(name: str, size: int) -> None:
    if not allowed(name):
        exts = ", ".join(sorted(SUPPORTED_EXTS))
        raise KnowledgeError(f"Lumi can't read {os.path.splitext(name)[1] or 'that kind of file'} yet. Try: {exts}.")
    if size <= 0:
        raise KnowledgeError(f"{name} is empty.")
    if size > MAX_BYTES:
        raise KnowledgeError(f"{name} is over {MAX_BYTES // (1024 * 1024)} MB.")


def _announce(signals, learned: List[Dict[str, Any]]) -> None:
    """Lumi says she learned something (spoken by the prompter, no model call)."""
    if not learned or signals is None or not hasattr(signals, "history"):
        return
    names = [os.path.splitext(x["file"])[0] for x in learned]
    what = names[0] if len(names) == 1 else f"{len(names)} new files"
    chunks = sum(x["chunks"] for x in learned)
    signals.history.append({
        "role": "user", "source": "announce_event", "hidden": True,
        "content": f"[KNOWLEDGE EVENT] Learned {', '.join(x['file'] for x in learned)} ({chunks} passages).",
        "say": f"[happy] I just learned something new: {what}! Ask me about it anytime.",
    })
    signals.new_message = True


def ingest_files(signals, paths: List[str], source: str) -> List[Dict[str, Any]]:
    """Index saved library files (blocking; run in an executor). Broadcasts progress."""
    results = []
    for path in paths:
        fid = secrets.token_hex(6)
        name = os.path.basename(path)
        size = os.path.getsize(path) if os.path.isfile(path) else 0
        ui_sync.broadcast("knowledge_ingest_start", {"id": fid, "file": name, "size": size, "source": source})
        t0 = time.time()
        try:
            chunks = int(index_single_file(signals, path) or 0)
            ok, error = chunks > 0, None if chunks > 0 else "nothing readable in it (or memory isn't loaded)"
        except Exception as e:                       # never let one file stop the rest
            chunks, ok, error = 0, False, f"{type(e).__name__}: {e}"
        res = {"id": fid, "file": name, "ok": ok, "chunks": chunks, "error": error, "source": source,
               "seconds": round(time.time() - t0, 2),
               "path": os.path.relpath(path, LIBRARY_DIR).replace("\\", "/")}
        ui_sync.broadcast("knowledge_ingest_finish", res)
        hub_emit(signals, "memory", f"knowledge: {name} -> {chunks} chunks" if ok else f"knowledge: {name} failed: {error}",
                 level="info" if ok else "warn")
        results.append(res)
    _announce(signals, [r for r in results if r["ok"]])
    return results


def save_bytes(name: str, data: bytes) -> str:
    check(name, len(data))
    path = destination(name)
    with open(path, "wb") as f:
        f.write(data)
    return path


# ─── web: POST /api/knowledge/upload ──────────────────────────────────

def register(router, server) -> None:
    from aiohttp import web
    from modules.socketIO import _json_error

    async def upload(request):
        if not server._origin_ok(request) or not server._current_user(request):
            return _json_error(401, "Not logged in.")
        ok, err = server._require_csrf_header(request)
        if not ok:
            return _json_error(403, err)
        try:
            reader = await request.multipart()
        except Exception:
            return _json_error(400, "Send the files as multipart/form-data.")
        saved, rejected = [], []
        while True:
            part = await reader.next()
            if part is None:
                break
            if not part.filename:
                continue
            if len(saved) + len(rejected) >= MAX_FILES:
                rejected.append({"file": part.filename, "error": f"At most {MAX_FILES} files at once."})
                continue
            buf = bytearray()
            too_big = False
            while True:
                chunk = await part.read_chunk(256 * 1024)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > MAX_BYTES:
                    too_big = True
                    break
            try:
                if too_big:
                    raise KnowledgeError(f"{part.filename} is over {MAX_BYTES // (1024 * 1024)} MB.")
                saved.append(save_bytes(part.filename, bytes(buf)))
            except KnowledgeError as e:
                rejected.append({"file": safe_name(part.filename), "error": str(e)})
        if not saved and not rejected:
            return _json_error(400, "No files in the upload.")
        results = await asyncio.get_running_loop().run_in_executor(
            None, ingest_files, server.signals, saved, "web")
        return web.json_response({
            "files": results + [{"file": r["file"], "ok": False, "chunks": 0, "error": r["error"]} for r in rejected],
            "chunks": sum(r["chunks"] for r in results),
        })

    router.add_post("/api/knowledge/upload", upload)


# ─── island: chunked upload over the WebSocket ────────────────────────

class _Upload:
    def __init__(self, name: str, size: int):
        self.name, self.size = name, size
        self.received = 0
        self.started = time.time()
        os.makedirs(INCOMING, exist_ok=True)
        self.tmp = os.path.join(INCOMING, secrets.token_hex(8) + ".part")
        self.fh = open(self.tmp, "wb")

    def close(self, keep: bool = False) -> Optional[str]:
        try:
            self.fh.close()
        except Exception:
            pass
        if not keep:
            try:
                os.remove(self.tmp)
            except OSError:
                pass
            return None
        path = destination(self.name)
        shutil.move(self.tmp, path)
        return path


_uploads: Dict[str, _Upload] = {}
_uploads_lock = threading.Lock()


def _sweep() -> None:
    now = time.time()
    with _uploads_lock:
        for uid in [u for u, up in _uploads.items() if now - up.started > UPLOAD_TTL_S]:
            _uploads.pop(uid).close()


def install_island_handlers(signals, server=None) -> None:
    """Register the island's upload messages on the approval server."""
    from modules import hitl_server
    reg = server.register_handler if server else hitl_server.register_handler
    send = server.send if server else hitl_server.send

    async def begin(ws, data):
        _sweep()
        name, size = safe_name(data.get("name") or ""), int(data.get("size") or 0)
        try:
            check(name, size)
        except KnowledgeError as e:
            return await send(ws, {"type": "knowledge_upload", "id": data.get("id"), "ok": False, "error": str(e)})
        uid = secrets.token_hex(8)
        with _uploads_lock:
            if len(_uploads) >= MAX_FILES:
                return await send(ws, {"type": "knowledge_upload", "id": data.get("id"), "ok": False,
                                       "error": "Too many uploads at once."})
            _uploads[uid] = _Upload(name, size)
        await send(ws, {"type": "knowledge_upload", "id": data.get("id"), "ok": True, "upload": uid})

    async def chunk(ws, data):
        with _uploads_lock:
            up = _uploads.get(str(data.get("upload") or ""))
        if up is None:
            return await send(ws, {"type": "knowledge_upload", "id": data.get("id"), "ok": False,
                                   "error": "That upload expired. Drop the file again."})
        try:
            raw = base64.b64decode(str(data.get("data") or ""), validate=True)
        except (ValueError, TypeError):
            return await send(ws, {"type": "knowledge_upload", "id": data.get("id"), "ok": False, "error": "Bad chunk."})
        if up.received + len(raw) > up.size:
            with _uploads_lock:
                _uploads.pop(str(data.get("upload")), None)
            up.close()
            return await send(ws, {"type": "knowledge_upload", "id": data.get("id"), "ok": False,
                                   "error": "The file is bigger than announced."})
        up.fh.write(raw)
        up.received += len(raw)
        await send(ws, {"type": "knowledge_upload", "id": data.get("id"), "ok": True, "received": up.received})

    async def end(ws, data):
        with _uploads_lock:
            up = _uploads.pop(str(data.get("upload") or ""), None)
        if up is None:
            return await send(ws, {"type": "knowledge_upload", "id": data.get("id"), "ok": False,
                                   "error": "That upload expired. Drop the file again."})
        if up.received != up.size:
            up.close()
            return await send(ws, {"type": "knowledge_upload", "id": data.get("id"), "ok": False,
                                   "error": "The upload was cut short."})
        path = up.close(keep=True)
        results = await asyncio.get_running_loop().run_in_executor(None, ingest_files, signals, [path], "island")
        await send(ws, {"type": "knowledge_upload", "id": data.get("id"), "ok": results[0]["ok"],
                        "result": results[0], "error": results[0]["error"]})

    reg("knowledge_upload_begin", begin)
    reg("knowledge_upload_chunk", chunk)
    reg("knowledge_upload_end", end)

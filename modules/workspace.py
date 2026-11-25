"""
Split-view workspace: the web client's right-hand pane (Browser, Terminal,
Files tabs).

HTTP (host-only, logged in, same origin):
  GET /api/workspace/files                 JSON tree of ./data/
  GET /api/workspace/files/content?path=   one text file from ./data/

Socket.io (only to tabs that sent ``workspace_join``; host-only):
  terminal_output  {text, stream: "stdout"|"stderr"|"info", ts}
  browser_frame    {data: <base64 jpeg>, url, ts}       ~5 fps while the deal finder runs
  browser_status   {message, active, ts}

  Model Pipeline tab (modules/claude_router.py, modules/autonomous_agent.py):
  claude_model_switch  {model: "haiku"|"sonnet"|"opus", model_id, reason, tier_cost, ts}
  claude_token_stream  {tokens, input_tokens, output_tokens, model, model_id,
                        classifier, totals: {tokens, cost_usd, opus_equivalent_usd, saved_usd, ...}, ts}
  autonomous_step      {step, phase: plan|execute|verify|done, status, tool?, session_id, ts}
  model_route          {tier, model, where: "local"|"claude"|"cloud", reason, mode, ts}
                       every chat / agent turn, local or not, so the tab shows what really ran
  model_pipeline_state {switch, tokens, steps, route, usage, router}   sent once on workspace_join:
                       usage = data/claude_usage.json totals, router = is Claude in use and why not

The file explorer is sandboxed to DATA_ROOT: paths are resolved with
realpath (symlinks and junctions followed first) and must stay inside it.
Sign-ins, cached mail, the browser profile, the user database and anything
that looks like a key or token are never listed or served, even though they
live under data/.

Every ``emit_*`` function is safe to call from any thread and is a no-op
when no tab is watching, so callers never pay for frames nobody sees.
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from aiohttp import web

from constants import DATA_ROOT

ROOM = "workspace"

# Top-level folders under data/ that hold secrets or private data.
PRIVATE_DIRS = frozenset({"credentials", "workspace", "browser_profile", "memories"})
# File names never listed or served, wherever they are.
PRIVATE_PATTERNS = (
    "*.db", "*.db-*", "*.sqlite", "*.sqlite3", "*.key", "*.pem", "*.pfx", "*.p12",
    ".env", ".env.*", "*token*", "*secret*", "*password*", "*credential*", "cookies*",
)
SKIP_NAMES = frozenset({"__pycache__", ".git"})

MAX_DEPTH = 8
MAX_ENTRIES = 3000
MAX_FILE_BYTES = 512 * 1024

_lock = threading.Lock()
_server = None                    # SocketIOServer, set by install()
_viewers: set = set()
_last_frame: Optional[Dict[str, Any]] = None
_last_status: Optional[Dict[str, Any]] = None
_last_switch: Optional[Dict[str, Any]] = None
_last_tokens: Optional[Dict[str, Any]] = None
_last_route: Optional[Dict[str, Any]] = None
_recent_steps: List[Dict[str, Any]] = []
MAX_RECENT_STEPS = 30


# ─── sandbox ─────────────────────────────────────────────────────────

def _root() -> str:
    return os.path.realpath(DATA_ROOT)


def _is_private_name(name: str) -> bool:
    low = name.lower()
    return any(fnmatch.fnmatch(low, pat) for pat in PRIVATE_PATTERNS)


def resolve_data_path(rel: Any) -> str:
    """Relative path under data/ -> absolute real path. Raises ValueError on
    anything that escapes data/ or names private data."""
    rel = str(rel or "").strip()
    if not rel or "\x00" in rel:
        raise ValueError("No file path given.")
    rel = rel.replace("\\", "/")
    if rel.startswith("/") or (len(rel) > 1 and rel[1] == ":"):
        raise ValueError("Paths must be relative to data/.")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ValueError("Path traversal is not allowed.")
    if not parts:
        raise ValueError("No file path given.")
    if parts[0].lower() in PRIVATE_DIRS or any(_is_private_name(p) for p in parts):
        raise ValueError("That file is private.")
    root = _root()
    target = os.path.realpath(os.path.join(root, *parts))
    t, r = os.path.normcase(target), os.path.normcase(root)
    if not t.startswith(r + os.sep):
        raise ValueError("Path is outside data/.")
    # A symlink inside data/ may land in a private folder: check the real path too.
    first = os.path.relpath(target, root).split(os.sep)[0].lower()
    if first in PRIVATE_DIRS:
        raise ValueError("That file is private.")
    return target


def build_tree(root: Optional[str] = None) -> Dict[str, Any]:
    """{name, path, type: "dir", children: [...]} for data/, skipping private
    entries and symlinks. Capped at MAX_DEPTH / MAX_ENTRIES."""
    root = os.path.realpath(root or DATA_ROOT)
    count = [0]

    def walk(abs_dir: str, rel: str, depth: int) -> List[Dict[str, Any]]:
        try:
            entries = sorted(os.scandir(abs_dir), key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
        except OSError:
            return []
        out = []
        for e in entries:
            if count[0] >= MAX_ENTRIES:
                break
            name = e.name
            if name in SKIP_NAMES or name.startswith(".") or _is_private_name(name):
                continue
            if depth == 0 and name.lower() in PRIVATE_DIRS:
                continue
            try:
                if e.is_symlink():
                    continue
                is_dir = e.is_dir(follow_symlinks=False)
            except OSError:
                continue
            path = f"{rel}/{name}" if rel else name
            count[0] += 1
            if is_dir:
                node = {"name": name, "path": path, "type": "dir"}
                node["children"] = walk(e.path, path, depth + 1) if depth + 1 < MAX_DEPTH else []
            else:
                try:
                    size = e.stat(follow_symlinks=False).st_size
                except OSError:
                    size = 0
                node = {"name": name, "path": path, "type": "file", "size": size}
            out.append(node)
        return out

    tree = {"name": "data", "path": "", "type": "dir", "children": walk(root, "", 0) if os.path.isdir(root) else []}
    tree["truncated"] = count[0] >= MAX_ENTRIES
    return tree


def read_data_file(rel: Any) -> Dict[str, Any]:
    target = resolve_data_path(rel)
    if not os.path.isfile(target):
        raise FileNotFoundError("No such file.")
    size = os.path.getsize(target)
    with open(target, "rb") as f:
        raw = f.read(MAX_FILE_BYTES)
    if b"\x00" in raw[:8192]:
        raise ValueError("Binary file; not shown.")
    return {"path": str(rel).replace("\\", "/"), "size": size,
            "truncated": size > MAX_FILE_BYTES,
            "content": raw.decode("utf-8", errors="replace")}


# ─── HTTP ────────────────────────────────────────────────────────────

def register(router, server) -> None:
    from modules.socketIO import _json_error

    def gate(request):
        if not server._origin_ok(request):
            return _json_error(403, "Bad origin.")
        if not server._current_user(request):
            return _json_error(401, "Not logged in.")
        if not server._is_local(request):
            return _json_error(403, "The workspace is host-only.")
        return None

    async def files(request):
        err = gate(request)
        if err is not None:
            return err
        tree = await asyncio.get_running_loop().run_in_executor(None, build_tree)
        return web.json_response(tree, headers={"Cache-Control": "no-store"})

    async def file_content(request):
        err = gate(request)
        if err is not None:
            return err
        rel = request.query.get("path", "")
        try:
            body = await asyncio.get_running_loop().run_in_executor(None, read_data_file, rel)
        except FileNotFoundError as e:
            return _json_error(404, str(e))
        except (ValueError, OSError) as e:
            return _json_error(400, str(e))
        return web.json_response(body, headers={"Cache-Control": "no-store"})

    router.add_get("/api/workspace/files", files)
    router.add_get("/api/workspace/files/content", file_content)


# ─── socket.io ───────────────────────────────────────────────────────

def install(server) -> None:
    """Register the join/leave events on the server's socket.io instance."""
    global _server
    _server = server
    sio = server.sio

    @sio.event
    async def workspace_join(sid, data=None):
        sess = await sio.get_session(sid)
        if not sess or not sess.get("user") or not sess.get("is_local"):
            return
        r = sio.enter_room(sid, ROOM)
        if asyncio.iscoroutine(r):
            await r
        with _lock:
            _viewers.add(sid)
            frame, status = _last_frame, _last_status
            pipeline = {"switch": _last_switch, "tokens": _last_tokens, "steps": list(_recent_steps),
                        "route": _last_route}
        pipeline.update(await asyncio.get_running_loop().run_in_executor(None, pipeline_extras))
        await sio.emit("model_pipeline_state", pipeline, to=sid)
        if status:
            await sio.emit("browser_status", status, to=sid)
        if frame:
            await sio.emit("browser_frame", frame, to=sid)

    @sio.event
    async def workspace_leave(sid, data=None):
        forget_sid(sid)
        r = sio.leave_room(sid, ROOM)
        if asyncio.iscoroutine(r):
            await r


def forget_sid(sid) -> None:
    with _lock:
        _viewers.discard(sid)


def has_viewers() -> bool:
    with _lock:
        return bool(_viewers) and _server is not None


def _emit(event: str, payload: Dict[str, Any]) -> None:
    server = _server
    if server is None or not has_viewers():
        return
    loop = getattr(server, "_loop", None)
    if loop is None or loop.is_closed():
        return

    async def send():
        try:
            await server.sio.emit(event, payload, room=ROOM)
        except Exception:
            pass
    try:
        asyncio.run_coroutine_threadsafe(send(), loop)
    except RuntimeError:
        pass


def emit_terminal(text: str, stream: str = "stdout") -> None:
    if text:
        _emit("terminal_output", {"text": str(text), "stream": stream, "ts": time.time()})


def emit_browser_frame(b64_jpeg: str, url: str = "") -> None:
    global _last_frame
    frame = {"data": b64_jpeg, "url": url, "ts": time.time()}
    with _lock:
        _last_frame = frame
    _emit("browser_frame", frame)


def emit_browser_status(message: str, active: Optional[bool] = None) -> None:
    global _last_status
    status = {"message": str(message), "ts": time.time()}
    if active is not None:
        status["active"] = bool(active)
    with _lock:
        _last_status = status
    _emit("browser_status", status)


# ─── Model Pipeline ──────────────────────────────────────────────────
# Kept even with nobody watching (they're tiny), so a tab opened mid-run catches up.

def emit_model_switch(tier: str, model_id: str, reason: str, tier_cost: str) -> None:
    global _last_switch
    ev = {"model": str(tier), "model_id": str(model_id), "reason": str(reason)[:240],
          "tier_cost": str(tier_cost), "ts": time.time()}
    with _lock:
        _last_switch = ev
    _emit("claude_model_switch", ev)


def emit_token_usage(tier: str, model_id: str, input_tokens: int, output_tokens: int,
                     totals: Optional[Dict[str, Any]] = None, classifier: bool = False) -> None:
    global _last_tokens
    ev = {"tokens": int(input_tokens) + int(output_tokens), "input_tokens": int(input_tokens),
          "output_tokens": int(output_tokens), "model": str(tier), "model_id": str(model_id),
          "classifier": bool(classifier), "totals": dict(totals or {}), "ts": time.time()}
    with _lock:
        _last_tokens = ev
    _emit("claude_token_stream", ev)


def router_status() -> Dict[str, Any]:
    """Whether chat can reach the Claude router right now, and if not, why."""
    from modules import hybrid_mode
    st = hybrid_mode.status()
    spec = (st.get("cloud_model") or "").strip()
    auto = spec.lower() == "anthropic:auto"
    if not st.get("enabled"):
        state, why = "off", "Cloud Escalation is off, so every turn runs on your local models."
    elif not st.get("cloud_ready"):
        state, why = "off", st.get("cloud_problem") or "The cloud model isn't ready."
    elif not auto:
        state, why = "off", f"Escalation goes straight to {spec}, not the router."
    else:
        state, why = "ready", "Hard (opus-tier) prompts go to the Claude router."
    return {"state": state, "reason": why, "cloud_model": spec}


def pipeline_extras() -> Dict[str, Any]:
    """Saved usage and router state for a tab that just opened. Never raises."""
    out: Dict[str, Any] = {}
    try:
        from modules import claude_router
        u = claude_router.usage_summary()
        out["usage"] = dict(u["totals"], calls_by_tier={t: u["tiers"][t]["calls"] for t in u["tiers"]},
                            updated=u.get("updated"))
    except Exception:
        out["usage"] = None
    try:
        out["router"] = router_status()
    except Exception:
        out["router"] = None
    return out


def emit_claude_failure(tier: str, model_id: str, error: str, retry_tier: Optional[str] = None) -> None:
    """A Claude call failed (claude_router); retry_tier is where it escalates next, if anywhere."""
    _emit("claude_call_failed", {"model": str(tier), "model_id": str(model_id), "error": str(error)[:240],
                                 "retry_tier": retry_tier, "ts": time.time()})


def emit_model_route(tier: str, model: str, where: str, reason: str = "", mode: str = "chat") -> None:
    """The model a turn runs on, local or Claude (prompter.py)."""
    global _last_route
    ev = {"tier": str(tier or "sonnet"), "model": str(model or ""), "where": where if where in ("local", "claude", "cloud") else "local",
          "reason": str(reason or "")[:200], "mode": str(mode), "ts": time.time()}
    with _lock:
        _last_route = ev
    _emit("model_route", ev)


def emit_autonomous_step(phase: str, data: Optional[Dict[str, Any]] = None) -> None:
    """One Plan -> Execute -> Verify progress event from an autonomous task."""
    d = dict(data or {})
    ev = {"step": str(d.get("step") or phase)[:120], "phase": str(phase), "status": str(d.get("status") or "running"),
          "ts": time.time()}
    for k in ("tool", "session_id", "round", "steps", "rounds"):
        if d.get(k) is not None:
            ev[k] = d[k] if isinstance(d[k], (int, float)) else str(d[k])[:120]
    if isinstance(d.get("checks"), list):
        ev["checks"] = [{"name": str(c.get("name"))[:40], "passed": bool(c.get("passed"))}
                        for c in d["checks"][:8] if isinstance(c, dict)]
    with _lock:
        if phase == "plan" and ev["step"] == "Planning":
            _recent_steps.clear()                       # a new run starts a fresh trail
        _recent_steps.append(ev)
        del _recent_steps[:-MAX_RECENT_STEPS]
    _emit("autonomous_step", ev)


# ─── subprocess streaming ────────────────────────────────────────────

def run_streaming(cmd, cwd: Optional[str] = None, timeout: float = 30) -> Tuple[int, str, str]:
    """Run ``cmd`` (shell=False), echoing stdout/stderr lines to the Terminal
    tab as they arrive. Returns (returncode, stdout, stderr). Raises
    subprocess.TimeoutExpired (after killing the process) and
    FileNotFoundError like subprocess.run would."""
    label = cmd if isinstance(cmd, str) else " ".join(map(str, cmd))
    emit_terminal(f"$ {label}\n", "info")
    proc = subprocess.Popen(
        cmd, cwd=cwd, shell=False, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    chunks: Dict[str, List[str]] = {"stdout": [], "stderr": []}

    def pump(pipe, name):
        try:
            for line in iter(pipe.readline, ""):
                chunks[name].append(line)
                emit_terminal(line, name)
        finally:
            pipe.close()

    readers = [threading.Thread(target=pump, args=(proc.stdout, "stdout"), daemon=True),
               threading.Thread(target=pump, args=(proc.stderr, "stderr"), daemon=True)]
    for t in readers:
        t.start()
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        for t in readers:
            t.join(timeout=2)
        emit_terminal(f"[timed out after {timeout:g}s]\n", "info")
        raise
    for t in readers:
        t.join(timeout=5)
    emit_terminal(f"[exit {code}]\n", "info")
    return code, "".join(chunks["stdout"]), "".join(chunks["stderr"])

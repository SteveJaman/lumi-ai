"""
MCP client: Lumi uses tools from external Model Context Protocol servers.

The SDK (sdk/server.py, ws://127.0.0.1:5056) lets programs connect IN to Lumi
and register sdk__<action> tools. This is the other direction: Lumi connects
OUT to the MCP servers listed in config/mcp_servers.json and offers their tools
to the model as mcp__<server>__<tool>, next to the SDK's tools.

    {"servers": [
      {"id": "filesystem", "transport": "stdio", "enabled": true, "read_only": true,
       "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:/notes"]},
      {"id": "docs", "transport": "http", "enabled": true, "url": "http://127.0.0.1:8931/mcp"}
    ]}

Optional per server: "env" {NAME: value} for a stdio server (it otherwise gets
only a minimal environment: PATH, temp and profile folders, never Lumi's API
keys). "expose_tools": false keeps a server connected but out of the model's
tool list, for a server Lumi calls only through its own wrapper tools (FL Studio).

Protocol. Dual-era client (spec 2026-07-28 "Versioning: Backward
Compatibility"): it first probes with `server/discover`, sending the current
revision in each request's `_meta`. A server that answers is modern and every
request carries `_meta`. Any other answer, or silence, means a legacy server.
For that one the client falls back to the `initialize` handshake
(2025-11-25 and earlier), then `notifications/initialized`. Either way it then
calls `tools/list`.

Transports:
  * stdio: newline-delimited JSON-RPC over the server's stdin/stdout.
  * Streamable HTTP: a POST per message; the answer is either JSON or an SSE
    stream. Session ids are handled for legacy servers.
  * The deprecated 2024-11-05 HTTP+SSE transport is detected and reported,
    not spoken.

Safety:
  * Every mcp__ call is approved through the same HITL gate as write_file and
    run_command (prompter._confirm -> hitl_server). The one exception is a
    clearly read-only tool on a server marked "read_only": true: its name
    starts or ends with list / get / read / search / query / fetch, contains
    no write-type word, and the server doesn't flag it as destructive.
  * Results go back to the model wrapped in [FROM MCP] provenance markers.
  * A server that crashes is marked "errored" and is NOT restarted; the
    Reconnect button (or POST /api/mcp/servers/<id>/reconnect) does that.

Not supported yet (follow-ups): MCP resources, prompts, sampling,
elicitation, roots, subscriptions/listen, progress notifications, and the
deprecated HTTP+SSE transport.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import constants
from modules.module import Module

MODERN_VERSION = "2026-07-28"
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MODERN_ERROR_CODES = {-32020, -32021, -32022}      # HeaderMismatch, MissingCapability, UnsupportedVersion
UNSUPPORTED_VERSION = -32022
CLIENT_INFO = {"name": "lumi", "version": str(getattr(constants, "SDK_VERSION", "0"))}

ID_RE = re.compile(r"^[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*$")    # no "__", so mcp__<id>__<tool> splits cleanly
MAX_ID_LEN = 32
MAX_TOOL_NAME_LEN = 64            # what Ollama / OpenAI-style tool calling accepts
MAX_TOOLS_PER_SERVER = 128
MAX_DESCRIPTION_CHARS = 1024
MAX_RESULT_CHARS = 12000
MAX_MESSAGE_BYTES = 32 * 1024 * 1024
STDERR_LINES = 20
CLOSE_GRACE_S = 2.0

READ_VERBS = frozenset({"list", "get", "read", "search", "query", "fetch"})
WRITE_WORDS = frozenset("""
write edit create delete remove rm move rename send run exec execute update set put post patch insert
drop kill start stop install uninstall upload push commit modify append replace save copy mkdir
merge apply submit publish deploy restart shutdown reset clear purge truncate approve pay buy order
""".split())

STATES = ("disabled", "connecting", "connected", "errored")

# What a stdio server inherits from Lumi's environment. Nothing else: Lumi's
# .env holds API keys that a third-party server has no business seeing.
_ENV_ALLOW = {
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR",
    "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "HOME", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "COMMONPROGRAMFILES", "PUBLIC",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS", "USERNAME", "USER", "LOGNAME",
    "LANG", "LC_ALL", "LC_CTYPE", "TERM", "SHELL", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME", "XDG_DATA_HOME", "NVM_HOME", "NVM_SYMLINK", "NVM_DIR", "NODE_PATH",
    "NODE_EXTRA_CA_CERTS", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "SSL_CERT_FILE",
}

GRAY = "\033[90m"
YELLOW = "\033[93m"
RESET = "\033[0m"


def _log(msg: str, warn: bool = False) -> None:
    print(f"{YELLOW if warn else GRAY}[MCP] {msg}{RESET}")


class MCPError(Exception):
    def __init__(self, message: str, code: Optional[int] = None, data: Any = None,
                 http_status: Optional[int] = None):
        super().__init__(message)
        self.code = code
        self.data = data
        self.http_status = http_status


class LegacyHttpSse(MCPError):
    """The server only speaks the deprecated 2024-11-05 HTTP+SSE transport."""


# ── config ──────────────────────────────────────────────────────

def config_path() -> str:
    p = str(getattr(constants, "MCP_CONFIG_PATH", "config/mcp_servers.json"))
    return p if os.path.isabs(p) else os.path.join(constants.WORKSPACE_ROOT, p)


def validate_server(raw: Any) -> Dict[str, Any]:
    """A clean server entry, or ValueError with a message for the user."""
    if not isinstance(raw, dict):
        raise ValueError("Each server must be an object.")
    sid = str(raw.get("id") or "").strip()
    if not sid or len(sid) > MAX_ID_LEN or not ID_RE.match(sid):
        raise ValueError("id: letters and digits, single - or _ between them, at most 32 characters.")
    transport = str(raw.get("transport") or "stdio").strip().lower()
    if transport not in ("stdio", "http"):
        raise ValueError("transport must be \"stdio\" or \"http\".")
    out: Dict[str, Any] = {"id": sid, "transport": transport,
                           "enabled": bool(raw.get("enabled", True)),
                           "read_only": bool(raw.get("read_only", False))}
    if raw.get("expose_tools") is False:
        # Connected, but its tools aren't offered to the model: Lumi's own wrappers call it
        # (e.g. modules/fl_studio_tools.py), so a big server doesn't fill the context window.
        out["expose_tools"] = False
    if transport == "stdio":
        command = str(raw.get("command") or "").strip()
        if not command:
            raise ValueError("A stdio server needs a command (for example npx).")
        args = raw.get("args") or []
        if isinstance(args, str):
            args = [a.strip() for a in args.split(",") if a.strip()]
        if not isinstance(args, list) or len(args) > 64 or not all(isinstance(a, (str, int, float)) for a in args):
            raise ValueError("args must be a list of strings.")
        out["command"] = command
        out["args"] = [str(a) for a in args]
        env = raw.get("env") or {}
        if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, (str, int, float))
                                                for k, v in env.items()):
            raise ValueError("env must be an object of NAME: value strings.")
        if env:
            out["env"] = {k: str(v) for k, v in env.items()}
    else:
        url = str(raw.get("url") or "").strip()
        if not re.match(r"^https?://[^\s/]+", url):
            raise ValueError("An http server needs a url starting with http:// or https://.")
        out["url"] = url
    return out


def load_config(path: Optional[str] = None) -> Tuple[List[Dict[str, Any]], List[str]]:
    """(valid servers, problems). Creates the file as {"servers": []} if missing."""
    path = path or config_path()
    if not os.path.exists(path):
        save_config([], path)
        return [], []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return [], [f"{os.path.basename(path)} can't be read: {e}"]
    servers, problems, seen = [], [], set()
    for raw in (data or {}).get("servers") or []:
        try:
            cfg = validate_server(raw)
        except ValueError as e:
            problems.append(f"skipped {raw.get('id') if isinstance(raw, dict) else raw!r}: {e}")
            continue
        if cfg["id"] in seen:
            problems.append(f"skipped duplicate id {cfg['id']}")
            continue
        seen.add(cfg["id"])
        servers.append(cfg)
    return servers, problems


def save_config(servers: List[Dict[str, Any]], path: Optional[str] = None) -> None:
    path = path or config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"servers": servers}, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


# ── tool naming, approval and results ───────────────────────────

def _words(name: str) -> List[str]:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name or "")
    return [w for w in re.split(r"[^a-z0-9]+", spaced.lower()) if w]


def is_read_only_tool(tool_name: str, annotations: Optional[dict] = None) -> bool:
    """Starts or ends with a read verb, has no write-type word, and the
    server doesn't say otherwise. Annotations are hints from the server, so
    they can only make a tool LESS trusted here, never more."""
    words = _words(tool_name)
    if not words or any(w in WRITE_WORDS for w in words):
        return False
    ann = annotations if isinstance(annotations, dict) else {}
    if ann.get("readOnlyHint") is False or ann.get("destructiveHint") is True:
        return False
    return words[0] in READ_VERBS or words[-1] in READ_VERBS


def exposed_name(server_id: str, tool_name: str, taken: set) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", tool_name).strip("_") or "tool"
    base = f"mcp__{server_id}__{safe}"[:MAX_TOOL_NAME_LEN]
    name, n = base, 2
    while name in taken:
        suffix = f"_{n}"
        name = base[:MAX_TOOL_NAME_LEN - len(suffix)] + suffix
        n += 1
    return name


def format_result(result: Any) -> str:
    """tools/call result -> text for the model. Errors start with "Error"."""
    if not isinstance(result, dict):
        return str(result)
    parts: List[str] = []
    for c in result.get("content") or []:
        if not isinstance(c, dict):
            continue
        kind = c.get("type")
        if kind == "text":
            parts.append(str(c.get("text") or ""))
        elif kind in ("image", "audio"):
            parts.append(f"[{kind} ({c.get('mimeType') or 'unknown type'}) not shown]")
        elif kind == "resource":
            r = c.get("resource") or {}
            parts.append(str(r.get("text") or f"[resource {r.get('uri', '')}]"))
        elif kind == "resource_link":
            parts.append(f"[link {c.get('uri', '')}] {c.get('name') or ''}".strip())
    if not any(p.strip() for p in parts) and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False, default=str))
    text = "\n".join(parts).strip() or "(the tool returned nothing)"
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + "\n... [truncated]"
    return f"Error: {text}" if result.get("isError") else text


# ── HTTP header helpers (Streamable HTTP, 2026-07-28) ───────────

_TCHAR = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


def header_value(value: Any) -> str:
    if isinstance(value, bool):
        s = "true" if value else "false"
    else:
        s = str(value)
    plain = (all(0x20 <= ord(ch) <= 0x7E or ch == "\t" for ch in s) and s == s.strip()
             and not (s.startswith("=?base64?") and s.endswith("?=")))
    return s if plain else "=?base64?" + base64.b64encode(s.encode("utf-8")).decode("ascii") + "?="


def header_params(schema: Any) -> List[Tuple[Tuple[str, ...], str]]:
    """[(property path, header name)] from x-mcp-header annotations.
    ValueError if an annotation breaks the spec's rules (the tool is then dropped)."""
    found: List[Tuple[Tuple[str, ...], str]] = []
    names = set()

    def walk(node, path):
        if not isinstance(node, dict):
            return
        for key, sub in (node.get("properties") or {}).items():
            if not isinstance(sub, dict):
                continue
            here = path + (key,)
            if "x-mcp-header" in sub:
                h = sub["x-mcp-header"]
                if not isinstance(h, str) or not h or not _TCHAR.match(h) or h.lower() in names:
                    raise ValueError(f"bad x-mcp-header on {'.'.join(here)}")
                if sub.get("type") not in ("string", "integer", "boolean"):
                    raise ValueError(f"x-mcp-header on non-primitive {'.'.join(here)}")
                names.add(h.lower())
                found.append((here, h))
            walk(sub, here)

    def misplaced(node, reachable):
        # An annotation anywhere other than a properties chain is invalid.
        if isinstance(node, dict):
            if not reachable and "x-mcp-header" in node:
                raise ValueError("x-mcp-header outside a properties chain")
            for k, v in node.items():
                if k == "properties" and isinstance(v, dict):
                    for sub in v.values():
                        misplaced(sub, reachable)
                elif k != "x-mcp-header":
                    misplaced(v, False)
        elif isinstance(node, list):
            for v in node:
                misplaced(v, False)

    misplaced(schema, True)
    walk(schema, ())
    return found


# ── one server ──────────────────────────────────────────────────

class MCPServer:
    """One connection. connect() -> list_tools() -> call_tool() -> close()."""

    def __init__(self, cfg: Dict[str, Any], on_event: Optional[Callable[["MCPServer", str], None]] = None):
        self.cfg = cfg
        self.id: str = cfg["id"]
        self.transport: str = cfg["transport"]
        self.state = "disabled" if not cfg.get("enabled", True) else "connecting"
        self.last_error = ""
        self.tools: List[Dict[str, Any]] = []
        self.era: Optional[str] = None          # "modern" | "legacy"
        self.version: Optional[str] = None
        self._next_id = 0
        self._on_event = on_event
        self._closing = False

    # -- protocol (shared) --
    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _meta(self, version: str) -> Dict[str, Any]:
        return {"io.modelcontextprotocol/protocolVersion": version,
                "io.modelcontextprotocol/clientInfo": CLIENT_INFO,
                "io.modelcontextprotocol/clientCapabilities": {}}

    def _params(self, params: Optional[dict], version: Optional[str] = None) -> dict:
        params = dict(params or {})
        if self.era == "modern" or version:
            params["_meta"] = {**(params.get("_meta") or {}), **self._meta(version or self.version)}
        return params

    async def request(self, method: str, params: Optional[dict] = None, timeout: float = 30.0,
                      version: Optional[str] = None) -> dict:
        """Send one request and return its result (raises MCPError)."""
        msg = {"jsonrpc": "2.0", "id": self._new_id(), "method": method,
               "params": self._params(params, version)}
        reply = await self._roundtrip(msg, timeout, version or self.version)
        if "error" in reply and reply["error"] is not None:
            err = reply["error"] if isinstance(reply["error"], dict) else {"message": str(reply["error"])}
            raise MCPError(str(err.get("message") or "error"), err.get("code"), err.get("data"))
        result = reply.get("result")
        if not isinstance(result, dict):
            raise MCPError(f"{method}: the server sent no result")
        kind = result.get("resultType", "complete")      # absent means complete (older servers)
        if kind == "input_required":
            raise MCPError("the server asked Lumi for extra input (sampling, elicitation or roots), "
                           "which Lumi's MCP client doesn't support yet")
        if kind != "complete":
            raise MCPError(f"{method}: unknown resultType {kind!r}")
        return result

    async def notify(self, method: str, params: Optional[dict] = None) -> None:
        msg: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params:
            msg["params"] = params
        await self._send_notification(msg)

    async def connect(self, timeout: float) -> None:
        """Open the transport, agree on a protocol era, then list the tools."""
        self._closing = False
        start = time.monotonic()
        await self._open()
        await self._handshake(timeout)
        left = max(2.0, timeout - (time.monotonic() - start))
        await self.list_tools(left)

    async def _handshake(self, timeout: float) -> None:
        probe = self._probe_timeout(timeout)
        try:
            res = await self.request("server/discover", {}, timeout=probe, version=MODERN_VERSION)
            supported = [str(v) for v in (res.get("supportedVersions") or [])]
            if supported and MODERN_VERSION not in supported:
                return await self._pick_from(supported, timeout)
            self.era, self.version = "modern", MODERN_VERSION
            return
        except LegacyHttpSse:
            raise
        except MCPError as e:
            if e.code == UNSUPPORTED_VERSION:
                supported = [str(v) for v in ((e.data or {}).get("supported") or [])] if isinstance(e.data, dict) else []
                return await self._pick_from(supported, timeout)
            if e.code in MODERN_ERROR_CODES:
                raise
            if self._dead():
                raise
        except asyncio.TimeoutError:
            if self._dead():
                raise MCPError("the server exited during startup" + self._stderr_hint())
        await self._legacy_initialize(timeout, LEGACY_VERSIONS[0])

    async def _pick_from(self, supported: List[str], timeout: float) -> None:
        if MODERN_VERSION in supported:
            self.era, self.version = "modern", MODERN_VERSION
            return
        legacy = [v for v in LEGACY_VERSIONS if v in supported]
        if legacy:
            return await self._legacy_initialize(timeout, legacy[0])
        raise MCPError(f"no protocol version in common (server supports {', '.join(supported) or 'none listed'})")

    async def _legacy_initialize(self, timeout: float, version: str) -> None:
        self.era, self.version = "legacy", None
        res = await self.request("initialize", {
            "protocolVersion": version, "capabilities": {}, "clientInfo": CLIENT_INFO,
        }, timeout=timeout)
        agreed = str(res.get("protocolVersion") or "")
        if agreed not in LEGACY_VERSIONS:
            raise MCPError(f"the server wants protocol {agreed or '(none)'}, which Lumi doesn't speak")
        self.version = agreed
        self._after_initialize(res)
        await self.notify("notifications/initialized")

    def _after_initialize(self, result: dict) -> None:
        pass

    async def list_tools(self, timeout: float = 30.0) -> List[Dict[str, Any]]:
        tools: List[Dict[str, Any]] = []
        cursor = None
        for _ in range(20):                                  # pages
            res = await self.request("tools/list", {"cursor": cursor} if cursor else {}, timeout=timeout)
            for t in res.get("tools") or []:
                if isinstance(t, dict) and isinstance(t.get("name"), str) and t["name"]:
                    tools.append(t)
            cursor = res.get("nextCursor")
            if not cursor or len(tools) >= MAX_TOOLS_PER_SERVER:
                break
        if len(tools) > MAX_TOOLS_PER_SERVER:
            _log(f"{self.id}: {len(tools)} tools; only the first {MAX_TOOLS_PER_SERVER} are offered", warn=True)
            tools = tools[:MAX_TOOLS_PER_SERVER]
        self.tools = self._filter_tools(tools)
        return self.tools

    def _filter_tools(self, tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return tools

    async def call_tool(self, name: str, arguments: Optional[dict], timeout: float = 30.0) -> dict:
        return await self.request("tools/call", {"name": name, "arguments": arguments or {}}, timeout=timeout)

    # -- transport hooks --
    def _probe_timeout(self, timeout: float) -> float:
        return max(1.0, timeout * 0.6)

    def _dead(self) -> bool:
        return False

    def _stderr_hint(self) -> str:
        return ""

    async def _open(self) -> None:
        raise NotImplementedError

    async def _roundtrip(self, msg: dict, timeout: float, version: Optional[str]) -> dict:
        raise NotImplementedError

    async def _send_notification(self, msg: dict) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError

    def _event(self, kind: str) -> None:
        if self._on_event is not None:
            try:
                self._on_event(self, kind)
            except Exception:
                pass


class StdioServer(MCPServer):
    def __init__(self, cfg, on_event=None):
        super().__init__(cfg, on_event)
        self.proc: Optional[asyncio.subprocess.Process] = None
        self._pending: Dict[Any, asyncio.Future] = {}
        self._tasks: List[asyncio.Task] = []
        self._stderr: List[str] = []
        self._write_lock = asyncio.Lock()

    def _child_env(self) -> Dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k.upper() in _ENV_ALLOW}
        env.update(self.cfg.get("env") or {})
        return env

    async def _open(self) -> None:
        command = self.cfg["command"]
        exe = shutil.which(command) or command
        kwargs: Dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self.proc = await asyncio.create_subprocess_exec(
                exe, *self.cfg.get("args", []),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=self._child_env(),
                limit=MAX_MESSAGE_BYTES, **kwargs)
        except FileNotFoundError:
            raise MCPError(f"couldn't find {command!r}; is it installed and on PATH?")
        except OSError as e:
            raise MCPError(f"couldn't start {command!r}: {e}")
        self._tasks = [asyncio.ensure_future(self._read_stdout()),
                       asyncio.ensure_future(self._read_stderr())]

    def _dead(self) -> bool:
        return self.proc is None or self.proc.returncode is not None

    def _stderr_hint(self) -> str:
        tail = [ln.strip() for ln in self._stderr[-3:] if ln.strip()]
        return (" (last stderr: " + " | ".join(tail))[:400] + ")" if tail else ""

    async def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        while True:
            try:
                line = await self.proc.stderr.readline()
            except (ValueError, asyncio.LimitOverrunError):
                continue
            if not line:
                return
            self._stderr.append(line.decode("utf-8", "replace").rstrip())
            del self._stderr[:-STDERR_LINES]

    async def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            while True:
                try:
                    line = await self.proc.stdout.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    _log(f"{self.id}: dropped an oversized message", warn=True)
                    continue
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue                          # servers must not print non-JSON; ignore it if they do
                for m in (msg if isinstance(msg, list) else [msg]):
                    if isinstance(m, dict):
                        await self._handle_incoming(m)
        finally:
            code = None
            if self.proc is not None:
                try:
                    code = await asyncio.wait_for(self.proc.wait(), 2)
                except asyncio.TimeoutError:
                    pass
            reason = f"the server process exited (code {code}){self._stderr_hint()}"
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(MCPError(reason))
            self._pending.clear()
            if not self._closing:
                self.last_error = reason
                self._event("died")

    async def _handle_incoming(self, m: dict) -> None:
        if "method" in m:
            if "id" in m:                  # a legacy server asking us something
                if m["method"] == "ping":
                    await self._write({"jsonrpc": "2.0", "id": m["id"], "result": {}})
                else:
                    await self._write({"jsonrpc": "2.0", "id": m["id"],
                                       "error": {"code": -32601, "message": "Lumi doesn't support " + str(m["method"])}})
            elif m["method"] == "notifications/tools/list_changed":
                self._event("tools_changed")
            return
        fut = self._pending.pop(m.get("id"), None)
        if fut is not None and not fut.done():
            fut.set_result(m)

    async def _write(self, msg: dict) -> None:
        if self._dead() or self.proc is None or self.proc.stdin is None:
            raise MCPError("the server process isn't running" + self._stderr_hint())
        data = (json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        async with self._write_lock:
            try:
                self.proc.stdin.write(data)
                await self.proc.stdin.drain()
            except (ConnectionError, OSError) as e:
                raise MCPError(f"the server's input closed ({e.__class__.__name__}){self._stderr_hint()}")

    async def _roundtrip(self, msg, timeout, version):
        fut = asyncio.get_running_loop().create_future()
        self._pending[msg["id"]] = fut
        try:
            await self._write(msg)
            return await asyncio.wait_for(asyncio.shield(fut), timeout)
        except asyncio.TimeoutError:
            if not self._dead():
                try:
                    await self._write({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                       "params": {"requestId": msg["id"], "reason": "timed out"}})
                except MCPError:
                    pass
            raise
        finally:
            self._pending.pop(msg["id"], None)

    async def _send_notification(self, msg):
        await self._write(msg)

    async def close(self) -> None:
        self._closing = True
        proc, self.proc = self.proc, None
        if proc is not None and proc.returncode is None:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()               # the polite shutdown signal
            except Exception:
                pass
            try:
                await asyncio.wait_for(proc.wait(), CLOSE_GRACE_S)
            except asyncio.TimeoutError:
                await _kill_tree(proc)
        for t in self._tasks:
            t.cancel()
        self._tasks = []
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(MCPError("disconnected"))
        self._pending.clear()


async def _kill_tree(proc) -> None:
    """npx & co. start a child (node); kill the whole tree, not just the shim."""
    if os.name == "nt":
        def tk():
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                           timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            await asyncio.get_running_loop().run_in_executor(None, tk)
        except Exception:
            pass
    else:
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), CLOSE_GRACE_S)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass
    if proc.returncode is None:
        try:
            proc.kill()
            await asyncio.wait_for(proc.wait(), CLOSE_GRACE_S)
        except Exception:
            pass


class HttpServer(MCPServer):
    def __init__(self, cfg, on_event=None):
        super().__init__(cfg, on_event)
        self.url: str = cfg["url"]
        self._session = None
        self._session_id: Optional[str] = None       # legacy (2025-11-25 and earlier) only
        self._headers_for: Dict[str, List[Tuple[Tuple[str, ...], str]]] = {}
        self._warned_json = False

    async def _open(self) -> None:
        import aiohttp
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        self._session_id = None

    def _after_initialize(self, result: dict) -> None:
        pass                                          # session id is captured from the response header

    def _filter_tools(self, tools):
        if self.era != "modern":
            return tools
        kept = []
        self._headers_for = {}
        for t in tools:
            try:
                self._headers_for[t["name"]] = header_params(t.get("inputSchema") or {})
            except ValueError as e:
                _log(f"{self.id}: dropped tool {t['name']!r} ({e})", warn=True)
                continue
            kept.append(t)
        return kept

    def _headers(self, msg: dict, version: Optional[str]) -> Dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        method = msg.get("method", "")
        if version and method != "initialize":
            h["MCP-Protocol-Version"] = version
        if self.era == "modern" or (version == MODERN_VERSION and self.era is None):
            h["Mcp-Method"] = method
            params = msg.get("params") or {}
            if method in ("tools/call", "resources/read", "prompts/get"):
                src = params.get("name") if "name" in params else params.get("uri")
                if src is not None:
                    h["Mcp-Name"] = header_value(src)
            if method == "tools/call":
                args = params.get("arguments") or {}
                for path, hname in self._headers_for.get(params.get("name"), []):
                    v: Any = args
                    for key in path:
                        v = v.get(key) if isinstance(v, dict) else None
                    if v is not None:
                        h[f"Mcp-Param-{hname}"] = header_value(v)
        elif self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        return h

    async def _post(self, msg: dict, timeout: float, version: Optional[str]) -> Optional[dict]:
        import aiohttp
        if self._session is None:
            raise MCPError("not connected")
        try:
            async with self._session.post(self.url, json=msg, headers=self._headers(msg, version),
                                          timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                sid = resp.headers.get("Mcp-Session-Id")
                if sid and self.era == "legacy":
                    self._session_id = sid
                ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if resp.status == 202:
                    return None
                if resp.status >= 400:
                    body = await resp.content.read(65536)
                    err = _jsonrpc_error(body)
                    if err is not None:
                        raise MCPError(str(err.get("message") or f"HTTP {resp.status}"), err.get("code"),
                                       err.get("data"), http_status=resp.status)
                    if resp.status in (404, 405) and msg.get("method") == "initialize":
                        raise LegacyHttpSse("this server seems to use the old HTTP+SSE transport (2024-11-05), "
                                            "which Lumi doesn't support; use its Streamable HTTP endpoint")
                    if resp.status == 404 and self._session_id:
                        raise MCPError("the server ended the session (HTTP 404); press Reconnect",
                                       http_status=404)
                    raise MCPError(f"HTTP {resp.status}: {body[:200].decode('utf-8', 'replace')}",
                                   http_status=resp.status)
                if ctype == "text/event-stream":
                    return await self._read_sse(resp, msg.get("id"))
                if "id" not in msg:
                    return None
                if ctype != "application/json" and not self._warned_json:
                    self._warned_json = True
                    _log(f"{self.id}: answered with {ctype or 'no content type'}, reading it as plain JSON",
                         warn=True)
                raw = await resp.content.read(MAX_MESSAGE_BYTES + 1)
                if len(raw) > MAX_MESSAGE_BYTES:
                    raise MCPError("the server's answer was too large")
                data = json.loads(raw or b"null")
                for m in (data if isinstance(data, list) else [data]):
                    if isinstance(m, dict) and m.get("id") == msg["id"]:
                        return m
                raise MCPError("the server's answer didn't match the request")
        except aiohttp.ClientError as e:
            raise MCPError(f"couldn't reach {self.url} ({e.__class__.__name__}: {e})")
        except ValueError as e:
            raise MCPError(f"the server sent invalid JSON ({e})")

    async def _read_sse(self, resp, want_id) -> Optional[dict]:
        buf = b""
        data_lines: List[bytes] = []
        total = 0
        async for chunk in resp.content.iter_any():
            total += len(chunk)
            if total > MAX_MESSAGE_BYTES:
                raise MCPError("the server's answer was too large")
            buf += chunk
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                line, buf = buf[:nl].rstrip(b"\r"), buf[nl + 1:]
                if not line:                                   # end of one event
                    if data_lines:
                        payload, data_lines = b"\n".join(data_lines), []
                        found = await self._sse_event(payload, want_id)
                        if found is not None:
                            return found
                    continue
                if line.startswith(b":"):
                    continue                                   # keep-alive comment
                if line.startswith(b"data:"):
                    data_lines.append(line[5:].lstrip(b" "))
        if data_lines:
            found = await self._sse_event(b"\n".join(data_lines), want_id)
            if found is not None:
                return found
        if want_id is None:
            return None
        raise MCPError("the server closed the stream without answering")

    async def _sse_event(self, payload: bytes, want_id) -> Optional[dict]:
        try:
            m = json.loads(payload)
        except ValueError:
            return None
        if not isinstance(m, dict):
            return None
        if "method" in m and "id" in m and self.era == "legacy":
            # Legacy servers may ask questions on the stream; answer ping, refuse the rest.
            reply = ({"jsonrpc": "2.0", "id": m["id"], "result": {}} if m["method"] == "ping" else
                     {"jsonrpc": "2.0", "id": m["id"],
                      "error": {"code": -32601, "message": "Lumi doesn't support " + str(m["method"])}})
            try:
                await self._post(reply, 10, self.version)
            except MCPError:
                pass
            return None
        if "method" in m:
            if m["method"] == "notifications/tools/list_changed":
                self._event("tools_changed")
            return None
        return m if m.get("id") == want_id else None

    async def _roundtrip(self, msg, timeout, version):
        reply = await asyncio.wait_for(self._post(msg, timeout, version), timeout)
        if reply is None:
            raise MCPError("the server answered a request with no body")
        return reply

    async def _send_notification(self, msg):
        await self._post(msg, 10, self.version)

    async def close(self) -> None:
        self._closing = True
        session, self._session = self._session, None
        if session is None:
            return
        if self._session_id:
            try:
                import aiohttp
                await session.delete(self.url, headers={"Mcp-Session-Id": self._session_id},
                                     timeout=aiohttp.ClientTimeout(total=2))
            except Exception:
                pass
            self._session_id = None
        try:
            await session.close()
        except Exception:
            pass


def _jsonrpc_error(body: bytes) -> Optional[dict]:
    try:
        data = json.loads(body or b"null")
    except ValueError:
        return None
    if isinstance(data, dict) and isinstance(data.get("error"), dict) and "code" in data["error"]:
        return data["error"]
    return None


def make_server(cfg: Dict[str, Any], on_event=None) -> MCPServer:
    return StdioServer(cfg, on_event) if cfg["transport"] == "stdio" else HttpServer(cfg, on_event)


# ── the registry (a Lumi module with its own event loop) ────────

class MCPRegistry(Module):
    """Owns every MCP connection. Runs on its own thread and loop like the
    SDK server; everything public is safe to call from any thread."""

    def __init__(self, signals, enabled: bool = True, path: Optional[str] = None,
                 broadcast: Optional[Callable[[dict], None]] = None):
        super().__init__(signals, enabled and bool(getattr(constants, "MCP_ENABLED", True)))
        self.path = path or config_path()
        self.configs: List[Dict[str, Any]] = []
        self.servers: Dict[str, MCPServer] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock = threading.Lock()
        self._snapshot: List[Dict[str, Any]] = []
        self._schemas: List[Dict[str, Any]] = []
        self._index: Dict[str, Tuple[str, str, bool]] = {}     # exposed -> (server id, tool, needs approval)
        self._tool_rows: List[Dict[str, str]] = []
        self._broadcast_fn = broadcast
        self._ready = threading.Event()
        self._stopped = False
        if signals is not None:
            signals.mcp = self

    @property
    def running(self) -> bool:
        return self._loop is not None and not self._stopped

    # -- lifecycle --
    async def run(self):
        if not self.enabled:
            self._ready.set()
            return
        await self.start()
        try:
            while not getattr(self.signals, "terminate", False) and not self._stopped:
                await asyncio.sleep(0.5)
        finally:
            await self.close_all()

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stopped = False
        try:
            self.configs, problems = load_config(self.path)
        except OSError as e:
            self.configs, problems = [], [f"couldn't create {self.path}: {e}"]
        for p in problems:
            _log(p, warn=True)
        for cfg in self.configs:
            self.servers[cfg["id"]] = make_server(cfg, self._on_server_event)
        self._publish()
        self._ready.set()
        for cfg in self.configs:
            if cfg["enabled"]:
                asyncio.ensure_future(self._connect(cfg["id"]))     # never blocks startup
        if self.configs:
            _log(f"{sum(c['enabled'] for c in self.configs)} of {len(self.configs)} server(s) enabled")

    async def close_all(self) -> None:
        self._stopped = True
        for s in list(self.servers.values()):
            try:
                await s.close()
            except Exception:
                pass

    def shutdown(self, timeout: float = 5.0) -> None:
        """From another thread (main.py's exit path): close every server now."""
        loop = self._loop
        if loop is None or self._stopped or not loop.is_running():
            return
        try:
            asyncio.run_coroutine_threadsafe(self.close_all(), loop).result(timeout)
        except Exception:
            pass

    # -- connections --
    def _timeout(self) -> float:
        return float(getattr(constants, "MCP_CONNECT_TIMEOUT_SEC", 10))

    async def _connect(self, sid: str) -> None:
        server = self.servers.get(sid)
        if server is None:
            return
        await server.close()
        server.state, server.last_error, server.tools = "connecting", "", []
        self._publish()
        try:
            await asyncio.wait_for(server.connect(self._timeout()), self._timeout())
        except asyncio.TimeoutError:
            await server.close()
            self._fail(server, f"didn't finish connecting within {self._timeout():g} s{_hint(server)}")
            return
        except asyncio.CancelledError:
            await server.close()
            raise
        except Exception as e:
            await server.close()
            self._fail(server, str(e) or e.__class__.__name__)
            return
        if self.servers.get(sid) is not server:
            await server.close()                 # replaced or removed while connecting
            return
        server.state = "connected"
        _log(f"{sid}: connected ({server.era} protocol {server.version}, {len(server.tools)} tool(s))")
        self._publish()

    def _fail(self, server: MCPServer, error: str) -> None:
        server.state, server.last_error, server.tools = "errored", error[:500], []
        _log(f"{server.id}: {server.last_error}", warn=True)      # once per failure; no retry loop
        self._publish()

    def _on_server_event(self, server: MCPServer, kind: str) -> None:
        if self.servers.get(server.id) is not server:
            return
        if kind == "died" and server.state in ("connected", "connecting"):
            if server.state == "connected":
                self._fail(server, server.last_error or "the server stopped")
        elif kind == "tools_changed" and server.state == "connected":
            asyncio.ensure_future(self._refresh_tools(server))

    async def _refresh_tools(self, server: MCPServer) -> None:
        try:
            await server.list_tools(float(getattr(constants, "MCP_CALL_TIMEOUT_SEC", 30)))
        except Exception as e:
            _log(f"{server.id}: couldn't refresh its tools ({e})", warn=True)
        self._publish()

    # -- state for everyone else --
    def _row(self, server: MCPServer, full: bool) -> Dict[str, Any]:
        row = {"id": server.id, "transport": server.transport, "enabled": bool(server.cfg.get("enabled")),
               "state": server.state, "tool_count": len(server.tools), "last_error": server.last_error or None}
        if full:
            row.update({"read_only": bool(server.cfg.get("read_only")), "protocol": server.version,
                        "command": server.cfg.get("command"), "args": server.cfg.get("args"),
                        "url": server.cfg.get("url")})
        return row

    def _publish(self) -> None:
        schemas, index, rows, taken = [], {}, [], set()
        for cfg in self.configs:
            s = self.servers.get(cfg["id"])
            if s is None or s.state != "connected":
                continue
            if cfg.get("expose_tools") is False:
                continue                        # wrapped by Lumi's own tools; never offered raw
            for t in s.tools:
                name = exposed_name(s.id, t["name"], taken)
                taken.add(name)
                desc = f"[MCP {s.id}] {str(t.get('description') or t.get('title') or t['name']).strip()}"
                params = t.get("inputSchema") if isinstance(t.get("inputSchema"), dict) else {}
                params = {k: v for k, v in params.items() if k != "$schema"}
                params.setdefault("type", "object")
                params.setdefault("properties", {})
                schemas.append({"type": "function", "function": {
                    "name": name, "description": desc[:MAX_DESCRIPTION_CHARS], "parameters": params}})
                read_ok = bool(cfg.get("read_only")) and is_read_only_tool(t["name"], t.get("annotations"))
                index[name] = (s.id, t["name"], not read_ok)
                rows.append({"name": name, "server": s.id, "description": desc[:MAX_DESCRIPTION_CHARS]})
        with self._lock:
            self._snapshot = [self._row(self.servers[c["id"]], True) for c in self.configs if c["id"] in self.servers]
            self._schemas, self._index, self._tool_rows = schemas, index, rows
            public = [self._row(self.servers[c["id"]], False) for c in self.configs if c["id"] in self.servers]
        self._broadcast({"type": "mcp_servers_update", "servers": public})

    def _broadcast(self, payload: dict) -> None:
        fn = self._broadcast_fn
        if fn is None:
            try:
                from modules import hitl_server        # the :5090 socket the islands use
                fn = hitl_server.broadcast_threadsafe
            except Exception:
                return
        try:
            fn(payload)
        except Exception:
            pass

    def list_servers(self) -> List[Dict[str, Any]]:
        if not self.running:
            return [dict(self._row(make_server(c), True), state="disabled") for c in self.configs]
        with self._lock:
            return [dict(r) for r in self._snapshot]

    def list_tools(self) -> List[Dict[str, str]]:
        with self._lock:
            return [dict(r) for r in self._tool_rows]

    def list_tools_for_registry(self) -> List[Dict[str, Any]]:
        """Tool schemas in the same shape as TOOL_SCHEMAS / the SDK's."""
        with self._lock:
            return list(self._schemas)

    def resolve(self, exposed: str) -> Optional[Tuple[str, str, bool]]:
        """mcp__<server>__<tool> -> (server id, real tool name, needs approval)."""
        with self._lock:
            return self._index.get(exposed)

    # -- calls --
    async def _call(self, sid: str, tool: str, arguments: Optional[dict]) -> str:
        server = self.servers.get(sid)
        if server is None or server.state != "connected":
            state = server.state if server is not None else "not configured"
            return f"Error: the MCP server '{sid}' is {state}."
        if not any(t["name"] == tool for t in server.tools):
            return f"Error: the MCP server '{sid}' has no tool named {tool!r}."
        timeout = float(getattr(constants, "MCP_CALL_TIMEOUT_SEC", 30))
        try:
            result = await server.call_tool(tool, arguments if isinstance(arguments, dict) else {}, timeout)
        except asyncio.TimeoutError:
            return f"Error: {sid}/{tool} didn't answer within {timeout:g} s."
        except MCPError as e:
            return f"Error: {sid}/{tool} failed: {e}"
        except Exception as e:
            return f"Error: {sid}/{tool} failed: {e.__class__.__name__}: {e}"
        return format_result(result)

    async def call_async(self, sid: str, tool: str, arguments: Optional[dict]) -> str:
        """Await from any event loop (the prompter's)."""
        if not self.running:
            return "Error: MCP isn't running."
        fut = asyncio.run_coroutine_threadsafe(self._call(sid, tool, arguments), self._loop)
        return await asyncio.wrap_future(fut)

    def call(self, sid: str, tool: str, arguments: Optional[dict]) -> str:
        """Blocking version for threads without a loop."""
        if not self.running:
            return "Error: MCP isn't running."
        timeout = float(getattr(constants, "MCP_CALL_TIMEOUT_SEC", 30)) + 5
        try:
            return asyncio.run_coroutine_threadsafe(self._call(sid, tool, arguments), self._loop).result(timeout)
        except Exception as e:
            return f"Error: {sid}/{tool} failed: {e.__class__.__name__}: {e}"

    # -- management (the web routes) --
    async def admin(self, coro_fn, *args):
        """Run a management coroutine on the registry's loop, from any loop."""
        if not self.running:
            raise MCPError("MCP is turned off (MCP_ENABLED in constants.py)")
        fut = asyncio.run_coroutine_threadsafe(coro_fn(*args), self._loop)
        return await asyncio.wrap_future(fut)

    def _persist(self) -> None:
        save_config(self.configs, self.path)

    def _server_row(self, sid: str) -> Optional[Dict[str, Any]]:
        s = self.servers.get(sid)
        return self._row(s, True) if s is not None else None

    async def upsert(self, raw: Any) -> Dict[str, Any]:
        cfg = validate_server(raw)
        sid = cfg["id"]
        before = next((c for c in self.configs if c["id"] == sid), None)
        if before and before.get("env") and isinstance(raw, dict) and "env" not in raw \
                and cfg["transport"] == "stdio":
            cfg["env"] = before["env"]          # the web form doesn't edit env; keep hand-written values
        if before and before.get("expose_tools") is False and isinstance(raw, dict) and "expose_tools" not in raw:
            cfg["expose_tools"] = False         # nor this: keep a hidden server hidden
        old = self.servers.get(sid)
        if old is not None:
            await old.close()
        idx = next((i for i, c in enumerate(self.configs) if c["id"] == sid), None)
        if idx is None:
            self.configs.append(cfg)
        else:
            self.configs[idx] = cfg
        self._persist()
        self.servers[sid] = make_server(cfg, self._on_server_event)
        if cfg["enabled"]:
            await self._connect(sid)
        else:
            self._publish()
        return self._server_row(sid)

    async def toggle(self, sid: str, enabled: bool) -> Dict[str, Any]:
        cfg = next((c for c in self.configs if c["id"] == sid), None)
        if cfg is None:
            raise KeyError(sid)
        cfg["enabled"] = bool(enabled)
        self._persist()
        server = self.servers[sid]
        server.cfg = cfg
        if enabled:
            await self._connect(sid)
        else:
            await server.close()
            server.state, server.tools, server.last_error = "disabled", [], ""
            self._publish()
        return self._server_row(sid)

    async def reconnect(self, sid: str) -> Dict[str, Any]:
        cfg = next((c for c in self.configs if c["id"] == sid), None)
        if cfg is None:
            raise KeyError(sid)
        if not cfg["enabled"]:
            raise ValueError("That server is disabled; enable it instead.")
        await self._connect(sid)
        return self._server_row(sid)

    async def remove(self, sid: str) -> None:
        if not any(c["id"] == sid for c in self.configs):
            raise KeyError(sid)
        self.configs = [c for c in self.configs if c["id"] != sid]
        self._persist()
        server = self.servers.pop(sid, None)
        if server is not None:
            await server.close()
        self._publish()


def _hint(server: MCPServer) -> str:
    if isinstance(server, StdioServer):
        h = server._stderr_hint()
        if h:
            return h
        if os.path.basename(str(server.cfg.get("command", ""))).lower().startswith("npx"):
            return " (npx may still be downloading the package on first run; press Reconnect in a moment)"
    return ""

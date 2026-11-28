"""MCP client (modules/mcp_client.py), its routes (modules/mcp_routes.py),
the prompter hook and the router's tool tiers.

Stdio servers are a small fake written to a temp file and run with this
Python; HTTP servers are aiohttp apps on a random port. No network, no npx.
"""

import asyncio
import json
import os
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from modules import mcp_client as mc
from modules import model_router

FAKE_SERVER = r'''
import json, sys
mode = sys.argv[1]
initialized = False
TOOLS = [
    {"name": "read_file", "description": "Read a file", "inputSchema": {"type": "object",
        "properties": {"path": {"type": "string"}}}},
    {"name": "write_file", "description": "Write a file", "inputSchema": {"type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}}}},
    {"name": "list_items", "description": "List things", "annotations": {"readOnlyHint": True}},
]
def send(m):
    sys.stdout.write(json.dumps(m) + "\n"); sys.stdout.flush()
for line in sys.stdin:
    m = json.loads(line)
    mid, method, params = m.get("id"), m.get("method"), m.get("params") or {}
    meta = params.get("_meta") or {}
    if mid is None:
        if method == "notifications/initialized":
            initialized = True
        continue
    if method == "server/discover":
        if mode == "modern":
            send({"jsonrpc": "2.0", "id": mid, "result": {"resultType": "complete",
                  "supportedVersions": ["2026-07-28", "2025-11-25"]}})
        elif mode in ("legacy", "badinit", "crash"):
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Method not found"}})
        continue                                   # "silent": never answers before initialize
    if method == "initialize":
        if mode == "badinit":
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": "boom: bad init"}})
            continue
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": "2025-11-25",
              "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "1"}}})
        continue
    if mode != "modern" and not initialized:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32002, "message": "not initialized"}})
        continue
    if mode == "modern" and meta.get("io.modelcontextprotocol/protocolVersion") != "2026-07-28":
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "missing _meta"}})
        continue
    if method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        if mode == "crash":
            pass
    elif method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name == "write_file" and args.get("path") == "fail":
            send({"jsonrpc": "2.0", "id": mid, "result": {"isError": True,
                  "content": [{"type": "text", "text": "disk full"}]}})
        elif name == "sleep":
            pass
        else:
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [
                {"type": "text", "text": f"{name} ok {json.dumps(args, sort_keys=True)} meta={bool(meta)}"}]}})
    else:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "nope"}})
'''


@pytest.fixture
def fake_script(tmp_path):
    p = tmp_path / "fake_mcp.py"
    p.write_text(FAKE_SERVER, encoding="utf-8")
    return str(p)


def stdio_cfg(script, mode, sid="fake", read_only=False, enabled=True):
    return {"id": sid, "transport": "stdio", "command": sys.executable, "args": ["-I", script, mode],
            "enabled": enabled, "read_only": read_only}


def run(coro):
    return asyncio.run(coro)


# ── config ───────────────────────────────────────────────────────

def test_validate_server_accepts_both_transports_and_rejects_bad_ids():
    s = mc.validate_server({"id": "filesystem", "command": "npx", "args": "-y, pkg, /tmp/test"})
    assert s == {"id": "filesystem", "transport": "stdio", "enabled": True, "read_only": False,
                 "command": "npx", "args": ["-y", "pkg", "/tmp/test"]}
    h = mc.validate_server({"id": "docs", "transport": "http", "url": "http://127.0.0.1:9/mcp"})
    assert h["url"] == "http://127.0.0.1:9/mcp" and "command" not in h
    for bad in ({"id": "a__b", "command": "x"}, {"id": "", "command": "x"}, {"id": "x" * 33, "command": "x"},
                {"id": "ok", "transport": "ws", "command": "x"}, {"id": "ok"},
                {"id": "ok", "transport": "http", "url": "ftp://x"}, {"id": "bad id", "command": "x"}):
        with pytest.raises(ValueError):
            mc.validate_server(bad)


def test_missing_config_is_created_empty_and_bad_entries_are_skipped(tmp_path):
    path = str(tmp_path / "config" / "mcp_servers.json")
    assert mc.load_config(path) == ([], [])
    assert json.load(open(path)) == {"servers": []}
    json.dump({"servers": [{"id": "a", "command": "x"}, {"id": "a", "command": "y"}, {"id": "b__c"}]},
              open(path, "w"))
    servers, problems = mc.load_config(path)
    assert [s["id"] for s in servers] == ["a"] and len(problems) == 2


# ── naming, approval, results, tiers ─────────────────────────────

def test_read_only_tool_rule():
    for name in ("read_file", "list_directory", "search_files", "get_file_info", "read_multiple_files",
                 "list_allowed_directories", "fetch", "issuesList", "web_search"):
        assert mc.is_read_only_tool(name), name
    for name in ("write_file", "edit_file", "create_directory", "move_file", "directory_tree",
                 "get_and_delete", "send_email", "run_query", "list_then_remove", ""):
        assert not mc.is_read_only_tool(name), name
    assert not mc.is_read_only_tool("read_file", {"destructiveHint": True})
    assert not mc.is_read_only_tool("read_file", {"readOnlyHint": False})
    assert not mc.is_read_only_tool("write_file", {"readOnlyHint": True})      # hints never loosen it


def test_exposed_names_are_safe_unique_and_short():
    taken = set()
    a = mc.exposed_name("fs", "read.file", taken); taken.add(a)
    b = mc.exposed_name("fs", "read file", taken); taken.add(b)
    assert a == "mcp__fs__read_file" and b == "mcp__fs__read_file_2"
    long = mc.exposed_name("server", "x" * 200, set())
    assert len(long) == 64 and long.startswith("mcp__server__")


def test_format_result():
    assert mc.format_result({"content": [{"type": "text", "text": "hi"}]}) == "hi"
    assert mc.format_result({"isError": True, "content": [{"type": "text", "text": "no"}]}) == "Error: no"
    assert "image" in mc.format_result({"content": [{"type": "image", "mimeType": "image/png", "data": "x"}]})
    assert mc.format_result({"content": [], "structuredContent": {"a": 1}}) == '{"a": 1}'
    assert mc.format_result({"content": [{"type": "text", "text": "y" * 20000}]}).endswith("[truncated]")


def test_router_tiers_for_tools():
    assert model_router.tool_tier("mcp__fs__read_file") == "sonnet"
    assert model_router.tool_tier("mcp__fs__list_directory") == "sonnet"
    for heavy in ("mcp__fs__write_file", "mcp__mail__send_email", "mcp__gh__deleteRepo", "mcp__sh__run_command"):
        assert model_router.tool_tier(heavy) == "opus", heavy
    assert model_router.tool_tier("read_file") == model_router.DEFAULT_TIER
    assert model_router.max_tier("haiku", "opus") == "opus"
    assert model_router.max_tier("opus", "sonnet") == "opus"
    assert model_router.max_tier(None, "haiku") == "sonnet"


def test_header_values_and_x_mcp_header_rules():
    assert mc.header_value("us-west1") == "us-west1"
    assert mc.header_value("Hello, 世界") == "=?base64?SGVsbG8sIOS4lueVjA==?="
    assert mc.header_value(" padded ") == "=?base64?IHBhZGRlZCA=?="
    assert mc.header_value(True) == "true"
    ok = {"type": "object", "properties": {"region": {"type": "string", "x-mcp-header": "Region"},
                                           "q": {"type": "string"}}}
    assert mc.header_params(ok) == [(("region",), "Region")]
    for bad in ({"properties": {"a": {"type": "number", "x-mcp-header": "A"}}},
                {"properties": {"a": {"type": "string", "x-mcp-header": "bad name"}}},
                {"properties": {"a": {"type": "array", "items": {"type": "string", "x-mcp-header": "A"}}}},
                {"properties": {"a": {"type": "string", "x-mcp-header": "A"},
                                "b": {"type": "string", "x-mcp-header": "a"}}}):
        with pytest.raises(ValueError):
            mc.header_params(bad)


# ── stdio servers ────────────────────────────────────────────────

@pytest.mark.parametrize("mode,era,version", [
    ("legacy", "legacy", "2025-11-25"),
    ("modern", "modern", "2026-07-28"),
])
def test_stdio_handshake_both_eras_then_list_and_call(fake_script, mode, era, version):
    async def go():
        s = mc.make_server(stdio_cfg(fake_script, mode))
        try:
            await s.connect(10)
            assert (s.era, s.version) == (era, version)
            assert [t["name"] for t in s.tools] == ["read_file", "write_file", "list_items"]
            out = mc.format_result(await s.call_tool("read_file", {"path": "a.txt"}, 5))
            assert out.startswith('read_file ok {"path": "a.txt"}')
            assert out.endswith("meta=True" if era == "modern" else "meta=False")
            bad = mc.format_result(await s.call_tool("write_file", {"path": "fail"}, 5))
            assert bad == "Error: disk full"
        finally:
            await s.close()
        assert s.proc is None
    run(go())


def test_a_server_silent_to_the_probe_is_treated_as_legacy(fake_script):
    async def go():
        s = mc.make_server(stdio_cfg(fake_script, "silent"))
        try:
            t0 = time.monotonic()
            await s.connect(3)
            assert s.era == "legacy" and len(s.tools) == 3
            assert time.monotonic() - t0 < 3.5
        finally:
            await s.close()
    run(go())


def test_missing_command_is_a_clear_error():
    async def go():
        s = mc.make_server({"id": "x", "transport": "stdio", "command": "definitely-not-a-real-binary-xyz",
                            "args": [], "enabled": True})
        with pytest.raises(mc.MCPError, match="couldn't find"):
            await s.connect(3)
        await s.close()
    run(go())


def test_a_call_that_times_out_returns_quickly(fake_script):
    async def go():
        s = mc.make_server(stdio_cfg(fake_script, "legacy"))
        try:
            await s.connect(10)
            with pytest.raises(asyncio.TimeoutError):
                await s.request("tools/call", {"name": "sleep", "arguments": {}}, timeout=0.5)
        finally:
            await s.close()
    run(go())


# ── registry ─────────────────────────────────────────────────────

class LoopThread:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.t = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.t.start()

    def call(self, coro, timeout=20):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.t.join(5)


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setattr(mc.constants, "MCP_CONNECT_TIMEOUT_SEC", 10, raising=False)
    monkeypatch.setattr(mc.constants, "MCP_CALL_TIMEOUT_SEC", 5, raising=False)
    made = []

    def make(servers=()):
        path = str(tmp_path / "mcp_servers.json")
        mc.save_config(list(servers), path)
        sent = []
        sig = SimpleNamespace(terminate=False)
        reg = mc.MCPRegistry(sig, path=path, broadcast=sent.append)
        lt = LoopThread()
        lt.call(reg.start())
        reg.sent, reg.lt, reg.path_ = sent, lt, path
        made.append(reg)
        return reg

    yield make
    for reg in made:
        try:
            reg.lt.call(reg.close_all())
        finally:
            reg.lt.stop()


def wait_state(reg, sid, states, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        row = next((s for s in reg.list_servers() if s["id"] == sid), None)
        if row and row["state"] in states:
            return row
        time.sleep(0.05)
    raise AssertionError(f"{sid} never reached {states}: {reg.list_servers()}")


def test_empty_config_lists_nothing_and_offers_no_tools(registry):
    reg = registry()
    assert reg.list_servers() == [] and reg.list_tools() == [] and reg.list_tools_for_registry() == []
    assert reg.sent[-1] == {"type": "mcp_servers_update", "servers": []}


def test_connects_in_background_and_exposes_prefixed_tools(registry, fake_script):
    reg = registry([stdio_cfg(fake_script, "legacy", sid="filesystem", read_only=True)])
    row = wait_state(reg, "filesystem", {"connected", "errored"})
    assert row["state"] == "connected" and row["tool_count"] == 3 and row["last_error"] is None
    names = [t["name"] for t in reg.list_tools()]
    assert names == ["mcp__filesystem__read_file", "mcp__filesystem__write_file", "mcp__filesystem__list_items"]
    schemas = reg.list_tools_for_registry()
    assert all(s["function"]["description"].startswith("[MCP filesystem] ") for s in schemas)
    assert schemas[2]["function"]["parameters"] == {"type": "object", "properties": {}}
    # read_only server: reads skip approval, writes never do
    assert reg.resolve("mcp__filesystem__read_file") == ("filesystem", "read_file", False)
    assert reg.resolve("mcp__filesystem__list_items") == ("filesystem", "list_items", False)
    assert reg.resolve("mcp__filesystem__write_file") == ("filesystem", "write_file", True)
    assert reg.resolve("mcp__nope__x") is None
    out = reg.call("filesystem", "read_file", {"path": "x"})
    assert out.startswith("read_file ok")
    pushed = [p for p in reg.sent if p["type"] == "mcp_servers_update"]
    assert pushed[-1]["servers"][0] == {"id": "filesystem", "transport": "stdio", "enabled": True,
                                        "state": "connected", "tool_count": 3, "last_error": None}


def test_without_read_only_every_tool_needs_approval(registry, fake_script):
    reg = registry([stdio_cfg(fake_script, "legacy", sid="fs")])
    wait_state(reg, "fs", {"connected"})
    assert all(reg.resolve(t["name"])[2] for t in reg.list_tools())


def test_failed_handshake_is_errored_and_others_still_connect(registry, fake_script):
    reg = registry([stdio_cfg(fake_script, "badinit", sid="broken"), stdio_cfg(fake_script, "modern", sid="good")])
    bad = wait_state(reg, "broken", {"errored", "connected"})
    good = wait_state(reg, "good", {"connected", "errored"})
    assert bad["state"] == "errored" and "boom: bad init" in bad["last_error"]
    assert good["state"] == "connected" and good["protocol"] == "2026-07-28"
    assert {t["server"] for t in reg.list_tools()} == {"good"}


def test_toggle_off_removes_tools_and_on_restores_them(registry, fake_script):
    reg = registry([stdio_cfg(fake_script, "legacy", sid="fs")])
    wait_state(reg, "fs", {"connected"})
    row = reg.lt.call(reg.toggle("fs", False))
    assert row["state"] == "disabled" and reg.list_tools() == [] and reg.resolve("mcp__fs__read_file") is None
    assert json.load(open(reg.path_))["servers"][0]["enabled"] is False
    assert reg.servers["fs"].proc is None
    row = reg.lt.call(reg.toggle("fs", True))
    assert row["state"] == "connected" and len(reg.list_tools()) == 3


def test_a_killed_server_turns_errored_is_not_restarted_and_reconnect_fixes_it(registry, fake_script):
    reg = registry([stdio_cfg(fake_script, "legacy", sid="fs")])
    wait_state(reg, "fs", {"connected"})
    reg.servers["fs"].proc.kill()                            # "killed externally"
    row = wait_state(reg, "fs", {"errored"}, timeout=5)
    assert "exited" in row["last_error"] and reg.list_tools() == []
    time.sleep(1.0)
    assert reg.list_servers()[0]["state"] == "errored"       # no auto-restart
    assert reg.call("fs", "read_file", {}).startswith("Error: the MCP server 'fs' is errored")
    row = reg.lt.call(reg.reconnect("fs"))
    assert row["state"] == "connected" and len(reg.list_tools()) == 3


def test_upsert_and_remove_persist(registry, fake_script):
    reg = registry()
    row = reg.lt.call(reg.upsert(stdio_cfg(fake_script, "modern", sid="new")))
    assert row["state"] == "connected"
    assert [s["id"] for s in json.load(open(reg.path_))["servers"]] == ["new"]
    reg.lt.call(reg.remove("new"))
    assert reg.list_servers() == [] and json.load(open(reg.path_)) == {"servers": []}
    with pytest.raises(KeyError):
        reg.lt.call(reg.remove("new"))
    with pytest.raises(ValueError):
        reg.lt.call(reg.upsert({"id": "x"}))


def test_expose_tools_false_round_trips_through_web_form_edits(registry, fake_script):
    hidden = dict(stdio_cfg(fake_script, "modern", sid="fl-studio"), expose_tools=False)
    reg = registry([hidden])
    wait_state(reg, "fl-studio", {"connected"})
    assert reg.list_tools_for_registry() == []                  # connected, but nothing offered raw
    assert reg.resolve("mcp__fl-studio__echo") is None

    form = stdio_cfg(fake_script, "modern", sid="fl-studio")     # the web form never sends the key
    assert "expose_tools" not in form
    row = reg.lt.call(reg.upsert(form))
    assert row["state"] == "connected"
    saved = json.load(open(reg.path_))["servers"]
    assert saved[0]["expose_tools"] is False                    # kept on disk
    assert reg.list_tools_for_registry() == []                  # and still hidden
    servers, _ = mc.load_config(reg.path_)
    assert servers[0]["expose_tools"] is False                  # and survives a reload

    reg.lt.call(reg.upsert(dict(form, expose_tools=True)))      # saying so explicitly un-hides it
    assert "expose_tools" not in json.load(open(reg.path_))["servers"][0]
    assert any(s["function"]["name"].startswith("mcp__fl-studio__") for s in reg.list_tools_for_registry())


def test_a_slow_server_times_out_without_blocking(registry, fake_script, monkeypatch):
    monkeypatch.setattr(mc.constants, "MCP_CONNECT_TIMEOUT_SEC", 1.5, raising=False)
    slow = {"id": "slow", "transport": "stdio", "command": sys.executable,
            "args": ["-c", "import time; time.sleep(30)"], "enabled": True, "read_only": False}
    t0 = time.time()
    reg = registry([slow])
    assert time.time() - t0 < 1.0                             # start() returned at once
    row = wait_state(reg, "slow", {"errored"}, timeout=8)
    assert "didn't finish connecting" in row["last_error"]
    assert reg.servers["slow"].proc is None


def test_env_is_minimal_plus_configured(fake_script, monkeypatch):
    monkeypatch.setenv("FISH_API_KEY", "secret")
    s = mc.StdioServer({**stdio_cfg(fake_script, "legacy"), "env": {"TOKEN": "t"}})
    env = s._child_env()
    assert "FISH_API_KEY" not in env and env["TOKEN"] == "t" and any(k.upper() == "PATH" for k in env)


# ── HTTP servers ─────────────────────────────────────────────────

def http_app(kind, seen):
    """kind: modern | legacy | sse | oldsse"""
    async def handle(request):
        if kind == "oldsse":
            return web.Response(status=405)
        m = await request.json()
        seen.append((dict(request.headers), m))
        method, mid = m.get("method"), m.get("id")
        params = m.get("params") or {}
        if method == "server/discover":
            if kind in ("modern", "sse"):
                return web.json_response({"jsonrpc": "2.0", "id": mid, "result": {
                    "supportedVersions": ["2026-07-28"]}})
            return web.json_response({"jsonrpc": "2.0", "id": None,
                                      "error": {"code": -32000, "message": "Bad Request: No valid session ID"}},
                                     status=400)
        if method == "initialize":
            return web.json_response({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": "2025-06-18",
                                      "capabilities": {}}}, headers={"Mcp-Session-Id": "sess-1"})
        if method == "notifications/initialized":
            return web.Response(status=202)
        if kind == "legacy" and request.headers.get("Mcp-Session-Id") != "sess-1":
            return web.Response(status=400, text="no session")
        if method == "tools/list":
            result = {"tools": [{"name": "search_docs", "inputSchema": {"type": "object", "properties": {
                "region": {"type": "string", "x-mcp-header": "Region"}, "q": {"type": "string"}}}},
                {"name": "bad", "inputSchema": {"properties": {"n": {"type": "number", "x-mcp-header": "N"}}}}]}
        else:
            result = {"content": [{"type": "text", "text": f"called {params.get('name')}"}]}
        body = {"jsonrpc": "2.0", "id": mid, "result": result}
        if kind == "sse":
            resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await resp.prepare(request)
            await resp.write(b": keep-alive\n\n")
            await resp.write(b'data: {"jsonrpc":"2.0","method":"notifications/progress","params":{}}\n\n')
            await resp.write(b"data: " + json.dumps(body).encode() + b"\n\n")
            await resp.write_eof()
            return resp
        return web.json_response(body)

    app = web.Application()
    app.router.add_post("/mcp", handle)
    app.router.add_get("/mcp", handle)
    async def delete(request):
        return web.Response(status=200)
    app.router.add_delete("/mcp", delete)
    return app


def run_http(kind, fn):
    seen = []

    async def go():
        server = TestServer(http_app(kind, seen))
        await server.start_server()
        try:
            s = mc.make_server({"id": "web", "transport": "http", "enabled": True,
                                "url": str(server.make_url("/mcp"))})
            try:
                return await fn(s, seen)
            finally:
                await s.close()
        finally:
            await server.close()
    return run(go())


@pytest.mark.parametrize("kind", ["modern", "sse"])
def test_http_modern_json_and_sse(kind):
    async def fn(s, seen):
        await s.connect(5)
        assert s.era == "modern" and [t["name"] for t in s.tools] == ["search_docs"]   # bad x-mcp-header dropped
        out = mc.format_result(await s.call_tool("search_docs", {"region": "us-west1", "q": "x"}, 5))
        assert out == "called search_docs"
        h, m = seen[-1]
        assert h["MCP-Protocol-Version"] == "2026-07-28" and h["Mcp-Method"] == "tools/call"
        assert h["Mcp-Name"] == "search_docs" and h["Mcp-Param-Region"] == "us-west1"
        assert "text/event-stream" in h["Accept"] and "application/json" in h["Accept"]
        assert m["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"] == "2026-07-28"
    run_http(kind, fn)


def test_http_legacy_falls_back_to_initialize_with_a_session():
    async def fn(s, seen):
        await s.connect(5)
        assert (s.era, s.version) == ("legacy", "2025-06-18") and len(s.tools) == 2
        assert mc.format_result(await s.call_tool("search_docs", {}, 5)) == "called search_docs"
        h, m = seen[-1]
        assert h["Mcp-Session-Id"] == "sess-1" and h["MCP-Protocol-Version"] == "2025-06-18"
        assert "_meta" not in m["params"]
    run_http("legacy", fn)


def test_http_old_sse_transport_is_reported():
    async def fn(s, seen):
        with pytest.raises(mc.MCPError, match="old HTTP\\+SSE transport"):
            await s.connect(5)
    run_http("oldsse", fn)


# ── routes ───────────────────────────────────────────────────────

class StubWeb:
    def __init__(self, reg):
        self.signals = SimpleNamespace(mcp=reg)

    def _is_local(self, request): return not request.headers.get("X-Remote")
    def _origin_ok(self, request): return request.headers.get("Origin") in (None, "http://127.0.0.1")
    def _current_user(self, request): return None if request.headers.get("X-Anon") else {"id": 1}
    def _require_csrf(self, request, body):
        return (True, None) if request.headers.get("X-CSRF-Token") == "tok" else (False, "Missing CSRF token.")


def with_routes(reg, fn):
    from modules import mcp_routes
    app = web.Application()
    mcp_routes.register(app.router, StubWeb(reg))

    async def go():
        async with TestClient(TestServer(app)) as c:
            return await fn(c)
    return run(go())


H = {"X-CSRF-Token": "tok"}


def test_routes_shapes_and_lifecycle(registry, fake_script):
    reg = registry()

    async def t(c):
        assert await (await c.get("/api/mcp/servers")).json() == {"servers": []}
        assert await (await c.get("/api/mcp/tools")).json() == {"tools": []}
        r = await c.post("/api/mcp/servers", headers=H, json=stdio_cfg(fake_script, "legacy", sid="filesystem",
                                                                         read_only=True))
        body = await r.json()
        assert r.status == 200 and body["server"]["state"] == "connected" and body["server"]["tool_count"] == 3
        tools = (await (await c.get("/api/mcp/tools")).json())["tools"]
        assert tools and all(t["name"].startswith("mcp__filesystem__") and t["server"] == "filesystem"
                             for t in tools)
        servers = (await (await c.get("/api/mcp/servers")).json())["servers"]
        assert {"id", "transport", "enabled", "state", "tool_count", "last_error"} <= set(servers[0])
        r = await c.post("/api/mcp/servers/filesystem/toggle", headers=H, json={"enabled": False})
        assert (await r.json())["enabled"] is False
        assert (await (await c.get("/api/mcp/tools")).json()) == {"tools": []}
        r = await c.post("/api/mcp/servers/filesystem/reconnect", headers=H)
        assert r.status == 400                                        # disabled: enable it instead
        await c.post("/api/mcp/servers/filesystem/toggle", headers=H, json={"enabled": True})
        r = await c.post("/api/mcp/servers/filesystem/reconnect", headers=H)
        assert (await r.json())["server"]["state"] == "connected"
        assert (await c.post("/api/mcp/servers/nope/reconnect", headers=H)).status == 404
        assert (await c.post("/api/mcp/servers", headers=H, json={"id": "bad id"})).status == 400
        r = await c.delete("/api/mcp/servers/filesystem", headers=H)
        assert r.status == 200
        assert await (await c.get("/api/mcp/servers")).json() == {"servers": []}
    with_routes(reg, t)


def test_routes_are_host_only_logged_in_and_csrf_checked(registry):
    reg = registry()

    async def t(c):
        remote = {**H, "X-Remote": "1"}
        assert (await c.post("/api/mcp/servers", headers=remote, json={"id": "x", "command": "y"})).status == 403
        assert (await c.get("/api/mcp/servers", headers={"X-Remote": "1"})).status == 403
        assert (await c.get("/api/mcp/tools", headers={"X-Remote": "1"})).status == 403
        assert (await c.delete("/api/mcp/servers/x", headers=remote)).status == 403
        assert (await c.post("/api/mcp/servers", json={"id": "x", "command": "y"})).status == 403   # no CSRF
        assert (await c.post("/api/mcp/servers", headers={**H, "Origin": "http://evil.test"},
                             json={"id": "x", "command": "y"})).status == 403
        assert (await c.get("/api/mcp/servers", headers={"X-Anon": "1"})).status == 401
        assert reg.list_servers() == []
    with_routes(reg, t)


def test_remote_sessions_cannot_reach_mcp_routes_in_the_real_allowlist():
    from modules.socketIO import _REMOTE_API_ALLOW
    assert not any(path.startswith("/api/mcp") for _, path in _REMOTE_API_ALLOW)


# ── prompter hook ────────────────────────────────────────────────

class FakeMCP:
    def __init__(self, needs_approval):
        self.needs = needs_approval
        self.calls = []

    def resolve(self, name):
        return ("filesystem", name.split("__")[-1], self.needs) if name.startswith("mcp__filesystem__") else None

    async def call_async(self, sid, tool, args):
        self.calls.append((sid, tool, args))
        return f"{tool} done"


def make_prompter(mcp, approve):
    from modules.prompter import Prompter
    p = Prompter.__new__(Prompter)
    p.signals = SimpleNamespace(mcp=mcp, sdk=None)
    asked = []

    async def confirm(tool_name, arguments, timeout=120, agent="main"):
        asked.append((tool_name, arguments))
        return approve
    p._confirm = confirm
    return p, asked


@pytest.mark.parametrize("needs,approve,expect_call", [(True, True, True), (True, False, False), (False, False, True)])
def test_prompter_routes_mcp_calls_through_the_approval_gate(needs, approve, expect_call):
    mcp = FakeMCP(needs)
    p, asked = make_prompter(mcp, approve)
    out = run(p._run_tool("mcp__filesystem__write_file", {"path": "a"}))
    assert bool(asked) == needs
    if needs:
        assert asked[0][0] == "mcp__filesystem__write_file"
        assert asked[0][1] == {"server": "filesystem", "tool": "write_file", "arguments": {"path": "a"}}
    assert bool(mcp.calls) == expect_call
    assert out == ("write_file done" if expect_call else "The user declined to allow this action.")
    assert p._route_tier == "opus"


def test_prompter_unknown_mcp_tool_and_provenance():
    p, _ = make_prompter(FakeMCP(True), True)
    assert run(p._run_tool("mcp__gone__x", {})).startswith("Error: mcp__gone__x isn't available")
    from modules.agent_tools import wrap_tool_result, PROTECTED_FILES
    assert wrap_tool_result("mcp__fs__read_file", "data") == "[FROM MCP]\ndata\n[END FROM MCP]"
    assert "config/mcp_servers.json" in PROTECTED_FILES


def test_mcp_module_never_mentions_vtube_studio():
    src = open(mc.__file__, encoding="utf-8").read().lower()
    assert "vts" not in src and "vtube" not in src

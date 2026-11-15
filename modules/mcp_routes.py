"""
Web routes for MCP servers (modules/mcp_client.py). Host only: none of these
are in socketIO._REMOTE_API_ALLOW, so the tier middleware answers a remote
session with 403 before they run, and each handler checks again. Every
state-changing route needs the CSRF token.

    GET    /api/mcp/servers                 {servers: [{id, transport, enabled, state, tool_count,
                                                        last_error, read_only, protocol, command, args, url}]}
    POST   /api/mcp/servers                 add or update one server, save, reconnect it
    POST   /api/mcp/servers/{id}/toggle     {"enabled": bool}
    POST   /api/mcp/servers/{id}/reconnect
    DELETE /api/mcp/servers/{id}
    GET    /api/mcp/tools                   {tools: [{name, server, description}]}
"""

from __future__ import annotations

from modules.mcp_client import MCPError


def register(router, server) -> None:
    from aiohttp import web
    from modules.socketIO import _json_error, _HOST_ONLY_MSG

    def registry():
        return getattr(server.signals, "mcp", None)

    def gate(request):
        if not server._is_local(request):
            return _json_error(403, _HOST_ONLY_MSG)
        if not server._current_user(request):
            return _json_error(401, "Not logged in.")
        return None

    async def body_of(request, need_json=True):
        err = gate(request)
        if err:
            return None, err
        if not server._origin_ok(request):
            return None, _json_error(403, "Bad origin.")
        try:
            body = await request.json()
        except Exception:
            if need_json:
                return None, _json_error(400, "Invalid JSON.")
            body = {}
        body = body if isinstance(body, dict) else {}
        ok, msg = server._require_csrf(request, body)
        return (body, None) if ok else (None, _json_error(403, msg))

    async def run(coro_fn, *args):
        reg = registry()
        if reg is None:
            return None, _json_error(503, "MCP isn't loaded.")
        try:
            return await reg.admin(coro_fn(reg), *args), None
        except KeyError:
            return None, _json_error(404, "No MCP server with that id.")
        except ValueError as e:
            return None, _json_error(400, str(e))
        except MCPError as e:
            return None, _json_error(503, str(e))
        except OSError as e:
            return None, _json_error(500, f"Couldn't save the MCP config: {e}")

    async def list_servers(request):
        err = gate(request)
        if err:
            return err
        reg = registry()
        return web.json_response({"servers": reg.list_servers() if reg is not None else []})

    async def list_tools(request):
        err = gate(request)
        if err:
            return err
        reg = registry()
        return web.json_response({"tools": reg.list_tools() if reg is not None else []})

    async def upsert(request):
        body, err = await body_of(request)
        if err:
            return err
        raw = body.get("server") if isinstance(body.get("server"), dict) else \
            {k: v for k, v in body.items() if k != "csrf"}
        row, err = await run(lambda r: r.upsert, raw)
        return err or web.json_response({"ok": True, "server": row})

    async def toggle(request):
        body, err = await body_of(request)
        if err:
            return err
        if not isinstance(body.get("enabled"), bool):
            return _json_error(400, "Send {\"enabled\": true|false}.")
        row, err = await run(lambda r: r.toggle, request.match_info["sid"], body["enabled"])
        return err or web.json_response({"ok": True, "enabled": body["enabled"], "server": row})

    async def reconnect(request):
        _, err = await body_of(request, need_json=False)
        if err:
            return err
        row, err = await run(lambda r: r.reconnect, request.match_info["sid"])
        return err or web.json_response({"ok": True, "server": row})

    async def delete(request):
        _, err = await body_of(request, need_json=False)
        if err:
            return err
        _, err = await run(lambda r: r.remove, request.match_info["sid"])
        return err or web.json_response({"ok": True})

    router.add_get("/api/mcp/servers", list_servers)
    router.add_post("/api/mcp/servers", upsert)
    router.add_post("/api/mcp/servers/{sid}/toggle", toggle)
    router.add_post("/api/mcp/servers/{sid}/reconnect", reconnect)
    router.add_delete("/api/mcp/servers/{sid}", delete)
    router.add_get("/api/mcp/tools", list_tools)

"""
Shared UI state for the web app and the islands: theme and hybrid (cloud
escalation) switch.

    web     GET  /api/ui/state          {theme, themes, hybrid}  (also for remote sessions)
            PUT  /api/ui/theme          {"theme": "sakura"}      (host only, CSRF)
            PUT  /api/ui/hybrid         {"enabled": true}        (host only, CSRF)
    island  ui_state / theme_set / hybrid_set over its WebSocket

Changes are broadcast to everyone (theme_update, hybrid_mode_update) by
theme_store / hybrid_mode through ui_sync.
"""

from __future__ import annotations

import asyncio

from modules import hybrid_mode, theme_store


def register(router, server) -> None:
    from aiohttp import web
    from modules.socketIO import _json_error

    async def get_state(request):
        if not server._current_user(request):
            return _json_error(401, "Not logged in.")
        return web.json_response(await asyncio.get_running_loop().run_in_executor(None, theme_store.state))

    async def body_of(request):
        if not server._origin_ok(request) or not server._current_user(request):
            return None, _json_error(401, "Not logged in.")
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        ok, err = server._require_csrf(request, body)
        return (body, None) if ok else (None, _json_error(403, err))

    async def put_theme(request):
        body, err = await body_of(request)
        if err:
            return err
        try:
            theme_store.set_theme(str(body.get("theme") or ""))
        except ValueError as e:
            return _json_error(400, str(e))
        return web.json_response(theme_store.state())

    async def put_hybrid(request):
        body, err = await body_of(request)
        if err:
            return err
        if not isinstance(body.get("enabled"), bool):
            return _json_error(400, "Send {\"enabled\": true|false}.")
        st = await asyncio.get_running_loop().run_in_executor(None, hybrid_mode.set_enabled, body["enabled"])
        return web.json_response({"hybrid": st})

    router.add_get("/api/ui/state", get_state)
    router.add_put("/api/ui/theme", put_theme)
    router.add_put("/api/ui/hybrid", put_hybrid)


def install_island_handlers(server=None) -> None:
    from modules import hitl_server
    reg = server.register_handler if server else hitl_server.register_handler
    send = server.send if server else hitl_server.send

    async def ui_state(ws, data):
        st = await asyncio.get_running_loop().run_in_executor(None, theme_store.state)
        await send(ws, {"type": "ui_state", "id": data.get("id"), **st})

    async def theme_set(ws, data):
        try:
            theme_store.set_theme(str(data.get("theme") or ""))
        except ValueError as e:
            return await send(ws, {"type": "error", "id": data.get("id"), "message": str(e)})
        await send(ws, {"type": "ui_state", "id": data.get("id"), **theme_store.state()})

    async def hybrid_set(ws, data):
        if not isinstance(data.get("enabled"), bool):
            return await send(ws, {"type": "error", "id": data.get("id"), "message": "enabled must be true or false"})
        st = await asyncio.get_running_loop().run_in_executor(None, hybrid_mode.set_enabled, data["enabled"])
        await send(ws, {"type": "hybrid_mode_update", "id": data.get("id"), **st})

    reg("ui_state", ui_state)
    reg("theme_set", theme_set)
    reg("hybrid_set", hybrid_set)

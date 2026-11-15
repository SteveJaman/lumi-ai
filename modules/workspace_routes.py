"""
/api/workspace/* routes for the web client (Settings → Connections and the
Workspace view). All of them are host-only: none is in socketIO's
_REMOTE_API_ALLOW, so a tunnel visitor gets 403 before reaching a handler.

Handlers follow socketIO.py's conventions: origin check, login check, and
CSRF on anything that changes state. Blocking provider calls run in the
default executor so the event loop stays responsive.
"""

import asyncio
import html
import time
from urllib.parse import urlparse

from aiohttp import web

from modules.workspace_hub import NotConnected, ProviderError

_LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}
_MAX_CLIENT_JSON = 20_000


def register(router, server) -> None:
    from modules.socketIO import _json_error

    def hub():
        return getattr(server.signals, "workspace_hub", None)

    async def gate(request, mutate=False):
        """-> (error_response, body). Origin + login (+ CSRF when mutating)."""
        if not server._origin_ok(request):
            return _json_error(403, "Bad origin."), {}
        if not server._current_user(request):
            return _json_error(401, "Not logged in."), {}
        if hub() is None:
            return _json_error(503, "Workspace isn't available."), {}
        body = {}
        if mutate:
            try:
                body = await request.json()
            except Exception:
                body = {}
            if not isinstance(body, dict):
                body = {}
            ok, err = server._require_csrf(request, body)
            if not ok:
                return _json_error(403, err), {}
        return None, body

    async def run(fn, *args):
        """Run a blocking hub call; map its errors to JSON responses."""
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, fn, *args)
        except NotConnected as e:
            return _json_error(409, str(e))
        except ValueError as e:                  # includes InvalidInput
            return _json_error(400, str(e))
        except ProviderError as e:
            return _json_error(502, str(e))
        return web.json_response(result if result is not None else {"ok": True})

    def flag(request, name):
        return request.query.get(name, "").lower() in ("1", "true", "yes")

    # ── status / snapshot ──────────────────────────────────────────

    async def status(request):
        err, _ = await gate(request)
        return err or web.json_response(hub().status())

    async def snapshot(request):
        err, _ = await gate(request)
        return err or web.json_response(hub().get_compressed_snapshot())

    # ── Google ─────────────────────────────────────────────────────

    async def google_client(request):
        err, body = await gate(request, True)
        if err:
            return err
        raw = body.get("json")
        if not isinstance(raw, str) or not raw.strip() or len(raw) > _MAX_CLIENT_JSON:
            return _json_error(400, "Paste the contents of the OAuth client JSON file.")
        return await run(lambda: {"type": hub().google.save_client(raw)})

    async def google_connect(request):
        err, _ = await gate(request, True)
        if err:
            return err
        host = urlparse(f"//{request.host}").hostname or ""
        if host not in _LOOPBACK:
            return _json_error(403, "Connect Google from the PC that runs Lumi, at 127.0.0.1:5000.")
        redirect = f"http://{request.host}/api/workspace/google/callback"
        return await run(lambda: {"auth_url": hub().google.begin(redirect), "redirect_uri": redirect})

    async def google_callback(request):
        """Google sends the browser back here with ?code=&state=."""
        def page(title, message, status=200):
            text = (f"<!doctype html><meta charset=utf-8><title>{html.escape(title)}</title>"
                    "<body style='font:16px system-ui;max-width:32rem;margin:4rem auto;padding:0 1rem'>"
                    f"<h2>{html.escape(title)}</h2><p>{html.escape(message)}</p>")
            return web.Response(text=text, status=status, content_type="text/html")

        if not server._current_user(request):
            return page("Sign in to Lumi first", "Open Lumi at 127.0.0.1:5000, sign in, and connect again.", 401)
        if hub() is None:
            return page("Workspace isn't available", "Lumi's workspace module isn't running.", 503)
        if request.query.get("error"):
            return page("Google sign-in cancelled", f'Google said: {request.query["error"][:80]}', 400)
        code, state = request.query.get("code", ""), request.query.get("state", "")
        if not code or not state:
            return page("Something's missing", "That link doesn't carry a sign-in code.", 400)
        loop = asyncio.get_running_loop()
        try:
            info = await loop.run_in_executor(None, hub().google.finish, state, code)
        except ProviderError as e:
            return page("Couldn't connect Google", str(e), 400)
        need = [k for k, label in (("calendar.events", "Calendar"), ("gmail.modify", "Gmail"), ("gmail.send", "sending mail"))
                if not any(g.endswith(k) for g in info["granted"])]
        note = (" Some permissions were left unticked (" + ", ".join(need) + "), so those features won't work."
                if need else "")
        return page("Google connected", f"Signed in as {info.get('email') or 'your account'}. "
                                        f"You can close this tab and go back to Lumi.{note}")

    async def google_disconnect(request):
        err, _ = await gate(request, True)
        return err or await run(lambda: (hub().disconnect_google(), hub().status())[1])

    # ── KSU ────────────────────────────────────────────────────────

    async def ksu_connect(request):
        err, body = await gate(request, True)
        if err:
            return err
        mode = body.get("mode")
        if mode == "graph":
            return await run(lambda: hub().set_ksu_graph(str(body.get("client_id") or ""),
                                                         str(body.get("tenant") or "")))
        if mode == "gmail":
            return await run(lambda: (hub().set_ksu_gmail(str(body.get("label") or "")), hub().status())[1])
        return _json_error(400, "mode must be 'graph' or 'gmail'.")

    async def ksu_device(request):
        err, _ = await gate(request)
        return err or web.json_response({**hub().microsoft.device_state(), "now": time.time()})

    async def ksu_disconnect(request):
        err, _ = await gate(request, True)
        return err or await run(lambda: (hub().disconnect_ksu(), hub().status())[1])

    # ── Calendar ───────────────────────────────────────────────────

    async def calendar(request):
        err, _ = await gate(request)
        if err:
            return err
        try:
            days = int(request.query.get("days", "14"))
        except ValueError:
            return _json_error(400, "days must be a number.")
        return await run(hub().get_events, days, flag(request, "refresh"))

    async def calendar_create(request):
        err, b = await gate(request, True)
        if err:
            return err
        return await run(lambda: hub().create_event(
            str(b.get("title") or ""), str(b.get("start") or ""), str(b.get("end") or "") or None,
            bool(b.get("all_day")), str(b.get("location") or "")[:200], str(b.get("description") or "")[:2000],
            None, str(b.get("timezone") or "")[:60] or None))

    # ── Mail ───────────────────────────────────────────────────────

    async def mail_list(request):
        err, _ = await gate(request)
        if err:
            return err
        try:
            limit = int(request.query.get("limit", "30"))
        except ValueError:
            return _json_error(400, "limit must be a number.")
        return await run(hub().get_mail, request.query.get("filter", "all"), flag(request, "refresh"), limit)

    async def mail_get(request):
        err, _ = await gate(request)
        return err or await run(hub().get_message, request.match_info["provider"], request.match_info["mid"])

    async def mail_read(request):
        err, b = await gate(request, True)
        if err:
            return err
        read = b.get("read", True)
        if not isinstance(read, bool):
            return _json_error(400, "'read' must be true or false.")
        return await run(hub().set_read, request.match_info["provider"], request.match_info["mid"], read)

    async def mail_archive(request):
        err, _ = await gate(request, True)
        return err or await run(hub().archive, request.match_info["provider"], request.match_info["mid"])

    async def mail_ask(request):
        """"Ask Lumi": put the thread into the chat as a user message.

        Deliberately NOT flagged from_host: the email text is untrusted and
        must not be able to pass for the owner's words (e.g. Minecraft
        orders, game_orders.py)."""
        err, b = await gate(request, True)
        if err:
            return err
        loop = asyncio.get_running_loop()
        try:
            text = await loop.run_in_executor(None, hub().build_ask_text, request.match_info["provider"],
                                              request.match_info["mid"], str(b.get("action") or "summarize"))
        except NotConnected as e:
            return _json_error(409, str(e))
        except ValueError as e:
            return _json_error(400, str(e))
        except ProviderError as e:
            return _json_error(502, str(e))
        sig = server.signals
        sig.history.append({"role": "user", "content": text})
        sig.new_message = True
        sig.last_message_time = time.time()
        if hasattr(sig, "save_history"):
            sig.save_history()
        await server.sio.emit("web_message", {"role": "user", "content": text, "ts": time.time(), "attachments": []})
        return web.json_response({"ok": True})

    add = router.add_get
    post = router.add_post
    add("/api/workspace/status", status)
    add("/api/workspace/snapshot", snapshot)
    post("/api/workspace/google/client", google_client)
    post("/api/workspace/google/connect", google_connect)
    add("/api/workspace/google/callback", google_callback)
    post("/api/workspace/google/disconnect", google_disconnect)
    post("/api/workspace/ksu/connect", ksu_connect)
    add("/api/workspace/ksu/device", ksu_device)
    post("/api/workspace/ksu/disconnect", ksu_disconnect)
    add("/api/workspace/calendar", calendar)
    post("/api/workspace/calendar/events", calendar_create)
    add("/api/workspace/mail", mail_list)
    add("/api/workspace/mail/{provider}/{mid}", mail_get)
    post("/api/workspace/mail/{provider}/{mid}/read", mail_read)
    post("/api/workspace/mail/{provider}/{mid}/archive", mail_archive)
    post("/api/workspace/mail/{provider}/{mid}/ask", mail_ask)

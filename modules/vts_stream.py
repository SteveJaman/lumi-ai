"""
Live VTube Studio view: captures the VTS window with mss and serves it as
MJPEG (multipart/x-mixed-replace), so a plain <img> shows Lumi's avatar.

    web app   GET /api/vts/stream             (modules/socketIO.py; login cookie, host-only)
    island    GET /vts/stream?t=<ticket>      (modules/hitl_server.py, port 5090)

An <img> can't send the island's token, so the island first asks for a
one-time ticket over its authenticated WebSocket ({"type":
"vts_stream_ticket"}) and puts it in the URL. Tickets expire after
TICKET_TTL_S and only open a stream; nothing else accepts them.

Cost control:
  * Capture runs only while at least one viewer is connected, and stops
    IDLE_STOP_S (30 s) after the last one leaves. One capture thread serves
    every viewer; frames are encoded once.
  * FPS (12) and MAX_WIDTH (640) keep a 1080p VTS window to a few % CPU.
  * At most MAX_CLIENTS streams at once.

mss grabs the screen region under the window, so another window on top of
VTS shows up too, and a minimized VTS can't be captured; a placeholder
frame says so. (VTS's own Spout/virtual-camera output is the zero-cost
alternative if you already use OBS.)
"""

from __future__ import annotations

import asyncio
import io
import secrets
import sys
import threading
import time
from typing import Callable, Dict, Optional, Tuple

FPS = 12
JPEG_QUALITY = 70
MAX_WIDTH = 640
IDLE_STOP_S = 30.0
MAX_CLIENTS = 4
TICKET_TTL_S = 120.0
VTS_TITLE = "VTube Studio"
VTS_PROCESS = "vtube studio.exe"
BOUNDARY = "lumiframe"

Rect = Tuple[int, int, int, int]          # left, top, width, height


# ─── window lookup (Windows) ──────────────────────────────────────────

def find_vts_rect() -> Tuple[Optional[Rect], str]:
    """Screen rectangle of the VTube Studio client area, or (None, why)."""
    if sys.platform != "win32":
        return None, "VTube Studio capture works on Windows only"
    import ctypes
    from ctypes import wintypes
    from modules.windows_agent import process_name

    u32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if not u32.IsWindowVisible(hwnd):
            return True
        n = u32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        u32.GetWindowTextW(hwnd, buf, n + 1)
        if VTS_TITLE.lower() in buf.value.lower():
            pid = wintypes.DWORD()
            u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            # A browser tab titled "VTube Studio ..." isn't it.
            if process_name(pid.value).lower() == VTS_PROCESS:
                found.append(hwnd)
                return False
        return True

    u32.EnumWindows(cb, 0)
    if not found:
        return None, "VTube Studio isn't running"
    hwnd = found[0]
    if u32.IsIconic(hwnd):
        return None, "VTube Studio is minimized"
    rect = wintypes.RECT()
    u32.GetClientRect(hwnd, ctypes.byref(rect))
    pt = wintypes.POINT(0, 0)
    u32.ClientToScreen(hwnd, ctypes.byref(pt))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w < 16 or h < 16:
        return None, "VTube Studio's window is too small"
    return (pt.x, pt.y, w, h), ""


# ─── capture ──────────────────────────────────────────────────────────

def _placeholder(text: str) -> bytes:
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (MAX_WIDTH, MAX_WIDTH * 9 // 16), (14, 16, 24))
    d = ImageDraw.Draw(img)
    try:
        w = d.textlength(text)
    except Exception:
        w = len(text) * 6
    d.text(((img.width - w) / 2, img.height / 2 - 6), text, fill=(150, 160, 180))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


def encode_jpeg(bgra: bytes, size: Tuple[int, int]) -> bytes:
    from PIL import Image
    img = Image.frombuffer("RGB", size, bgra, "raw", "BGRX", 0, 1)
    if img.width > MAX_WIDTH:
        img = img.resize((MAX_WIDTH, max(1, img.height * MAX_WIDTH // img.width)), Image.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


class VTSStream:
    """One capture thread shared by every viewer."""

    def __init__(self, locate: Callable[[], Tuple[Optional[Rect], str]] = find_vts_rect,
                 grab: Optional[Callable[[Rect], Tuple[bytes, Tuple[int, int]]]] = None,
                 fps: float = FPS, idle_stop_s: float = IDLE_STOP_S):
        self._locate = locate
        self._grab = grab
        self.fps = fps
        self.idle_stop_s = idle_stop_s
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.frame: Optional[bytes] = None
        self.frame_id = 0
        self.clients = 0
        self._last_client_at = time.monotonic()
        self._placeholders: Dict[str, bytes] = {}
        self._tickets: Dict[str, float] = {}

    # -- viewers -------------------------------------------------------
    def join(self) -> bool:
        with self._lock:
            if self.clients >= MAX_CLIENTS:
                return False
            self.clients += 1
            self._last_client_at = time.monotonic()
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(target=self._run, name="vts-stream", daemon=True)
                self._thread.start()
        return True

    def leave(self) -> None:
        with self._lock:
            self.clients = max(0, self.clients - 1)
            self._last_client_at = time.monotonic()

    @property
    def capturing(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop(self) -> None:
        self._stop.set()

    # -- tickets (island) ----------------------------------------------
    def issue_ticket(self) -> str:
        now = time.monotonic()
        with self._lock:
            self._tickets = {t: exp for t, exp in self._tickets.items() if exp > now}
            if len(self._tickets) > 32:
                self._tickets.pop(min(self._tickets, key=self._tickets.get))
            t = secrets.token_urlsafe(24)
            self._tickets[t] = now + TICKET_TTL_S
        return t

    def take_ticket(self, t: str) -> bool:
        """Single use: a leaked URL can't be replayed."""
        with self._lock:
            exp = self._tickets.pop(t or "", None)
        return exp is not None and exp > time.monotonic()

    # -- capture thread ------------------------------------------------
    def _publish(self, jpeg: bytes) -> None:
        with self._lock:
            self.frame = jpeg
            self.frame_id += 1

    def _placeholder(self, why: str) -> bytes:
        if why not in self._placeholders:
            try:
                self._placeholders[why] = _placeholder(why)
            except Exception:
                self._placeholders[why] = b""
        return self._placeholders[why]

    def _run(self) -> None:
        interval = 1.0 / self.fps
        sct = None
        try:
            if self._grab is None:
                import mss
                sct = (getattr(mss, "MSS", None) or mss.mss)()

                def grab(r: Rect):
                    shot = sct.grab({"left": r[0], "top": r[1], "width": r[2], "height": r[3]})
                    return shot.bgra, shot.size
            else:
                grab = self._grab
            rect, why, located_at = None, "", 0.0
            while not self._stop.is_set():
                t0 = time.monotonic()
                with self._lock:
                    idle = self.clients == 0 and t0 - self._last_client_at > self.idle_stop_s
                if idle:
                    break
                if rect is None or t0 - located_at > 1.0:     # windows move; re-find once a second
                    rect, why = self._locate()
                    located_at = t0
                try:
                    if rect is None:
                        self._publish(self._placeholder(why))
                    else:
                        data, size = grab(rect)
                        self._publish(encode_jpeg(data, size))
                except Exception as e:
                    rect = None
                    self._publish(self._placeholder(f"capture failed: {type(e).__name__}"))
                self._stop.wait(max(0.0, interval - (time.monotonic() - t0)))
        finally:
            if sct is not None:
                sct.close()
            with self._lock:
                self.frame = None

    # -- HTTP ----------------------------------------------------------
    async def mjpeg(self, request):
        """aiohttp handler body: stream frames until the viewer leaves."""
        from aiohttp import web
        if not self.join():
            return web.Response(status=503, text="too many viewers")
        resp = web.StreamResponse(headers={
            "Content-Type": f"multipart/x-mixed-replace; boundary={BOUNDARY}",
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
        })
        try:
            await resp.prepare(request)
            sent = -1
            while True:
                with self._lock:
                    frame, fid = self.frame, self.frame_id
                if frame and fid != sent:
                    await resp.write(
                        f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(frame)}\r\n\r\n".encode()
                        + frame + b"\r\n")
                    sent = fid
                if not self.capturing:
                    break
                await asyncio.sleep(1.0 / (self.fps * 2))
        except (ConnectionResetError, ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self.leave()
        return resp


_stream: Optional[VTSStream] = None


def instance() -> VTSStream:
    global _stream
    if _stream is None:
        _stream = VTSStream()
    return _stream


# ─── route helpers ────────────────────────────────────────────────────

def web_handler(server):
    """GET /api/vts/stream for the web app (socketIO.py): needs a login."""
    async def handle(request):
        from modules.socketIO import _json_error
        if not server._current_user(request):
            return _json_error(401, "Not logged in.")
        return await instance().mjpeg(request)
    return handle


async def hitl_handler(request):
    """GET /vts/stream?t=<ticket> on the approval server (island)."""
    from aiohttp import web
    if not instance().take_ticket(request.query.get("t", "")):
        return web.Response(status=403, text="ticket missing or expired")
    return await instance().mjpeg(request)


async def hitl_ticket(server, ws, data) -> None:
    """{"type":"vts_stream_ticket"} over the island socket -> a ticket and
    the path to open."""
    await server.send(ws, {"type": "vts_stream_ticket", "id": data.get("id"),
                           "ticket": instance().issue_ticket(), "path": "/vts/stream"})

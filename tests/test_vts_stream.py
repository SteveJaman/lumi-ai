"""modules/vts_stream.py: MJPEG serving, idle shutdown, island tickets."""

import asyncio
import time

import aiohttp
from aiohttp import web

from modules import vts_stream


def fake_grab(rect):
    w, h = rect[2], rect[3]
    return bytes([40, 80, 160, 255]) * (w * h), (w, h)


def make_stream(**kw):
    return vts_stream.VTSStream(locate=lambda: ((0, 0, 320, 180), ""), grab=fake_grab,
                                fps=30, **kw)


async def serve(handler):
    app = web.Application()
    app.router.add_get("/s", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}/s"


async def read_frames(url, n, params=None):
    frames = []
    async with aiohttp.ClientSession() as s:
        async with s.get(url, params=params) as r:
            assert r.status == 200
            assert r.headers["Content-Type"].startswith("multipart/x-mixed-replace")
            buf = b""
            async for chunk in r.content.iter_any():
                buf += chunk
                while b"\r\n\r\n" in buf:
                    head, rest = buf.split(b"\r\n\r\n", 1)
                    length = int(head.split(b"Content-Length: ")[1].split(b"\r\n")[0])
                    if len(rest) < length + 2:
                        break
                    frames.append(rest[:length])
                    buf = rest[length + 2:]
                if len(frames) >= n:
                    return frames
    return frames


def test_streams_jpeg_frames_and_stops_capturing_when_nobody_watches():
    stream = make_stream(idle_stop_s=0.3)

    async def go():
        runner, url = await serve(stream.mjpeg)
        try:
            frames = await read_frames(url, 3)
            assert len(frames) == 3 and all(f[:2] == b"\xff\xd8" for f in frames)      # JPEG SOI
            assert stream.capturing
            await asyncio.sleep(0.2)
            assert stream.clients == 0
            await asyncio.sleep(0.6)
            assert not stream.capturing                                                # idle: thread gone
        finally:
            await runner.cleanup()
    asyncio.run(go())


def test_frames_are_downscaled():
    from PIL import Image
    import io
    jpeg = vts_stream.encode_jpeg(*fake_grab((0, 0, 1920, 1080)))
    img = Image.open(io.BytesIO(jpeg))
    assert img.size == (vts_stream.MAX_WIDTH, vts_stream.MAX_WIDTH * 1080 // 1920)


def test_placeholder_when_vts_is_missing():
    stream = vts_stream.VTSStream(locate=lambda: (None, "VTube Studio isn't running"), grab=fake_grab, fps=30)

    async def go():
        runner, url = await serve(stream.mjpeg)
        try:
            frames = await read_frames(url, 1)
            assert frames and frames[0][:2] == b"\xff\xd8"
        finally:
            await runner.cleanup()
    asyncio.run(go())


def test_viewer_cap():
    stream = make_stream()
    assert all(stream.join() for _ in range(vts_stream.MAX_CLIENTS))
    assert not stream.join()
    for _ in range(vts_stream.MAX_CLIENTS):
        stream.leave()
    stream.stop()


def test_island_tickets_are_single_use_and_expire(monkeypatch):
    stream = make_stream()
    monkeypatch.setattr(vts_stream, "_stream", stream)

    async def go():
        runner, url = await serve(vts_stream.hitl_handler)
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url) as r:
                    assert r.status == 403
                async with s.get(url, params={"t": "made-up"}) as r:
                    assert r.status == 403
            t = stream.issue_ticket()
            frames = await read_frames(url, 1, params={"t": t})
            assert frames
            async with aiohttp.ClientSession() as s:
                async with s.get(url, params={"t": t}) as r:                       # replay
                    assert r.status == 403
            old = stream.issue_ticket()
            stream._tickets[old] = time.monotonic() - 1                               # expired
            async with aiohttp.ClientSession() as s:
                async with s.get(url, params={"t": old}) as r:
                    assert r.status == 403
        finally:
            stream.stop()
            await runner.cleanup()
    asyncio.run(go())


def test_web_route_needs_a_login(monkeypatch):
    stream = make_stream()
    monkeypatch.setattr(vts_stream, "_stream", stream)
    server = type("S", (), {"_current_user": lambda self, r: None})()

    async def go():
        runner, url = await serve(vts_stream.web_handler(server))
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url) as r:
                    assert r.status == 401
            assert not stream.capturing
        finally:
            await runner.cleanup()
    asyncio.run(go())


def test_hitl_ticket_message():
    sent = []

    class Srv:
        async def send(self, ws, payload):
            sent.append(payload)
    asyncio.run(vts_stream.hitl_ticket(Srv(), None, {"id": 7}))
    assert sent[0]["id"] == 7 and sent[0]["path"] == "/vts/stream" and len(sent[0]["ticket"]) > 20

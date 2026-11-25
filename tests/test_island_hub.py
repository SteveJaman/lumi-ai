"""Lumi Island Hub: the shortcuts store (modules/shortcuts_manager.py), its
/api/shortcuts routes, the island bridge's shortcut and quick_note messages,
and the island files wiring the Hub tab and the Rust URL opener together."""

import asyncio
import json
import os
import re
import types

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from modules import island_bridge, shortcuts_manager as sm
from tests.test_island_bridge import Env, recv, run

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    path = str(tmp_path / "shortcuts.json")
    monkeypatch.setattr(sm, "PATH", path)
    monkeypatch.setattr(sm, "_listeners", [])
    return path


# ─── the store ──────────────────────────────────────────────────

def test_first_run_seeds_the_defaults(store):
    items = sm.list_shortcuts()
    assert [(i["title"], i["url"]) for i in items] == list(sm.DEFAULTS)
    assert len({i["id"] for i in items}) == 4
    with open(store, encoding="utf-8") as f:
        assert len(json.load(f)["shortcuts"]) == 4


def test_add_and_remove_persist_and_notify():
    seen = []
    sm.add_listener(seen.append)
    item = sm.add_shortcut("  Khan   Academy ", "khanacademy.org")
    assert item["title"] == "Khan Academy" and item["url"] == "https://khanacademy.org"
    assert sm.list_shortcuts()[-1] == item
    assert sm.remove_shortcut(item["id"]) is True
    assert sm.remove_shortcut(item["id"]) is False
    assert all(i["id"] != item["id"] for i in sm.list_shortcuts())
    assert len(seen) == 2 and len(seen[0]) == 5 and len(seen[1]) == 4


def test_removing_a_default_keeps_it_removed():
    first = sm.list_shortcuts()[0]
    sm.remove_shortcut(first["id"])
    assert first["title"] not in [i["title"] for i in sm.list_shortcuts()]


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "file:///C:/Windows/System32/calc.exe", "ms-settings:privacy",
    "https://", "https://user:pw@evil.com", "https://a b.com", "", "ftp://example.com",
])
def test_only_plain_web_links_are_accepted(url):
    with pytest.raises(sm.ShortcutError):
        sm.add_shortcut("x", url)


def test_title_defaults_to_the_site_name():
    assert sm.add_shortcut("", "https://www.reddit.com/r/python")["title"] == "Reddit"


def test_limit(monkeypatch):
    monkeypatch.setattr(sm, "MAX_SHORTCUTS", 5)
    sm.add_shortcut("One", "https://one.example")
    with pytest.raises(sm.ShortcutError, match="already have 5"):
        sm.add_shortcut("Two", "https://two.example")


def test_bad_entries_in_the_file_are_dropped_and_a_corrupt_file_is_not_reseeded(store):
    with open(store, "w", encoding="utf-8") as f:
        json.dump({"shortcuts": [{"id": "ok1", "title": "Ok", "url": "https://ok.example"},
                                 {"id": "bad", "title": "Bad", "url": "javascript:alert(1)"}, "junk"]}, f)
    assert [i["id"] for i in sm.list_shortcuts()] == ["ok1"]
    with open(store, "w", encoding="utf-8") as f:
        f.write("{not json")
    assert sm.list_shortcuts() == []


# ─── HTTP ───────────────────────────────────────────────────────

class FakeServer:
    def __init__(self, user=True, local=True, origin=True):
        self.user, self.local, self.origin = user, local, origin

    def _origin_ok(self, request): return self.origin
    def _current_user(self, request): return {"username": "me"} if self.user else None
    def _is_local(self, request): return self.local


def http(server, steps):
    async def t():
        app = web.Application()
        sm.register(app.router, server)
        async with TestClient(TestServer(app)) as c:
            return await steps(c)
    return asyncio.run(t())


def test_routes_list_add_delete():
    async def steps(c):
        r = await c.get("/api/shortcuts")
        assert r.status == 200 and len((await r.json())["shortcuts"]) == 4
        r = await c.post("/api/shortcuts", json={"title": "Docs", "url": "https://docs.python.org"})
        assert r.status == 201
        sid = (await r.json())["shortcut"]["id"]
        r = await c.post("/api/shortcuts", json={"title": "Bad", "url": "javascript:alert(1)"})
        assert r.status == 400 and "http" in (await r.json())["error"]
        r = await c.delete(f"/api/shortcuts/{sid}")
        assert r.status == 200
        r = await c.delete(f"/api/shortcuts/{sid}")
        assert r.status == 404
    http(FakeServer(), steps)


def test_routes_need_a_login_and_changes_need_the_host():
    async def anon(c):
        assert (await c.get("/api/shortcuts")).status == 401
    http(FakeServer(user=False), anon)

    async def remote(c):
        assert (await c.get("/api/shortcuts")).status == 200
        r = await c.post("/api/shortcuts", json={"title": "X", "url": "https://x.example"})
        assert r.status == 403
        assert (await c.delete("/api/shortcuts/whatever")).status == 403
    http(FakeServer(local=False), remote)


# ─── island bridge ──────────────────────────────────────────────

class FakeMemoryAPI:
    def __init__(self):
        self.saved = []

    def create_memory(self, text, importance=0.5, memory_type="manual"):
        self.saved.append((text, importance, memory_type))


async def ask(e, ws, payload, kind):
    await ws.send_json(payload)
    return await recv(ws, kind, timeout=5)


def test_bridge_shortcuts_round_trip_and_push():
    async def t():
        async with Env() as e:
            a = await e.connect(); await recv(a, "hello_ok")
            b = await e.connect(); await recv(b, "hello_ok")
            r = await ask(e, a, {"type": "get_shortcuts", "id": 1}, "shortcuts")
            assert r["id"] == 1 and len(r["shortcuts"]) == 4
            r = await ask(e, a, {"type": "shortcut_add", "id": 2, "title": "Notion", "url": "https://notion.so"}, "shortcuts")
            assert r["ok"] and r["shortcut"]["title"] == "Notion" and len(r["shortcuts"]) == 5
            pushed = await recv(b, "shortcuts_update", timeout=5)          # the other island hears it
            assert len(pushed["shortcuts"]) == 5
            r = await ask(e, a, {"type": "shortcut_add", "id": 3, "title": "x", "url": "file:///etc/passwd"}, "shortcuts")
            assert r["ok"] is False and "http" in r["error"]
            r = await ask(e, a, {"type": "shortcut_delete", "id": 4, "sid": r.get("sid", "nope")}, "shortcuts")
            assert r["ok"] is False and len(r["shortcuts"]) == 5
            sid = [s for s in r["shortcuts"] if s["title"] == "Notion"][0]["id"]
            r = await ask(e, a, {"type": "shortcut_delete", "id": 5, "sid": sid}, "shortcuts")
            assert r["ok"] and len(r["shortcuts"]) == 4
    run(t())


@pytest.mark.parametrize("text,kind,saved", [
    ("hw: lab 3 writeup", "homework", "Homework: lab 3 writeup"),
    ("finish the essay due friday", "homework", "Homework: finish the essay due friday"),
    ("buy: oat milk", "grocery", "Grocery list: oat milk"),
    ("eggs and bread", "grocery", "Grocery list: eggs and bread"),
    ("an app that sorts my photos", "idea", "Idea: an app that sorts my photos"),
])
def test_quick_note_is_saved_to_memory_with_its_kind(text, kind, saved):
    async def t():
        async with Env() as e:
            e.sig.memory = types.SimpleNamespace(API=FakeMemoryAPI())
            ws = await e.connect(); await recv(ws, "hello_ok")
            r = await ask(e, ws, {"type": "quick_note", "id": 9, "text": f"  {text} "}, "quick_note")
            assert r == {"type": "quick_note", "id": 9, "ok": True, "kind": kind}
            assert e.sig.memory.API.saved == [(saved, 0.8, "manual")]
    run(t())


def test_quick_note_kind_override_and_errors():
    async def t():
        async with Env() as e:
            ws = await e.connect(); await recv(ws, "hello_ok")
            r = await ask(e, ws, {"type": "quick_note", "text": "x"}, "quick_note")
            assert r["ok"] is False and "isn't loaded" in r["error"]
            e.sig.memory = types.SimpleNamespace(API=FakeMemoryAPI())
            r = await ask(e, ws, {"type": "quick_note", "text": "   "}, "quick_note")
            assert r["ok"] is False
            r = await ask(e, ws, {"type": "quick_note", "text": "milk", "kind": "idea"}, "quick_note")
            assert r["kind"] == "idea" and e.sig.memory.API.saved[-1][0] == "Idea: milk"
    run(t())


# ─── island files ───────────────────────────────────────────────

def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def test_island_wires_the_hub_and_the_url_opener():
    html = read("island", "src", "index.html")
    js = read("island", "src", "island.js")
    rs = read("island", "src-tauri", "src", "main.rs")
    for tab in ("home", "hub", "chat", "calendar", "pipeline"):
        assert f'data-app="{tab}"' in html and f'id="app-{tab}"' in html
    assert "invoke('open_external_url', { url })" in js
    for msg in ("get_shortcuts", "shortcut_add", "shortcut_delete", "quick_note", "shortcuts_update"):
        assert f"'{msg}'" in js
    assert "fn open_external_url(url: String)" in rs
    assert re.search(r"generate_handler!\[[^\]]*open_external_url", rs, re.S)
    assert "iframe" not in html.lower()

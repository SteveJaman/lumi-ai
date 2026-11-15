"""Theme engine files, hybrid (cloud escalation) routing, drag-and-drop knowledge,
and the routes / island messages that sync them."""

import asyncio
import base64
import filecmp
import json
import os
import re
import types

import aiohttp
import pytest

import constants
from modules import hybrid_mode, knowledge, theme_store, ui_routes, ui_sync
from tests.conftest import LOCAL, PUBLIC

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
THEMES = os.path.join(ROOT, "web", "themes")
COLOR = re.compile(r"^(#[0-9A-Fa-f]{6}|rgba?\([\d.,\s]+\))$")


# ─── themes ───────────────────────────────────────────────────────────

def theme_ids():
    with open(os.path.join(THEMES, "index.json"), encoding="utf-8") as f:
        return [t["id"] for t in json.load(f)["themes"]]


@pytest.mark.parametrize("tid", theme_ids())
def test_theme_manifest_is_complete(tid):
    with open(os.path.join(THEMES, tid, "theme.json"), encoding="utf-8") as f:
        m = json.load(f)
    assert m["id"] == tid and m["name"]
    for k in ("primary", "secondary", "bg", "text"):
        assert COLOR.match(m["palette"][k]), (tid, k)
    if m.get("style"):
        assert os.path.isfile(os.path.join(THEMES, tid, m["style"]))
    bg = m.get("background")
    if bg:
        assert os.path.isfile(os.path.join(THEMES, tid, bg["script"]))
        assert 0 < bg["fps"] <= 30                                   # the engine caps at 30 anyway
        src = open(os.path.join(THEMES, tid, bg["script"]), encoding="utf-8").read()
        assert f"LumiThemes.registerBackground('{tid}'" in src
    if m.get("room"):
        assert COLOR.match(m["room"]["tint"]) and 0 < m["room"]["alpha"] <= 0.5
    assert m.get("cursor") in (None, "sparkle")


def test_theme_css_is_scoped_to_its_theme():
    for tid in theme_ids():
        css = open(os.path.join(THEMES, tid, "style.css"), encoding="utf-8").read()
        rules = re.findall(r"^(html[^{]*)\{", css, re.M)
        assert all(f'data-lumi-theme="{tid}"' in r for r in rules), tid


def test_sakura_has_the_requested_effects():
    css = open(os.path.join(THEMES, "sakura", "style.css"), encoding="utf-8").read()
    assert "clip-path" in css and "conic-gradient" in css and "backdrop-filter: blur(30px)" in css
    assert ".msg-bubble" in css and ".bub" in css                    # holographic bubbles on both surfaces
    assert json.load(open(os.path.join(THEMES, "sakura", "theme.json"), encoding="utf-8"))["cursor"] == "sparkle"


def test_island_ships_the_same_theme_files():
    cmp = filecmp.dircmp(THEMES, os.path.join(ROOT, "island", "src", "themes"))

    def same(c):
        return not (c.left_only or c.right_only or c.diff_files) and all(same(s) for s in c.subdirs.values())
    assert same(cmp), "run python scripts/sync_themes.py"
    for f in ("theme-engine.js", "pipeline.js"):
        assert filecmp.cmp(os.path.join(ROOT, "web", f), os.path.join(ROOT, "island", "src", f), shallow=False), f


def test_engine_caps_fps_and_pauses_when_hidden():
    src = open(os.path.join(ROOT, "web", "theme-engine.js"), encoding="utf-8").read()
    assert "MAX_FPS = 30" in src and "document.hidden" in src and "prefers-reduced-motion" in src


def test_theme_store(tmp_path, monkeypatch):
    monkeypatch.setattr(theme_store, "PATH", str(tmp_path / "ui_theme.json"))
    sent = []
    monkeypatch.setattr(ui_sync, "broadcast", lambda ev, p: sent.append((ev, p)))
    assert theme_store.current() == "default"
    theme_store.set_theme("sakura")
    assert theme_store.current() == "sakura" and sent == [("theme_update", {"theme": "sakura"})]
    with pytest.raises(ValueError):
        theme_store.set_theme("vaporwave")


# ─── hybrid ───────────────────────────────────────────────────────────

@pytest.fixture
def hybrid(tmp_path, monkeypatch):
    monkeypatch.setattr(hybrid_mode, "PATH", str(tmp_path / "hybrid_mode.json"))
    monkeypatch.setitem(hybrid_mode._state, "loaded", False)
    monkeypatch.setitem(hybrid_mode._state, "enabled", False)
    monkeypatch.setattr(constants, "HYBRID_MODE", False, raising=False)
    monkeypatch.setattr(constants, "CLOUD_ESCALATION_MODEL", "anthropic:claude-opus-5-5", raising=False)
    for k, v in (("MODEL_NAME", "dolphin3:8b"), ("FAST_MODEL_NAME", "dolphin3:8b"),
                 ("AGENT_MODEL_NAME", "qwen3:8b"), ("VISION_MODEL_NAME", "qwen2.5vl:latest")):
        monkeypatch.setattr(constants, k, v, raising=False)
    sent = []
    monkeypatch.setattr(ui_sync, "broadcast", lambda ev, p: sent.append((ev, p)))
    return sent


def test_local_mode_never_leaves_ollama(hybrid, monkeypatch):
    monkeypatch.setattr(hybrid_mode, "cloud_ready", lambda: None)
    for tier in ("haiku", "sonnet", "opus"):
        assert hybrid_mode.model_for(tier, "qwen3:8b") == "qwen3:8b"
    st = hybrid_mode.status()
    assert st["fully_local"] and st["badge"] == "100% Local Privacy" and not hybrid_mode.claude_code_allowed()


def test_escalation_sends_only_opus_to_the_cloud(hybrid, monkeypatch):
    monkeypatch.setattr(hybrid_mode, "cloud_ready", lambda: None)
    hybrid_mode.set_enabled(True)
    assert hybrid_mode.model_for("opus", "qwen3:8b") == "anthropic:claude-opus-5-5"
    assert hybrid_mode.model_for("sonnet", "qwen3:8b") == "qwen3:8b"
    assert hybrid_mode.model_for("haiku", "dolphin3:8b") == "dolphin3:8b"
    assert hybrid[-1][0] == "hybrid_mode_update" and hybrid[-1][1]["enabled"] is True
    assert hybrid_mode.status()["badge"] == "Cloud Escalation on" and hybrid_mode.claude_code_allowed()


def test_escalation_without_an_api_key_stays_local(hybrid, monkeypatch):
    monkeypatch.setattr(hybrid_mode, "cloud_ready", lambda: "No API key for anthropic.")
    hybrid_mode.set_enabled(True)
    assert hybrid_mode.model_for("opus", "qwen3:8b") == "qwen3:8b"
    st = hybrid_mode.status()
    assert st["cloud_ready"] is False and "API key" in st["cloud_problem"]


def test_the_switch_persists(hybrid):
    hybrid_mode.set_enabled(True)
    hybrid_mode._state["loaded"] = False
    hybrid_mode._state["enabled"] = False
    assert hybrid_mode.enabled() is True


def test_badge_is_honest_about_cloud_models_in_constants(hybrid, monkeypatch):
    monkeypatch.setattr(constants, "MODEL_NAME", "openai:gpt-4o", raising=False)
    st = hybrid_mode.status()
    assert not st["fully_local"] and st["badge"] == "Cloud models configured"


# ─── knowledge ────────────────────────────────────────────────────────

@pytest.fixture
def library(tmp_path, monkeypatch):
    lib = tmp_path / "library"
    monkeypatch.setattr(knowledge, "LIBRARY_DIR", str(lib))
    monkeypatch.setattr(knowledge, "INCOMING", str(lib / ".incoming"))
    monkeypatch.setattr(knowledge, "index_single_file", lambda signals, path: 7 if os.path.getsize(path) > 3 else 0)
    sent = []
    monkeypatch.setattr(ui_sync, "broadcast", lambda ev, p: sent.append((ev, p)))
    return lib, sent


def test_files_land_in_the_right_folder_with_safe_unique_names(library):
    lib, _ = library
    p1 = knowledge.save_bytes("../../evil notes.md", b"hello world")
    p2 = knowledge.save_bytes("evil notes.md", b"again")
    p3 = knowledge.save_bytes("Report.PDF", b"%PDF-1.4 ...")
    assert p1 == str(lib / "text" / "evil notes.md") and p2.endswith("evil notes (2).md")
    assert p3 == str(lib / "documents" / "Report.pdf")
    with pytest.raises(knowledge.KnowledgeError):
        knowledge.save_bytes("virus.exe", b"MZ")
    with pytest.raises(knowledge.KnowledgeError):
        knowledge.check("big.txt", knowledge.MAX_BYTES + 1)
    assert knowledge.allowed("script.py") and knowledge.allowed("main.rs")       # code counts


def test_ingest_broadcasts_and_lumi_announces(library):
    lib, sent = library
    sig = types.SimpleNamespace(history=[], new_message=False)
    good = knowledge.save_bytes("lily facts.txt", b"Allium triquetrum is a white flower.")
    bad = knowledge.save_bytes("empty.txt", b"x")
    res = knowledge.ingest_files(sig, [good, bad], "web")
    assert [r["ok"] for r in res] == [True, False] and res[0]["chunks"] == 7
    assert [e for e, _ in sent] == ["knowledge_ingest_start", "knowledge_ingest_finish"] * 2
    ann = sig.history[-1]
    assert ann["source"] == "announce_event" and "I just learned something new: lily facts" in ann["say"]
    assert sig.new_message


def test_island_chunked_upload(library):
    lib, sent = library

    class Srv:
        def __init__(self): self.handlers, self.out = {}, []
        def register_handler(self, t, fn): self.handlers[t] = fn
        async def send(self, ws, payload): self.out.append(payload)

    srv = Srv()
    sig = types.SimpleNamespace(history=[], new_message=False)
    knowledge.install_island_handlers(sig, srv)
    data = b"Lumi likes lilies. " * 5000          # ~95 KB: three chunks
    h = srv.handlers

    async def go():
        await h["knowledge_upload_begin"](None, {"id": 1, "name": "notes.md", "size": len(data)})
        up = srv.out[-1]["upload"]
        for i in range(0, len(data), 36 * 1024):
            await h["knowledge_upload_chunk"](None, {"id": 2, "upload": up, "data": base64.b64encode(data[i:i + 36 * 1024]).decode()})
            assert srv.out[-1]["ok"]
        await h["knowledge_upload_end"](None, {"id": 3, "upload": up})
    asyncio.run(go())
    done = srv.out[-1]
    assert done["ok"] and done["result"]["chunks"] == 7 and done["result"]["source"] == "island"
    assert open(lib / "text" / "notes.md", "rb").read() == data
    assert not os.listdir(lib / ".incoming")


def test_island_upload_rejects_lies_and_bad_types(library):
    class Srv:
        def __init__(self): self.handlers, self.out = {}, []
        def register_handler(self, t, fn): self.handlers[t] = fn
        async def send(self, ws, payload): self.out.append(payload)
    srv = Srv()
    knowledge.install_island_handlers(types.SimpleNamespace(history=[]), srv)
    h = srv.handlers

    async def go():
        await h["knowledge_upload_begin"](None, {"id": 1, "name": "x.exe", "size": 10})
        assert not srv.out[-1]["ok"]
        await h["knowledge_upload_begin"](None, {"id": 2, "name": "a.txt", "size": 4})
        up = srv.out[-1]["upload"]
        await h["knowledge_upload_chunk"](None, {"id": 3, "upload": up, "data": base64.b64encode(b"more than four").decode()})
        assert not srv.out[-1]["ok"] and "bigger" in srv.out[-1]["error"]
        await h["knowledge_upload_end"](None, {"id": 4, "upload": up})
        assert not srv.out[-1]["ok"]                                   # aborted upload is gone
    asyncio.run(go())


# ─── routes ───────────────────────────────────────────────────────────

def test_ui_and_knowledge_routes(live_lumi, tmp_path, monkeypatch, library, hybrid):
    monkeypatch.setattr(theme_store, "PATH", str(tmp_path / "ui_theme.json"))
    monkeypatch.setattr(hybrid_mode, "cloud_ready", lambda: None)

    async def scenario(live):
        r = await live.local.get("/api/ui/state")
        assert r.status == 401
        csrf = await live.register()
        h = {"Origin": LOCAL, "X-CSRF-Token": csrf}
        s = await (await live.local.get("/api/ui/state")).json()
        assert s["theme"] == "default" and {t["id"] for t in s["themes"]} >= {"sakura", "cyberpunk"}
        assert s["hybrid"]["badge"] == "100% Local Privacy"
        r = await live.local.put("/api/ui/theme", json={"theme": "sakura"}, headers=h)
        assert r.status == 200 and (await r.json())["theme"] == "sakura"
        assert (await live.local.put("/api/ui/theme", json={"theme": "nope"}, headers=h)).status == 400
        assert (await live.local.put("/api/ui/theme", json={"theme": "cyberpunk"}, headers={"Origin": LOCAL})).status == 403
        r = await live.local.put("/api/ui/hybrid", json={"enabled": True}, headers=h)
        assert (await r.json())["hybrid"]["enabled"] is True

        fd = aiohttp.FormData()
        fd.add_field("files", b"Lumi's favourite flower is the three-cornered leek.", filename="flower.txt")
        fd.add_field("files", b"MZ", filename="bad.exe")
        r = await live.local.post("/api/knowledge/upload", data=fd, headers=h)
        d = await r.json()
        assert r.status == 200 and d["chunks"] == 7
        assert [(f["file"], f["ok"]) for f in d["files"]] == [("flower.txt", True), ("bad.exe", False)]

        _, rcsrf = await live.remote_login()
        assert (await live.remote.get("/api/ui/state", headers={"Origin": PUBLIC})).status == 200   # read-only: allowed
        r = await live.remote.put("/api/ui/hybrid", json={"enabled": False},
                                  headers={"Origin": PUBLIC, "X-CSRF-Token": rcsrf})
        assert r.status == 403                                          # host only
        fd = aiohttp.FormData()
        fd.add_field("files", b"hello there", filename="x.txt")
        r = await live.remote.post("/api/knowledge/upload", data=fd, headers={"Origin": PUBLIC, "X-CSRF-Token": rcsrf})
        assert r.status == 403
    live_lumi(scenario)


def test_island_ui_messages(tmp_path, monkeypatch, hybrid):
    monkeypatch.setattr(theme_store, "PATH", str(tmp_path / "ui_theme.json"))
    monkeypatch.setattr(hybrid_mode, "cloud_ready", lambda: None)

    class Srv:
        def __init__(self): self.handlers, self.out = {}, []
        def register_handler(self, t, fn): self.handlers[t] = fn
        async def send(self, ws, payload): self.out.append(payload)
    srv = Srv()
    ui_routes.install_island_handlers(srv)

    async def go():
        await srv.handlers["ui_state"](None, {"id": 1})
        await srv.handlers["theme_set"](None, {"id": 2, "theme": "cyberpunk"})
        await srv.handlers["hybrid_set"](None, {"id": 3, "enabled": True})
        await srv.handlers["theme_set"](None, {"id": 4, "theme": "nope"})
    asyncio.run(go())
    assert srv.out[0]["type"] == "ui_state" and srv.out[0]["theme"] == "default"
    assert srv.out[1]["theme"] == "cyberpunk"
    assert srv.out[2]["type"] == "hybrid_mode_update" and srv.out[2]["enabled"] is True
    assert srv.out[3]["type"] == "error"
    assert ("theme_update", {"theme": "cyberpunk"}) in hybrid

"""Lumi Island Quick Capture and Focus Mode, backend side: the three socket
messages (quick_capture, heartbeat_set_paused, focus_summary), the heartbeat
and the ambient sampler honouring the pause, and the island's file-type list
matching the library's. Real server and websocket clients; memory, library
indexing and the model are faked."""

import asyncio
import os
import re
import time
import types

import pytest

from modules import ambient_perception, heartbeat, island_bridge, knowledge
from tests.test_island_bridge import Env, recv, run

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FakeMemoryAPI:
    def __init__(self, fail=False):
        self.saved, self.fail = [], fail

    def create_memory(self, data, importance=0.9, valence=None, memory_type="manual"):
        if self.fail:
            raise RuntimeError("chroma down")
        self.saved.append((data, importance, memory_type))
        return "id-1"


@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setattr(knowledge, "LIBRARY_DIR", str(tmp_path))
    monkeypatch.setattr(knowledge.ui_sync, "broadcast", lambda *a, **k: None)
    logged = []
    monkeypatch.setattr(knowledge, "hub_emit", lambda sig, cat, text, **k: logged.append(text))
    indexed = []
    monkeypatch.setattr(knowledge, "index_single_file", lambda sig, path: indexed.append(path) or 3)
    return types.SimpleNamespace(dir=tmp_path, indexed=indexed, logged=logged)


async def ask(e, payload, kind):
    ws = await e.connect()
    await recv(ws, "hello_ok")
    await ws.send_json(payload)
    return await recv(ws, kind, timeout=5)


# ── Quick Capture ─────────────────────────────────────────────────

def test_text_goes_to_memory():
    async def t():
        async with Env() as e:
            e.sig.memory = types.SimpleNamespace(API=FakeMemoryAPI())
            r = await ask(e, {"type": "quick_capture", "id": 1, "kind": "text", "text": "  dentist on friday at 3  "}, "quick_capture")
            assert r == {"type": "quick_capture", "id": 1, "kind": "text", "ok": True, "name": "memory"}
            assert e.sig.memory.API.saved == [("dentist on friday at 3", 0.8, "manual")]
    run(t())


def test_text_without_memory_or_with_a_failing_store_says_why():
    async def t():
        async with Env() as e:
            r = await ask(e, {"type": "quick_capture", "kind": "text", "text": "x"}, "quick_capture")
            assert r["ok"] is False and "isn't loaded" in r["error"]
            e.sig.memory = types.SimpleNamespace(API=FakeMemoryAPI(fail=True))
            r = await ask(e, {"type": "quick_capture", "kind": "text", "text": "x"}, "quick_capture")
            assert r["ok"] is False and r["error"] == "memory: RuntimeError"
    run(t())


def test_link_goes_to_the_library_without_logging_the_url(library):
    url = "https://example.org/papers/attention-is-all"
    async def t():
        async with Env() as e:
            r = await ask(e, {"type": "quick_capture", "kind": "link", "url": url}, "quick_capture")
            assert r["ok"] is True and r["name"] == "example.org/papers/attention-is-all"
    run(t())
    assert len(library.indexed) == 1
    path = library.indexed[0]
    assert os.path.basename(path).startswith("Quick capture link ") and path.endswith(".md")
    assert url in open(path, encoding="utf-8").read()
    assert library.logged and not any("example" in line for line in library.logged)


@pytest.mark.parametrize("url", ["ftp://example.org/x", "https://", "javascript:alert(1)", "https://a b"])
def test_bad_links_are_refused(library, url):
    async def t():
        async with Env() as e:
            r = await ask(e, {"type": "quick_capture", "kind": "link", "url": url}, "quick_capture")
            assert r["ok"] is False
    run(t())
    assert library.indexed == []


# ── Focus Mode: pause ─────────────────────────────────────────────

def test_pause_sets_and_clears_both_flags():
    async def t():
        async with Env() as e:
            r = await ask(e, {"type": "heartbeat_set_paused", "paused": True, "minutes": 30}, "heartbeat_paused")
            assert r["paused"] is True and 29 * 60 < r["until"] - time.time() <= 30 * 60
            assert island_bridge.heartbeat_paused(e.sig) and island_bridge.perception_paused(e.sig)
            await ask(e, {"type": "heartbeat_set_paused", "paused": False}, "heartbeat_paused")
            assert not island_bridge.heartbeat_paused(e.sig) and not island_bridge.perception_paused(e.sig)
            await ask(e, {"type": "heartbeat_set_paused", "paused": True, "minutes": 9999, "perception": False}, "heartbeat_paused")
            assert e.sig.heartbeat_paused_until - time.time() <= island_bridge.MAX_PAUSE_MIN * 60
            assert not island_bridge.perception_paused(e.sig)
    run(t())


def test_heartbeat_stays_quiet_while_paused_and_the_pause_expires():
    sig = types.SimpleNamespace(heartbeat_paused_until=time.time() + 60)
    hb = heartbeat.Heartbeat.__new__(heartbeat.Heartbeat)
    hb.signals = sig
    assert hb._should_consider_acting() == (False, "paused (focus)")
    sig.heartbeat_paused_until = time.time() - 1
    hb._is_busy = lambda: True                     # past the pause, the usual checks run
    assert hb._should_consider_acting() == (False, "prompter busy")


def test_sampler_skips_ticks_while_paused(monkeypatch):
    sig = types.SimpleNamespace(terminate=False, perception_paused_until=time.time() + 60)
    amb = ambient_perception.AmbientPerception.__new__(ambient_perception.AmbientPerception)
    amb.signals, amb.enabled, amb.sample_interval, amb.min_vlm_interval = sig, True, 0.01, 1
    ticks = []
    amb._tick = lambda: ticks.append(1)
    monkeypatch.setattr(ambient_perception, "HAS_MSS", True)
    monkeypatch.setattr(ambient_perception, "hub_emit", lambda *a, **k: None)

    async def go():
        task = asyncio.create_task(amb.run())
        await asyncio.sleep(0.15)
        paused_ticks = len(ticks)                  # just the start-up tick
        sig.perception_paused_until = 0
        await asyncio.sleep(0.15)
        sig.terminate = True
        await asyncio.sleep(0.05)
        task.cancel()
        return paused_ticks, len(ticks)
    paused_ticks, total = asyncio.run(go())
    assert paused_ticks == 1 and total > 2


# ── Focus Mode: summary ───────────────────────────────────────────

class Ambient:
    def __init__(self, hist): self.hist = hist
    def get_recent_history(self, limit=3): return self.hist[-limit:]


def test_summary_from_recent_observations(monkeypatch):
    monkeypatch.setattr(island_bridge, "summarize_focus", lambda seen: f"{len(seen)} notes, last {seen[-1][0]}")
    now = time.time()
    async def t():
        async with Env() as e:
            e.sig.ambient = Ambient([(now - 99999, "old.txt", "ancient"), (now - 120, "Code", "editing island.js")])
            r = await ask(e, {"type": "focus_summary", "minutes": 25}, "focus_summary")
            assert r["summary"] == "1 notes, last Code"
    run(t())


def test_no_observations_means_no_summary():
    async def t():
        async with Env() as e:
            assert (await ask(e, {"type": "focus_summary", "minutes": 25}, "focus_summary"))["summary"] is None
            e.sig.ambient = Ambient([])
            assert (await ask(e, {"type": "focus_summary", "minutes": 25}, "focus_summary"))["summary"] is None
    run(t())


def test_summarize_uses_the_local_model_and_falls_back(monkeypatch):
    import modules.llm_providers as lp
    calls = []
    def fake_chat(model, messages, **kw):
        calls.append(model)
        return types.SimpleNamespace(content='"You were working on: the island focus timer. Then lunch."')
    monkeypatch.setattr(lp, "chat_sync", fake_chat)
    seen = [("Code", "Editing island.js in VS Code. A test file is open.")]
    assert island_bridge.summarize_focus(seen) == "the island focus timer."
    assert ":" not in calls[0].split("/")[0] or not calls[0].startswith(("anthropic:", "openai:", "gemini:"))
    monkeypatch.setattr(lp, "chat_sync", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ollama down")))
    assert island_bridge.summarize_focus(seen) == "editing island.js in VS Code."


# ── the island's file list matches the library's ──────────────────

def test_island_capture_extensions_match_the_library():
    from modules.library_indexer import SUPPORTED_EXTS
    src = open(os.path.join(ROOT, "island", "src-tauri", "src", "main.rs"), encoding="utf-8").read()
    block = re.search(r"const CAPTURE_EXTS: &\[&str\] = &\[(.*?)\];", src, re.S).group(1)
    assert {"." + e for e in re.findall(r'"([a-z0-9]+)"', block)} == SUPPORTED_EXTS


def test_no_vtube_references_added():
    for rel in ("modules/island_bridge.py", "island/src/island.js", "island/src-tauri/src/main.rs"):
        assert not re.search(r"vts|vtube", open(os.path.join(ROOT, rel), encoding="utf-8").read(), re.I), rel

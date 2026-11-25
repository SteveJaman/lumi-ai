"""IslandManager: a fresh secret per launch, handed to the child's environment
and to the socket server; no real process is started."""

import types

import pytest

from modules import hitl_server
from modules import island_manager as im


class FakeProc:
    def __init__(self):
        self.terminated = False
        self.pid = 4242
    def poll(self): return 0 if self.terminated else None
    def terminate(self): self.terminated = True
    def wait(self, timeout=None): return 0
    def kill(self): self.terminated = True


@pytest.fixture
def mgr(tmp_path, monkeypatch):
    exe = tmp_path / "lumi-island.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(im, "EXE_PATH", str(exe))
    monkeypatch.setattr(im, "STATE_PATH", str(tmp_path / "island.json"))
    launches = []

    def fake_popen(cmd, **kw):
        launches.append((cmd, kw))
        return FakeProc()
    monkeypatch.setattr(im.subprocess, "Popen", fake_popen)
    before = hitl_server._server._token
    m = im.IslandManager(types.SimpleNamespace())
    m.launches = launches
    yield m
    hitl_server.set_token(before)


def test_each_launch_gets_a_fresh_secret_that_the_server_expects(mgr):
    mgr.set_enabled(True)
    env1 = mgr.launches[0][1]["env"]
    token1 = env1["LUMI_ISLAND_TOKEN"]
    assert len(token1) >= 32 and hitl_server._server._token == token1
    mgr.set_enabled(False)
    mgr.set_enabled(True)
    token2 = mgr.launches[1][1]["env"]["LUMI_ISLAND_TOKEN"]
    assert token2 != token1 and hitl_server._server._token == token2


def test_the_child_is_told_where_the_socket_is(mgr):
    mgr.set_enabled(True)
    env = mgr.launches[0][1]["env"]
    assert env["LUMI_ISLAND_WS"] == f"ws://{hitl_server.HITL_HOST}:{hitl_server.HITL_PORT}{hitl_server.HITL_PATH}"


def test_it_starts_silently_and_only_once(mgr):
    mgr.set_enabled(True)
    mgr.set_enabled(True)
    assert len(mgr.launches) == 1
    if im.os.name == "nt":
        assert mgr.launches[0][1]["creationflags"] == im.subprocess.CREATE_NO_WINDOW
    assert mgr.status()["running"] is True
    mgr.set_enabled(False)
    assert mgr.status()["running"] is False


def test_an_unbuilt_island_is_refused_with_instructions(mgr, monkeypatch):
    monkeypatch.setattr(im, "EXE_PATH", "Z:/definitely/not/here.exe")
    with pytest.raises(RuntimeError, match="npm run build"):
        mgr.set_enabled(True)
    assert mgr.launches == []


# ── keeping the exe current with its source ─────────────────────────

import os
import types as _types


def _touch(path, mtime):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        open(path, "w").close()
    os.utime(path, (mtime, mtime))


@pytest.fixture
def src(tmp_path, mgr, monkeypatch):
    page = tmp_path / "island" / "src"
    _touch(str(page / "index.html"), 1000)
    _touch(str(page / "node_modules" / "huge.js"), 9999)       # ignored
    monkeypatch.setattr(im, "SOURCE_PATHS", (str(page),))
    os.utime(im.EXE_PATH, (2000, 2000))
    return page


def test_stale_only_when_a_source_is_newer_than_the_exe(src):
    assert im.is_stale(paths=im.SOURCE_PATHS) is False
    _touch(str(src / "island.js"), 3000)
    assert im.is_stale(paths=im.SOURCE_PATHS) is True
    assert im.is_stale(exe="Z:/no/such.exe", paths=im.SOURCE_PATHS) is False


def test_status_reports_a_stale_build(src, mgr):
    _touch(str(src / "island.js"), 3000)
    st = mgr.status()
    assert st["stale"] is True and "older than its source" in st["message"]


def _ok_runner(calls, code=0):
    def run(cmd, **kw):
        calls.append((cmd, kw))
        return _types.SimpleNamespace(returncode=code, stdout="built", stderr="")
    return run


def test_rebuild_stops_the_island_and_runs_npm_build(src, mgr, monkeypatch):
    monkeypatch.setattr(im, "build_command", lambda: ["npm", "run", "build"])
    mgr.set_enabled(True)
    calls = []
    ok, msg = mgr.rebuild(runner=_ok_runner(calls))
    assert ok and "rebuilt" in msg
    assert calls[0][0] == ["npm", "run", "build"] and calls[0][1]["cwd"] == im.ISLAND_DIR
    assert mgr.running() is False and mgr.status()["building"] is False


def test_a_failed_build_is_reported_not_raised(src, mgr, monkeypatch):
    monkeypatch.setattr(im, "build_command", lambda: ["npm", "run", "build"])
    ok, msg = mgr.rebuild(runner=_ok_runner([], code=1))
    assert not ok and "exit 1" in msg and mgr.last_build["ok"] is False
    monkeypatch.setattr(im, "build_command", lambda: None)
    ok, msg = mgr.rebuild()
    assert not ok and "npm" in msg


def test_no_start_while_building(mgr):
    mgr._building = True
    with pytest.raises(RuntimeError, match="being updated"):
        mgr.set_enabled(True)
    assert mgr.launches == []


def test_boot_rebuilds_a_stale_island_then_starts_it(src, mgr, monkeypatch):
    mgr._save(True)
    _touch(str(src / "island.js"), 3000)
    order = []
    monkeypatch.setattr(mgr, "rebuild", lambda: order.append("build"))
    real_resume = mgr.resume_if_enabled
    monkeypatch.setattr(mgr, "resume_if_enabled", lambda: (order.append("start"), real_resume()))
    mgr.start_at_boot().join(5)
    assert order == ["build", "start"] and len(mgr.launches) == 1


def test_boot_skips_the_build_when_current_off_or_opted_out(src, mgr, monkeypatch):
    built = []
    monkeypatch.setattr(mgr, "rebuild", lambda: built.append(1))
    mgr._save(True)
    mgr.start_at_boot().join(5)                                   # current: no build
    assert built == [], "current"
    _touch(str(src / "island.js"), 3000)
    monkeypatch.setenv("LUMI_ISLAND_AUTOBUILD", "0")
    mgr.shutdown()
    mgr.start_at_boot().join(5)                                   # opted out
    assert built == [], "opted out"
    monkeypatch.delenv("LUMI_ISLAND_AUTOBUILD")
    mgr._save(False)
    mgr.start_at_boot().join(5)                                   # switch off
    assert built == []

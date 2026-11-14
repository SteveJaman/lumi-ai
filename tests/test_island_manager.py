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

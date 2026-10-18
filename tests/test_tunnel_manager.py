"""modules/tunnel.py with cloudflared simulated: no real process, no
network. Real-cloudflared checks are manual (docs/remote-access.md)."""

import subprocess
import threading
import time

import pytest

import modules.tunnel as tunnel_mod
from modules.tunnel import QuickTunnel, start_quick_tunnel

URL = "https://brave-otter-lamp-river.trycloudflare.com"

# What cloudflared prints for a quick tunnel (abridged).
BANNER = [
    "INF Thank you for trying Cloudflare Tunnel...",
    "INF Requesting new quick Tunnel on trycloudflare.com...",
    "INF +--------------------------------------------------------+",
    f"INF |  {URL}  |",
    "INF +--------------------------------------------------------+",
    "INF Registered tunnel connection connIndex=0 location=atl01",
]


class FakeProc:
    """Stands in for subprocess.Popen(cloudflared ...)."""

    def __init__(self, lines=(), exit_after=False, returncode=1,
                 hang_on_terminate=False):
        self._lines = list(lines)
        self._exit_after = exit_after
        self._done = threading.Event()
        self._hang = hang_on_terminate
        self.returncode = None
        self._final_code = returncode
        self.calls = []
        self.stdout = self._stream()

    def _stream(self):
        for ln in self._lines:
            yield ln + "\n"
        if self._exit_after:
            self.returncode = self._final_code
            return
        self._done.wait()            # block like a live process

    def poll(self):
        return self.returncode

    def terminate(self):
        self.calls.append("terminate")
        if not self._hang:
            self._finish(0)

    def kill(self):
        self.calls.append("kill")
        self._finish(-9)

    def wait(self, timeout=None):
        self.calls.append("wait")
        if self._hang and "kill" not in self.calls:
            raise subprocess.TimeoutExpired("cloudflared", timeout)
        self._done.wait(1)
        return self.returncode

    def _finish(self, code):
        self.returncode = code
        self._done.set()


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("FOO=1\n", encoding="utf-8")
    monkeypatch.setattr(tunnel_mod, "ENV_PATH", str(path))
    monkeypatch.setattr(tunnel_mod, "find_cloudflared",
                        lambda: "C:/fake/cloudflared.exe")
    # Never touch the real metrics port (a refused connect takes ~2 s on
    # Windows); tests that need it patch this back in.
    monkeypatch.setattr(QuickTunnel, "_poll_metrics", lambda self: None)
    return path


def _popen_returning(monkeypatch, proc, seen=None):
    def fake_popen(cmd, **kwargs):
        if seen is not None:
            seen.append(cmd)
        return proc
    monkeypatch.setattr(tunnel_mod.subprocess, "Popen", fake_popen)


def test_url_from_cloudflared_output(env, monkeypatch):
    proc = FakeProc(BANNER)
    seen = []
    _popen_returning(monkeypatch, proc, seen)

    t = start_quick_tunnel(5080, metrics_port=0, timeout=5)
    assert t.url == URL
    assert t.connected.is_set()
    cmd = seen[0]
    assert cmd[cmd.index("--url") + 1] == "http://127.0.0.1:5080"
    assert f"LUMI_PUBLIC_ORIGINS={URL}" in env.read_text(encoding="utf-8")

    t.stop()
    assert proc.calls[0] == "terminate"
    assert "LUMI_PUBLIC_ORIGINS" not in env.read_text(encoding="utf-8")
    assert env.read_text(encoding="utf-8") == "FOO=1\n"


def test_url_from_metrics_endpoint(env, monkeypatch):
    proc = FakeProc(["INF Requesting new quick Tunnel on trycloudflare.com..."])
    _popen_returning(monkeypatch, proc)
    calls = {"n": 0}

    def metrics(self):
        calls["n"] += 1
        return URL if calls["n"] >= 2 else None
    monkeypatch.setattr(QuickTunnel, "_poll_metrics", metrics)

    t = QuickTunnel(5080, timeout=5)
    t.connected.set()          # skip the post-URL connection wait
    assert t.start() == URL
    t.stop()


def test_metrics_parses_quicktunnel_json(monkeypatch):
    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def read(self, *a):
            return b'{"hostname": "brave-otter-lamp-river.trycloudflare.com"}'
    seen = []

    def fake_urlopen(url, timeout):
        seen.append(url)
        return Resp()
    monkeypatch.setattr(tunnel_mod.urllib.request, "urlopen", fake_urlopen)
    assert QuickTunnel(5080, metrics_port=20999)._poll_metrics() == URL
    assert seen == ["http://127.0.0.1:20999/quicktunnel"]


def test_api_hostname_in_errors_is_not_the_url(env, monkeypatch):
    proc = FakeProc([
        "ERR Error unmarshaling QuickTunnel response: "
        "https://api.trycloudflare.com/tunnel returned 429",
    ], exit_after=True)
    _popen_returning(monkeypatch, proc)
    with pytest.raises(RuntimeError, match="exited"):
        start_quick_tunnel(5080, timeout=5)


def test_missing_cloudflared(env, monkeypatch):
    monkeypatch.setattr(tunnel_mod, "find_cloudflared", lambda: None)
    with pytest.raises(RuntimeError, match="not installed"):
        start_quick_tunnel(5080)


def test_popen_file_not_found(env, monkeypatch):
    def boom(cmd, **kwargs):
        raise FileNotFoundError(2, "No such file", cmd[0])
    monkeypatch.setattr(tunnel_mod.subprocess, "Popen", boom)
    with pytest.raises(RuntimeError, match="Couldn't run cloudflared"):
        start_quick_tunnel(5080)


def test_timeout_with_process_alive(env, monkeypatch):
    proc = FakeProc(["INF Requesting new quick Tunnel on trycloudflare.com..."])
    _popen_returning(monkeypatch, proc)
    t0 = time.monotonic()
    with pytest.raises(RuntimeError, match="didn't publish a URL within"):
        start_quick_tunnel(5080, timeout=0.6)
    assert time.monotonic() - t0 < 3
    assert "terminate" in proc.calls          # cleaned up, no orphan
    assert "LUMI_PUBLIC_ORIGINS" not in env.read_text(encoding="utf-8")


def test_process_exits_before_url(env, monkeypatch):
    proc = FakeProc(["ERR failed to request quick Tunnel: 503"],
                    exit_after=True)
    _popen_returning(monkeypatch, proc)
    with pytest.raises(RuntimeError, match="503"):
        start_quick_tunnel(5080, timeout=5)


def test_stop_terminates_then_kills(env, monkeypatch):
    proc = FakeProc(BANNER, hang_on_terminate=True)
    _popen_returning(monkeypatch, proc)
    t = start_quick_tunnel(5080, timeout=5)
    t.stop()
    assert proc.calls[:3] == ["terminate", "wait", "kill"]
    t.stop()                                   # idempotent


def test_refuses_host_port(env, monkeypatch):
    def never(*a, **k):
        raise AssertionError("cloudflared must not be spawned")
    monkeypatch.setattr(tunnel_mod.subprocess, "Popen", never)
    with pytest.raises(ValueError, match="5000"):
        start_quick_tunnel(5000)


def test_origin_list_keeps_named_drops_stale_quick(env):
    env.write_text(
        "LUMI_PUBLIC_ORIGINS=https://lumi.example.com,"
        "https://old-dead-tunnel.trycloudflare.com\n", encoding="utf-8")
    tunnel_mod.add_quick_origin(URL)
    assert tunnel_mod.read_env_var("LUMI_PUBLIC_ORIGINS") == \
        f"https://lumi.example.com,{URL}"
    tunnel_mod.remove_origin(URL)
    assert tunnel_mod.read_env_var("LUMI_PUBLIC_ORIGINS") == \
        "https://lumi.example.com"


def test_busy_metrics_port_falls_back(env, monkeypatch):
    import socket
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    held.listen()
    busy = held.getsockname()[1]
    try:
        proc = FakeProc(BANNER)
        seen = []
        _popen_returning(monkeypatch, proc, seen)
        t = start_quick_tunnel(5080, metrics_port=busy, timeout=5)
        metrics = seen[0][seen[0].index("--metrics") + 1]
        assert metrics != f"127.0.0.1:{busy}"
        t.stop()
    finally:
        held.close()

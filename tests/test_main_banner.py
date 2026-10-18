"""main.start_tunnel / print_banner with a mocked tunnel. main() itself
isn't run: it prompts for an audio mode and loads Whisper, Chroma, and
every module."""

import signal
from types import SimpleNamespace

import pytest

# Importing main installs its own SIGINT handler; keep pytest's.
_prev = signal.getsignal(signal.SIGINT)
import main  # noqa: E402
signal.signal(signal.SIGINT, _prev)

URL = "https://brave-otter-lamp-river.trycloudflare.com"


class FakeTunnel:
    def __init__(self):
        self.url = URL
        self.stopped = False

    def stop(self):
        self.stopped = True


@pytest.fixture
def server():
    return SimpleNamespace(tunnel_port=main.TUNNEL_PORT)


@pytest.fixture(autouse=True)
def reset_global(monkeypatch):
    monkeypatch.setattr(main, "_global_tunnel", None)


def test_banner_shows_public_url(monkeypatch, capsys, server):
    monkeypatch.setattr(main, "AUTO_START_TUNNEL", True)
    seen = {}

    def fake_start(port, metrics_port):
        seen["port"] = port
        return FakeTunnel()
    monkeypatch.setattr(main, "start_quick_tunnel", fake_start)

    url = main.start_tunnel(server)
    main.print_banner("local", url)
    out = capsys.readouterr().out
    assert f"Public URL:  \033[96m{URL}" in out
    assert seen["port"] == main.TUNNEL_PORT == 5080

    tunnel = main._global_tunnel
    main.stop_tunnel()
    assert tunnel.stopped and main._global_tunnel is None


def test_banner_not_started_when_start_fails(monkeypatch, capsys, server):
    monkeypatch.setattr(main, "AUTO_START_TUNNEL", True)

    def boom(port, metrics_port):
        raise RuntimeError("cloudflared is not installed")
    monkeypatch.setattr(main, "start_quick_tunnel", boom)

    url = main.start_tunnel(server)
    main.print_banner("local", url)
    out = capsys.readouterr().out
    assert url is None
    assert "TUNNEL: auto-start failed — cloudflared is not installed" in out
    assert "Public URL:  (not started" in out
    assert "trycloudflare.com" not in out


def test_disabled_by_default(monkeypatch, capsys, server):
    assert main.AUTO_START_TUNNEL is False      # shipped default

    def never(*a, **k):
        raise AssertionError("tunnel must not start")
    monkeypatch.setattr(main, "start_quick_tunnel", never)
    assert main.start_tunnel(server) is None
    main.print_banner("local", None)
    assert "Public URL:  (not started" in capsys.readouterr().out


def test_no_tunnel_without_remote_listener(monkeypatch, capsys):
    monkeypatch.setattr(main, "AUTO_START_TUNNEL", True)

    def never(*a, **k):
        raise AssertionError("tunnel must not start")
    monkeypatch.setattr(main, "start_quick_tunnel", never)
    assert main.start_tunnel(SimpleNamespace(tunnel_port=None)) is None
    assert "isn't on port 5080" in capsys.readouterr().out


def test_stop_tunnel_without_tunnel_is_noop():
    main.stop_tunnel()

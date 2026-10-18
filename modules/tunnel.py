"""
Cloudflare quick tunnel manager.

Shared by main.py (AUTO_START_TUNNEL) and scripts/setup_tunnel.py. A
quick tunnel gets a random https://<words>.trycloudflare.com hostname
from Cloudflare and forwards it to one local port. That port must be
Lumi's remote-tier listener (TUNNEL_PORT, 5080), never the host port
5000: everything reaching 5000 gets full admin access.

Lifecycle:
    tunnel = start_quick_tunnel(5080)   # blocks until the URL is known
    tunnel.url                          # https://….trycloudflare.com
    tunnel.stop()                       # kills cloudflared, cleans .env

While running, the URL is listed in LUMI_PUBLIC_ORIGINS in .env. The
web server re-reads .env when it changes (socketIO._public_origins), so
nothing else needs notifying.

Stdlib only and does not import constants.py, so setup_tunnel.py keeps
working when Lumi's dependencies are broken.
"""

import collections
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(_ROOT, ".env")

# The web client's full-access port (socketIO.start_server). A tunnel
# pointed here would hand every visitor admin access.
HOST_PORT = 5000

_ORIGINS_KEY = "LUMI_PUBLIC_ORIGINS"
# api.trycloudflare.com is the service cloudflared asks for a hostname;
# it shows up in error lines and is never the tunnel's own URL.
_QUICK_URL = re.compile(r"https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com")
_CONNECTED = "Registered tunnel connection"


# ─────────────────────────────────────────────────────────────
# .env
# ─────────────────────────────────────────────────────────────
def set_env_var(name, value, env_path=None):
    """Upsert (value=str) or remove (value=None) one key, keeping
    every other line and comment as-is."""
    env_path = env_path or ENV_PATH
    lines = []
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    pattern = re.compile(r"^\s*" + re.escape(name) + r"\s*=")
    out = [ln for ln in lines if not pattern.match(ln)]
    if value is not None:
        out.append(f"{name}={value}")
    with open(env_path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")


def read_env_var(name, env_path=None):
    env_path = env_path or ENV_PATH
    if not os.path.exists(env_path):
        return ""
    pattern = re.compile(r"^\s*" + re.escape(name) + r"\s*=(.*)$")
    value = ""
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            m = pattern.match(line.rstrip("\n"))
            if m:
                value = m.group(1).strip().strip('"').strip("'")
    return value


def _origins(env_path):
    return [o.strip() for o in read_env_var(_ORIGINS_KEY, env_path).split(",")
            if o.strip()]


def add_quick_origin(url, env_path=None):
    """List url in LUMI_PUBLIC_ORIGINS. Other quick-tunnel URLs are
    dropped (they belong to dead tunnels from earlier runs); a named
    tunnel's hostname is kept."""
    kept = [o for o in _origins(env_path)
            if not _QUICK_URL.fullmatch(o.rstrip("/").lower())]
    set_env_var(_ORIGINS_KEY, ",".join(kept + [url]), env_path)


def remove_origin(url, env_path=None):
    """Drop url from LUMI_PUBLIC_ORIGINS; remove the line if empty."""
    kept = [o for o in _origins(env_path) if o.rstrip("/") != url.rstrip("/")]
    set_env_var(_ORIGINS_KEY, ",".join(kept) if kept else None, env_path)


# ─────────────────────────────────────────────────────────────
# cloudflared
# ─────────────────────────────────────────────────────────────
def find_cloudflared():
    exe = shutil.which("cloudflared")
    if exe:
        return exe
    # winget installs here but the PATH update needs a new terminal.
    for candidate in (
        r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
        r"C:\Program Files\cloudflared\cloudflared.exe",
    ):
        if os.path.exists(candidate):
            return candidate
    return None


def _port_free(port):
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class QuickTunnel:
    """One cloudflared quick tunnel to 127.0.0.1:<local_port>."""

    def __init__(self, local_port, metrics_port=20241, timeout=30.0,
                 exe=None, env_path=None, on_line=None):
        self.local_port = int(local_port)
        self.metrics_port = metrics_port
        self.timeout = timeout
        self.exe = exe
        self.env_path = env_path or ENV_PATH
        self.on_line = on_line          # called with each cloudflared line
        self.url = None
        self.connected = threading.Event()
        self.proc = None
        self._log_url = None
        self._tail = collections.deque(maxlen=15)
        self._reader = None

    # -- startup --------------------------------------------------------
    def start(self):
        """Spawn cloudflared and block until its public URL is known.
        Returns the URL. Raises RuntimeError if cloudflared is missing,
        exits, or doesn't produce a URL within `timeout` seconds."""
        if self.local_port == HOST_PORT:
            raise ValueError(
                f"Refusing to tunnel port {HOST_PORT}: it is the host's "
                f"full-access port. Tunnel the remote-tier port instead.")
        exe = self.exe or find_cloudflared()
        if not exe:
            raise RuntimeError(
                "cloudflared is not installed "
                "(winget install --id Cloudflare.cloudflared).")
        if not _port_free(self.metrics_port):
            self.metrics_port = _free_port()

        cmd = [exe, "tunnel", "--no-autoupdate",
               "--metrics", f"127.0.0.1:{self.metrics_port}",
               "--url", f"http://127.0.0.1:{self.local_port}"]
        try:
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                errors="replace")
        except OSError as e:          # FileNotFoundError, PermissionError
            raise RuntimeError(f"Couldn't run cloudflared: {e}") from None

        # Drain cloudflared's output for its whole life, not just until
        # the URL shows up: an unread pipe fills and stalls the process.
        self._reader = threading.Thread(target=self._read_output,
                                        daemon=True, name="cloudflared-log")
        self._reader.start()

        deadline = time.monotonic() + self.timeout
        url = None
        while time.monotonic() < deadline:
            url = self._log_url or self._poll_metrics()
            if url:
                break
            if self.proc.poll() is not None:
                self._reader.join(timeout=1)
                raise RuntimeError(
                    f"cloudflared exited (code {self.proc.returncode}) "
                    f"before publishing a URL:\n" + "\n".join(self._tail))
            time.sleep(0.25)
        if not url:
            self.stop()
            raise RuntimeError(
                f"cloudflared didn't publish a URL within "
                f"{self.timeout:.0f}s:\n" + "\n".join(self._tail))

        self.url = url
        add_quick_origin(url, self.env_path)
        # The hostname exists a moment before Cloudflare can route to
        # it; give the connection a little time so the URL works when
        # it's printed. Not fatal if it's slow.
        self.connected.wait(timeout=max(0.0, min(10.0,
                                                 deadline - time.monotonic())))
        return url

    def _read_output(self):
        for line in self.proc.stdout:
            line = line.rstrip("\n")
            self._tail.append(line)
            if self._log_url is None:
                m = _QUICK_URL.search(line)
                if m:
                    self._log_url = m.group(0)
            if _CONNECTED in line:
                self.connected.set()
            if self.on_line is not None:
                try:
                    self.on_line(line)
                except Exception:
                    pass

    def _poll_metrics(self):
        """cloudflared's metrics server reports the quick-tunnel
        hostname at /quicktunnel once it has one."""
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.metrics_port}/quicktunnel",
                    timeout=1) as resp:
                host = (json.load(resp).get("hostname") or "").strip()
        except Exception:
            return None
        return f"https://{host}" if host else None

    # -- shutdown -------------------------------------------------------
    def stop(self):
        """Kill cloudflared and drop the URL from .env. Idempotent."""
        if self.url:
            try:
                remove_origin(self.url, self.env_path)
            except OSError:
                pass
            self.url = None
        proc, self.proc = self.proc, None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()


def start_quick_tunnel(local_port, metrics_port=20241, timeout=30.0,
                       **kwargs):
    """Start a quick tunnel and return it once its URL is known."""
    tunnel = QuickTunnel(local_port, metrics_port=metrics_port,
                         timeout=timeout, **kwargs)
    tunnel.start()
    return tunnel

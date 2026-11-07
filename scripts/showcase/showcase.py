"""
Lumi tunnel showcase: a live view of real HTTP responses, separate from Lumi.

    pip install flask
    python scripts/showcase/showcase.py   ->  http://127.0.0.1:5090

Imports nothing from Lumi and changes nothing in it. See README.md in this folder.
"""

# ── Config ───────────────────────────────────────────────────
# "auto" follows the running quick tunnel, whose URL changes on every start.
# With a named tunnel on your own domain, use "https://lumi.example.com".
PUBLIC_URL = "auto"
LOCAL_TUN = "http://127.0.0.1:5080"      # remote tier, what cloudflared hits
LOCAL_FULL = "http://127.0.0.1:5000"     # full local access, never tunneled
HEALTH_PATH = "/healthz"
MANAGE_TUNNEL = False    # True: this app starts and restarts the tunnel

import os                                                        # noqa: E402
import sys                                                       # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TUNNEL_MODE = "quick" if PUBLIC_URL == "auto" else "run"
TUNNEL_COMMAND = [sys.executable,
                  os.path.join(ROOT, "scripts", "setup_tunnel.py"), TUNNEL_MODE]

SLOW_MS = 500               # a 200 slower than this is yellow
POLL_TIMEOUT = 1.8          # seconds; keeps a poll inside the 2 s tick
KILL_TIMEOUT = 60           # give up waiting for PUBLIC to fail
RECOVERY_TIMEOUT = 120      # give up waiting for PUBLIC to return
PROBE_INTERVAL = 0.25       # seconds between probes during kill/restart
METRICS_PORTS = range(20241, 20246)   # cloudflared's default metrics ports

# ── Implementation ───────────────────────────────────────────
import atexit                                                    # noqa: E402
import collections                                               # noqa: E402
import csv                                                       # noqa: E402
import io                                                        # noqa: E402
import platform                                                  # noqa: E402
import subprocess                                                # noqa: E402
import tempfile                                                  # noqa: E402
import threading                                                 # noqa: E402
import time                                                      # noqa: E402
from concurrent.futures import ThreadPoolExecutor                # noqa: E402
from urllib.parse import urlsplit                                # noqa: E402

import requests                                                  # noqa: E402
from flask import Flask, jsonify, render_template, request       # noqa: E402

WINDOWS = platform.system() == "Windows"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if WINDOWS else 0
TUNNEL_LOG = os.path.join(tempfile.gettempdir(), "lumi_showcase_tunnel.log")
UP_STATES = ("ok", "slow")   # a 200 that crossed the whole path
TRANSPORT_HEADERS = {"date", "connection", "keep-alive", "transfer-encoding",
                     "content-length", "content-type", "content-encoding",
                     "vary"}     # either side may send these: not edge proof

app = Flask(__name__)
fetch_pool = ThreadPoolExecutor(12, thread_name_prefix="fetch")
probe_pool = ThreadPoolExecutor(5, thread_name_prefix="probe")
log_rows = collections.deque(maxlen=30)
last_headers = {"public": None, "local_tun": None}
state_lock = threading.Lock()
action_lock = threading.Lock()     # one kill or restart at a time
tunnel_proc = None
discovered_url = None       # latest quick tunnel URL, kept through an outage


def public_url():
    return discovered_url if PUBLIC_URL == "auto" else PUBLIC_URL


def public_configured():
    url = public_url()
    return bool(url) and "<" not in url


def is_login_redirect(status, location):
    return status in (301, 302, 303, 307) and (
        "cloudflareaccess.com" in location or "/cdn-cgi/access/" in location)


def classify(status, latency, location):
    """Returns (state, detail). Only a real response can make it non-red."""
    if status is None:
        return "down", "no response"
    if is_login_redirect(status, location):
        return "gated", "Access gate"
    if status == 200:
        return ("slow", f"over {SLOW_MS} ms") if latency > SLOW_MS else ("ok", "")
    if status == 530:
        return "down", "edge up, no tunnel"     # Cloudflare error 1033
    if status == 404:
        return "down", "404: check HEALTH_PATH"
    return "down", f"HTTP {status}"


def fetch(base):
    """One real GET. A new connection each time, so a pooled socket can
    never hide a dead tunnel. Redirects are not followed: the 302 from
    Access is the thing being measured."""
    started = time.perf_counter()
    status, headers, error = None, {}, None
    try:
        r = requests.get(base + HEALTH_PATH, timeout=POLL_TIMEOUT,
                         allow_redirects=False,
                         headers={"User-Agent": "LumiShowcase/1.0",
                                  "Cache-Control": "no-cache",
                                  "Connection": "close"})
        status, headers = r.status_code, dict(r.headers)
    except requests.Timeout:
        error = "timeout"
    except requests.RequestException:
        error = "connection error"
    latency = round((time.perf_counter() - started) * 1000, 1)
    state, detail = classify(status, latency, headers.get("Location", ""))
    if "Set-Cookie" in headers:
        headers["Set-Cookie"] = "(hidden)"      # do not show cookies on video
    return {"status": status, "latency": latency, "headers": headers,
            "state": state, "detail": error or detail,
            "time": time.strftime("%H:%M:%S"), "received": time.monotonic()}


def unconfigured():
    return {"status": None, "latency": None, "headers": {},
            "state": "unconfigured", "detail": "no public URL",
            "time": time.strftime("%H:%M:%S"), "received": time.monotonic()}


def list_cloudflared_pids():
    if WINDOWS:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq cloudflared.exe",
             "/FO", "CSV", "/NH"], capture_output=True, text=True,
            timeout=5, creationflags=NO_WINDOW).stdout
        return [int(row[1]) for row in csv.reader(io.StringIO(out))
                if len(row) > 1 and row[0].lower() == "cloudflared.exe"]
    out = subprocess.run(["pgrep", "-a", "cloudflared"], capture_output=True,
                         text=True, timeout=5).stdout
    return [int(line.split()[0]) for line in out.splitlines() if line.strip()]


def read_metrics(port):
    """(ready connections, quick tunnel hostname) from the metrics server
    of cloudflared, or None if nothing answers on this port."""
    base = f"http://127.0.0.1:{port}"
    try:
        ready = requests.get(base + "/ready", timeout=0.25).json()
        host = requests.get(base + "/quicktunnel", timeout=0.25).json()
        return ready.get("readyConnections"), host.get("hostname")
    except (requests.RequestException, ValueError):
        return None


def tunnel_status():
    global discovered_url
    pids = list_cloudflared_pids()
    connections = None
    if pids:
        found = [m for m in probe_pool.map(read_metrics, METRICS_PORTS) if m]
        connections = sum(n or 0 for n, _ in found) if found else None
        hosts = [host for _, host in found if host]
        if hosts:
            discovered_url = f"https://{hosts[0]}"
    problem = None
    if MANAGE_TUNNEL and not pids and tunnel_proc is not None \
            and tunnel_proc.poll() not in (None, 0):
        problem = (f"setup_tunnel.py exited with code {tunnel_proc.poll()}, "
                   f"see {TUNNEL_LOG}")
    return {"running": bool(pids), "pid": pids[0] if pids else None,
            "pids": pids, "connections": connections, "problem": problem}


def poll_all():
    started = time.monotonic()
    tunnel = tunnel_status()     # first: it may reveal a new quick tunnel URL
    jobs = {"local_tun": fetch_pool.submit(fetch, LOCAL_TUN),
            "local_full": fetch_pool.submit(fetch, LOCAL_FULL)}
    if public_configured():
        jobs["public"] = fetch_pool.submit(fetch, public_url())
    result = {name: job.result() for name, job in jobs.items()}
    result["tunnel"] = tunnel
    result.setdefault("public", unconfigured())
    result["started"] = started
    return result


class Cycle:
    """One kill -> outage -> recovery, fed only by real samples.

    Phases: idle, killing (waiting for PUBLIC to fail), down, recovered,
    no_failure (PUBLIC never failed after the kill)."""

    def __init__(self):
        self.lock = threading.RLock()
        self.begin(idle=True)

    def begin(self, idle=False):
        with self.lock:
            self.phase = "idle" if idle else "killing"
            self.t_kill = time.monotonic()
            self.t_down = self.t_restart = self.t_up = None
            self.clock = None
            self.tun_ok = self.full_ok = False

    def mark_restart(self, clock, at=None):
        with self.lock:
            if self.t_restart is None:
                self.t_restart, self.clock = at or time.monotonic(), clock

    def give_up(self):
        with self.lock:
            if self.phase == "killing":
                self.phase = "no_failure"

    def observe(self, sample):
        with self.lock:
            public, started = sample["public"], sample["started"]
            # Outages are timed from when the failing request was sent, so
            # a request that hangs until POLL_TIMEOUT doesn't inflate them.
            # Samples sent before that point say nothing about the outage.
            if self.phase == "killing" and public["state"] == "down"                     and started >= self.t_kill:
                self.phase, self.t_down = "down", started
            if self.phase != "down" or started < self.t_down:
                return
            self.tun_ok = self.tun_ok or sample["local_tun"]["status"] == 200
            self.full_ok = self.full_ok or sample["local_full"]["status"] == 200
            if sample["tunnel"]["running"]:
                self.mark_restart("process_seen", started)
            if public["state"] in UP_STATES:
                self.phase, self.t_up = "recovered", public["received"]

    def snapshot(self):
        with self.lock:
            down = self.t_down
            end = self.t_up or time.monotonic()
            return {
                "phase": self.phase,
                "time_to_unreachable": round(down - self.t_kill, 2) if down else None,
                "outage": round(end - down, 2) if down else None,
                "recovery": round(self.t_up - self.t_restart, 2)
                if self.t_up and self.t_restart else None,
                "recovery_clock": self.clock,
                "origin_healthy": self.tun_ok and self.full_ok,
                "origin_5080": self.tun_ok, "origin_5000": self.full_ok,
            }


cycle = Cycle()


def wait_for(phases, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and cycle.phase not in phases:
        cycle.observe(poll_all())
        time.sleep(PROBE_INTERVAL)
    return cycle.phase in phases


def kill_cloudflared():
    if WINDOWS:
        cmd = ["taskkill", "/F", "/IM", "cloudflared.exe"]
    else:
        cmd = ["pkill", "cloudflared"]
    return subprocess.run(cmd, capture_output=True, text=True,
                          creationflags=NO_WINDOW).returncode == 0


def start_tunnel():
    global tunnel_proc
    with open(TUNNEL_LOG, "ab") as log:
        tunnel_proc = subprocess.Popen(
            TUNNEL_COMMAND, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log,
            stderr=subprocess.STDOUT, creationflags=NO_WINDOW)


def stop_tunnel():
    if tunnel_proc is None or tunnel_proc.poll() is not None:
        return
    if WINDOWS:     # /T also takes down cloudflared, the child of the script
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(tunnel_proc.pid)],
                       capture_output=True, creationflags=NO_WINDOW)
    else:
        tunnel_proc.terminate()


def edge_only(public, local):
    """Headers the edge added, plus any it rewrote, such as Server."""
    if not public or not local:
        return []
    theirs = {k.lower(): v for k, v in local["headers"].items()}
    return [k for k, v in public["headers"].items()
            if k.lower() not in TRANSPORT_HEADERS
            and (k.lower() not in theirs
                 or (k.lower() == "server" and theirs["server"] != v))]


def add_log(when, endpoint, r):
    ray = r["headers"].get("CF-Ray", "")
    log_rows.appendleft({
        "time": when, "endpoint": endpoint, "status": r["status"],
        "latency": r["latency"], "ray": ray.split("-")[0][:8],
        "state": r["state"], "detail": r["detail"]})


@app.before_request
def same_origin_posts_only():
    """Stops any other web page from POSTing /api/kill to this port."""
    origin = request.headers.get("Origin")
    if request.method == "POST" and origin \
            and urlsplit(origin).netloc != request.host:
        return jsonify(error="cross-origin request refused"), 403


@app.get("/")
def index():
    return render_template("showcase.html")


@app.get("/api/poll")
def api_poll():
    sample = poll_all()
    cycle.observe(sample)
    tick = sample["public"]["time"]
    with state_lock:
        if sample["public"]["state"] != "unconfigured":
            add_log(tick, "PUBLIC", sample["public"])
        add_log(tick, ":5080", sample["local_tun"])
        add_log(tick, ":5000", sample["local_full"])
        # An Access 302 still came from the edge, so it counts for headers.
        if sample["public"]["status"] == 200 or sample["public"]["state"] == "gated":
            last_headers["public"] = sample["public"]
        if sample["local_tun"]["status"] == 200:
            last_headers["local_tun"] = sample["local_tun"]
        snapshot = dict(last_headers)
        rows = list(log_rows)
    return jsonify(
        **sample, log=rows, cycle=cycle.snapshot(),
        header_diff={"public": snapshot["public"], "local": snapshot["local_tun"],
                     "edge_only": edge_only(snapshot["public"],
                                            snapshot["local_tun"])},
        config={"public_url": public_url() if public_configured() else None,
                "health_path": HEALTH_PATH, "manage_tunnel": MANAGE_TUNNEL,
                "restart_hint": f"python scripts/setup_tunnel.py {TUNNEL_MODE}"})


@app.post("/api/kill")
def api_kill():
    global tunnel_proc
    if not public_configured():
        return jsonify(error="No public URL yet. Start a tunnel, or set "
                       "PUBLIC_URL in showcase.py."), 400
    if not action_lock.acquire(blocking=False):
        return jsonify(error="Another kill or restart is running."), 409
    try:
        before = poll_all()
        state = before["public"]["state"]
        if state == "gated":
            return jsonify(error=f"Nothing killed. Access answers {HEALTH_PATH} "
                           "at the edge whether or not the tunnel is up, so an "
                           "outage can't be seen. Add an Access Bypass policy "
                           f"for {HEALTH_PATH}."), 409
        if state not in UP_STATES:
            return jsonify(error="Nothing killed. PUBLIC isn't returning 200 "
                           f"yet ({before['public']['detail']})."), 409
        if not before["tunnel"]["running"]:
            return jsonify(error="cloudflared is not running on this machine."), 409
        if not kill_cloudflared():
            return jsonify(error="The kill command failed."), 500
        tunnel_proc = None      # it died on purpose, so not a startup failure
        cycle.begin()
        wait_for(("down",), KILL_TIMEOUT)
        cycle.give_up()
        return jsonify(cycle.snapshot())
    finally:
        action_lock.release()


@app.post("/api/restart")
def api_restart():
    if cycle.phase != "down":
        return jsonify(error="No outage in progress."), 409
    if not action_lock.acquire(blocking=False):
        return jsonify(error="Another kill or restart is running."), 409
    try:
        if MANAGE_TUNNEL:
            start_tunnel()
            cycle.mark_restart("restart_spawned")
        elif tunnel_status()["running"]:
            cycle.mark_restart("button_press")
        wait_for(("recovered",), RECOVERY_TIMEOUT)
        return jsonify(cycle.snapshot())
    finally:
        action_lock.release()


def main():
    if MANAGE_TUNNEL:
        atexit.register(stop_tunnel)
        if not list_cloudflared_pids():
            start_tunnel()
    if "<" in PUBLIC_URL:
        print("PUBLIC_URL is still the placeholder: PUBLIC shows as not configured.")
    print("Showcase: http://127.0.0.1:5090")
    app.run(host="127.0.0.1", port=5090, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()

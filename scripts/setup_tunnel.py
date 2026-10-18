"""
Expose Lumi's web client over HTTPS with a Cloudflare Tunnel.

cloudflared makes an outbound connection to Cloudflare and forwards a
public hostname to Lumi's tunnel listener, 127.0.0.1:5080 (TUNNEL_PORT
in constants.py). Nothing needs a port forward, and campus or hotel
networks that block inbound traffic don't matter.

Everything arriving on 5080 gets Lumi's remote tier: chat, history,
voice, and a read-only dashboard. Your own browser keeps using
http://127.0.0.1:5000 with full access. Never point cloudflared at
5000.

Two modes:

  quick   A throwaway https://<random>.trycloudflare.com URL. No
          account, no domain. Good for a test. Cloudflare Access can't
          protect it, so Lumi's own login is the only gate.

  setup   A permanent hostname on a domain you have on Cloudflare
  run     (e.g. lumi.example.com), protectable with Cloudflare Access.
          `setup` once, then `run` whenever Lumi is up.

Each mode writes the public origin to LUMI_PUBLIC_ORIGINS in .env.
Lumi re-reads it on the fly, so order doesn't matter: start Lumi
first or the tunnel first.

This script does NOT import constants.py, so it runs even when Lumi's
dependencies are broken.

Usage:
    python scripts/setup_tunnel.py check
    python scripts/setup_tunnel.py quick
    python scripts/setup_tunnel.py setup --hostname lumi.example.com
    python scripts/setup_tunnel.py run
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ENV_PATH = os.path.join(_ROOT, ".env")
_TUNNEL_DIR = os.path.join(_ROOT, "data", "cloudflared")
_CONFIG_PATH = os.path.join(_TUNNEL_DIR, "config.yml")

# Lumi's remote-tier listener (constants.TUNNEL_PORT). Not imported, so
# keep the two in sync.
LOCAL_SERVICE = "http://127.0.0.1:5080"
DEFAULT_TUNNEL_NAME = "lumi"

_QUICK_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


# ─────────────────────────────────────────────────────────────
# Output helpers
# ─────────────────────────────────────────────────────────────
def info(msg):
    print(f"\033[96m[tunnel]\033[0m {msg}")


def warn(msg):
    print(f"\033[93m[tunnel]\033[0m {msg}")


def fail(msg):
    print(f"\033[91m[tunnel]\033[0m {msg}")
    sys.exit(1)


# ─────────────────────────────────────────────────────────────
# .env
# ─────────────────────────────────────────────────────────────
def set_env_var(name, value):
    """Upsert (value=str) or remove (value=None) one key, keeping
    every other line and comment as-is."""
    lines = []
    if os.path.exists(_ENV_PATH):
        with open(_ENV_PATH, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    pattern = re.compile(r"^\s*" + re.escape(name) + r"\s*=")
    out = [ln for ln in lines if not pattern.match(ln)]
    if value is not None:
        out.append(f"{name}={value}")
    with open(_ENV_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")


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


def require_cloudflared():
    exe = find_cloudflared()
    if exe:
        return exe
    print()
    warn("cloudflared is not installed. Install it, open a NEW terminal, "
         "and rerun this script:")
    print()
    if sys.platform == "win32":
        print("    winget install --id Cloudflare.cloudflared")
    elif sys.platform == "darwin":
        print("    brew install cloudflared")
    else:
        print("    https://developers.cloudflare.com/cloudflare-one/"
              "connections/connect-networks/downloads/")
    print()
    sys.exit(1)


def lumi_is_up():
    import urllib.request
    try:
        urllib.request.urlopen(LOCAL_SERVICE + "/healthz", timeout=2)
        return True
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────
# Commands
# ─────────────────────────────────────────────────────────────
def cmd_check(_args):
    exe = find_cloudflared()
    info(f"cloudflared: {exe or 'NOT INSTALLED'}")
    if exe:
        out = subprocess.run([exe, "--version"], capture_output=True,
                             text=True).stdout.strip()
        info(f"  {out}")
    info(f"Lumi web client at {LOCAL_SERVICE}: "
         f"{'up' if lumi_is_up() else 'not running'}")
    info(f"Named tunnel config: "
         f"{_CONFIG_PATH if os.path.exists(_CONFIG_PATH) else 'none yet'}")
    origins = ""
    if os.path.exists(_ENV_PATH):
        with open(_ENV_PATH, encoding="utf-8") as f:
            for ln in f:
                if ln.strip().startswith("LUMI_PUBLIC_ORIGINS="):
                    origins = ln.split("=", 1)[1].strip()
    info(f"LUMI_PUBLIC_ORIGINS: {origins or '(unset — local only)'}")


def cmd_quick(_args):
    exe = require_cloudflared()
    if not lumi_is_up():
        warn(f"Lumi isn't answering on {LOCAL_SERVICE} yet. The tunnel "
             f"will start anyway and work once Lumi is up.")
    warn("Quick tunnels can't use Cloudflare Access. Anyone with the URL "
         "reaches Lumi's login page. Use `setup` for anything long-lived.")
    info("Starting quick tunnel (Ctrl+C to stop)…")

    proc = subprocess.Popen(
        [exe, "tunnel", "--no-autoupdate", "--url", LOCAL_SERVICE],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    origin = None
    try:
        for line in proc.stdout:
            if origin is None:
                m = _QUICK_URL.search(line)
                if m:
                    origin = m.group(0)
                    set_env_var("LUMI_PUBLIC_ORIGINS", origin)
                    print()
                    info(f"Lumi is reachable at  \033[1m{origin}\033[0m")
                    info("Wrote LUMI_PUBLIC_ORIGINS to .env. It is removed "
                         "again when you stop this script.")
                    print()
            elif "ERR" in line:
                sys.stdout.write(line)
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
    finally:
        if origin:
            set_env_var("LUMI_PUBLIC_ORIGINS", None)
            info("Tunnel closed; removed LUMI_PUBLIC_ORIGINS.")


def _tunnel_id(exe, name):
    res = subprocess.run(
        [exe, "tunnel", "list", "--output", "json", "--name", name],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        return None
    try:
        tunnels = json.loads(res.stdout or "[]")
    except json.JSONDecodeError:
        return None
    for t in tunnels or []:
        if t.get("name") == name and not t.get("deleted_at"):
            return t.get("id")
    return None


def cmd_setup(args):
    exe = require_cloudflared()
    hostname = args.hostname.strip().lower().removeprefix("https://").rstrip("/")
    if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", hostname):
        fail(f"{hostname!r} doesn't look like a hostname "
             f"(expected e.g. lumi.example.com).")
    name = args.name

    cert = os.path.join(os.path.expanduser("~"), ".cloudflared", "cert.pem")
    if not os.path.exists(cert):
        info("Step 1/4 — log in to Cloudflare. A browser window opens; pick "
             "the domain that will hold the hostname.")
        if subprocess.run([exe, "tunnel", "login"]).returncode != 0:
            fail("cloudflared login failed.")
    else:
        info("Step 1/4 — already logged in to Cloudflare.")

    tid = _tunnel_id(exe, name)
    if tid:
        info(f"Step 2/4 — reusing tunnel '{name}' ({tid}).")
    else:
        info(f"Step 2/4 — creating tunnel '{name}'.")
        if subprocess.run([exe, "tunnel", "create", name]).returncode != 0:
            fail("Tunnel creation failed.")
        tid = _tunnel_id(exe, name)
        if not tid:
            fail("Tunnel was created but can't be found in `tunnel list`.")

    info(f"Step 3/4 — pointing {hostname} at the tunnel.")
    res = subprocess.run([exe, "tunnel", "route", "dns", "--overwrite-dns",
                          name, hostname])
    if res.returncode != 0:
        fail(f"DNS routing failed. Is {hostname} under the domain you "
             f"picked at login?")

    creds = os.path.join(os.path.expanduser("~"), ".cloudflared",
                         f"{tid}.json")
    os.makedirs(_TUNNEL_DIR, exist_ok=True)
    # Only the web client goes through the tunnel. The diagnostics hub
    # (:5001) and SDK (:5056) have no login and must stay local.
    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(
            f"# Generated by scripts/setup_tunnel.py\n"
            f"tunnel: {tid}\n"
            f"credentials-file: {creds}\n"
            f"ingress:\n"
            f"  - hostname: {hostname}\n"
            f"    service: {LOCAL_SERVICE}\n"
            f"  - service: http_status:404\n"
        )
    set_env_var("LUMI_PUBLIC_ORIGINS", f"https://{hostname}")
    info(f"Step 4/4 — wrote {os.path.relpath(_CONFIG_PATH, _ROOT)} and "
         f"LUMI_PUBLIC_ORIGINS=https://{hostname}")

    print(f"""
\033[1mNext: put a login wall in front of it (Cloudflare Access)\033[0m

  1. Open https://one.dash.cloudflare.com → Access → Applications
     → Add an application → Self-hosted.
  2. Application domain: {hostname}
  3. Add a policy: Action = Allow, Include = Emails → your address
     (or "Emails ending in" → @students.kennesaw.edu for classmates).
  4. Save. Visitors now prove who they are to Cloudflare before they
     ever reach Lumi's own login page.

\033[1mThen start the tunnel\033[0m (each time, alongside main.py):

    python scripts/setup_tunnel.py run
""")


def cmd_run(_args):
    exe = require_cloudflared()
    if not os.path.exists(_CONFIG_PATH):
        fail("No tunnel configured yet. Run "
             "`python scripts/setup_tunnel.py setup --hostname <host>` first.")
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        config = f.read()
    # A config from before the tunnel listener existed points at :5000,
    # which would give remote visitors host-level access. Repoint it.
    fixed = re.sub(r"(service:\s*)http://(127\.0\.0\.1|localhost):5000(?!\d)",
                   r"\g<1>" + LOCAL_SERVICE, config)
    if fixed != config:
        with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write(fixed)
        warn(f"Repointed the tunnel from :5000 to {LOCAL_SERVICE}.")
        config = fixed
    m = re.search(r"hostname:\s*(\S+)", config)
    if m:
        set_env_var("LUMI_PUBLIC_ORIGINS", f"https://{m.group(1)}")
        info(f"Serving https://{m.group(1)} → {LOCAL_SERVICE} "
             f"(Ctrl+C to stop)")
    if not lumi_is_up():
        warn("Lumi isn't running yet; the tunnel will connect once it is.")
    try:
        subprocess.run([exe, "tunnel", "--no-autoupdate",
                        "--config", _CONFIG_PATH, "run"])
    except KeyboardInterrupt:
        pass


def main():
    parser = argparse.ArgumentParser(
        description="Expose Lumi's web client through a Cloudflare Tunnel.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="Show cloudflared, Lumi, and tunnel status")
    sub.add_parser("quick", help="Temporary trycloudflare.com URL, no account")
    p_setup = sub.add_parser("setup", help="One-time named tunnel setup")
    p_setup.add_argument("--hostname", required=True,
                         help="Public hostname, e.g. lumi.example.com")
    p_setup.add_argument("--name", default=DEFAULT_TUNNEL_NAME,
                         help=f"Tunnel name (default: {DEFAULT_TUNNEL_NAME})")
    sub.add_parser("run", help="Start the named tunnel from `setup`")
    args = parser.parse_args()

    {"check": cmd_check, "quick": cmd_quick,
     "setup": cmd_setup, "run": cmd_run}[args.cmd](args)


if __name__ == "__main__":
    main()

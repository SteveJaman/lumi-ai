# Lumi Tunnel Showcase

A live dashboard, built for a university cloud computing project, showing how
Lumi is reached over the internet with **no inbound ports open**.

Lumi runs on a home PC. A public request goes to Cloudflare's edge, then down a
tunnel that the PC opened outbound, and arrives at a local port. The dashboard
demonstrates two things:

1. Public traffic really does pass through Cloudflare.
2. Killing the tunnel breaks public access, while the local origin stays healthy.

The dashboard is a separate process that polls from outside, like any other
client. It imports nothing from Lumi and changes nothing in it. There is no
simulated data: every dot, latency and log row comes from a real HTTP response.

## Contents

- [Quick start](#quick-start)
- [How it works](#how-it-works)
- [The panels](#the-panels)
- [How states are decided](#how-states-are-decided)
- [How the measurements work](#how-the-measurements-work)
- [Configuration](#configuration)
- [Quick tunnels and named tunnels](#quick-tunnels-and-named-tunnels)
- [HTTP API](#http-api)
- [Safety behavior](#safety-behavior)
- [Limitations](#limitations)
- [Recording the video](#recording-the-video)
- [Troubleshooting](#troubleshooting)
- [Files](#files)

## Quick start

Run these from the repo root, with Lumi's conda environment active.

1. Install Flask once:

   ```powershell
   conda activate lumi
   pip install flask
   ```

2. In `scripts/showcase/showcase.py`, set `MANAGE_TUNNEL = True`.

3. Start Lumi in one terminal. With `AUTO_START_TUNNEL = True` in `constants.py`,
   this also starts the quick tunnel:

   ```powershell
   python main.py
   ```

4. Start the dashboard in a second terminal:

   ```powershell
   python scripts/showcase/showcase.py
   ```

5. Open <http://127.0.0.1:5090>.

If `AUTO_START_TUNNEL` is off and `MANAGE_TUNNEL` is on, the dashboard starts
the tunnel itself.

## How it works

```text
                       PUBLIC_URL poll (hops 1-3)
   showcase.py  ──▶  Cloudflare edge  ──▶  cloudflared  ──▶  :5080  remote tier
       │                                  (outbound only)
       │               LOCAL_TUN poll (hop 4)
       ├──────────────────────────────────────────────────▶  :5080  remote tier
       │               LOCAL_FULL poll (hop 5)
       └──────────────────────────────────────────────────▶  :5000  full access
```

`:5080` is Lumi's reduced remote tier, the only port cloudflared forwards to.
`:5000` is full local access and is never tunneled.

### The polling cycle

The page requests `GET /api/poll` every 2 seconds. A request can't overlap the
one before it, so a slow tick delays the next rather than stacking up. The
browser can't read `CF-Ray` on a cross-origin response, so `showcase.py` makes
all the requests server side. Each tick it:

1. Checks whether cloudflared is running, with `tasklist` on Windows or `pgrep`
   elsewhere.
2. Reads cloudflared's local metrics server for its connection count and, for a
   quick tunnel, the current public hostname.
3. Sends `GET /healthz` to each endpoint in parallel, using a new connection
   each time so a pooled socket can't hide a dead tunnel. Redirects aren't
   followed, because the redirect from Access is itself a result.
4. Classifies each response, updates the log and feeds the sample to the
   outage tracker.

The page only renders what comes back. Dot changes are CSS transitions on real
state changes, with no scripted animation. If the page loses contact with
`showcase.py`, the dots go grey rather than keep showing old results.

## The panels

| Panel | Shows | What it proves |
|---|---|---|
| **A. Request path** | Five hops: USER, EDGE, TUNNEL, :5080, ORIGIN | Public hops fail with the tunnel while local hops stay up, so the tunnel is the only way in |
| **B. Response headers** | Public and local headers side by side, with edge-added headers in green | Responses carrying `CF-Ray`, `CF-Cache-Status` and `Server: cloudflare` crossed the edge |
| **C. Tunnel process** | cloudflared state, PID and edge connections, plus the Kill and Restart buttons | The outage lines up with a real process stopping on this machine |
| **D. Live request log** | The last 30 responses, with time, endpoint, status, latency and CF-Ray prefix | Each dot is backed by a timestamped response, including the `530` during an outage |
| **E. Measurement** | Outage, recovery, and whether the origin stayed healthy. Appears after a kill | The cost of depending on the tunnel, in seconds |

### Notes on the panels

- **Hops 1-3 share one number.** They come from a single public request, which
  can't be split by hop, so they always show the same latency and state.
- **Panel B keeps the last good public response** during an outage. Its
  timestamp shows how old that response is.
- **Generic headers are never highlighted,** such as `Date` and `Content-Type`,
  because either side can send them and they prove nothing. `Server` appears on
  both sides, so it's highlighted because the value differs.
- **`CF-Connecting-IP` is never shown.** Cloudflare adds it to the request going
  to the origin, so it can't appear in a response. The panel says so on screen.

## How states are decided

| Dot | State | Condition |
|---|---|---|
| Green | `ok` | 200 within 500 ms |
| Yellow | `slow` | 200 slower than 500 ms |
| Yellow | `gated` | Redirect to a Cloudflare Access login page |
| Red | `down` | No response, or any other status |
| Grey | `unconfigured` | No public URL found yet |

**Red doesn't mean "no response".** When a tunnel dies, Cloudflare's edge stays
up and answers **HTTP 530**, so most outage polls are real responses. Testing
against real Cloudflare showed one timed-out request after the kill, followed by
steady 530s still carrying `CF-Ray` and `Server: cloudflare`. The log labels
these rows "edge up, no tunnel".

## How the measurements work

**Kill tunnel** sequence:

1. Confirm PUBLIC is returning 200 and cloudflared is running.
2. Kill cloudflared.
3. Probe all endpoints about every 0.25 s until PUBLIC fails.
4. Keep sampling through the outage, in the page's polls and during a restart.

| Measurement | Starts | Ends |
|---|---|---|
| **Last outage** | First failed request sent | First public 200 received |
| **Last recovery** | Restart command, or cloudflared first seen running | First public 200 received |
| **Kill to first failed request** | Kill completes | First failed request sent |

**Origin stayed healthy** reads YES if :5080 and :5000 each returned 200 at least
once during the outage.

Some details matter when reading these numbers:

- **Kill to first failed request is often 0.0s.** That's a real result: the first
  request sent after the kill already failed.
- **Failures are timed from when the request was sent,** so a request hanging
  until the 1.8 s timeout doesn't inflate the result.
- **Requests sent before the outage are ignored,** so a response already in
  flight during the kill can't count as a recovery.
- **Recovery is detected even without pressing a button.** If the tunnel returns
  from another terminal, the regular polls record it.

The panel states which event started the recovery clock:

| Clock | Used when |
|---|---|
| Restart command | The dashboard started the tunnel |
| cloudflared first seen running | You restarted it yourself |
| Button press | It was already running when you pressed the button |

## Configuration

These settings are at the top of `showcase.py`.

| Setting | Default | Meaning |
|---|---|---|
| `PUBLIC_URL` | `"auto"` | `"auto"` follows the running quick tunnel. Otherwise, a fixed `https://` hostname |
| `LOCAL_TUN` | `http://127.0.0.1:5080` | Remote tier, polled for hop 4 |
| `LOCAL_FULL` | `http://127.0.0.1:5000` | Full local access, polled for hop 5 |
| `HEALTH_PATH` | `/healthz` | Answered without sign-in on both ports |
| `MANAGE_TUNNEL` | `False` | `True` lets the dashboard start and restart the tunnel |
| `SLOW_MS` | `500` | A 200 slower than this is yellow |
| `POLL_TIMEOUT` | `1.8` | Seconds before a request counts as failed |
| `KILL_TIMEOUT` | `60` | Seconds to wait for PUBLIC to fail after a kill |
| `RECOVERY_TIMEOUT` | `120` | Seconds to wait for PUBLIC to return |
| `PROBE_INTERVAL` | `0.25` | Seconds between probes during a kill or restart |
| `METRICS_PORTS` | `20241-20245` | Where to look for cloudflared's metrics server |

### Tunnel control modes

**`MANAGE_TUNNEL = True` (recommended).** The dashboard starts a tunnel on launch
if none is running, and **Restart tunnel** starts a new one. The demo happens in
one window. Pressing Ctrl+C stops a tunnel the dashboard started. It doesn't stop
one started by `main.py`.

**`MANAGE_TUNNEL = False`.** The dashboard only watches. **Kill tunnel** still
works. After a kill, start the tunnel yourself, and recovery is recorded when it
returns:

```powershell
python scripts/setup_tunnel.py quick
```

## Quick tunnels and named tunnels

This project uses a **quick tunnel** because it needs no domain.

| | Quick tunnel (this project) | Named tunnel |
|---|---|---|
| Needs a domain on Cloudflare | No | Yes |
| URL | New random `trycloudflare.com` address each start | Fixed hostname |
| Cloudflare Access | Not available | Available |
| Who can reach the login page | Anyone with the URL | Only people Access admits |
| Dashboard setting | `PUBLIC_URL = "auto"` | `PUBLIC_URL = "https://your.host"` |
| Restart command | `setup_tunnel.py quick` | `setup_tunnel.py run` |

With a quick tunnel:

- **Recovery means a new address.** The old link stays dead, and the URL under
  the dots changes on camera.
- **Recovery time includes the new hostname going live,** not only cloudflared
  reconnecting.
- **The yellow "gated" state won't appear,** because Access isn't available.
  Lumi's own login is the only gate.

### Using a named tunnel

If you later have a domain on Cloudflare:

1. Run `python scripts/setup_tunnel.py setup --hostname lumi.yourdomain.com`.
2. Follow the Access steps it prints.
3. Add an Access **Bypass** policy for **Everyone** on the path `/healthz`.
4. Set `PUBLIC_URL` in `showcase.py` to your hostname.
5. Set `AUTO_START_TUNNEL = False` in `constants.py`, so that a quick tunnel
   doesn't run alongside it.

The bypass in step 3 is required. Access answers at the edge without using the
tunnel, so a gated path looks the same whether the tunnel is up or dead.

## HTTP API

All routes are served on `127.0.0.1:5090`.

### `GET /`

Serves the dashboard page.

### `GET /api/poll`

Polls every endpoint and returns the results.

```json
{
  "public":     { "status": 200, "latency": 138.2, "headers": {}, "state": "ok", "detail": "" },
  "local_tun":  { "status": 200, "latency": 4.1, "headers": {}, "state": "ok", "detail": "" },
  "local_full": { "status": 200, "latency": 3.8, "headers": {}, "state": "ok", "detail": "" },
  "tunnel":     { "running": true, "pid": 29664, "pids": [29664], "connections": 1, "problem": null },
  "log": [],
  "cycle": {},
  "header_diff": {},
  "config": {}
}
```

### `POST /api/kill`

Kills cloudflared and returns once PUBLIC fails or `KILL_TIMEOUT` passes.

```json
{
  "phase": "down",
  "time_to_unreachable": 0.0,
  "outage": 2.39,
  "recovery": null,
  "recovery_clock": null,
  "origin_healthy": true,
  "origin_5080": true,
  "origin_5000": true
}
```

`phase` is one of these values:

| Phase | Meaning |
|---|---|
| `idle` | No kill yet |
| `killing` | Waiting for PUBLIC to fail |
| `down` | Outage in progress |
| `recovered` | PUBLIC returned |
| `no_failure` | PUBLIC never failed within `KILL_TIMEOUT` |

Kill returns an error, and **kills nothing**, in these cases:

| Status | Reason |
|---|---|
| 400 | No public URL is known |
| 409 | PUBLIC is gated, PUBLIC isn't returning 200, or cloudflared isn't running |
| 409 | Another kill or restart is running |

### `POST /api/restart`

With `MANAGE_TUNNEL = True`, starts the tunnel. Then, in either mode, waits for
PUBLIC to return or for `RECOVERY_TIMEOUT` to pass, and returns the same object
as kill. It returns 409 if no outage is in progress.

## Safety behavior

- **Local only.** The dashboard binds to `127.0.0.1`, so it can't be reached from
  the network or through the tunnel.
- **Port 5000 stays private.** The dashboard polls it directly and never exposes
  it. The tunnel still points only at :5080.
- **Other websites can't press Kill.** POST requests from another origin are
  refused with a 403.
- **Cookies stay off camera.** `Set-Cookie` values are replaced with `(hidden)`.
- **Kill checks first.** It only runs when an outage can actually be measured.
- **Kill is machine-wide.** It stops every `cloudflared.exe`, whichever process
  started it, including a `cloudflared tunnel login` waiting in another terminal.

## Limitations

- Hops 1-3 can't be measured separately. All three come from one public request.
- `/healthz` shows that Lumi's web server is answering, not that chat, voice or
  other features work.
- Time resolution is about 0.25 s during a kill or restart, and about 2 s if
  recovery is only picked up by the regular polls.
- The dashboard holds no history. Measurements and the log reset when it restarts.
- A quick tunnel's URL can't be found if cloudflared picked a metrics port outside
  20241-20245, which happens only when those ports are in use.
- The connection count needs cloudflared's metrics server, and can briefly read
  "not reported" just after a restart.

## Recording the video

Press **Kill tunnel** and **Restart tunnel** once as a rehearsal. Each restart
creates a new URL. Allow a moment for the dots to go green before you start.

### 45-second narration

**Dots green (about 15 s).** "This is Lumi, running on my own PC with no
inbound ports open. A public request goes to Cloudflare's edge, down an
outbound tunnel, and into a local port. These headers are the proof: CF-Ray and
Server: cloudflare appear on the public side and not on the local side."

**Press Kill tunnel (about 8 s).** "Now I'll kill cloudflared, the only process
connecting this machine to the internet."

**Dots red (about 12 s).** "The public path is down. Cloudflare's edge is still
answering, but with a 530, because it has no tunnel to send traffic through.
Meanwhile these two stay green: the origin never noticed."

**Press Restart, dots recover (about 10 s).** "I'll restart the tunnel. It
connects outbound again, so Lumi is public with no firewall change, at a new
address. Here's the measured outage and recovery, and the origin stayed healthy
throughout."

## Troubleshooting

**`ModuleNotFoundError: No module named 'flask'`.** Run `conda activate lumi`,
then `pip install flask`.

**PUBLIC is grey with "No public tunnel found".** No quick tunnel is running.
Start one, or set `MANAGE_TUNNEL = True`. If one is running, cloudflared may have
picked a metrics port outside 20241-20245, so restart it.

**`tunnel login` opens a browser and hangs.** You ran `setup`, which needs your
own domain. Press Ctrl+C in that terminal. Quick tunnels don't need it.

**:5080 and :5000 are red.** `main.py` isn't running or hasn't finished booting.

**`/healthz` shows 404.** `HEALTH_PATH` is wrong. Lumi serves `/healthz` on both
ports. On a named tunnel, a 404 on PUBLIC only can also mean the hostname isn't
in the tunnel's ingress rules.

**Kill says PUBLIC isn't returning 200.** The tunnel hasn't connected yet. Wait
for the dots to turn green, then try again.

**Kill doesn't stick, or cloudflared comes back.** Nothing in Lumi respawns
cloudflared. Look for a second terminal running `setup_tunnel.py`, or cloudflared
installed as a Windows service.

**Public stays green after a kill.** The result reports that PUBLIC never failed.
Another connector for the same tunnel is serving traffic, perhaps on another
machine.

**PUBLIC stays yellow ("gated").** Named tunnels only. Access is answering
`/healthz`, so add the Bypass policy described in
[Using a named tunnel](#using-a-named-tunnel).

**"setup_tunnel.py exited" in Panel C.** The tunnel failed to start. The output is
in `lumi_showcase_tunnel.log` in your temp folder.

## Files

```text
scripts/showcase/
├── README.md                  This document
├── showcase.py                Flask app: polling, outage tracking, tunnel control
├── templates/
│   └── showcase.html          Page layout
└── static/
    ├── showcase.css           Dark theme, states and transitions
    └── showcase.js            Polls the API and renders the results
```

The dashboard also relies on these existing Lumi files, without modifying them:

| File | Used for |
|---|---|
| `scripts/setup_tunnel.py` | Starting the tunnel |
| `GET /healthz` in `modules/socketIO.py` | The health check on each port |

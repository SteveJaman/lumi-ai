# Remote access

How to let someone open a link, sign in with credentials you gave them,
and chat with Lumi from anywhere, while everything still runs on your PC.

## Shape

```
 Your PC                                                 Anywhere
┌────────────────────────────────────────────────┐
│ your browser ─► 127.0.0.1:5000 ─┐  host tier   │
│                                 ├─► web client │
│ cloudflared ──► 127.0.0.1:5080 ─┘  remote tier │◄── https://lumi.example.com
│   ▲ (outbound only)                            │     Cloudflare Access (optional)
│   └────────────── Cloudflare ◄─────────────────┼──── remote browser
│                                                │
│ :5001 log hub   :5056 SDK   :5057 Minecraft    │  never tunneled
└────────────────────────────────────────────────┘
```

One web app listens on two loopback ports. Your own browser uses
`:5000` and has full access. `cloudflared` forwards the public hostname to
`:5080` (`TUNNEL_PORT` in `constants.py`), and everything that arrives
there gets the remote tier. Nothing listens on a public port:
`cloudflared` dials out to Cloudflare, so campus networks that block
inbound traffic don't matter.

### How Lumi tells remote from local

The **listening port** decides, not the peer address and not any header.
`cloudflared` runs on your PC and connects to Lumi over loopback, so the
peer address of tunnel traffic is always `127.0.0.1`, exactly like your
own browser's. A peer-address check would wave every tunnel visitor
through as local. Checking Cloudflare's IP ranges fails for the same
reason: Lumi never sees a Cloudflare edge address.

Three more signals each mark a request remote, and none of them can make a
request local:

| Signal | Catches |
|---|---|
| Arrived on `TUNNEL_PORT` | All tunnel traffic |
| Peer isn't `127.0.0.1` / `::1` | LAN clients when `ALLOW_LAN_EXPOSURE = True` |
| `Cf-Connecting-Ip`, `Cf-Ray`, or `X-Forwarded-For` present | A tunnel pointed at `:5000` by mistake |

A remote client can send any headers it likes, including
`Origin: http://127.0.0.1:5000`, and still gets the remote tier. Code:
`modules/socketIO.py:SocketIOServer._is_local`.

## What remote users can and cannot do

| Feature | Local (`:5000`) | Remote (tunnel) |
|---|---|---|
| Sign in | ✓ | ✓ |
| Create an account | ✓ | ✗ "Registration is disabled here" (override: `REMOTE_ALLOW_REGISTRATION`) |
| Chat, chat history, file attachments | ✓ | ✓ |
| Chat commands (`!agent_on`, `!set`, `wipe memory`, `set mood …`) | ✓ | ✗ "Commands only work on the host machine" |
| Voice playback in the browser, hold-to-talk | ✓ | ✓ (spoken commands are refused too) |
| Dashboard stats | ✓ | ✓ read-only |
| Dashboard event log | ✓ | ✗ "Log events are local-only" (events can include window titles and file names) |
| Agent mode toggle | ✓ | ✗ shown read-only |
| Persona, Memory, Models, Integrations, Settings pages | ✓ | ✗ hidden from the sidebar; their APIs return 403 |
| `GET /healthz` | ✓ | ✓, no sign-in needed |

The server enforces this with an allowlist. A remote request may only
call `GET /api/status`, `GET /api/history`, `POST /api/chat/send`,
`POST /api/chat/upload`, `POST /api/voice_message`, and `GET /api/models`
(the list only). Every other `/api/` route returns 403
`{"error": "Only available on the host machine."}`, including routes
added later, until someone adds them to `_REMOTE_API_ALLOW`.

**What remote users still affect.** They share your one Lumi: the same
conversation, the same mood, and the same long-term memory she forms from
chatting. Files they attach are saved under `data/library/` and can turn
up in later answers for everyone. If agent mode is **on**, their messages
can trigger tools; the `y/n` confirmation for writes and commands appears
in *your* terminal. Turn agent mode off before you share the link.

## Set up the tunnel

### Install cloudflared

```powershell
winget install --id Cloudflare.cloudflared
```

winget installs it to `C:\Program Files (x86)\cloudflared\cloudflared.exe`
(or `C:\Program Files\cloudflared\` on newer builds) and adds it to `PATH`.
Open a **new** terminal so the `PATH` change applies. The script also
looks in both folders directly.

```powershell
python scripts/setup_tunnel.py check
```

`check` prints the `cloudflared` path and version, whether Lumi answers
on `http://127.0.0.1:5080/healthz`, the named-tunnel config if there is
one, and the current `LUMI_PUBLIC_ORIGINS`.

### Quick tunnel (testing only)

```powershell
python scripts/setup_tunnel.py quick
```

This prints a random `https://….trycloudflare.com` URL and writes it to
`LUMI_PUBLIC_ORIGINS`. Lumi picks that up without a restart, and the
entry is removed when you press Ctrl+C. No Cloudflare account is needed,
and no Access login is possible: anyone with the URL reaches Lumi's login
page. They can't register, but a guessed password gets them in. Don't
share a quick-tunnel URL with anyone you wouldn't hand your laptop to.

### Permanent hostname (for campus)

You need a domain whose DNS is on Cloudflare (a free plan is fine).

```powershell
python scripts/setup_tunnel.py setup --hostname lumi.example.com
python scripts/setup_tunnel.py run
```

`setup` runs once:

1. Logs in to Cloudflare in your browser.
2. Creates a tunnel named `lumi`.
3. Points the hostname at it.
4. Writes `data/cloudflared/config.yml` (gitignored) with the service set
   to `http://127.0.0.1:5080`.
5. Sets `LUMI_PUBLIC_ORIGINS=https://lumi.example.com`.

After that, start `run` alongside `python main.py`, in either order. If
`run` finds an older config pointing at `:5000`, it repoints it to
`:5080` and tells you.

Only the web client goes through the tunnel. Never add the log hub
(`:5001`), SDK (`:5056`), or Minecraft bot API (`:5057`) to the config.
None of them has a login.

### Put Cloudflare Access in front

With Access, people prove who they are to Cloudflare before any request
reaches your PC. Use it for anything shared beyond a quick test.

1. [Cloudflare One dashboard](https://one.dash.cloudflare.com) →
   **Access → Applications → Add an application → Self-hosted**.
2. **Application domain:** `lumi.example.com`.
3. **Policy:** Action **Allow**, Include **Emails** → the exact
   addresses you're sharing with. "Emails ending in
   `@students.kennesaw.edu`" admits the whole school, so only use it if
   you mean that.
4. Optional: add a second policy, **Bypass** for **Everyone**, on path
   `/healthz`, so uptime checks don't need to sign in.

Access works in one of two modes, chosen by `TRUST_CLOUDFLARE_ACCESS` in
`constants.py`.

**Mode 1: Access as a gate (default, `False`).** Visitors pass
Cloudflare's sign-in, then Lumi's own login with the username and
password you gave them. Lumi ignores every Access header. Choose this
when you want two independent locks.

**Mode 2: Access as the login (`True`).** Visitors pass Cloudflare's
sign-in and land straight in the chat. On their first request Lumi:

- verifies the `Cf-Access-Jwt-Assertion` JWT: RS256 signature against
  your team's published keys, issuer, audience, and expiry;
- signs them in as an account named `cf-<name>-<hash>`, created on first
  visit, with their email as the display name.

These accounts can't use the login form (their password is random and
never shown), always get the remote tier, and never collide with
accounts you create; the `cf-` prefix is reserved. Mode 2 needs two
values in `.env`:

```
CF_ACCESS_TEAM_DOMAIN=yourteam.cloudflareaccess.com
CF_ACCESS_AUD=<Application Audience (AUD) Tag from the app's Overview page>
```

The `Cf-Access-Authenticated-User-Email` header is never trusted on its
own. A request that skips Access, such as through a quick tunnel, can set
it to anything. Only a JWT signed by your team counts.

## Verify before sharing

Run these yourself; each takes a minute or two. The automated tests
cover the same ground with real sockets but without a real tunnel or
phone.

**Setup**

1. `python main.py`
2. `python scripts/setup_tunnel.py quick` (or `run`), and note the URL.
3. Open `<URL>/healthz`. You should see `{"ok": true, "version": "…"}`.
   If not, the tunnel isn't up.

**Local (on the host)**

4. `http://127.0.0.1:5000` works exactly as before, with every sidebar
   item present.
5. The Dashboard shows events.

**Remote (a phone on cellular, not campus Wi-Fi, or a friend's laptop)**

6. The tunnel URL loads the login page.
7. Registering fails with "Registration is disabled here…".
8. Logging in lands on the chat.
9. Send a message; Lumi replies.
10. `!agent_on` fails with "Commands only work on the host machine".
11. The Dashboard shows stats and "Log events are local-only".
12. Persona, Memory, Models, Integrations, and Settings are not in the
    sidebar, and the agent-mode chip can't be clicked.
13. Click the Voice chip, send another message, and audio plays in the
    browser.

| Step fails | Likely cause |
|---|---|
| 3 or 6 | Tunnel not running, or `cloudflared` pointed at the wrong port. `check` shows both |
| 7 succeeds | The tunnel points at `:5000` *and* strips Cloudflare's headers. Rerun `run` to repoint it |
| 8 hangs or loops back to login | Cookie or origin problem; see below |
| 9 | Chat send rejected (origin) or socket.io refused (see below) |
| 11 empty with no message | Browser cached the old `app.js`; hard-refresh |
| 12 shows admin pages | The request isn't reaching `:5080`; `check` and the config's `service:` line |
| 13 silent | Voice chip not opted in, or TTS isn't configured on the host |

## Troubleshooting

The host console prints one line per remote sign-in, which tells you
quickly whether the request reached Lumi:

```
AUTH: remote login ok for 'alice' (origin 'https://lumi.example.com', secure cookie=True)
AUTH: remote login failed for 'alice' — Invalid username or password.
SOCKETIO:   accepted as alice (remote)
SOCKETIO:   rejected — bad origin 'https://old-name.trycloudflare.com'
```

| Symptom | Check |
|---|---|
| **Page loads but chat doesn't send** | The address bar's origin must be in `LUMI_PUBLIC_ORIGINS` exactly (scheme + host, no path). The console shows `SOCKETIO: rejected — bad origin …` or the send fails with `Bad origin.` A new quick tunnel has a new URL; restart `quick` rather than reusing an old one |
| **Dashboard empty** | Expected on remote: it says "Log events are local-only". Open it on the host |
| **Login hangs or returns to the login page** | Look for `AUTH: remote login ok … secure cookie=True` on the host. If it's there, the browser didn't keep the cookie. That happens when it's on plain `http://`; always use the `https://` tunnel URL. If no AUTH line appears, the request isn't reaching Lumi; check the tunnel hostname is in `LUMI_PUBLIC_ORIGINS` |
| "Lumi has not been set up yet" | No accounts exist. Create the first one on the host at `http://127.0.0.1:5000` |
| `Only available on the host machine.` | Working as intended for remote users; see the table above |
| Startup prints `tunnel port 5080 unavailable` | Another program holds 5080. Change `TUNNEL_PORT` in `constants.py` *and* `LOCAL_SERVICE` in `scripts/setup_tunnel.py` |
| Mode 2: lands on Lumi's login anyway | The host console shows `AUTH: Cloudflare Access JWT rejected — <reason>`. Usually `CF_ACCESS_AUD` or `CF_ACCESS_TEAM_DOMAIN` is wrong |

Static assets need nothing special. `/web/app.js`, `/web/style.css`, and
the login scripts are served by Lumi through the tunnel. The socket.io
client loads from `cdn.socket.io` in `web/index.html`, straight from the
visitor's browser. Lumi doesn't serve `/socket.io/socket.io.js`, so a
404 there is expected.

## Streaming (disabled)

The OBS control module, the Stream page, and spoken-line subtitles are
still in the tree (`modules/obs_control.py`, `modules/subtitles.py`) but
off. `STREAMING_ENABLED = False` keeps the module from loading, keeps the
`/api/stream/*` routes unregistered, and hides the Stream page and the OBS
integration card. `OBS_SUBTITLES_ENABLED = False` stops the subtitle file.
`obsws-python` isn't in `requirements.txt`; install it by hand before
turning streaming back on.

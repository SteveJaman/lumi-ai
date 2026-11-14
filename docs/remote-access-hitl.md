# Remote access: approving Lumi's tool calls from a Surface Pro

This guide covers putting Tailscale on the Desktop and a Surface Pro, starting
the HITL approval server, and opening the Island PWA on the Surface. The
architecture behind it is in
[architecture.md](architecture.md#two-machine-setup).

> **Status: partly built.** Read this before you start.
>
> | Step | State |
> |---|---|
> | Install Tailscale on both machines | Works now |
> | Start Lumi and the HITL server on the Desktop | Works now |
> | Check the server is listening on the Desktop | Works now |
> | Serve the Island page at `/island` | **Not built.** No route serves it yet |
> | Reach the HITL server from the Surface over Tailscale | **Not built.** The server accepts only loopback origins |
> | Authenticate the WebSocket | **Not built.** Anyone who can reach the port can approve tool calls |
>
> Do not expose port 5090 to your tailnet until the authentication step is
> built. Any device on the tailnet, or anything running on a compromised
> device, could approve `run_command` or `write_file`. The Surface steps below
> are marked as not yet working for this reason.

---

## 1. Install Tailscale

Do this on both machines, using the same account. (Working now.)

### Desktop (Windows)

- [ ] Install Tailscale. Either download it from <https://tailscale.com/download>,
      or run in PowerShell:

  ```powershell
  winget install --id Tailscale.Tailscale -e
  ```

- [ ] Open the Tailscale tray icon and sign in. Use the same identity provider
      you'll use on the Surface.
- [ ] Confirm the Desktop is connected and note its tailnet address:

  ```powershell
  tailscale status
  tailscale ip -4
  ```

  `tailscale ip -4` prints an address in the `100.x.y.z` range. Keep it.

- [ ] In the Tailscale admin console (<https://login.tailscale.com/admin>),
      turn on **MagicDNS** under **DNS**, so the Desktop has a stable name
      such as `desktop.<tailnet>.ts.net`. (Needed for the HTTPS step later.)

### Surface Pro (Windows)

- [ ] Install Tailscale from the Microsoft Store, or from
      <https://tailscale.com/download>.
- [ ] Sign in with the same account as the Desktop.
- [ ] Confirm both machines appear in the device list and can reach each other:

  ```powershell
  tailscale status
  tailscale ping <desktop-name>
  ```

  Replace `<desktop-name>` with the name shown by `tailscale status`.
  `tailscale ping` should get replies. If it times out, check that both
  machines are signed in and that neither has a firewall rule blocking the
  Tailscale adapter.

---

## 2. Start Lumi and the HITL server (Desktop)

The HITL server is not a separate program. It starts inside `main.py`, on the
prompter's event loop, so launching Lumi starts it. (Working now.)

- [ ] Open PowerShell in the workspace and activate the environment:

  ```powershell
  cd "C:\Users\anguy\OneDrive - Kennesaw State University\Documents\Lumi-master"
  conda activate lumi
  python main.py
  ```

- [ ] Choose your audio routing when asked. Once Lumi is ready, the terminal
      should print the approval server line:

  ```
  [HITL] approval server on ws://127.0.0.1:5090/hitl
  ```

  If you see `[HITL] could not bind 127.0.0.1:5090`, another process already
  holds the port. Find it and stop it, then restart Lumi:

  ```powershell
  Get-NetTCPConnection -LocalPort 5090 -State Listen |
    Select-Object LocalAddress, OwningProcess
  ```

- [ ] Confirm the port is listening, from a second PowerShell window:

  ```powershell
  Test-NetConnection -ComputerName 127.0.0.1 -Port 5090
  ```

  `TcpTestSucceeded : True` means the server is up.

- [ ] Optional, to test an approval without the Island: leave Lumi running and
      trigger a tool call that needs approval. With no client connected, the
      prompt appears in the terminal as usual. That confirms the fallback works.

### What the server accepts

- It listens on `127.0.0.1` only. Other machines can't connect to it yet.
- It refuses any WebSocket upgrade that carries a browser `Origin` header other
  than `localhost`, `127.0.0.1` or `::1`. A PWA on the Surface sends its own
  origin, so it is refused today.
- Non-browser clients send no `Origin` header and are accepted. Only do this
  on your own machine.

---

## 3. Open the Island PWA on the Surface Pro

**Not yet working.** These steps describe the intended flow. They fail at
step 2 today because nothing serves `/island`, and the origin check blocks the
Surface.

### 3a. Serve the Island page (not built)

The Island UI is `ui/Island.jsx`. It needs a build and a route that serves it,
for example at `http://127.0.0.1:5000/island`. Neither exists yet.

### 3b. Expose the approval server to the tailnet (not built, do not do yet)

When the authentication step is built, the intended command is:

```powershell
tailscale serve --bg 5090
tailscale serve status
```

- `tailscale serve` gives the Desktop an HTTPS address on the tailnet, which a
  PWA needs before it can be installed.
- `tailscale serve reset` removes the forwarding. Run it when you're done.
- The HITL server would also need its origin allowlist updated for the
  tailnet address. Until it is, these commands only produce refused
  connections.

### 3c. Install the PWA on the Surface (not built)

Once the page is served over HTTPS:

- [ ] Open Edge on the Surface and go to
      `https://desktop.<tailnet>.ts.net/island`.
- [ ] Select **⋯ → Apps → Install this site as an app**.
- [ ] Pin it to the Start menu or taskbar.

The window should sit at the top of the screen, stay out of the way, and show
an approval card when Lumi asks to run a tool. Approve or decline there, and
the decision goes back to the Desktop.

---

## 4. Turn Gaming Mode on before a game

Gaming Mode unloads the models from VRAM and pauses the Minecraft agent. (Working now.)

- [ ] In the web client on the Desktop, go to **Settings → General** and turn
      on **Gaming Mode**, or run this from the Desktop:

  ```powershell
  Invoke-RestMethod -Method Post -Uri http://127.0.0.1:5000/api/gaming_mode `
    -ContentType "application/json" -Body '{"enabled": true}'
  ```

  This route needs a logged-in session and a CSRF token, so the PowerShell
  command above will return 401 or 403 unless you add them. Using the Settings
  switch is simpler.

- [ ] Turn it off again after the game.

---

## Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| `tailscale ping` times out | Either machine is signed out, or a firewall blocks Tailscale | Check `tailscale status` on both, and allow the Tailscale app through Windows Firewall |
| `[HITL] could not bind` in the log | Port 5090 is in use | Use the `Get-NetTCPConnection` command in step 2 |
| `Test-NetConnection` fails on 5090 | Lumi isn't running, or the server didn't start | Check the terminal for the `[HITL]` line |
| Approval prompts only appear in the terminal | No client is connected | Expected. The fallback is working |
| Surface prompts never reach the Desktop | Not built yet (see step 3) | Keep approvals on the Desktop until the steps above are built |

## Security notes

- Tailscale encrypts the traffic between your devices, and only devices on your
  tailnet can reach them. Keep the tailnet to devices you own.
- Turn on two-factor sign-in for the account Tailscale uses.
- Keep `tailscale serve` off until the WebSocket is authenticated.
- Never bind the HITL server to `0.0.0.0`. That exposes it on every network
  the Desktop is connected to, including public Wi-Fi.
- If you lose the Surface, remove it in the Tailscale admin console under
  **Machines**, so it can't reconnect.

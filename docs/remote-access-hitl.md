# Remote access: approving Lumi's tool calls from a Surface Pro

This guide covers putting Tailscale on the Desktop and a Surface Pro, starting
the approval server, and pairing Lumi Island on the Surface. The architecture
behind it is in [architecture.md](architecture.md#remote-islands-pairing).

> **Status: built.** Lumi Island installs on the Surface and connects to the
> Desktop over Tailscale with a pairing code. Remote access is off until you
> turn it on in Lumi's web client, and it listens on the Tailscale address
> only.

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

- It always listens on `127.0.0.1`. When you turn on **Remote island** it
  also listens on this PC's Tailscale address (100.x.x.x), never on Wi-Fi, a
  LAN or `0.0.0.0`.
- It refuses any WebSocket upgrade that carries a browser `Origin` header other
  than `localhost`, `127.0.0.1` or `::1` (the island app sends its own
  `tauri.localhost` origin, which is allowed).
- A connection from another machine must open with the pairing code. The
  local launch token doesn't work remotely, and with remote turned off other
  machines are refused outright.

---

## 3. Pair Lumi Island on the Surface Pro

### 3a. Build the installer (Desktop, once)

```powershell
cd island
npm run build:standalone
```

The installer is
`island\src-tauri\target\release\bundle\nsis\Lumi Island_0.1.0_x64-setup.exe`
(about 1 MB; there is also an `.msi` next to it under `bundle\msi`). Copy it
to the Surface by USB, OneDrive or Taildrop.

### 3b. Turn on remote islands (Desktop)

- [ ] In Lumi's web client open **Settings, then General, then Remote island**.
      It shows the Tailscale address it found. If it says Tailscale isn't
      running, start Tailscale and sign in, then reload.
- [ ] Select **Turn on and show a pairing code**. Write the code down; it is
      shown only this once (Lumi keeps only a hash of it).
- [ ] Windows Firewall may ask about Python the first time. Allow it on
      **Private** networks; Tailscale traffic counts as private.

### 3c. Install and connect (Surface)

- [ ] Run the setup exe. It installs for your account only, with no admin
      prompt.
- [ ] Start **Lumi Island** from the Start menu. Hover the top of the screen,
      click Lumi's face, then the gear.
- [ ] Enter the Desktop's Tailscale address, port `5090` and the pairing code,
      then **Save**.

The status under Lumi's name changes to **Here with you**. Approvals, chat,
calendar and the Pipeline view now come from the Desktop. The island
reconnects by itself after either machine restarts.

### 3d. Unpair

- **New code** on the Desktop disconnects every paired island; enter the new
  code on the devices you still want.
- **Turn off** closes the Tailscale listener.
- **Use this PC** in the island's gear panel forgets the Desktop on that
  device.

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
| Island on the Surface says **Can't reach** *address* | Remote island is off, Tailscale is down on either machine, or the firewall blocks Python | Check **Remote island** shows **On**, run `tailscale ping <desktop>`, and allow Python on Private networks |
| Island says **Pairing code rejected** | Wrong code, or a new code was made | Make a new code on the Desktop and enter it in the gear panel. Five wrong tries lock that device out for ten minutes |
| **Remote island** says it couldn't listen | Tailscale started after Lumi, or its address changed | Select **Turn off**, then turn it on again |

## Security notes

- Tailscale encrypts the traffic between your devices, and only devices on your
  tailnet can reach them. Keep the tailnet to devices you own.
- Turn on two-factor sign-in for the account Tailscale uses.
- A paired island can approve tools, chat as you and read your calendar and
  mail summary. Pair only your own devices, and make a new code if one may
  have leaked.
- You don't need `tailscale serve`; the island connects to the Tailscale
  address directly.
- Never bind the HITL server to `0.0.0.0`. That exposes it on every network
  the Desktop is connected to, including public Wi-Fi.
- If you lose the Surface, remove it in the Tailscale admin console under
  **Machines**, so it can't reconnect.

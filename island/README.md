# Lumi Island

A small island that hangs from the top of the screen. It's hidden as a thin
sliver, peeks out when you hover, and opens when you click. Lumi's face is
its icon: a looping video cropped from her art, ringed by her live status
(here, thinking, speaking, gaming, offline). Open it for four mini-apps, so
you don't have to switch to the web client:

- **Home:** the time and day, the weather, your next event, today's
  remaining events, and your top unread mail. The clock is 12- or 24-hour as
  chosen in the web client (**Settings, then General, then Display**), and
  changing it there updates an open island at once. Calendar times follow the
  same choice. The weather needs a city set in that same card; see below.
- **Chat:** talk to Lumi. Her replies appear the moment they exist, and
  while she thinks you see typing dots. Messages you send are host messages,
  the same as typing in the web client on this PC.
- **Calendar:** your Google Calendar as **List**, **Day**, **Month** or
  **Year**, with previous/next/today. Click a day to open it, a month name to
  zoom in.
- **Pipeline:** what each of Lumi's agents is doing right now, as three cards
  side by side: **Main** (Lumi herself), **Minecraft** (her in-game half) and
  **Office** (calendar, email and Claude Code work). Each card shows a status,
  the task, a progress bar, the model tier (HAIKU green, SONNET blue, OPUS
  purple) and the model that actually runs it, such as `qwen3:8b` or
  `Claude Code (opus)`. Thinking pulses blue, executing is green with a moving
  bar, waiting for you pulses yellow, and an error flashes red.

Tool approvals take the island over until you answer them, then it goes back
to what you were doing. If the island is open on Pipeline, the approval
appears on that agent's card instead (it grows to show **Decline** and
**Approve**), so you can see who is asking. An open island stays open while you work in it; it
tucks away on Esc, the collapse button, or when you click back into a game.

All of it comes over one socket (`ws://127.0.0.1:5090/hitl` on this PC, or
your desktop's Tailscale address from another device) and is pushed, never
polled. See [../docs/architecture.md](../docs/architecture.md#lumi-island-bridge).

It's a [Tauri 2](https://tauri.app) app: a native window with real
transparency and a small memory footprint. The page in `src/` is plain
HTML/CSS/JS with no bundler; `src-tauri/` is the Rust shell.

Behaviour is inspired by [coucou](https://github.com/Louis-CFM/coucou) (MIT).
No coucou name, mascot, art or sounds are used. The face video is cropped
from Lumi's own artwork (`assets/branding/`).

## Weather

Weather comes from [Open-Meteo](https://open-meteo.com) (free, no account or
key). It is off until you enter a city under **Settings, then General, then
Display**. The city name is sent once to find its coordinates, then the
coordinates are sent to fetch the forecast. There is no IP lookup or
auto-detect. A reading is kept for 15 minutes and fetched only when the Home
tab asks, so nothing runs in the background. °F or °C is a setting too.

## Idle cost

Hidden, the island plays no video and runs no timers; a reconnect timer only
runs while Lumi is unreachable. The Home clock ticks once a minute, and only
while the island is open on Home. The video also pauses in Gaming Mode.

## Build once

1. Install Rust from <https://rustup.rs> (the default options are fine), then
   open a new PowerShell window.
2. Build:

   ```powershell
   cd island
   npm install
   npm run build
   ```

   The first build takes several minutes. The result is
   `src-tauri/target/release/lumi-island.exe`.

Stop Lumi and the island before building again: Windows locks a running exe.

## Installers for another device

```powershell
cd island
npm run build:standalone
```

This builds the exe and two installers (the first run downloads the WiX and
NSIS tools):

| File | Notes |
|---|---|
| `src-tauri/target/release/bundle/nsis/Lumi Island_0.1.0_x64-setup.exe` | Installs for the current user, no admin needed. Use this one. |
| `src-tauri/target/release/bundle/msi/Lumi Island_0.1.0_x64_en-US.msi` | For machines that prefer MSI |

Both are about 2 MB. If the other device lacks the WebView2 runtime (rare on
Windows 11), the installer downloads it.

## Use on another device (your Surface)

The island has no server of its own; it only opens the socket. So on another
device you point it at your desktop and pair it once.

1. On the desktop: make sure Tailscale is running and signed in, then in
   Lumi's web client open **Settings, then General, then Remote island** and
   select **Turn on and show a pairing code**. The code is shown once.
2. Copy the setup exe to the Surface (USB, OneDrive, Taildrop) and run it.
3. Start **Lumi Island** from the Start menu. Hover the top of the screen,
   click Lumi's face, then the **gear**.
4. Enter the desktop's Tailscale address (shown next to the code), port
   `5090`, and the code. Select **Save**.

The island shows **Here with you** once it's connected. The address and code
are kept on that device, so it reconnects by itself after a restart.
**Use this PC** forgets them. If the island says **Pairing code rejected**, the
code was wrong or replaced: make a new one on the desktop and enter it again.
After a rejected code the island stops retrying until you save a new one,
because five wrong codes in ten minutes lock that device out for ten minutes.

On the desktop, **New code** disconnects every paired island at once, and
**Turn off** closes the Tailscale listener entirely.

A paired island can do everything the local one can: approve tools, chat as
you, and see your calendar and mail summary. Only pair devices you own.

## Use

Turn on **Settings → General → Lumi Island** in Lumi's web client. Lumi starts
the island with no console window, closes it when Lumi exits, and starts it
again on the next boot while the switch is on.

To work on the page with live reload, run `npm run dev` (Lumi should be
running so the island can connect).

## Files

| Path | What |
|---|---|
| `src/index.html`, `island.css`, `island.js` | The island: states, status, approvals |
| `src/lumi.mp4`, `lumi_poster.jpg` | Lumi's face: a 128 px, 42 KB loop and its still frame (cropped from `assets/branding/.../lumi_sleeping_brb.mp4`) |
| `src/lumi.png` | Spare avatar image |
| `src-tauri/src/main.rs` | Window setup, the `fit` command that sizes the window to the island, and `island_token` / `island_ws_url`, which hand the page the launch secret and socket address Lumi put in its environment |
| `src-tauri/tauri.conf.json` | Transparent, frameless, always-on-top window; installers (`targets: all`); CSP allows scripts and media from the app itself and WebSockets to any host, because the host you enter in the gear panel decides where it connects (the server enforces who gets in) |
| `../modules/island_manager.py` | Starts/stops the app for the Settings switch; gives each launch a fresh secret |
| `../modules/island_bridge.py` | The Home / Chat / Calendar messages on the socket |
| `../modules/agent_tracker.py` | The Pipeline's state; every change is pushed as `agent_update` |
| `../modules/model_router.py` | Picks haiku / sonnet / opus for each message |
| `../modules/island_remote.py` | Remote pairing: Tailscale-only listener, code hash, lockout |

# Lumi Island

A small island that hangs from the top of the screen. It's hidden as a thin
sliver, peeks out when you hover, and opens when you click. Lumi's face is
its icon: a looping video cropped from her art, ringed by her live status
(here, thinking, speaking, gaming, offline). Open it for three mini-apps, so
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

Tool approvals take the island over until you answer them, then it goes back
to what you were doing. An open island stays open while you work in it; it
tucks away on Esc, the collapse button, or when you click back into a game.

All of it comes over one local socket (`ws://127.0.0.1:5090/hitl`) and is
pushed, never polled. See [../docs/architecture.md](../docs/architecture.md#lumi-island-bridge).

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
| `src-tauri/tauri.conf.json` | Transparent, frameless, always-on-top window; CSP allows scripts and media from the app itself and sockets to loopback only |
| `../modules/island_manager.py` | Starts/stops the app for the Settings switch; gives each launch a fresh secret |
| `../modules/island_bridge.py` | The Home / Chat / Calendar messages on the socket |

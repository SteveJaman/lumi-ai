# Lumi Island

A small island that hangs from the top of the screen. It's hidden as a thin
sliver, peeks out when you hover, and opens when you click. It shows Lumi's
live status (here, thinking, speaking, offline). Tool approvals from Lumi's
HITL server (`modules/hitl_server.py`, `ws://127.0.0.1:5090/hitl`) take it
over until you answer them.

It's a [Tauri 2](https://tauri.app) app: a native window with real
transparency and a small memory footprint. The page in `src/` is plain
HTML/CSS/JS with no bundler; `src-tauri/` is the Rust shell.

Behaviour is inspired by [coucou](https://github.com/Louis-CFM/coucou) (MIT).
No coucou name, mascot, art or sounds are used.

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
| `src/lumi.png` | Lumi's avatar (from `assets/branding`) |
| `src-tauri/src/main.rs` | Window setup and the `fit` command that sizes the window to the island |
| `src-tauri/tauri.conf.json` | Transparent, frameless, always-on-top window; CSP allows only the HITL socket |
| `../modules/island_manager.py` | Starts/stops the app for the Settings switch |

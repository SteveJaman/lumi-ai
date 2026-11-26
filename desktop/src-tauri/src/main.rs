// Lumi Desktop: Lumi's web app in its own window.
//
// The bundled page (../src) is a small connect screen: pick "This PC" or
// the address of your desktop (from a laptop), and the window then loads
// Lumi's real web app from that server. Lumi's pages run as remote content
// with no Tauri IPC (capabilities/default.json gives IPC to nothing but
// this app's own page), so the shell can't be driven from a page it loads.
//
// A small menu goes back to the connect screen, reloads, or quits.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use tauri::menu::{MenuBuilder, MenuItemBuilder, SubmenuBuilder};
use tauri::{Manager, Url};

/// The connect page's address inside the app (Tauri 2 serves bundled files
/// from http://tauri.localhost on Windows and tauri://localhost elsewhere).
fn connect_url() -> Url {
    let base = if cfg!(windows) {
        "http://tauri.localhost/index.html?switch=1"
    } else {
        "tauri://localhost/index.html?switch=1"
    };
    Url::parse(base).expect("static url")
}

fn main() {
    tauri::Builder::default()
        .setup(|app| {
            let switch = MenuItemBuilder::with_id("switch", "Switch server…")
                .accelerator("CmdOrCtrl+Shift+L")
                .build(app)?;
            let reload = MenuItemBuilder::with_id("reload", "Reload")
                .accelerator("CmdOrCtrl+R")
                .build(app)?;
            let quit = MenuItemBuilder::with_id("quit", "Quit").build(app)?;
            let lumi = SubmenuBuilder::new(app, "Lumi")
                .item(&switch)
                .item(&reload)
                .separator()
                .item(&quit)
                .build()?;
            let menu = MenuBuilder::new(app).item(&lumi).build()?;
            app.set_menu(menu)?;
            app.on_menu_event(|app, event| {
                let Some(win) = app.get_webview_window("main") else {
                    return;
                };
                match event.id().as_ref() {
                    "switch" => {
                        let _ = win.navigate(connect_url());
                    }
                    "reload" => {
                        let _ = win.eval("window.location.reload()");
                    }
                    "quit" => app.exit(0),
                    _ => {}
                }
            });
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running Lumi Desktop");
}

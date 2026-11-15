// Lumi Island: a transparent, always-on-top window at the top centre of
// the screen. The page (../src) decides the island's size per state and
// calls `fit`; the window is always exactly the island, so it never
// covers anything else. Lumi starts and stops this process itself
// (modules/island_manager.py).
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use tauri::{LogicalPosition, LogicalSize, Manager, WebviewWindow};

/// Resize the window to `width` x `height` (logical px), centred at the
/// top edge of the monitor it is on.
#[tauri::command]
fn fit(window: WebviewWindow, width: f64, height: f64) -> Result<(), String> {
    let width = width.clamp(40.0, 600.0);
    let height = height.clamp(4.0, 600.0);
    let monitor = window
        .current_monitor()
        .map_err(|e| e.to_string())?
        .or(window.primary_monitor().map_err(|e| e.to_string())?)
        .ok_or("no monitor")?;
    let scale = monitor.scale_factor();
    let size = monitor.size().to_logical::<f64>(scale);
    let pos = monitor.position().to_logical::<f64>(scale);
    window
        .set_size(LogicalSize::new(width, height))
        .map_err(|e| e.to_string())?;
    window
        .set_position(LogicalPosition::new(pos.x + (size.width - width) / 2.0, pos.y))
        .map_err(|e| e.to_string())?;
    Ok(())
}

/// The per-launch secret Lumi put in this process's environment; the page
/// sends it as the first message on the socket. Empty when started by hand.
#[tauri::command]
fn island_token() -> String {
    std::env::var("LUMI_ISLAND_TOKEN").unwrap_or_default()
}

/// Where Lumi's approval/bridge socket listens. Lumi's island manager sets
/// LUMI_ISLAND_WS from its configured port; the default is the standard one.
#[tauri::command]
fn island_ws_url() -> String {
    std::env::var("LUMI_ISLAND_WS").unwrap_or_else(|_| "ws://127.0.0.1:5090/hitl".to_string())
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![fit, island_token, island_ws_url])
        .setup(|app| {
            if let Some(window) = app.get_webview_window("island") {
                let _ = fit(window, 200.0, 8.0);
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("Lumi Island failed to start");
}

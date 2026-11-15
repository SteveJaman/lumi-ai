// Lumi Island: a transparent, always-on-top window at the top centre of
// the screen. The page (../src) decides the island's size per state and
// calls `fit`; the window is always exactly the island, so it never
// covers anything else. Lumi starts and stops this process itself
// (modules/island_manager.py).
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use tauri::{AppHandle, Emitter, LogicalPosition, LogicalSize, Manager, WebviewWindow};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, Shortcut, ShortcutState};

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

// ── Quick Capture ─────────────────────────────────────────────────────
// A global hotkey focuses the island and tells the page to open its
// capture bar ("quick-capture" event). The page owns the bar; this side
// only registers the key and reads a file the user typed the path of.

const DEFAULT_SHORTCUT: &str = "Alt+Space";
const FALLBACK_SHORTCUT: &str = "Alt+Shift+Space";
const MAX_CAPTURE_BYTES: u64 = 25 * 1024 * 1024; // modules/knowledge.py MAX_BYTES
// The library's readable kinds: exactly modules/library_indexer.py SUPPORTED_EXTS
// (tests/test_island_capture_focus.py checks they match).
const CAPTURE_EXTS: &[&str] = &[
    "pdf", "docx", "doc", "txt", "md", "json", "csv", "log", "rst", "yaml", "yml", "toml", "ini",
    "cfg", "xml", "html", "css", "py", "js", "mjs", "ts", "tsx", "jsx", "java", "kt", "c", "h",
    "cpp", "hpp", "cs", "go", "rs", "rb", "php", "swift", "lua", "sql", "sh", "ps1", "bat",
];

fn open_capture(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("island") {
        let _ = window.show();
        let _ = window.set_focus();
    }
    let _ = app.emit("quick-capture", ());
}

/// Register the Quick Capture hotkey (replacing any earlier one). An empty
/// value means the default. If the key is taken by another app, logs it and
/// uses Alt+Shift+Space; returns the one actually registered.
#[tauri::command]
fn set_capture_shortcut(app: AppHandle, accel: String) -> Result<String, String> {
    let wanted = if accel.trim().is_empty() { DEFAULT_SHORTCUT } else { accel.trim() };
    let shortcut: Shortcut = wanted.parse().map_err(|e| format!("{wanted} isn't a shortcut: {e}"))?;
    let gs = app.global_shortcut();
    let _ = gs.unregister_all();
    match gs.register(shortcut) {
        Ok(()) => Ok(wanted.to_string()),
        Err(e) => {
            eprintln!("[island] {wanted} is unavailable ({e}); using {FALLBACK_SHORTCUT}");
            let fallback: Shortcut = FALLBACK_SHORTCUT.parse().map_err(|e| format!("{e}"))?;
            gs.register(fallback)
                .map_err(|e2| format!("{wanted} and {FALLBACK_SHORTCUT} are both taken ({e2})"))?;
            Ok(FALLBACK_SHORTCUT.to_string())
        }
    }
}

/// The bytes of a file whose absolute path was typed into Quick Capture, for
/// the page to send through the usual chunked library upload. "missing" when
/// there's no such file (the page then saves the text as a memory instead).
#[tauri::command]
fn read_capture_file(path: String) -> Result<tauri::ipc::Response, String> {
    let p = std::path::Path::new(path.trim());
    if !p.is_absolute() {
        return Err("missing".into());
    }
    let meta = std::fs::metadata(p).map_err(|_| "missing".to_string())?;
    if !meta.is_file() {
        return Err("missing".into());
    }
    let ext = p.extension().and_then(|e| e.to_str()).unwrap_or("").to_ascii_lowercase();
    if !CAPTURE_EXTS.contains(&ext.as_str()) {
        return Err(format!("Lumi can't read .{ext} files yet"));
    }
    if meta.len() > MAX_CAPTURE_BYTES {
        return Err("it's over 25 MB".into());
    }
    std::fs::read(p).map(tauri::ipc::Response::new).map_err(|e| e.to_string())
}

fn main() {
    tauri::Builder::default()
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(|app, _shortcut, event| {
                    if event.state() == ShortcutState::Pressed {
                        open_capture(app);
                    }
                })
                .build(),
        )
        .invoke_handler(tauri::generate_handler![
            fit,
            island_token,
            island_ws_url,
            set_capture_shortcut,
            read_capture_file
        ])
        .setup(|app| {
            if let Some(window) = app.get_webview_window("island") {
                // The window keeps the island's widest size (island.js WIN_W) and
                // only its height changes, so re-centring never shifts the island.
                let _ = fit(window, 480.0, 8.0);
            }
            // The default key right away; the page re-registers the saved one.
            if let Err(e) = set_capture_shortcut(app.handle().clone(), String::new()) {
                eprintln!("[island] no Quick Capture hotkey: {e}");
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("Lumi Island failed to start");
}

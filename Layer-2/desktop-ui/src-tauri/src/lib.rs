mod server;

use server::LocalServer;
use tauri::Manager;

// Learn more about Tauri commands at https://tauri.app/develop/calling-rust/
#[tauri::command]
fn greet(name: &str) -> String {
    format!("Hello, {}! You've been greeted from Rust!", name)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(LocalServer::default())
        .setup(|app| {
            let state = app.state::<LocalServer>();
            if let Err(error) = state.start(app.handle()) {
                eprintln!("EPIS local server could not start: {error}");
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            if matches!(event, tauri::WindowEvent::Destroyed) {
                window.app_handle().state::<LocalServer>().stop();
            }
        })
        .invoke_handler(tauri::generate_handler![greet, server::server_config])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

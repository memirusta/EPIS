mod server;
mod startup;

use server::LocalServer;
use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Manager,
};

fn show_main_window(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let start_in_background = std::env::args().any(|argument| argument == "--background");

    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(LocalServer::default())
        .setup(move |app| {
            let state = app.state::<LocalServer>();
            if let Err(error) = state.start(app.handle()) {
                eprintln!("EPIS desktop runtime could not start: {error}");
            }

            let open_item = MenuItem::with_id(app, "open", "Open EPIS", true, None::<&str>)?;
            let quit_item = MenuItem::with_id(app, "quit", "Quit EPIS", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&open_item, &quit_item])?;

            let mut tray = TrayIconBuilder::with_id("epis")
                .tooltip("EPIS")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "open" => show_main_window(app),
                    "quit" => {
                        app.state::<LocalServer>().stop();
                        app.exit(0);
                    }
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        show_main_window(tray.app_handle());
                    }
                });

            if let Some(icon) = app.default_window_icon() {
                tray = tray.icon(icon.clone());
            }
            tray.build(app)?;

            if start_in_background {
                if let Some(window) = app.get_webview_window("main") {
                    window.hide()?;
                }
            }

            Ok(())
        })
        .on_window_event(|window, event| match event {
            tauri::WindowEvent::CloseRequested { api, .. } => {
                api.prevent_close();
                let _ = window.hide();
            }
            tauri::WindowEvent::Destroyed => {
                window.app_handle().state::<LocalServer>().stop();
            }
            _ => {}
        })
        .invoke_handler(tauri::generate_handler![
            server::server_config,
            startup::startup_state,
            startup::set_startup_enabled,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

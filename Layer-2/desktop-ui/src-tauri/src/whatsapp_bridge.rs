//! Own the local Baileys bridge for the lifetime of the Windows desktop app.
//! Existing listeners are left alone; only a child spawned here is stopped.

use std::env;
use std::fs::{self, OpenOptions};
use std::net::{IpAddr, Ipv4Addr, SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Mutex,
};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};
use tauri::{AppHandle, Manager};

use std::os::windows::process::CommandExt;

const CREATE_NO_WINDOW: u32 = 0x0800_0000;
const DEFAULT_PORT: u16 = 8766;
const WATCHDOG_POLL: Duration = Duration::from_secs(5);

pub struct WhatsAppBridge {
    child: Arc<Mutex<Option<Child>>>,
    stop_requested: Arc<AtomicBool>,
    watchdog: Mutex<Option<JoinHandle<()>>>,
}

impl Default for WhatsAppBridge {
    fn default() -> Self {
        Self {
            child: Arc::new(Mutex::new(None)),
            stop_requested: Arc::new(AtomicBool::new(false)),
            watchdog: Mutex::new(None),
        }
    }
}

impl WhatsAppBridge {
    pub fn start(&self, app: &AppHandle) -> Result<(), String> {
        let mut watchdog = self
            .watchdog
            .lock()
            .map_err(|_| "bridge watchdog lock poisoned")?;
        if watchdog.is_some() {
            return Ok(());
        }
        self.stop_requested.store(false, Ordering::SeqCst);

        match spawn_bridge(app) {
            Ok(Some(child)) => {
                *self
                    .child
                    .lock()
                    .map_err(|_| "bridge child lock poisoned")? = Some(child);
            }
            Ok(None) => eprintln!(
                "EPIS WhatsApp bridge port is already in use; leaving the existing listener alone"
            ),
            Err(error) => eprintln!("EPIS WhatsApp bridge initial start failed: {error}"),
        }

        let child_slot = Arc::clone(&self.child);
        let stop_requested = Arc::clone(&self.stop_requested);
        let app = app.clone();
        let handle = thread::Builder::new()
            .name("epis-whatsapp-bridge-watchdog".to_owned())
            .spawn(move || {
                let mut last_start_error = String::new();
                while !stop_requested.load(Ordering::SeqCst) {
                    let needs_spawn = match child_slot.lock() {
                        Ok(mut slot) => {
                            if let Some(child) = slot.as_mut() {
                                match child.try_wait() {
                                    Ok(None) => false,
                                    Ok(Some(status)) => {
                                        eprintln!("EPIS WhatsApp bridge exited: {status}");
                                        *slot = None;
                                        true
                                    }
                                    Err(error) => {
                                        eprintln!(
                                            "EPIS WhatsApp bridge status check failed: {error}"
                                        );
                                        *slot = None;
                                        true
                                    }
                                }
                            } else {
                                true
                            }
                        }
                        Err(_) => break,
                    };

                    if needs_spawn && !stop_requested.load(Ordering::SeqCst) {
                        match spawn_bridge(&app) {
                            Ok(Some(mut child)) => {
                                last_start_error.clear();
                                if stop_requested.load(Ordering::SeqCst) {
                                    let _ = child.kill();
                                    let _ = child.wait();
                                } else if let Ok(mut slot) = child_slot.lock() {
                                    *slot = Some(child);
                                } else {
                                    let _ = child.kill();
                                    let _ = child.wait();
                                    break;
                                }
                            }
                            Ok(None) => last_start_error.clear(),
                            Err(error) => {
                                if error != last_start_error {
                                    eprintln!("EPIS WhatsApp bridge restart failed: {error}");
                                    last_start_error = error;
                                }
                            }
                        }
                    }
                    sleep_interruptible(&stop_requested, WATCHDOG_POLL);
                }
            })
            .map_err(|error| error.to_string())?;
        *watchdog = Some(handle);
        Ok(())
    }

    pub fn stop(&self) {
        self.stop_requested.store(true, Ordering::SeqCst);
        if let Ok(mut watchdog) = self.watchdog.lock() {
            if let Some(handle) = watchdog.take() {
                let _ = handle.join();
            }
        }
        if let Ok(mut slot) = self.child.lock() {
            if let Some(mut child) = slot.take() {
                let _ = child.kill();
                let _ = child.wait();
            }
        }
    }
}

fn spawn_bridge(app: &AppHandle) -> Result<Option<Child>, String> {
    if port_is_listening(configured_port()) {
        return Ok(None);
    }
    let resource_dir = if cfg!(debug_assertions) {
        None
    } else {
        Some(
            app.path()
                .resource_dir()
                .map_err(|error| error.to_string())?,
        )
    };

    let bridge_dir = if let Some(resources) = resource_dir.as_ref() {
        resources.join("whatsapp-bridge")
    } else {
        PathBuf::from(
            env!("CARGO_MANIFEST_DIR")
        ).join("../../whatsapp-bridge")
    };
    let script = bridge_dir.join("bridge.mjs");
    if !script.is_file() || !bridge_dir.join("policy.mjs").is_file() {
        return Err(format!(
            "bundled WhatsApp bridge files missing: {}",
            bridge_dir.display()
        ));
    }
    if !bridge_dir.join("node_modules").is_dir() {
        return Err("WhatsApp bridge dependencies are missing; run npm ci in Layer-2/whatsapp-bridge before building".to_owned());
    }
    if !private_config_path().is_file() && env::var("EPIS_WHATSAPP_BRIDGE_TOKEN").is_err() {
        return Err("private WhatsApp bridge config is missing".to_owned());
    }
    let log = bridge_log(app);
    let mut last_error = String::from("Node.js 20+ could not be found");
    for program in node_candidates(
        resource_dir.as_deref(),
    ) {
        let mut command = Command::new(&program);
        command
            // Tauri may return a Windows verbatim resource path
            // (\\?\C:\...). Node's CLI can mis-resolve that form as
            // the entry script. The child already runs inside bridge_dir,
            // so use a relative entrypoint instead.
            .arg("bridge.mjs")
            .current_dir(&bridge_dir)
            .env_clear()
            .env("EPIS_WHATSAPP_HEADLESS", "1")
            .stdin(Stdio::null())
            .creation_flags(CREATE_NO_WINDOW);
        copy_bridge_environment(&mut command);
        if let Some((stdout, stderr)) = &log {
            command.stdout(
                stdout
                    .try_clone()
                    .map(Stdio::from)
                    .unwrap_or_else(|_| Stdio::null()),
            );
            command.stderr(
                stderr
                    .try_clone()
                    .map(Stdio::from)
                    .unwrap_or_else(|_| Stdio::null()),
            );
        } else {
            command.stdout(Stdio::null()).stderr(Stdio::null());
        }
        match command.spawn() {
            Ok(mut child) => {
                thread::sleep(Duration::from_millis(350));
                match child.try_wait() {
                    Ok(None) => return Ok(Some(child)),
                    Ok(Some(status)) => {
                        last_error = format!(
                            "{} exited during bridge startup with {status}",
                            program.display()
                        );
                    }
                    Err(error) => last_error = error.to_string(),
                }
            }
            Err(error) => last_error = format!("{}: {error}", program.display()),
        }
    }
    Err(last_error)
}

fn node_candidates(
    resource_dir: Option<&Path>,
) -> Vec<PathBuf> {
    let mut candidates = Vec::new();

    // Release builds are self-contained: use the Node runtime bundled
    // alongside the bridge before considering developer/system fallbacks.
    if let Some(resources) = resource_dir {
        candidates.push(
            resources
                .join("whatsapp-bridge")
                .join("runtime")
                .join("node.exe"),
        );
    }

    if let Some(configured) = env::var_os("EPIS_NODE") {
        candidates.push(
            PathBuf::from(configured)
        );
    }

    for key in [
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
    ] {
        if let Some(base) = env::var_os(key) {
            candidates.push(
                PathBuf::from(base)
                    .join("nodejs")
                    .join("node.exe"),
            );
        }
    }

    candidates.push(
        PathBuf::from("node.exe")
    );

    candidates
}

fn private_config_path() -> PathBuf {
    env::var_os("EPIS_WHATSAPP_BRIDGE_CONFIG")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .or_else(|| {
            env::var_os("LOCALAPPDATA")
                .map(|base| PathBuf::from(base).join("EPIS").join("whatsapp-bridge.env"))
        })
        .unwrap_or_default()
}

fn configured_port() -> u16 {
    if let Some(value) = env::var_os("EPIS_WHATSAPP_BRIDGE_PORT") {
        return parse_port(&value.to_string_lossy()).unwrap_or(DEFAULT_PORT);
    }
    fs::read_to_string(private_config_path())
        .ok()
        .and_then(|config| {
            config.lines().find_map(|line| {
                let (key, value) = line.trim().split_once('=')?;
                (key.trim() == "EPIS_WHATSAPP_BRIDGE_PORT")
                    .then(|| parse_port(value.trim()))
                    .flatten()
            })
        })
        .unwrap_or(DEFAULT_PORT)
}

fn parse_port(value: &str) -> Option<u16> {
    value.parse::<u16>().ok().filter(|port| *port >= 1024)
}

fn port_is_listening(port: u16) -> bool {
    TcpStream::connect_timeout(
        &SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), port),
        Duration::from_millis(150),
    )
    .is_ok()
}

fn copy_bridge_environment(command: &mut Command) {
    for key in [
        "SYSTEMROOT",
        "WINDIR",
        "SYSTEMDRIVE",
        "COMSPEC",
        "PATH",
        "PATHEXT",
        "TEMP",
        "TMP",
        "APPDATA",
        "LOCALAPPDATA",
        "USERPROFILE",
        "EPIS_WHATSAPP_BRIDGE_CONFIG",
        "EPIS_WHATSAPP_BRIDGE_PORT",
        "EPIS_WHATSAPP_BRIDGE_TOKEN",
        "EPIS_WHATSAPP_AUTH_DIR",
        "EPIS_WHATSAPP_CONTACTS_FILE",
        "EPIS_SERVER_HTTP_URL",
        "EPIS_INTERNAL_EVENT_TOKEN",
    ] {
        if let Some(value) = env::var_os(key) {
            command.env(key, value);
        }
    }
}

fn bridge_log(app: &AppHandle) -> Option<(fs::File, fs::File)> {
    let dir = app.path().app_log_dir().ok()?;
    fs::create_dir_all(&dir).ok()?;
    let path = dir.join("epis-whatsapp-bridge.log");
    let stdout = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
        .ok()?;
    let stderr = OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .ok()?;
    Some((stdout, stderr))
}

fn sleep_interruptible(stop: &AtomicBool, duration: Duration) {
    let deadline = Instant::now() + duration;
    while !stop.load(Ordering::SeqCst) && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(200));
    }
}

#[cfg(test)]
mod tests {
    use super::{
        copy_bridge_environment,
        node_candidates,
        parse_port,
        port_is_listening,
    };
    use std::net::TcpListener;
    use std::path::Path;
    use std::process::Command;

    #[test]
    fn only_loopback_service_ports_are_accepted() {
        assert_eq!(parse_port("8766"), Some(8766));
        assert_eq!(parse_port("80"), None);
        assert_eq!(parse_port("65536"), None);
    }

    #[test]
    fn occupied_loopback_port_is_not_started_again() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        assert!(port_is_listening(port));
    }

    #[test]
    fn bundled_release_node_is_first_candidate() {
        let root = Path::new(
            r"C:\EPIS\resources"
        );

        let candidates = node_candidates(
            Some(root)
        );

        assert_eq!(
            candidates.first().unwrap(),
            &root
                .join("whatsapp-bridge")
                .join("runtime")
                .join("node.exe")
        );
    }

    #[test]
    fn bridge_child_does_not_inherit_core_or_model_credentials() {
        let mut command = Command::new("node.exe");
        command.env_clear();
        copy_bridge_environment(&mut command);
        let keys: Vec<_> = command
            .get_envs()
            .map(|(key, _)| key.to_string_lossy().to_string())
            .collect();
        for private in ["EPIS_SERVER_TOKEN", "OPENAI_API_KEY", "PYTHONPATH"] {
            assert!(!keys.iter().any(|key| key == private));
        }
    }
}

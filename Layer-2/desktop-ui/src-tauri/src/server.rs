use serde::{Deserialize, Serialize};
use std::env;
use std::fs::{self, OpenOptions};
use std::net::{IpAddr, Ipv4Addr, SocketAddr, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;
use tauri::{AppHandle, Manager};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

const LOCAL_SERVER_URL: &str = "ws://127.0.0.1:8000/ws";

#[derive(Clone, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ServerConfig {
    url: String,
    token: String,
}

#[derive(Default)]
pub struct LocalServer {
    child: Mutex<Option<Child>>,
}

#[tauri::command]
pub fn server_config(app: AppHandle) -> ServerConfig {
    resolved_server_config(&app)
}

impl LocalServer {
    pub fn start(&self, app: &AppHandle) -> Result<(), String> {
        let config = resolved_server_config(app);
        let Some(port) = local_server_port(&config.url) else {
            return self.start_remote_device_agent(app, &config);
        };
        if local_server_is_ready(port) {
            return Ok(());
        }

        let source_root = source_root(app)?;
        let script = source_root.join("server").join("local_server.py");
        if !script.is_file() {
            return Err(format!(
                "EPIS server entry point not found: {}",
                script.display()
            ));
        }

        let log_stdio = server_log(app);
        let mut last_error = String::from("Python runtime not found");
        for (program, prefix_args) in python_candidates() {
            let mut command = Command::new(&program);
            command
                .args(prefix_args)
                .arg(&script)
                .current_dir(&source_root)
                .env("PYTHONPATH", &source_root)
                .env("PYTHONUNBUFFERED", "1")
                .env("EPIS_LOCAL_SERVER_MANAGED", "1")
                .env("EPIS_SERVER_PORT", port.to_string())
                .stdin(Stdio::null());

            if !cfg!(debug_assertions) {
                if let Ok(data_dir) = app.path().app_data_dir() {
                    command
                        .env("EPIS_MEMORY_DIR", data_dir.join("Layer-1").join("memory"))
                        .env("EPIS_KEY_PATH", data_dir.join("Layer-3").join("epis.key"));
                }
                if let Ok(identity_dir) = app.path().resource_dir() {
                    command.env(
                        "EPIS_IDENTITY_DIR",
                        identity_dir.join("Layer-1").join("identity"),
                    );
                }
            }

            match &log_stdio {
                Some((stdout, stderr)) => {
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
                }
                None => {
                    command.stdout(Stdio::null()).stderr(Stdio::null());
                }
            }

            #[cfg(windows)]
            command.creation_flags(CREATE_NO_WINDOW);

            match command.spawn() {
                Ok(mut child) => {
                    std::thread::sleep(Duration::from_millis(250));
                    match child.try_wait() {
                        Ok(None) => {
                            *self.child.lock().map_err(|_| "server lock poisoned")? = Some(child);
                            return Ok(());
                        }
                        Ok(Some(status)) => {
                            last_error = format!(
                                "{} exited during startup with {}",
                                program.display(),
                                status
                            );
                        }
                        Err(error) => {
                            last_error = error.to_string();
                        }
                    }
                }
                Err(error) => {
                    last_error = format!("{}: {}", program.display(), error);
                }
            }
        }
        Err(last_error)
    }

    fn start_remote_device_agent(
        &self,
        app: &AppHandle,
        config: &ServerConfig,
    ) -> Result<(), String> {
        if !config.url.starts_with("wss://") {
            return Err("Remote EPIS server must use wss://".to_owned());
        }
        if config.token.trim().is_empty() {
            return Err("Remote EPIS server token is missing".to_owned());
        }

        let source_root = source_root(app)?;
        let script = source_root.join("server").join("cloud_device_agent.py");
        if !script.is_file() {
            return Err(format!(
                "EPIS device agent entry point not found: {}",
                script.display()
            ));
        }

        let log_stdio = server_log(app);
        let mut last_error = String::from("Python runtime not found");
        for (program, prefix_args) in python_candidates() {
            let mut command = Command::new(&program);
            command
                .args(prefix_args)
                .args(["-I", "-X", "utf8"])
                .arg(&script)
                .current_dir(&source_root)
                .env_clear();

            copy_safe_device_environment(&mut command);
            command
                .env("PYTHONUNBUFFERED", "1")
                .env("PYTHONUTF8", "1")
                .env("EPIS_SERVER_URL", &config.url)
                .env("EPIS_SERVER_TOKEN", &config.token)
                .stdin(Stdio::null());

            match &log_stdio {
                Some((stdout, stderr)) => {
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
                }
                None => {
                    command.stdout(Stdio::null()).stderr(Stdio::null());
                }
            }

            #[cfg(windows)]
            command.creation_flags(CREATE_NO_WINDOW);

            match command.spawn() {
                Ok(mut child) => {
                    std::thread::sleep(Duration::from_millis(350));
                    match child.try_wait() {
                        Ok(None) => {
                            *self.child.lock().map_err(|_| "server lock poisoned")? = Some(child);
                            return Ok(());
                        }
                        Ok(Some(status)) if status.code() == Some(73) => {
                            return Ok(());
                        }
                        Ok(Some(status)) => {
                            last_error = format!(
                                "{} exited during device-agent startup with {}",
                                program.display(),
                                status
                            );
                        }
                        Err(error) => {
                            last_error = error.to_string();
                        }
                    }
                }
                Err(error) => {
                    last_error = format!("{}: {}", program.display(), error);
                }
            }
        }
        Err(last_error)
    }

    pub fn stop(&self) {
        let Ok(mut slot) = self.child.lock() else {
            return;
        };
        if let Some(mut child) = slot.take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

fn is_allowed_url(url: &str) -> bool {
    url == LOCAL_SERVER_URL
        || url.starts_with("ws://localhost:")
        || url.starts_with("ws://127.0.0.1:")
        || url.starts_with("wss://")
}

fn resolved_server_config(app: &AppHandle) -> ServerConfig {
    let mut config = stored_server_config(app).unwrap_or_else(|| ServerConfig {
        url: LOCAL_SERVER_URL.to_owned(),
        token: String::new(),
    });

    if let Ok(requested_url) = env::var("EPIS_SERVER_URL") {
        if is_allowed_url(&requested_url) {
            config.url = requested_url;
        }
    }
    if let Ok(token) = env::var("EPIS_SERVER_TOKEN") {
        config.token = token.trim().to_owned();
    }
    if !is_allowed_url(&config.url) {
        config.url = LOCAL_SERVER_URL.to_owned();
        config.token.clear();
    }
    config
}

fn stored_server_config(app: &AppHandle) -> Option<ServerConfig> {
    let path = app.path().app_config_dir().ok()?.join("server.json");
    let contents = fs::read_to_string(path).ok()?;
    serde_json::from_str(&contents).ok()
}

fn local_server_port(url: &str) -> Option<u16> {
    let address = url
        .strip_prefix("ws://127.0.0.1:")
        .or_else(|| url.strip_prefix("ws://localhost:"))?;
    address.split('/').next()?.parse().ok()
}

fn local_server_is_ready(port: u16) -> bool {
    let address = SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), port);
    TcpStream::connect_timeout(&address, Duration::from_millis(120)).is_ok()
}

fn source_root(app: &AppHandle) -> Result<PathBuf, String> {
    if cfg!(debug_assertions) {
        Ok(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../src"))
    } else {
        app.path()
            .resource_dir()
            .map(|path| path.join("python"))
            .map_err(|error| error.to_string())
    }
}

fn python_candidates() -> Vec<(PathBuf, Vec<&'static str>)> {
    let mut candidates = Vec::new();
    if let Some(configured) = env::var_os("EPIS_PYTHON") {
        candidates.push((PathBuf::from(configured), Vec::new()));
    }
    if let Some(local_app_data) = env::var_os("LOCALAPPDATA") {
        let base = PathBuf::from(local_app_data)
            .join("Programs")
            .join("Python");
        for version in ["Python314", "Python313", "Python312", "Python311"] {
            candidates.push((base.join(version).join("python.exe"), Vec::new()));
        }
    }
    candidates.push((PathBuf::from("python.exe"), Vec::new()));
    candidates.push((PathBuf::from("py.exe"), vec!["-3"]));
    candidates
}

fn copy_safe_device_environment(command: &mut Command) {
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
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "EPIS_DEVICE_ID",
        "EPIS_DEVICE_NAME",
    ] {
        if let Some(value) = env::var_os(key) {
            command.env(key, value);
        }
    }
}

fn server_log(app: &AppHandle) -> Option<(std::fs::File, std::fs::File)> {
    let log_dir = app.path().app_log_dir().ok()?;
    fs::create_dir_all(&log_dir).ok()?;
    let log_path = log_dir.join("epis-server.log");
    let stdout = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)
        .ok()?;
    let stderr = OpenOptions::new()
        .create(true)
        .append(true)
        .open(log_path)
        .ok()?;
    Some((stdout, stderr))
}

use serde::{Deserialize, Serialize};
use std::collections::VecDeque;
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
use url::Url;

#[cfg(windows)]
use std::os::windows::process::CommandExt;

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

const LOCAL_SERVER_URL: &str = "ws://127.0.0.1:8000/ws";
const WATCHDOG_POLL: Duration = Duration::from_secs(1);
const WATCHDOG_STABLE_AFTER: Duration = Duration::from_secs(60);
const WATCHDOG_WINDOW: Duration = Duration::from_secs(60);
const WATCHDOG_CRASH_LIMIT: usize = 5;
const WATCHDOG_COOLDOWN: Duration = Duration::from_secs(60);
const EXTERNAL_AGENT_RETRY: Duration = Duration::from_secs(10);

#[derive(Clone, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ServerConfig {
    url: String,
    token: String,
}

#[derive(Deserialize)]
struct StoredServerConfig {
    url: String,
    #[serde(default)]
    token: String,
}

#[derive(Serialize)]
struct StoredServerConfigV2<'a> {
    url: &'a str,
}

enum AgentSpawn {
    Running(Child),
    AlreadyRunning,
}

pub struct LocalServer {
    child: Arc<Mutex<Option<Child>>>,
    stop_requested: Arc<AtomicBool>,
    watchdog: Mutex<Option<JoinHandle<()>>>,
}

impl Default for LocalServer {
    fn default() -> Self {
        Self {
            child: Arc::new(Mutex::new(None)),
            stop_requested: Arc::new(AtomicBool::new(false)),
            watchdog: Mutex::new(None),
        }
    }
}

#[tauri::command]
pub fn server_config(app: AppHandle) -> Result<ServerConfig, String> {
    resolved_server_config(&app)
}

impl LocalServer {
    pub fn start(&self, app: &AppHandle) -> Result<(), String> {
        self.stop_requested.store(false, Ordering::SeqCst);

        // local_server.py is a development/test escape hatch only. Release
        // builds are cloud-only and never silently fall back to localhost.
        if explicit_local_dev_mode() {
            return self.start_dev_local_server(app);
        }

        let config = resolved_server_config(app)?;
        validate_remote_server_url(&config.url)?;
        if config.token.trim().is_empty() {
            return Err("Remote EPIS server token is missing".to_owned());
        }

        {
            let guard = self
                .watchdog
                .lock()
                .map_err(|_| "watchdog lock poisoned")?;
            if guard.is_some() {
                return Ok(());
            }
        }

        match spawn_remote_device_agent(app, &config) {
            Ok(AgentSpawn::Running(child)) => {
                *self.child.lock().map_err(|_| "server lock poisoned")? = Some(child);
            }
            Ok(AgentSpawn::AlreadyRunning) => {
                // Keep the watchdog alive. It periodically retries; if the
                // external/older agent later exits, this process takes over.
            }
            Err(error) => {
                // Chat can still work without the Windows device agent. The
                // watchdog keeps retrying with backoff instead of making the
                // whole Desktop runtime fail at startup.
                eprintln!("EPIS device agent initial start failed: {error}");
            }
        }

        self.start_watchdog(app.clone(), config)
    }

    fn start_watchdog(&self, app: AppHandle, config: ServerConfig) -> Result<(), String> {
        let child_slot = Arc::clone(&self.child);
        let stop_requested = Arc::clone(&self.stop_requested);

        let handle = thread::Builder::new()
            .name("epis-device-agent-watchdog".to_owned())
            .spawn(move || {
                let mut restart_times: VecDeque<Instant> = VecDeque::new();
                let mut backoff = Duration::from_secs(1);
                let mut child_started_at = Instant::now();

                while !stop_requested.load(Ordering::SeqCst) {
                    let mut needs_spawn = false;
                    let mut unexpected_exit = false;

                    if let Ok(mut slot) = child_slot.lock() {
                        if let Some(child) = slot.as_mut() {
                            match child.try_wait() {
                                Ok(None) => {
                                    if child_started_at.elapsed() >= WATCHDOG_STABLE_AFTER {
                                        restart_times.clear();
                                        backoff = Duration::from_secs(1);
                                    }
                                }
                                Ok(Some(status)) => {
                                    eprintln!(
                                        "EPIS device agent exited unexpectedly: {status}"
                                    );
                                    *slot = None;
                                    needs_spawn = true;
                                    unexpected_exit = true;
                                }
                                Err(error) => {
                                    eprintln!(
                                        "EPIS device agent status check failed: {error}"
                                    );
                                    *slot = None;
                                    needs_spawn = true;
                                    unexpected_exit = true;
                                }
                            }
                        } else {
                            needs_spawn = true;
                        }
                    } else {
                        eprintln!("EPIS device-agent lock poisoned; watchdog stopping");
                        break;
                    }

                    if !needs_spawn {
                        sleep_interruptible(&stop_requested, WATCHDOG_POLL);
                        continue;
                    }

                    if unexpected_exit {
                        let now = Instant::now();
                        restart_times.push_back(now);
                        while restart_times
                            .front()
                            .is_some_and(|first| now.duration_since(*first) > WATCHDOG_WINDOW)
                        {
                            restart_times.pop_front();
                        }

                        if restart_times.len() >= WATCHDOG_CRASH_LIMIT {
                            eprintln!(
                                "EPIS device agent entered a crash loop; cooling down for {} seconds",
                                WATCHDOG_COOLDOWN.as_secs()
                            );
                            sleep_interruptible(&stop_requested, WATCHDOG_COOLDOWN);
                            restart_times.clear();
                            backoff = Duration::from_secs(1);
                            continue;
                        }

                        sleep_interruptible(&stop_requested, backoff);
                        backoff = std::cmp::min(backoff * 2, Duration::from_secs(30));
                        if stop_requested.load(Ordering::SeqCst) {
                            break;
                        }
                    }

                    match spawn_remote_device_agent(&app, &config) {
                        Ok(AgentSpawn::Running(child)) => {
                            if let Ok(mut slot) = child_slot.lock() {
                                *slot = Some(child);
                                child_started_at = Instant::now();
                            } else {
                                break;
                            }
                        }
                        Ok(AgentSpawn::AlreadyRunning) => {
                            // Another per-user instance owns the lock. Do not
                            // spin or count this as a crash; periodically retry.
                            sleep_interruptible(&stop_requested, EXTERNAL_AGENT_RETRY);
                        }
                        Err(error) => {
                            eprintln!("EPIS device agent restart failed: {error}");
                            let now = Instant::now();
                            restart_times.push_back(now);
                            while restart_times
                                .front()
                                .is_some_and(|first| now.duration_since(*first) > WATCHDOG_WINDOW)
                            {
                                restart_times.pop_front();
                            }
                            if restart_times.len() >= WATCHDOG_CRASH_LIMIT {
                                eprintln!(
                                    "EPIS device agent restart limit reached; cooling down"
                                );
                                sleep_interruptible(&stop_requested, WATCHDOG_COOLDOWN);
                                restart_times.clear();
                                backoff = Duration::from_secs(1);
                            } else {
                                sleep_interruptible(&stop_requested, backoff);
                                backoff = std::cmp::min(backoff * 2, Duration::from_secs(30));
                            }
                        }
                    }
                }
            })
            .map_err(|error| error.to_string())?;

        *self
            .watchdog
            .lock()
            .map_err(|_| "watchdog lock poisoned")? = Some(handle);
        Ok(())
    }

    fn start_dev_local_server(&self, app: &AppHandle) -> Result<(), String> {
        let port = local_server_port(LOCAL_SERVER_URL)
            .ok_or_else(|| "Invalid local development server URL".to_owned())?;
        if local_server_is_ready(port) {
            return Ok(());
        }

        let source_root = source_root(app)?;
        let script = source_root.join("server").join("local_server.py");
        if !script.is_file() {
            return Err(format!(
                "EPIS local development server entry point not found: {}",
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

            apply_log_stdio(&mut command, &log_stdio);

            #[cfg(windows)]
            command.creation_flags(CREATE_NO_WINDOW);

            match command.spawn() {
                Ok(mut child) => {
                    thread::sleep(Duration::from_millis(250));
                    match child.try_wait() {
                        Ok(None) => {
                            *self.child.lock().map_err(|_| "server lock poisoned")? = Some(child);
                            return Ok(());
                        }
                        Ok(Some(status)) => {
                            last_error = format!(
                                "{} exited during local development startup with {}",
                                program.display(),
                                status
                            );
                        }
                        Err(error) => last_error = error.to_string(),
                    }
                }
                Err(error) => last_error = format!("{}: {}", program.display(), error),
            }
        }
        Err(last_error)
    }

    pub fn stop(&self) {
        self.stop_requested.store(true, Ordering::SeqCst);

        if let Ok(mut slot) = self.child.lock() {
            if let Some(mut child) = slot.take() {
                let _ = child.kill();
                let _ = child.wait();
            }
        }

        if let Ok(mut watchdog) = self.watchdog.lock() {
            if let Some(handle) = watchdog.take() {
                let _ = handle.join();
            }
        }
    }
}

fn explicit_local_dev_mode() -> bool {
    cfg!(debug_assertions)
        && env::var("EPIS_DESKTOP_LOCAL_SERVER")
            .map(|value| matches!(value.trim().to_ascii_lowercase().as_str(), "1" | "true" | "yes"))
            .unwrap_or(false)
}

fn resolved_server_config(app: &AppHandle) -> Result<ServerConfig, String> {
    if explicit_local_dev_mode() {
        return Ok(ServerConfig {
            url: LOCAL_SERVER_URL.to_owned(),
            token: String::new(),
        });
    }

    let mut config = stored_server_config(app)?.unwrap_or(ServerConfig {
        url: String::new(),
        token: String::new(),
    });

    if let Ok(requested_url) = env::var("EPIS_SERVER_URL") {
        if !requested_url.trim().is_empty() {
            config.url = requested_url.trim().to_owned();
        }
    }
    if let Ok(token) = env::var("EPIS_SERVER_TOKEN") {
        if !token.trim().is_empty() {
            config.token = token.trim().to_owned();
        }
    }

    if config.url.trim().is_empty() {
        return Err(
            "EPIS cloud server is not configured. Set EPIS_SERVER_URL or server.json."
                .to_owned(),
        );
    }
    config.url = validate_remote_server_url(&config.url)?;

    if config.token.trim().is_empty() {
        return Err(
            "EPIS cloud server token is not configured. Set EPIS_SERVER_TOKEN or migrate server.json."
                .to_owned(),
        );
    }

    Ok(config)
}

fn validate_remote_server_url(value: &str) -> Result<String, String> {
    let mut parsed = Url::parse(value.trim())
        .map_err(|_| "EPIS cloud server URL is invalid".to_owned())?;
    if parsed.scheme() != "wss" {
        return Err("Production EPIS server must use wss://".to_owned());
    }
    if parsed.host_str().is_none() {
        return Err("EPIS cloud server URL must include a host".to_owned());
    }
    if !parsed.username().is_empty() || parsed.password().is_some() {
        return Err("EPIS cloud server URL must not contain credentials".to_owned());
    }
    if parsed.query().is_some() || parsed.fragment().is_some() {
        return Err("EPIS cloud server URL must not contain query/fragment data".to_owned());
    }
    if parsed.path() == "/" || parsed.path().is_empty() {
        parsed.set_path("/ws");
    } else if parsed.path() != "/ws" {
        return Err("EPIS cloud server URL must use /ws".to_owned());
    }
    Ok(parsed.to_string())
}

fn stored_server_config(app: &AppHandle) -> Result<Option<ServerConfig>, String> {
    let path = app
        .path()
        .app_config_dir()
        .map_err(|error| error.to_string())?
        .join("server.json");
    if !path.is_file() {
        return Ok(None);
    }

    let contents = fs::read_to_string(&path).map_err(|error| error.to_string())?;
    let stored: StoredServerConfig =
        serde_json::from_str(&contents).map_err(|error| format!("Invalid server.json: {error}"))?;

    let mut secure_token = load_secure_token(app)?;
    if !stored.token.trim().is_empty() {
        if secure_token.as_deref().unwrap_or("").trim().is_empty() {
            store_secure_token(app, stored.token.trim())?;
            secure_token = Some(stored.token.trim().to_owned());
        }
        strip_legacy_plaintext_token(&path, stored.url.trim())?;
    }

    Ok(Some(ServerConfig {
        url: stored.url.trim().to_owned(),
        token: secure_token.unwrap_or_default(),
    }))
}

fn strip_legacy_plaintext_token(path: &Path, url: &str) -> Result<(), String> {
    let clean = serde_json::to_vec_pretty(&StoredServerConfigV2 { url })
        .map_err(|error| error.to_string())?;
    // DPAPI storage is committed first. If this rewrite fails, the legacy
    // file stays recoverable instead of being deleted before a rename.
    fs::write(path, clean).map_err(|error| error.to_string())
}

fn secure_token_path(app: &AppHandle) -> Result<PathBuf, String> {
    let dir = app
        .path()
        .app_config_dir()
        .map_err(|error| error.to_string())?;
    fs::create_dir_all(&dir).map_err(|error| error.to_string())?;
    Ok(dir.join("server-token.dpapi"))
}

#[cfg(windows)]
fn store_secure_token(app: &AppHandle, token: &str) -> Result<(), String> {
    let path = secure_token_path(app)?;
    let mut command = powershell_command();
    command
        .env("EPIS_TOKEN_PLAIN", token)
        .env("EPIS_TOKEN_PATH", &path)
        .args([
            "-NoProfile",
            "-NonInteractive",
            "-WindowStyle",
            "Hidden",
            "-Command",
            "Add-Type -AssemblyName System.Security; $b=[Text.Encoding]::UTF8.GetBytes($env:EPIS_TOKEN_PLAIN); $p=[Security.Cryptography.ProtectedData]::Protect($b,$null,[Security.Cryptography.DataProtectionScope]::CurrentUser); [IO.File]::WriteAllBytes($env:EPIS_TOKEN_PATH,$p)",
        ])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    let status = command.status().map_err(|error| error.to_string())?;
    if !status.success() {
        return Err("Windows DPAPI could not protect the EPIS server token".to_owned());
    }
    Ok(())
}

#[cfg(not(windows))]
fn store_secure_token(_app: &AppHandle, _token: &str) -> Result<(), String> {
    Err("Secure persisted EPIS tokens are currently Windows-only".to_owned())
}

#[cfg(windows)]
fn load_secure_token(app: &AppHandle) -> Result<Option<String>, String> {
    let path = secure_token_path(app)?;
    if !path.is_file() {
        return Ok(None);
    }
    let mut command = powershell_command();
    command
        .env("EPIS_TOKEN_PATH", &path)
        .args([
            "-NoProfile",
            "-NonInteractive",
            "-WindowStyle",
            "Hidden",
            "-Command",
            "Add-Type -AssemblyName System.Security; $b=[IO.File]::ReadAllBytes($env:EPIS_TOKEN_PATH); $p=[Security.Cryptography.ProtectedData]::Unprotect($b,$null,[Security.Cryptography.DataProtectionScope]::CurrentUser); [Console]::Out.Write([Text.Encoding]::UTF8.GetString($p))",
        ])
        .stdin(Stdio::null())
        .stderr(Stdio::null());
    let output = command.output().map_err(|error| error.to_string())?;
    if !output.status.success() {
        return Err("Windows DPAPI could not unlock the EPIS server token".to_owned());
    }
    let token = String::from_utf8(output.stdout)
        .map_err(|_| "DPAPI token was not valid UTF-8".to_owned())?;
    if token.trim().is_empty() {
        return Err("DPAPI token store is empty".to_owned());
    }
    Ok(Some(token))
}

#[cfg(not(windows))]
fn load_secure_token(_app: &AppHandle) -> Result<Option<String>, String> {
    Ok(None)
}

#[cfg(windows)]
fn powershell_command() -> Command {
    let mut command = Command::new("powershell.exe");
    command.creation_flags(CREATE_NO_WINDOW);
    command
}

fn spawn_remote_device_agent(app: &AppHandle, config: &ServerConfig) -> Result<AgentSpawn, String> {
    validate_remote_server_url(&config.url)?;
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

        apply_log_stdio(&mut command, &log_stdio);

        #[cfg(windows)]
        command.creation_flags(CREATE_NO_WINDOW);

        match command.spawn() {
            Ok(mut child) => {
                thread::sleep(Duration::from_millis(350));
                match child.try_wait() {
                    Ok(None) => return Ok(AgentSpawn::Running(child)),
                    Ok(Some(status)) if status.code() == Some(73) => {
                        return Ok(AgentSpawn::AlreadyRunning)
                    }
                    Ok(Some(status)) => {
                        last_error = format!(
                            "{} exited during device-agent startup with {}",
                            program.display(),
                            status
                        );
                    }
                    Err(error) => last_error = error.to_string(),
                }
            }
            Err(error) => last_error = format!("{}: {}", program.display(), error),
        }
    }
    Err(last_error)
}

fn apply_log_stdio(command: &mut Command, log_stdio: &Option<(fs::File, fs::File)>) {
    match log_stdio {
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
}

fn sleep_interruptible(stop_requested: &AtomicBool, duration: Duration) {
    let deadline = Instant::now() + duration;
    while !stop_requested.load(Ordering::SeqCst) && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(200));
    }
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
        let base = PathBuf::from(local_app_data).join("Programs").join("Python");
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
        "EPIS_SPOTIFY_CLIENT_ID",
        "EPIS_SPOTIFY_REDIRECT_URI",
        "EPIS_KEY_PATH",
        "EPIS_MEMORY_VAULT_PATH",
    ] {
        if let Some(value) = env::var_os(key) {
            command.env(key, value);
        }
    }
}

fn server_log(app: &AppHandle) -> Option<(fs::File, fs::File)> {
    let log_dir = app.path().app_log_dir().ok()?;
    fs::create_dir_all(&log_dir).ok()?;
    let log_path = log_dir.join("epis-runtime.log");
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

#[cfg(test)]
mod tests {
    use super::validate_remote_server_url;

    #[test]
    fn remote_server_requires_wss() {
        assert!(validate_remote_server_url("ws://example.com/ws").is_err());
    }

    #[test]
    fn remote_server_normalizes_root_to_ws() {
        let value = validate_remote_server_url("wss://example.com").unwrap();
        assert_eq!(value, "wss://example.com/ws");
    }

    #[test]
    fn remote_server_rejects_credentials_query_and_wrong_path() {
        assert!(validate_remote_server_url("wss://user@example.com/ws").is_err());
        assert!(validate_remote_server_url("wss://example.com/ws?token=x").is_err());
        assert!(validate_remote_server_url("wss://example.com/other").is_err());
    }
}

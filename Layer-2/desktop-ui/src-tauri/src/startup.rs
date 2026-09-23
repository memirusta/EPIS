use serde::Serialize;
use std::env;
use std::process::{Command, Stdio};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

const RUN_KEY_PS: &str = r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Run";

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct StartupState {
    supported: bool,
    enabled: bool,
}

#[cfg(windows)]
fn powershell() -> Command {
    let mut command = Command::new("powershell.exe");
    command.creation_flags(CREATE_NO_WINDOW);
    command
}

#[cfg(windows)]
fn is_enabled() -> bool {
    let Ok(executable) = env::current_exe() else {
        return false;
    };

    let script = format!(
        "$p='{}'; try {{ \
         $v=[string](Get-ItemPropertyValue -Path $p -Name 'EPIS' -ErrorAction Stop); \
         if ([string]::IsNullOrWhiteSpace($v)) {{ exit 1 }}; \
         if ($v -match '^\\s*\"([^\"]+)\"') {{ $exe=$matches[1] }} \
         else {{ $exe=($v -split '\\s+')[0] }}; \
         if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {{ exit 1 }}; \
         $actual=[IO.Path]::GetFullPath($exe); \
         $expected=[IO.Path]::GetFullPath($env:EPIS_CURRENT_EXE); \
         if (-not [string]::Equals($actual,$expected,[StringComparison]::OrdinalIgnoreCase)) {{ exit 1 }}; \
         exit 0 \
         }} catch {{ exit 1 }}",
        RUN_KEY_PS,
    );

    powershell()
        .env("EPIS_CURRENT_EXE", executable)
        .args([
            "-NoProfile",
            "-NonInteractive",
            "-WindowStyle",
            "Hidden",
            "-Command",
            &script,
        ])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|status| status.success())
        .unwrap_or(false)
}

#[cfg(not(windows))]
fn is_enabled() -> bool {
    false
}

#[tauri::command]
pub fn startup_state() -> StartupState {
    StartupState {
        supported: cfg!(windows),
        enabled: is_enabled(),
    }
}

#[tauri::command]
pub fn set_startup_enabled(enabled: bool) -> Result<StartupState, String> {
    if !cfg!(windows) {
        return Err("Startup control is only supported on Windows".to_owned());
    }

    #[cfg(windows)]
    {
        let script;
        let mut command = powershell();

        if enabled {
            let executable = env::current_exe().map_err(|error| error.to_string())?;
            if !executable.is_file() {
                return Err("Current EPIS executable could not be verified".to_owned());
            }

            let startup_value = format!("\"{}\" --background", executable.display());
            command.env("EPIS_STARTUP_VALUE", startup_value);

            script = format!(
                "$p='{}'; \
                 New-Item -Path $p -Force | Out-Null; \
                 New-ItemProperty \
                   -Path $p \
                   -Name 'EPIS' \
                   -Value $env:EPIS_STARTUP_VALUE \
                   -PropertyType String \
                   -Force | Out-Null",
                RUN_KEY_PS,
            );
        } else {
            script = format!(
                "$p='{}'; \
                 Remove-ItemProperty \
                   -Path $p \
                   -Name 'EPIS' \
                   -ErrorAction SilentlyContinue",
                RUN_KEY_PS,
            );
        }

        let status = command
            .args([
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-Command",
                &script,
            ])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .map_err(|error| error.to_string())?;

        if !status.success() {
            return Err("Windows startup setting could not be changed".to_owned());
        }
    }

    Ok(startup_state())
}

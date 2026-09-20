param(
    [string]$Python = "python",
    [string]$EnvFile,
    [ValidateSet("stdio", "paired", "inprocess")][string]$Transport,
    [string]$PairingDir,
    [switch]$DebugLogs
)
$ErrorActionPreference = "Stop"
$episArgs = @("-X", "utf8", (Join-Path $PSScriptRoot "main.py"))
if ($EnvFile) { $episArgs += @("--env-file", $EnvFile) }
if ($Transport) { $episArgs += @("--device-transport", $Transport) }
if ($PairingDir) { $episArgs += @("--pairing-dir", $PairingDir) }
if ($DebugLogs) { $episArgs += "--debug" }
& $Python @episArgs
exit $LASTEXITCODE

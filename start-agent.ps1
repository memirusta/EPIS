param(
    [string]$Python = "python",
    [string]$EnvFile,
    [switch]$DebugLogs
)
$ErrorActionPreference = "Stop"
$episArgs = @("-X", "utf8", (Join-Path $PSScriptRoot "main.py"))
if ($EnvFile) { $episArgs += @("--env-file", $EnvFile) }
if ($DebugLogs) { $episArgs += "--debug" }
& $Python @episArgs
exit $LASTEXITCODE

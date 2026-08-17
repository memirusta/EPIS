# EPIS tek pencere: Ollama + Webhook + WhatsApp + Kairos (gizli) + Web UI (bu pencere)
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$Src  = Join-Path $Root "Layer-2\src"
$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

. (Join-Path $PSScriptRoot "load_keys_env.ps1") -Root $Root

if ($env:ADB_PATH -and (Test-Path $env:ADB_PATH)) {
    $adbDir = Split-Path $env:ADB_PATH -Parent
    if ($adbDir) { $env:Path = "$adbDir;$env:Path" }
}

$script:EpisBg = @()

function Start-EpisBg {
    param(
        [string]$Name,
        [string]$FilePath,
        [string[]]$ArgumentList,
        [string]$WorkingDirectory
    )
    $outLog = Join-Path $LogDir "$Name.out.log"
    $errLog = Join-Path $LogDir "$Name.err.log"
    $p = Start-Process -FilePath $FilePath `
        -ArgumentList $ArgumentList `
        -WorkingDirectory $WorkingDirectory `
        -WindowStyle Hidden `
        -PassThru `
        -RedirectStandardOutput $outLog `
        -RedirectStandardError $errLog
    $script:EpisBg += $p
    Write-Host ("  {0,-12} PID {1}  -> logs\{0}.*.log" -f $Name, $p.Id) -ForegroundColor Green
}

function Stop-EpisBg {
    foreach ($p in $script:EpisBg) {
        if ($null -eq $p) { continue }
        try {
            if (-not $p.HasExited) {
                Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
                # child process tree (npm -> node)
                Get-CimInstance Win32_Process -Filter "ParentProcessId=$($p.Id)" -ErrorAction SilentlyContinue |
                    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
            }
        } catch {}
    }
}

Write-Host ""
Write-Host "=== EPIS (tek pencere) ===" -ForegroundColor Cyan
Write-Host ""

Write-Host "[1/5] Ollama..." -ForegroundColor Yellow
& (Join-Path $PSScriptRoot "start_qwen.ps1")

Write-Host "[2/5] WhatsApp Webhook..." -ForegroundColor Yellow
Start-EpisBg -Name "webhook" -FilePath "python" -WorkingDirectory $Src -ArgumentList @(
    "-m", "uvicorn", "whatsapp_webhook:app", "--host", "0.0.0.0", "--port", "8000"
)
Start-Sleep -Seconds 2

Write-Host "[3/5] WhatsApp Bridge..." -ForegroundColor Yellow
$bridgeDir = Join-Path $Root "whatsapp_bridge"
if (-not (Test-Path (Join-Path $bridgeDir "node_modules"))) {
    Write-Host "  UYARI: whatsapp_bridge/node_modules yok - scripts\install_deps.ps1" -ForegroundColor Yellow
} else {
    if (-not $env:PYTHON_WEBHOOK_URL) { $env:PYTHON_WEBHOOK_URL = "http://localhost:8000/whatsapp/incoming" }
    if (-not $env:BRIDGE_PORT) { $env:BRIDGE_PORT = "3001" }
    Start-EpisBg -Name "whatsapp" -FilePath "npm.cmd" -WorkingDirectory $bridgeDir -ArgumentList @("start")
}
Start-Sleep -Seconds 2

Write-Host "[4/5] Kairos..." -ForegroundColor Yellow
Start-EpisBg -Name "kairos" -FilePath "python" -WorkingDirectory $Src -ArgumentList @("kairos.py")
Start-Sleep -Seconds 1

Write-Host "[5/5] Web UI (bu pencere)..." -ForegroundColor Yellow
Write-Host ""
$hostBind = if ($env:EPIS_UI_HOST) { $env:EPIS_UI_HOST } else { "127.0.0.1" }
Write-Host "  Web UI:    http://localhost:8080  (bind $hostBind)" -ForegroundColor Cyan
Write-Host "  Webhook:   http://localhost:8000"
Write-Host "  WA Bridge: http://localhost:3001/qr"
Write-Host "  Loglar:    $LogDir"
Write-Host "  Durdur:    Ctrl+C (arkadaki servisler de kapanir)" -ForegroundColor Yellow
Write-Host ""

Start-Process "http://localhost:8080"

try {
    Set-Location $Src
    python -m uvicorn epis_chat:app --host $hostBind --port 8080
} finally {
    Write-Host ""
    Write-Host "EPIS kapatiliyor..." -ForegroundColor Yellow
    Stop-EpisBg
    Write-Host "Bitti." -ForegroundColor Green
}

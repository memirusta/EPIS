# EPIS — Nightly Recalculation (manuel, bir kez veya test)
# RunPod Ollama: NIGHTLY_STAGE*_MODEL=qwen... + NIGHTLY_OLLAMA_BASE_URL
# Claude: NIGHTLY_STAGE*_MODEL=claude-... + CLAUDE_API_KEY
# Oneri: EPIS UI sekmesini kapat veya -FlushSession

param(
    [switch]$FlushSession
)

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
$Src  = Join-Path $Root "Layer-2\src"
$Keys = Join-Path $Root "Layer-3\keys.env"

Write-Host "=== EPIS Nightly Recalculation ===" -ForegroundColor Cyan

if (-not (Test-Path $Keys)) {
    Write-Host "HATA: keys.env yok: $Keys" -ForegroundColor Red
    exit 1
}

# keys.env yukle
Get-Content $Keys | ForEach-Object {
    if ($_ -match '^\s*([^#][^=]+)=(.*)$') {
        $name = $matches[1].Trim()
        $val  = $matches[2].Trim()
        [Environment]::SetEnvironmentVariable($name, $val, "Process")
    }
}

$stage1 = $env:NIGHTLY_STAGE1_MODEL
$stage2 = $env:NIGHTLY_STAGE2_MODEL
$ollama = $env:NIGHTLY_OLLAMA_BASE_URL
if (-not $ollama) { $ollama = $env:QWEN_BASE_URL }
$claude = $env:CLAUDE_API_KEY

$usesClaude = ($stage1 -match 'claude') -or ($stage2 -match 'claude')
$usesOllama = -not $usesClaude

if ($usesClaude -and (-not $claude -or $claude -match 'buraya|placeholder|your_')) {
    Write-Host "HATA: Claude nightly icin CLAUDE_API_KEY gerekli" -ForegroundColor Red
    exit 1
}
if ($usesOllama) {
    Write-Host "Mod: Ollama/RunPod" -ForegroundColor Green
    Write-Host "  Stage1/2: $stage1 / $stage2" -ForegroundColor Gray
    Write-Host "  URL: $ollama" -ForegroundColor Gray
} else {
    Write-Host "Mod: Claude API" -ForegroundColor Green
}

if ($FlushSession) {
    Write-Host "Canli oturum DB'ye yaziliyor (UI kapali olmali)..." -ForegroundColor Gray
    try {
        $headers = @{}
        $secret = $env:EPIS_UI_SHARED_SECRET
        if (-not $secret) { $secret = $env:WEBHOOK_SHARED_SECRET }
        if ($secret) { $headers["X-EPIS-UI-Secret"] = $secret }
        Invoke-RestMethod -Uri "http://localhost:8080/api/sessions/close" -Method POST -Headers $headers -TimeoutSec 5 | Out-Null
        Write-Host "  Oturum kaydedildi." -ForegroundColor Green
    } catch {
        Write-Host "  UI kapali veya erisilemiyor - session_buffer.json yeterli olabilir." -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "Calistiriliyor (birkac dakika surebilir)..." -ForegroundColor Gray
Push-Location $Src
python nightly_recalculation.py
$code = $LASTEXITCODE
Pop-Location

Write-Host ""
Write-Host "=== Olusacak / guncellenecek dosyalar ===" -ForegroundColor Cyan
@(
    "Layer-1\memory\morning_report.json",
    "Layer-1\memory\weekly.json",
    "Layer-1\identity\epis_self.json",
    "Layer-1\identity\identity_calculated.json",
    "Layer-1\habits\habit_log.json",
    "nightly.log"
) | ForEach-Object {
    $p = Join-Path $Root $_
    if (Test-Path $p) { Write-Host "  OK  $_" -ForegroundColor Green }
    else { Write-Host "  --  $_ (henuz yok)" -ForegroundColor DarkGray }
}

$mr = Join-Path $Root "Layer-1\memory\morning_report.json"
if (Test-Path $mr) {
    try {
        $j = Get-Content $mr -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($j.epis_voice) {
            Write-Host ""
            Write-Host "EPIS sesi (stage3):" -ForegroundColor Cyan
            Write-Host "  $($j.epis_voice)" -ForegroundColor White
        } elseif ($j.stages.stage3_epis) {
            Write-Host "Stage3: $($j.stages.stage3_epis)" -ForegroundColor Yellow
        }
    } catch {}
}

if ($code -ne 0) { exit $code }

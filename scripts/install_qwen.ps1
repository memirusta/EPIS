# EPIS -- Qwen (Ollama) kurulumu, Windows
# RTX 5060 8GB: qwen3.5:9b Q4 (~6.6GB VRAM, sikiyor; yavaslarsa qwen3.5:4b)

$ErrorActionPreference = "Stop"
$Model = if ($env:EPIS_QWEN_MODEL) { $env:EPIS_QWEN_MODEL } else { "qwen3.5:9b" }

Write-Host ""
Write-Host "EPIS Qwen Kurulumu (Ollama)" -ForegroundColor Cyan
Write-Host "============================" -ForegroundColor Cyan
Write-Host ""

function Test-Ollama {
    try {
        $r = Invoke-RestMethod -Uri "http://localhost:11434/api/tags" -TimeoutSec 5
        return $true
    } catch {
        return $false
    }
}

$ollama = Get-Command ollama -ErrorAction SilentlyContinue
if (-not $ollama) {
    Write-Host "Ollama bulunamadi. Winget ile kuruluyor..." -ForegroundColor Yellow
    winget install Ollama.Ollama --accept-package-agreements --accept-source-agreements
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path", "User")
    $ollama = Get-Command ollama -ErrorAction SilentlyContinue
    if (-not $ollama) {
        Write-Host "HATA: Ollama kuruldu ama PATH'te yok. PowerShell'i kapatip yeniden ac." -ForegroundColor Red
        exit 1
    }
}

if (-not (Test-Ollama)) {
    Write-Host "Ollama baslatiliyor (Windows tray uygulamasi)..." -ForegroundColor Yellow
    $ollamaApp = Join-Path $env:LOCALAPPDATA "Programs\Ollama\Ollama.exe"
    if (Test-Path $ollamaApp) {
        Start-Process $ollamaApp
    } else {
        Start-Process "ollama" -ArgumentList "serve" -WindowStyle Hidden
    }
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        if (Test-Ollama) { break }
        Start-Sleep -Seconds 2
    }
    if (-not (Test-Ollama)) {
        Write-Host "HATA: Ollama ayaga kalkmadi. Baslat menusunden 'Ollama'yi ac." -ForegroundColor Red
        exit 1
    }
}

Write-Host "Ollama calisiyor." -ForegroundColor Green
Write-Host "Model indiriliyor: $Model (~6-7 GB, biraz surer)..." -ForegroundColor Yellow
ollama pull $Model

Write-Host ""
Write-Host "Test istegi gonderiliyor..." -ForegroundColor Yellow
$body = @{
    model    = $Model
    messages = @(@{ role = "user"; content = "Merhaba, tek kelimeyle yanit ver: tamam" })
    stream   = $false
} | ConvertTo-Json -Depth 5

try {
    $resp = Invoke-RestMethod -Uri "http://localhost:11434/v1/chat/completions" `
        -Method Post -ContentType "application/json" -Body $body -TimeoutSec 120
    $text = $resp.choices[0].message.content
    Write-Host "Qwen yaniti: $text" -ForegroundColor Green
} catch {
    Write-Host "HATA: API testi basarisiz: $_" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "Kurulum tamam." -ForegroundColor Green
Write-Host "keys.env: LAYER1_BACKEND=qwen, QWEN_BASE_URL=http://localhost:11434/v1, QWEN_MODEL=$Model"
Write-Host "Sohbet: D:\EPIS\scripts\start_chat.ps1"
Write-Host ""

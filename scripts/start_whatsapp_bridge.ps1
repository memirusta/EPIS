$Root = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "load_keys_env.ps1") -Root $Root

Set-Location (Join-Path $Root "whatsapp_bridge")
if (-not (Test-Path "node_modules")) {
    Write-Host "Once: D:\EPIS\scripts\install_deps.ps1" -ForegroundColor Red
    exit 1
}

if (-not $env:MY_WHATSAPP_NUMBER) {
    Write-Host "UYARI: MY_WHATSAPP_NUMBER keys.env'de yok" -ForegroundColor Yellow
} else {
    Write-Host "kullanıcı numarasi (hedef/filtre): $($env:MY_WHATSAPP_NUMBER)" -ForegroundColor Cyan
}

$mode = if ($env:WHATSAPP_MODE) { $env:WHATSAPP_MODE } else { "epis_account" }
Write-Host "Mod: $mode" -ForegroundColor Cyan
if ($mode -eq "epis_account") {
    Write-Host "QR'yi EPIS telefonundan tara. Detay: scripts\kurulum_whatsapp_epis.txt" -ForegroundColor Yellow
}

$env:PYTHON_WEBHOOK_URL = if ($env:PYTHON_WEBHOOK_URL) { $env:PYTHON_WEBHOOK_URL } else { "http://localhost:8000/whatsapp/incoming" }
$env:BRIDGE_PORT        = if ($env:BRIDGE_PORT) { $env:BRIDGE_PORT } else { "3001" }

Write-Host "WhatsApp Bridge http://localhost:$($env:BRIDGE_PORT)" -ForegroundColor Green
Write-Host "Webhook: $($env:PYTHON_WEBHOOK_URL)" -ForegroundColor Green
Write-Host "Ilk kurulumda QR kodu tara (terminalde veya http://localhost:3001/qr)" -ForegroundColor Yellow
Write-Host ""

npm start

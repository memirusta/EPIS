# WhatsApp bridge + webhook saglik kontrolu

$Root = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "load_keys_env.ps1") -Root $Root

$bridge = $env:WHATSAPP_BRIDGE_URL
if (-not $bridge) { $bridge = "http://localhost:3001" }
$webhook = "http://localhost:8000/status"

Write-Host ""
Write-Host "WhatsApp durum kontrolu" -ForegroundColor Cyan
Write-Host "=======================" -ForegroundColor Cyan

try {
    $b = Invoke-RestMethod -Uri "$bridge/status" -TimeoutSec 5
    if ($b.ready) {
        Write-Host "Bridge: HAZIR" -ForegroundColor Green
        Write-Host "  EPIS hesap : $($b.number)" -ForegroundColor Gray
        Write-Host "  Mod        : $($b.mode)" -ForegroundColor Gray
        Write-Host "  kullanıcı no    : $($b.user)" -ForegroundColor Gray
        if ($b.mode -eq "epis_account" -and $b.number -eq $b.user) {
            Write-Host "  UYARI: EPIS ve kullanıcı ayni numara -- ayri SIM gerekli" -ForegroundColor Red
        }
    } else {
        Write-Host "Bridge: QR bekliyor - start_whatsapp_bridge.ps1 ac, telefondan tara" -ForegroundColor Yellow
    }
} catch {
    Write-Host "Bridge: KAPALI ($bridge)" -ForegroundColor Red
    Write-Host "  -> D:\EPIS\scripts\start_whatsapp_bridge.ps1" -ForegroundColor Yellow
}

try {
    $w = Invoke-RestMethod -Uri $webhook -TimeoutSec 5
    if ($w.epis_ready) {
        Write-Host "Webhook: HAZIR (backend=$($w.backend), model=$($w.model))" -ForegroundColor Green
    } else {
        Write-Host "Webhook: EPIS engine yok" -ForegroundColor Red
    }
} catch {
    Write-Host "Webhook: KAPALI (port 8000)" -ForegroundColor Red
    Write-Host "  -> D:\EPIS\scripts\start_webhook.ps1" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Sira: Ollama -> start_webhook.ps1 -> start_whatsapp_bridge.ps1 (QR) -> Kairos" -ForegroundColor Cyan
Write-Host ""

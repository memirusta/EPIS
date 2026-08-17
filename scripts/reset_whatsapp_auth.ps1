# WhatsApp bridge oturumunu sifirla (EPIS hesabina gecis icin)

$Root = Split-Path -Parent $PSScriptRoot
$Auth = Join-Path $Root "whatsapp_bridge\.wwebjs_auth"

Write-Host ""
Write-Host "WhatsApp oturum sifirlaniyor..." -ForegroundColor Yellow

if (Test-Path $Auth) {
    Remove-Item -Recurse -Force $Auth
    Write-Host "Silindi: $Auth" -ForegroundColor Green
} else {
    Write-Host "Oturum klasoru yok (zaten temiz)." -ForegroundColor Cyan
}

Write-Host ""
Write-Host "Simdi:" -ForegroundColor Yellow
Write-Host "  1) keys.env: WHATSAPP_MODE=epis_account"
Write-Host "  2) start_whatsapp_bridge.ps1"
Write-Host "  3) QR'yi EPIS telefonundan tara"
Write-Host "  4) Rehberde EPIS numarasini kaydet"
Write-Host ""
Write-Host "Detay: D:\EPIS\scripts\kurulum_whatsapp_epis.txt"
Write-Host ""

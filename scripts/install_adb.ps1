# Android Platform Tools (adb) kurulumu — winget

$ErrorActionPreference = "Stop"

if (Get-Command adb -ErrorAction SilentlyContinue) {
    Write-Host "adb zaten kurulu:" -ForegroundColor Green
    adb version
    exit 0
}

Write-Host "Android Platform Tools kuruluyor (winget)..." -ForegroundColor Cyan
winget install Google.PlatformTools --accept-package-agreements --accept-source-agreements

Write-Host ""
Write-Host "Kurulum tamam. Yeni bir PowerShell penceresi ac ve calistir:" -ForegroundColor Yellow
Write-Host "  adb version"
Write-Host "  adb devices"
Write-Host ""
Write-Host "Wi-Fi baglanti icin: scripts\kurulum_band_bildirim.txt"

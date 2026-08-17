$Root = Split-Path -Parent $PSScriptRoot
$Src  = Join-Path $Root "Layer-2\src"
. (Join-Path $PSScriptRoot "load_keys_env.ps1") -Root $Root

# adb PATH'te yoksa keys.env'deki ADB_PATH kullanilsin
if ($env:ADB_PATH -and (Test-Path $env:ADB_PATH)) {
    $adbDir = Split-Path $env:ADB_PATH -Parent
    if ($adbDir) { $env:Path = "$adbDir;$env:Path" }
}

Set-Location $Src

Write-Host ""
Write-Host "EPIS Web Arayuzu" -ForegroundColor Cyan
Write-Host "  http://localhost:8080 (sadece bu PC — 127.0.0.1)" -ForegroundColor Green
Write-Host "  (Ollama acik olmali)" -ForegroundColor Yellow
if ($env:BAND_NOTIFICATIONS -eq "true") {
    Write-Host "  Mi Band bildirim: acik" -ForegroundColor Green
}
Write-Host "  Kapatmak icin: once tarayici sekmesini kapat, sonra Ctrl+C" -ForegroundColor Yellow
Write-Host ""

$hostBind = if ($env:EPIS_UI_HOST) { $env:EPIS_UI_HOST } else { "127.0.0.1" }
python -m uvicorn epis_chat:app --host $hostBind --port 8080

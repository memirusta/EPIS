$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
Write-Host "Bagimliliklar kuruluyor..."
python -m pip install -r requirements.txt
if (Get-Command node -ErrorAction SilentlyContinue) {
    Set-Location (Join-Path $Root "whatsapp_bridge")
    if (-not (Test-Path "node_modules")) { npm install }
    Set-Location $Root
    Write-Host "Node/whatsapp_bridge: OK"
} else {
    Write-Host "Node yok -- WhatsApp bridge atlanir (nodejs.org'dan kur)" -ForegroundColor Yellow
}
Write-Host "Tamam."

$Root = Split-Path -Parent $PSScriptRoot
$Src  = Join-Path $Root "Layer-2\src"
. (Join-Path $PSScriptRoot "load_keys_env.ps1") -Root $Root
Set-Location $Src
Write-Host "WhatsApp Webhook http://0.0.0.0:8000"
if ($env:WEBHOOK_SHARED_SECRET) {
    Write-Host "Webhook auth: ACIK (shared secret)" -ForegroundColor Green
} else {
    Write-Host "Webhook auth: KAPALI - keys.env WEBHOOK_SHARED_SECRET ekle" -ForegroundColor Yellow
}
python -m uvicorn whatsapp_webhook:app --host 0.0.0.0 --port 8000

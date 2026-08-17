# Kairos proaktif mesaj testi - pending.json'a bildirim yazar.
# Sonra start_chat.ps1'i YENIDEN ac (acik sohbette gelmez).

param(
    [string]$Type = "morning",
    [string]$Context = "kullanıcı sabah uyandi. Test mesaji - Kairos calisiyor mu?",
    [string]$Priority = "medium"
)

$Root = Split-Path -Parent $PSScriptRoot
$Path = Join-Path $Root "Layer-1\memory\pending.json"

if (-not (Test-Path $Path)) {
    @{ items = @(); last_updated = (Get-Date -Format "o") } | ConvertTo-Json -Depth 5 | Set-Content $Path -Encoding UTF8
}

$data = Get-Content $Path -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $data.items) { $data | Add-Member -NotePropertyName items -NotePropertyValue @() }

$item = [ordered]@{
    id           = [guid]::NewGuid().ToString()
    created_at   = (Get-Date -Format "o")
    trigger_type = $Type
    context      = $Context
    priority     = $Priority
    status       = "pending"
}
$data.items += [pscustomobject]$item
$data.last_updated = (Get-Date -Format "o")
$data | ConvertTo-Json -Depth 6 | Set-Content $Path -Encoding UTF8

Write-Host ""
Write-Host "Pending eklendi: [$Priority] $Type" -ForegroundColor Green
Write-Host "  $Context"
Write-Host ""
Write-Host "Simdi sohbeti KAPAT (quit) ve yeniden ac:" -ForegroundColor Yellow
Write-Host "  D:\EPIS\scripts\start_chat.ps1"
Write-Host ""
if ($Priority -in @("high", "urgent")) {
    Write-Host "high/urgent: Kairos aciksa + WA bridge bagliysa telefona ham metin gider." -ForegroundColor Cyan
    Write-Host "EPIS sesi icin: webhook acikken kendine WA'dan mesaj at (30dk sessizlik sonrasi pending)." -ForegroundColor Cyan
}

# Qwen / Ollama saglik testi (keys.env'deki QWEN_* degerlerini okur)

$Root = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $Root "Layer-3\keys.env"

function Get-EnvVal($key) {
    if (-not (Test-Path $envFile)) { return $null }
    foreach ($line in Get-Content $envFile) {
        if ($line -match "^\s*$key\s*=\s*(.+)\s*$") {
            return $Matches[1].Trim()
        }
    }
    return $null
}

$base  = Get-EnvVal "QWEN_BASE_URL"
$model = Get-EnvVal "QWEN_MODEL"
if (-not $base)  { $base  = "http://localhost:11434/v1" }
if (-not $model) { $model = "qwen3.5:9b" }

$url = $base.TrimEnd("/") + "/chat/completions"
Write-Host "Qwen test: $url  model=$model" -ForegroundColor Cyan

$body = @{
    model    = $model
    messages = @(@{ role = "user"; content = "ping" })
    stream   = $false
    max_tokens = 16
} | ConvertTo-Json -Depth 5

try {
    $resp = Invoke-RestMethod -Uri $url -Method Post -ContentType "application/json" -Body $body -TimeoutSec 90
    Write-Host "OK: $($resp.choices[0].message.content)" -ForegroundColor Green
} catch {
    Write-Host "HATA: $_" -ForegroundColor Red
    Write-Host "Ollama acik mi? scripts\start_qwen.ps1 veya scripts\install_qwen.ps1" -ForegroundColor Yellow
    exit 1
}

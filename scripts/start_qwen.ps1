# Ollama durumunu kontrol et; kapaliysa baslat

function Test-Ollama {
    try {
        Invoke-RestMethod -Uri "http://localhost:11434/api/tags" -TimeoutSec 3 | Out-Null
        return $true
    } catch {
        return $false
    }
}

if (Test-Ollama) {
    Write-Host "Ollama zaten calisiyor (http://localhost:11434)" -ForegroundColor Green
    ollama list
    exit 0
}

Write-Host "Ollama baslatiliyor..." -ForegroundColor Yellow
$ollamaApp = Join-Path $env:LOCALAPPDATA "Programs\Ollama\Ollama.exe"
if (Test-Path $ollamaApp) {
    Start-Process $ollamaApp
} else {
    Start-Process "ollama" -ArgumentList "serve" -WindowStyle Hidden
}

$deadline = (Get-Date).AddSeconds(20)
while ((Get-Date) -lt $deadline) {
    if (Test-Ollama) {
        Write-Host "Ollama hazir." -ForegroundColor Green
        ollama list
        exit 0
    }
    Start-Sleep -Seconds 2
}

Write-Host "Ollama ayaga kalkmadi. Baslat menusunden 'Ollama' uygulamasini ac." -ForegroundColor Red
exit 1

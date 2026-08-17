# Layer-3/keys.env -> ortam degiskenleri (WhatsApp bridge icin)

param(
    [string]$Root = (Split-Path -Parent $PSScriptRoot)
)

$envFile = Join-Path $Root "Layer-3\keys.env"
if (-not (Test-Path $envFile)) {
    Write-Warning "keys.env bulunamadi: $envFile"
    return
}

Get-Content $envFile -Encoding UTF8 | ForEach-Object {
    $line = $_.Trim()
    if (-not $line -or $line.StartsWith("#")) { return }
    if ($line -match '^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
        $name = $Matches[1]
        $val  = $Matches[2].Trim()
        Set-Item -Path "env:$name" -Value $val
    }
}

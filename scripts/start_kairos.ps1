$Root = Split-Path -Parent $PSScriptRoot
$Src  = Join-Path $Root "Layer-2\src"
. (Join-Path $PSScriptRoot "load_keys_env.ps1") -Root $Root

if ($env:ADB_PATH -and (Test-Path $env:ADB_PATH)) {
    $adbDir = Split-Path $env:ADB_PATH -Parent
    if ($adbDir) { $env:Path = "$adbDir;$env:Path" }
}

Set-Location $Src
Write-Host "Kairos baslatiliyor (Ctrl+C ile durdur)..."
python kairos.py

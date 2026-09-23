param(
    [string]$Repo = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = "Stop"
Set-Location $Repo

function Assert-LastExit([string]$Message) {
    if ($LASTEXITCODE -ne 0) { throw $Message }
}

Write-Host "=== EPIS FINAL RELEASE CHECK ===" -ForegroundColor Cyan
Write-Host "Repo: $Repo"

Write-Host "`n[1/7] Python syntax" -ForegroundColor Cyan
python -m py_compile `
    Layer-2/src/server/app.py `
    Layer-2/src/epis_core.py `
    Layer-2/src/crypto_layer.py `
    Layer-2/src/agentic/path_grants.py
Assert-LastExit "Python syntax failed"

Write-Host "`n[2/7] Phase 6 focused tests" -ForegroundColor Cyan
python -m pytest tests/test_unified_runtime_phase6.py -q
Assert-LastExit "Phase 6 tests failed"

Write-Host "`n[3/7] Full pytest" -ForegroundColor Cyan
python -m pytest -q
Assert-LastExit "Full pytest failed"

Write-Host "`n[4/7] Desktop frontend build" -ForegroundColor Cyan
Push-Location Layer-2/desktop-ui
try {
    npm run build
    Assert-LastExit "Desktop npm build failed"
} finally {
    Pop-Location
}

Write-Host "`n[5/7] Tauri Rust tests/check" -ForegroundColor Cyan
Push-Location Layer-2/desktop-ui/src-tauri
try {
    cargo test
    Assert-LastExit "Tauri cargo test failed"
    cargo check
    Assert-LastExit "Tauri cargo check failed"
} finally {
    Pop-Location
}

Write-Host "`n[6/7] Mobile analyze" -ForegroundColor Cyan
if (Get-Command flutter -ErrorAction SilentlyContinue) {
    Push-Location Layer-2/mobile
    try {
        flutter analyze --no-fatal-infos --no-fatal-warnings
        Assert-LastExit "Flutter analyze failed"
    } finally {
        Pop-Location
    }
} else {
    Write-Host "[SKIP] flutter komutu PATH'te degil." -ForegroundColor Yellow
}

Write-Host "`n[7/7] Git whitespace + release hygiene" -ForegroundColor Cyan
git -c core.whitespace=cr-at-eol diff --check
Assert-LastExit "git diff --check failed"

$forbidden = @(
    "Layer-2/src/agentic/core.py.bak-session-read-20260920-202247",
    "Layer-2/src/agentic/core.py.bak-session-read-20260920-202948",
    "Layer-2/src/agentic/luna.py.bak-sol-output-20260920-203956",
    "Layer-2/src/agentic/file_tools.py.before-reparse-fix-20260923-115829.bak",
    "Layer-2/src/server/app.py.before-phase2-usage-compat-20260923-143117.bak",
    "patch_session_reads.py",
    "patch_session_reads_v2.py",
    "patch_sol_output.py"
)
foreach ($item in $forbidden) {
    if (Test-Path -LiteralPath $item) {
        throw "Release hygiene failed; stale artifact exists: $item"
    }
}

# Ignore Python bytecode/cache artifacts. py_compile embeds the absolute source
# path in .pyc files, so scanning __pycache__ creates a false D:\EPIS hit even
# when the actual product source is portable.
$hardcoded = Get-ChildItem Layer-2/src,Layer-2/desktop-ui/src,Layer-2/desktop-ui/src-tauri/src,Layer-2/mobile/lib -Recurse -File |
    Where-Object {
        $_.Extension -notin @('.pyc', '.pyo') -and
        $_.FullName -notmatch '[\\/]__pycache__[\\/]'
    } |
    Select-String -SimpleMatch 'D:\EPIS'
if ($hardcoded) {
    $hardcoded | ForEach-Object {
        Write-Host ("[HARDCODE] {0}:{1}: {2}" -f $_.Path, $_.LineNumber, $_.Line.Trim()) -ForegroundColor Red
    }
    throw "Product source still contains hard-coded D:\EPIS"
}

Write-Host "`n[OK] EPIS release checks passed." -ForegroundColor Green
Write-Host "Not: cloud hot-session files are process-local unless EPIS_CLOUD_RUNTIME_DIR is backed by durable storage." -ForegroundColor Yellow

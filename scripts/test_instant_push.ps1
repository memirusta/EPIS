# Aninda Kairos push testi (WA + Ollama + webhook bridge gerekir)
# idle: 1 saat sessizlik simule eder ve push tetikler

param(
    [ValidateSet("idle", "morning", "anomaly")]
    [string]$Type = "idle"
)

$Root = Split-Path -Parent $PSScriptRoot
$Src  = Join-Path $Root "Layer-2\src"

Write-Host ""
Write-Host "Aninda push testi: $Type" -ForegroundColor Cyan
Write-Host "Gereken: start_qwen + start_epis_ui (http://localhost:8080)" -ForegroundColor Yellow
Write-Host ""

$py = @"
import sys, os
from datetime import datetime, timedelta
sys.path.insert(0, r'$Src')
os.chdir(r'$Src')

from kairos import Kairos

k = Kairos()
t = '$Type'

if t == 'idle':
    k.memory.update_current_state({
        'last_message_at': (datetime.now() - timedelta(hours=2)).isoformat(),
        'session_active': False,
    })
    k._check_idle()
elif t == 'morning':
    k._trigger_morning()
elif t == 'anomaly':
    k._write_pending(
        'anomaly',
        'Test: nabiz son 15 dk 118 bpm.',
        'high',
    )
print('Tetik tamam -- telefona bak (20-30 sn).')
"@

python -c $py

# Telefon ekran izleme testi (ADB PhoneScreenMonitor)

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
$Src  = Join-Path $Root "Layer-2\src"
$Keys = Join-Path $Root "Layer-3\keys.env"

if (Test-Path $Keys) {
    Get-Content $Keys | ForEach-Object {
        if ($_ -match '^\s*([^#][^=]+)=(.*)$') {
            $k = $matches[1].Trim()
            $v = $matches[2].Trim()
            [Environment]::SetEnvironmentVariable($k, $v, 'Process')
        }
    }
}

if (-not $env:PHONE_MONITORING) {
    $env:PHONE_MONITORING = "true"
}

Push-Location $Src
try {
    python -c @"
from phone_monitor import PhoneScreenMonitor
import time

p = PhoneScreenMonitor()
print('ADB hazir:', p.is_available())
print('On plan:', p.get_foreground_package())
print('Ekran acik:', p._screen_on())

for _ in range(3):
    time.sleep(2)
    p.tick()

summary = p.get_daily_summary()
print()
print('Bugun telefon (dk):')
for k, v in summary.get('categories', {}).items():
    print(f'  {k}: {v}')
active = summary.get('active') or {}
if active:
    print('Son aktif:', active.get('label'), '(' + str(active.get('package')) + ')')
print()
print('Kayit:', r'$((Join-Path $Root "Layer-1\memory\phone_daily.json"))')
"@
} finally {
    Pop-Location
}

# Ekran izleme testi (Kairos ScreenMonitor)

$ErrorActionPreference = "Stop"
$Src = Join-Path (Split-Path $PSScriptRoot -Parent) "Layer-2\src"
Push-Location $Src
try {
    python -c @"
from sensors import ScreenMonitor
import json, time

s = ScreenMonitor()
print('pywin32:', s.is_available())
w = s.get_active_window()
print('Aktif pencere:', w.get('title'), '|', w.get('process'))
print('Dikkat kategorisi:', ScreenMonitor.distraction_from_window(w))

for _ in range(3):
    time.sleep(2)
    s.tick()

summary = s.get_daily_summary()
print()
print('Bugun (dk):')
for k, v in summary.get('categories', {}).items():
    print(f'  {k}: {v}')
print('Uygulamalar:', summary.get('by_app'))
print()
print('Kayit:', r'$((Join-Path (Split-Path $PSScriptRoot -Parent) "Layer-1\memory\screen_daily.json"))')
"@
} finally {
    Pop-Location
}

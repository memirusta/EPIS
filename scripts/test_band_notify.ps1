# Mi Band bildirim testi (Gadgetbridge ADB)

$ErrorActionPreference = "Stop"
. "$PSScriptRoot\load_keys_env.ps1"

$src = Join-Path (Split-Path $PSScriptRoot -Parent) "Layer-2\src"
Push-Location $src
try {
    python -c @"
from band_client import BandClient
c = BandClient()
print('enabled:', c.is_ready())
print('mode:', c.mode)
if c.is_ready():
    ok = c.notify('EPIS test bildirimi — kurulum tamam.', respect_quiet_hours=False)
    print('sent:', ok)
else:
    print('Hazir degil. keys.env: BAND_NOTIFICATIONS=true ve ADB_DEVICE ayarla.')
"@
} finally {
    Pop-Location
}

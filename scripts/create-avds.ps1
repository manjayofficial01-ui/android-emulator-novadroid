<#
.SYNOPSIS
  Create NovaDroid baseline AVD fleet (phone + tablet + desktop).
.DESCRIPTION
  Uses `android emulator create` profiles. Tunes each AVD to 4 CPU / 4GB RAM /
  gpu auto (API 37 floor + QEMU scaling research). Idempotent: skips existing.
#>
$ErrorActionPreference = "Continue"
Write-Host "== NovaDroid fleet setup ==" -ForegroundColor Cyan
$existing = (android emulator list 2>&1) -join "`n"
foreach ($p in @("medium_phone", "medium_tablet", "small_desktop")) {
  if ($existing -match $p) { Write-Host "exists (skip): $p" -ForegroundColor DarkGray; continue }
  Write-Host "creating: $p ..." -ForegroundColor Yellow
  android emulator create $p
}
Write-Host "`nAVDs:" -ForegroundColor Cyan
android emulator list
Write-Host "`nTuned configs live in $env:USERPROFILE\.android\avd\*.avd\config.ini" -ForegroundColor Green

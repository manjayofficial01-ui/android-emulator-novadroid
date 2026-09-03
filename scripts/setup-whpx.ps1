<#
.SYNOPSIS
  NovaDroid Windows setup: enables WHPX stack for hardware-accelerated emulation.
.DESCRIPTION
  Research basis: AEHD sunsets Dec 31 2026; WHPX is mandatory. Requires
  BIOS VT-x/AMD-V + Windows Hypervisor Platform + Virtual Machine Platform.
  Run as Administrator. Reboot after enabling.
#>
#Requires -RunAsAdministrator
$ErrorActionPreference = "Stop"
Write-Host "== NovaDroid WHPX setup ==" -ForegroundColor Cyan

$features = @("HypervisorPlatform", "VirtualMachinePlatform")
foreach ($f in $features) {
  $st = (Get-WindowsOptionalFeature -Online -FeatureName $f).State
  Write-Host "$f ... $st"
  if ($st -ne "Enabled") {
    Write-Host "Enabling $f ..." -ForegroundColor Yellow
    Enable-WindowsOptionalFeature -Online -FeatureName $f -All -NoRestart | Out-Null
  }
}
Write-Host "`nChecking emulator acceleration:" -ForegroundColor Cyan
$sdk = "$env:LOCALAPPDATA\Android\Sdk"
$check = Join-Path $sdk "emulator\emulator-check.exe"
if (Test-Path $check) { & $check accel } else { Write-Warning "emulator-check.exe not found at $check" }

Write-Host "`nBIOS check (VT-x/AMD-V):" -ForegroundColor Cyan
Get-CimInstance Win32_Processor | Select-Object Name, VirtualizationFirmwareEnabled | Format-List
Write-Host "`nDone. REBOOT if any feature was newly enabled, then run: python novadroid.py" -ForegroundColor Green

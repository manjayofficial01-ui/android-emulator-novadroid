<#
.SYNOPSIS
  Expose the local NovaDroid agent to the internet via a Cloudflare quick tunnel,
  so the Vercel-hosted dashboard can reach your PC's emulator.
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\expose-tunnel.ps1 -Port 8080
  # copy the https://xxx.trycloudflare.com URL into the dashboard's Backend bar,
  # together with the --token you started the agent with.
NOTES
  Quick-tunnel URLs are random and change on every restart — fine for personal
  use. For a stable URL, create a named tunnel: `cloudflared tunnel login`
  + `cloudflared tunnel create novadroid`, then route it once.
#>
param([int]$Port = 8080)
$ErrorActionPreference = "Stop"
$exe = "$env:TEMP\cloudflared.exe"
if (-not (Test-Path $exe)) {
  Write-Host "Downloading cloudflared ..." -ForegroundColor Yellow
  Invoke-WebRequest "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" -OutFile $exe -TimeoutSec 300
}
Write-Host "Agent must be running with a token first:" -ForegroundColor Cyan
Write-Host "  python novadroid.py --host 0.0.0.0 --port $Port --token <secret> --no-browser"
Write-Host ""
Write-Host "Starting tunnel for http://localhost:$Port ..." -ForegroundColor Green
& $exe tunnel --no-autoupdate --url "http://localhost:$Port"

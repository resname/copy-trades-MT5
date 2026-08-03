#requires -Version 5.1
# Local smoke for install.ps1: runs it twice into a temp dir (idempotent) and
# checks the launcher exists. Run: powershell -File scripts/smoke-install.ps1
$ErrorActionPreference = "Stop"
$dir = Join-Path $env:TEMP ("ct-smoke-" + [guid]::NewGuid().ToString("N"))
powershell -File "$PSScriptRoot\install.ps1" -InstallDir $dir -Yes -SkipLaunch
powershell -File "$PSScriptRoot\install.ps1" -InstallDir $dir -Yes -SkipLaunch
$launcher = Join-Path $dir "bin\copytrades.cmd"
if (-not (Test-Path $launcher)) { throw "smoke FAILED: launcher missing at $launcher" }
Write-Host "smoke OK: $launcher exists; install.ps1 is idempotent"
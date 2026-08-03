#requires -Version 5.1
<#
.SYNOPSIS
  Install or update the CopyTrades MT5 local manager (Claude-Code style).
.DESCRIPTION
  Idempotent: run once to install, run again to update. Ensures Python >=3.11
  (winget-first, python.org fallback), creates a venv, pip-installs the latest
  wheel from GitHub Releases (SHA256-verified), and creates a `copytrades`
  command on PATH + a Start Menu shortcut. On an update with the app running,
  prompts before stopping it (a live copy session would be interrupted).
.EXAMPLE
  irm https://github.com/resname/copy-trades-MT5/releases/latest/download/install.ps1 | iex
#>
[CmdletBinding()]
param(
  [string]$InstallDir = "$env:LOCALAPPDATA\CopyTradesMT5",
  [switch]$Yes,
  [switch]$SkipLaunch
)

$ErrorActionPreference = "Stop"
$Repo = "resname/copy-trades-MT5"
$Base = "https://github.com/$Repo/releases/latest/download"
$WheelUrl = "$Base/manager-latest.whl"
$ShaUrl = "$Base/manager-latest.whl.sha256"

function Resolve-Py {
  foreach ($c in @("python", "py")) {
    try {
      $v = & $c --version 2>$null
      if ($v -match "Python (\d+)\.(\d+)") {
        $maj = [int]$Matches[1]; $min = [int]$Matches[2]
        if ($maj -gt 3 -or ($maj -eq 3 -and $min -ge 11)) {
          return (Get-Command $c).Source
        }
      }
    } catch {}
  }
  return $null
}

# 1. Ensure Python >=3.11
$PyExe = Resolve-Py
if (-not $PyExe) {
  Write-Host "Python >=3.11 not found. Installing..."
  $winget = Get-Command winget -ErrorAction SilentlyContinue
  if ($winget) {
    winget install --id Python.Python.3.12 -e --silent `
      --accept-source-agreements --accept-package-agreements
  } else {
    $inst = "$env:TEMP\python-3.12.7-amd64.exe"
    Invoke-WebRequest "https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe" -OutFile $inst -UseBasicParsing
    Start-Process -FilePath $inst -ArgumentList "/quiet InstallAllUsers=0 PrependPath=1 Include_pip=1" -Wait
  }
  $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "User") + ";" + $env:Path
  $PyExe = Resolve-Py
  if (-not $PyExe) {
    throw "Python install failed. Install Python >=3.11 from https://www.python.org/downloads/ then re-run."
  }
}

# 2. venv
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$Venv = Join-Path $InstallDir "venv"
if (-not (Test-Path $Venv)) {
  Write-Host "Creating venv at $Venv"
  & $PyExe -m venv $Venv
}
$Pip = Join-Path $Venv "Scripts\pip.exe"
$PyVenv = Join-Path $Venv "Scripts\python.exe"

# 3. Stop a running app before reinstall (update safety)
$running = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
  Where-Object { $_.CommandLine -match "manager" -and $_.CommandLine -match ([regex]::Escape($Venv)) })
if ($running.Count -gt 0) {
  if (-not $Yes) {
    $choice = Read-Host "The app is running. Stop & update? A live copy session will be interrupted. [y/N]"
    if ($choice -notmatch "^[yY]") { Write-Host "Aborted."; return }
  }
  # Graceful-first: send WM_CLOSE to the process tree (taskkill /T, no /F) so
  # the manager's own controller.stop() + orderly quit runs. Then fall back to a
  # hard kill only for survivors after the graceful timeout — a hung app must
  # not block the update forever, but a live copy session should not be
  # terminated mid-trade if it can shut down cleanly.
  foreach ($p in $running) { try { taskkill /PID $p.ProcessId /T 2>$null | Out-Null } catch {} }
  for ($i = 0; $i -lt 30; $i++) {
    $still = $false
    foreach ($p in $running) { if (Get-Process -Id $p.ProcessId -ErrorAction SilentlyContinue) { $still = $true } }
    if (-not $still) { break }
    Start-Sleep -Milliseconds 500
  }
  # Force fallback for any process that did not exit gracefully within ~15s.
  foreach ($p in $running) {
    if (Get-Process -Id $p.ProcessId -ErrorAction SilentlyContinue) {
      try { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue } catch {}
    }
  }
}

# 4. Download + SHA256-verify wheel
$tmp = Join-Path $env:TEMP ("copytrades-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
$wheelPath = Join-Path $tmp "manager-latest.whl"
Invoke-WebRequest $WheelUrl -OutFile $wheelPath -UseBasicParsing
$expected = ((Invoke-WebRequest $ShaUrl -UseBasicParsing).Content.Trim() -split '\s+')[0]
$actual = (Get-FileHash $wheelPath -Algorithm SHA256).Hash.ToLower()
if ($actual -ne $expected.ToLower()) {
  Write-Error "Wheel checksum mismatch (expected $expected got $actual). Aborting; existing install untouched."
  return
}

# 5. Install/upgrade
Write-Host "Installing/upgrading..."
& $Pip install --upgrade --force-reinstall $wheelPath

# 6. Launcher + PATH
$Bin = Join-Path $InstallDir "bin"
New-Item -ItemType Directory -Force -Path $Bin | Out-Null
$Cmd = Join-Path $Bin "copytrades.cmd"
@"
@echo off
"$PyVenv" -m manager %*
"@ | Set-Content -Path $Cmd -Encoding ASCII
$userPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$Bin*") {
  [System.Environment]::SetEnvironmentVariable("Path", ($userPath.TrimEnd(';') + ";$Bin"), "User")
  $env:Path = "$env:Path;$Bin"
}

# 7. Start Menu shortcut
$Lnk = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\CopyTradesMT5.lnk"
try {
  $ws = New-Object -ComObject WScript.Shell
  $sc = $ws.CreateShortcut($Lnk)
  $sc.TargetPath = $PyVenv
  $sc.Arguments = "-m manager"
  $sc.WorkingDirectory = $InstallDir
  $sc.Description = "CopyTrades MT5 Local Manager"
  $sc.Save()
} catch { Write-Warning "Could not create Start Menu shortcut: $_" }

Write-Host "Installed. Run with: copytrades  (or Start Menu: CopyTradesMT5)"

if (-not $SkipLaunch) {
  Start-Process -FilePath $PyVenv -ArgumentList "-m", "manager" -WorkingDirectory $InstallDir
  Write-Host "Launched."
}
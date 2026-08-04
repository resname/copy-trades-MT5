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

function Test-Py {
  # $src = path to python.exe. Returns $src if it is usable for THIS app,
  # otherwise $null. Two requirements:
  #  - Python >= 3.11 (pyproject/python_requires floor).
  #  - x86_64 (win-amd64) ONLY. MetaTrader5 (a hard runtime dep) publishes
  #    only win_amd64 wheels, so an ARM64 Python cannot `pip install
  #    MetaTrader5`. On ARM64 Windows we must use x64 Python under emulation.
  #
  # The arch check uses sysconfig.get_platform() ("win-amd64" / "win-arm64"),
  # NOT platform.machine(): on ARM64 Windows, platform.machine() reports the
  # NATIVE OS arch ("ARM64") even for an x64-emulated process, so it cannot
  # tell x64-from-arm64 and would reject the x64 Python we actually need.
  # sysconfig.get_platform() reflects the interpreter's own build and
  # correctly returns "win-amd64" for x64 Python (native or emulated) and
  # "win-arm64" for a native ARM64 build. On a real x64 machine every Python
  # is win-amd64, so this is a no-op there.
  param([string]$src)
  if (-not $src -or -not (Test-Path $src)) { return $null }
  try {
    $info = & $src -c "import sys,sysconfig; v=sys.version_info; print('%d.%d %s'%(v[0],v[1],sysconfig.get_platform()))" 2>$null
    if ($info -match "(\d+)\.(\d+)\s+(.+)") {
      $maj = [int]$Matches[1]; $min = [int]$Matches[2]; $plat = $Matches[3].Trim()
      if (($maj -gt 3 -or ($maj -eq 3 -and $min -ge 11)) -and $plat -eq "win-amd64") {
        return $src
      }
    }
  } catch {}
  return $null
}

function Resolve-Py {
  # Returns the path to a REAL (non-Microsoft-Store), x86_64 (win-amd64)
  # Python >=3.11, or $null. See Test-Py for the win-amd64 rationale.
  #
  # The Microsoft Store Python — the App Execution Alias under
  # %LOCALAPPDATA%\Microsoft\WindowsApps — MUST be skipped: it is sandboxed
  # and redirects venv writes into a per-app LocalCache path that this
  # (non-Store) PowerShell process cannot see, so pip and the launcher
  # (built from the requested venv path) would be unusable.
  foreach ($c in @("python", "py")) {
    try {
      foreach ($cmd in (Get-Command $c -All -ErrorAction SilentlyContinue)) {
        $src = $cmd.Source
        if (-not $src) { continue }
        if ($src -like "*\Microsoft\WindowsApps\*") { continue }
        $r = Test-Py $src
        if ($r) { return $r }
      }
    } catch {}
  }
  # Fallback: known python.org / winget per-user install locations. PATH may
  # not be refreshed in this session yet after a just-run install. Test-Py
  # accepts only win-amd64, so a native-arm64 build (e.g. Python312-arm64)
  # is skipped and an x64 Python312 (if present) wins.
  foreach ($root in @("$env:LOCALAPPDATA\Programs\Python",
                      "C:\Program Files\Python",
                      "C:\Program Files (x86)\Python")) {
    if (Test-Path $root) {
      foreach ($d in (Get-ChildItem $root -Directory -ErrorAction SilentlyContinue |
                      Where-Object Name -match "^Python3\d+(-arm64)?$" |
                      Sort-Object Name)) {
        $cand = Join-Path $d.FullName "python.exe"
        $r = Test-Py $cand
        if ($r) { return $r }
      }
    }
  }
  return $null
}

# 1. Ensure Python >=3.11
$PyExe = Resolve-Py
if (-not $PyExe) {
  Write-Host "Python >=3.11 not found. Installing..."
  $winget = Get-Command winget -ErrorAction SilentlyContinue
  if ($winget) {
    # --override passes the python.org installer args directly so the new
    # Python is added to PATH (PrependPath=1) — a plain --silent winget
    # install leaves it off PATH, so Resolve-Py could not find it.
    #
    # --architecture x64 forces the x86_64 Python on every host. This is a
    # no-op on a real x64 machine, but on an ARM64 Windows machine winget
    # would otherwise install the arm64 Python — and MetaTrader5 ships only
    # win_amd64 wheels, so `pip install` of the manager would fail with
    # "Could not find a version that satisfies the requirement MetaTrader5".
    # x64 Python runs on ARM64 under emulation and is the only build the
    # manager's dependencies can install on.
    #
    # --force is required for the ARM64 case where an arm64 Python.Python.3.12
    # is ALREADY installed: without it winget reports "no available upgrade"
    # and installs nothing (no x64), leaving Resolve-Py empty. --force makes
    # winget install the x64 build alongside the arm64 one (to Python312,
    # coexisting with Python312-arm64). On a real x64 machine --force just
    # re-runs the same-version installer once, harmlessly.
    winget install --id Python.Python.3.12 -e `
      --accept-source-agreements --accept-package-agreements `
      --architecture x64 --force `
      --override "/quiet InstallAllUsers=0 PrependPath=1 Include_pip=1"
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "User") + ";" + $env:Path
    $PyExe = Resolve-Py
  }
  if (-not $PyExe) {
    # winget was absent. Fall back to the python.org x64 (win-amd64)
    # installer — the same one winget downloads. 3.12.10 (not an older patch)
    # so it is not refused as a "downgrade" if any 3.12 is already registered.
    # This path is best-effort for hosts without winget; with winget present
    # the --force --architecture x64 branch above handles every case.
    $inst = "$env:TEMP\python-3.12.10-amd64.exe"
    Invoke-WebRequest "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe" -OutFile $inst -UseBasicParsing
    Start-Process -FilePath $inst -ArgumentList "/quiet InstallAllUsers=0 PrependPath=1 Include_pip=1" -Wait
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "User") + ";" + $env:Path
    $PyExe = Resolve-Py
  }
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
$shaPath = Join-Path $tmp "manager-latest.whl.sha256"
Invoke-WebRequest $WheelUrl -OutFile $wheelPath -UseBasicParsing
Invoke-WebRequest $ShaUrl -OutFile $shaPath -UseBasicParsing
# Read the checksum from the file, not (Invoke-WebRequest).Content: GitHub
# Releases serves the .sha256 asset with Content-Type application/octet-stream,
# so .Content is a [byte[]], not a string, and .Trim() would throw. Get-Content
# -Raw always decodes to a string and tolerates a trailing newline or a
# "<hash>  filename" pair.
$expected = ((Get-Content -Raw $shaPath).Trim() -split '\s+')[0]
$actual = (Get-FileHash $wheelPath -Algorithm SHA256).Hash.ToLower()
if ($actual -ne $expected.ToLower()) {
  Write-Error "Wheel checksum mismatch (expected $expected got $actual). Aborting; existing install untouched."
  return
}

# 5. Install/upgrade
Write-Host "Installing/upgrading..."
# pip requires a PEP 427-valid wheel filename: {name}-{version}-{pythontag}-
# {abitag}-{platformtag}.whl. The release asset is renamed to a stable URL
# (manager-latest.whl) so the one-liner always points at the newest build,
# but that name is NOT a valid wheel — pip rejects it ("is not a valid wheel
# filename"). Reconstruct the original valid name from the wheel's own
# .dist-info directory (which is "<distname>-<version>.dist-info") and
# install that. The platform tag is py3-none-any: this is a pure-Python
# project (python -m build --wheel emits only that tag), so hardcoding it
# is correct; if the project ever gains compiled deps the whole stable-URL
# scheme changes anyway.
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::OpenRead($wheelPath)
# Wheel zips often store NO explicit directory entries — only files like
# "<base>.dist-info/METADATA". Match entries that CONTAIN ".dist-info/"
# (files inside the dist-info dir) rather than a trailing-slash dir entry.
$distInfoEntry = ($zip.Entries |
                  Where-Object { $_.FullName -match '\.dist-info/' } |
                  Select-Object -First 1).FullName
$zip.Dispose()
if (-not $distInfoEntry) { throw "Downloaded wheel has no .dist-info; cannot rename to a valid wheel filename." }
# e.g. "copy_trades_mt5_manager-0.1.3.dist-info/METADATA" -> base
# "copy_trades_mt5_manager-0.1.3" (the {name}-{version} part pip expects).
$distBase = ($distInfoEntry -split '\.dist-info/')[0]
$validWheel = Join-Path $tmp ("$distBase-py3-none-any.whl")
Move-Item $wheelPath $validWheel -Force
& $Pip install --upgrade --force-reinstall $validWheel

# 6. Launcher + PATH
$Bin = Join-Path $InstallDir "bin"
New-Item -ItemType Directory -Force -Path $Bin | Out-Null
$PyWenv = Join-Path $Venv "Scripts\pythonw.exe"
$Cmd = Join-Path $Bin "copytrades.cmd"
@"
@echo off
if "%~1"=="" (
  "$PyWenv" -m manager
) else (
  "$PyVenv" -m manager %*
)
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
  $sc.TargetPath = $PyWenv
  $sc.Arguments = "-m manager"
  $sc.WorkingDirectory = $InstallDir
  $sc.Description = "CopyTrades MT5 Local Manager"
  $sc.Save()
} catch { Write-Warning "Could not create Start Menu shortcut: $_" }

Write-Host "Installed. Run with: copytrades  (or Start Menu: CopyTradesMT5)"

if (-not $SkipLaunch) {
  Start-Process -FilePath $PyWenv -ArgumentList "-m", "manager" -WorkingDirectory $InstallDir
  Write-Host "Launched."
}
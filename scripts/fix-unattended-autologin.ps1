#requires -Version 5.1
<#
.SYNOPSIS
  Repair the CopyTrades MT5 unattended-boot setup created by v0.1.34.

.DESCRIPTION
  One-shot, elevated, idempotent. Fixes the two issues found in the field,
  without asking for or changing the Windows password:

    1. ForceAutoLogon=1 -> 0. ForceAutoLogon re-logs in after every logoff
       and can repeatedly kick RDP sessions on single-session Windows clients.
    2. The old CopyTradesMT5-Lock task ran rundll32 directly and therefore
       also locked RDP sessions. Replace it with a version that locks only the
       physical console (GetSystemMetrics(SM_REMOTESESSION)).

  AutoAdminLogon, DefaultUserName, DefaultPassword, DisableCAD and
  DisableAutomaticRestartSignOn are left unchanged. The Winlogon domain is
  corrected to the computer name for the current local account.

.PARAMETER RemoveLockTask
  Do not install the console lock; remove CopyTradesMT5-Lock instead.

.PARAMETER LockDelaySeconds
  Delay after logon before locking the console (default 5).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File fix-unattended-autologin.ps1
#>
[CmdletBinding()]
param(
  [switch]$RemoveLockTask,
  [int]$LockDelaySeconds = 5
)

$ErrorActionPreference = 'Stop'

function Test-IsAdmin {
  $principal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())
  return $principal.IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-IsAdmin)) {
  throw "Run this script from an elevated PowerShell (Run as administrator)."
}

$winlogon = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
$p = Get-ItemProperty -LiteralPath $winlogon
$userName = $env:USERNAME
$computerName = $env:COMPUTERNAME
$lockName = 'CopyTradesMT5-Lock'
$lockTask = Get-ScheduledTask -TaskName $lockName -ErrorAction SilentlyContinue
$autoConfigured = ($p.AutoAdminLogon -eq '1')

if (-not $autoConfigured -and -not $lockTask -and $p.ForceAutoLogon -ne '1') {
  Write-Host "No CopyTrades MT5 autologin setup found; nothing to fix."
  return
}

$changes = @()

# ---- 1. ForceAutoLogon must be OFF -----------------------------------------
if ($p.ForceAutoLogon -eq '1') {
  Set-ItemProperty -LiteralPath $winlogon -Name ForceAutoLogon -Value '0' -Type String
  $changes += 'ForceAutoLogon: 1 -> 0'
} elseif ($null -eq $p.ForceAutoLogon) {
  Set-ItemProperty -LiteralPath $winlogon -Name ForceAutoLogon -Value '0' -Type String
  $changes += 'ForceAutoLogon: set to 0'
} else {
  $changes += "ForceAutoLogon: already $($p.ForceAutoLogon)"
}

# ---- 2. Local account domain ----------------------------------------------
if ($autoConfigured -and $p.DefaultUserName -eq $userName `
    -and $p.DefaultDomainName -ne $computerName) {
  Set-ItemProperty -LiteralPath $winlogon -Name DefaultDomainName `
    -Value $computerName -Type String
  $changes += "DefaultDomainName: '$($p.DefaultDomainName)' -> '$computerName'"
} elseif ($autoConfigured -and $p.DefaultUserName -ne $userName) {
  Write-Warning "AutoAdminLogon is configured for '$($p.DefaultUserName)', not '$userName'; leaving its domain unchanged."
}

# ---- 3. Lock task: console only, never RDP --------------------------------
$marker = Join-Path $env:LOCALAPPDATA 'CopyTradesMT5\no-console-lock'
if ($RemoveLockTask) {
  if ($lockTask) {
    Unregister-ScheduledTask -TaskName $lockName -Confirm:$false
    $changes += "${lockName}: removed"
  } else {
    $changes += "${lockName}: already absent"
  }
  New-Item -ItemType File -Force -Path $marker | Out-Null
  $changes += 'no-console-lock marker written (the app will not re-add the lock)'
} elseif ($autoConfigured) {
  Remove-Item -LiteralPath $marker -Force -ErrorAction SilentlyContinue
  $lockDir = Join-Path $env:LOCALAPPDATA 'CopyTradesMT5\bin'
  New-Item -ItemType Directory -Force -Path $lockDir | Out-Null
  $lockHelper = Join-Path $lockDir 'lock-console-session.ps1'
  $lockHelperCode = @'
Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; public static class Desk { [DllImport("user32.dll")] public static extern int GetSystemMetrics(int nIndex); }'
$SM_REMOTESESSION = 0x1000
if ([Desk]::GetSystemMetrics($SM_REMOTESESSION) -eq 0) {
  rundll32.exe user32.dll,LockWorkStation
}
'@
  Set-Content -LiteralPath $lockHelper -Value $lockHelperCode -Encoding ASCII
  $currentUser = "$computerName\$userName"
  $action = New-ScheduledTaskAction `
    -Execute "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$lockHelper`""
  $trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
  try {
    $trigger.Delay = "PT${LockDelaySeconds}S"
  } catch {
    Write-Warning "Could not set a logon delay; the lock task will run immediately."
  }
  $principal = New-ScheduledTaskPrincipal -UserId $currentUser `
    -LogonType Interactive -RunLevel Limited
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew
  $task = New-ScheduledTask -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings
  Register-ScheduledTask -TaskName $lockName -InputObject $task -Force | Out-Null
  $changes += "${lockName}: console-only version registered"
} elseif ($lockTask) {
  # No auto-logon, but a leftover lock task: remove the old RDP-locking one.
  Unregister-ScheduledTask -TaskName $lockName -Confirm:$false
  $changes += "${lockName}: old task removed (auto-logon is not configured)"
} else {
  $changes += "${lockName}: skipped (auto-logon is not configured)"
}

Write-Host ""
Write-Host "Repair complete:"
$changes | ForEach-Object { Write-Host " - $_" }
if ($autoConfigured -and $null -eq $p.PSObject.Properties['DefaultPassword']) {
  Write-Warning "AutoAdminLogon=1 but DefaultPassword is missing; run enable-unattended-autologin.ps1 to store it."
}
Write-Host ""
Write-Host "Reboot to verify: auto-login at boot, console locks ${LockDelaySeconds}s after logon, RDP sessions stay unlocked."

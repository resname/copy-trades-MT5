#requires -Version 5.1
<#
.SYNOPSIS
  Configure unattended boot for the CopyTrades MT5 rig.

.DESCRIPTION
  Run this once from an elevated PowerShell. It:
    1. Ensures the current local user has a Windows password (prompts locally;
       the password is never written into this repo).
    2. Configures Winlogon auto-logon for that user (AutoAdminLogon,
       DefaultPassword, ForceAutoLogon, DisableCAD) and enables
       Automatic Restart Sign-On so a Windows Update reboot does not stop at
       the lock screen.
    3. Registers the CopyTradesMT5-Lock task, which locks the workstation a few
       seconds after every logon. The manager and MT5 keep running behind the
       lock screen; the desktop cannot be used without the password.

  The CopyTradesMT5 manager task itself is managed by the app's "Launch on
  Windows startup" checkbox (elevated scheduled task). Enable that checkbox
  after running this script.

.NOTES
  Auto-logon requires Windows to store the password (Winlogon DefaultPassword).
  That is convenient, not high-security: an administrator who can read the
  machine can recover it. Use a password dedicated to this trading PC, not one
  reused for other accounts. If you forget it, the built-in Administrator
  account is disabled, so keep the password somewhere safe.

  ForceAutoLogon is OFF by default. With it ON, Windows logs in again after
  every logoff, which can repeatedly kick RDP sessions on single-session
  Windows clients. Pass -ForceAutoLogon only if you really need re-login after
  a manual logoff.

  The CopyTradesMT5-Lock task locks only the physical console. RDP sessions
  are skipped (GetSystemMetrics(SM_REMOTESESSION)).
#>
[CmdletBinding()]
param(
  [switch]$SkipPasswordPrompt,
  [switch]$ForceAutoLogon,
  [int]$LockDelaySeconds = 5
)

$ErrorActionPreference = 'Stop'

function Test-IsAdmin {
  $principal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())
  return $principal.IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function ConvertTo-PlainText {
  param([Security.SecureString]$Secure)
  $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
  try {
    return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
  } finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
  }
}

if (-not (Test-IsAdmin)) {
  throw "Run this script from an elevated PowerShell (Run as administrator)."
}

$errorLog = Join-Path $env:TEMP 'copytrades-autologin-error.txt'
try {

$userName = $env:USERNAME
# This script configures the LOCAL account (Get-LocalUser below), so the
# Winlogon domain and the task principal use the computer name. $env:USERDOMAIN
# may be a domain on a domain-joined machine while the local account is
# separate; the computer name is correct for the local SAM account.
$domainName = $env:COMPUTERNAME
$currentUser = "$domainName\$userName"

try {
  $localUser = Get-LocalUser -Name $userName -ErrorAction Stop
} catch {
  throw "Local user '$userName' not found. This script supports local accounts only."
}

# ---- 1. Password -----------------------------------------------------------
$plain = $null
if (-not $SkipPasswordPrompt) {
  if ($localUser.PasswordRequired) {
    Add-Type -AssemblyName System.DirectoryServices.AccountManagement
    $context = New-Object System.DirectoryServices.AccountManagement.PrincipalContext(
      'Machine', $env:COMPUTERNAME)
    while ($null -eq $plain) {
      $secure = Read-Host -AsSecureString "Windows password for $currentUser (used for auto-login)"
      $candidate = ConvertTo-PlainText $secure
      if ($context.ValidateCredentials($userName, $candidate)) {
        $plain = $candidate
      } else {
        Write-Host "Invalid password, try again." -ForegroundColor Yellow
      }
    }
  } else {
    Write-Host "User '$currentUser' currently has no password."
    while ($null -eq $plain) {
      $first = Read-Host -AsSecureString "New Windows password (min. 8 characters)"
      $second = Read-Host -AsSecureString "Repeat the password"
      $plainFirst = ConvertTo-PlainText $first
      $plainSecond = ConvertTo-PlainText $second
      if ($plainFirst -cne $plainSecond) {
        Write-Host "The passwords do not match, try again." -ForegroundColor Yellow
        continue
      }
      if ($plainFirst.Length -lt 8) {
        Write-Host "Use at least 8 characters, try again." -ForegroundColor Yellow
        continue
      }
      Set-LocalUser -Name $userName -Password $first
      $plain = $plainFirst
      Write-Host "Password set for '$currentUser'."
    }
  }
} else {
  Write-Warning "SkipPasswordPrompt: DefaultPassword is left unchanged. Auto-logon only works if it was already configured."
}

# ---- 2. Auto-logon ---------------------------------------------------------
$winlogon = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
$forceAutoLogonValue = if ($ForceAutoLogon) { '1' } else { '0' }
Set-ItemProperty -LiteralPath $winlogon -Name AutoAdminLogon -Value '1' -Type String
Set-ItemProperty -LiteralPath $winlogon -Name DefaultUserName -Value $userName -Type String
Set-ItemProperty -LiteralPath $winlogon -Name DefaultDomainName -Value $domainName -Type String
Set-ItemProperty -LiteralPath $winlogon -Name ForceAutoLogon -Value $forceAutoLogonValue -Type String
Set-ItemProperty -LiteralPath $winlogon -Name DisableCAD -Value '1' -Type String
if ($null -ne $plain) {
  Set-ItemProperty -LiteralPath $winlogon -Name DefaultPassword -Value $plain -Type String
}
$plain = $null
$plainFirst = $null
$plainSecond = $null
[GC]::Collect()

$policy = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System'
if (-not (Test-Path -LiteralPath $policy)) {
  New-Item -Path $policy -Force | Out-Null
}
Set-ItemProperty -LiteralPath $policy -Name DisableAutomaticRestartSignOn -Value 0 -Type DWord

# ---- 3. Lock right after logon (console only) ------------------------------
$lockName = 'CopyTradesMT5-Lock'
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
$lockAction = New-ScheduledTaskAction `
  -Execute "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" `
  -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$lockHelper`""
$lockTrigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
try {
  $lockTrigger.Delay = "PT${LockDelaySeconds}S"
} catch {
  Write-Warning "Could not set a logon delay; the lock task will run immediately at logon."
}
$lockPrincipal = New-ScheduledTaskPrincipal -UserId $currentUser `
  -LogonType Interactive -RunLevel Limited
$lockSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
  -MultipleInstances IgnoreNew
$lockTask = New-ScheduledTask -Action $lockAction -Trigger $lockTrigger `
  -Principal $lockPrincipal -Settings $lockSettings
Register-ScheduledTask -TaskName $lockName -InputObject $lockTask -Force | Out-Null

# ---- Summary ---------------------------------------------------------------
$check = Get-ItemProperty -LiteralPath $winlogon
Write-Host ""
Write-Host "Auto-login: AutoAdminLogon=$($check.AutoAdminLogon) DefaultUserName=$($check.DefaultUserName) ForceAutoLogon=$($check.ForceAutoLogon)"
Write-Host "Lock task : $lockName ($((Get-ScheduledTask -TaskName $lockName).State)), console only, ${LockDelaySeconds}s after logon"
if (-not (Get-ScheduledTask -TaskName 'CopyTradesMT5' -ErrorAction SilentlyContinue)) {
  Write-Warning "The CopyTradesMT5 manager task is not registered yet. Enable 'Launch on Windows startup' in the app (one UAC prompt)."
}
Write-Host ""
Write-Host "Reboot to verify: Windows signs in automatically, the manager starts elevated, then the console locks (RDP sessions are not locked)."

} catch {
  $message = $_ | Out-String
  try { Set-Content -LiteralPath $errorLog -Value $message -Encoding UTF8 } catch {}
  Write-Host ""
  Write-Host "FEHLER: $($_.Exception.Message)" -ForegroundColor Red
  Write-Host "Details: $errorLog"
  Read-Host "Press Enter to close"
  exit 1
}

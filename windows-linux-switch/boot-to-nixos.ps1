<#
.SYNOPSIS
    Restart straight from Windows into NixOS - one time only - via the UEFI "BootNext" variable.

.DESCRIPTION
    Tells the UEFI firmware which boot entry to start on the NEXT boot only, then restarts.
    Under the hood this is nothing more than:

        bcdedit /set {fwbootmgr} bootsequence {GUID-of-the-NixOS-entry}
        shutdown /r

    Windows maps the firmware boot manager's "bootsequence" to the UEFI BootNext variable,
    so the firmware starts the NixOS entry exactly once. The permanent boot order
    (BootOrder / "displayorder") is never touched: the reboot after that one goes back to
    whatever the firmware's default is.

    Machine-specific values are NOT in this file. They live in a config file that is
    git-ignored (default: boot-to-nixos.psd1 next to this script). Create it by copying
    boot-to-nixos.example.psd1 and replacing the placeholders. Full manual: README.md.

.PARAMETER List
    Print the firmware boot entries Windows knows about (parsed "bcdedit /enum firmware")
    and exit. Use it to find the values for the config file. Changes nothing.

.PARAMETER DryRun
    Resolve the target entry and print exactly what would be executed. Changes nothing,
    does not restart.

.PARAMETER NoReboot
    Set BootNext but do not restart. The next restart or power-on boots the target entry.

.PARAMETER Delay
    Seconds to wait before restarting (0 = immediately). Overrides RebootDelay from the config.

.PARAMETER Config
    Path to the config file. Default: boot-to-nixos.psd1 in the folder of this script.

.PARAMETER PauseOnExit
    Internal. Added automatically when the script relaunches itself elevated, so that the new
    console window does not close before you can read the output.

.EXAMPLE
    .\boot-to-nixos.ps1 -List
    Show all firmware boot entries (to fill in the config file).

.EXAMPLE
    .\boot-to-nixos.ps1 -DryRun
    Check that the config resolves to exactly one entry, without changing anything.

.EXAMPLE
    .\boot-to-nixos.ps1
    Set BootNext to the configured entry and restart.

.EXAMPLE
    .\boot-to-nixos.ps1 -NoReboot
    Only arm BootNext; the next restart you trigger yourself boots NixOS.

.NOTES
    Requires: Windows booted in UEFI mode, administrator rights (the script asks for
    elevation itself via UAC), Windows PowerShell 5.1 or PowerShell 7+.
    Run it from a terminal (not from the PowerShell ISE).
#>
[CmdletBinding()]
param(
    [switch]$List,
    [switch]$DryRun,
    [switch]$NoReboot,
    [int]$Delay = -1,
    [string]$Config = '',
    [switch]$PauseOnExit
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

# ----------------------------------------------------------------------------
# Constants. Nothing machine-specific lives here - see the config file for that.
# ----------------------------------------------------------------------------
$ScriptTag         = 'boot-to-nixos'
$PlaceholderValue  = 'REPLACE_ME'
$DefaultConfigName = 'boot-to-nixos.psd1'
$ExampleConfigName = 'boot-to-nixos.example.psd1'
$GuidPattern       = '^\{[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\}$'

# ----------------------------------------------------------------------------
# Output helpers
# ----------------------------------------------------------------------------
function Write-Info {
    param([string]$Message)
    Write-Host "[$ScriptTag] $Message"
}

function Write-Warn {
    param([string]$Message)
    Write-Host "[$ScriptTag] WARNING: $Message" -ForegroundColor Yellow
}

function Finish {
    param([int]$ExitCode)
    if ($PauseOnExit) {
        Write-Host ''
        Write-Host 'Press Enter to close this window...'
        [void](Read-Host)
    }
    exit $ExitCode
}

function Fail {
    param([string]$Message)
    Write-Host "[$ScriptTag] ERROR: $Message" -ForegroundColor Red
    Finish 1
}

# ----------------------------------------------------------------------------
# Environment helpers
# ----------------------------------------------------------------------------
function Test-IsAdministrator {
    $identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-SystemExecutable {
    # Full path of a tool under %SystemRoot%\System32, avoiding WOW64 file-system
    # redirection when a 32-bit PowerShell runs on 64-bit Windows.
    param([string]$Name)
    $candidates = @()
    if ([Environment]::Is64BitOperatingSystem -and -not [Environment]::Is64BitProcess) {
        $candidates += (Join-Path $env:SystemRoot "Sysnative\$Name")
    }
    $candidates += (Join-Path $env:SystemRoot "System32\$Name")
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    Fail "$Name was not found on this system."
}

function Invoke-Native {
    # Runs a native executable and returns its exit code plus all output lines
    # (stdout and stderr merged). $ErrorActionPreference is relaxed around the call
    # because Windows PowerShell 5.1 would otherwise turn any stderr output of a
    # native command into a terminating error.
    param(
        [string]$FilePath,
        [string[]]$Arguments
    )
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $lines = @(& $FilePath @Arguments 2>&1 | ForEach-Object { [string]$_ })
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
    return [pscustomobject]@{ ExitCode = $exitCode; Lines = $lines }
}

# ----------------------------------------------------------------------------
# Config file
# ----------------------------------------------------------------------------
function Resolve-ConfigPath {
    param([string]$Requested)
    if ([string]::IsNullOrWhiteSpace($Requested)) {
        $folder = $PSScriptRoot
        if ([string]::IsNullOrEmpty($folder)) { $folder = (Get-Location).Path }
        return (Join-Path $folder $DefaultConfigName)
    }
    if ([System.IO.Path]::IsPathRooted($Requested)) { return $Requested }
    return (Join-Path (Get-Location).Path $Requested)
}

function Import-Config {
    # Reads the .psd1 config as DATA (Import-PowerShellDataFile never executes code),
    # validates it and returns a hashtable with TargetMatch, TargetGuid, RebootDelay.
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        Fail ("Config file not found: {0}`n  Copy '{1}' to '{2}' (same folder as the script) and replace the placeholders.`n  See README.md, section 'Configuration'." -f $Path, $ExampleConfigName, (Split-Path -Leaf $Path))
    }
    try {
        $data = Import-PowerShellDataFile -Path $Path
    }
    catch {
        Fail "Config file '$Path' could not be parsed: $($_.Exception.Message)"
    }
    if ($data -isnot [hashtable]) {
        Fail "Config file '$Path' must contain exactly one hashtable: @{ TargetMatch = '...'; TargetGuid = ''; RebootDelay = 5 }"
    }

    $settings = @{
        TargetMatch = ''
        TargetGuid  = ''
        RebootDelay = 5
    }
    foreach ($key in @($data.Keys)) {
        if (-not $settings.ContainsKey($key)) {
            Write-Warn "Unknown key '$key' in the config file is ignored."
            continue
        }
        $settings[$key] = $data[$key]
    }

    $settings.TargetMatch = ([string]$settings.TargetMatch).Trim()
    $settings.TargetGuid  = ([string]$settings.TargetGuid).Trim()

    if ($settings.TargetGuid -eq $PlaceholderValue) { $settings.TargetGuid = '' }
    if ($settings.TargetGuid -ne '' -and -not $settings.TargetGuid.StartsWith('{')) {
        $settings.TargetGuid = '{' + $settings.TargetGuid + '}'
    }
    if ($settings.TargetGuid -ne '' -and $settings.TargetGuid -notmatch $GuidPattern) {
        Fail "TargetGuid '$($settings.TargetGuid)' is not a GUID of the form {12345678-1234-1234-1234-123456789abc}."
    }
    if ($settings.TargetGuid -eq '' -and ($settings.TargetMatch -eq '' -or $settings.TargetMatch -eq $PlaceholderValue)) {
        Fail ("The config file still contains placeholders: {0}`n  Run '.\{1} -List', find the entry that boots NixOS and set TargetMatch (or TargetGuid).`n  See README.md, section 'Configuration'." -f $Path, (Split-Path -Leaf $PSCommandPath))
    }

    $delayValue = 0
    if (-not [int]::TryParse([string]$settings.RebootDelay, [ref]$delayValue) -or $delayValue -lt 0) {
        Fail "RebootDelay must be a whole number of seconds >= 0 (got '$($settings.RebootDelay)')."
    }
    $settings.RebootDelay = $delayValue
    return $settings
}

# ----------------------------------------------------------------------------
# Firmware boot entries ("bcdedit /enum firmware")
#
# Example of one block of that output (blocks are separated by blank lines):
#
#   Firmware Application (101fffff)
#   -------------------------------
#   identifier              {12345678-1234-1234-1234-123456789abc}
#   device                  partition=\Device\HarddiskVolume5
#   path                    \EFI\systemd\systemd-bootx64.efi
#   description             Linux Boot Manager
#
# The labels in the left column can be localized (e.g. "Bezeichner" on a German
# Windows), so only the VALUES are interpreted: the first value in braces is the
# identifier, and TargetMatch is compared against every other value.
# ----------------------------------------------------------------------------
function ConvertTo-FirmwareEntry {
    param([string[]]$BlockLines)

    $identifier = $null
    $fields = New-Object System.Collections.Generic.List[object]
    foreach ($line in $BlockLines) {
        if ($null -eq $identifier) {
            if ($line -match '^\S.*?(\{[^{}]+\})\s*$') {
                $identifier = $Matches[1]
            }
            continue
        }
        if ($line -match '^(\S+)\s{2,}(\S.*?)\s*$') {
            $fields.Add([pscustomobject]@{ Key = $Matches[1]; Value = $Matches[2] })
        }
    }
    if ($null -eq $identifier) { return $null }

    $description = $null
    foreach ($field in $fields) {
        if ($field.Key -ieq 'description') { $description = $field.Value; break }
    }
    if ($null -eq $description) {
        if ($fields.Count -gt 0) { $description = $fields[0].Value } else { $description = '(no description)' }
    }

    return [pscustomobject]@{
        Identifier  = $identifier
        Description = $description
        Fields      = $fields
        Raw         = $BlockLines
    }
}

function Get-FirmwareEntries {
    param([string]$BcdEditPath)

    $result = Invoke-Native -FilePath $BcdEditPath -Arguments @('/enum', 'firmware')
    if ($result.ExitCode -ne 0) {
        Fail ("'bcdedit /enum firmware' failed with exit code {0}:`n{1}" -f $result.ExitCode, ($result.Lines -join "`n"))
    }

    $entries = @()
    $block = New-Object System.Collections.Generic.List[string]
    foreach ($line in $result.Lines) {
        if ([string]::IsNullOrWhiteSpace($line)) {
            if ($block.Count -gt 0) {
                $entry = ConvertTo-FirmwareEntry -BlockLines $block.ToArray()
                if ($null -ne $entry) { $entries += $entry }
                $block.Clear()
            }
            continue
        }
        $block.Add($line.TrimEnd())
    }
    if ($block.Count -gt 0) {
        $entry = ConvertTo-FirmwareEntry -BlockLines $block.ToArray()
        if ($null -ne $entry) { $entries += $entry }
    }
    return ,$entries
}

function Show-FirmwareEntries {
    param([object[]]$Entries)

    Write-Host ''
    Write-Host 'Firmware boot entries known to Windows (bcdedit /enum firmware):'
    Write-Host ''
    foreach ($entry in $Entries) {
        if ($entry.Identifier -ieq '{fwbootmgr}') { continue }
        Write-Host ("  {0}" -f $entry.Identifier) -ForegroundColor Cyan
        foreach ($field in $entry.Fields) {
            Write-Host ("      {0,-14} {1}" -f $field.Key, $field.Value)
        }
        Write-Host ''
    }
    foreach ($entry in $Entries) {
        if ($entry.Identifier -ine '{fwbootmgr}') { continue }
        Write-Host '  Firmware boot manager ({fwbootmgr}): "displayorder" is the permanent boot order,'
        Write-Host '  "bootsequence" (only present when armed) is a pending one-time BootNext.'
        foreach ($line in $entry.Raw) { Write-Host ("      {0}" -f $line) }
        Write-Host ''
    }
    Write-Host '  Put the "description" of the NixOS entry into TargetMatch (or its GUID into TargetGuid).'
}

function Find-TargetEntries {
    param(
        [object[]]$Entries,
        [string]$TargetMatch,
        [string]$TargetGuid
    )
    $found = @()
    foreach ($entry in $Entries) {
        if ($entry.Identifier -ieq '{fwbootmgr}') { continue }
        if ($TargetGuid -ne '') {
            if ($entry.Identifier -ieq $TargetGuid) { $found += $entry }
            continue
        }
        foreach ($field in $entry.Fields) {
            if ($field.Value -like $TargetMatch) {
                $found += $entry
                break
            }
        }
    }
    return ,$found
}

# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
$configPath = Resolve-ConfigPath -Requested $Config

if (-not (Test-IsAdministrator)) {
    Write-Info 'Administrator rights are needed (bcdedit reads and writes the firmware boot entries). Asking for elevation...'
    $hostExe = (Get-Process -Id $PID).Path
    if (-not $hostExe -or ((Split-Path -Leaf $hostExe) -notin @('pwsh.exe', 'powershell.exe'))) {
        $hostExe = Get-SystemExecutable -Name 'WindowsPowerShell\v1.0\powershell.exe'
    }
    $argList = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass',
        '-File', ('"{0}"' -f $PSCommandPath),
        '-PauseOnExit',
        '-Config', ('"{0}"' -f $configPath)
    )
    if ($List)        { $argList += '-List' }
    if ($DryRun)      { $argList += '-DryRun' }
    if ($NoReboot)    { $argList += '-NoReboot' }
    if ($Delay -ge 0) { $argList += @('-Delay', [string]$Delay) }
    try {
        Start-Process -FilePath $hostExe -ArgumentList $argList -Verb RunAs | Out-Null
    }
    catch {
        Fail "Elevation was refused or failed: $($_.Exception.Message)"
    }
    Finish 0
}

$firmwareType = $env:firmware_type
if ($firmwareType -and $firmwareType -ieq 'Legacy') {
    Fail 'Windows is booted in legacy BIOS/CSM mode. UEFI BootNext only exists on UEFI systems.'
}
if (-not $firmwareType) {
    Write-Warn 'Could not determine the firmware type (FIRMWARE_TYPE is not set); assuming UEFI.'
}

$bcdedit = Get-SystemExecutable -Name 'bcdedit.exe'
$entries = Get-FirmwareEntries -BcdEditPath $bcdedit

if ($List) {
    Show-FirmwareEntries -Entries $entries
    Write-Host ''
    Write-Info "Config file that would be used: $configPath"
    Finish 0
}

$settings     = Import-Config -Path $configPath
$delaySeconds = $settings.RebootDelay
if ($Delay -ge 0) { $delaySeconds = $Delay }

$targets  = Find-TargetEntries -Entries $entries -TargetMatch $settings.TargetMatch -TargetGuid $settings.TargetGuid
$criteria = "TargetMatch = '$($settings.TargetMatch)'"
if ($settings.TargetGuid -ne '') { $criteria = "TargetGuid = $($settings.TargetGuid)" }

if ($targets.Count -eq 0) {
    Show-FirmwareEntries -Entries $entries
    Fail ("No firmware boot entry matches {0}.`n  Pick one of the entries above and update {1}." -f $criteria, $configPath)
}
if ($targets.Count -gt 1) {
    Show-FirmwareEntries -Entries $targets
    Fail ("{0} entries match {1} (listed above). Make the pattern more specific or set TargetGuid in {2}." -f $targets.Count, $criteria, $configPath)
}

$target = $targets[0]
Write-Info ("Target entry: {0}  {1}" -f $target.Identifier, $target.Description)

if ($DryRun) {
    Write-Info 'Dry run - nothing will be changed. The real run would execute:'
    Write-Host ("    bcdedit /set {{fwbootmgr}} bootsequence {0}" -f $target.Identifier)
    if ($NoReboot) {
        Write-Host '    (no restart because of -NoReboot)'
    }
    else {
        Write-Host ("    shutdown /r /t {0}" -f $delaySeconds)
    }
    Finish 0
}

$setResult = Invoke-Native -FilePath $bcdedit -Arguments @('/set', '{fwbootmgr}', 'bootsequence', $target.Identifier)
if ($setResult.ExitCode -ne 0) {
    Fail ("'bcdedit /set {{fwbootmgr}} bootsequence {0}' failed with exit code {1}:`n{2}" -f $target.Identifier, $setResult.ExitCode, ($setResult.Lines -join "`n"))
}

# Verify: the firmware boot manager object should now carry a "bootsequence" with our GUID.
$verify        = Invoke-Native -FilePath $bcdedit -Arguments @('/enum', '{fwbootmgr}')
$verified      = $false
$verifyPattern = '^bootsequence\s+' + [regex]::Escape($target.Identifier) + '\s*$'
foreach ($line in $verify.Lines) {
    if ($line -match $verifyPattern) { $verified = $true; break }
}
if ($verified) {
    Write-Info ("BootNext is armed: the next boot starts '{0}' once." -f $target.Description)
}
else {
    Write-Warn 'bcdedit reported success, but "bootsequence" could not be confirmed in "bcdedit /enum {fwbootmgr}". Current state:'
    foreach ($line in $verify.Lines) { Write-Host ("    {0}" -f $line) }
}

if ($NoReboot) {
    Write-Info 'Not restarting (-NoReboot). The next restart or power-on boots the target entry once.'
    Write-Info 'To disarm it instead:  bcdedit /deletevalue {fwbootmgr} bootsequence'
    Finish 0
}

$shutdown = Get-SystemExecutable -Name 'shutdown.exe'
Write-Info ("Restarting into '{0}' in {1} second(s). Open programs will be closed without saving." -f $target.Description, $delaySeconds)
if ($delaySeconds -gt 0) {
    Write-Info 'Abort with:  shutdown /a   then disarm with:  bcdedit /deletevalue {fwbootmgr} bootsequence'
}
$comment      = "${ScriptTag}: restarting into $($target.Description)"
$rebootResult = Invoke-Native -FilePath $shutdown -Arguments @('/r', '/t', [string]$delaySeconds, '/c', $comment)
if ($rebootResult.ExitCode -ne 0) {
    Fail ("shutdown.exe failed with exit code {0}:`n{1}`n  BootNext is still armed: restart manually, or disarm with:  bcdedit /deletevalue {{fwbootmgr}} bootsequence" -f $rebootResult.ExitCode, ($rebootResult.Lines -join "`n"))
}
Finish 0

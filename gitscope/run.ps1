# Run without Docker on Windows, the PowerShell twin of run.sh (needs Python 3.11+ and git on PATH).
# Works in Windows PowerShell 5.1 and PowerShell 7+. Examples:
#   .\run.ps1
#   .\run.ps1 -ReposRoot C:/Users/simon/code -Port 9000
#   $env:REPOS_ROOT = "C:/Users/simon/code"; .\run.ps1
[CmdletBinding()]  # a typo like -RepoRoot is an error instead of silently serving $HOME
param(
    [string]$ReposRoot = $(if ($env:REPOS_ROOT) { $env:REPOS_ROOT } else { $HOME }),
    [int]$Port = $(if ($env:PORT) { $env:PORT } else { 8080 })
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Fail([string]$Message) {
    Write-Host "run.ps1: $Message" -ForegroundColor Red
    exit 1
}

# First interpreter on PATH that really is Python 3.11+. `python` goes first so an activated venv wins, and every
# match is probed, not just the first, to get past the Microsoft Store stub that only prints an install hint.
function Find-Python {
    $ErrorActionPreference = "Continue"  # see Invoke-Python
    foreach ($candidate in @("python"), @("python3"), @("py", "-3")) {
        $prefix = @($candidate | Select-Object -Skip 1)
        foreach ($cmd in @(Get-Command $candidate[0] -CommandType Application -ErrorAction SilentlyContinue)) {
            try { $found = & $cmd.Source @prefix -c "import sys; print(sys.executable); sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>$null } catch { continue }
            if ($LASTEXITCODE -eq 0) { return @{ Exe = $cmd.Source; Args = $prefix; Path = "$found" } }
        }
    }
    return $null
}

# pip notices and uvicorn's log go to stderr. PowerShell 5.1 turns that into terminating errors under "Stop"
# as soon as the output is redirected (.\run.ps1 *> log.txt), so success is judged by the exit code instead.
function Invoke-Python {
    $ErrorActionPreference = "Continue"
    & $python.Exe @($python.Args) @args
}

$python = Find-Python
if (-not $python) { Fail "Python 3.11+ not found on PATH (tried python, python3, py -3)." }
if (-not (Get-Command git -CommandType Application -ErrorAction SilentlyContinue)) { Fail "git not found on PATH." }

# Absolute paths, because the server runs from backend/ and relative ones would resolve against that.
$root = Resolve-Path -LiteralPath $ReposRoot -ErrorAction SilentlyContinue
if (-not $root -or -not (Test-Path -LiteralPath $root.ProviderPath -PathType Container)) { Fail "REPOS_ROOT is not a folder: $ReposRoot" }
$ReposRoot = $root.ProviderPath
$dataDir = if ($env:DATA_DIR) { $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($env:DATA_DIR) } else { Join-Path $PSScriptRoot "data" }

# Unlike `export` in a child bash process, $env: changes outlive the script, so they are put back on the way out.
# Otherwise a second run with a different -ReposRoot would inherit the first run's REPOS_ROOT_HOST.
$envNames = "REPOS_ROOT", "REPOS_ROOT_HOST", "DATA_DIR"
$savedEnv = @{}
foreach ($name in $envNames) { $savedEnv[$name] = [Environment]::GetEnvironmentVariable($name) }

Push-Location -LiteralPath (Join-Path $PSScriptRoot "backend")
try {
    $env:REPOS_ROOT = $ReposRoot
    $env:REPOS_ROOT_HOST = if ($savedEnv["REPOS_ROOT_HOST"]) { $savedEnv["REPOS_ROOT_HOST"] } else { $ReposRoot }
    $env:DATA_DIR = $dataDir

    Write-Host "Python: $($python.Path)"  # requirements get installed into this one
    Invoke-Python -m pip install -q -r requirements.txt
    if ($LASTEXITCODE -ne 0) { Fail "pip install failed (exit code $LASTEXITCODE)." }

    Write-Host "gitscope on http://localhost:$Port  (repos root: $ReposRoot)"
    Invoke-Python -m uvicorn app:app --host 127.0.0.1 --port $Port --reload
    exit $LASTEXITCODE
}
finally {
    # Set-Item removes a variable for $null. [Environment]::SetEnvironmentVariable would get "" from PowerShell's
    # string binding, and .NET 9+ (pwsh 7.5+) keeps that as an empty variable, which e.g. breaks docker compose's .env.
    foreach ($name in $envNames) { Set-Item -LiteralPath "Env:$name" -Value $savedEnv[$name] }
    Pop-Location
}

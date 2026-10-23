# Run contract + MVP tests (Windows dev machine).
#
# NOTE: this machine's shell is Windows PowerShell 5.1, which reads BOM-less
# UTF-8 script files as ANSI. Keep this file ASCII-only.
#
#   powershell -File scripts/test.ps1
#   powershell -File scripts/test.ps1 -Store sqlite
#   powershell -File scripts/test.ps1 -Python "C:\path\to\python.exe"
param(
    [string]$Python = "",
    [string]$Store = "reference"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

function Invoke-Native {
    # PowerShell 5.1 turns a native command's stderr into ErrorRecords, which
    # becomes a terminating error under $ErrorActionPreference = Stop. Run the
    # child with Continue, capture every stream into a log, then replay it.
    param(
        [string]$Exe,
        [string[]]$Arguments,
        [string]$LogPath
    )
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Exe @Arguments *> $LogPath
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previous
    }
    if (Test-Path $LogPath) {
        # Replay only the tail; the full log stays on disk as evidence.
        Get-Content -Path $LogPath -Encoding UTF8 -Tail 25 | Write-Host
        Write-Host "(full log: $LogPath)" -ForegroundColor DarkGray
    }
    return $code
}

Push-Location $root
try {
    $env:PYTHONIOENCODING = "utf-8"

    if (-not $Python) {
        $cache = Join-Path $env:USERPROFILE ".dsh\skills\run-python\env-cache.json"
        if (Test-Path $cache) {
            try {
                $json = Get-Content $cache -Raw | ConvertFrom-Json
                if ($json.PSObject.Properties.Name -contains "sessions" -and $json.sessions) {
                    $active = $json.sessions."asistent-main"
                    if ($active -and ($json.PSObject.Properties.Name -contains "environments")) {
                        $envEntry = $json.environments.$active
                        if ($envEntry -and $envEntry.path) {
                            $candidate = Join-Path $envEntry.path "python.exe"
                            if (Test-Path $candidate) { $Python = $candidate }
                        }
                    }
                }
            }
            catch {
                Write-Host "env-cache lookup skipped: $($_.Exception.Message)" -ForegroundColor DarkYellow
            }
        }
    }
    if (-not $Python) {
        $cmd = Get-Command python -ErrorAction SilentlyContinue
        if ($cmd) { $Python = $cmd.Source }
    }
    if (-not $Python) { throw "No python interpreter found; pass -Python <path>" }

    Write-Host "== python: $Python" -ForegroundColor Cyan
    Invoke-Native -Exe $Python -Arguments @("--version") -LogPath (Join-Path $env:TEMP "asistent-python-version.log") | Out-Null

    Write-Host "== contract snapshot check (--check)" -ForegroundColor Cyan
    $checkCode = Invoke-Native -Exe $Python -Arguments @("scripts/gen_contract_snapshot.py", "--check") -LogPath (Join-Path $env:TEMP "asistent-contract-check.log")
    if ($checkCode -ne 0) { throw "contract snapshot is out of date" }

    Write-Host "== tests (ASISTENT_STORE=$Store)" -ForegroundColor Cyan
    $env:ASISTENT_STORE = $Store
    $testLog = Join-Path $env:TEMP "asistent-tests.log"
    $testCode = Invoke-Native -Exe $Python -Arguments @("-m", "unittest", "discover", "-s", "tests", "-t", ".", "-v") -LogPath $testLog
    if ($testCode -ne 0) { throw "tests failed (see $testLog)" }

    Write-Host "OK" -ForegroundColor Green
}
finally {
    Pop-Location
}

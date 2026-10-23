#Requires -Version 5.1
<#
.SYNOPSIS
    asistent publish script: gate on the secret scan, then push `main` (+ tags) to GitHub.

.DESCRIPTION
    Design basis:
      - SPEC.md section 21.6.5 (delivery evidence: what you publish must be what you verified)
      - docs/adr/ADR-0002 (repo history is the delivery record)
      - scripts/publish_scan.py (secret / internal-info scanner used as the gate)

    Hard behavior contract:
      1. Read-only on the working tree: this script never commits, never edits tracked files,
         never touches .git/config beyond adding/updating the ONE remote named by -RemoteName.
      2. The gate runs FIRST. If the scan reports BLOCK (real credentials), nothing is pushed.
      3. Only committed `main` and existing tags are pushed. Untracked/dirty files never travel.
      4. Private repository is the recommended target: the docs contain internal topology
         (LAN addresses, jump host, cloud IP, dev-machine user name). The scan reports this
         as REVIEW, not as a blocker, so the decision stays explicit and visible here.

    IMPORTANT (measured on Windows): this file is intentionally ASCII-ONLY.
    Windows PowerShell 5.1 decodes a BOM-less UTF-8 .ps1 as ANSI, which mangles non-ASCII
    literals and can even break the parser.

.PARAMETER RemoteUrl
    GitHub repository URL, e.g. https://github.com/<user>/asistent.git

.PARAMETER RemoteName
    Remote name to add/update. Default: origin.

.PARAMETER Branch
    Branch to push. Default: main.

.PARAMETER Python
    Python executable used to run scripts/publish_scan.py. Default: python.

.PARAMETER SkipTags
    Do not push tags.

.PARAMETER DryRun
    Run every check and print the exact git commands, but do not push.

.EXAMPLE
    pwsh scripts/publish-github.ps1 -RemoteUrl https://github.com/me/asistent.git

.EXAMPLE
    pwsh scripts/publish-github.ps1 -RemoteUrl https://github.com/me/asistent.git -DryRun
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$RemoteUrl,
    [string]$RemoteName = 'origin',
    [string]$Branch = 'main',
    [string]$Python = 'python',
    [switch]$SkipTags,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) '..')).Path
Set-Location $RepoRoot

function Write-Section([string]$Title) {
    Write-Host ''
    Write-Host ('=' * 72)
    Write-Host $Title
    Write-Host ('=' * 72)
}

# ---------------------------------------------------------------- 0/4 preflight
Write-Section '0/4 preflight'

if (-not (Test-Path (Join-Path $RepoRoot '.git'))) {
    throw "not a git repository: $RepoRoot"
}
$head = (& git rev-parse --short HEAD).Trim()
$subject = (& git log -1 --pretty=%s).Trim()
Write-Host "  repo     : $RepoRoot"
Write-Host "  HEAD     : $head  $subject"
Write-Host "  remote   : $RemoteName -> $RemoteUrl"
if ($DryRun) { Write-Host '  MODE     : DRY RUN (nothing will be pushed)' }

$dirty = (& git status --porcelain | Measure-Object).Count
if ($dirty -gt 0) {
    Write-Host "  [WARN] $dirty uncommitted/untracked entries - only committed $Branch is pushed"
}

# ---------------------------------------------------------------- 1/4 secret gate
Write-Section '1/4 secret / internal-info gate'

$scan = Join-Path $RepoRoot 'scripts/publish_scan.py'
$scanOut = & $Python $scan 2>&1
$scanRc = $LASTEXITCODE
$scanOut | ForEach-Object { "    $_" }
if ($scanRc -ne 0) {
    throw 'publish_scan.py reported BLOCK-level findings; refusing to push. Fix them first.'
}
Write-Host '  [OK] no real credentials in tracked files'

# ---------------------------------------------------------------- 2/4 remote
Write-Section "2/4 remote $RemoteName"

$existing = (& git remote) -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ }
if ($existing -contains $RemoteName) {
    $current = (& git remote get-url $RemoteName).Trim()
    if ($current -ne $RemoteUrl) {
        Write-Host "  updating $RemoteName : $current -> $RemoteUrl"
        & git remote set-url $RemoteName $RemoteUrl
    }
    else {
        Write-Host "  $RemoteName already points at $RemoteUrl"
    }
}
else {
    Write-Host "  adding $RemoteName -> $RemoteUrl"
    & git remote add $RemoteName $RemoteUrl
}
if ($LASTEXITCODE -ne 0) { throw "git remote setup failed" }

# ---------------------------------------------------------------- 3/4 push
Write-Section "3/4 push $Branch"

$hasUpstream = $false
try {
    & git rev-parse --abbrev-ref "$Branch@{upstream}" 2>$null | Out-Null
    $hasUpstream = ($LASTEXITCODE -eq 0)
}
catch { $hasUpstream = $false }

if ($DryRun) {
    Write-Host "  would run: git push -u $RemoteName $Branch"
}
else {
    & git push -u $RemoteName $Branch
    if ($LASTEXITCODE -ne 0) { throw "git push failed (auth? repo exists? branch protection?)" }
}

if (-not $SkipTags) {
    $tags = @(& git tag)
    Write-Host ("  tags found: {0}" -f $tags.Count)
    if ($DryRun) {
        Write-Host "  would run: git push $RemoteName --tags"
    }
    else {
        if ($tags.Count -gt 0) {
            & git push $RemoteName --tags
            if ($LASTEXITCODE -ne 0) { Write-Host '  [WARN] tag push failed (tags are optional for rollback naming)' }
        }
    }
}

# ---------------------------------------------------------------- 4/4 verify
Write-Section '4/4 verify'

if ($DryRun) {
    Write-Host '  dry run: verification skipped'
}
else {
    $remoteHead = (& git ls-remote $RemoteName "refs/heads/$Branch" 2>&1)
    if ($LASTEXITCODE -ne 0) {
        Write-Host '  [WARN] could not read back from the remote (network?); local push reported success'
    }
    else {
        Write-Host "  remote $Branch -> $remoteHead"
        $localHead = (& git rev-parse HEAD).Trim()
        if ($remoteHead -match [regex]::Escape($localHead)) {
            Write-Host '  [OK] remote HEAD matches local HEAD'
        }
        else {
            Write-Host '  [WARN] remote HEAD does not match local HEAD'
        }
    }
}

Write-Section 'RESULT'
Write-Host "  published: $RemoteUrl"
Write-Host "  commit   : $head"

#Requires -Version 5.1
<#
.SYNOPSIS
    asistent deploy script: push the current dev-machine `main` to the device
    (board:~/code/asistent), then run read-only verification on the device
    (HEAD / tree / branch / worktree cleanliness / file count / contract-test
    runnability) and print the device Git HEAD.

.DESCRIPTION
    Design basis:
      - docs/adr/ADR-0002-dev-and-deploy-topology.md (device is the authoritative target;
        push updates device history AND worktree in one step)
      - SPEC.md section 14.1 (target dir ~/code/asistent), section 18 (environment gate),
        section 21.6.5 / 21.6.8 (never copy a final directory; record deployed Git HEAD)

    Hard behavior contract:
      1. The dev-machine repository is only read (status / rev-parse / ls-files /
         hash-object). The ONLY write action is `git push <remote>:<path> <branch>`.
         This script never creates a commit, never touches branches, never edits .git config.
      2. All device-side commands are read-only. No software is installed, no sudo is used.
      3. Run-data lives OUTSIDE the repo at ~/asistent-var (never overwritten by push) and is
         only created when -InitVarDir is passed.
      4. Remote commands are piped into `bash -s` via stdin (no quoting games), and every
         remote script body is normalized to LF first. CRLF breaks bash heredocs.

    IMPORTANT (measured on Windows): this file is intentionally ASCII-ONLY.
    Windows PowerShell 5.1 decodes a BOM-less UTF-8 .ps1 as ANSI, which mangles non-ASCII
    literals and can even break the parser. ASCII keeps the script valid on both
    Windows PowerShell 5.1 and PowerShell 7. Chinese explanations live in docs/env/*.md.

.PARAMETER Remote
    SSH alias. Default: board.

.PARAMETER RepoRoot
    Dev-machine repository root. Default: parent of the script directory.

.PARAMETER Branch
    Branch to deploy. Default: main.

.PARAMETER RemotePath
    Device-side repo path relative to HOME. Default: code/asistent.

.PARAMETER VarDir
    Device-side run-data directory (outside the repo). Default: ~/asistent-var.

.PARAMETER VerifyOnly
    Skip the push; verify the device as-is.

.PARAMETER InitVarDir
    Create the run-data directory skeleton on the device (idempotent).

.PARAMETER SkipTestProbe
    Skip the device-side contract-test probe.

.EXAMPLE
    powershell -File scripts/deploy.ps1
    Push, verify, print the device HEAD.

.EXAMPLE
    powershell -File scripts/deploy.ps1 -VerifyOnly
    Verify the current device state without pushing.

.NOTES
    Exit codes: 0 = all checks passed
                1 = preflight or push failure
                2 = device-side verification mismatch
#>
[CmdletBinding()]
param(
    [string]$Remote         = 'board',
    [string]$RepoRoot       = (Split-Path -Parent $PSScriptRoot),
    [string]$Branch         = 'main',
    [string]$RemotePath     = 'code/asistent',
    [string]$VarDir         = '~/asistent-var',
    [switch]$VerifyOnly,
    [switch]$InitVarDir,
    [switch]$SkipTestProbe
)

# Deliberately 'Continue': native commands (ssh/git) writing to stderr produce PowerShell
# error records, and 'Stop' would abort before we can inspect the real exit code.
$ErrorActionPreference = 'Continue'
$ProgressPreference    = 'SilentlyContinue'

$SshConnectTimeout = 15
$failPush          = 1
$failVerify        = 2

function Write-Section {
    param([string]$Text)
    Write-Host ''
    Write-Host ("=" * 72) -ForegroundColor DarkGray
    Write-Host $Text -ForegroundColor Cyan
    Write-Host ("=" * 72) -ForegroundColor DarkGray
}

function Write-Ok    { param([string]$t) Write-Host "  [OK]   $t" -ForegroundColor Green }
function Write-Warn2 { param([string]$t) Write-Host "  [WARN] $t" -ForegroundColor Yellow }
function Write-Err2  { param([string]$t) Write-Host "  [FAIL] $t" -ForegroundColor Red }
function Write-Info  { param([string]$t) Write-Host "  $t" }

function ConvertTo-Lf {
    # Two measured Windows traps when piping a here-string into `ssh host "bash -s"`:
    #   1. CRLF inside the body breaks remote heredoc terminators (<<'PY' stops matching),
    #      so the body is normalized to LF.
    #   2. The transport appends a lone CR to the FINAL line. If that line is a command,
    #      bash sees e.g. `exit 0<CR>` -> "numeric argument required"; a trailing newline
    #      only moves the problem to a bare `<CR>` line -> "command not found".
    # Therefore every remote script is terminated by a comment line, so the stray CR is
    # swallowed by the comment regardless of how it is appended.
    param([string]$Text)
    $t = (($Text -replace "`r`n", "`n") -replace "`r", "").TrimEnd("`n")
    return ($t + "`n# __END__")
}

function Invoke-Native {
    param(
        [Parameter(Mandatory)][string]$Exe,
        [string[]]$Arguments = @(),
        [string]$StdinText
    )
    $out = $null
    if ($PSBoundParameters.ContainsKey('StdinText')) {
        $out = (ConvertTo-Lf $StdinText) | & $Exe @Arguments 2>&1
    } else {
        $out = & $Exe @Arguments 2>&1
    }
    $code = $LASTEXITCODE
    # TrimEnd CR: captured stdout may carry \r from the transport; never let it leak into values.
    $lines = @($out | ForEach-Object { ("$_").TrimEnd([char]13) })
    return [pscustomobject]@{ Output = $lines; ExitCode = $code }
}

function Invoke-Remote {
    param([Parameter(Mandatory)][string]$Script)
    $sshArgs = @('-o', 'BatchMode=yes', '-o', "ConnectTimeout=$SshConnectTimeout", $Remote, 'bash -s 2>&1')
    return (Invoke-Native -Exe 'ssh' -Arguments $sshArgs -StdinText $Script)
}

function Invoke-RemoteChecked {
    param(
        [Parameter(Mandatory)][string]$Script,
        [string]$What = 'remote command',
        [switch]$TolerateFailure
    )
    $r = Invoke-Remote -Script $Script
    if ($r.ExitCode -ne 0 -and -not $TolerateFailure) {
        Write-Err2 "$What failed (ssh exit=$($r.ExitCode))"
        $r.Output | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkRed }
        return $null
    }
    return $r
}

function Invoke-GitLocal {
    param([string[]]$Arguments)
    return (Invoke-Native -Exe 'git' -Arguments (@('-C', $RepoRoot) + $Arguments))
}

function Get-KeyValue {
    param([string[]]$Lines, [string]$Key)
    $hit = $Lines | Where-Object { $_ -match "^$Key=" } | Select-Object -First 1
    if ($null -eq $hit) { return '' }
    return (($hit -replace "^$Key=", '')).Trim()
}

function Get-HashMap {
    param([string[]]$Lines)
    $map = @{}
    $in = $false
    foreach ($line in $Lines) {
        if ($line -match '^HASHES_BEGIN') { $in = $true;  continue }
        if ($line -match '^HASHES_END')   { $in = $false; continue }
        if ($in -and $line.Trim().Length -gt 0) {
            $parts = $line -split '\s+', 2
            if ($parts.Count -eq 2) { $map[$parts[1].Trim()] = $parts[0].Trim() }
        }
    }
    return $map
}

# ---------------------------------------------------------------- 0. Preflight
Write-Section '0/6 Preflight'

if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) { Write-Err2 'ssh not found'; exit $failPush }
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Write-Err2 'git not found'; exit $failPush }

$gitProbe = Invoke-GitLocal @('rev-parse', '--git-dir')
if ($gitProbe.ExitCode -ne 0) {
    Write-Err2 "RepoRoot is not a git repository: $RepoRoot"
    $gitProbe.Output | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkRed }
    exit $failPush
}
Write-Ok "dev repository: $RepoRoot"

$branchProbe = Invoke-GitLocal @('rev-parse', '--verify', '--quiet', "refs/heads/$Branch")
if ($branchProbe.ExitCode -ne 0) {
    Write-Err2 "local branch not found: $Branch"
    exit $failPush
}
$devHead    = ($branchProbe.Output[0]).Trim()
$devTree    = ((Invoke-GitLocal @('rev-parse', 'HEAD^{tree}')).Output[0]).Trim()
$devSubject = ((Invoke-GitLocal @('log', '-1', '--format=%s', $Branch)).Output[0]).Trim()
Write-Info "local $Branch  = $devHead"
Write-Info "local tree     = $devTree"
Write-Info "commit subject = $devSubject"

# Uncommitted work is NOT deployed: say so loudly. This script never commits for you.
$statusProbe = Invoke-GitLocal @('status', '--porcelain', '--untracked-files=all')
$dirtyLines  = @($statusProbe.Output | Where-Object { $_.Trim().Length -gt 0 })
if ($dirtyLines.Count -gt 0) {
    Write-Warn2 "dev worktree has $($dirtyLines.Count) uncommitted/untracked entries - NOT deployed (only committed $Branch is pushed)"
    $dirtyLines | Select-Object -First 10 | ForEach-Object { Write-Host "      $_" -ForegroundColor DarkYellow }
} else {
    Write-Ok 'dev worktree clean'
}

# Reachability. BatchMode: never prompt for a password.
$reach = Invoke-RemoteChecked -What "ssh connectivity ($Remote)" -Script @'
set -e
echo "remote_user=$(whoami)"
echo "remote_host=$(hostname)"
echo "remote_uname=$(uname -m) $(uname -r)"
echo "remote_date=$(date -Is)"
echo "remote_python=$(python3 -V 2>&1)"
'@
if ($null -eq $reach) {
    Write-Err2 "cannot connect to $Remote in BatchMode. Check ~/.ssh/config and the key."
    exit $failPush
}
$reach.Output | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
Write-Ok "device reachable ($Remote)"

# Device repo must exist. NOTE: single-quoted here-string is mandatory here; inside a
# double-quoted here-string "$HOME" would be expanded by PowerShell into the Windows path.
$repoProbeScript = @'
if [ -d "$HOME/__REMOTE_PATH__/.git" ]; then
  echo "state=repo"
  echo "repo_abs=$(cd "$HOME/__REMOTE_PATH__" && pwd)"
else
  echo "state=missing"
fi
exit 0
'@
$repoProbeScript = $repoProbeScript.Replace('__REMOTE_PATH__', $RemotePath)
$repoProbe = Invoke-RemoteChecked -What 'device repo check' -TolerateFailure -Script $repoProbeScript
if ($null -eq $repoProbe) { Write-Err2 'device repo check failed'; exit $failPush }
if (($repoProbe.Output -join "`n") -notmatch 'state=repo') {
    Write-Err2 "device path ~/$RemotePath is not a git repository yet - run the one-time init in docs/env/deploy.md"
    exit $failPush
}
$repoAbs = Get-KeyValue -Lines $repoProbe.Output -Key 'repo_abs'
Write-Ok "device repo present: $repoAbs"

if ($VerifyOnly) { Write-Warn2 'VerifyOnly: push skipped' }

# ---------------------------------------------------------------- 1. Push
if (-not $VerifyOnly) {
    Write-Section "1/6 push $Branch -> ${Remote}:$RemotePath"
    $pushArgs = @('-C', $RepoRoot, 'push', "${Remote}:${RemotePath}", $Branch)
    $push = Invoke-Native -Exe 'git' -Arguments $pushArgs
    $push.Output | ForEach-Object { Write-Host "    $_" }
    if ($push.ExitCode -ne 0) {
        Write-Err2 "push failed (git exit=$($push.ExitCode))"
        if (($push.Output -join "`n") -match 'would be overwritten|Working directory|not clean|denyCurrentBranch|updateInstead') {
            Write-Warn2 'device worktree is not clean: receive.denyCurrentBranch=updateInstead refuses to update it.'
            Write-Warn2 'Fix: ssh board; cd ~/code/asistent; git status  -> decide what the local change is.'
            Write-Warn2 'Never force-push, never discard device-local changes blindly.'
        }
        exit $failPush
    }

    # Contract freeze tags are pushed too, as a separate non-fatal step.
    #
    # Why: the device repository is the authoritative target, and a rollback drill
    # needs to say WHICH frozen revision it rolled back to. Without the tags the
    # device can only see commit SHAs, and an AT-17 style drill silently falls
    # back to "some old commit" (observed: it picked the repo skeleton, which has
    # no tests at all, and reported the rollback target as unusable).
    # Tag pushes never move a branch and never touch the worktree, so a failure
    # here cannot damage the deployed checkout - it is reported as a WARN only.
    $tagNames = @(git -C $RepoRoot tag --list 'contracts-*')
    if ($tagNames.Count -gt 0) {
        $tagPush = Invoke-Native -Exe 'git' -Arguments @('-C', $RepoRoot, 'push', "${Remote}:${RemotePath}", '--tags')
        $tagPush.Output | ForEach-Object { Write-Host "    $_" }
        if ($tagPush.ExitCode -ne 0) {
            Write-Warn2 "contract tag push failed (git exit=$($tagPush.ExitCode)) - rollback drills cannot name frozen revisions on the device"
        } else {
            Write-Ok "pushed $($tagNames.Count) contract freeze tag(s) so rollback targets are nameable on the device"
        }
    }
} else {
    Write-Section '1/6 push - skipped (-VerifyOnly)'
}

# ---------------------------------------------------------------- 2. Device read-only verification
Write-Section '2/6 device read-only verification'

$verifyScript = @'
set -e
cd "$HOME/__REMOTE_PATH__"
echo "HEAD=$(git rev-parse HEAD)"
echo "TREE=$(git rev-parse 'HEAD^{tree}')"
echo "BRANCH=$(git rev-parse --abbrev-ref HEAD)"
echo "SUBJECT=$(git log -1 --format=%s)"
echo "COMMIT_DATE=$(git log -1 --format=%cI)"
echo "TRACKED=$(git ls-files | wc -l)"
echo "WORKTREE_FILES=$(find . -path ./.git -prune -o -type f -print | wc -l)"
echo "UNTRACKED=$(git ls-files --others --exclude-standard | wc -l)"
echo "IGNORED=$(git ls-files --others --ignored --exclude-standard | wc -l)"
echo "DIRTY_COUNT=$(git status --porcelain -uall | wc -l)"
echo "COMMIT_COUNT=$(git rev-list --count HEAD)"
echo "RECEIVE_DENY=$(git config receive.denyCurrentBranch || echo unset)"
echo "HASHES_BEGIN"
git ls-files | while IFS= read -r f; do
  printf '%s  %s\n' "$(git hash-object "$f")" "$f"
done
echo "HASHES_END"
'@
$verifyScript = $verifyScript.Replace('__REMOTE_PATH__', $RemotePath)

$v = Invoke-RemoteChecked -What 'device verification' -Script $verifyScript
if ($null -eq $v) { exit $failVerify }
$v.Output | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }

$devHeadRemote = Get-KeyValue -Lines $v.Output -Key 'HEAD'
$devTreeRemote = Get-KeyValue -Lines $v.Output -Key 'TREE'
$branchRemote  = Get-KeyValue -Lines $v.Output -Key 'BRANCH'
$trackedRemote = Get-KeyValue -Lines $v.Output -Key 'TRACKED'
$wtFilesRemote = Get-KeyValue -Lines $v.Output -Key 'WORKTREE_FILES'
$dirtyRemote   = Get-KeyValue -Lines $v.Output -Key 'DIRTY_COUNT'
$untrackedRemote = Get-KeyValue -Lines $v.Output -Key 'UNTRACKED'
$ignoredRemote   = Get-KeyValue -Lines $v.Output -Key 'IGNORED'
$subjectRemote = Get-KeyValue -Lines $v.Output -Key 'SUBJECT'
$denyRemote    = Get-KeyValue -Lines $v.Output -Key 'RECEIVE_DENY'

$problems = @()

if ($branchRemote -ne $Branch)      { $problems += "device branch is '$branchRemote', expected '$Branch' (updateInstead only updates the checked-out branch)" }
if ($devHeadRemote -ne $devHead)    { $problems += "HEAD mismatch: device $devHeadRemote / dev $devHead" }
if ($devTreeRemote -ne $devTree)    { $problems += "tree mismatch: device $devTreeRemote / dev $devTree" }
if ($dirtyRemote -ne '0')           { $problems += "device worktree not clean: $dirtyRemote entries" }
if ($untrackedRemote -ne '0')       { $problems += "device has $untrackedRemote untracked (non-ignored) file(s) - unexpected leftovers" }
if ($subjectRemote -ne $devSubject) { $problems += "subject mismatch: device '$subjectRemote' / dev '$devSubject'" }
if ($denyRemote -notmatch 'updateInstead') { $problems += "device receive.denyCurrentBranch='$denyRemote' (expected updateInstead)" }

if ($problems.Count -eq 0) {
    Write-Ok "HEAD matches: $devHeadRemote"
    Write-Ok "tree matches: $devTreeRemote"
    Write-Ok "branch/worktree: $branchRemote / clean"
    Write-Ok "receive.denyCurrentBranch=updateInstead confirmed on device"
} else {
    $problems | ForEach-Object { Write-Err2 $_ }
}

# ---------------------------------------------------------------- 3. Per-file worktree comparison
Write-Section '3/6 per-file worktree comparison (git hash-object vs committed blob)'

# Expected content hash comes from the COMMITTED blob (HEAD:<path>), not from the dev working
# tree: only commits are deployed, so uncommitted local edits must not be reported as a
# device-side mismatch. Comparing the device's working-tree hash against the committed blob
# still catches smudge/filter/CRLF damage on the device (worktree != blob).
$trackedFiles = @((Invoke-GitLocal @('ls-tree', '-r', '--name-only', 'HEAD')).Output | Where-Object { $_.Trim().Length -gt 0 })
$devHashes = @{}
foreach ($rel in $trackedFiles) {
    $h = (Invoke-Native -Exe 'git' -Arguments @('-C', $RepoRoot, 'rev-parse', "HEAD:$rel")).Output[0]
    if ($null -ne $h) { $devHashes[$rel.Trim()] = $h.Trim() }
}
$remoteHashes = Get-HashMap -Lines $v.Output

$missing = @(); $diff = @()
foreach ($k in $devHashes.Keys) {
    if (-not $remoteHashes.ContainsKey($k)) { $missing += $k }
    elseif ($remoteHashes[$k] -ne $devHashes[$k]) { $diff += $k }
}
if ($missing.Count -eq 0 -and $diff.Count -eq 0 -and $devHashes.Count -eq [int]$trackedRemote) {
    Write-Ok "device worktree: $($devHashes.Count) tracked files byte-identical to committed HEAD blobs"
} else {
    $problems += "worktree content mismatch: missing $($missing.Count) / different $($diff.Count) / count $($devHashes.Count) vs $trackedRemote"
    $missing | Select-Object -First 10 | ForEach-Object { Write-Err2 "missing on device: $_" }
    $diff    | Select-Object -First 10 | ForEach-Object { Write-Err2 "content differs:   $_" }
}
if ($trackedRemote -ne $wtFilesRemote) {
    # NOTE: the raw file count includes gitignored artifacts (e.g. __pycache__ produced by
    # running the contract tests on the device, or var/ runtime data). Only untracked,
    # NON-ignored files indicate a real problem, so that is the check above; this is info.
    Write-Info "device file count: tracked=$trackedRemote raw=$wtFilesRemote ignored-artifacts=$ignoredRemote"
} else {
    Write-Ok "device file count matches: $trackedRemote tracked files, no extra files"
}

# ---------------------------------------------------------------- 4. Run-data separation
Write-Section '4/6 run-data separation'

if ($InitVarDir) {
    $initScript = @'
set -e
umask 077
mkdir -p "$HOME/asistent-var"/db "$HOME/asistent-var"/index "$HOME/asistent-var"/logs \
         "$HOME/asistent-var"/archive "$HOME/asistent-var"/queue "$HOME/asistent-var"/config \
         "$HOME/asistent-var"/tmp
chmod 700 "$HOME/asistent-var"
ls -ld "$HOME/asistent-var" "$HOME/asistent-var"/*
echo "INIT_OK=1"
'@
    $iv = Invoke-RemoteChecked -What 'init run-data dir' -Script $initScript
    if ($null -ne $iv) {
        $iv.Output | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
        Write-Ok "run-data skeleton ready: $VarDir"
    } else { $problems += 'run-data dir init failed' }
}

$varProbeScript = @'
if [ -e "$HOME/asistent-var" ]; then
  if [ -L "$HOME/asistent-var" ]; then echo "VAR_STATE=symlink"; else echo "VAR_STATE=dir"; fi
else
  echo "VAR_STATE=missing"
fi
if [ -d "$HOME/asistent-var" ]; then
  echo "VAR_MODE=$(stat -c %a "$HOME/asistent-var")"
fi
echo "DF=$(df -h "$HOME" | tail -1)"
exit 0
'@
$vp = Invoke-RemoteChecked -What 'run-data dir check' -Script $varProbeScript
$vp.Output | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }

$varState = Get-KeyValue -Lines $vp.Output -Key 'VAR_STATE'
$varMode  = Get-KeyValue -Lines $vp.Output -Key 'VAR_MODE'
$dfLine   = Get-KeyValue -Lines $vp.Output -Key 'DF'

switch ($varState) {
    'dir'     {
        Write-Ok "run-data dir exists: $VarDir (outside repo, survives push/rollback)"
        if ($varMode -and $varMode -ne '700') { Write-Warn2 "permissions are $varMode, expected 700 (holds credentials)" }
    }
    'missing' { Write-Warn2 "run-data dir $VarDir not created yet - use -InitVarDir when needed" }
    'symlink' { $problems += "$VarDir is a symlink; use a real directory so rollback/cleanup cannot redirect into the repo" }
    default   { $problems += "cannot determine $VarDir state (VAR_STATE='$varState')" }
}
if ($dfLine)    { Write-Info "device disk: $dfLine" }
if ($repoAbs)   { Write-Info "device repo abs path: $repoAbs" }
if ($repoAbs -and $repoAbs.StartsWith("$repoAbs/asistent-var")) { $problems += 'repo path and run-data path overlap' }

# ---------------------------------------------------------------- 5. Contract-test runnability
Write-Section '5/6 device contract-test runnability probe'

if ($SkipTestProbe) {
    Write-Warn2 'skipped (-SkipTestProbe)'
} else {
    $testScript = @'
cd "$HOME/__REMOTE_PATH__"
echo "PY=$(python3 -V 2>&1)"
echo "PY_BIN=$(command -v python3)"
if [ -d tests ]; then
  echo "TESTS_DIR=yes"
  set +e
  python3 -m unittest discover -s tests -t . -p 'test_*.py' > /tmp/asistent-deploy-unittest.log 2>&1
  rc=$?
  set -e
  # 不要用 `| tail -N`：测试会往 stderr 打大量证据行，尾部窗口会被证据挤满，
  # 把 "Ran N tests / OK" 挤出窗口，于是"测试通过"被误判成失败（曾真实发生）。
  # 判定一律看退出码，摘要单独 grep 全量日志。
  grep -E "^(Ran [0-9]+ tests?|OK|FAILED)" /tmp/asistent-deploy-unittest.log | tail -3
  grep -E "^FAILED|^ERROR:" /tmp/asistent-deploy-unittest.log | head -5
  echo "UNITTEST_RC=$rc"
else
  echo "TESTS_DIR=absent"
  python3 -m unittest discover -s . -p 'test_*.py' 2>&1 | tail -4
fi
exit 0
'@
    $testScript = $testScript.Replace('__REMOTE_PATH__', $RemotePath)
    $tp = Invoke-RemoteChecked -What 'contract-test probe' -Script $testScript -TolerateFailure
    if ($null -eq $tp) {
        $problems += 'contract-test probe command failed'
    } else {
        $tp.Output | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
        $joined = $tp.Output -join "`n"
        if ($joined -match 'TESTS_DIR=absent') {
            Write-Warn2 'tests/ does not exist yet - runnability cannot be proven this round (tracked as TODO)'
        } elseif ($joined -match 'UNITTEST_RC=0') {
            Write-Ok 'device test suite passed (UNITTEST_RC=0)'
        } elseif ($joined -match 'UNITTEST_RC=[1-9]') {
            $problems += 'device test suite FAILED (see log above; full log: /tmp/asistent-deploy-unittest.log)'
        } elseif ($joined -match 'OK') {
            Write-Ok 'device can run python3 -m unittest discover (exit code not captured)'
        } else {
            Write-Warn2 'device tests did not all pass - see output above'
        }
    }
}

# ---------------------------------------------------------------- 6. Deployment record
Write-Section '6/6 deployment record (no secrets)'

Write-Host "  deployed_at   : $((Get-Date).ToString('yyyy-MM-ddTHH:mm:sszzz'))"
Write-Host "  dev HEAD      : $devHead"
Write-Host "  device HEAD   : $devHeadRemote"
Write-Host "  device tree   : $devTreeRemote"
Write-Host "  branch        : $branchRemote"
Write-Host "  subject       : $subjectRemote"
Write-Host "  tracked files : $trackedRemote (worktree $wtFilesRemote)"
Write-Host "  device repo   : ${Remote}:~/$RemotePath"
Write-Host "  run data      : $VarDir (outside repo)"
Write-Host "  device python : see section 5"

if ($problems.Count -gt 0) {
    Write-Section "RESULT: verification FAILED ($($problems.Count) problem(s))"
    $problems | ForEach-Object { Write-Err2 $_ }
    exit $failVerify
}

Write-Section 'RESULT: deploy path verified'
Write-Host "  device Git HEAD = $devHeadRemote" -ForegroundColor Green
exit 0

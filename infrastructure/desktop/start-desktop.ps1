<#
.SYNOPSIS
    Bring the desktop back to a working state after a reboot, and say whether it is.

.DESCRIPTION
    The Scheduled Task starts the runner at logon, so after a normal restart this
    should find everything already running and simply confirm it. It exists for the
    times that is not true: a dependency changed, the venv was rebuilt, the task was
    stopped and forgotten, or a previous runner is still holding the worker name.

    Deliberately does NOT `git pull`. Pulling would change which code the desktop runs
    as a side effect of "check that things are up", and a runner claiming production
    jobs should only ever change when someone decides it should.

    Nothing here is destructive. It syncs dependencies, starts the task if it is not
    running, and reports. The single-instance lock (ADR-0014) means running this while
    the runner is already up cannot produce a second claimant.

.PARAMETER RepoRoot
    Repository root. Defaults to two levels above this script.

.PARAMETER SkipSync
    Skip the dependency sync. Useful when offline, or when you know nothing changed and
    want the report in a second rather than ten.
#>
[CmdletBinding()]
param(
    [string]$RepoRoot,
    [string]$WorkerName = "desktop-4070",
    [string]$TaskName = "TheDrop Agent Runner",
    [switch]$SkipSync
)

$ErrorActionPreference = "Stop"

# $PSScriptRoot is NOT populated during parameter binding in Windows PowerShell 5.1.
if (-not $PSBoundParameters.ContainsKey('RepoRoot') -or [string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
}

function Say([string]$status, [string]$message) {
    Write-Output ("  {0,-6} {1}" -f $status, $message)
}

Write-Output ""
Write-Output "THE DROP - desktop readiness"
Write-Output "============================"

# ---------------------------------------------------------------- repository
if (-not (Test-Path (Join-Path $RepoRoot "services\agent-runner"))) {
    Say "FAIL" "no agent-runner under $RepoRoot. Pass -RepoRoot explicitly."
    exit 2
}
Set-Location $RepoRoot
$branch = (& git rev-parse --abbrev-ref HEAD 2>$null)
$sha = (& git rev-parse --short HEAD 2>$null)
Say "ok" "repo $RepoRoot ($branch @ $sha)"

# ---------------------------------------------------------------- credentials
# Checked before anything expensive: a missing token is the one failure that no amount
# of syncing or restarting will fix, and it should be the first thing reported.
$missing = @("THEDROP_API_URL", "WORKER_TOKEN") |
    Where-Object { -not [Environment]::GetEnvironmentVariable($_, "User") }
if ($missing.Count -gt 0) {
    Say "FAIL" "$($missing -join ' and ') not set in your user environment."
    Say "" "Fix: powershell -File infrastructure\desktop\install-task.ps1"
    exit 2
}
Say "ok" "credentials present ($([Environment]::GetEnvironmentVariable('THEDROP_API_URL','User')))"

# ---------------------------------------------------------------- dependencies
if ($SkipSync) {
    Say "skip" "dependency sync (-SkipSync)"
} else {
    Write-Output "  ...    syncing dependencies (uv sync --group desktop-ml)"
    # desktop-ml, not desktop: without it torch and the models are resolved out of the
    # venv and the runner starts fine while advertising neither embeddings nor entity
    # extraction -- which looks exactly like an idle queue.
    & uv sync --group desktop-ml 2>&1 | Where-Object { $_ -match 'error|warning|Installed|Uninstalled' } |
        ForEach-Object { Say "" $_ }
    if ($LASTEXITCODE -ne 0) {
        Say "FAIL" "uv sync failed. The runner may still start, but without its models."
        exit 2
    }
    Say "ok" "dependencies synced"
}

# ---------------------------------------------------------------- gpu
$gpu = & uv run python -c "import torch; print(f'{torch.__version__}|{torch.cuda.is_available()}|' + (torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'none'))" 2>$null
if ($LASTEXITCODE -eq 0 -and $gpu) {
    $parts = $gpu.Trim().Split('|')
    if ($parts[1] -eq 'True') {
        Say "ok" "torch $($parts[0]) on $($parts[2])"
    } else {
        # A CPU build runs, slowly, and reports success -- the exact failure ADR-0016
        # and the CUDA index pin exist to prevent. Worth shouting about.
        Say "WARN" "torch $($parts[0]) has NO CUDA. Embeddings will run on the CPU."
        Say "" "Fix: uv lock --upgrade-package torch ; uv sync --group desktop-ml"
    }
} else {
    Say "WARN" "torch not importable; the runner will not advertise embeddings or entities"
}

# ---------------------------------------------------------------- the runner
. (Join-Path $PSScriptRoot "runner-control.ps1")

if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
    Say "FAIL" "scheduled task '$TaskName' is not registered."
    Say "" "Fix: powershell -File infrastructure\desktop\install-task.ps1"
    exit 2
}

$lock = Get-RunnerLock -Name $WorkerName
if ($lock -and $lock.IsRunning) {
    Say "ok" "runner already running (pid $($lock.ProcessId), since $($lock.Since))"
} else {
    Write-Output "  ...    starting the runner"
    Start-ScheduledTask -TaskName $TaskName
    # The wrapper has to start python, which has to take the lock. Polling for the lock
    # rather than sleeping a fixed time reports the truth on a slow morning.
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 500
        $lock = Get-RunnerLock -Name $WorkerName
        if ($lock -and $lock.IsRunning) { break }
    }
    if ($lock -and $lock.IsRunning) {
        Say "ok" "runner started (pid $($lock.ProcessId))"
    } else {
        Say "FAIL" "runner did not come up within 20s. Last log lines:"
        Get-Content "$env:LOCALAPPDATA\thedrop\logs\agent-runner.log" -Tail 8 -ErrorAction SilentlyContinue |
            ForEach-Object { Say "" $_ }
        exit 1
    }
}

# ---------------------------------------------------------------- recent work
Write-Output ""
Write-Output "Recent activity:"
$log = "$env:LOCALAPPDATA\thedrop\logs\agent-runner.log"
if (Test-Path $log) {
    Get-Content $log -Tail 6 | ForEach-Object { Write-Output "  $_" }
} else {
    Write-Output "  (no log yet)"
}

Write-Output ""
Write-Output "Ready. The runner claims jobs from https://thedrop.channel on its own;"
Write-Output "nothing else needs starting on this machine."
Write-Output ""
Write-Output "  log:    $log"
Write-Output "  stop:   . .\infrastructure\desktop\runner-control.ps1 ; Stop-Runner -Name $WorkerName"
Write-Output ""

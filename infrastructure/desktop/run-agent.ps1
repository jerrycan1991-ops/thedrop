<#
.SYNOPSIS
    Runs the desktop agent-runner. Invoked by the Scheduled Task; also runnable by hand.

.DESCRIPTION
    A wrapper rather than pointing the task straight at `uv run python -m agent`, for
    three reasons:

      * The task definition stays stable. Changing how the runner starts is an edit
        here, not a re-registration of a Windows task.
      * Output goes to a log file. A Scheduled Task's own history records that a
        process exited, not what it said on the way out.
      * Configuration is checked before the runner starts, so a missing token is one
        clear line rather than a task that appears to run and immediately stops.

    Reads THEDROP_API_URL and WORKER_TOKEN from the USER environment (set once by
    install-task.ps1). Nothing is stored in this file.

.PARAMETER RepoRoot
    Repository root. Defaults to two levels above this script, so it follows the file.
#>
[CmdletBinding()]
param(
    [string]$RepoRoot = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)),
    [string]$LogDir = "$env:LOCALAPPDATA\thedrop\logs"
)

$ErrorActionPreference = "Stop"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$logFile = Join-Path $LogDir "agent-runner.log"

function Write-Log([string]$message) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-ddTHH:mm:ss"), $message
    Add-Content -Path $logFile -Value $line -Encoding utf8
    Write-Output $line
}

# A log nobody rotates fills a disk. 20MB is generous for text and small enough that
# truncating loses only history nobody reads.
if ((Test-Path $logFile) -and ((Get-Item $logFile).Length -gt 20MB)) {
    $keep = Get-Content $logFile -Tail 2000
    Set-Content -Path $logFile -Value $keep -Encoding utf8
    Write-Log "log truncated at 20MB"
}

if (-not (Test-Path (Join-Path $RepoRoot "services\agent-runner"))) {
    Write-Log "ERROR: no agent-runner under $RepoRoot. Pass -RepoRoot explicitly."
    exit 2
}

# Checked here so the failure is one readable line in the log, rather than a task that
# starts, exits 2, and reports only 'the operation completed'.
foreach ($name in @("THEDROP_API_URL", "WORKER_TOKEN")) {
    if (-not [Environment]::GetEnvironmentVariable($name, "User") -and -not (Get-Item "env:$name" -ErrorAction SilentlyContinue)) {
        Write-Log "ERROR: $name is not set. Re-run install-task.ps1 to configure it."
        exit 2
    }
}

# A Scheduled Task does not inherit User-scope variables set after it was created, so
# they are loaded explicitly rather than assumed.
foreach ($name in @("THEDROP_API_URL", "WORKER_TOKEN", "WORKER_NAME")) {
    $value = [Environment]::GetEnvironmentVariable($name, "User")
    if ($value) { Set-Item -Path "env:$name" -Value $value }
}

Set-Location $RepoRoot
Write-Log "starting agent-runner from $RepoRoot"

# 2>&1 so a traceback reaches the log rather than being discarded with stderr.
& uv run python -m agent 2>&1 | ForEach-Object { Write-Log $_ }
$code = $LASTEXITCODE

Write-Log "agent-runner exited with code $code"

# Exit 2 is the runner's fatal condition: the token was rejected. Restarting cannot fix
# it, and the Scheduled Task's restart policy would otherwise retry a credential that
# will never work. Surfaced distinctly so the task history shows it.
if ($code -eq 2) {
    Write-Log "FATAL: worker token rejected. Re-provision on the VPS and re-run install-task.ps1."
}

exit $code

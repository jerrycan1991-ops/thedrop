<#
.SYNOPSIS
    Registers the agent-runner as a Windows Scheduled Task that starts at logon.

.DESCRIPTION
    Task Scheduler rather than a Windows Service or PM2:

      * Native. Nothing to install, nothing to keep updated, and it is where a Windows
        operator already looks when something is not running.
      * A real Windows Service would need a wrapper (NSSM or similar) because Python is
        not a service host, which is a dependency for no gain here.
      * PM2 runs the VPS, but its Windows boot persistence needs a third-party helper
        and is the flakiest part of PM2 on this platform.

    The task runs as the current user, not SYSTEM. The runner needs that user's `uv`,
    nvm and PATH, and it holds a credential that has no business being available to
    every process on the machine.

.PARAMETER Token
    Worker token from `provision_worker` on the VPS. Prompted for (hidden) if omitted.

.PARAMETER ApiUrl
    Defaults to https://thedrop.channel.

.PARAMETER RepoRoot
    Repository root. Defaults to two levels above this script.

.EXAMPLE
    .\install-task.ps1
    Prompts for the token, stores config, registers the task and starts it.

.EXAMPLE
    .\install-task.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [string]$ApiUrl = "https://thedrop.channel",
    [securestring]$Token,
    [string]$WorkerName = "desktop-4070",
    [string]$RepoRoot,
    [string]$TaskName = "TheDrop Agent Runner",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

# $PSScriptRoot is NOT populated during parameter binding in Windows PowerShell 5.1 --
# only once the body begins. A param-block default referencing it silently evaluates to
# an empty string, and the first Split-Path then fails with an error naming the wrong
# thing. Resolved here instead.
if (-not $PSBoundParameters.ContainsKey('RepoRoot') -or [string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
}

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Output "Removed scheduled task: $TaskName"
    } else {
        Write-Output "No task named '$TaskName' to remove."
    }
    Write-Output "WORKER_TOKEN is still in your user environment. Clear it with:"
    Write-Output '  [Environment]::SetEnvironmentVariable("WORKER_TOKEN", $null, "User")'
    return
}

$wrapper = Join-Path $PSScriptRoot "run-agent.ps1"
if (-not (Test-Path $wrapper)) { throw "run-agent.ps1 not found next to this script" }
if (-not (Test-Path (Join-Path $RepoRoot "services\agent-runner"))) {
    throw "No agent-runner under $RepoRoot. Pass -RepoRoot explicitly."
}

if (-not $Token) {
    # Read-Host -AsSecureString so the token is not echoed and does not reach the
    # PowerShell history file.
    $Token = Read-Host -AsSecureString "Paste the worker token (input hidden)"
}

$plain = [System.Net.NetworkCredential]::new("", $Token).Password
if ([string]::IsNullOrWhiteSpace($plain)) { throw "No token supplied." }

# User scope, not Machine: the token belongs to this account, and Machine scope would
# hand it to every process and every user on the box.
[Environment]::SetEnvironmentVariable("THEDROP_API_URL", $ApiUrl, "User")
[Environment]::SetEnvironmentVariable("WORKER_TOKEN", $plain, "User")
[Environment]::SetEnvironmentVariable("WORKER_NAME", $WorkerName, "User")
$plain = $null
Write-Output "Stored THEDROP_API_URL, WORKER_NAME and WORKER_TOKEN in your user environment."

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$wrapper`" -RepoRoot `"$RepoRoot`"" `
    -WorkingDirectory $RepoRoot

# At logon rather than at startup: the task runs as this user and needs that user's
# profile for uv and nvm, which is not available before logon.
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -RestartCount 3 `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

# ExecutionTimeLimit zero: the runner is meant to run forever, and the default
# three-day limit would kill it silently on the fourth day.
#
# MultipleInstances IgnoreNew: two runners sharing one token would both claim jobs.
# The VPS tolerates that -- claiming is SKIP LOCKED -- but it doubles the poll rate and
# makes the admin's current_job_count meaningless.
#
# RestartCount 3: the runner already survives an unreachable VPS on its own, backing
# off to 120s. If it has exited three times in fifteen minutes, retrying is not the
# answer and a person should look.

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "THE DROP desktop agent-runner. Claims leased AI jobs over outbound HTTPS (ADR-0001)." `
    -Force | Out-Null

Write-Output "Registered scheduled task: $TaskName"

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3
$state = (Get-ScheduledTask -TaskName $TaskName).State
Write-Output "Task state: $state"
Write-Output ""
Write-Output "Log:    $env:LOCALAPPDATA\thedrop\logs\agent-runner.log"
Write-Output "Status: Get-ScheduledTask -TaskName '$TaskName' | Get-ScheduledTaskInfo"
Write-Output "Stop:   Stop-ScheduledTask -TaskName '$TaskName'"
Write-Output "Remove: .\install-task.ps1 -Uninstall"

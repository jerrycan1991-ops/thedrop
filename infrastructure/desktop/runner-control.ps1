<#
.SYNOPSIS
    Find and stop the agent-runner that owns a given worker name.

.DESCRIPTION
    Dot-source this from install-task.ps1, or use it by hand.

    It exists because `Stop-ScheduledTask` does not stop the runner. The task action is
    powershell.exe running run-agent.ps1, which spawns `uv run python -m agent`; stopping
    the task kills the wrapper and leaves the python grandchild running. Observed
    directly: the task reported `Ready` while four runner processes kept polling.

    That is how orphans accumulate, and it is why a guard that only stops the task is
    not a guard.

    Attribution is the hard part -- a second runner may legitimately be serving a
    different WORKER_NAME, which is invisible from a command line. So this does not
    pattern-match on `-m agent`. It reads the single-instance lock file, which records
    exactly which pid owns which worker name (see agent/single_instance.py), and acts
    only on the process that claims the name it was asked about.

    The lock filename contains a hash of the worker name. Rather than reimplement that
    hashing in PowerShell -- two implementations of one naming scheme is a defect
    waiting to happen -- these scan the lock directory and match on the name recorded
    INSIDE each file.
#>

# No Set-StrictMode here: this file is dot-sourced, and strict mode would follow into
# the caller's session and change how unrelated code behaves.

function Get-RunnerLockDir {
    Join-Path $env:LOCALAPPDATA 'thedrop'
}

function Read-RunnerLockNote {
    <#
        The note starts at byte 1: byte 0 is the lock itself, and Windows byte-range
        locks block reads of the locked region. Opened with FileShare ReadWrite so a
        live holder does not make its own note unreadable.
    #>
    param([Parameter(Mandatory)][string]$Path)

    try {
        $stream = [System.IO.File]::Open($Path, 'Open', 'Read', 'ReadWrite')
    } catch {
        return $null
    }
    try {
        if ($stream.Length -le 1) { return $null }
        $stream.Seek(1, 'Begin') | Out-Null
        $buffer = New-Object byte[] 200
        $read = $stream.Read($buffer, 0, 200)
        return [System.Text.Encoding]::UTF8.GetString($buffer, 0, $read).Trim()
    } finally {
        $stream.Close()
    }
}

function Get-RunnerLock {
    <#
    .SYNOPSIS
        The lock entry for a worker name, or $null when nothing holds it.
    #>
    param([Parameter(Mandatory)][string]$Name)

    $dir = Get-RunnerLockDir
    if (-not (Test-Path -LiteralPath $dir)) { return $null }

    foreach ($file in Get-ChildItem (Join-Path $dir 'runner-*.lock') -ErrorAction SilentlyContinue) {
        $note = Read-RunnerLockNote -Path $file.FullName
        if (-not $note) { continue }
        # Format is: pid=<n> name=<worker> since=<iso>. The name is captured lazily up
        # to " since=" so a worker name containing spaces still parses.
        if ($note -notmatch '^pid=(\d+) name=(.+?) since=(\S+)') { continue }
        if ($Matches[2] -ne $Name) { continue }

        [pscustomobject]@{
            Path       = $file.FullName
            ProcessId  = [int]$Matches[1]
            WorkerName = $Matches[2]
            Since      = $Matches[3]
            # A lock file outlives its holder on purpose (deleting it would let a second
            # runner lock a NEW file at the same path while the first held the old
            # inode). So a recorded pid proves nothing until it is checked.
            IsRunning  = [bool](Get-Process -Id ([int]$Matches[1]) -ErrorAction SilentlyContinue)
        }
        return
    }
    return $null
}

function Stop-Runner {
    <#
    .SYNOPSIS
        Stop the runner holding $Name. Returns the lock entry it stopped, or $null.

    .DESCRIPTION
        Returns rather than prints, so a caller can suppress the value without also
        losing the message. In PowerShell a function's Write-Output IS its return
        value, so `Stop-Runner ... | Out-Null` silently discarded both -- an operator
        saw a runner disappear with nothing saying why.
    #>
    param(
        [Parameter(Mandatory)][string]$Name,
        [int]$TimeoutSeconds = 15
    )

    $lock = Get-RunnerLock -Name $Name
    if (-not $lock -or -not $lock.IsRunning) { return $null }

    Stop-Process -Id $lock.ProcessId -Force -ErrorAction SilentlyContinue

    # Wait for the process to actually be gone, so the OS has released the lock.
    # Starting a replacement first would have the new runner exit 3 -- the exact
    # confusing outcome this is here to prevent.
    for ($i = 0; $i -lt ($TimeoutSeconds * 4); $i++) {
        if (-not (Get-Process -Id $lock.ProcessId -ErrorAction SilentlyContinue)) { return $lock }
        Start-Sleep -Milliseconds 250
    }

    Write-Warning "runner pid $($lock.ProcessId) did not exit within ${TimeoutSeconds}s"
    return $null
}

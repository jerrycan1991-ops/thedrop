# ADR-0014: One runner per worker name, guarded on the desktop

Status: Accepted (Phase 2)

Date: 2026-09-03

## Context

Three `agent-runner` processes were found polling as `desktop-4070` simultaneously, and
had been for over a day. Two were orphans: one started by hand, one left behind when
`install-task.ps1` re-registered the Scheduled Task at a new repository root —
`Register-ScheduledTask -Force` replaces the task definition but does not stop a
process already running under the old one.

Nothing surfaced it. The VPS sees one worker *name* heartbeating, PM2 knows nothing
about the desktop, and the runner log is append-only, so three writers simply
interleave. It was found only because a `git worktree remove` failed on a locked
`python.exe`.

Duplicates are not merely wasteful, because **worker identity is the token, not the
process**. Every runner sharing a token is one `worker_nodes` row:

- `Runner._release_orphaned_leases()` runs at startup and fails *every* job leased to
  the node. A sibling mid-job has its lease pulled, its `complete` answered with 409,
  and its finished result discarded — after the GPU work was done.
- `attempts` was already incremented by the claim, so a job bounced this way a few
  times reaches `max_attempts` and is recorded FAILED having never actually failed.
- `current_job_count` becomes whichever process beat last, so the admin worker panel
  reports a number belonging to no one.

No damage has occurred: Phase 3 has not started, so no job was ever leased. The window
in which this is free to fix is now.

## Options considered

1. **Fix `install-task.ps1` only.** Stop the task before re-registering. Necessary, but
   not sufficient on two counts: it would not have caught the hand-started orphan or a
   runner launched from a second checkout, and — measured while implementing this —
   **`Stop-ScheduledTask` does not stop the runner at all.** It kills the PowerShell
   wrapper; the `uv run python -m agent` grandchild survives. The task reported `Ready`
   while four runner processes kept polling. A guard that only covers the path we
   happened to observe, using a mechanism that does not do what it appears to, is not a
   guard.
2. **A pidfile.** Needs stale detection, and PID reuse makes that unreliable: a stale
   file naming a PID that now belongs to something else either blocks a legitimate
   start or is ignored and blocks nothing.
3. **An OS lock held by the runner process.** The kernel releases it when the process
   dies, however it dies. No stale state, no reuse race.
4. **Server-side arbitration.** An instance id on `worker_nodes`; a runner registers,
   the newest registration owns the node, and a superseded runner is told to exit on
   its next heartbeat. This is the only option that sees two *machines* sharing a
   token.

## Decision

Option 3, plus option 1. The runner takes an exclusive OS lock keyed on `WORKER_NAME`
before entering the claim loop and exits `3` if another process holds it.

Because stopping the task does not stop the runner, `install-task.ps1` also stops the
*process* — via `Stop-Runner` in `infrastructure/desktop/runner-control.ps1`, both
before re-registering and on `-Uninstall`. Attribution comes from the lock file itself,
which records which pid owns which worker name, so it stops exactly the runner holding
the name being configured. It deliberately does not pattern-match on `-m agent`: a
second runner may legitimately be serving a different `WORKER_NAME`, which is invisible
from a command line. Runners it cannot attribute are reported, not killed.

Option 4 is **deliberately not built**. It is recorded here so the gap is a decision
rather than an oversight.

## Rationale

- The observed and likely failure is duplicates on one machine — a re-run installer, a
  manual start, a second checkout. A local lock closes that completely.
- An OS lock has no failure mode that needs code: there is no stale lock, because there
  is no lock once the holder is gone.
- Option 4 costs a schema migration, a protocol change, and a new way for a runner to
  be told to stop — against a scenario that requires deliberately copying a token
  between machines. `provision_worker` issues one token per worker name, so that is a
  misconfiguration, not a normal path.
- Keying on `WORKER_NAME` rather than on the machine keeps two workers on one desktop
  legal, which is a configuration we may actually want when the second GPU arrives.

## Consequences

- A second runner exits `3` instead of running. `2` (needs a human) and `3` (resolves
  itself) are distinct so a supervisor can treat them differently; `run-agent.ps1` logs
  them differently for the same reason.
- `--check` does not take the lock. Refusing to diagnose a worker because it is running
  would be exactly backwards.
- **Two machines sharing one token remain unguarded.** If a second desktop is ever
  added, that must be closed first — and the fix is option 4, not a wider local lock,
  which cannot see another machine by construction.
- **`Stop-ScheduledTask` alone is now a documented trap**, in the runner README and in
  what `install-task.ps1` prints on exit. It was previously advertised as the way to
  stop the runner, which is how at least one orphan survived a re-registration.
- The lock file is never unlinked. Deleting it on release would let a second runner
  create and lock a *new* file at the same path while the first still held the old
  inode: two runners, both holding "the" lock.

# ADR-0012: Unprivileged deployment — managed Postgres, user-space Redis, PM2 supervision

Status: Accepted (Phase 1) — **contingent**

Date: 2026-09-02

Applies only while the production host denies root. It does not supersede
[ADR-0011](0011-native-data-services.md); that remains the target architecture, and this
is the degraded mode we run in until root is available.

## Context

The production VPS (`rover`) is managed by CloudPanel, and the only account we hold is
`thedropdeploy`, a CloudPanel **site user**. Verified on the host:

```
sudo -l          → (ALL) NOPASSWD: /usr/bin/clpctlWrapper   # nothing else
su -             → /usr/bin/su: Permission denied           # execute bit stripped
passwd root      → may not view or modify password information for root
usermod -aG sudo → Permission denied
getent group sudo → sudo:x:27:                              # empty, no admin account exists
```

No escalation path exists from the host, and none should be sought. Root is reachable
only through the VPS provider's out-of-band console, which was not available.

That makes ADR-0011 unexecutable: it requires `apt install postgresql-16`, writing to
`/etc/systemd/system`, and `systemctl`. It also rules out `loginctl enable-linger`, so
even user-scoped systemd units would not survive a reboot.

The alternative to this ADR is not a better deployment; it is no deployment.

## Decision

- **PostgreSQL moves off the VPS** to a managed provider offering pgvector. The
  application reaches it over the network via `DATABASE_URL`.
- **Redis stays on the VPS** as an unprivileged process. `/usr/bin/redis-server` is
  executable by any user and 6380 is above 1024, so the dedicated instance ADR-0011
  wanted — own password, own eviction policy, `FLUSHALL`/`FLUSHDB`/`CONFIG` renamed
  away — is achievable without privilege. CloudPanel's Redis on 6379 is untouched.
- **Process supervision is PM2**, defined by `infrastructure/pm2/ecosystem.config.cjs`.
  PM2 was already installed on the host and already had a `@reboot ... pm2 resurrect`
  crontab entry managing the web process; a hand-rolled supervisor was written first and
  discarded, because two supervisors for one job is the worse outcome.
- **Paths move under `$HOME`**: code at `~/thedrop`, secrets at
  `~/.config/thedrop/thedrop.env` (0600), state at `~/.local/state/thedrop/`.
- **`infrastructure/scripts/deploy-userspace.sh`** is a separate script rather than a
  mode of `deploy.sh`. The two differ in how they check data services, restart
  processes and resolve paths; branching on all of that inside one script would make
  the privileged path — the one we intend to return to — harder to read.

## Rationale

- Every safeguard in `deploy.sh` is preserved: the pre-deploy backup requirement, the
  build-time `NEXT_PUBLIC_SITE_URL` gate, the health, route-ownership and static-asset
  gates, and automatic rollback. Losing root is not a licence to drop controls
  (`CLAUDE.md`, rule 4).
- The backup requirement survives in altered form. There is no `pg_dump` on the host and
  no way to install one, so the script takes a snapshot when a client is present and
  otherwise **refuses to migrate** unless the operator passes `--backup-verified` to
  assert a provider-side snapshot or PITR point. The check is explicit and named, not
  silently skipped.
- Redis running unprivileged is strictly better than sharing CloudPanel's instance,
  which evicts under an LRU policy shared with PHP sites. Admin sessions and login
  rate-limit counters live in Redis; an evicted rate-limit counter is a disabled
  safeguard.

## Consequences

These are real losses, recorded so nobody mistakes this for an equivalent setup.

- **"The VPS is the source of truth" (ARCHITECTURE.md §3) no longer holds.** Canonical
  state lives with a third party. Their outage is our outage, and their region choice is
  our render-path latency.
- **Every database query crosses the network.** Previously a loopback socket. This lands
  directly on TTFB, which feeds Core Web Vitals and Google News eligibility — the
  consideration that drove ADR-0010.
- **Memory ceilings are PM2's `max_memory_restart`, not systemd's `MemoryMax`.** The
  limits are preserved (700M/700M/800M), but the mechanism differs in kind: systemd
  refuses the allocation, PM2 restarts the process after it has already exceeded the
  limit. On a box already ~900 MB into a 2 GB swapfile, the overshoot is real.
- **No journald.** Logs are files under `~/.local/state/thedrop/log`. `logrotate` needs
  root, so `pm2 install pm2-logrotate` is required or they grow without bound.
- **Restart semantics are PM2's**, which is closer to systemd than expected:
  `restart_delay`, `exp_backoff_restart_delay` and `max_restarts` cover most of
  `RestartSec`/`StartLimitBurst`. What is lost is boot ordering -- PM2 starts everything
  at once, so the API may briefly race Redis on a cold boot and restart into health.
- **A second beat scheduler is now a live risk.** systemd guaranteed a single instance.
  Here it rests on PM2's unique process names and `instances: 1`. `pm2 start` on an
  already-running app is refused rather than duplicated, but a second worker started
  outside PM2 would not be caught, and would fire every scheduled task twice.
- **Database credentials leave the machine.** `DATABASE_URL` now contains a
  network-reachable host, so the blast radius of a leaked env file is larger.

## Exit

When root becomes available: install PostgreSQL per ADR-0011, migrate the data back,
install the systemd units, `pm2 delete all` and remove its crontab entry, and delete
`deploy-userspace.sh` and `infrastructure/pm2/`. This ADR is then superseded rather
than amended — it describes a situation, not a preference.

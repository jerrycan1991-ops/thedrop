# ADR-0011: PostgreSQL and Redis run natively under systemd; Docker is removed from the VPS

Status: Accepted (Phase 1)
Date: 2026-09-02

Supersedes the data-services half of [ADR-0002](0002-docker-for-stateful-only.md). The
other half of that decision — application processes run natively, not in containers —
still stands and is reinforced here.

## Context

ADR-0002 put PostgreSQL and Redis in Docker Compose while the app processes ran under
systemd, on the reasoning that pgvector is preinstalled in `pgvector/pgvector:pg16` and
that image tags make version pinning trivial.

Two things changed when the target host became concrete.

The VPS is managed by **CloudPanel**, which already runs its own MySQL, nginx, Redis,
Varnish and ten PHP-FPM pools. Redis in particular collides: CloudPanel binds 6379, and
the compose file published the same port, so `docker compose up` failed outright with
*port is already allocated*.

More importantly, Docker on this host is a third process manager on a box that already
has two, for two services the distro packages perfectly well. The daemon costs
100–200 MB of the 8 GB budget before a single container starts — against an inventory
(ARCHITECTURE.md §3.1) that already totals ~4.0 GB and shares the machine with a hosting
panel.

## Decision

- **PostgreSQL 16** from the distro, with `postgresql-16-pgvector` for the extension,
  listening on `127.0.0.1:5432`.
- **Redis** as a dedicated second instance on `127.0.0.1:6380`, managed by this
  project's own `thedrop-redis.service` and `/etc/thedrop/redis.conf`. CloudPanel's
  Redis on 6379 is left entirely alone.
- **Docker is not installed on the VPS.** `infrastructure/docker/` remains for local
  development (`docker-compose.dev.yml`) and is not used in production.
- Host ports are configurable via `POSTGRES_HOST_PORT` / `REDIS_HOST_PORT` so a
  collision is an env change, not a file edit on the server.

## Rationale

- **pgvector is a package, not a reason for a container.** `apt install
  postgresql-16-pgvector` is one line. The convenience ADR-0002 was buying turned out to
  be small, and `deploy.sh` now gates on `pg_available_extensions` so a missing
  extension fails in seconds rather than mid-migration.
- **One less process manager.** systemd already supervises the three app services.
  Adding Docker meant restart policies, resource limits and log destinations expressed
  two different ways on one host.
- **A separate Redis instance is a safeguard, not fastidiousness.** CloudPanel's Redis
  is shared with PHP sites under an eviction policy. This application keeps admin
  sessions and login rate-limit counters in Redis; an evicted rate-limit counter is a
  silently disabled control, which `CLAUDE.md` forbids. The dedicated instance is
  password-protected, capped at 512 MB, AOF-persisted, and has `FLUSHALL`, `FLUSHDB` and
  `CONFIG` renamed away — the same hardening the container command carried.
- **Docker's port publishing bypasses UFW.** The compose file needed a load-bearing
  `127.0.0.1:` prefix and a comment explaining that a bare `5432:5432` would expose the
  database to the internet despite the firewall. A `bind 127.0.0.1` line in a config
  file has no such trapdoor.

## Consequences

- **Version pinning is now apt's problem.** An `apt upgrade` can move the Postgres minor
  version where an image tag would not. Unattended-upgrades must exclude `postgresql-*`,
  and major upgrades become a `pg_upgradecluster` exercise rather than a tag change.
  This is the real cost of this decision.
- The Redis password lives in two files: `/etc/thedrop/thedrop.env` for the application
  and `/etc/thedrop/redis.conf` for the server. `deploy.sh` compares them and refuses to
  continue when they disagree, because the failure is otherwise diagnosed as a generic
  connection error.
- The three app units now declare `Requires=postgresql.service thedrop-redis.service`
  in place of `Requires=docker.service`.
- Local development is unchanged and still uses Docker Compose. The two environments now
  differ in how data services are managed, which is a genuine divergence; it is accepted
  because the alternative is installing Docker on a panel-managed production host to
  preserve a symmetry nothing depends on.
- Backups no longer go through `docker exec`. `pg_dump` runs on the host over loopback.

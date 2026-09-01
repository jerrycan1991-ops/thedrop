# ADR-0002: Docker Compose for stateful services only; app processes under systemd

Status: Partially superseded by [ADR-0011](0011-native-data-services.md) (2026-09-02)

> The data-services half of this decision is reversed: PostgreSQL and Redis now
> run natively under systemd and Docker is not installed on the VPS. The other
> half -- application processes run natively rather than in containers -- stands,
> and ADR-0011 reinforces it. The rationale below is preserved as the record of
> why containers were chosen first.
Date: 2026-08-30

## Context

8 GB RAM shared with an existing hosting panel. Containerizing everything is the default instinct, but every container adds memory overhead and every deploy would require an image build on a 4-core box.

## Decision

- PostgreSQL and Redis run under Docker Compose, bound to `127.0.0.1`, pinned by image digest.
- Next.js, FastAPI and the Celery worker run natively under systemd.

## Rationale

- Postgres with pgvector is genuinely easier as a container: the extension is preinstalled, the version is pinned, and upgrades are a tag change.
- App containers would buy little. There is one host, no orchestrator, no horizontal scaling. What they would cost is an image build per deploy (slow and memory-hungry here), a registry, and roughly 100–200 MB of runtime overhead.
- systemd already gives restart policies, resource limits (`MemoryMax`), sandboxing (`ProtectSystem`, `NoNewPrivileges`), dependency ordering and journald logging — most of what we wanted containers for.
- Deploys become `git pull && build && systemctl restart`: seconds, not minutes.

## Consequences

- Two operational models instead of one. Mitigated by keeping the split on a clean line: stateful = Docker, stateless = systemd.
- Node and Python versions are managed on the host and must match CI. Pinned in `.tool-versions` and asserted by the deploy script.
- If we ever move to multiple hosts, app containers come back. That is a real migration and an acceptable future cost.

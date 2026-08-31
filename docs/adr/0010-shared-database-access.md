# ADR-0010: Node and FastAPI share direct database access; Alembic remains the sole schema authority

Status: Accepted
Date: 2026-08-31
Supersedes: ADR-0006 (FastAPI is the sole owner of the database)

## Context

ADR-0006 gave FastAPI exclusive database access, and the web tier read everything
through the HTTP API. The Node-first migration removed that: ten endpoints now run as
Next.js route handlers that query PostgreSQL and Redis directly, and the page data
layer calls the same query functions rather than fetching its own API.

That was the point of the migration — the HTTP hop sat on the render path, and TTFB
feeds Core Web Vitals, which feeds Google News eligibility. But it invalidates the
premise of ADR-0006, and pretending otherwise leaves a document asserting a boundary
that no longer exists.

## Decision

Both tiers connect to the same PostgreSQL database and the same Redis instance.

**Alembic remains the sole schema migration authority.** Nothing else creates, alters
or drops schema objects. There is no Drizzle, no `db push`, and no migrations directory
outside `packages/database/migrations`.

The division is by *responsibility*, not by access:

| | Owns |
|---|---|
| Alembic | every schema change, in every environment |
| FastAPI | worker lease/heartbeat/job endpoints, `PUT /admin/settings/{key}`, health checks, Celery |
| Node | public reads, admin reads, authentication, session lifecycle |
| Both | the `users`, `articles`, `settings`, `categories` and `roles` tables, read-only from Node except during login |

## What ADR-0006 bought, and how each guarantee is replaced

ADR-0006 was not wrong; it bought four things that now need other mechanisms.

**One schema owner.** Unchanged — Alembic still is. Node introspects the live schema
and writes hand-audited SQL; it never migrates. `alembic check` runs in verification and
must report no drift.

**Business rules in exactly one place.** This is the guarantee that genuinely weakened:
publishing gates, claim-to-fact rules and RBAC could now be implemented twice. It is
replaced by tests, not by convention — the API baseline, the parity suites and the RBAC
matrix are the compatibility authority, and they compare status, body, headers and
Redis side effects between the tiers. A shared query layer is *not* a guarantee;
nothing stops someone editing one path and not the other.

**Caching, rate limiting and audit logging at one boundary.** Partially replaced. Both
tiers write `audit_logs` for login. Rate limiting is shared by construction: the counter
is a Redis key on `(ip, email)`, so attempts against either server count against both —
verified by test.

**Database credentials never reaching the Node process.** This one is simply lost, and
it is the real cost of the migration. A compromise of the web process now reaches
PostgreSQL and Redis. Mitigations, all verified: `server-only` on every database and
Redis module (a client-component import is a *build error*, proven by building one);
no credential in any `NEXT_PUBLIC_*` variable; and a scan of the production client
bundle for connection strings, passwords and driver names on every verification pass.

Least-privilege database roles are **not** implemented and remain the outstanding
mitigation. The application connects as the owning role.

## Consequences

- Two connection pools against one `max_connections`. Documented in
  `docs/CONNECTIONS.md`; the budget is not resolved by this ADR.
- Rollback stays real: FastAPI still implements every migrated endpoint, and sessions
  are interchangeable between the tiers in both directions.
- Any future endpoint must state which tier owns it, and route ownership is asserted by
  `tests/test_route_ownership.py` rather than assumed — a Next.js route handler can
  exist, build, and still be shadowed by a proxy rewrite.

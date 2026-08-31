# ADR-0006: FastAPI is the sole owner of the database

Status: **SUPERSEDED by ADR-0010** (the Node migration gave the web tier direct
database access). Retained unchanged as the record of why the boundary was drawn this
way originally — the reasoning below is still correct about what the boundary bought,
which is what ADR-0010 has to replace with something else.

Originally accepted: Phase 0
Date: 2026-08-30

## Context

Next.js could query Postgres directly with an ORM, which is common and fast. That would give us two applications, in two languages, writing to one schema.

## Decision

Only `services/api` connects to Postgres. `apps/web` reads through `/api/v1/public/*` and `/api/v1/admin/*`. The desktop runner reads and writes only through `/api/v1/worker/*`.

## Rationale

- One schema owner means one migration story. Two ORMs over one schema makes every migration a two-language coordination problem, and drift is inevitable.
- Business rules — publishing gates, claim-to-fact rules, rights checks, RBAC — exist in exactly one place. A rule that must be implemented twice will eventually be implemented once.
- The API boundary is where caching, rate limiting and audit logging naturally live.
- Database credentials never reach the Node process, shrinking the blast radius of a frontend compromise.

## Consequences

- Every public page render involves an HTTP hop. Mitigated by ISR (most requests never reach the API at all) and Redis caching behind it.
- Read endpoints must be designed for the pages that consume them, or the web app makes N+1 API calls. Page-shaped endpoints are the rule; generic REST resource endpoints are the exception.
- Local development needs both processes running. `pnpm dev` starts them together.

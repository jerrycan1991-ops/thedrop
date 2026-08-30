# ADR-0004: The admin dashboard lives inside `apps/web` as a route group

Status: Accepted (Phase 0)
Date: 2026-08-30
Supersedes: the standalone `apps/admin` application in the originally proposed tree

## Context

The proposed structure had `apps/web` and `apps/admin` as separate applications. On a 4-core / 8 GB VPS a second Next.js server costs roughly 300 MB of RAM and a second vhost. The hosting panel manages nginx, and we have committed to changing nothing there in Phase 1.

## Options considered

1. Separate Next.js app on port 3101, proxied at `admin.thedrop.channel`. Costs a process and an nginx change.
2. Separate static SPA (Vite + React + shadcn), served by nginx from disk. Costs no process but still needs an nginx `location`/`alias` block.
3. Admin as a route group inside `apps/web` at `/admin`. Costs nothing new.

## Decision

Option 3. `apps/web/app/(admin)/admin/*`, protected by middleware plus server-side session checks against the FastAPI admin API.

## Rationale

- Zero additional processes and zero nginx changes — the two hardest constraints in this deployment.
- Shares the design system, tokens, components and types with the public site with no package plumbing.
- Admin pages are server components hitting the API; admin code does not ship in public route bundles (asserted by a bundle-analysis test).
- The admin UI is not SEO-relevant and does not need its own build pipeline.

## Consequences

- The public site and admin share one process, so an admin bug can affect public availability. Mitigated by: admin routes behind auth, per-route error boundaries, strict rate limits on `/admin`, and `MemoryMax` on the unit.
- If the admin later grows heavy (charts, editors, real-time), splitting it out is a contained change: the route group moves to its own app and the FastAPI API is unchanged.
- Revisit if admin bundle size or admin traffic starts affecting public p95.

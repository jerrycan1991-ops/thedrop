# API baseline — the hybrid architecture contract

Frozen at tag `v0.1.0-hybrid` (commit `3a0dae8`), captured 31 August 2026.

This is the reference every migrated endpoint must match. When a Next.js implementation
replaces a FastAPI one, "it behaves the same" stops being a judgement call:

```bash
uv run python infrastructure/scripts/api_baseline.py compare --base-url http://127.0.0.1:3100
```

Machine-readable captures live in `tests/baseline/`. Volatile fields (timestamps,
request ids) are normalised to `<volatile>`; **status code, content type,
`Cache-Control`, key names, types and null-vs-absent are compared exactly** — those are
the details a reimplementation gets subtly wrong.

---

## 1. Endpoint inventory — 18 routes

### Health (2) — no auth

| Method | Path | Status | Notes |
|---|---|---|---|
| GET | `/healthz` | 200 | Liveness only. Never touches the database. |
| GET | `/readyz` | 200 / 503 | 503 when Postgres or Redis is unreachable, or migrations are behind. Body always includes `database`, `redis`, `migrations`. |

### Public (4) — no auth, cacheable

All send `Cache-Control: public, max-age=60, stale-while-revalidate=300`. **Reproduce
this header exactly** — ISR and any CDN in front of the site depend on it.

| Method | Path | Behaviour |
|---|---|---|
| GET | `/api/v1/public/categories` | Active categories, ordered by `sort_order`. Fields: `slug`, `name`, `description`, `accentToken`, `isCommercial`. |
| GET | `/api/v1/public/articles` | Published only, newest first. Query: `category`, `page` (≥1, ≤500), `page_size` (≥1, ≤50). Returns `{items, page, pageSize, hasMore, total}`. |
| GET | `/api/v1/public/articles/{category}/{yyyy}/{mm}/{dd}/{slug}` | Single article with `body`, `keyFacts`, `sources`, `corrections`, `tags`, `seo`, `structuredData`, `disclosure`. |
| GET | `/api/v1/public/latest` | Newest N. Query: `limit` (≥1, ≤50, default 20). |

**Behaviours that are easy to get wrong and are asserted by the baseline:**

- An unknown category returns **200 with an empty list**, not 404. The category filter is
  a filter, not a lookup.
- A date path that does not match the article's `first_published_at` returns **404**, not
  the article. The date is part of the canonical URL; serving the same content at two
  paths creates duplicates.
- `page=0` and `page_size=9999` return **422**, not a clamped result. Out-of-range is an
  error, not something to silently correct.
- Unpublished and soft-deleted articles are invisible to every public route.
- `total` is `offset + len(items) + (1 if hasMore else 0)` — deliberately an estimate, to
  avoid a second `COUNT(*)` on every page load. A rewrite returning a true count would
  differ, and the baseline will catch it.

### Admin (7) — session cookie + RBAC

All return **401** when unauthenticated (asserted in the baseline for four of them).

| Method | Path | Role | Notes |
|---|---|---|---|
| POST | `/api/v1/admin/auth/login` | — | argon2id verify, Redis session, sets `thedrop_session`. Rate limited 5 / 15 min per IP+email. Locks the account for 15 min after 5 failures. Wrong password and unknown account return an **identical** 401 body. |
| POST | `/api/v1/admin/auth/logout` | any | Destroys the Redis session, clears the cookie with matching `domain` and `path`. |
| GET | `/api/v1/admin/auth/me` | any | Current user and roles. |
| GET | `/api/v1/admin/articles` | editor / analyst / viewer | Paginated, includes drafts. |
| GET | `/api/v1/admin/system/metrics` | analyst / viewer | Article counts, job queue depth, worker heartbeat age, Redis reachability. |
| GET | `/api/v1/admin/settings` | editor | All settings incl. `isProtected`. |
| PUT | `/api/v1/admin/settings/{key}` | **admin** | Writes an `audit_logs` row with before/after. |

`admin` implicitly satisfies every role requirement.

**Session semantics to preserve exactly:** httpOnly; `secure` in production; `SameSite=Lax`;
absolute expiry 12 h **and** idle expiry 2 h (the idle window slides on each request);
`session_epoch` mismatch invalidates every session for that user at once.

### Worker (5) — bearer token

The desktop's only interface. Token compared against a stored SHA-256 digest in constant
time; a previous token stays valid during a rotation grace window.

| Method | Path | Notes |
|---|---|---|
| POST | `/api/v1/worker/heartbeat` | Updates liveness **and extends every lease held by that node** in the same round trip. |
| POST | `/api/v1/worker/jobs/claim` | `UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED)`. Only leases job types the runner advertises. |
| POST | `/api/v1/worker/jobs/{id}/complete` | **Idempotent** — completing an already-done job returns `{"status": "already_complete"}`, not an error. |
| POST | `/api/v1/worker/jobs/{id}/fail` | Retryable → requeued with exponential backoff `min(60 · 2^(attempts-1), 3600)`. Exhausted → `FAILED`. |
| GET | `/api/v1/worker/status` | Runner self-check. |

A job leased to a different node returns **409**, not 403 — it almost always means the
lease expired and was reaped, which is recoverable, not a permissions problem.

---

## 2. Cross-cutting behaviour

- Every response carries `X-Request-ID` (echoed from the request, or generated).
- Security headers on every response: `X-Content-Type-Options`, `X-Frame-Options`,
  `Referrer-Policy`, `Permissions-Policy`; HSTS in production.
- Unhandled `OperationalError` → **503** with `Retry-After: 30`, never 500. The driver
  message is logged, never returned — it contains the connection string.
- Validation failures → **422** with `{detail, errors[], requestId}`. Field names are
  returned; submitted values are not.
- CORS headers are emitted **only** when `CORS_ALLOWED_ORIGINS` is set. Empty means no
  CORS headers at all.

---

## 3. Database state at baseline

| | |
|---|---|
| Alembic revision | `bf45495a0cae` (head) |
| Tables | 32 (31 + `alembic_version`) |
| Foreign keys | 35 |
| Check constraints | 7 |
| Indexes | 119 |
| Extensions | `vector`, `pg_trgm`, `plpgsql` |
| Seeded rows | 8 categories, 4 roles, 10 settings, 1 user, 6 ad placements, 4 CTA templates, 1 disclosure |
| Articles | 0 — nothing published yet |

Full snapshot: `tests/baseline/_database.json`.

---

## 4. Migration governance

### Alembic is the only schema authority

**Alembic owns every schema change. Nothing else may generate a migration.**

If Drizzle is introduced it is for TypeScript access and type generation only:

- Use `drizzle-kit pull` (introspection) to derive types **from** the live schema.
- Never run `drizzle-kit generate` or `drizzle-kit push`.
- Add no `drizzle/migrations` directory. If one appears, it is a bug.
- A schema change means: write an Alembic migration → apply it → re-introspect.

Two migration authorities over one database produce divergent histories that are only
discovered when a deploy fails against a database neither tool fully understands.

### Security requirements for Node database access

The migration gives the Next.js server database credentials it does not have today.
This is the one real regression identified in the audit, so the safeguards are
mandatory, not aspirational:

- The database module is `server-only` — importing it from a client component must be a
  **build error**, not a code-review catch.
- No connection string, password or secret in any `NEXT_PUBLIC_*` variable, ever.
- Least-privilege database roles where practical: the web tier does not need `DROP`.
- Secrets only via environment variables; production secrets never in git.
- Every phase verifies no credential reaches the client bundle:
  `grep -r "postgres://\|postgresql://" apps/web/.next/static/` must return nothing.

---

## 5. Migration status

| Group | Endpoints | Implementation | Verified |
|---|---|---|---|
| `public` | 4 routes / 16 baseline cases | **Both** — Node serves them; FastAPI still live | baseline + live parity |
| `health` | 2 | FastAPI only | baseline |
| `admin` | 7 | FastAPI only | baseline |
| `worker` | 5 | FastAPI only | baseline |

Since Phase 2 the public endpoints exist in **both** implementations. Next.js route
handlers under `app/api/v1/public/` take precedence over the `afterFiles` rewrite in
`next.config.ts`; everything else still falls through to FastAPI. Nothing was deleted.

> **The two implementations can drift, and the tests are what stop it.** They currently
> share a query layer, but that is a convention, not a guarantee — nothing prevents
> someone editing the Python route, the SQL, or the serialiser on one side only.
> `compare` and `parity` are the compatibility authority and are **permanent regression
> protection**, not scaffolding to be removed once a migration lands. Run them in CI and
> before every release for as long as both implementations exist.

Three commands, three different questions:

```bash
# Is FastAPI still behaving as captured?
uv run python infrastructure/scripts/api_baseline.py compare

# Does the Node implementation match the captured contract?
uv run python infrastructure/scripts/api_baseline.py compare --base-url http://127.0.0.1:3100 --group public

# Do the two live servers agree right now, on today's data?
uv run python infrastructure/scripts/api_baseline.py parity --group public
```

`parity` exists because the stored baseline was captured against an empty `articles`
table: it cannot pin pagination, ordering or article serialisation. Parity compares two
live servers, so temporary fixture data can exercise those paths. Phase 2 used seven
temporary articles to verify pagination across four pages, hero-image serialisation,
wrong-date 404s and wrong-category 404s, then removed them.

## 6. Session format — a migration contract

Captured before any auth migration, because Node must read exactly this. Verified by
`tests/test_session_lifecycle.py`.

**Redis key:** `session:<id>` where `<id>` is `secrets.token_urlsafe(32)` — the same
opaque value carried in the `thedrop_session` cookie. The cookie holds no user data and
no signature; the payload never leaves the server.

**Value:** a JSON object with exactly these six keys.

| Key | Type | Meaning |
|---|---|---|
| `user_id` | int | Internal `users.id`, not the public UUID |
| `email` | str | Convenience copy; the database is authoritative |
| `roles` | list[str] | Convenience copy; **re-read from the database every request**, so a revoked role takes effect immediately |
| `epoch` | int | Snapshot of `users.session_epoch` at login |
| `created_at` | str | ISO 8601 |
| `absolute_expiry` | str | ISO 8601, login + 12 h |

**Two independent expiries, both required:**

- **Idle, 2 h** — the Redis key TTL, refreshed on *every* authenticated request.
- **Absolute, 12 h** — `absolute_expiry` inside the payload, checked in application code.

A sliding TTL alone would let an active session live forever, which is why the second
one exists. Dropping the TTL refresh is the failure mode most likely to survive review:
sessions would simply expire two hours after login regardless of activity, and nobody
notices until two hours have passed.

**Rejection paths, all 401:**

| Condition | `detail` | Side effect |
|---|---|---|
| No cookie, or empty cookie | `Not authenticated` | none |
| Key absent from Redis | `Session expired` | none |
| `absolute_expiry` in the past | `Session expired` | key deleted |
| User missing or inactive | `Account unavailable` | key deleted |
| `epoch` ≠ `users.session_epoch` | `Session invalidated` | key deleted |

**Cookie flags** (pinned in `tests/baseline/auth_login_contract.json`): `httponly=true`,
`samesite=lax`, `path=/`, `max-age=43200`, `secure` in production only, `domain` from
`COOKIE_DOMAIN`.

> Authenticated baselines redact `email` — the admin's login identifier is half a
> credential pair and does not belong in version control. `user_id` and the public UUID
> are captured as-is; if the database is reseeded they change and `auth_me` will diff,
> which is a legitimate signal to re-capture rather than a regression.

## 7. Rollback

The hybrid architecture is tagged. Returning to it is one command:

```bash
git checkout v0.1.0-hybrid
```

To reset `main` to it after a failed migration phase:

```bash
git reset --hard v0.1.0-hybrid
```

The database is **not** rolled back by either command, and does not need to be: the
migration phases add no schema changes. If a phase ever does add one, it gets its own
Alembic revision and its own documented downgrade path.

Verify a rollback succeeded:

```bash
uv run pytest -q && uv run python infrastructure/scripts/api_baseline.py compare
```

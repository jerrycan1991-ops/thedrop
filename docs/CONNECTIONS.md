# PostgreSQL connection budget

Documented in Phase 3B-Prep. **Nothing here is implemented** — no pool size was changed
and no pooler was added. This records the current state and the recommended strategy so
the decision can be made deliberately, and separately from the authentication migration.

Verified against commit `c375cd6`.

---

## 1. Current configured ceilings

| Tier | Processes | Pool per process | Ceiling | Configured in |
|---|---|---|---|---|
| FastAPI | 2 (`uvicorn --workers 2`) | `pool_size=10` + `max_overflow=5` | **30** | `packages/database/src/thedrop_database/session.py` |
| Celery | 2 (prefork children) | `pool_size=10` + `max_overflow=5` | **30** | same |
| Next.js | 1 per instance | `DATABASE_POOL_MAX=5` | **5 × N** | `apps/web/lib/db/client.ts` |
| Alembic | 1, during deploy only | ~1–2 | **2** | pre-deploy command |
| | | **Worst case (1 web instance)** | **67** | |
| | | **`max_connections`** | **60** | `infrastructure/docker/docker-compose.yml` |

Both Python tiers share one `create_engine` call, so a change to `session.py` moves the
API and the worker together — worth knowing before tuning either.

---

## 2. Why 67 exceeds 60

Three multiplications, none of them obvious from reading a single file.

**Uvicorn workers are processes, not threads.** `--workers 2` forks two independent
Python processes. `_build_engine` is `lru_cache`d *per process*, so each builds its own
pool: 2 × (10 + 5) = 30.

**Celery's default pool is prefork.** `--concurrency=2` means two child processes, each
with its own engine — another 30. This is the surprising one: a worker that is idle
almost all the time is provisioned for half the database's capacity. It is also the
cheapest thing to fix, because those children rarely need more than one connection each.

**Overflow is not headroom, it is a second ceiling.** `max_overflow=5` permits five
connections *beyond* `pool_size` under load. The steady state is 10 per process; the
worst case is 15, and the worst case is what has to fit.

Adding the tiers: 30 + 30 + 5 + 2 = **67 against a ceiling of 60**.

### Why nothing is failing today

Pools open connections lazily. A live check during this audit found **2 open
connections** against `thedrop`. Every tier would have to be under simultaneous load to
approach its ceiling.

That is exactly what makes it dangerous. This is a load-spike failure, not a steady-state
one: it will first appear under the traffic conditions where you least want it, as
`FATAL: sorry, too many clients already` — which surfaces as a total outage across every
tier at once, not as a slow query.

The development database is unaffected (`max_connections=100`, no explicit limit in
`docker-compose.dev.yml`), so this cannot be reproduced locally without changing that
setting first.

---

## 3. Recommended strategy — awaiting approval

### Step 1 — right-size the pools (no new services)

| Tier | Now | Proposed | Ceiling after |
|---|---|---|---|
| FastAPI | 10 + 5 × 2 workers | `pool_size=5`, `max_overflow=2` | 14 |
| Celery | 10 + 5 × 2 children | `pool_size=3`, `max_overflow=1` | 8 |
| Next.js | 5 | 3 | 3 × N |
| Alembic | 2 | 2 | 2 |
| | | **Worst case** | **~27 of 60** |

That is a configuration change in two files, reversible, and it leaves genuine headroom
for a second web instance. It should be done **on its own**, not bundled with the
authentication migration — mixing a connection change with an auth change means a
production incident has two candidate causes.

Verify by load-testing while watching:

```sql
SELECT application_name, state, count(*)
FROM pg_stat_activity WHERE datname = 'thedrop'
GROUP BY 1, 2 ORDER BY 3 DESC;
```

`application_name` is already set to `thedrop-web` on the Node pool; setting it on the
Python engines would make this query far more useful and is a one-line change.

### Step 2 — a pooler, before public launch and not before

Only once traffic justifies it:

- **Neon or Supabase** — use their built-in pooler endpoint. No service to run, no
  container to monitor. This is the preferred option.
- **Railway Postgres** — PgBouncer in transaction mode, as a separate service.

### The psycopg gotcha, worth knowing in advance

PgBouncer in **transaction** mode is incompatible with server-side prepared statements,
which psycopg3 uses by default. Without `prepare_threshold=None` in the connection args
you get intermittent `prepared statement "_pg3_0" already exists` errors under
concurrency — non-deterministic, load-dependent, and it looks like a driver bug rather
than a configuration one. Session mode avoids this but gives up most of the pooling
benefit.

### Why not simply raise `max_connections`

Each PostgreSQL backend costs several MB of RAM. On an 8 GB VPS already running
Postgres, Redis, Next.js, FastAPI and a worker, raising the limit trades a clean failure
for the OOM killer choosing a victim — which is worse, because it takes down a process
that had nothing to do with the load.

### Serverless is the real pressure

Every warm Vercel instance holds its own pool, so `N` is not a number you control. Under
a traffic spike Vercel scales out and the connection count scales with it. Pool
right-sizing buys time; a pooler is the actual answer, and it becomes necessary at the
point the site gets real traffic rather than at any particular article count.

---

## 4. What was NOT done

- No pool size changed.
- No PgBouncer, no pooler, no new service.
- No `max_connections` change.
- No connection tuning mixed into the authentication work.

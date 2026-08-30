# Railway service configuration

Two services, one image, one database, one Redis. Both services point at
`infrastructure/docker/Dockerfile.api`; the start command is what differentiates them.

| Service | Config path (set in Settings → Config-as-code) | Public? |
|---|---|---|
| `thedrop-api` | `infrastructure/railway/api.json` | Yes — `api.thedrop.channel` |
| `thedrop-worker` | `infrastructure/railway/worker.json` | **No** — never expose it |
| `Postgres` | Railway plugin (`pgvector` image, see below) | No |
| `Redis` | Railway plugin | No |

---

## Three things that will bite if you skip them

### 1. Migrations run on the API service only

`api.json` declares a `preDeployCommand`. Railway runs it once, before the new version
takes traffic, and **aborts the deploy if it fails** — so a broken migration never
reaches a live container.

The worker deliberately has no `preDeployCommand`. If both services migrated, two
containers would race `alembic upgrade head` on the same database during every deploy.
Alembic takes a lock, so one would simply fail and fail the deploy with it.

### 2. The worker must stay at exactly one replica

`worker.json` starts Celery with `-B`, which embeds the beat scheduler. Two replicas
means every scheduled task fires twice — duplicate ingestion, duplicate publishing,
duplicate spend. `numReplicas` is pinned to 1 for this reason. If you ever need to
scale the worker, split beat into its own service **first**.

### 3. Postgres needs pgvector — the default image does not have it

Railway's standard Postgres has no `vector` extension, and migration `0001` will fail
with `could not open extension control file`. Deploy Postgres from the
`pgvector/pgvector:pg16` image instead (Railway → New → Docker Image), or use their
pgvector template. Verify before your first deploy:

```sql
SELECT * FROM pg_available_extensions WHERE name = 'vector';
```

---

## Wiring services together

Use Railway's reference variables rather than pasting connection strings. They resolve
to the **private** network, which keeps database traffic off the public internet and
avoids egress charges:

```
DATABASE_URL = postgresql+psycopg://${{Postgres.PGUSER}}:${{Postgres.PGPASSWORD}}@${{Postgres.RAILWAY_PRIVATE_DOMAIN}}:5432/${{Postgres.PGDATABASE}}
REDIS_URL    = ${{Redis.REDIS_URL}}
```

Note the `+psycopg` driver prefix. Railway's own `DATABASE_URL` is a bare
`postgresql://`, which SQLAlchemy resolves to psycopg2 — not installed here. Copying
Railway's variable verbatim produces `ModuleNotFoundError: No module named 'psycopg2'`.

## Health checks

- `healthcheckPath` is `/readyz`, not `/healthz`. `/healthz` only proves the process is
  alive; `/readyz` proves it can reach Postgres and Redis and that migrations are at
  head. A container that cannot serve should not receive traffic.
- The Dockerfile's own `HEALTHCHECK` uses `/healthz` — that one is about process
  liveness for the container runtime, which is a different question.

## Cost note

Both services and both databases run 24/7. The worker is idle most of the time but
cannot scale to zero — it holds the beat schedule. Budget for it.

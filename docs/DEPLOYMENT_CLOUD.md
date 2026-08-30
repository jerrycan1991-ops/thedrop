# THE DROP — Cloud deployment (Vercel + Railway)

Target: `https://thedrop.channel`

This is the **managed-platform** deployment path. The single-VPS path is still
documented in [DEPLOYMENT.md](DEPLOYMENT.md) and remains valid — see ADR-0010 for the
trade-offs between them. Nothing here has been deployed; no DNS has been changed.

---

## 1. Topology

```
                      thedrop.channel  (Vercel)
                               |
                    +----------+----------+
                    | Next.js 15          |
                    |  * public site, ISR |
                    |  * /admin           |
                    |  * /api/v1/* -------+---- rewrite ----+
                    +---------------------+                 |
                                                            v
                                          api.thedrop.channel (Railway)
                                          +--------------------------+
                                          | thedrop-api (FastAPI)    |
                                          |  public / admin / worker |
                                          +--------------------------+
                                                 |            |
                                    private net  |            |  private net
                                                 v            v
                                   +---------------+   +--------------+
                                   | Postgres 16   |   | Redis 7      |
                                   | + pgvector    |   |              |
                                   +---------------+   +--------------+
                                                 ^            ^
                                          +--------------------------+
                                          | thedrop-worker (Celery)  |
                                          | ingest|maintain|publish  |
                                          | NOT publicly exposed     |
                                          +--------------------------+
                                                      ^
                                       HTTPS, outbound-initiated only
                                                      |
                                    RTX 4070 SUPER desktop (agent-runner)
                                    unchanged: it polls a public URL,
                                    and does not care which host serves it
```

The desktop worker contract (ADR-0001) is untouched. It makes outbound HTTPS calls to
`/api/v1/worker/*`; whether that is a VPS or Railway is irrelevant to it.

---

## 2. Recommended services

| Component | Service | Plan notes |
|---|---|---|
| Next.js frontend | **Vercel** | Hobby works for launch; Pro when you need team seats or longer function timeouts. |
| FastAPI | **Railway** service, Dockerfile build | ~512 MB–1 GB. |
| Celery worker | **Railway** service, same image | ~512 MB. Pinned to 1 replica. |
| PostgreSQL + pgvector | **Railway**, `pgvector/pgvector:pg16` image | **Not** the default Postgres — it lacks the `vector` extension. |
| Redis | **Railway** Redis plugin | 256 MB is plenty. |
| Media/object storage | **Cloudflare R2** or **AWS S3** | Required before Phase 6. See §8. |

**Why Railway over Vercel functions for the API:** the Celery worker holds an embedded
beat scheduler and must run continuously. That is not a serverless workload, and
splitting the API onto a third platform to avoid one long-running container buys
nothing.

---

## 3. Environment variables

### 3.1 Vercel — project `thedrop-web`

| Variable | Value | Scope |
|---|---|---|
| `API_INTERNAL_URL` | `https://api.thedrop.channel` | Production |
| `NEXT_PUBLIC_SITE_URL` | `https://thedrop.channel` | Production |
| `NEXT_PUBLIC_ADS_ENABLED` | `false` | Production |

That is the complete list. The web app holds **no** database credential and **no**
API secret — it reads through the API (ADR-0006), which is exactly why a frontend
compromise cannot reach Postgres.

### 3.2 Railway — service `thedrop-api`

| Variable | Value |
|---|---|
| `ENVIRONMENT` | `production` |
| `LOG_LEVEL` | `INFO` |
| `SITE_URL` | `https://thedrop.channel` |
| `SITE_NAME` | `The Drop` |
| `DATABASE_URL` | `postgresql+psycopg://${{Postgres.PGUSER}}:${{Postgres.PGPASSWORD}}@${{Postgres.RAILWAY_PRIVATE_DOMAIN}}:5432/${{Postgres.PGDATABASE}}` |
| `REDIS_URL` | `${{Redis.REDIS_URL}}` |
| `CELERY_BROKER_URL` | `${{Redis.REDIS_URL}}` |
| `CELERY_RESULT_BACKEND` | `${{Redis.REDIS_URL}}` |
| `SESSION_SECRET` | generate — see below |
| `CORS_ALLOWED_ORIGINS` | `https://thedrop.channel,https://www.thedrop.channel` |
| `TRUSTED_HOSTS` | `api.thedrop.channel` |
| `COOKIE_DOMAIN` | `.thedrop.channel` |
| `WEB_CONCURRENCY` | `2` |
| `PUBLISHING_ENABLED` | `false` |
| `AI_ENABLED` | `false` |
| `ADS_ENABLED` | `false` |
| `AFFILIATE_ENABLED` | `false` |

Generate the session secret with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Settings validation **rejects** a placeholder or short `SESSION_SECRET` when
`ENVIRONMENT=production`, so a forgotten value fails the deploy rather than shipping a
guessable session signer.

> `DATABASE_URL` must carry the `+psycopg` driver prefix. Railway's own variable is a
> bare `postgresql://`, which SQLAlchemy maps to psycopg2 — not installed. Copying it
> verbatim gives `ModuleNotFoundError: No module named 'psycopg2'`.

**Set once, then delete:** `ADMIN_EMAIL` and `ADMIN_INITIAL_PASSWORD`, used only by the
seed. Remove both from the service after the first seed so a live admin password is not
sitting in the platform environment.

### 3.3 Railway — service `thedrop-worker`

Same variables as the API, minus `CORS_ALLOWED_ORIGINS`, `TRUSTED_HOSTS`,
`COOKIE_DOMAIN` and `WEB_CONCURRENCY` (it serves no HTTP).

### 3.4 Not committed

`.env` is gitignored and has been verified absent from git history. `.dockerignore`
excludes it from the build context, so it cannot be baked into an image layer. Only
`.env.example` — names and shapes, no values — is tracked.

---

## 4. Deployment commands

### 4.1 Push the repository

```bash
git remote add origin <YOUR_REPO_URL> && git push -u origin main
```

### 4.2 Verify the image builds (run locally first)

```bash
docker build -f infrastructure/docker/Dockerfile.api -t thedrop-api:test .
```

```bash
docker run --rm -e ENVIRONMENT=development -e PORT=8080 -p 8080:8080 thedrop-api:test
```

Expect `/healthz` on `http://127.0.0.1:8080` to return `{"status":"ok",...}`.
`/readyz` will report `degraded` — correct, it has no database in this context.

### 4.3 Railway

```bash
npm i -g @railway/cli && railway login
```

```bash
railway init --name thedrop
```

Then in the dashboard, in this order:

1. **Postgres** — New → Docker Image → `pgvector/pgvector:pg16`. Confirm:
   `SELECT * FROM pg_available_extensions WHERE name='vector';`
2. **Redis** — New → Database → Redis.
3. **`thedrop-api`** — New → GitHub repo. Settings → Config-as-code path:
   `infrastructure/railway/api.json`. Add the §3.2 variables.
4. **`thedrop-worker`** — New → same repo. Config-as-code path:
   `infrastructure/railway/worker.json`. Add the §3.3 variables. **Do not** generate a
   public domain for it.
5. `thedrop-api` → Settings → Networking → Custom Domain → `api.thedrop.channel`.

Migrations run automatically: `api.json` declares a `preDeployCommand` that runs
`alembic upgrade head` before the new version takes traffic, and **aborts the deploy if
it fails**. The worker deliberately does not migrate — two services racing
`alembic upgrade head` on every deploy would deadlock one of them.

Seed once, after the first successful deploy:

```bash
railway run --service thedrop-api python -m thedrop_database.seed
```

### 4.4 Vercel

```bash
npm i -g vercel && vercel login
```

```bash
cd apps/web && vercel link
```

In the project settings: **Root Directory** = `apps/web`, and enable **Include source
files outside of the Root Directory**. This is a pnpm workspace — the web app imports
`@thedrop/config` and `@thedrop/shared`, which live outside `apps/web`, so a build
scoped to that folder alone cannot resolve them.

Add the §3.1 variables, then:

```bash
vercel --prod
```

---

## 5. DNS records for `thedrop.channel`

Add these at your registrar **after** both platforms report their targets. Confirm each
value in the respective dashboard — providers do change them, and a stale IP copied
from documentation is a broken site.

| Type | Name | Value | TTL | Purpose |
|---|---|---|---|---|
| `A` | `@` | value shown in Vercel → Domains (currently documented as `76.76.21.21`) | 3600 | Apex → Vercel |
| `CNAME` | `www` | `cname.vercel-dns.com` | 3600 | www → Vercel |
| `CNAME` | `api` | `<your-service>.up.railway.app` (from Railway → Networking) | 3600 | API → Railway |
| `CAA` | `@` | `0 issue "letsencrypt.org"` | 3600 | Restrict who may issue certs |
| `CAA` | `@` | `0 issue "pki.goog"` | 3600 | Google Trust Services (Vercel) |

Both platforms provision TLS automatically once the records resolve. Do not add an
`AAAA` record unless the dashboard gives you one.

If your DNS is on Cloudflare, set the `api` record to **DNS only** (grey cloud) during
setup — proxying before Railway has issued its certificate causes a redirect loop.

Verify before switching anything live:

```bash
dig +short thedrop.channel && dig +short api.thedrop.channel
```

```bash
curl -sS https://api.thedrop.channel/readyz
```

---

## 6. HTTPS and cookies — how this actually works

- **TLS** is terminated by Vercel and Railway. Both redirect HTTP→HTTPS automatically.
- **`secure` cookies**: the API sets `secure=True` whenever `ENVIRONMENT=production`.
  Uvicorn runs with `--proxy-headers`, so it trusts the platform's `X-Forwarded-Proto`
  and correctly sees the request as HTTPS. Without that flag the app would believe it
  was serving plain HTTP.
- **`SameSite=Lax`, not `None`.** `thedrop.channel` and `api.thedrop.channel` share a
  registrable domain, so requests between them are *same-site*. Lax cookies are sent,
  and we keep the CSRF protection that `SameSite=None` would throw away.
- **`COOKIE_DOMAIN=.thedrop.channel`** lets the cookie set by the API host be sent to
  the apex. Logout deletes with the same domain and path — mismatched attributes are
  the classic cause of "logout leaves you logged in".
- **HSTS**: two years with `includeSubDomains`, from both the API and the web app.
  `preload` is deliberately **not** set — getting off the preload list takes months, so
  add it only once every subdomain can serve HTTPS permanently.

---

## 7. Health checks

| Endpoint | Answers | Used by |
|---|---|---|
| `/healthz` | Is the process alive? | Docker `HEALTHCHECK` |
| `/readyz` | Can it reach Postgres and Redis, and are migrations at head? | Railway `healthcheckPath` |

Railway gates on `/readyz`, not `/healthz` — a container that is running but cannot
reach its database should not receive traffic. `/readyz` returns `503` when degraded.

---

## 8. Known production gaps

Honest list. None of these block a launch with no articles yet; several block Phase 6.

| # | Issue | Severity | Fix |
|---|---|---|---|
| 1 | **Media on local disk.** ADR-0007 serves media from a symlinked `public/media`. Vercel's filesystem is ephemeral and read-only at runtime; a Railway container loses it on redeploy. | **Blocks Phase 6** | Implement `S3CompatibleStorage` behind the existing `MediaStorage` interface, point it at Cloudflare R2, and add the bucket host to `images.remotePatterns`. |
| 2 | **CSRF is documented but not implemented.** SECURITY.md §4 describes a double-submit token; no code enforces it. `SameSite=Lax` blocks the common cross-site POST, so this is defence-in-depth rather than an open hole — but the doc currently overstates reality. | High | Implement the double-submit token, or amend SECURITY.md. Do not leave the doc claiming a control that does not exist. |
| 3 | **Rate limiting only covers admin login.** Public read endpoints and the worker endpoints have none, despite SECURITY.md §4 describing limits for both. | High | Redis token bucket middleware before opening the worker API to the internet. |
| 4 | **MFA is schema-only.** `mfa_enabled` / `mfa_secret_enc` exist; there is no TOTP flow. | High | Implement before the admin controls real revenue settings. |
| 5 | **`audit_logs` is not actually append-only.** DATABASE.md §9 specifies `INSERT`/`SELECT` grants only; the app role has full rights, so the audit trail is editable by the application. | Medium | Create a restricted role and grant accordingly in a migration. |
| 6 | **No Content-Security-Policy on the web app.** Other security headers are set; CSP is not. | Medium | Add a nonce-based CSP. Needs care with Next's inline scripts. |
| 7 | **Backups.** The VPS path had a nightly `pg_dump` cron and a rehearsed restore. Railway's backups are a different mechanism and are **not** configured by anything in this repo. | **High** | Enable Railway backups AND schedule an external `pg_dump` off-platform. A backup you have never restored is not a backup. |
| 8 | **Admin password is a known placeholder.** `changeme-on-first-login`. | **High** | Change it immediately after seeding, then delete `ADMIN_INITIAL_PASSWORD` from the environment. |
| 9 | **No worker-token issuance path.** Registering the desktop runner needs a `worker_nodes` row and a token; there is no CLI or endpoint yet. | Blocks Phase 2 | Add an admin endpoint or a management command. |
| 10 | **Vercel function egress to Railway.** Every uncached render crosses the public internet. Keep both in the same region (`iad1`) or p95 will suffer. | Medium | Region-pin, and lean on ISR. |

---

## 9. Rollback

| Scenario | Action |
|---|---|
| Bad frontend deploy | Vercel → Deployments → previous → Promote to Production. Instant. |
| Bad API deploy | Railway → Deployments → previous → Redeploy. |
| Bad migration | `preDeployCommand` fails and the deploy aborts before traffic moves. If a migration applied and then broke something, restore from backup — never `alembic downgrade` in production without rehearsing it. |
| Runaway AI cost | Set `ai.enabled=false` in admin settings. Effective within 60s. |
| Need the site down | Vercel → Deployment Protection, or point DNS at a maintenance page. |

Because the two tiers deploy independently, **deploy the API before the frontend** when
a change spans both — an old frontend against a new API is usually fine; the reverse
usually is not.

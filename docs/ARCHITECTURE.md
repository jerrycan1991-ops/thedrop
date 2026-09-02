# THE DROP — Architecture

Status: **Phase 0 design document, partially superseded.** The Node-first migration
moved the public and admin HTTP layer into Next.js; see "Current state" below and
ADR-0010. The design reasoning here is retained because it still explains *why* the
system is shaped this way, but where it describes Next.js as a pure proxy it is out of
date.
Domain: https://thedrop.channel
Last updated: 2026-08-31

---

## 0. Current state (post-migration)

| | |
|---|---|
| Served by **Node** (Next.js route handlers) | 4 public read endpoints, 4 admin reads, login, logout — 10 total |
| Served by **FastAPI** (proxied through Next) | `PUT /api/v1/admin/settings/{key}`, 5 worker endpoints |
| FastAPI only (not proxied) | `/healthz`, `/readyz` |
| Database | Both tiers connect directly. **Alembic remains the sole schema authority** (ADR-0010) |
| Sessions | Redis, interchangeable between tiers in both directions |

Route ownership is declared by the rewrite list in `apps/web/next.config.ts` and
asserted by `tests/test_route_ownership.py`. It is deliberately **not** left to
framework precedence: a catch-all rewrite once shadowed a Node route handler that
existed and built correctly, and FastAPI served the endpoint unnoticed because both
tiers returned identical responses.

The rest of this document describes the original Phase 0 design.

---

## 1. Design constraints that drive everything

| Constraint | Consequence |
|---|---|
| VPS: 4 vCPU / 8 GB RAM, shared with an existing hosting panel | Public tier must be *lean*. No ML runtimes, no search cluster, no metrics stack. |
| Nginx is managed by the hosting panel | Phase 1 ships with **zero nginx changes**. Everything is reachable through the single existing proxy to `127.0.0.1:3100`. |
| Desktop: Ryzen 7 5700X3D / RTX 4070 SUPER 12 GB / 64 GB RAM | All embeddings, clustering, image and video generation, and most Claude orchestration run here. |
| Desktop must not be publicly reachable | Desktop is **outbound-only**. It polls the VPS. The VPS never dials the desktop. |
| Desktop may be offline for hours | The site, the database and the queue must be fully functional without it. Work waits; nothing breaks. |
| VPS is the source of truth | Postgres on the VPS holds all canonical state. The desktop holds only caches and model weights. |

**Governing principle:** the VPS is a *publishing and coordination* tier. It stores, serves, schedules and gates. It does not think. The desktop is the *newsroom*: it thinks, writes and renders.

---

## 2. Tier diagram

```
                            Internet
                               |
                     (hosting panel nginx, TLS)
                               |
                        127.0.0.1:3100
                               |
   +-----------------------------------------------------------+
   |  VPS - Ubuntu 24.04, 4 vCPU / 8 GB                         |
   |                                                            |
   |   +----------------------------------------------+         |
   |   | apps/web  - Next.js (systemd, :3100)         |         |
   |   |   * public site (SSR/ISR)                    |         |
   |   |   * /admin route group (auth-gated)          |         |
   |   |   * /api/* -> rewrite -> FastAPI             |         |
   |   |   * /media/* static from disk                |         |
   |   +----------------------------------------------+         |
   |                      | 127.0.0.1:8000                      |
   |   +----------------------------------------------+         |
   |   | services/api - FastAPI (systemd, :8000)      |         |
   |   |   * public read API   * admin API            |         |
   |   |   * worker lease API (claim/heartbeat)       |         |
   |   |   * auth, RBAC, audit log                    |         |
   |   +----------------------------------------------+         |
   |                      |                                     |
   |   +----------------------------------------------+         |
   |   | services/worker - ONE Celery process         |         |
   |   |   queues: ingest | maintain | publish        |         |
   |   |   embedded beat scheduler (-B)               |         |
   |   +----------------------------------------------+         |
   |                      |                                     |
   |   +----------------+   +----------------+                  |
   |   | PostgreSQL 16  |   | Redis 7        | (systemd, apt)   |
   |   | + pgvector     |   | broker + cache |                  |
   |   | 127.0.0.1:5432 |   | 127.0.0.1:6380 |                  |
   |   +----------------+   +----------------+                  |
   +-----------------------------------------------------------+
                               ^
                    HTTPS, outbound-initiated only
                    bearer token + request HMAC
                               |
   +-----------------------------------------------------------+
   |  DESKTOP - RTX 4070 SUPER (private, no inbound ports)      |
   |                                                            |
   |   agent-runner (long-poll loop)                            |
   |     |- embed        (bge-small-en-v1.5, GPU)               |
   |     |- cluster      (incremental centroid + HDBSCAN)       |
   |     |- score        (viral / opportunity / US relevance)   |
   |     |- extract      (entities, atomic claims)              |
   |     |- verify       (cross-source, Claude)                 |
   |     |- write        (Claude article generation)            |
   |     |- qa           (editorial QA, second-model review)    |
   |     |- image        (Flux/SDXL via ComfyUI)                |
   |     +- video        (ffmpeg / Remotion + TTS)              |
   |                                                            |
   |   Local: Ollama, ComfyUI, Piper/Kokoro TTS, ffmpeg         |
   +-----------------------------------------------------------+
```

---

## 3. What runs where — and why nothing else does

### 3.1 VPS process inventory (final for Phase 1)

| Process | Manager | Bind | Est. RSS |
|---|---|---|---|
| `thedrop-web` (Next.js standalone) | systemd | 127.0.0.1:3100 | ~350 MB |
| `thedrop-api` (uvicorn, 2 workers) | systemd | 127.0.0.1:8000 | ~350 MB |
| `thedrop-worker` (Celery, concurrency 2, `-B`) | systemd | — | ~400 MB |
| `postgres:16` + pgvector | systemd (apt) | 127.0.0.1:5432 | ~1.5 GB |
| `thedrop-redis` (dedicated instance) | systemd | 127.0.0.1:6380 | ~0.6 GB |
| OS + journald + panel nginx | — | — | ~0.8 GB |
| **Total** | | | **≈ 4.0 GB** |

Leaves ~4 GB for page cache and build spikes. A 4 GB swapfile is provisioned as a safety net (DEPLOYMENT.md §3).

### 3.2 Explicitly rejected components

Each line is a decision, not an oversight.

| Rejected | Why | What we do instead |
|---|---|---|
| Separate `ingestion-worker`, `ai-worker`, `media-worker`, `analytics-worker` services | 4 Python processes ≈ 1.6 GB for work that is 95 % idle | **One** Celery worker, multiple named queues. AI/media never run on the VPS at all. |
| Separate `apps/admin` Next.js server | A second Node process (~300 MB) and a second nginx vhost | `/admin` route group **inside** `apps/web`. Zero extra process, zero nginx change. ADR-0004. |
| Celery Beat as its own process | ~60 MB for a cron loop | Embedded beat (`celery worker -B`). Hard rule: this unit must never scale past 1 replica. |
| Elasticsearch / Meilisearch | 1–2 GB RAM for a site with <20 k articles/year | Postgres `tsvector` + GIN, plus pgvector for semantic search. |
| Prometheus + Grafana + exporters | ~700 MB for dashboards nobody watches at 3 a.m. | Structured JSON logs to journald; `/healthz` + `/metrics.json`; System Health page reads live from Postgres/Redis; external uptime pinger. |
| MinIO / self-hosted S3 | Another 400 MB and another failure domain | Media on local disk at `/var/www/thedrop/media`. S3-compatible offload is a config swap. ADR-0007. |
| Ollama / torch / ONNX on the VPS | 2–4 GB of ML deps on a 4-core box | VPS does **cheap** dedup only (canonical URL, SimHash of title+lede, `pg_trgm`). All embeddings computed on the desktop. ADR-0005. |
| Docker anywhere on the VPS | A third process manager on a panel-managed box, plus daemon overhead, for two services the distro packages well | Everything under systemd: Postgres and Redis from apt, app code native. ADR-0011 supersedes ADR-0002. |
| Kubernetes, service mesh, Kafka/NATS | Absurd at this scale | Redis + Postgres. |
| ~~Next.js route handlers as the real backend~~ | *Reversed.* The extra HTTP hop sat on the render path, and TTFB feeds Core Web Vitals and Google News eligibility | Next.js now owns the public and admin HTTP layer; Python keeps worker, queue and AI work. ADR-0010. |

---

## 4. Service boundaries

### 4.1 `apps/web` — Next.js (TypeScript, Tailwind, shadcn/ui)

Responsibilities:
- Server-render public pages. React Server Components by default; client components only for interactive islands.
- Own all SEO surface area: metadata, JSON-LD, sitemaps, RSS, robots.
- Host the `/admin` route group behind session auth.
- Proxy `/api/*` to FastAPI via `next.config.ts` rewrites, so FastAPI never binds a public interface and no nginx rule is needed.
- Serve `/media/*` from disk.

It does **not** talk to Postgres directly, run business rules, or hold secrets beyond a session-signing key and the internal API base URL.

> **Data access rule (SUPERSEDED).** This originally required the web app to read
> through the FastAPI API rather than its own DB client. Since the migration, Next.js
> queries PostgreSQL and Redis directly through `server-only` modules. Alembic is still
> the single schema authority; what changed is *access*, not *ownership*. The
> guarantees this rule used to provide, and what replaced each of them, are set out in
> ADR-0010.

### 4.2 `services/api` — FastAPI (Python 3.12)

Three logical routers, one process:

- **`/api/v1/public/*`** — read-only, cacheable, unauthenticated. Feeds the website.
- **`/api/v1/admin/*`** — session-cookie auth + RBAC. Feeds the admin dashboard.
- **`/api/v1/worker/*`** — bearer-token auth. The desktop's only interface: register, heartbeat, claim, extend, complete, fail, upload artifact.

Owns SQLAlchemy models, Alembic migrations, Pydantic schemas, authorization, rate limits, audit logging and cost accounting.

### 4.3 `services/worker` — Celery

| Queue | Work | Cadence |
|---|---|---|
| `ingest` | Poll providers, normalize, cheap-dedup, persist `RawArticle`, enqueue desktop jobs | 5–15 min per provider |
| `maintain` | Trend refresh, analytics rollups, sitemap regen, cache warm, retention sweeps, budget checks, **lease reaping** | 1 min – 1 h |
| `publish` | Promote approved articles to live, revalidate ISR paths, ping sitemaps, enqueue distribution | on demand |

Never runs model inference, image generation or video rendering.

### 4.4 `agent-runner` — desktop

Implemented in `services/agent-runner/`; see its README for provisioning and
operation. Tokens are minted by `python -m thedrop_database.provision_worker` on the
VPS and stored only as a SHA-256 digest.

One supervised Python process that long-polls `POST /api/v1/worker/jobs/claim`, dispatches to a handler for the returned job type, and posts results back. Handlers are pluggable. Capabilities are advertised at registration (`{"gpu": true, "vram_gb": 12, "handlers": [...]}`) so the VPS only leases jobs the runner can actually execute.

---

## 5. The desktop ↔ VPS contract

### 5.1 Why pull, not push

The desktop sits behind residential NAT with a dynamic IP and no inbound ports. A push model would require exposing the desktop, punching holes, or babysitting a persistent tunnel. A pull model needs nothing but outbound HTTPS, survives IP changes, reconnects for free, and is trivially firewalled. ADR-0001.

### 5.2 Job lifecycle

```
QUEUED --claim--> LEASED --complete--> DONE
   ^                 |
   |                 |--fail (retryable, attempts < max)--> QUEUED (backoff)
   |                 |--fail (permanent | attempts = max)--> FAILED -> dead letter
   +--lease expiry (reaper: no heartbeat for 3x interval)---+
```

- **Lease.** Claiming sets `leased_by`, `leased_at`, `lease_expires_at` in a single `UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED)`. Safe under concurrency with no distributed lock.
- **Heartbeat.** Every 30 s the runner extends its leases. A `maintain` task reaps expired leases every minute and returns those jobs to `QUEUED`.
- **Idempotency.** Every job carries an `idempotency_key`. Completion upserts on that key, so a job that finished exactly as its lease expired cannot produce a duplicate article.
- **Offline desktop.** Jobs accumulate in `QUEUED`. Nothing on the VPS blocks. Admin shows `AI DESKTOP: OFFLINE` after 2 missed heartbeats, with queue depth and oldest-job age.

### 5.3 Transport security

Bearer token (rotatable, hashed at rest) in `Authorization`, plus a per-request HMAC over `body + timestamp + nonce` to defeat replay. An optional WireGuard tunnel is supported but **not required** — the endpoint is designed to be safe on the public internet. Token rotation is overlap-capable: two tokens valid during the rotation window.

---

## 6. Request paths

**Public article read (cache hit):** `browser → nginx → Next.js (ISR HTML)`. No Python, no DB. Target p95 < 120 ms.

**Public article read (revalidation):** `Next.js → GET /api/v1/public/articles/{slug} → FastAPI → Redis → (miss) Postgres`.

**Admin action:** `browser → nginx → Next.js /admin → rewrite → FastAPI admin router → RBAC → Postgres → AuditLog`.

**Desktop job:** `agent-runner → HTTPS thedrop.channel/api/v1/worker/jobs/claim → nginx → Next.js rewrite → FastAPI → Postgres`.

> The Next.js rewrite hop costs ~2–4 ms and keeps FastAPI off the public interface with no nginx edit. If API volume ever makes that hop matter, the fix is one `location /api/` block added **through the hosting panel** — documented in DEPLOYMENT.md §7 as an optional, reversible optimization.

---

## 7. Data flow (condensed; full detail in PIPELINE.md)

```
providers --> RawArticle --cheap dedup--> Story (cluster stub)
                                            |
                              [desktop] embed + cluster + score
                                            |
                                    ViralSignal, scores
                                            |
                         [desktop] entity + claim extraction
                                            |
                              cross-source verification
                                            |
                                 Story Evidence Packet
                                            |
                                 [desktop] Claude writes
                                            |
                                editorial QA + confidence
                                            |
                             publishing gate (thresholds)
                                            |
                    Article --> PublicationQueue --> live + ISR revalidate
                                            |
                                media jobs (image / video)
                                            |
                              distribution + analytics
```

Every `[desktop]` arrow is a leased job. Every other arrow is VPS-local.

---

## 8. Failure modes and degradation

| Failure | Behavior |
|---|---|
| Desktop offline | Site fully live. Ingestion continues. Jobs queue. Admin shows OFFLINE. No publishing — by design, nothing ships unverified. |
| Redis down | Site serves from ISR cache. API degrades to direct Postgres reads. Celery stops; ingestion pauses. Alert fires. |
| Postgres down | Site serves stale ISR pages until revalidation fails, then 503 on dynamic routes. Hard alert. Restore from PITR. |
| Claude API down or over budget | Generation jobs fail retryable with backoff. Emergency AI kill-switch (`ai.enabled=false`) stops job *creation* without touching ingestion. |
| A provider errors | Per-provider circuit breaker. Other providers unaffected. `provider_errors` metric increments. |
| Next.js build OOM on VPS | Builds run on the desktop by default and ship as a verified bundle (`build-and-push.sh`). VPS-side builds remain available, memory-capped and swap-backed. |
| Disk fills with media | Retention sweep plus alert at 75 %. Offload to S3-compatible storage is a config change. |

---

## 9. Technology choices, fixed

| Layer | Choice | Note |
|---|---|---|
| Frontend | Next.js 15 (App Router), TypeScript, Tailwind v4, shadcn/ui | RSC-first; client JS budget < 90 KB gzip on article pages |
| Backend | Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.0, Alembic | |
| Database | PostgreSQL 16 + pgvector (HNSW) | `pgvector/pgvector:pg16` image |
| Queue | Celery 5 + Redis 7 | Redis is also cache and rate-limit store |
| Embeddings | `bge-small-en-v1.5`, 384-dim, desktop only | one shared vector space, ADR-0005 |
| LLM | Anthropic Claude, routed by task tier | Haiku → classify/extract; Sonnet → write; Opus → high-risk verify and second review |
| Local LLM | Ollama on desktop (cheap passes, offline fallback) | never on VPS |
| Images | ComfyUI + Flux.1-schnell / SDXL on RTX 4070 SUPER | 12 GB VRAM budget |
| Video | ffmpeg + Remotion compositions, local TTS | MEDIA_PIPELINE.md |
| Package mgmt | pnpm workspaces (JS), uv (Python) | |
| Process mgmt | systemd, for everything on the VPS | ADR-0011 |

Model IDs are configuration, never literals in code. See `packages/config`.

---

## 10. Open questions for the operator

1. Is `D:\trade` on the RTX 4070 SUPER desktop? (Assumed yes — it becomes both dev machine and AI newsroom.)
2. Does the hosting panel allow adding a subdomain (`admin.thedrop.channel`)? Not required, but preferred later.
3. Is Cloudflare or any CDN in front of the domain? Materially improves the media and caching strategy if so.
4. Postgres: containerized (recommended, chosen here), or reuse a panel-managed instance if one already exists?

---

## 11. ADR index

See `docs/adr/`.

- ADR-0001 — Desktop pulls work over HTTPS; VPS never dials the desktop
- ADR-0002 — Docker for stateful services only; systemd for app processes *(data-services half superseded by ADR-0011)*
- ADR-0003 — Two-tier queue: Celery on VPS, HTTP job-lease for desktop
- ADR-0004 — Admin lives inside `apps/web` as a route group
- ADR-0005 — Single 384-dim embedding space, computed only on the desktop
- ADR-0006 — FastAPI is the sole database owner *(superseded by ADR-0010)*
- ADR-0007 — Media on local disk behind a storage abstraction
- ADR-0008 — Untrusted source content is structurally isolated from instructions
- ADR-0009 — Affiliate product data carries per-field provenance; adapters are network-agnostic
- ADR-0010 — Node and FastAPI share direct database access; Alembic remains the sole schema authority
- ADR-0011 — PostgreSQL and Redis run natively under systemd; no Docker on the VPS
- ADR-0012 — Unprivileged deployment: managed Postgres, user-space Redis, PM2
- ADR-0013 — A source is a hostname; independence is a separate judgement

# THE DROP — Phase 1 Task Breakdown

Goal: a live, themed, database-backed website at `https://thedrop.channel` with a protected admin shell — and **zero AI, zero providers, zero ads**.

Phase 1 is complete when the site is boring, correct and deployed.

Legend: `[L]` = local machine (`D:\trade`), `[V]` = VPS.

---

## Work packages

### T1.0 — Repo skeleton `[L]`
`/` — pnpm workspace, `uv` workspace, `.gitignore`, `.editorconfig`, `.env.example`, `README.md`, `CLAUDE.md`, git init + first commit.
**Done when:** `pnpm -r list` and `uv sync` both succeed on a clean clone.

### T1.1 — Shared config and design tokens `[L]`
`packages/config` — typed env loading (zod for TS, pydantic-settings for Python), single source for thresholds, feature flags, model routing.
`packages/shared` — TS types shared between web and admin UI (article, category, media shapes).
**Done when:** a missing required env var fails fast at startup with a readable message, not a runtime `undefined`.

### T1.2 — Database package and migrations `[L]`
`packages/database` — SQLAlchemy 2.0 models for the Phase 1 subset: `categories`, `tags`, `article_tags`, `sources`, `providers`, `articles`, `article_versions`, `article_source_refs`, `media_assets`, `users`, `roles`, `user_roles`, `audit_logs`, `newsletter_subscribers`, `jobs`, `worker_nodes`, `settings`.
Alembic initialized; migration 0001 creates `vector` and `pg_trgm` extensions, all Phase 1 tables, indexes, and enum types. Seed script inserts the 7 categories, the `settings` defaults, and one admin user.
**Done when:** `alembic upgrade head` then `alembic downgrade base` then `upgrade head` runs clean on an empty DB.

> Full-schema note: the remaining entities (stories, claims, viral signals, video assets, ai_runs…) land in Phases 2–6 as separate migrations. Phase 1 deliberately does not create tables nothing reads yet.

### T1.3 — FastAPI service `[L]`
`services/api` — app factory, settings, structured JSON logging with `request_id`, exception handlers, CORS off by default, security headers.
Routers: `GET /healthz`, `GET /readyz`, `/api/v1/public/{categories,articles,article/{slug}}`, `/api/v1/admin/{auth,me,articles,settings}`, `/api/v1/worker/{register,heartbeat}` (lease endpoints stubbed for Phase 2).
Auth: argon2id, session records in Redis, httpOnly cookies, CSRF double-submit, login rate limit, RBAC dependency.
**Done when:** `/readyz` returns 200 only with DB + Redis up and migrations at head; unauthenticated admin routes return 401; every admin route has an authz dependency (asserted by test).

### T1.4 — Celery worker `[L]`
`services/worker` — celery app, three queues, embedded beat, one real periodic task (`maintain.heartbeat_sweep`) and one no-op ingest placeholder.
**Done when:** worker starts, beat fires the sweep, and `/api/v1/admin/system/metrics` reports queue depth.

### T1.5 — Design system `[L]`
`apps/web` — Tailwind v4 + shadcn/ui. CSS custom properties for every token: `--bg`, `--bg-elevated`, `--bg-sunken`, `--fg`, `--fg-muted`, `--fg-subtle`, `--border`, `--border-strong`, `--accent`, `--accent-fg`, `--breaking`, `--positive`, `--warning`, `--danger`, plus per-category accent tokens.
Three modes: dark (default), light, system — via `class` strategy plus a no-flash inline script reading `localStorage` then `prefers-color-scheme`.
Typography: variable display face + readable body face, self-hosted from an open license (Inter / Geist / Instrument Sans class), `font-display: swap`, subset to latin. **No unlicensed font files.**
**Done when:** a Storybook-less token page at `/_design` renders every token in both modes; no component contains a raw hex value (lint rule + test).

### T1.6 — Brand placeholders `[L]`
The "D" mark as inline SVG (geometric, high-contrast, legible at 16 px), wordmark lockup, favicon set, app icons, OG default image, video watermark asset.
**Done when:** favicon renders correctly at 16/32/180/512; the mark is recognizable in a 24 px avatar crop.
**Marked clearly as placeholder** — `docs/BRAND.md` states no trademark claim is made and final marks require design and clearance.

### T1.7 — Public routes and layout `[L]`
`/`, `/trending`, `/politics`, `/entertainment`, `/sports`, `/business`, `/technology`, `/world`, `/latest`, `/search`, `/about`, `/contact`, `/editorial-policy`, `/corrections`, `/privacy`, `/terms`, and `/{category}/{yyyy}/{mm}/{dd}/{slug}`.
Homepage modules: top nav + logo, breaking strip, hero story, trending rail, latest list, category modules, most read, opinion/analysis module, newsletter block, ad slot placeholders (rendering nothing), footer.
RSC by default; client components only for the theme toggle, mobile nav and search box.
**Done when:** every route returns 200 with real data from Postgres via the API; 404 for unknown slugs; client JS on an article page < 90 KB gzip.

### T1.8 — Article template `[L]`
Header (category, headline, dek, byline, timestamps, article-type label), hero media, body block renderer, key facts, source references, corrections notice, share row, related stories, ad slot positions.
Article-type labels (`NEWS` / `ANALYSIS` / `OPINION` / `COMMENTARY`) are visually distinct and always rendered — the labeling discipline is in the template from day one, not retrofitted.
**Done when:** a seeded article renders completely in both themes at 360 px, 768 px and 1440 px.

### T1.9 — Admin shell `[L]`
`apps/web/app/(admin)` — login page, session middleware, layout with the full section nav (Dashboard, Incoming Stories, Viral Radar, Story Clusters, Drafts, Published, Rejected, Articles, Media, Videos, Sources, Providers, Analytics, Revenue, AI Costs, API Costs, Corrections, Prompt Versions, Experiments, System Health, Logs, Settings).
Phase 1 implements Dashboard (real health metrics), Articles (list/edit/publish), Settings, System Health, Logs. The rest are routed placeholders stating which phase fills them.
**Done when:** unauthenticated access to any `/admin/*` path redirects to login; admin code is not in the public route bundles.

### T1.10 — Media serving `[L/V]`
`/media` symlink into the web public dir, `MediaStorage` interface with `LocalDiskStorage`, image derivative generation.
**Done when:** an image written to `/var/www/thedrop/media/...` is served at `https://thedrop.channel/media/...` with a long cache header — no nginx change.

### T1.11 — Testing and CI `[L]`
pytest (unit, DB via testcontainers or a disposable compose DB, API, auth, RBAC coverage), Vitest + Testing Library (components, tokens), Playwright smoke (homepage, article, theme toggle, admin login). GitHub Actions running lint, typecheck, tests, and `gitleaks`.
**Done when:** CI is green on a clean clone; coverage reported; no test hits a live external API.

### T1.12 — Deployment `[V]`
Compose file, systemd units, `deploy.sh` with health gate and auto-rollback, backup cron, restore drill.
**Done when:** the deploy script runs twice consecutively with no manual intervention, and a deliberately broken build triggers automatic rollback.

---

## Execution sequence

### Step 1 — Local prerequisites `[L]`

Working directory: `D:\trade`

```bash
node -v && python --version && git --version
```
Expected: `v24.x`, `Python 3.13.x` (uv will pin 3.12 for the project), `git version 2.x`.

**Docker Desktop is required locally and is not currently installed.** Install it (with the WSL2 backend) before Step 3, or Phase 1 local development has no database.

```bash
corepack enable && corepack prepare pnpm@latest --activate && pnpm -v
```
Expected: a pnpm version prints.

```bash
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```
Expected: uv installed; reopen the shell, then `uv --version` prints.

### Step 2 — Scaffold the repository `[L]`

Directory: `D:\trade`
This is the work of T1.0–T1.2 and is the first thing I will do on your approval. It creates files only — nothing is started, nothing is deployed.

**Verification:** `git status` shows a clean tree after the initial commit; `pnpm install` and `uv sync` both succeed.
**Rollback:** `git reset --hard <initial-sha>` — or delete `D:\trade` and start over. Nothing outside this directory is touched.

### Step 3 — Local data services `[L]`

Directory: `D:\trade`

```bash
docker compose -f infrastructure/docker/docker-compose.dev.yml up -d
```
Expected: `postgres` and `redis` containers healthy within ~20 s.

**Verification:**
```bash
docker compose -f infrastructure/docker/docker-compose.dev.yml ps
```
Both services `running (healthy)`.

**Rollback:**
```bash
docker compose -f infrastructure/docker/docker-compose.dev.yml down -v
```
(`-v` also drops the dev volume — safe locally, never on the VPS.)

### Step 4 — Migrate and seed `[L]`

Directory: `D:\trade`

```bash
uv run alembic upgrade head
```
Expected: `Running upgrade  -> 0001_initial`, no errors.

```bash
uv run python -m services.api.scripts.seed
```
Expected: `seeded 7 categories, 1 admin user, 12 sample articles`.

**Verification:**
```bash
uv run python -c "from sqlalchemy import create_engine,text;import os;print(create_engine(os.environ['DATABASE_URL']).connect().execute(text('select count(*) from articles')).scalar())"
```
Expected: `12`.

**Rollback:** `uv run alembic downgrade base` (local only).

### Step 5 — Run locally `[L]`

Directory: `D:\trade`

```bash
pnpm dev
```
Expected: Next.js on `http://localhost:3100`, FastAPI on `http://localhost:8000`, worker attached — one command, via `turbo`/`concurrently`.

**Verification:** `http://localhost:3100` renders the homepage in dark mode; the theme toggle switches to light with no flash; `http://localhost:3100/admin` redirects to login; `http://localhost:8000/readyz` returns `{"status":"ok","db":true,"redis":true,"migrations":"head"}`.

**Rollback:** Ctrl-C. Nothing persistent changed.

### Step 6 — Tests `[L]`

```bash
pnpm test && uv run pytest -q
```
Expected: all green. This gate must pass before anything touches the VPS.

### Step 7 — VPS provisioning `[V]` — *you run these*

Follow DEPLOYMENT.md §3 in order (base packages, swap, UFW, Docker, Node, uv, service account). Every block is idempotent and none of them touch nginx.

**Verification after §3:** `free -h` shows 4 GB swap; `sudo ufw status` shows only 22/80/443; `docker --version`, `node -v`, `uv --version` all print.
**Rollback:** `sudo swapoff /swapfile && sudo rm /swapfile` (and remove the fstab line); `sudo ufw disable`; `sudo apt remove docker-ce nodejs`. Nothing here is destructive to the existing panel or sites.

### Step 8 — First deploy `[V]`

```bash
sudo -u thedrop git clone <repo-url> /opt/thedrop
```

```bash
sudo install -m 640 -o root -g thedrop /dev/null /etc/thedrop/thedrop.env
```
Then populate it from `.env.example` with real values (see DEPLOYMENT.md §11 — four secrets needed for Phase 1).

```bash
cd /opt/thedrop && sudo -u thedrop bash infrastructure/scripts/deploy.sh
```
Expected tail:
```
[deploy] build ok
[deploy] migrations at head
[deploy] health gate: web 200, api 200
[deploy] recorded last-good sha <sha>
```

**Verification:**
```bash
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:3100/ && curl -sS http://127.0.0.1:8000/readyz && curl -sS -o /dev/null -w '%{http_code}\n' https://thedrop.channel/
```
Expected: `200`, the readyz JSON, `200`.

```bash
systemctl is-active thedrop-web thedrop-api thedrop-worker
```
Expected: three × `active`.

**Rollback:**
```bash
cd /opt/thedrop && sudo -u thedrop git checkout $(cat .last_good_sha) && sudo -u thedrop bash infrastructure/scripts/deploy.sh
```
If the site must go down cleanly instead: `sudo systemctl stop thedrop-web` (nginx then returns 502; the panel's static maintenance page is the nicer option if it has one).

### Step 9 — Post-deploy checks `[V]`

```bash
sudo -u thedrop docker exec thedrop-postgres-1 pg_dump -U thedrop -Fc thedrop > /var/backups/thedrop/initial.dump && ls -lh /var/backups/thedrop/
```
Expected: a non-trivial dump file. Then run the restore drill (DEPLOYMENT.md §9) once.

```bash
free -h && df -h / && systemctl status thedrop-web --no-pager | head -20
```
Expected: > 3 GB available memory, disk under 50 %, web unit active with a low restart count.

---

## Phase 1 definition of done

- [ ] `https://thedrop.channel` live from `127.0.0.1:3100`, **no nginx modified**
- [ ] All 16 route groups render; article route works with a real DB record
- [ ] Dark / light / system theming, tokenized, no flash, no hardcoded colors
- [ ] Admin login + session + RBAC; `/admin` protected
- [ ] Postgres + pgvector + Redis running, migrations at head, seeded
- [ ] `/healthz`, `/readyz`, `/metrics.json` live and honest
- [ ] Test suite green in CI; `gitleaks` clean
- [ ] `deploy.sh` proven, including an automatic rollback on a deliberately broken build
- [ ] Backup taken and **restored once**
- [ ] Memory footprint measured and under ~4.5 GB steady state
- [ ] Docs updated with anything discovered during implementation

---

## Blocked on the operator

1. **Approval of the architecture and repo tree** (blocks everything).
2. **Docker Desktop installed locally** (blocks Step 3).
3. **Git remote URL** — where should this repo live? (blocks Step 8).
4. **SSH access details for the VPS** — or you run Steps 7–9 yourself; the commands above are complete as written.
5. **Four Phase 1 secrets** — `POSTGRES_PASSWORD`, `REDIS_PASSWORD`, `SESSION_SECRET`, initial admin credentials.
6. **Confirmation** that `D:\trade` is on the RTX 4070 SUPER desktop.

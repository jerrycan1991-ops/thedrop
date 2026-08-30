# THE DROP — Deployment

Target: Ubuntu 24.04 VPS, 4 vCPU / 8 GB RAM, existing hosting-panel nginx already proxying `https://thedrop.channel` → `127.0.0.1:3100`.

**Standing constraint: we do not hand-edit nginx.** Phase 1 requires zero nginx changes. If a change ever becomes genuinely necessary, it is made *through the hosting panel*, documented here, and is reversible (§7).

---

## 1. Layout on the VPS

```
/opt/thedrop/                  # git checkout (deploy tree)
  releases/                    # optional: timestamped releases for fast rollback
  current -> releases/<ts>     # symlink the systemd units point at
/etc/thedrop/thedrop.env       # secrets, 0640 root:thedrop
/var/www/thedrop/media/        # generated media, symlinked into the web public dir
/var/lib/thedrop/pgdata/       # Postgres volume (docker compose bind mount)
/var/lib/thedrop/redis/        # Redis volume
/var/backups/thedrop/          # nightly dumps
/var/log/thedrop/              # only for anything not going to journald
```

Service account: `thedrop` (system user, no login shell, member of `docker`).

---

## 2. Runtime split

| Component | Runs as | Why |
|---|---|---|
| PostgreSQL 16 + pgvector | Docker Compose | pinned image, pgvector preinstalled, isolated, easy version pinning |
| Redis 7 | Docker Compose | same |
| Next.js, FastAPI, Celery | systemd, native | no image rebuild per deploy, ~300 MB less overhead, faster restarts, simpler logs |

Rationale in ADR-0002.

---

## 3. One-time provisioning

Run as a sudo-capable user on the VPS. Each block is idempotent.

### 3.1 Base packages and swap

```bash
sudo apt update && sudo apt install -y ca-certificates curl git build-essential ufw jq
```

```bash
sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile && echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

Verify: `free -h` shows 4.0Gi swap.

> Swap exists so a Next.js production build cannot OOM-kill Postgres. It is a safety net, not a performance plan.

### 3.2 Firewall

```bash
sudo ufw default deny incoming && sudo ufw default allow outgoing && sudo ufw allow OpenSSH && sudo ufw allow 80/tcp && sudo ufw allow 443/tcp && sudo ufw --force enable
```

Verify: `sudo ufw status verbose` — 22, 80, 443 only.

### 3.3 Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
```

(Reviewed installer, official source. Verify: `docker --version && docker compose version`.)

### 3.4 Node 22 and pnpm

```bash
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - && sudo apt install -y nodejs && sudo corepack enable && corepack prepare pnpm@latest --activate
```

Verify: `node -v` → v22.x, `pnpm -v`.

### 3.5 Python 3.12 and uv

Ubuntu 24.04 ships Python 3.12.

```bash
sudo apt install -y python3.12 python3.12-venv python3-pip && curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin sh
```

Verify: `python3.12 --version`, `uv --version`.

### 3.6 Service account and directories

```bash
sudo useradd --system --home /opt/thedrop --shell /usr/sbin/nologin thedrop || true
sudo usermod -aG docker thedrop
sudo mkdir -p /opt/thedrop /etc/thedrop /var/www/thedrop/media /var/lib/thedrop/pgdata /var/lib/thedrop/redis /var/backups/thedrop
sudo chown -R thedrop:thedrop /opt/thedrop /var/www/thedrop /var/lib/thedrop /var/backups/thedrop
sudo chown root:thedrop /etc/thedrop && sudo chmod 750 /etc/thedrop
```

---

## 4. Data services

`infrastructure/docker/docker-compose.yml` (bind addresses are load-bearing — see SECURITY.md §2):

```yaml
services:
  postgres:
    image: pgvector/pgvector:pg16
    restart: unless-stopped
    environment:
      POSTGRES_DB: thedrop
      POSTGRES_USER: thedrop
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    ports:
      - "127.0.0.1:5432:5432"   # MUST stay loopback-bound; a bare 5432:5432 bypasses UFW
    volumes:
      - /var/lib/thedrop/pgdata:/var/lib/postgresql/data
    command: >
      postgres -c shared_buffers=1GB -c effective_cache_size=3GB
               -c work_mem=16MB -c maintenance_work_mem=256MB
               -c max_connections=60 -c random_page_cost=1.1
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U thedrop -d thedrop"]
      interval: 10s
      timeout: 5s
      retries: 5
    deploy:
      resources:
        limits:
          memory: 2G

  redis:
    image: redis:7-alpine
    restart: unless-stopped
    command: redis-server --requirepass ${REDIS_PASSWORD} --maxmemory 512mb --maxmemory-policy allkeys-lru --appendonly yes
    ports:
      - "127.0.0.1:6379:6379"
    volumes:
      - /var/lib/thedrop/redis:/data
    healthcheck:
      test: ["CMD-SHELL", "redis-cli -a $$REDIS_PASSWORD ping | grep PONG"]
      interval: 10s
      timeout: 5s
      retries: 5
    deploy:
      resources:
        limits:
          memory: 768M
```

Start:

```bash
cd /opt/thedrop && docker compose -f infrastructure/docker/docker-compose.yml --env-file /etc/thedrop/thedrop.env up -d
```

Verify:

```bash
docker compose -f infrastructure/docker/docker-compose.yml ps && docker exec thedrop-postgres-1 psql -U thedrop -d thedrop -c "SELECT extname FROM pg_extension;"
```

Expect `vector` present after migrations create it.

---

## 5. systemd units

`infrastructure/systemd/thedrop-api.service`:

```ini
[Unit]
Description=THE DROP API (FastAPI)
After=network-online.target docker.service
Requires=docker.service

[Service]
Type=exec
User=thedrop
Group=thedrop
WorkingDirectory=/opt/thedrop/services/api
EnvironmentFile=/etc/thedrop/thedrop.env
ExecStartPre=/opt/thedrop/.venv/bin/alembic upgrade head
ExecStart=/opt/thedrop/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 2
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/www/thedrop /var/log/thedrop
MemoryMax=700M

[Install]
WantedBy=multi-user.target
```

`thedrop-worker.service` — same hardening; `ExecStart=/opt/thedrop/.venv/bin/celery -A app.worker.celery_app worker -B -Q ingest,maintain,publish -c 2 --loglevel=INFO`, `MemoryMax=700M`.

> **Hard rule:** this unit embeds the beat scheduler (`-B`). It must never run more than one instance, or schedules fire twice. If a second worker is ever needed, split beat into its own unit first.

`thedrop-web.service` — `ExecStart=/usr/bin/node /opt/thedrop/apps/web/.next/standalone/apps/web/server.js`, `Environment=PORT=3100 HOSTNAME=127.0.0.1 NODE_ENV=production`, `MemoryMax=800M`.

Enable:

```bash
sudo cp /opt/thedrop/infrastructure/systemd/*.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now thedrop-api thedrop-worker thedrop-web
```

---

## 6. Deploy procedure

`infrastructure/scripts/deploy.sh`, run as `thedrop`:

1. `git fetch --all && git checkout <ref>` in `/opt/thedrop`
2. `pnpm install --frozen-lockfile`
3. `uv sync --frozen`
4. `pnpm --filter @thedrop/web build` with `NODE_OPTIONS=--max-old-space-size=1536`
5. `alembic upgrade head` (also runs as `ExecStartPre`; idempotent)
6. `sudo systemctl restart thedrop-api thedrop-worker thedrop-web`
7. Health gate: poll `http://127.0.0.1:3100/healthz` and `http://127.0.0.1:8000/healthz` for up to 60 s; non-200 → automatic rollback

Downtime is a ~2–4 s restart window. Nginx returns 502 briefly. If that ever becomes unacceptable, the release-symlink + socket-activation path is documented in §10 — but it adds moving parts, so it is deliberately not in Phase 1.

### Build-on-desktop option

If a VPS build is ever slow or memory-tight:

```bash
pnpm --filter @thedrop/web build && rsync -az --delete apps/web/.next/standalone/ thedrop@<vps>:/opt/thedrop/apps/web/.next/standalone/
```

Only valid when the desktop's Node version matches the VPS's.

---

## 7. Nginx (do not touch unless necessary)

Phase 1 needs **nothing**. The existing panel proxy to `127.0.0.1:3100` serves the site, `/admin`, `/api/*` (via Next rewrite) and `/media/*`.

Optional later optimizations, each applied **through the hosting panel UI**, each independently revertible:

| Optimization | Change | When it's worth it |
|---|---|---|
| Direct API route | `location /api/ { proxy_pass http://127.0.0.1:8000; }` | if the Next rewrite hop shows up in p95 |
| Direct media serving | `location /media/ { alias /var/www/thedrop/media/; expires 1y; }` | when media traffic is significant |
| Admin IP allowlist | `location /admin { allow <ip>; deny all; }` | once the operator has a static IP |
| Larger upload body | `client_max_body_size 32m;` | only if worker artifact uploads start failing with 413 |

Note the last one: worker media uploads pass through nginx. If the panel's default `client_max_body_size` is 1–2 MB, video uploads will 413. **Mitigation without touching nginx:** the worker uploads in chunks under 1 MB (the artifact API supports chunked upload). That is why chunking is in the API design rather than a config change.

---

## 8. Monitoring

- `journalctl -u thedrop-* -f` for live logs; JSON lines, one per request, with `request_id`.
- `GET /healthz` — liveness (process up).
- `GET /readyz` — DB + Redis reachable, migrations at head.
- `GET /api/v1/admin/system/metrics` — queue depths, worker heartbeat age, provider circuit states, budget usage, disk and memory.
- External uptime pinger hits `https://thedrop.channel/healthz` every 60 s (free tier; configure separately).
- Disk alert at 75 %, memory alert at 85 %, swap-in-use alert.

No Prometheus stack in Phase 1 (ARCHITECTURE.md §3.2).

---

## 9. Backups and restore

Nightly cron as `thedrop`:

```bash
docker exec thedrop-postgres-1 pg_dump -U thedrop -Fc thedrop > /var/backups/thedrop/thedrop-$(date +%F).dump
```

Retention 14 daily + 8 weekly, plus an off-box copy (rsync to the desktop is sufficient and free).

**Restore drill — run once before Phase 2, then quarterly:**

```bash
docker exec -i thedrop-postgres-1 createdb -U thedrop thedrop_restoretest && docker exec -i thedrop-postgres-1 pg_restore -U thedrop -d thedrop_restoretest < /var/backups/thedrop/<file>.dump
```

A backup that has never been restored is not a backup.

---

## 10. Rollback

| Scenario | Action |
|---|---|
| Bad code deploy | `git checkout <previous-sha> && ./infrastructure/scripts/deploy.sh` — or `systemctl stop` + previous release symlink |
| Bad migration | Restore from the pre-deploy dump (deploy.sh takes one automatically before `alembic upgrade`). Forward-fix preferred; never `downgrade` in production without a rehearsal. |
| Bad content | Unpublish from admin (sets `noindex`, returns 410, audit-logged) |
| Runaway AI cost | Set `ai.enabled=false` in admin settings — effective within 60 s |
| Bad provider | Disable the provider row; circuit breaker also opens automatically |
| Total service failure | `systemctl stop thedrop-web` → nginx 502; or point the panel at a static maintenance page |

`deploy.sh` snapshots the current git SHA to `/opt/thedrop/.last_good_sha` after a successful health gate, so rollback is one command.

---

## 11. Required credentials (operator must supply)

| Secret | Needed by | Phase |
|---|---|---|
| `POSTGRES_PASSWORD` | compose, API | 1 |
| `REDIS_PASSWORD` | compose, API, worker | 1 |
| `SESSION_SECRET` (32+ bytes random) | web, API | 1 |
| `ADMIN_EMAIL` / initial admin password | seed script | 1 |
| `ANTHROPIC_API_KEY` | desktop runner (and API for cost sync) | 3–4 |
| `WORKER_TOKEN` (generated by the API, not chosen) | desktop runner | 2 |
| `GNEWS_API_KEY`, `NEWSAPI_KEY` | ingestion | 2 |
| AdSense publisher ID | web | 5 |
| Newsletter ESP key | newsletter | 5 |
| Social platform API credentials | distribution | 7 |

None are needed to complete Phase 1 except the first four.

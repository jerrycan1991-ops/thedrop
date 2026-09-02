# THE DROP — Deployment

Target: Ubuntu 24.04 VPS, 4 vCPU / 8 GB RAM, existing hosting-panel nginx already proxying `https://thedrop.channel` → `127.0.0.1:3100`.

> **No root on the host?** §3–§6 assume it throughout. If the account you have cannot
> `sudo`, jump to [§12](#12-unprivileged-deployment-no-root-on-the-host) — that is the
> path `thedrop.channel` currently runs on, and ADR-0012 records what it costs.

**Standing constraint: we do not hand-edit nginx.** Phase 1 requires zero nginx changes. If a change ever becomes genuinely necessary, it is made *through the hosting panel*, documented here, and is reversible (§7).

---

## 1. Layout on the VPS

```
/opt/thedrop/                  # git checkout (deploy tree)
  releases/                    # optional: timestamped releases for fast rollback
  current -> releases/<ts>     # symlink the systemd units point at
/etc/thedrop/thedrop.env       # secrets, 0640 root:thedrop
/var/www/thedrop/media/        # generated media, symlinked into the web public dir
/var/lib/thedrop/pgdata/       # unused; Postgres keeps its own /var/lib/postgresql
/var/lib/thedrop/redis/        # Redis volume
/var/backups/thedrop/          # nightly dumps
/var/log/thedrop/              # only for anything not going to journald
```

Service account: `thedrop` (system user, no login shell). It owns the deploy tree, the Redis data directory and the media directory. No `docker` group — there is no Docker on this host.

---

## 2. Runtime split

| Component | Runs as | Why |
|---|---|---|
| PostgreSQL 16 + pgvector | systemd (distro package) | one process manager on the host; pgvector is an apt package, not a reason for a container |
| Redis 7 | systemd (dedicated instance, port 6380) | isolated from the panel's Redis on 6379, which evicts under an LRU policy |
| Next.js, FastAPI, Celery | systemd, native | no image rebuild per deploy, ~300 MB less overhead, faster restarts, simpler logs |

Rationale in ADR-0011, which supersedes the data-services half of ADR-0002.

**Docker is not installed on the VPS.** `infrastructure/docker/` is local-development
only.

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

### 3.3 Data service packages

No Docker. PostgreSQL and Redis are installed as host packages in §4 (ADR-0011).

---


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
sudo mkdir -p /opt/thedrop /etc/thedrop /var/www/thedrop/media /var/lib/thedrop/pgdata /var/lib/thedrop/redis /var/backups/thedrop
sudo chown -R thedrop:thedrop /opt/thedrop /var/www/thedrop /var/lib/thedrop /var/backups/thedrop
sudo chown root:thedrop /etc/thedrop && sudo chmod 750 /etc/thedrop
```

---

## 4. Data services

PostgreSQL and Redis run **natively under systemd**, not in containers (ADR-0011). The
VPS is managed by a hosting panel that already runs its own MySQL, nginx, Redis, Varnish
and PHP-FPM pools; adding a Docker daemon would be a third process manager on the box
for two services the distro packages well.

### 4.1 PostgreSQL 16 + pgvector

```bash
sudo apt install -y postgresql-16 postgresql-16-pgvector
```

Verify the extension is available before going further — it is a separate package, and a
missing one surfaces as a failed migration minutes into a deploy:

```bash
sudo -u postgres psql -tAc "SELECT 1 FROM pg_available_extensions WHERE name='vector'"
```

Expect `1`. If the package is not found, add the PGDG repository rather than dropping
pgvector: semantic search and dedup depend on it (ADR-0005).

Create the role and database. Use the same password you put in `POSTGRES_PASSWORD`:

```bash
sudo -u postgres createuser --pwprompt thedrop && sudo -u postgres createdb -O thedrop thedrop
```

Postgres listens on `127.0.0.1:5432` by default on Ubuntu, which is what we want — the
firewall is a backstop, not the control. Confirm it is not listening publicly:

```bash
sudo ss -lntp | grep 5432
```

Tuning for an 8 GB box shared with a panel, in
`/etc/postgresql/16/main/conf.d/thedrop.conf`:

```
shared_buffers = 1GB
effective_cache_size = 3GB
work_mem = 16MB
maintenance_work_mem = 256MB
max_connections = 60
random_page_cost = 1.1
log_min_duration_statement = 1000
```

> **Pin the version.** An `apt upgrade` can move the Postgres minor version where an
> image tag could not. Add `postgresql-*` to unattended-upgrades' blacklist. This is the
> cost ADR-0011 accepts in exchange for dropping Docker.

### 4.2 Redis — a second, dedicated instance

The panel's Redis on 6379 is left alone. This project runs its own on **6380**.

Reusing the panel's instance is not an option: it is shared with PHP sites under an
eviction policy, and this application keeps admin sessions and login rate-limit counters
in Redis. An evicted rate-limit counter is a silently disabled safeguard.

```bash
sudo apt install -y redis-server
```

Ubuntu's package enables `redis-server.service` on 6379. That unit belongs to the panel;
leave it running and add ours alongside:

```bash
sudo install -m 0640 -o root -g thedrop /opt/thedrop/infrastructure/redis/thedrop-redis.conf /etc/thedrop/redis.conf
```

Then set the password in `/etc/thedrop/redis.conf` — uncomment `requirepass` and use the
**same value** as `REDIS_PASSWORD` in `/etc/thedrop/thedrop.env`. `deploy.sh` compares
the two and refuses to run if they disagree, because a mismatch otherwise presents as a
generic connection error.

```bash
sudo cp /opt/thedrop/infrastructure/systemd/thedrop-redis.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now thedrop-redis
```

Verify both instances are up and separate:

```bash
redis-cli -p 6380 -a "$REDIS_PASSWORD" --no-auth-warning ping && sudo ss -lntp | grep -E ':(6379|6380)'
```

Expect `PONG` and two listeners.

### 4.3 Env file wiring

The application reaches both over loopback. In `/etc/thedrop/thedrop.env`:

```
POSTGRES_HOST_PORT=5432
REDIS_HOST_PORT=6380
DATABASE_URL=postgresql+psycopg://thedrop:<password>@127.0.0.1:5432/thedrop
REDIS_URL=redis://:<password>@127.0.0.1:6380/0
CELERY_BROKER_URL=redis://:<password>@127.0.0.1:6380/1
CELERY_RESULT_BACKEND=redis://:<password>@127.0.0.1:6380/2
```

`deploy.sh` verifies all of this — both services active, both reachable with the
configured credentials, `vector` available, passwords consistent — before it touches
anything.

---


## 5. systemd units

`infrastructure/systemd/thedrop-api.service`:

```ini
[Unit]
Description=THE DROP API (FastAPI)
After=network-online.target postgresql.service thedrop-redis.service
Requires=postgresql.service thedrop-redis.service

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

`thedrop-worker.service` — same hardening; `ExecStart=/opt/thedrop/.venv/bin/celery -A app.celery_app worker -B -Q ingest,maintain,publish -c 2 --loglevel=INFO`, and `Environment=PYTHONPATH=/opt/thedrop/services/worker` (WorkingDirectory does not affect `sys.path` for a console script), `MemoryMax=700M`.

> **Hard rule:** this unit embeds the beat scheduler (`-B`). It must never run more than one instance, or schedules fire twice. If a second worker is ever needed, split beat into its own unit first.

`thedrop-web.service` — `ExecStart=/usr/bin/node /opt/thedrop/apps/web/.next/standalone/apps/web/server.js`, `Environment=PORT=3100 HOSTNAME=127.0.0.1 NODE_ENV=production`, `MemoryMax=800M`.

Enable:

```bash
sudo cp /opt/thedrop/infrastructure/systemd/*.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now thedrop-api thedrop-worker thedrop-web
```

---

## 6. Deploy procedure

**The default path builds on the desktop.** `next build` is the only sustained
CPU/RAM spike a deploy asks of the VPS, and the VPS is a publishing and coordination
tier — it does not think (ARCHITECTURE.md §3). Building where the work belongs keeps
~1.5 GB of build pressure off a box that is also running Postgres, Redis and a hosting
panel.

### Standard deploy — build on the desktop

On the desktop, from the repo root:

```bash
VPS_HOST=thedrop@<vps-ip> NEXT_PUBLIC_SITE_URL=https://thedrop.channel bash infrastructure/scripts/build-and-push.sh <git-ref>
```

Then on the VPS:

```bash
sudo -u thedrop bash /opt/thedrop/infrastructure/scripts/deploy.sh <git-ref> --no-build
```

`build-and-push.sh` refuses to build from a dirty tree, enforces the Node major from
`.tool-versions`, gates on an `https://` `NEXT_PUBLIC_SITE_URL`, copies the static assets
into the standalone tree, writes a `.thedrop-build` manifest recording the SHA, site URL
and Node major, then rsyncs with `--delete`.

`deploy.sh --no-build` verifies that manifest against the SHA it is deploying before it
restarts anything. That check exists because `NEXT_PUBLIC_SITE_URL` is inlined at build
time: once the bundle reaches the VPS the value is already baked in, nothing on the
server can correct it, and a site serving `http://localhost:3100` canonical URLs looks
completely healthy. A stale bundle from a failed rsync fails the SHA comparison rather
than shipping yesterday's UI.

### Fallback — build on the VPS

```bash
sudo -u thedrop bash /opt/thedrop/infrastructure/scripts/deploy.sh <git-ref>
```

Same script without `--no-build`. Capped at `--max-old-space-size=1536` with the 4 GB
swapfile behind it. Use when the desktop is unavailable.

### What deploy.sh does, either way

1. `git fetch && git checkout <ref>` in `/opt/thedrop`
2. Node major matches `.tool-versions`
3. Data services active and reachable; `vector` extension available; Redis passwords consistent
4. `pg_dump` snapshot to `/var/backups/thedrop` — refuses to migrate without one
5. `uv sync --frozen`
6. Build, or verify the prebuilt bundle's manifest
7. `alembic upgrade head` (also runs as `ExecStartPre`; idempotent)
8. `systemctl restart thedrop-api thedrop-worker thedrop-web`
9. Health gate, route-ownership gate, static-asset gate — any failure rolls back

Downtime is a ~2–4 s restart window. Nginx returns 502 briefly. If that ever becomes unacceptable, the release-symlink + socket-activation path is documented in §10 — but it adds moving parts, so it is deliberately not in Phase 1.

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
PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -h 127.0.0.1 -U thedrop -Fc thedrop > /var/backups/thedrop/thedrop-$(date +%F).dump
```

Retention 14 daily + 8 weekly, plus an off-box copy (rsync to the desktop is sufficient and free).

**Restore drill — run once before Phase 2, then quarterly:**

```bash
PGPASSWORD="$POSTGRES_PASSWORD" createdb -h 127.0.0.1 -U thedrop thedrop_restoretest && PGPASSWORD="$POSTGRES_PASSWORD" pg_restore -h 127.0.0.1 -U thedrop -d thedrop_restoretest /var/backups/thedrop/<file>.dump
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
| **`NEXT_PUBLIC_SITE_URL`** = `https://thedrop.channel` | web — **BUILD TIME** | 1 |
| `DATABASE_URL`, `REDIS_URL` | web (as of the Node migration), API, worker | 1 |
| `ADMIN_EMAIL` / initial admin password | seed script | 1 |
| `ANTHROPIC_API_KEY` | desktop runner (and API for cost sync) | 3–4 |
| `WORKER_TOKEN` (generated by the API, not chosen) | desktop runner | 2 |
| `GNEWS_API_KEY`, `NEWSAPI_KEY` | ingestion | 2 |
| AdSense publisher ID | web | 5 |
| Newsletter ESP key | newsletter | 5 |
| Social platform API credentials | distribution | 7 |

> **`NEXT_PUBLIC_SITE_URL` is inlined by `next build`.** Setting it only at runtime has
> no effect: the value is baked into the bundle. Missing during a production build,
> every canonical URL, sitemap entry and OpenGraph tag ships as
> `http://localhost:3100`. `deploy.sh` sources `/etc/thedrop/thedrop.env` before
> building and refuses a production build without an `https://` value.

> **The web tier now needs `DATABASE_URL` and `REDIS_URL`.** Before the Node migration
> it needed neither. `thedrop-web.service` reads the same `EnvironmentFile`, and now
> declares `Requires=postgresql.service thedrop-redis.service` (ADR-0011).

None are needed to complete Phase 1 except the first four.


---

## 12. Unprivileged deployment (no root on the host)

Everything above assumes root. This section is the path taken when the host refuses it,
and it is what `thedrop.channel` currently runs on. The decision and its costs are in
[ADR-0012](adr/0012-unprivileged-deployment.md); this is the procedure.

**Read §1–§11 first.** This section replaces §3.3, §4, §5 and §6 and leaves the rest
intact.

### 12.1 Confirm root really is unavailable

Do not adopt this path to avoid a conversation with whoever owns the box. Confirm it:

```bash
sudo -l; su -; getent group sudo
```

On a CloudPanel site user you will see `sudo` limited to `clpctlWrapper`, `su` denied at
the *binary* (`/usr/bin/su: Permission denied` — the execute bit is stripped, it is not a
password failure), and an empty `sudo` group meaning no admin account exists to ask for.
There is no escalation path from the host, and none should be sought. Root is reachable
only through the provider's out-of-band console.

### 12.2 What replaces what

| §  | Privileged | Unprivileged |
|---|---|---|
| 3.6 | `/opt/thedrop`, `/etc/thedrop` | `~/thedrop`, `~/.config/thedrop`, `~/.local/state/thedrop` |
| 4.1 | PostgreSQL from apt | managed Postgres off-box |
| 4.2 | `thedrop-redis.service` | same Redis, run by PM2 on 6380 |
| 5 | systemd units | `infrastructure/pm2/ecosystem.config.cjs` |
| 6 | `deploy.sh` | `deploy-userspace.sh` |
| 9 | `pg_dump` on the host | provider snapshot, asserted with `--backup-verified` |

Prerequisites already present on a CloudPanel host: Node (via nvm), Python 3.12, `uv`,
`pnpm`, `redis-server`, PM2, and a writable crontab. Verify Node's major matches
`.tool-versions` — the deploy script aborts otherwise.

### 12.3 Managed PostgreSQL — pick the region by measurement

**Region is the single highest-impact choice here.** Every query becomes a network round
trip on the render path, and TTFB feeds Core Web Vitals and Google News eligibility.

Measure before creating the project — this is TCP handshake time to each region:

```bash
for r in us-east-1 us-east-2 us-west-2 eu-central-1 eu-west-2 ap-south-1 ap-southeast-1; do printf '%-16s ' $r; curl -sS -o /dev/null -w '%{time_connect}s\n' --max-time 6 https://ec2.$r.amazonaws.com; done
```

Create the project in the lowest one. Providers cannot move a project between regions, so
getting this wrong costs a data migration later. For reference, the first attempt here
landed in Ohio at **152 ms/query**; Frankfurt measured **12 ms**. A page issuing five
sequential queries pays 760 ms versus 60 ms.

Verify pgvector is available before going further — it is checked by the deploy script,
but finding out now is cheaper.

### 12.4 The env file, and four traps

Config lives at `~/.config/thedrop/thedrop.env`, mode 0600. Start from `.env.example`.

All four of the following cost real time during the first deployment. They are listed in
the order they bite.

**1. Replace whole values, never fragments.** Pasting a connection string over the
`CHANGE_ME` inside `DATABASE_URL=postgresql+psycopg://thedrop:CHANGE_ME@127.0.0.1:5432/thedrop`
leaves the surrounding scheme and host attached, producing
`postgresql+psycopg://thedrop:postgresql://user:pass@real-host/db@127.0.0.1:5432/thedrop`.
It fails as `failed to resolve host 'thedrop'`. Rewrite the line:

```bash
read -rs -p "Paste the connection string: " U; echo; sed -i '/^DATABASE_URL=/d' ~/.config/thedrop/thedrop.env && printf 'DATABASE_URL="%s"\n' "$(echo "$U" | sed 's|^postgresql://|postgresql+psycopg://|')" >> ~/.config/thedrop/thedrop.env && unset U
```

**2. Quote anything containing `&`.** Managed providers append query parameters
(`?sslmode=require&channel_binding=require`). Unquoted, `bash` sourcing the file treats
`&` as a command separator and **backgrounds the assignment** — the variable ends up
truncated and the shell prints a stray `[1]+ Done`.

**3. Do not verify with a mask that hides the defect.** `sed 's|://[^@]*@|://***@|'`
consumes everything up to the first `@`, which includes a duplicated scheme, so a broken
value renders as a correct one. Verify by *parsing*:

```bash
set -a; . ~/.config/thedrop/thedrop.env; set +a; ~/thedrop/.venv/bin/python -c "import os;from urllib.parse import urlsplit;u=urlsplit(os.environ['DATABASE_URL'].replace('postgresql+psycopg://','postgresql://'));print('user:',u.username,'| host:',u.hostname,'| db:',u.path.lstrip('/'))"
```

**4. Remove any `.env` from the repo root.** `uv run` loads it and it overrides the
exported environment, so `alembic upgrade head` silently targets whatever that file
names — a stale placeholder pointing at `127.0.0.1:5432`, in the observed case. Rename
it: `mv ~/thedrop/.env ~/thedrop/.env.disabled`.

Then confirm nothing is left:

```bash
grep -c CHANGE_ME ~/.config/thedrop/thedrop.env
```

`ADMIN_INITIAL_PASSWORD=CHANGE_ME_ON_FIRST_LOGIN` counts — it is a real credential the
seed script uses, not a placeholder to ignore.

### 12.5 Redis on 6380

`/usr/bin/redis-server` is executable by any user and 6380 is above 1024, so the
dedicated instance ADR-0011 wanted is achievable without privilege. The panel's Redis on
6379 stays untouched — it is shared with other sites under an eviction policy, and this
application keeps admin sessions and login rate-limit counters in Redis.

```bash
mkdir -p ~/.local/state/thedrop/{run,log,backups,redis} && install -m 600 ~/thedrop/infrastructure/redis/thedrop-redis-userspace.conf ~/.config/thedrop/redis.conf
```

`dir` must be absolute — Redis resolves a relative path against PM2's working directory,
which scatters the AOF and loses every session on restart:

```bash
sed -i "s|^dir .*|dir $HOME/.local/state/thedrop/redis|" ~/.config/thedrop/redis.conf
```

Set `requirepass` from the value already in the env file, so the two cannot drift:

```bash
P=$(grep '^REDIS_PASSWORD=' ~/.config/thedrop/thedrop.env | cut -d= -f2-) && sed -i "s|^# *requirepass .*|requirepass $P|" ~/.config/thedrop/redis.conf
```

> Generate the Redis password with `openssl rand -hex 24`, **not** `-base64`. Base64
> emits `/`, `+` and `=`; a `/` inside `redis://:PASSWORD@host` is read as the start of a
> path and the connection fails obscurely. Hex is URL-safe by construction.

### 12.6 PM2

```bash
cd ~/thedrop && pm2 start infrastructure/pm2/ecosystem.config.cjs && pm2 save
```

`pm2 save` writes the dump that the existing `@reboot ... pm2 resurrect` crontab entry
restores. **Without it a reboot restores whatever was saved last**, which is how a stale
process outlives a deploy. `deploy-userspace.sh` runs it for you; a manual `pm2 start`
does not.

Two things the ecosystem file encodes that are easy to get wrong:

- Each app needs an **explicit** `out_file`/`error_file`. PM2 does not expand `%name%` or
  any placeholder — a templated path on shared defaults gives every app the *same* log
  file, and `pm2 logs thedrop-worker` then prints everything except the worker.
- The worker needs `PYTHONPATH=~/thedrop/services/worker`. `cd` is not enough: Python
  adds the CWD to `sys.path` for `-c` and `-m` but never for an installed console script,
  so `celery -A app.celery_app` resolves from its own `bin/` and crash-loops on
  *The module app.celery_app was not found*.

### 12.7 Migrations and seed

```bash
cd ~/thedrop && set -a; . ~/.config/thedrop/thedrop.env; set +a; ~/thedrop/.venv/bin/alembic -c packages/database/alembic.ini upgrade head
```

```bash
cd ~/thedrop && set -a; . ~/.config/thedrop/thedrop.env; set +a; ~/thedrop/.venv/bin/python -m thedrop_database.seed
```

The seed creates the category taxonomy, roles, default settings and the initial admin
user. **Without it `/api/v1/public/categories` returns `[]` and there is no account to
log in with** — a state that otherwise looks like a healthy deployment.

Call `.venv/bin/…` directly rather than `uv run`; see trap 4 above.

### 12.8 Deploy

```bash
cd ~/thedrop && bash infrastructure/scripts/deploy-userspace.sh [git-ref] [--no-build] [--backup-verified]
```

`--backup-verified` is required on a host with no `pg_dump`, and asserts that a
provider-side snapshot or PITR point exists. It is an explicit claim, not a skip: the
script refuses to migrate without either a dump it took itself or this flag.

Prefer `--no-build` with a bundle from `build-and-push.sh` on the desktop. A VPS build
works but is the only sustained memory spike a deploy asks of the host.

All five gates must pass, or the script rolls back:

```
health gate: api OK
health gate: web OK
health gate: route ownership OK (Node 200, proxy 401)
health gate: static assets OK (…)
health gate: worker stable (no restarts in 15s)
```

The worker gate samples PM2's restart counter twice, fifteen seconds apart. Status alone
proves nothing — PM2 reports `online` the instant it spawns a process that is about to
die, which is how three consecutive green deploys shipped a dead worker.

### 12.9 Cutting over from an existing process

When something already serves port 3100, free it *before* the deploy but keep it
recoverable:

Find its name with `pm2 list`; on this host it was `thedrop`.

```bash
pm2 stop thedrop
```

`stop` rather than `delete`: a stopped app releases the port and can be restarted if the
deploy fails. Once all gates pass:

```bash
pm2 delete thedrop && pm2 save
```

### 12.10 Operations

| Task | Command |
|---|---|
| Status | `pm2 list` — watch the `↺` column, not just `status` |
| Logs | `pm2 logs thedrop-worker --lines 40 --nostream` |
| Raw logs | `~/.local/state/thedrop/log/thedrop-<app>.log` and `.err.log` |
| Restart one | `pm2 restart thedrop-api --update-env` |
| Rollback | `deploy-userspace.sh <previous-sha>`; the sha is in `~/thedrop/.last_good_sha` |

`logrotate` needs root, so install PM2's own rotation once:

```bash
pm2 install pm2-logrotate
```

### 12.11 Returning to the privileged deployment

When root becomes available, follow ADR-0012's exit: install PostgreSQL per §4, migrate
the data back, install the systemd units from §5, `pm2 delete all` and drop its crontab
entry, then use `deploy.sh` instead of `deploy-userspace.sh`.

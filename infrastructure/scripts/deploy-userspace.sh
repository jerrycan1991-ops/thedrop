#!/usr/bin/env bash
#
# THE DROP deploy, for a host where we have no root (ADR-0012).
#
#   bash infrastructure/scripts/deploy-userspace.sh [git-ref] [--no-build] [--backup-verified]
#
# Differences from deploy.sh, all forced by the lack of privilege:
#   * PostgreSQL is a managed service off-box; there is nothing local to start or check
#     with systemctl, and no postgresql-client to check it with either.
#   * Redis is our own unprivileged instance on 6380, run under PM2.
#   * Processes are managed by PM2 instead of systemctl.
#   * Paths live under $HOME rather than /opt and /etc.
#
# Everything else -- the pre-deploy backup requirement, the build-time env gate, the
# health, route-ownership and static-asset gates, automatic rollback -- is preserved.
# Those are safeguards, not conveniences, and losing root is not a reason to drop them.

set -Eeuo pipefail

APP_DIR="${APP_DIR:-$HOME/thedrop}"
ENV_FILE="${ENV_FILE:-$HOME/.config/thedrop/thedrop.env}"
REDIS_CONF="${REDIS_CONF:-$HOME/.config/thedrop/redis.conf}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/.local/state/thedrop/backups}"
LAST_GOOD_FILE="$APP_DIR/.last_good_sha"
BUILD_MANIFEST_NAME=".thedrop-build"
ECOSYSTEM="$APP_DIR/infrastructure/pm2/ecosystem.config.cjs"

SKIP_BUILD=0
BACKUP_VERIFIED=0
TARGET_REF=""
for arg in "$@"; do
  case "$arg" in
    --no-build)        SKIP_BUILD=1 ;;
    --backup-verified) BACKUP_VERIFIED=1 ;;
    -*) echo "unknown flag: $arg" >&2; exit 2 ;;
    *)  TARGET_REF="$arg" ;;
  esac
done

HEALTH_TIMEOUT=60
WEB_URL="http://127.0.0.1:3100/"
API_URL="http://127.0.0.1:8000/readyz"

log()  { echo "[deploy] $*"; }
fail() { echo "[deploy] ERROR: $*" >&2; exit 1; }

cd "$APP_DIR" || fail "cannot enter $APP_DIR"
[[ -f "$ENV_FILE" ]] || fail "missing $ENV_FILE - see docs/DEPLOYMENT.md §12"

PREVIOUS_SHA="$(git rev-parse HEAD)"
log "current revision $PREVIOUS_SHA"

# ---------------------------------------------------------------- fetch
if [[ -n "$TARGET_REF" ]]; then
  log "fetching $TARGET_REF"
  git fetch --all --prune
  git checkout --detach "$TARGET_REF"
fi
TARGET_SHA="$(git rev-parse HEAD)"

# ---------------------------------------------------------------- toolchain parity
EXPECTED_NODE="$(awk '/^nodejs/ {print $2}' .tool-versions | cut -d. -f1)"
ACTUAL_NODE="$(node -v | sed 's/^v//' | cut -d. -f1)"
[[ "$EXPECTED_NODE" == "$ACTUAL_NODE" ]] \
  || fail "node major mismatch: expected $EXPECTED_NODE, found $ACTUAL_NODE"

# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a

RD_PORT="${REDIS_HOST_PORT:-6380}"

# ---------------------------------------------------------------- data services
# No systemctl here. Redis is a PM2-managed user process; Postgres is somebody else's
# server. Both are checked by actually talking to them, which is the only thing that
# ever mattered.
log "checking data services"

command -v pm2 >/dev/null 2>&1 || fail "pm2 is not on PATH; this deployment is supervised by PM2 (ADR-0012)"

# Idempotent: starts the app if absent, leaves it alone if already running.
pm2 describe thedrop-redis >/dev/null 2>&1 \
  || pm2 start "$ECOSYSTEM" --only thedrop-redis >/dev/null \
  || fail "could not start the local redis instance"

redis-cli -h 127.0.0.1 -p "$RD_PORT" -a "$REDIS_PASSWORD" --no-auth-warning ping \
  2>/dev/null | grep -q PONG \
  || fail "cannot authenticate to redis on 127.0.0.1:$RD_PORT"

if [[ -r "$REDIS_CONF" ]] && grep -q '^requirepass ' "$REDIS_CONF"; then
  CONF_PASS="$(awk '/^requirepass /{print $2; exit}' "$REDIS_CONF")"
  [[ "$CONF_PASS" == "$REDIS_PASSWORD" ]] \
    || fail "REDIS_PASSWORD in $ENV_FILE does not match requirepass in $REDIS_CONF"
fi

# psycopg comes from the project venv, so this works without postgresql-client, which we
# cannot install. pgvector is checked the same way: on a managed provider the extension
# is available but often not enabled by default, and discovering that during `alembic
# upgrade` wastes a deploy.
uv run python - <<'PYCHECK' || fail "database preflight failed"
import os
import sys

import psycopg

url = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
try:
    with psycopg.connect(url, connect_timeout=10) as conn:
        row = conn.execute(
            "SELECT 1 FROM pg_available_extensions WHERE name = 'vector'"
        ).fetchone()
        if not row:
            sys.exit("the 'vector' extension is unavailable on this server")
except Exception as exc:  # noqa: BLE001 - the message is the whole point here
    sys.exit(f"cannot reach the database: {exc}")
print("[deploy] database reachable, pgvector available")
PYCHECK

# ---------------------------------------------------------------- backup
# deploy.sh takes its own snapshot. Here there is no pg_dump and no way to install one,
# so the safeguard is preserved by refusing to migrate unless a backup demonstrably
# exists -- either one this script can take, or one the operator asserts.
mkdir -p "$BACKUP_DIR"
if command -v pg_dump >/dev/null 2>&1; then
  SNAPSHOT="$BACKUP_DIR/pre-deploy-$(date +%Y%m%d-%H%M%S).dump"
  log "snapshotting database to $SNAPSHOT"
  pg_dump -Fc "$(echo "$DATABASE_URL" | sed 's|postgresql+psycopg://|postgresql://|')" \
    > "$SNAPSHOT" || fail "backup failed - refusing to migrate without one"
  [[ -s "$SNAPSHOT" ]] || fail "backup file is empty - refusing to migrate"
elif [[ "$BACKUP_VERIFIED" -eq 1 ]]; then
  log "no pg_dump on this host; proceeding on --backup-verified"
  log "  (you asserted a provider-side snapshot or PITR point exists for $TARGET_SHA)"
else
  fail "no pg_dump on this host and no backup asserted.
  Take one from the desktop first:
    pg_dump -Fc \"\$DATABASE_URL\" > thedrop-\$(date +%F).dump
  or, if your provider has PITR/branching, create a restore point and re-run with
  --backup-verified. Migrating without a way back is not an option."
fi

# ---------------------------------------------------------------- build-time env gate
if [[ "${ENVIRONMENT:-}" == "production" ]]; then
  [[ -n "${NEXT_PUBLIC_SITE_URL:-}" ]] \
    || fail "NEXT_PUBLIC_SITE_URL is not set. It is inlined at build time and drives every canonical URL, the sitemap and OpenGraph. Set it in $ENV_FILE before deploying."
  case "$NEXT_PUBLIC_SITE_URL" in
    https://*) : ;;
    *) fail "NEXT_PUBLIC_SITE_URL must be an https:// origin in production (got: $NEXT_PUBLIC_SITE_URL)" ;;
  esac
  log "build-time env gate: NEXT_PUBLIC_SITE_URL=$NEXT_PUBLIC_SITE_URL"
fi

# ---------------------------------------------------------------- dependencies
log "syncing python dependencies"
uv sync --frozen

STANDALONE="$APP_DIR/apps/web/.next/standalone/apps/web"
MANIFEST="$STANDALONE/$BUILD_MANIFEST_NAME"

if [[ "$SKIP_BUILD" -eq 1 ]]; then
  log "skipping build; verifying prebuilt bundle"
  [[ -f "$STANDALONE/server.js" ]] \
    || fail "--no-build but no bundle at $STANDALONE/server.js - run build-and-push.sh first"
  [[ -f "$MANIFEST" ]] \
    || fail "bundle has no $BUILD_MANIFEST_NAME manifest - it was not built by build-and-push.sh"

  BUILT_SHA="$(awk -F= '/^sha=/{print $2; exit}' "$MANIFEST")"
  BUILT_URL="$(awk '/^site_url=/{sub(/^site_url=/, ""); print; exit}' "$MANIFEST")"
  BUILT_NODE="$(awk -F= '/^node=/{print $2; exit}' "$MANIFEST")"

  [[ "$BUILT_SHA" == "$TARGET_SHA" ]] \
    || fail "bundle was built from $BUILT_SHA but this deploy targets $TARGET_SHA - rsync did not run, or ran before the commit"
  [[ "$BUILT_NODE" == "$EXPECTED_NODE" ]] \
    || fail "bundle was built with node $BUILT_NODE, this host expects $EXPECTED_NODE"
  if [[ "${ENVIRONMENT:-}" == "production" ]]; then
    [[ "$BUILT_URL" == "$NEXT_PUBLIC_SITE_URL" ]] \
      || fail "bundle baked in NEXT_PUBLIC_SITE_URL=$BUILT_URL but $ENV_FILE says $NEXT_PUBLIC_SITE_URL"
  fi

  CHUNKS=$(find "$STANDALONE/.next/static" -name '*.js' 2>/dev/null | wc -l)
  [[ "$CHUNKS" -gt 0 ]] \
    || fail "bundle contains no JS chunks; static assets were not copied before rsync"
  log "prebuilt bundle OK (sha $BUILT_SHA, node $BUILT_NODE, $CHUNKS chunks)"
else
  # Building here is discouraged: 7.7GB of RAM already ~900MB into swap, no MemoryMax to
  # contain it, and an OOM takes the live site down with it. Prefer build-and-push.sh.
  log "WARNING: building on the VPS. Prefer build-and-push.sh from the desktop."
  pnpm install --frozen-lockfile
  NODE_OPTIONS="--max-old-space-size=1536" pnpm --filter @thedrop/web build

  [[ -f "$STANDALONE/server.js" ]] || fail "standalone build missing at $STANDALONE/server.js"
  log "copying static assets into the standalone tree"
  rm -rf "$STANDALONE/.next/static"
  mkdir -p "$STANDALONE/.next"
  cp -r "$APP_DIR/apps/web/.next/static" "$STANDALONE/.next/static"
  if [[ -d "$APP_DIR/apps/web/public" ]]; then
    rm -rf "$STANDALONE/public"
    cp -r "$APP_DIR/apps/web/public" "$STANDALONE/public"
  fi
  CHUNKS=$(find "$STANDALONE/.next/static" -name '*.js' | wc -l)
  [[ "$CHUNKS" -gt 0 ]] || fail "no JS chunks copied into the standalone tree; the site would load unstyled"
  log "copied $CHUNKS static chunks"

  # `next build` regenerates apps/web/next-env.d.ts, and on some hosts it writes
  # content that differs from what is committed. The tree is then dirty, and the NEXT
  # `git merge` or `git checkout` aborts -- a deploy blocked by a file nobody edited.
  #
  # Restored rather than committed, because the file is generated and Next's own docs
  # say not to edit it. Reported rather than restored silently: a persistent difference
  # here means the build environments genuinely diverge, and that is worth someone
  # looking at rather than being quietly reverted on every deploy.
  if ! git -C "$APP_DIR" diff --quiet -- apps/web/next-env.d.ts 2>/dev/null; then
    log "NOTE: the build rewrote apps/web/next-env.d.ts; restoring it so the next merge is not blocked"
    git -C "$APP_DIR" diff -- apps/web/next-env.d.ts | head -20
    git -C "$APP_DIR" checkout -- apps/web/next-env.d.ts
  fi
fi

# ---------------------------------------------------------------- migrate
log "running migrations"
uv run alembic -c packages/database/alembic.ini upgrade head

# ---------------------------------------------------------------- restart
# `startOrRestart` is the idempotent form: it starts apps that are absent and restarts
# those already running, in one pass, from the single definition in the ecosystem file.
# It also cannot produce a second thedrop-worker, which would run a second beat
# scheduler and fire every scheduled task twice.
log "restarting application processes"
pm2 startOrRestart "$ECOSYSTEM" --update-env || fail "pm2 could not start the application"

# Persist the process list so the existing `@reboot ... pm2 resurrect` crontab entry
# brings this exact set back after a reboot. Without it the machine comes up serving
# whatever was saved last time -- which is how a stale build ends up live.
pm2 save >/dev/null || log "WARNING: pm2 save failed; a reboot may restore a stale process list"

# ---------------------------------------------------------------- health gate
wait_for() {
  local url="$1" name="$2" deadline=$((SECONDS + HEALTH_TIMEOUT))
  while (( SECONDS < deadline )); do
    if curl -fsS --max-time 5 -o /dev/null "$url"; then
      log "health gate: $name OK"
      return 0
    fi
    sleep 2
  done
  return 1
}

rollback() {
  log "ROLLING BACK to $PREVIOUS_SHA"
  git checkout --detach "$PREVIOUS_SHA"
  uv sync --frozen
  pm2 startOrRestart "$ECOSYSTEM" --update-env || true
  pm2 save >/dev/null || true
  log "rolled back."
  log "NOTE: schema migrations are NOT auto-reverted. If this deploy migrated,"
  log "      restore from your backup. See docs/DEPLOYMENT.md §10."
  log "NOTE: the web bundle was NOT rebuilt. Re-run build-and-push.sh from the desktop"
  log "      against $PREVIOUS_SHA to put the matching bundle back."
  exit 1
}

wait_for "$API_URL" "api" || { log "api failed readiness"; rollback; }
wait_for "$WEB_URL" "web" || { log "web failed readiness"; rollback; }

ownership_gate() {
  local node_code proxied_code
  node_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
    "http://127.0.0.1:3100/api/v1/public/categories" || echo 000)
  [[ "$node_code" == "200" ]] || {
    log "ownership gate: Node-owned /api/v1/public/categories returned $node_code (want 200)"
    return 1
  }
  proxied_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
    "http://127.0.0.1:3100/api/v1/worker/status" || echo 000)
  [[ "$proxied_code" == "401" ]] || {
    log "ownership gate: proxied /api/v1/worker/status returned $proxied_code (want 401)"
    log "                the FastAPI proxy rewrite is broken or the api process is down"
    return 1
  }
  log "health gate: route ownership OK (Node 200, proxy 401)"
}

asset_gate() {
  local asset code
  asset=$(curl -fsS --max-time 10 "http://127.0.0.1:3100/" \
    | grep -oE '/_next/static/[^"]+\.(css|js)' | head -1)
  [[ -n "$asset" ]] || { log "asset gate: no /_next/static reference in the homepage HTML"; return 1; }
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "http://127.0.0.1:3100$asset" || echo 000)
  [[ "$code" == "200" ]] || {
    log "asset gate: $asset returned $code (want 200)"
    return 1
  }
  log "health gate: static assets OK ($asset)"
}

# --- worker gate ------------------------------------------------------------
# The HTTP gates above say nothing about the worker, and a dead worker is invisible
# from outside: the site serves perfectly while nothing is ever ingested or published.
# This shipped three green deploys in a row with the worker crash-looping on a missing
# PYTHONPATH, so "all gates passed" now means the worker too.
#
# PM2 reports `online` the moment it spawns a process, so status alone is worthless for
# something failing a second later. Sampling the restart counter twice is what
# distinguishes running from restarting.
worker_gate() {
  local first second
  first=$(pm2 jlist | node -e 'let d="";process.stdin.on("data",c=>d+=c).on("end",()=>{const a=JSON.parse(d).find(x=>x.name==="thedrop-worker");process.stdout.write(a?String(a.pm2_env.restart_time):"missing")})')

  if [[ "$first" == "missing" ]]; then
    log "worker gate: thedrop-worker is not registered with pm2"
    return 1
  fi

  sleep 15
  second=$(pm2 jlist | node -e 'let d="";process.stdin.on("data",c=>d+=c).on("end",()=>{const a=JSON.parse(d).find(x=>x.name==="thedrop-worker");process.stdout.write(a?String(a.pm2_env.restart_time):"missing")})')

  if [[ "$second" != "$first" ]]; then
    log "worker gate: thedrop-worker restarted $first -> $second in 15s; it is crash-looping"
    log "             last lines of ${LOG_DIR:-$HOME/.local/state/thedrop/log}/thedrop-worker.err.log:"
    tail -n 15 "${LOG_DIR:-$HOME/.local/state/thedrop/log}/thedrop-worker.err.log" >&2 2>/dev/null || true
    return 1
  fi

  log "health gate: worker stable (no restarts in 15s)"
}

# A stable worker still says nothing about whether the SCHEDULED work runs. Celery
# catches an exception in a task, logs it, and waits for the next tick -- so a beat task
# raising every 120 seconds leaves the process perfectly healthy and every gate above
# truthfully green. That shipped: `dispatch_embedding_batches` crash-looped through two
# deploys that reported six passing gates.
#
# This runs each interval-scheduled task once and fails the deploy if any raises. Cron
# scheduled tasks are deliberately excluded -- see the script -- because running one
# early is a behaviour change, not an early tick.
beat_gate() {
  local output
  if ! output=$(cd "$APP_DIR" && PYTHONPATH="$APP_DIR/services/worker"       uv run python infrastructure/scripts/beat_smoke.py 2>&1); then
    log "beat gate: a scheduled task failed to run"
    printf '%s
' "$output" >&2
    return 1
  fi
  printf '%s
' "$output" | sed 's/^/[deploy]   /'
  log "health gate: scheduled tasks run"
}

ownership_gate || { log "route ownership gate failed"; rollback; }
asset_gate     || { log "static asset gate failed"; rollback; }
worker_gate    || { log "worker gate failed"; rollback; }
beat_gate      || { log "beat gate failed"; rollback; }

echo "$TARGET_SHA" > "$LAST_GOOD_FILE"
log "migrations at head"
log "health gate: web 200, api 200"
log "recorded last-good sha $TARGET_SHA"

find "$BACKUP_DIR" -name 'pre-deploy-*.dump' -mtime +14 -delete 2>/dev/null || true

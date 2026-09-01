#!/usr/bin/env bash
#
# THE DROP deploy.
#
# Run on the VPS as the `thedrop` user from /opt/thedrop:
#   sudo -u thedrop bash infrastructure/scripts/deploy.sh [git-ref] [--no-build]
#
# --no-build expects apps/web/.next/standalone to have been built on the desktop and
# rsynced in by infrastructure/scripts/build-and-push.sh. The bundle carries a manifest
# recording the SHA and the site URL it was built from, and this script refuses one that
# does not match -- otherwise a failed rsync silently ships yesterday's UI.
#
# Takes a database snapshot before migrating, gates on health, and rolls back
# automatically if the gate fails. Touches nothing in nginx.

set -Eeuo pipefail

APP_DIR="${APP_DIR:-/opt/thedrop}"
ENV_FILE="${ENV_FILE:-/etc/thedrop/thedrop.env}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/thedrop}"
REDIS_CONF="${REDIS_CONF:-/etc/thedrop/redis.conf}"
LAST_GOOD_FILE="$APP_DIR/.last_good_sha"
BUILD_MANIFEST_NAME=".thedrop-build"

SKIP_BUILD=0
TARGET_REF=""
for arg in "$@"; do
  case "$arg" in
    --no-build) SKIP_BUILD=1 ;;
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

[[ -f "$ENV_FILE" ]] || fail "missing $ENV_FILE - see docs/DEPLOYMENT.md §11"

PREVIOUS_SHA="$(git rev-parse HEAD)"
log "current revision $PREVIOUS_SHA"

# ---------------------------------------------------------------- fetch
if [[ -n "$TARGET_REF" ]]; then
  log "fetching $TARGET_REF"
  git fetch --all --prune
  git checkout --detach "$TARGET_REF"
else
  log "using working tree as-is"
fi
TARGET_SHA="$(git rev-parse HEAD)"

# ---------------------------------------------------------------- toolchain parity
# A Node major mismatch between build and runtime produces failures that look like
# application bugs. Check it before spending five minutes on a build.
EXPECTED_NODE="$(awk '/^nodejs/ {print $2}' .tool-versions | cut -d. -f1)"
ACTUAL_NODE="$(node -v | sed 's/^v//' | cut -d. -f1)"
[[ "$EXPECTED_NODE" == "$ACTUAL_NODE" ]] \
  || fail "node major mismatch: expected $EXPECTED_NODE, found $ACTUAL_NODE"

# ---------------------------------------------------------------- data services
# Postgres and Redis are host services under systemd (ADR-0011), not containers. This
# script does not start them -- they are enabled at boot and own their own lifecycle.
# It refuses to continue when they are unreachable, because every later failure would
# otherwise be a confusing symptom of the same cause.
# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a

PG_PORT="${POSTGRES_HOST_PORT:-5432}"
RD_PORT="${REDIS_HOST_PORT:-6380}"

log "checking data services"
systemctl is-active --quiet postgresql \
  || fail "postgresql is not running: sudo systemctl status postgresql"
systemctl is-active --quiet thedrop-redis \
  || fail "thedrop-redis is not running: sudo systemctl status thedrop-redis"

PGPASSWORD="$POSTGRES_PASSWORD" psql -h 127.0.0.1 -p "$PG_PORT" -U "$POSTGRES_USER" \
  -d "$POSTGRES_DB" -tAc 'SELECT 1' >/dev/null \
  || fail "cannot reach postgres on 127.0.0.1:$PG_PORT as $POSTGRES_USER"

redis-cli -h 127.0.0.1 -p "$RD_PORT" -a "$REDIS_PASSWORD" --no-auth-warning ping \
  2>/dev/null | grep -q PONG \
  || fail "cannot authenticate to redis on 127.0.0.1:$RD_PORT"

# pgvector is an extension, not a server feature. The container image had it
# preinstalled; a host install needs a separate package, and forgetting it surfaces as
# a failed migration several minutes into a deploy.
PGPASSWORD="$POSTGRES_PASSWORD" psql -h 127.0.0.1 -p "$PG_PORT" -U "$POSTGRES_USER" \
  -d "$POSTGRES_DB" -tAc "SELECT 1 FROM pg_available_extensions WHERE name='vector'" \
  | grep -q 1 \
  || fail "the 'vector' extension is unavailable: sudo apt install postgresql-16-pgvector"

# The Redis password lives in two files: the env file the app reads and the config file
# the server reads. A mismatch otherwise presents as a generic connection error.
if [[ -r "$REDIS_CONF" ]] && grep -q '^requirepass ' "$REDIS_CONF"; then
  CONF_PASS="$(awk '/^requirepass /{print $2; exit}' "$REDIS_CONF")"
  [[ "$CONF_PASS" == "$REDIS_PASSWORD" ]] \
    || fail "REDIS_PASSWORD in $ENV_FILE does not match requirepass in $REDIS_CONF"
fi

log "data services OK (postgres :$PG_PORT, redis :$RD_PORT)"

# ---------------------------------------------------------------- backup
mkdir -p "$BACKUP_DIR"
SNAPSHOT="$BACKUP_DIR/pre-deploy-$(date +%Y%m%d-%H%M%S).dump"
log "snapshotting database to $SNAPSHOT"
PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -h 127.0.0.1 -p "$PG_PORT" \
  -U "$POSTGRES_USER" -Fc "$POSTGRES_DB" > "$SNAPSHOT" \
  || fail "backup failed - refusing to migrate without one"

# A redirected pg_dump can still leave an empty file behind on some failures.
[[ -s "$SNAPSHOT" ]] || fail "backup file is empty - refusing to migrate"

# ---------------------------------------------------------------- build-time env gate
# NEXT_PUBLIC_* values are inlined by `next build`; setting them at runtime does
# nothing. A production release built without NEXT_PUBLIC_SITE_URL ships canonical
# URLs, a sitemap and OpenGraph tags all pointing at http://localhost:3100 -- and the
# site looks completely healthy while doing it.
if [[ "${ENVIRONMENT:-}" == "production" ]]; then
  if [[ -z "${NEXT_PUBLIC_SITE_URL:-}" ]]; then
    fail "NEXT_PUBLIC_SITE_URL is not set. It is inlined at build time and drives every canonical URL, the sitemap and OpenGraph. Set it in $ENV_FILE before deploying."
  fi
  case "$NEXT_PUBLIC_SITE_URL" in
    https://*) : ;;
    *) fail "NEXT_PUBLIC_SITE_URL must be an https:// origin in production (got: $NEXT_PUBLIC_SITE_URL)" ;;
  esac
  log "build-time env gate: NEXT_PUBLIC_SITE_URL=$NEXT_PUBLIC_SITE_URL"
fi

# ---------------------------------------------------------------- dependencies
# The Python venv is always needed: the API and worker run from it. pnpm is only needed
# when this host is doing the build -- the standalone bundle ships its own node_modules.
log "syncing python dependencies"
uv sync --frozen

STANDALONE="$APP_DIR/apps/web/.next/standalone/apps/web"
MANIFEST="$STANDALONE/$BUILD_MANIFEST_NAME"

if [[ "$SKIP_BUILD" -eq 1 ]]; then
  # ------------------------------------------------------------ prebuilt bundle
  # Building on the desktop keeps the only real CPU/RAM spike off the VPS, but it moves
  # the build-time env gate off this host too: NEXT_PUBLIC_SITE_URL is inlined by
  # `next build`, so by the time the bundle arrives here it is already baked in and
  # nothing on the VPS can fix it. The manifest is how the desktop reports what it
  # actually built, and these checks are the only thing standing between a bad rsync
  # and a site that looks healthy while serving localhost canonical URLs.
  log "skipping build; verifying prebuilt bundle"

  [[ -f "$STANDALONE/server.js" ]] \
    || fail "--no-build but no bundle at $STANDALONE/server.js - run build-and-push.sh first"
  [[ -f "$MANIFEST" ]] \
    || fail "bundle has no $BUILD_MANIFEST_NAME manifest - it was not built by build-and-push.sh"

  BUILT_SHA="$(awk -F= '/^sha=/{print $2; exit}' "$MANIFEST")"
  BUILT_URL="$(awk -F= '/^site_url=/{sub(/^site_url=/, ""); print; exit}' "$MANIFEST")"
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
  # ------------------------------------------------------------ build on this host
  log "installing node dependencies"
  pnpm install --frozen-lockfile

  # Capped so a build cannot OOM-kill Postgres on an 8GB box. The 4GB swapfile is the
  # backstop if this is still tight. Prefer --no-build with a desktop build.
  log "building web"
  NODE_OPTIONS="--max-old-space-size=1536" pnpm --filter @thedrop/web build

  # --- standalone asset copy ------------------------------------------------
  # `output: "standalone"` deliberately does NOT copy .next/static or public/ into the
  # standalone tree -- Next.js expects the deployer to do it, on the assumption a CDN
  # usually serves them. We serve them from the Node process, so without this step the
  # site returns HTML with every CSS file and JS chunk 404ing: unstyled, no theme
  # toggle, no admin login form, and error pages 500 because they cannot render.
  #
  # Verified against a real build: the standalone tree contained 0 static chunks while
  # the build produced 47.
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
fi

# ---------------------------------------------------------------- migrate
log "running migrations"
uv run alembic -c packages/database/alembic.ini upgrade head

# ---------------------------------------------------------------- restart
log "restarting services"
sudo systemctl restart thedrop-api thedrop-worker thedrop-web

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
  pnpm install --frozen-lockfile
  uv sync --frozen
  NODE_OPTIONS="--max-old-space-size=1536" pnpm --filter @thedrop/web build
  sudo systemctl restart thedrop-api thedrop-worker thedrop-web
  log "rolled back. Database snapshot preserved at $SNAPSHOT"
  log "NOTE: schema migrations are NOT auto-reverted. If this deploy migrated,"
  log "      restore manually from the snapshot. See docs/DEPLOYMENT.md §10."
  exit 1
}

wait_for "$API_URL" "api" || { log "api failed readiness"; rollback; }
wait_for "$WEB_URL" "web" || { log "web failed readiness"; rollback; }

# --- route ownership gate ---------------------------------------------------
# A homepage 200 does not prove the API tier is wired correctly. A route handler can
# exist, build, and still be shadowed by a proxy rewrite so that FastAPI answers
# instead -- that happened to the article detail route and no test caught it, because
# both tiers returned identical responses.
#
# These two checks assert the split is intact end to end:
#   * a Node-owned endpoint answers through the web port
#   * a FastAPI-owned endpoint still reaches FastAPI through the same port
ownership_gate() {
  local node_code proxied_code

  node_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
    "http://127.0.0.1:3100/api/v1/public/categories" || echo 000)
  if [[ "$node_code" != "200" ]]; then
    log "ownership gate: Node-owned /api/v1/public/categories returned $node_code (want 200)"
    return 1
  fi

  # Unauthenticated worker status must reach FastAPI and be rejected by it.
  proxied_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
    "http://127.0.0.1:3100/api/v1/worker/status" || echo 000)
  if [[ "$proxied_code" != "401" ]]; then
    log "ownership gate: proxied /api/v1/worker/status returned $proxied_code (want 401)"
    log "                the FastAPI proxy rewrite is broken or thedrop-api is down"
    return 1
  fi

  log "health gate: route ownership OK (Node 200, proxy 401)"
  return 0
}

# --- static asset gate ------------------------------------------------------
# A 200 on the homepage says nothing about whether its CSS and JS actually load: the
# HTML renders fine while every chunk 404s. This pulls a real asset URL out of the
# rendered homepage and fetches it.
asset_gate() {
  local asset code
  asset=$(curl -fsS --max-time 10 "http://127.0.0.1:3100/" \
    | grep -oE '/_next/static/[^"]+\.(css|js)' | head -1)

  if [[ -z "$asset" ]]; then
    log "asset gate: found no /_next/static reference in the homepage HTML"
    return 1
  fi

  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "http://127.0.0.1:3100$asset" || echo 000)
  if [[ "$code" != "200" ]]; then
    log "asset gate: $asset returned $code (want 200)"
    log "            static assets were not copied into the standalone tree"
    return 1
  fi

  log "health gate: static assets OK ($asset)"
  return 0
}

ownership_gate || { log "route ownership gate failed"; rollback; }
asset_gate     || { log "static asset gate failed"; rollback; }

# ---------------------------------------------------------------- done
echo "$TARGET_SHA" > "$LAST_GOOD_FILE"
log "build ok"
log "migrations at head"
log "health gate: web 200, api 200"
log "recorded last-good sha $TARGET_SHA"

# Keep 14 daily snapshots; a backup directory that fills the disk is its own outage.
find "$BACKUP_DIR" -name 'pre-deploy-*.dump' -mtime +14 -delete

#!/usr/bin/env bash
#
# THE DROP deploy.
#
# Run on the VPS as the `thedrop` user from /opt/thedrop:
#   sudo -u thedrop bash infrastructure/scripts/deploy.sh [git-ref]
#
# Takes a database snapshot before migrating, gates on health, and rolls back
# automatically if the gate fails. Touches nothing in nginx.

set -Eeuo pipefail

APP_DIR="${APP_DIR:-/opt/thedrop}"
ENV_FILE="${ENV_FILE:-/etc/thedrop/thedrop.env}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/thedrop}"
COMPOSE_FILE="$APP_DIR/infrastructure/docker/docker-compose.yml"
LAST_GOOD_FILE="$APP_DIR/.last_good_sha"
TARGET_REF="${1:-}"

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
log "ensuring postgres and redis are up"
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" ps

# ---------------------------------------------------------------- backup
mkdir -p "$BACKUP_DIR"
SNAPSHOT="$BACKUP_DIR/pre-deploy-$(date +%Y%m%d-%H%M%S).dump"
log "snapshotting database to $SNAPSHOT"
# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" exec -T postgres \
  pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB" > "$SNAPSHOT" \
  || fail "backup failed - refusing to migrate without one"

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
log "installing dependencies"
pnpm install --frozen-lockfile
uv sync --frozen

# ---------------------------------------------------------------- build
# Capped so a build cannot OOM-kill Postgres on an 8GB box. The 4GB swapfile is the
# backstop if this is still tight.
log "building web"
NODE_OPTIONS="--max-old-space-size=1536" pnpm --filter @thedrop/web build

# --- standalone asset copy --------------------------------------------------
# `output: "standalone"` deliberately does NOT copy .next/static or public/ into the
# standalone tree -- Next.js expects the deployer to do it, on the assumption a CDN
# usually serves them. We serve them from the Node process, so without this step the
# site returns HTML with every CSS file and JS chunk 404ing: unstyled, no theme
# toggle, no admin login form, and error pages 500 because they cannot render.
#
# Verified against a real build: the standalone tree contained 0 static chunks while
# the build produced 47.
STANDALONE="$APP_DIR/apps/web/.next/standalone/apps/web"
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

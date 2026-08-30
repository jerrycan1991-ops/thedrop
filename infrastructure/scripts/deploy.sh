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

# ---------------------------------------------------------------- dependencies
log "installing dependencies"
pnpm install --frozen-lockfile
uv sync --frozen

# ---------------------------------------------------------------- build
# Capped so a build cannot OOM-kill Postgres on an 8GB box. The 4GB swapfile is the
# backstop if this is still tight.
log "building web"
NODE_OPTIONS="--max-old-space-size=1536" pnpm --filter @thedrop/web build

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

# ---------------------------------------------------------------- done
echo "$TARGET_SHA" > "$LAST_GOOD_FILE"
log "build ok"
log "migrations at head"
log "health gate: web 200, api 200"
log "recorded last-good sha $TARGET_SHA"

# Keep 14 daily snapshots; a backup directory that fills the disk is its own outage.
find "$BACKUP_DIR" -name 'pre-deploy-*.dump' -mtime +14 -delete

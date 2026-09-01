#!/usr/bin/env bash
#
# Build the Next.js bundle on the DESKTOP and push it to the VPS.
#
#   bash infrastructure/scripts/build-and-push.sh [git-ref]
#
# The VPS is a publishing and coordination tier; it does not think (ARCHITECTURE.md §3).
# `next build` is the one genuinely heavy thing a deploy asks of it -- a sustained
# ~1.5 GB spike on a box that also runs Postgres, Redis and a hosting panel. Building
# here and shipping the artifact keeps that off the VPS entirely.
#
# Afterwards, on the VPS:
#   sudo -u thedrop bash /opt/thedrop/infrastructure/scripts/deploy.sh <ref> --no-build
#
# This script never touches the database, never restarts a service and never writes
# outside the standalone tree. Everything it produces is verified again on the far end.

set -Eeuo pipefail

REPO_DIR="${REPO_DIR:-$(git rev-parse --show-toplevel)}"
VPS_HOST="${VPS_HOST:-}"
VPS_PATH="${VPS_PATH:-/opt/thedrop}"
TARGET_REF="${1:-}"
MANIFEST_NAME=".thedrop-build"

log()  { echo "[build-and-push] $*"; }
fail() { echo "[build-and-push] ERROR: $*" >&2; exit 1; }

cd "$REPO_DIR" || fail "cannot enter $REPO_DIR"

[[ -n "$VPS_HOST" ]] || fail "set VPS_HOST, e.g. VPS_HOST=thedrop@151.247.197.111"

# ---------------------------------------------------------------- ref
if [[ -n "$TARGET_REF" ]]; then
  log "checking out $TARGET_REF"
  git fetch --all --prune
  git checkout --detach "$TARGET_REF"
fi
TARGET_SHA="$(git rev-parse HEAD)"

# A bundle built from a dirty tree cannot be reproduced from its recorded SHA, and the
# VPS has no way to detect that. Refuse rather than ship something untraceable.
if [[ -n "$(git status --porcelain)" ]]; then
  fail "working tree is dirty; commit or stash before building a release bundle"
fi

# ---------------------------------------------------------------- toolchain parity
# The VPS asserts this too, but failing here costs seconds instead of a full build.
EXPECTED_NODE="$(awk '/^nodejs/ {print $2}' .tool-versions | cut -d. -f1)"
ACTUAL_NODE="$(node -v | sed 's/^v//' | cut -d. -f1)"
[[ "$EXPECTED_NODE" == "$ACTUAL_NODE" ]] \
  || fail "node major mismatch: .tool-versions wants $EXPECTED_NODE, this machine has $ACTUAL_NODE"

# ---------------------------------------------------------------- build-time env gate
# NEXT_PUBLIC_SITE_URL is inlined by `next build`. Building on the desktop moves this
# gate off the VPS, so it has to be enforced here -- otherwise the bundle ships
# http://localhost:3100 in every canonical URL, sitemap entry and OpenGraph tag, and
# the site looks entirely healthy while doing it.
[[ -n "${NEXT_PUBLIC_SITE_URL:-}" ]] \
  || fail "NEXT_PUBLIC_SITE_URL is not set in this shell. Export the production value before building: export NEXT_PUBLIC_SITE_URL=https://thedrop.channel"

case "$NEXT_PUBLIC_SITE_URL" in
  https://*) : ;;
  *) fail "NEXT_PUBLIC_SITE_URL must be an https:// origin (got: $NEXT_PUBLIC_SITE_URL)" ;;
esac
log "building $TARGET_SHA for $NEXT_PUBLIC_SITE_URL"

# ---------------------------------------------------------------- build
pnpm install --frozen-lockfile
pnpm --filter @thedrop/web build

STANDALONE="$REPO_DIR/apps/web/.next/standalone/apps/web"
[[ -f "$STANDALONE/server.js" ]] || fail "standalone build missing at $STANDALONE/server.js"

# ---------------------------------------------------------------- static assets
# `output: "standalone"` does not copy .next/static or public/ into the standalone
# tree. Doing it here means the VPS receives a complete, servable bundle.
log "copying static assets into the standalone tree"
rm -rf "$STANDALONE/.next/static"
mkdir -p "$STANDALONE/.next"
cp -r "$REPO_DIR/apps/web/.next/static" "$STANDALONE/.next/static"

if [[ -d "$REPO_DIR/apps/web/public" ]]; then
  rm -rf "$STANDALONE/public"
  cp -r "$REPO_DIR/apps/web/public" "$STANDALONE/public"
fi

CHUNKS=$(find "$STANDALONE/.next/static" -name '*.js' | wc -l)
[[ "$CHUNKS" -gt 0 ]] || fail "no JS chunks in the bundle; the site would load unstyled"

# ---------------------------------------------------------------- manifest
# What the VPS checks the bundle against. Written last so a half-finished build cannot
# leave a manifest claiming success.
cat > "$STANDALONE/$MANIFEST_NAME" <<MANIFEST
sha=$TARGET_SHA
site_url=$NEXT_PUBLIC_SITE_URL
node=$EXPECTED_NODE
chunks=$CHUNKS
built_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
built_on=$(hostname)
MANIFEST

log "built $CHUNKS chunks; manifest written"

# ---------------------------------------------------------------- push
# --delete so a chunk removed by this build cannot linger and be served. The trailing
# slashes are load-bearing: they sync directory CONTENTS, not the directory itself.
log "pushing to $VPS_HOST:$VPS_PATH"
rsync -az --delete \
  "$STANDALONE/" \
  "$VPS_HOST:$VPS_PATH/apps/web/.next/standalone/apps/web/"

log "done. On the VPS run:"
log "  sudo -u thedrop bash $VPS_PATH/infrastructure/scripts/deploy.sh $TARGET_SHA --no-build"

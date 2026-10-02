#!/usr/bin/env bash
# Deploy Tykee: update code, check config, rebuild, restart, and confirm the bot came up.
#
#   ./deploy.sh              deploy the latest commit of the current branch
#   ./deploy.sh <branch>     switch to <branch> first (e.g. a PR branch to test), then deploy
#   ./deploy.sh main         go back to main
#
# Run it from anywhere; it works in the directory it lives in.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

BRANCH="${1:-}"
STARTUP_TIMEOUT_S=60

fail() { echo "❌ $*" >&2; exit 1; }
step() { echo; echo "[$1/5] $2"; }

if docker compose version >/dev/null 2>&1; then
  DC=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  DC=(docker-compose)
else
  fail "docker compose not found"
fi

[[ -f .env ]] || fail ".env not found. Run: cp .env.example .env  (then fill it in)"
grep -qE '^TELEGRAM_BOT_TOKEN=.+' .env || fail "TELEGRAM_BOT_TOKEN is empty in .env"
grep -qE '^ALLOWED_TELEGRAM_IDS=.+' .env || fail "ALLOWED_TELEGRAM_IDS is empty in .env"

# --- 1. code -----------------------------------------------------------------------------------
step 1 "Updating code"
PREV="$(git rev-parse --short HEAD)"
git fetch --prune --quiet origin
if [[ -n "$BRANCH" ]]; then
  git checkout --quiet "$BRANCH"
fi
if git symbolic-ref -q HEAD >/dev/null; then
  git pull --ff-only --quiet
fi
echo "    $(git rev-parse --abbrev-ref HEAD) @ $(git log -1 --format='%h %s')"
[[ "$PREV" == "$(git rev-parse --short HEAD)" ]] && echo "    (no new commits; rebuilding anyway)"

# --- 2. data directory -------------------------------------------------------------------------
step 2 "Checking data directory"
DATA_DIR="$(grep -E '^TYKEE_DATA_DIR=' .env | tail -1 | cut -d= -f2- | tr -d "\"'" || true)"
DATA_DIR="${DATA_DIR:-./data}"
mkdir -p "$DATA_DIR" 2>/dev/null || true
[[ -d "$DATA_DIR" ]] || fail "cannot create $DATA_DIR. Run: sudo mkdir -p $DATA_DIR && sudo chown 1000:1000 $DATA_DIR"
OWNER="$(stat -c '%u' "$DATA_DIR" 2>/dev/null || stat -f '%u' "$DATA_DIR")"
if [[ "$OWNER" != "1000" ]]; then
  if [[ "$(id -u)" == "0" ]]; then
    chown 1000:1000 "$DATA_DIR"
    echo "    fixed ownership of $DATA_DIR (now 1000:1000)"
  else
    fail "$DATA_DIR is owned by uid $OWNER, but the container runs as uid 1000. Run: sudo chown 1000:1000 $DATA_DIR"
  fi
fi
echo "    $DATA_DIR OK"

# --- 3. build ----------------------------------------------------------------------------------
step 3 "Building image"
"${DC[@]}" build

# --- 4. restart --------------------------------------------------------------------------------
step 4 "Restarting"
SINCE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
# `up -d` replaces the running container; exactly one poller ever runs (§10).
"${DC[@]}" up -d

# --- 5. verify ---------------------------------------------------------------------------------
step 5 "Waiting for the bot to start polling (up to ${STARTUP_TIMEOUT_S}s)"
for _ in $(seq "$STARTUP_TIMEOUT_S"); do
  LOGS="$("${DC[@]}" logs --no-color --since "$SINCE" tykee 2>&1 || true)"
  if grep -q '"msg": "polling"' <<<"$LOGS"; then
    echo "    ✅ polling"
    break
  fi
  if grep -q '"level": "ERROR"' <<<"$LOGS"; then
    echo "$LOGS" | tail -20
    echo
    fail "startup failed (see logs above). Roll back with: ./deploy.sh $PREV"
  fi
  sleep 1
done
grep -q '"msg": "polling"' <<<"$LOGS" || {
  echo "$LOGS" | tail -20
  fail "bot did not report 'polling' within ${STARTUP_TIMEOUT_S}s"
}

"${DC[@]}" ps
docker image prune -f >/dev/null 2>&1 || true   # drop dangling images left by rebuilds

echo
echo "✅ Deployed $(git rev-parse --abbrev-ref HEAD) @ $(git rev-parse --short HEAD)"
echo "   Logs:     ${DC[*]} logs -f tykee"
echo "   Rollback: ./deploy.sh $PREV   (then ./deploy.sh main to return to main)"

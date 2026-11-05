#!/usr/bin/env bash
# Ship the committed HEAD to the production VM and roll the container.
# Usage: deploy/ship.sh [--skip-gates]
#   DEPLOY_HOST      ssh host (default agent-ptt-test)
#   DEPLOY_SSH_CONF  ssh -F config (default ~/.ssh/agent-ptt-test.conf)
#   PUBLIC_URL       external URL to verify after rollout (default https://aptt.arvidsson.tech)
set -euo pipefail
cd "$(dirname "$0")/.."

HOST="${DEPLOY_HOST:-agent-ptt-test}"
SSH_CONF="${DEPLOY_SSH_CONF:-$HOME/.ssh/agent-ptt-test.conf}"
PUBLIC_URL="${PUBLIC_URL:-https://aptt.arvidsson.tech}"
REMOTE_DIR=/opt/agent-ptt
REL="$(date +%Y%m%d-%H%M%S)"
SHA="$(git rev-parse --short HEAD)"
TARBALL="$(mktemp -t agent-ptt-$REL).tgz"
ssh_() { ssh -F "$SSH_CONF" -o BatchMode=yes "$HOST" "$@"; }

if [ -n "$(git status --porcelain)" ]; then
  echo "✗ working tree is not clean; commit first (ship deploys HEAD only)"; exit 1
fi

if [ "${1:-}" != "--skip-gates" ]; then
  echo "→ gates"
  uv run ruff check . >/dev/null
  uv run ruff format --check . >/dev/null
  uv run pytest -q >/dev/null
  node --test tests/workspace-ui.test.mjs >/dev/null 2>&1
fi

echo "→ package $SHA"
git archive --format=tar HEAD | gzip > "$TARBALL"

echo "→ push to $HOST"
scp -q -F "$SSH_CONF" "$TARBALL" "$HOST:/tmp/agent-ptt-release.tgz"
rm "$TARBALL"

ssh_ bash -s "$REL" "$SHA" <<'REMOTE'
set -euo pipefail
REL="$1"; SHA="$2"
cd /opt/agent-ptt
C="sudo docker compose --env-file deploy/.env -f deploy/compose.yaml -f deploy/compose.proxmox.yaml"
BK="backups/$REL"
mkdir -p "$BK"

echo "  · backup database → $BK"
$C exec -T database pg_dump -U agent_ptt -d agent_ptt | gzip > "$BK/agent_ptt.sql.gz"
sudo docker tag agent-ptt:production "agent-ptt:before-$REL" 2>/dev/null || true

echo "  · replace source (deploy/.env and backups/ preserved)"
find . -mindepth 1 -maxdepth 1 ! -name deploy ! -name backups -exec rm -rf {} +
tar -xzf /tmp/agent-ptt-release.tgz -C . && rm /tmp/agent-ptt-release.tgz
echo "$SHA" > RELEASE

echo "  · build image"
$C build --quiet server

echo "  · migrate"
$C run --rm migrate >/dev/null

echo "  · roll server"
$C up -d --no-deps --force-recreate server >/dev/null

for i in $(seq 1 60); do
  if curl -sf http://127.0.0.1:8080/health/ready >/dev/null; then echo "  · ready"; break; fi
  [ "$i" = 60 ] && { echo "  ✗ not ready; logs:"; $C logs --tail=50 server; exit 1; }
  sleep 2
done

ls -1dt backups/*/ | tail -n +6 | xargs -r rm -rf
sudo docker images "agent-ptt" --format '{{.Tag}}' | grep '^before-' | sort -r | tail -n +4 \
  | xargs -r -I{} sudo docker rmi -f "agent-ptt:{}" >/dev/null
REMOTE

echo "→ public check"
for i in $(seq 1 15); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "$PUBLIC_URL/health/ready" || true)"
  [ "$code" = "200" ] && { echo "✓ $SHA live at $PUBLIC_URL"; exit 0; }
  sleep 2
done
echo "✗ $PUBLIC_URL/health/ready returned $code (container is up; check the tunnel)"; exit 1

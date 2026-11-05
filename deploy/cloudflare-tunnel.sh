#!/usr/bin/env bash
# Publish the production VM at a public hostname through a Cloudflare Tunnel (no Access layer).
# Usage:
#   deploy/cloudflare-tunnel.sh create    # tunnel + ingress + DNS in Cloudflare; writes deploy/.cf-*
#   deploy/cloudflare-tunnel.sh install   # cloudflared service on the VM, switch the app to the public origin
#   CF_API_TOKEN_FILE  file holding a Cloudflare API token with Tunnel:Edit, DNS:Edit
#   HOSTNAME           public hostname (default aptt.arvidsson.tech)
#   DEPLOY_HOST / DEPLOY_SSH_CONF  as in ship.sh
set -euo pipefail
cd "$(dirname "$0")/.."

HOSTNAME="${HOSTNAME:-aptt.arvidsson.tech}"
ZONE="${HOSTNAME#*.}"
TUNNEL_NAME="${TUNNEL_NAME:-agent-ptt}"
HOST="${DEPLOY_HOST:-agent-ptt-test}"
SSH_CONF="${DEPLOY_SSH_CONF:-$HOME/.ssh/agent-ptt-test.conf}"
API=https://api.cloudflare.com/client/v4

cf() { # cf METHOD PATH [JSON]
  local method="$1" path="$2" data="${3:-}"
  curl -sS -X "$method" "$API$path" -H "Authorization: Bearer $CF_TOKEN" -H "Content-Type: application/json" \
    ${data:+--data "$data"} | python3 -c '
import sys, json
d = json.load(sys.stdin)
if not d.get("success"):
    sys.exit("cloudflare: " + json.dumps(d.get("errors")))
print(json.dumps(d["result"]))'
}
jq_() { python3 -c "import sys, json; r = json.load(sys.stdin); print($1)"; }

create() {
  : "${CF_API_TOKEN_FILE:?set CF_API_TOKEN_FILE to a file containing the Cloudflare API token}"
  CF_TOKEN="$(tr -d '[:space:]' < "$CF_API_TOKEN_FILE")"

  zone="$(cf GET "/zones?name=$ZONE")"
  ZONE_ID="$(echo "$zone" | jq_ 'r[0]["id"]')"
  ACCOUNT_ID="$(echo "$zone" | jq_ 'r[0]["account"]["id"]')"

  existing="$(cf GET "/accounts/$ACCOUNT_ID/cfd_tunnel?name=$TUNNEL_NAME&is_deleted=false")"
  if [ "$(echo "$existing" | jq_ 'len(r)')" = "0" ]; then
    echo "→ create tunnel $TUNNEL_NAME"
    TUNNEL_ID="$(cf POST "/accounts/$ACCOUNT_ID/cfd_tunnel" "{\"name\":\"$TUNNEL_NAME\",\"config_src\":\"cloudflare\"}" | jq_ 'r["id"]')"
  else
    TUNNEL_ID="$(echo "$existing" | jq_ 'r[0]["id"]')"
    echo "→ tunnel $TUNNEL_NAME exists ($TUNNEL_ID)"
  fi

  echo "→ ingress $HOSTNAME → http://127.0.0.1:8080"
  cf PUT "/accounts/$ACCOUNT_ID/cfd_tunnel/$TUNNEL_ID/configurations" "{\"config\":{\"ingress\":[
    {\"hostname\":\"$HOSTNAME\",\"service\":\"http://127.0.0.1:8080\"},
    {\"service\":\"http_status:404\"}]}}" >/dev/null

  target="$TUNNEL_ID.cfargotunnel.com"
  rec="$(cf GET "/zones/$ZONE_ID/dns_records?name=$HOSTNAME")"
  body="{\"type\":\"CNAME\",\"name\":\"$HOSTNAME\",\"content\":\"$target\",\"proxied\":true,\"ttl\":1}"
  if [ "$(echo "$rec" | jq_ 'len(r)')" = "0" ]; then
    echo "→ DNS CNAME $HOSTNAME → $target"; cf POST "/zones/$ZONE_ID/dns_records" "$body" >/dev/null
  else
    echo "→ DNS CNAME $HOSTNAME updated"; cf PUT "/zones/$ZONE_ID/dns_records/$(echo "$rec" | jq_ 'r[0]["id"]')" "$body" >/dev/null
  fi

  umask 077
  echo "$TUNNEL_ID" > deploy/.cf-tunnel-id
  cf GET "/accounts/$ACCOUNT_ID/cfd_tunnel/$TUNNEL_ID/token" | jq_ 'r' > deploy/.cf-tunnel-token
  echo "✓ tunnel ready; run: deploy/cloudflare-tunnel.sh install"
}

install() {
  [ -s deploy/.cf-tunnel-token ] || { echo "✗ deploy/.cf-tunnel-token missing; run create first"; exit 1; }
  echo "→ install cloudflared on $HOST and switch origin to https://$HOSTNAME"
  ssh -F "$SSH_CONF" -o BatchMode=yes "$HOST" bash -s "$HOSTNAME" "$(cat deploy/.cf-tunnel-token)" <<'REMOTE'
set -euo pipefail
HOSTNAME="$1"; TOKEN="$2"
if ! command -v cloudflared >/dev/null; then
  sudo mkdir -p --mode=0755 /usr/share/keyrings
  curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
  echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared $(lsb_release -cs) main" \
    | sudo tee /etc/apt/sources.list.d/cloudflared.list >/dev/null
  sudo apt-get update -qq && sudo apt-get install -y -qq cloudflared >/dev/null
fi
if systemctl is-enabled cloudflared >/dev/null 2>&1; then
  sudo cloudflared service uninstall >/dev/null 2>&1 || true
fi
sudo cloudflared service install "$TOKEN" >/dev/null
sudo systemctl restart cloudflared

cd /opt/agent-ptt
set_env() { grep -q "^$1=" deploy/.env && sudo sed -i "s#^$1=.*#$1=$2#" deploy/.env || echo "$1=$2" | sudo tee -a deploy/.env >/dev/null; }
set_env AGENT_PTT_PUBLIC_ORIGIN "https://$HOSTNAME"
set_env AGENT_PTT_SIGNUP 0
sudo docker compose --env-file deploy/.env -f deploy/compose.yaml -f deploy/compose.proxmox.yaml up -d --no-deps --force-recreate server >/dev/null
for i in $(seq 1 30); do curl -sf http://127.0.0.1:8080/health/ready >/dev/null && break; sleep 2; done
systemctl is-active cloudflared
REMOTE
  for i in $(seq 1 20); do
    curl -sf "https://$HOSTNAME/health/ready" >/dev/null && { echo "✓ https://$HOSTNAME/workspace/"; exit 0; }
    sleep 3
  done
  echo "✗ https://$HOSTNAME not answering yet (DNS may still be propagating)"; exit 1
}

case "${1:-}" in
  create) create ;;
  install) install ;;
  *) echo "usage: $0 create|install"; exit 2 ;;
esac

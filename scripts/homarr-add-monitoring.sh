#!/bin/bash
# Add Dozzle and Watchtower to a running Homarr instance
# Usage: HOMARR_API_KEY=<your-key> bash homarr-add-monitoring.sh

SERVER_IP="${SERVER_IP:?Set SERVER_IP env var (your services VM IP)}"
HOMARR="http://$SERVER_IP:7575"
API_KEY="${HOMARR_API_KEY:?Set HOMARR_API_KEY env var}"
ICON_BASE="https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/svg"

create_app() {
  local name="$1" href="$2" ping_url="$3" icon="$4"
  result=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$HOMARR/api/apps" \
    -H "ApiKey: $API_KEY" \
    -H "Content-Type: application/json" \
    -d "{\"name\":\"$name\",\"description\":null,\"iconUrl\":\"$icon\",\"href\":\"$href\",\"pingUrl\":\"$ping_url\"}")
  if [ "$result" = "200" ]; then
    echo "  ✓ $name"
  else
    echo "  ✗ $name (HTTP $result)"
  fi
}

echo "=== Adding monitoring apps to Homarr ==="
create_app "Dozzle"     "http://$SERVER_IP:8888"                    "http://$SERVER_IP:8888"  "$ICON_BASE/dozzle.svg"
create_app "Watchtower" "http://$SERVER_IP:8888/container/watchtower" "http://$SERVER_IP:8888"  "$ICON_BASE/watchtower.svg"
echo "Done."

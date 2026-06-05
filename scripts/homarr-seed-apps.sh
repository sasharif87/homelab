#!/bin/bash
# Seed all homelab apps into Homarr via API
# Usage: HOMARR_API_KEY=<your-key> bash homarr-seed-apps.sh

SERVER_IP="${SERVER_IP:?Set SERVER_IP env var (your services VM IP)}"
PROXMOX_IP="${PROXMOX_IP:?Set PROXMOX_IP env var (your Proxmox host IP)}"
HOMARR="http://$SERVER_IP:7575"
API_KEY="${HOMARR_API_KEY:?Set HOMARR_API_KEY env var}"

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

ICON_BASE="https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/svg"

echo "=== Infrastructure ==="
create_app "Portainer"            "http://$SERVER_IP:9443"  "http://$SERVER_IP:9443"  "$ICON_BASE/portainer.svg"
create_app "Nginx Proxy Manager"  "http://$SERVER_IP:8181"  "http://$SERVER_IP:8181"  "$ICON_BASE/nginx-proxy-manager.svg"
create_app "AdGuard Home"         "http://$SERVER_IP:3003"  "http://$SERVER_IP:3003"  "$ICON_BASE/adguard-home.svg"
create_app "Uptime Kuma"          "http://$SERVER_IP:3001"  "http://$SERVER_IP:3001"  "$ICON_BASE/uptime-kuma.svg"
create_app "Duplicati"            "http://$SERVER_IP:8200"  "http://$SERVER_IP:8200"  "$ICON_BASE/duplicati.svg"
create_app "Dozzle"               "http://$SERVER_IP:8888"  "http://$SERVER_IP:8888"  "$ICON_BASE/dozzle.svg"
create_app "Netdata"              "http://$PROXMOX_IP:19999" "http://$PROXMOX_IP:19999" "$ICON_BASE/netdata.svg"
create_app "Proxmox"              "https://$PROXMOX_IP:8006" "https://$PROXMOX_IP:8006" "$ICON_BASE/proxmox.svg"

echo "=== Media ==="
create_app "Jellyfin"        "http://$SERVER_IP:8096"  "http://$SERVER_IP:8096"  "$ICON_BASE/jellyfin.svg"
create_app "Immich"          "http://$SERVER_IP:2283"  "http://$SERVER_IP:2283"  "$ICON_BASE/immich.svg"
create_app "Audiobookshelf"  "http://$SERVER_IP:13378" "http://$SERVER_IP:13378" "$ICON_BASE/audiobookshelf.svg"
create_app "Calibre-Web"     "http://$SERVER_IP:8085"  "http://$SERVER_IP:8085"  "$ICON_BASE/calibre-web.svg"

echo "=== Automation ==="
create_app "Sonarr"          "http://$SERVER_IP:8989"  "http://$SERVER_IP:8989"  "$ICON_BASE/sonarr.svg"
create_app "Radarr"          "http://$SERVER_IP:7878"  "http://$SERVER_IP:7878"  "$ICON_BASE/radarr.svg"
create_app "Lidarr"          "http://$SERVER_IP:8686"  "http://$SERVER_IP:8686"  "$ICON_BASE/lidarr.svg"
create_app "Prowlarr"        "http://$SERVER_IP:9696"  "http://$SERVER_IP:9696"  "$ICON_BASE/prowlarr.svg"
create_app "Bazarr"          "http://$SERVER_IP:6767"  "http://$SERVER_IP:6767"  "$ICON_BASE/bazarr.svg"
create_app "LazyLibrarian"   "http://$SERVER_IP:5299"  "http://$SERVER_IP:5299"  "$ICON_BASE/lazylibrarian.svg"
create_app "qBittorrent"     "http://$SERVER_IP:8082"  "http://$SERVER_IP:8082"  "$ICON_BASE/qbittorrent.svg"

echo "=== Home ==="
create_app "Vaultwarden"  "http://$SERVER_IP:8080"  "http://$SERVER_IP:8080"  "$ICON_BASE/vaultwarden.svg"
create_app "Homebox"      "http://$SERVER_IP:7745"  "http://$SERVER_IP:7745"  "$ICON_BASE/homebox.svg"
create_app "Mealie"       "http://$SERVER_IP:9925"  "http://$SERVER_IP:9925"  "$ICON_BASE/mealie.svg"

echo "=== Knowledge ==="
create_app "Kiwix"      "http://$SERVER_IP:8086"  "http://$SERVER_IP:8086"  "$ICON_BASE/kiwix.svg"
create_app "Kolibri"    "http://$SERVER_IP:8088"  "http://$SERVER_IP:8088"  "$ICON_BASE/kolibri.svg"
create_app "Tileserver" "http://$SERVER_IP:8087"  "http://$SERVER_IP:8087"  "$ICON_BASE/maptiler.svg"

echo "=== AI ==="
create_app "Ollama"  "http://$SERVER_IP:11434"  "http://$SERVER_IP:11434"  "$ICON_BASE/ollama.svg"

echo "Done."

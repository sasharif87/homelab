#!/bin/bash
# Seed all homelab apps into Homarr via API
# Usage: HOMARR_API_KEY=<your-key> bash homarr-seed-apps.sh

HOMARR="http://<server-ip>:7575"
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
create_app "Portainer"            "http://<server-ip>:9443"  "http://<server-ip>:9443"  "$ICON_BASE/portainer.svg"
create_app "Nginx Proxy Manager"  "http://<server-ip>:8181"  "http://<server-ip>:8181"  "$ICON_BASE/nginx-proxy-manager.svg"
create_app "AdGuard Home"         "http://<server-ip>:3003"  "http://<server-ip>:3003"  "$ICON_BASE/adguard-home.svg"
create_app "Uptime Kuma"          "http://<server-ip>:3001"  "http://<server-ip>:3001"  "$ICON_BASE/uptime-kuma.svg"
create_app "Duplicati"            "http://<server-ip>:8200"  "http://<server-ip>:8200"  "$ICON_BASE/duplicati.svg"
create_app "Dozzle"               "http://<server-ip>:8888"  "http://<server-ip>:8888"  "$ICON_BASE/dozzle.svg"
create_app "Netdata"              "http://<proxmox-ip>:19999" "http://<proxmox-ip>:19999" "$ICON_BASE/netdata.svg"
create_app "Proxmox"              "https://<proxmox-ip>:8006" "https://<proxmox-ip>:8006" "$ICON_BASE/proxmox.svg"

echo "=== Media ==="
create_app "Jellyfin"        "http://<server-ip>:8096"  "http://<server-ip>:8096"  "$ICON_BASE/jellyfin.svg"
create_app "Immich"          "http://<server-ip>:2283"  "http://<server-ip>:2283"  "$ICON_BASE/immich.svg"
create_app "Audiobookshelf"  "http://<server-ip>:13378" "http://<server-ip>:13378" "$ICON_BASE/audiobookshelf.svg"
create_app "Calibre-Web"     "http://<server-ip>:8085"  "http://<server-ip>:8085"  "$ICON_BASE/calibre-web.svg"

echo "=== Automation ==="
create_app "Sonarr"          "http://<server-ip>:8989"  "http://<server-ip>:8989"  "$ICON_BASE/sonarr.svg"
create_app "Radarr"          "http://<server-ip>:7878"  "http://<server-ip>:7878"  "$ICON_BASE/radarr.svg"
create_app "Lidarr"          "http://<server-ip>:8686"  "http://<server-ip>:8686"  "$ICON_BASE/lidarr.svg"
create_app "Prowlarr"        "http://<server-ip>:9696"  "http://<server-ip>:9696"  "$ICON_BASE/prowlarr.svg"
create_app "Bazarr"          "http://<server-ip>:6767"  "http://<server-ip>:6767"  "$ICON_BASE/bazarr.svg"
create_app "Bookshelf Audio" "http://<server-ip>:8787"  "http://<server-ip>:8787"  "$ICON_BASE/readarr.svg"
create_app "Bookshelf eBook" "http://<server-ip>:8788"  "http://<server-ip>:8788"  "$ICON_BASE/readarr.svg"
create_app "qBittorrent"     "http://<server-ip>:8082"  "http://<server-ip>:8082"  "$ICON_BASE/qbittorrent.svg"

echo "=== Home ==="
create_app "Vaultwarden"  "http://<server-ip>:8080"  "http://<server-ip>:8080"  "$ICON_BASE/vaultwarden.svg"
create_app "Homebox"      "http://<server-ip>:7745"  "http://<server-ip>:7745"  "$ICON_BASE/homebox.svg"
create_app "Mealie"       "http://<server-ip>:9925"  "http://<server-ip>:9925"  "$ICON_BASE/mealie.svg"

echo "=== Knowledge ==="
create_app "Kiwix"      "http://<server-ip>:8086"  "http://<server-ip>:8086"  "$ICON_BASE/kiwix.svg"
create_app "Kolibri"    "http://<server-ip>:8088"  "http://<server-ip>:8088"  "$ICON_BASE/kolibri.svg"
create_app "Tileserver" "http://<server-ip>:8087"  "http://<server-ip>:8087"  "$ICON_BASE/maptiler.svg"

echo "=== AI ==="
create_app "Ollama"  "http://<server-ip>:11434"  "http://<server-ip>:11434"  "$ICON_BASE/ollama.svg"

echo "Done."

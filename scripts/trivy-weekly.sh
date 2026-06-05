#!/bin/bash
# Weekly container image vulnerability scan — runs Sunday 5am CDT, one hour after Watchtower.
# Scans all running container images for CRITICAL CVEs and alerts Discord if found.
# Requires: trivy installed natively (apt install trivy), /etc/container-watchdog.env for webhook.
set -euo pipefail

set -a; source /etc/container-watchdog.env; set +a

SEVERITY="CRITICAL"
FINDINGS=()
ERRORS=()

notify_ntfy() {
    local msg="$1"
    local priority="${2:-high}"
    [ -z "${NTFY_URL:-}" ] && return 0
    curl -fsS -u "${NTFY_USER}:${NTFY_PASS}" \
        -H "Priority: ${priority}" \
        -d "$msg" "${NTFY_URL}/${NTFY_TOPIC}" > /dev/null 2>&1 || true
}

notify_discord() {
    local msg="$1"
    local color="${2:-16711680}"
    DISCORD_MSG="$msg" DISCORD_COLOR="$color" python3 -c "
import json, os, subprocess
payload = json.dumps({
    'embeds': [{'description': os.environ['DISCORD_MSG'], 'color': int(os.environ['DISCORD_COLOR'])}]
})
subprocess.run(
    ['curl', '-sS', '-X', 'POST', os.environ.get('DISCORD_WEBHOOK_URL', ''),
     '-H', 'Content-Type: application/json', '-d', payload],
    timeout=10, capture_output=True
)
" || true
}

IMAGES=$(docker ps --format '{{.Image}}' | sort -u)

if [ -z "$IMAGES" ]; then
    echo "trivy-weekly: no running containers"
    exit 0
fi

IMAGE_COUNT=$(echo "$IMAGES" | wc -l)
echo "trivy-weekly: scanning $IMAGE_COUNT images for $SEVERITY CVEs..."

# Update trivy vulnerability database once before scanning
trivy image --download-db-only --quiet 2>/dev/null || true

while IFS= read -r image; do
    count=$(trivy image \
        --severity "$SEVERITY" \
        --skip-db-update \
        --quiet \
        --format json \
        "$image" 2>/dev/null \
        | python3 -c "
import json, sys
data = json.load(sys.stdin)
total = sum(len(r.get('Vulnerabilities') or []) for r in data.get('Results', []))
print(total)
" 2>/dev/null || echo "error")

    if [ "$count" = "error" ]; then
        ERRORS+=("$image")
    elif [ "$count" -gt 0 ]; then
        FINDINGS+=("$image: **${count}** CRITICAL CVE(s)")
    fi
done <<< "$IMAGES"

if [ ${#FINDINGS[@]} -eq 0 ] && [ ${#ERRORS[@]} -eq 0 ]; then
    echo "trivy-weekly: all $IMAGE_COUNT images clean"
    exit 0
fi

MSG=":shield: **Trivy Weekly Scan**\n"

if [ ${#FINDINGS[@]} -gt 0 ]; then
    MSG+=":rotating_light: **CRITICAL CVEs found:**\n"
    for f in "${FINDINGS[@]}"; do
        MSG+="• $f\n"
    done
fi

if [ ${#ERRORS[@]} -gt 0 ]; then
    MSG+="\n:warning: **Scan errors:**\n"
    for e in "${ERRORS[@]}"; do
        MSG+="• $e\n"
    done
fi

notify_discord "$MSG" 16711680
notify_ntfy "$MSG"
echo "trivy-weekly: ${#FINDINGS[@]} vulnerable images, ${#ERRORS[@]} errors — Discord + ntfy notified"

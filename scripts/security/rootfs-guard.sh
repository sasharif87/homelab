#!/bin/bash
# Root filesystem guard — runs nightly at 2 AM.
# Detects files or directories that have accumulated on the root partition under
# /mnt, which only happens when a path is used before its NFS/zvol mount is
# active. All data under /mnt should live on a mount, never on the root ext4.
#
# Uses `find -xdev` to stay on the root filesystem. Any result at depth >= 2
# means something leaked onto the root partition.
set -euo pipefail

set -a; source /etc/container-watchdog.env; set +a

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

LEAKS=$(find /mnt -xdev -mindepth 2 2>/dev/null || true)

if [ -z "$LEAKS" ]; then
    echo "rootfs-guard: clean"
    exit 0
fi

# Get sizes for each leaked top-level entry
DETAILS=$(echo "$LEAKS" | awk -F/ 'NF==4{print}' | sort -u | xargs -I{} du -sh {} 2>/dev/null | sort -rh | head -20 || true)
ROOT_USED=$(df -h / | awk 'NR==2{print $3"/"$2" ("$5" used)"}')
COUNT=$(echo "$LEAKS" | wc -l)

MSG=$(printf ':warning: **Root filesystem leak detected** (%d unexpected paths under /mnt)\n\nRoot disk: %s\n\n```\n%s\n```\n\nThese paths are on the root ext4 partition, not on any NFS/zvol mount. Check fstab for a missing mount entry.' \
    "$COUNT" "$ROOT_USED" "$DETAILS")

notify_discord "$MSG" 16711680
notify_ntfy "$MSG" urgent
echo "rootfs-guard: $COUNT leaked paths found — Discord + ntfy notified"
# 10 = check ran and reported findings; already paged, so OnFailure= stays out of it.
exit 10

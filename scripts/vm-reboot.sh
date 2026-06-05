#!/bin/bash
# Sends a Discord alert then reboots the VM.
# Called by vm-reboot.timer (weekly) or manually: ./vm-reboot.sh "reason"
set -euo pipefail

set -a; source /etc/container-watchdog.env; set +a

REASON="${1:-Weekly maintenance reboot}"

curl -sS -X POST "$DISCORD_WEBHOOK_URL" \
    -H "Content-Type: application/json" \
    -d "{\"embeds\":[{\"description\":\":construction: **Services VM rebooting.** ${REASON}.\",\"color\":16763904}]}" \
    || true

[ -n "${NTFY_URL:-}" ] && curl -fsS -u "${NTFY_USER}:${NTFY_PASS}" \
    -H "Priority: default" \
    -d "Services VM rebooting — ${REASON}." \
    "${NTFY_URL}/${NTFY_TOPIC}" > /dev/null 2>&1 || true

sleep 5
/sbin/shutdown -r now "$REASON"

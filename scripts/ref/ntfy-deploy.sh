#!/bin/bash
# One-shot: wire ntfy alongside Discord for all homelab notification sources.
# Reads credentials from compose/.env (NTFY_USER, NTFY_PASS must be set there).
# Run from the repo root on the Windows machine via Git Bash / WSL.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VM="root@<server-ip>"
PROXMOX="root@<proxmox-ip>"
SSH_KEY="$HOME/.ssh/id_ed25519"
SSH="ssh -i $SSH_KEY"
SCP="scp -i $SSH_KEY"

NTFY_SERVER="https://ntfy.<your-domain>"
NTFY_TOPIC="homelab"

# Read credentials from compose/.env
NTFY_USER=$(grep '^NTFY_USER=' "$REPO_ROOT/compose/.env" | cut -d= -f2-)
NTFY_PASS=$(grep '^NTFY_PASS=' "$REPO_ROOT/compose/.env" | cut -d= -f2-)
NTFY_SHOUTRRR="ntfy://${NTFY_USER}:${NTFY_PASS}@ntfy.<your-domain>/homelab-updates"

if [ -z "$NTFY_USER" ] || [ -z "$NTFY_PASS" ]; then
    echo "ERROR: NTFY_USER and NTFY_PASS must be set in compose/.env"
    exit 1
fi

echo "=== 1/5  Copying updated scripts to VM ==="
$SCP "$REPO_ROOT/scripts/container-watchdog.py" "$VM:/opt/scripts/"
$SCP "$REPO_ROOT/scripts/boot-check.py"         "$VM:/opt/scripts/"
$SCP "$REPO_ROOT/scripts/rootfs-guard.sh"        "$VM:/opt/scripts/"
$SCP "$REPO_ROOT/scripts/preflight-check.sh"    "$VM:/opt/scripts/"
$SCP "$REPO_ROOT/scripts/maintenance-reboot.sh" "$VM:/opt/scripts/"
$SCP "$REPO_ROOT/scripts/vm-reboot.sh"          "$VM:/opt/scripts/"
$SCP "$REPO_ROOT/scripts/trivy-weekly.sh"        "$VM:/opt/scripts/"
$SSH "$VM" "chmod +x /opt/scripts/*.sh /opt/scripts/*.py"
echo "    done."

echo "=== 2/5  Adding ntfy creds to /etc/container-watchdog.env ==="
$SSH "$VM" bash <<EOF
# Remove any existing ntfy lines then re-add
sed -i '/^NTFY_/d' /etc/container-watchdog.env
printf "NTFY_URL=%s\nNTFY_USER=%s\nNTFY_PASS='%s'\nNTFY_TOPIC=%s\n" \
    "${NTFY_SERVER}" "${NTFY_USER}" "${NTFY_PASS}" "${NTFY_TOPIC}" \
    >> /etc/container-watchdog.env
echo "    /etc/container-watchdog.env updated:"
grep NTFY /etc/container-watchdog.env
EOF

echo "=== 3/5  Adding WATCHTOWER_NTFY_URL to compose/.env and redeploying Watchtower ==="
$SSH "$VM" bash <<EOF
ENVFILE="/mnt/apps/compose/.env"
sed -i '/^WATCHTOWER_NTFY_URL=/d' "\$ENVFILE"
echo "WATCHTOWER_NTFY_URL=${NTFY_SHOUTRRR}" >> "\$ENVFILE"
echo "    compose/.env updated."
EOF
$SCP "$REPO_ROOT/compose/watchtower.yml" "$VM:/mnt/apps/compose/"
$SSH "$VM" "cd /mnt/apps/compose && docker compose -f watchtower.yml up -d"
echo "    Watchtower redeployed."

echo "=== 4/5  Restarting container-watchdog on VM ==="
$SSH "$VM" "systemctl restart container-watchdog"
echo "    container-watchdog restarted."

echo "=== 5/5  Adding ntfy to Netdata on Proxmox ==="
$SSH "$PROXMOX" bash <<EOF
NOTIFY_CONF="/etc/netdata/health_alarm_notify.conf"
# Remove existing ntfy block if present, then append
sed -i '/^SEND_NTFY\|^NTFY_URL\|^NTFY_USERNAME\|^NTFY_PASSWORD\|^DEFAULT_RECIPIENT_NTFY/d' "\$NOTIFY_CONF"
printf "SEND_NTFY=\"YES\"\nNTFY_URL=\"%s\"\nNTFY_USERNAME=\"%s\"\nNTFY_PASSWORD='%s'\nDEFAULT_RECIPIENT_NTFY=\"%s\"\n" \
    "${NTFY_SERVER}" "${NTFY_USER}" "${NTFY_PASS}" "${NTFY_TOPIC}" \
    >> "\$NOTIFY_CONF"
echo "    Netdata config updated — restarting Netdata..."
systemctl restart netdata
echo "    Netdata restarted."
EOF

echo ""
echo "=== Done! ==="
echo "Remaining manual step:"
echo "  Uptime Kuma → Settings → Notifications → Add → ntfy"
echo "    Server URL: ${NTFY_SERVER}"
echo "    Topic:      ${NTFY_TOPIC}"
echo "    Username:   ${NTFY_USER}"
echo "    Password:   (from secrets.md)"

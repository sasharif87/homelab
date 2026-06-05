#!/bin/bash
# Install and configure Sanoid on the Proxmox HOST for automated ZFS snapshot management.
# Run this directly on the Proxmox host — NOT inside the Services VM.
# Usage: bash sanoid-proxmox-install.sh

set -e

echo "=== Installing Sanoid ==="
apt-get update -qq
apt-get install -y sanoid

echo "=== Deploying config ==="
mkdir -p /etc/sanoid
cp "$(dirname "$0")/sanoid.conf" /etc/sanoid/sanoid.conf

echo "=== Enabling systemd timer ==="
systemctl enable --now sanoid.timer

echo ""
echo "Done. Verify before trusting:"
echo "  1. Check your pool names match sanoid.conf:  zpool list && zfs list"
echo "  2. Check timer is active:                    systemctl status sanoid.timer"
echo "  3. Trigger a dry run:                        sanoid --take-snapshots --verbose"
echo "  4. Confirm first snapshots appear:           zfs list -t snapshot | head -20"

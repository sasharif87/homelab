#!/bin/bash
# GPU health heartbeat — runs every 20h via systemd timer.
# Checks nvidia-smi to confirm the GPU is accessible, then pushes status
# to the Uptime Kuma push monitor (25h window).
set -euo pipefail

ENV_FILE="/etc/gpu-health.env"
[ -f "$ENV_FILE" ] && source "$ENV_FILE"
: "${PUSH_URL:?PUSH_URL must be set in $ENV_FILE}"

if nvidia-smi --query-gpu=name,temperature.gpu,power.draw,memory.used,memory.total \
              --format=csv,noheader,nounits > /tmp/gpu-health-output 2>&1; then
    GPU_INFO=$(cat /tmp/gpu-health-output)
    MSG=$(echo "$GPU_INFO" | tr ',' '|' | sed 's/^ //g')
    curl -fsS "${PUSH_URL}?status=up&msg=OK&ping=" > /dev/null
    echo "gpu-health: OK — $MSG"
else
    ERR=$(cat /tmp/gpu-health-output | head -3 | tr '\n' ' ')
    curl -fsS "${PUSH_URL}?status=down&msg=nvidia-smi+failed&ping=" > /dev/null || true
    echo "gpu-health: FAILED — $ERR"
    exit 1
fi

#!/bin/sh
# Called by Gluetun whenever ProtonVPN assigns a new forwarded port.
# Updates qBittorrent's listen port via its API so incoming connections land correctly.
PORT=$1

# qBittorrent starts after gluetun, so give it time to be ready.
sleep 30

ATTEMPTS=0
MAX_ATTEMPTS=10
while [ $ATTEMPTS -lt $MAX_ATTEMPTS ]; do
  wget -q \
    --post-data "username=admin&password=${QBIT_PASS}" \
    --save-cookies /tmp/qbt_cookies.txt \
    --keep-session-cookies \
    -O /dev/null \
    http://localhost:8080/api/v2/auth/login && \
  wget -q \
    --post-data "json={\"listen_port\":$PORT}" \
    --load-cookies /tmp/qbt_cookies.txt \
    -O /dev/null \
    http://localhost:8080/api/v2/app/setPreferences && \
  echo "[port-forward] qBittorrent listen port updated to $PORT" && \
  exit 0

  ATTEMPTS=$((ATTEMPTS + 1))
  echo "[port-forward] attempt $ATTEMPTS failed, retrying in 15s..."
  sleep 15
done

echo "[port-forward] ERROR: failed to update qBittorrent after $MAX_ATTEMPTS attempts"
exit 1

#!/bin/bash
# Staged Docker startup — run after VM boot
LOG=/var/log/staged-startup.log
echo "=== Staged startup $(date) ===" >> $LOG

cd /mnt/apps/compose || exit 1

up() { docker compose -f "$1" up -d >> $LOG 2>&1 && echo "  OK: $1" || echo "  FAIL: $1"; }

echo "1. Infrastructure" >> $LOG
up npm.yml; up portainer.yml; up vaultwarden.yml
up uptime-kuma.yml; up homarr.yml; up duplicati.yml; up adguard.yml
up dozzle.yml; up watchtower.yml; up ntfy.yml; up forgejo.yml; up crowdsec.yml
sleep 10

echo "2. Media" >> $LOG
up jellyfin.yml; up audiobookshelf.yml
up calibre.yml; up calibre-web.yml; up homebox.yml; up navidrome.yml
sleep 10

echo "3. Knowledge" >> $LOG
up knowledge.yml
sleep 5

echo "4. Immich" >> $LOG
up immich.yml
sleep 10

echo "5. VPN stack" >> $LOG
up gluetun.yml
sleep 15

echo "6. AI" >> $LOG
up ollama.yml; up open-webui.yml

echo "7. Tools" >> $LOG
up profilarr.yml; up tubearchivist.yml; up samba.yml

echo "=== Done $(date) ===" >> $LOG

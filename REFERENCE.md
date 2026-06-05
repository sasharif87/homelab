# Reference

Quick reference for common operations, patterns, and decisions. Assumes you have a working homelab — see [GETTING_STARTED.md](GETTING_STARTED.md) if you're just starting out.

---

## Environment Variables

All secrets and host-specific values are in a `.env` file that lives next to your compose files. See [compose/.env.example](compose/.env.example) for the full list. The `.env` is gitignored — never commit it.

Variables used across compose files:

| Variable | Used by | What it is |
|:---|:---|:---|
| `SERVER_IP` | jellyfin, open-webui, tubearchivist, gluetun, others | Your services VM / Docker host IP |
| `DOMAIN` | vaultwarden, npm, ntfy | Your public domain (e.g. `yourname.duckdns.org`) |
| `LAN_SUBNET` | gluetun | Your LAN CIDR (e.g. `192.168.1.0/24`) — lets VPN containers reach local services |
| `WIREGUARD_PRIVATE_KEY` | gluetun | WireGuard key from your VPN provider |
| `SAMBA_USER` / `SAMBA_PASSWORD` | samba | LAN file share credentials |

---

## Deploying a Service

```bash
# Start a service
docker compose -f /path/to/compose/service.yml up -d

# Stop a service
docker compose -f /path/to/compose/service.yml down

# Restart a service
docker compose -f /path/to/compose/service.yml restart

# Pull a new image and redeploy
docker compose -f /path/to/compose/service.yml pull && docker compose -f /path/to/compose/service.yml up -d

# Follow logs
docker compose -f /path/to/compose/service.yml logs -f
```

---

## After a Hard Reboot

**Don't `docker start` stopped containers.** Hard shutdowns corrupt overlay2 metadata. Always recreate:

```bash
docker container prune -f
docker compose -f compose/service.yml up -d
```

If you see `RWLayer is unexpectedly nil`, you need to recreate, not restart.

If the entire Docker data directory is corrupted (rare — usually from rebooting during heavy I/O):
```bash
# Stop Docker and containerd
systemctl stop docker containerd

# Clear the corrupted state (WARNING: deletes all local image layers)
rm -rf /your/docker-root/containerd /your/docker-root/docker/image /your/docker-root/docker/overlay2

# Start Docker — it will re-pull everything on next compose up
systemctl start docker
```

Re-pulling images takes 10–20 minutes depending on your internet speed. Your app data is safe as long as it's on a volume (not inside the container).

---

## GPU Containers

Containers using NVIDIA GPUs need these environment variables:

```yaml
environment:
  - NVIDIA_VISIBLE_DEVICES=all
  - NVIDIA_DRIVER_CAPABILITIES=all
```

If you use VFIO passthrough (not Docker's `runtime: nvidia`), the GPU is owned by the VM. The above env vars are all that's needed — no special runtime flag.

**NVML version mismatch:** If a GPU container throws driver/library version errors after an update, the container's userspace libs don't match the kernel module. Fix: reboot the VM. Don't update GPU container images without planning a reboot window.

---

## VPN Stack (Gluetun)

All media automation containers share Gluetun's network namespace. This means:

- They all exit through the VPN
- They share a single external IP
- Container-to-container connections use `localhost`, not the host IP

```yaml
# Wrong — routes out through VPN tunnel and back
qBittorrent host in Sonarr: 192.168.1.x:8082

# Right — stays local
qBittorrent host in Sonarr: localhost:8080  # container port, not host-mapped port
```

**Port forwarding:** ProtonVPN (and some other providers) assign a dynamic port on each VPN connect. The `gluetun-port-forward.sh` script handles this automatically — it fires when the VPN connects and updates qBittorrent's listen port via its API. You don't manage this manually.

---

## NFS Mounts

The recommended fstab options for NFS mounts:

```
server:/export/path  /mnt/local  nfs4  rw,sync,hard,_netdev,nofail  0  0
```

| Option | Why |
|:---|:---|
| `nfs4` | Use NFSv4, more reliable than v3 |
| `hard` | Keep retrying if server drops mid-operation |
| `_netdev` | Don't attempt mount until network is up |
| `nofail` | Don't block boot if mount fails |

**Mount order matters.** If you have `/mnt/storage` sub-path mounts (like `/mnt/storage/media`), you must also have a base `/mnt/storage` mount. Without the base, writes to unmapped paths land silently on your root partition.

---

## ZFS Quick Reference

```bash
# Pool status
zpool status
zpool list

# Dataset/zvol usage
zfs list

# ARC stats
arc_summary        # Linux ARC summary script
cat /proc/spl/kstat/zfs/arcstats | grep -E "hits|misses|size"

# Check when last scrub ran
zpool status | grep scan

# Start a scrub
zpool scrub poolname

# Resize a zvol (three steps — don't skip the VM config update)
zfs set volsize=Xg poolname/zvolname
# Then update VM config to match
# Then cold reboot VM, then: resize2fs /dev/vda
```

---

## Automation Scripts

All scripts are in `scripts/`. On the server they live at `/opt/scripts/`.

| Script | Runs | What it does |
|:---|:---|:---|
| `container-watchdog.py` | Always | Watches Docker events, restarts failed containers with backoff, sends alerts |
| `boot-check.py` | Every boot | Posts per-container status table after 90s settle time |
| `preflight-check.sh` | Sunday 3:30 AM | Checks pending kernel/driver packages, schedules maintenance reboot if needed |
| `maintenance-reboot.sh` | Monday 4 AM (if needed) | Installs flagged packages, sends alert, reboots cleanly |
| `staged-startup.sh` | Boot | Brings containers up in groups with delays to avoid I/O bursts |
| `malware-watch.sh` | File events + weekly | 3-layer scan (YARA → ClamAV → MalwareBazaar) on new downloads |
| `gluetun-port-forward.sh` | VPN connect | Updates qBittorrent listen port when VPN assigns a new forwarded port |

### Weekly Maintenance Window

```
[SUNDAY NIGHT]
3:30 AM — preflight: check for pending packages → Discord + schedule reboot if needed
4:00 AM — Watchtower: pull updated container images

[MONDAY MORNING]
4:00 AM — maintenance-reboot (if packages pending): install → alert → reboot
          OR vm-reboot (plain weekly reboot if nothing pending)
~4:01 AM — VM back up, drivers loaded, staged-startup brings containers up
~5:01 AM — boot-check: per-container status posted to notifications
```

---

## Reverse Proxy (Nginx Proxy Manager)

NPM handles SSL termination for all public-facing services. The pattern:

1. DuckDNS (or any dynamic DNS) updates your domain → home IP
2. Router forwards ports 80 and 443 to your server
3. NPM receives all traffic and routes by subdomain
4. Let's Encrypt certificates are auto-renewed

Each subdomain gets a proxy host in NPM pointing to the container's internal port. No SSL config needed in the individual containers.

---

## Backup Strategy

Three layers:

1. **Compose files in git** — the stack is always recoverable from git + `docker compose up`
2. **App data backups (Duplicati)** — daily encrypted backup of all container config/data directories to a separate volume or NAS
3. **Offsite** — copy backups to an offsite location (a second site, cloud cold storage, a trusted friend's NAS)

The 3-2-1 rule: 3 copies, 2 different media, 1 offsite. App configs are small — even a cheap cloud storage tier works for offsite.

---

## Common Failure Modes and Fixes

| Symptom | Likely cause | Fix |
|:---|:---|:---|
| All containers fail with `RWLayer is unexpectedly nil` | Hard reboot during I/O | `docker container prune -f` + `docker compose up -d` for each service |
| VM hangs at boot indefinitely | NFS mount without `nofail` | Boot into recovery, add `nofail` to fstab NFS entries |
| Disk shows 100% but files were just deleted | NFS cache | Run `sync && sleep 5`, recheck |
| AdGuard can't bind port 53 | `systemd-resolved` stub listener | Set `DNSStubListener=no` in `/etc/systemd/resolved.conf`, restart |
| Everything goes slow, high I/O wait, ZFS pool is fine | ZFS ARC starvation | Check ARC stats, reduce RAM allocated elsewhere, or add RAM |
| Container restarts but GPU operations fail | NVML driver/library mismatch | Reboot the VM |
| qBittorrent "file error" on every torrent | Wrong path in API call | Use container-internal paths, not host paths |
| SSH banner timeout connecting to VM | Heavy NFS I/O stall | SSH through Proxmox console instead, wait for I/O to settle |

---

## Services by Category

### Public (need a domain + SSL)
Jellyfin, Vaultwarden, Audiobookshelf, Immich, Calibre-Web, Homebox, Ntfy

### Management (LAN only)
Proxmox, Nginx Proxy Manager, Portainer, Homarr, Uptime Kuma, Dozzle, Netdata, Duplicati, Forgejo, AdGuard Home

### Media Automation (VPN required — share Gluetun network)
qBittorrent, Sonarr, Radarr, Lidarr, Prowlarr, Bazarr, LazyLibrarian, FlareSolverr, Cross-seed

### AI / LLM (LAN only)
Ollama, Open WebUI, Qdrant

### Knowledge (LAN only)
Kiwix, Kolibri, Tileserver

### Security (no UI)
CrowdSec, Trivy, ClamAV, malware-watch pipeline

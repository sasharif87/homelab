# Homelab

Config files, automation scripts, and hard-won knowledge from running a self-hosted home server. Shared so others don't have to figure it all out from scratch.

This repo is the actual configs that run a real server — not a demo or a tutorial project. Take what's useful.

---

## Where This Started → Where It Got To

This began with a single used desktop, Docker, and Jellyfin. No rack. No plan. Just wanting to stop paying Plex and having my library depend on someone else's servers.

Over time that turned into a Proxmox hypervisor, a 32TB ZFS storage pool built from enterprise drives off Facebook Marketplace, 40+ self-hosted services, local AI models running on a used Quadro GPU, an offline knowledge archive, and automation that keeps everything running without babysitting.

None of it was built at once. Each piece got added when there was a real reason for it — not because it seemed cool, but because the previous thing was working and the next problem was obvious.

[GETTING_STARTED.md](GETTING_STARTED.md) maps out that progression honestly, including what things actually cost and what not to buy. If you're at the beginning, start there.

---

## What's Here

| File | What it's for |
| :--- | :--- |
| [GETTING_STARTED.md](GETTING_STARTED.md) | The honest path from zero to a working homelab — hardware, services, what to avoid |
| [LESSONS.md](LESSONS.md) | Things that cost hours and only make sense in hindsight |
| [REFERENCE.md](REFERENCE.md) | Commands, patterns, and troubleshooting for day-to-day operation |
| [compose/](compose/) | Docker Compose files — one per service |
| [scripts/](scripts/) | Automation scripts and systemd units |
| [compose/.env.example](compose/.env.example) | All environment variables the compose files expect |

---

## The Stack

A single Proxmox host running one Ubuntu 24.04 VM with ~40 Docker services. Covers media, AI inference, photo backup, knowledge archive, and automated maintenance.

**Media:** Jellyfin (NVENC transcoding), Audiobookshelf, Calibre, Navidrome, TubeArchivist

**Media Automation:** Sonarr, Radarr, Lidarr, Prowlarr, Bazarr, LazyLibrarian, qBittorrent — all routed through ProtonVPN via Gluetun

**AI / LLM:** Ollama + Open WebUI on a Quadro RTX 5000 (16GB VRAM). Models from 3B to 70B with RAM offload.

**Personal:** Vaultwarden (passwords), Immich (photos), Homebox (inventory), Mealie (recipes)

**Knowledge:** Kiwix (offline Wikipedia, Stack Exchange, iFixit, Gutenberg, medical), Kolibri (offline education), Tileserver (offline maps)

**Security:** CrowdSec IPS, Trivy CVE scanning, ClamAV + YARA malware pipeline

**Infrastructure:** Nginx Proxy Manager, AdGuard Home, Forgejo, Duplicati, Portainer, Homarr, Uptime Kuma, Ntfy

---

## Using These Configs

Copy the compose file for whatever service you want. Set your variables in a `.env` file next to it — see [compose/.env.example](compose/.env.example) for what's needed.

```bash
# Example
cp compose/.env.example compose/.env
# Edit .env with your values
docker compose -f compose/jellyfin.yml up -d
```

The scripts in `scripts/` are meant to run on the server itself. Most are standalone shell scripts or Python — no framework dependencies.

---

## Scripts

All scripts live in `scripts/`. They require no framework — just bash or Python 3. Systemd unit files for the scheduled ones are in `scripts/systemd/`; drop them into `/etc/systemd/system/` and `systemctl enable --now` them.

### Startup sequence

| Script | What it does |
| :--- | :--- |
| `preflight-check.sh` / `.py` | Runs on boot — verifies NVMe mounts, NFS, and ZFS are healthy before Docker is allowed to start |
| `staged-startup.sh` | Starts containers in waves with short delays between groups — prevents I/O spikes and lets dependencies come up in order |
| `vm-reboot.sh` / `maintenance-reboot.sh` | Clean shutdown sequence for the services VM before Proxmox maintenance or scheduled reboots |
| `boot-check.py` | Post-boot health checks — confirms containers are up and services are responding |

### Watchdogs

| Script | What it does |
| :--- | :--- |
| `container-watchdog.py` | Polls containers on a schedule, restarts any that are unhealthy or exited unexpectedly |
| `gpu-health.sh` | Checks GPU utilization and VRAM; sends a notification if the card isn't responding |
| `gluetun-port-forward.sh` | Refreshes the VPN forwarded port on a timer so qBittorrent stays seeding |
| `rootfs-guard.sh` | Monitors root filesystem usage; alerts before it fills up |

### Security

| Script | What it does |
| :--- | :--- |
| `malware-setup.sh` | One-time setup for ClamAV + YARA scanning — installs rules and wires up the pipeline |
| `malware-watch.sh` | Inotify-based watcher that scans new files in download directories as they arrive |
| `malware-weekly.sh` | Weekly full-system ClamAV scan |
| `yara-update.sh` | Pulls updated YARA rules from community sources |
| `rkhunter-weekly.sh` | Weekly rootkit hunter scan |
| `trivy-weekly.sh` | Weekly Trivy CVE scan against running container images |

### Dashboard and service setup (one-time)

| Script | What it does |
| :--- | :--- |
| `homarr-seed-apps.sh` | Seeds all services into Homarr via API — run once after a fresh install |
| `homarr-add-monitoring.sh` | Adds monitoring apps to Homarr; separate from the main seed for incremental adds |
| `duplicati-seed-backups.sh` | Seeds Duplicati backup jobs via API — requires `DUPLICATI_API_KEY` |
| `ntfy-deploy.sh` | Configures ntfy notification topics and access controls |

### Knowledge and AI

| Script | What it does |
| :--- | :--- |
| `kiwix-stage-download.sh` | Downloads Kiwix ZIM files for offline knowledge bases |
| `ingest-rag.py` | Ingests documents into the local vector database (Qdrant) |
| `upload-rag-kiwix.py` | Processes Kiwix ZIM content and uploads extracted text to the RAG pipeline |
| `rag-status.sh` | Shows collection sizes and indexing status for the RAG vector store |

The `scripts/ref/` folder contains earlier versions of some scripts kept for reference — not active, not required.

---

## Hardware

| Component | What's running here |
| :--- | :--- |
| CPU | AMD Ryzen 9 5900XT — 16C/32T |
| RAM | 128GB DDR4 |
| GPU | Quadro RTX 5000 16GB — VFIO passthrough to VM |
| Storage | 32.7TB ZFS raidz2 (enterprise SAS drives in an EMC JBOD) |
| Docker data | 1TB NVMe ZFS zvol |
| OS | Proxmox VE host → Ubuntu 24.04 VM |

Not required to start. See [GETTING_STARTED.md](GETTING_STARTED.md) for what actually matters when you're beginning.

---

## Contributing / Questions

Issues and PRs are welcome. This repo is shared to be useful — if something's wrong, unclear, or could be done better, a contribution is genuinely appreciated.

Documentation is especially welcome. If you ran through this and something was confusing, missing, or out of date, fixing it is one of the most valuable things you can do. A paragraph that explains something someone will spend an hour Googling is worth as much as a code change.

If you want to add a script or service config: keep it env-var based (no hardcoded IPs or domains), and make sure it works standalone without requiring the rest of the stack.

Questions are welcome as issues too — if something isn't clear from the docs, that's a gap worth knowing about.

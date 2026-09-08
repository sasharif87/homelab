# Scripts Reference

These scripts exist because I've rebuilt this stack more than once, and the second time through I broke things I didn't break the first time. Not because the stack got more complicated — because I was doing it from memory and missed steps.

The startup sequence, the watchdogs, the boot checks — none of them existed originally. They got written after something went wrong and I had to figure out why. The malware pipeline came from wanting to know if a compromised container had already done something before I caught it. The dashboard seeders exist because re-clicking 40 services into a fresh Homarr install is exactly the kind of thing that makes you question your choices.

The goal isn't a polished automation framework. It's that when something breaks — and it will — the recovery is a `git clone` and a few commands, not a weekend of archaeology.

Scripts live under `scripts/`, grouped by what they do. They require no framework — just bash or Python 3.

| Directory | Contents |
| :--- | :--- |
| `scripts/boot/` | Startup sequence — preflight checks, staged container startup, reboot handling |
| `scripts/security/` | Malware scanning, rootkit checks, image CVE scanning, rootfs guard |
| `scripts/ai/` | RAG ingestion, Kiwix staging, Ollama and Open WebUI tooling |
| `scripts/media/` | Library maintenance, TubeArchivist, VPN port forwarding |
| `scripts/infra/` | GPU health, notification setup, connectivity tests |
| `scripts/backup/` | Sanoid install and the Duplicati job seeder |
| `scripts/home/` | Homarr and Homebox dashboard seeding |
| `scripts/systemd/` | Unit and timer files for everything scheduled |

Systemd unit files for the scheduled ones are in `scripts/systemd/`. To install one:

```bash
sudo cp scripts/systemd/<unit>.service /etc/systemd/system/
sudo cp scripts/systemd/<unit>.timer /etc/systemd/system/   # if it has one
sudo systemctl daemon-reload
sudo systemctl enable --now <unit>.timer   # or .service if no timer
```

---

## Startup Sequence

These run on boot, in order, to make sure the system comes up cleanly before Docker starts. See [LESSONS.md](LESSONS.md) for why the order matters.

| Script | What it does |
| :--- | :--- |
| `preflight-check.sh` / `.py` | Verifies NVMe mounts, NFS, and ZFS are healthy before Docker is allowed to start |
| `staged-startup.sh` | Starts containers in waves with short delays between groups — prevents I/O spikes and lets dependencies come up in order |
| `boot-check.py` | Post-boot health checks — confirms containers are up and services are responding |
| `vm-reboot.sh` / `maintenance-reboot.sh` | Clean shutdown sequence for the services VM before Proxmox maintenance or scheduled reboots |

The `preflight-check` service uses a systemd `ExecStartPre` condition so Docker's startup waits until it passes. If you skip this on a server where Docker data lives on a separately mounted drive, containers will start against an empty overlay and corrupt their state.

---

## Watchdogs

These run on a schedule and keep things healthy without requiring manual checks.

| Script | What it does |
| :--- | :--- |
| `container-watchdog.py` | Polls containers on a schedule, restarts any that are unhealthy or exited unexpectedly |
| `gpu-health.sh` | Checks GPU utilization and VRAM; sends a notification if the card isn't responding |
| `gluetun-port-forward.sh` | Refreshes the VPN forwarded port on a timer so qBittorrent stays seeding |
| `rootfs-guard.sh` | Monitors root filesystem usage; alerts before it fills up |

`container-watchdog.py` is distinct from Docker's own restart policies — it handles cases where a container is technically running but in a broken state (unhealthy, stuck). Run it via a systemd timer every few minutes.

---

## Security

| Script | Frequency | What it does |
| :--- | :--- | :--- |
| `malware-setup.sh` | Once | Installs ClamAV + YARA, pulls initial rule sets, wires up the pipeline |
| `malware-watch.sh` | Always-on | Inotify watcher — scans new files in download directories as they arrive |
| `malware-weekly.sh` | Weekly | Full-system ClamAV scan |
| `yara-update.sh` | Weekly | Pulls updated YARA rules from community sources |
| `rkhunter-weekly.sh` | Weekly | Rootkit hunter scan |
| `trivy-weekly.sh` | Weekly | Trivy CVE scan against all running container images |

Run `malware-setup.sh` once before enabling the rest. It installs the dependencies and creates the directory structure the other scripts expect. The `systemd/` folder has service and timer units for the weekly scans.

---

## Dashboard and Service Setup (One-Time)

These seed configuration into running services via their APIs. Run them once after a fresh install — they're not meant to run on a schedule.

| Script | Requires | What it does |
| :--- | :--- | :--- |
| `homarr-seed-apps.sh` | `HOMARR_API_KEY`, `SERVER_IP`, `PROXMOX_IP` | Seeds all services into Homarr dashboard |
| `homarr-add-monitoring.sh` | `HOMARR_API_KEY`, `SERVER_IP` | Adds monitoring apps to Homarr — for incremental additions after initial seed |
| `duplicati-seed-backups.sh` | `DUPLICATI_API_KEY` | Creates backup jobs in Duplicati via API |
| `ntfy-deploy.sh` | ntfy running | Configures ntfy notification topics and access controls |

`homarr-seed-apps.sh` uses env vars for all IPs — pass them inline or via `.env`:

```bash
SERVER_IP=192.168.x.x PROXMOX_IP=192.168.x.x HOMARR_API_KEY=<key> bash scripts/home/homarr-seed-apps.sh
```

---

## Knowledge and AI

| Script | What it does |
| :--- | :--- |
| `kiwix-stage-download.sh` | Downloads Kiwix ZIM files for offline knowledge bases (Wikipedia, Stack Exchange, etc.) |
| `ingest-rag.py` | Ingests documents into the local vector database (Qdrant) |
| `upload-rag-kiwix.py` | Processes Kiwix ZIM content and uploads extracted text to the RAG pipeline |
| `rag-status.sh` | Shows collection sizes and indexing status for the RAG vector store |

These require Qdrant and Ollama to be running. `ingest-rag.py` and `upload-rag-kiwix.py` read from the same `.env` as the compose stack — `QDRANT_URL` and `OLLAMA_URL` need to be set.

---

## Notes

- Scripts that send notifications use `ntfy` — the topic and server URL come from env vars (`NTFY_URL`, `NTFY_TOPIC`).
- Nothing here requires root except the systemd unit installs and the malware scanner setup.

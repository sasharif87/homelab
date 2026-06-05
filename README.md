# Homelab

Config files, automation scripts, and hard-won knowledge from running a self-hosted home server. Shared so others don't have to figure it all out from scratch.

This repo is the actual configs that run a real server — not a demo or a tutorial project. Take what's useful.

---

## The Why

This started because I lost access to books and movies I had paid for. Not pirated — purchased. The platform lost the license, the library disappeared, and there was nothing to be done about it. That was the moment it became clear that "buying" something through someone else's platform doesn't mean you own it. It means you're renting access until they decide otherwise.

So I decided to take the control back. When it lives on your hardware, what you have is what you have. No license server to phone home, no account to get locked, no service announcement telling you your library is going away in 30 days. That principle extended naturally to everything else — photos, passwords, documents, AI. If it matters, it should be somewhere you control.

Self-hosting doesn't make you invisible. It lets you control where the surface is. Every service you move inhouse is one fewer breach target, one fewer set of terms that can change, one fewer company that gets acquired with your data inside it.

This stack is built around utility and controlled exposure — not lockdown. Things are accessible because you chose to make them accessible, through an entry point you control, on terms you set. The goal is that you decide what's reachable, how it's reachable, and who can reach it. Not a default config somewhere you forgot about.

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
| [SCRIPTS.md](SCRIPTS.md) | What every script does and how to install the systemd units |
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

The scripts in `scripts/` are meant to run on the server itself. Most are standalone shell scripts or Python — no framework dependencies. They exist because I've rebuilt this stack more than once and the second time I broke things I didn't break the first time — doing it from memory and missing steps. The goal is that the next rebuild is a `git clone` and a few commands, not a weekend of archaeology. See [SCRIPTS.md](SCRIPTS.md) for a breakdown of what each one does and how to wire up the systemd units.

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

# Getting Started with a Home Server

This is the guide I wish existed when I started. Not "here's my impressive rack" — but "here's the actual path, what it costs, and what will bite you."

The goal is to stop depending on cloud services you don't control, for data that matters to you. Photos, media, passwords, documents. You can host all of it yourself, on hardware you own, with no monthly subscription and no terms-of-service changes that make your data inaccessible.

---

## Where to Start

You don't need a rack. You don't need enterprise hardware. You need one machine that stays on.

**Minimum viable homelab:**
- Any desktop or tower with at least 8GB RAM and a spare hard drive
- A router you control (not an ISP-provided combo unit if you can help it)
- A free weekend

That's it. Everything else is optional and can be added later.

---

## The Progression

Most people go through roughly the same phases. Each phase is independently useful — you don't have to commit to all of them.

**Phase 1 — One machine, Docker**
Install Docker on whatever you have. Run Jellyfin or a password manager. Learn how Docker volumes and networking work. This phase teaches you more than any tutorial.

**Phase 2 — Reverse proxy + SSL**
Add Nginx Proxy Manager. Point a free DuckDNS domain at your home IP. Get a real SSL certificate via Let's Encrypt. Now your services have real URLs instead of `http://192.168.1.x:port`.

**Phase 3 — A real server**
Move to dedicated hardware when your current machine struggles or you need it for something else. Used workstations and servers from eBay, Facebook Marketplace, and university surplus programs are the best value by far.

**Phase 4 — ZFS storage**
When you have data you care about, add a real storage pool. ZFS gives you checksumming, snapshots, and transparent compression. An HBA card + a handful of used enterprise drives is cheaper than you think.

**Phase 5 — Virtualization**
Proxmox on bare metal lets you separate your storage host from your services VM. This is worth doing once your server has enough RAM — it makes upgrades, snapshots, and disaster recovery dramatically cleaner.

**Phase 6 — Automation**
Once you have things running, you'll want them to stay running without babysitting. Container watchdogs, automated updates, health checks to your phone. This is where a homelab becomes infrastructure rather than a hobby project.

---

## Hardware

### What Actually Matters

**RAM first.** More RAM than you think you need. ZFS wants RAM for its cache (ARC). Containers want RAM. If you're running AI models, they want RAM. 32GB is a reasonable floor for a machine doing real work. 64GB+ is better.

**Storage second.** Enterprise drives pulled from data centers are the best value for ZFS. They're cheap because businesses replace drives on a schedule, not because they're worn out. Look for Seagate Exos, WD Ultrastar, and Toshiba enterprise series. Check SMART data before buying.

**GPU optional.** Only matters if you're doing hardware video transcoding (Jellyfin) or running local AI models. A used Nvidia Quadro card from the same era as your server is often the best value — designed for sustained workloads, not gaming.

### Where to Buy

- **Facebook Marketplace** — best prices, local pickup, can inspect before buying
- **eBay** — wider selection, factor in shipping for large items
- **University surplus programs** — genuinely excellent finds, often underpriced
- **r/homelabsales** — community with a reputation system

### What Not to Buy

| Avoid | Why |
|:---|:---|
| SMR hard drives | Severely degraded write performance in RAIDZ. Common in consumer NAS drives — check before buying. Confirmed CMR: Seagate Exos, WD Ultrastar, WD Gold, Toshiba N300 |
| Any drive with reallocated sectors | Zero tolerance. One bad sector disqualifies a drive for ZFS |
| LSI HBA cards not in IT mode | ZFS needs direct drive access. IR (RAID) mode breaks it |
| Consumer router/modem combos from ISP | You can't control DNS, port forwarding, or VLANs properly |
| Mining cards resold as AI compute | Heavy wear, driver conflicts, non-poolable VRAM |
| Cloud cameras (Wyze, Ring, Blink, Arlo) | Proprietary streams that can't connect to local NVR software |

---

## First Services to Run

These are the ones with the clearest value and the gentlest learning curve:

**Start here:**
1. **Vaultwarden** — self-hosted Bitwarden-compatible password manager. Your passwords, your server, your control. This is the one most people regret not doing sooner.
2. **Jellyfin** — media server for movies, TV, music. Replace your streaming subscriptions with things you actually own.
3. **Immich** — Google Photos replacement. Automatic backup from your phone, face recognition, shared albums. Runs entirely on your hardware.

**Once those are running:**
4. **Nginx Proxy Manager** — makes everything accessible at real URLs with SSL instead of IP:port
5. **Uptime Kuma** — tells you when something is down before you notice it yourself
6. **Duplicati** — automated encrypted backups of your app configs

**If you want to go deeper:**
7. **Audiobookshelf** — audiobooks and podcasts, self-hosted
8. **Kiwix** — offline Wikipedia, Stack Exchange, and dozens of other knowledge bases. Works when the internet doesn't.
9. **Ollama + Open WebUI** — run large language models on your own hardware

---

## Compose Files in Git: Do This First

Before you run a single service, set up a git repository for your Docker Compose files. This is the single most important operational decision you can make.

When (not if) something goes wrong — a corrupted overlay, a bad update, a failed drive — your entire stack is recoverable from a `git clone` and `docker compose up -d`. Without this, you're rebuilding from memory.

The pattern:
```
your-repo/
  compose/
    jellyfin.yml
    vaultwarden.yml
    immich.yml
    ...
```

Each service gets its own file. Deploy with `docker compose -f compose/jellyfin.yml up -d`. Commit every change.

---

## Security Basics

These aren't optional if you're exposing anything to the internet:

- **Never expose services directly on ports 80/443 without a reverse proxy.** Nginx Proxy Manager or Caddy handles SSL termination and gives you one place to manage access.
- **Vaultwarden signup should be disabled** after creating your accounts (`SIGNUPS_ALLOWED=false`).
- **Don't expose management interfaces** (Portainer, Proxmox, database UIs) to the internet. These are LAN-only.
- **Watchtower auto-updates** keep your containers patched. Exclude containers that require manual review before updates (password managers, anything with a database that needs migration testing).
- **CrowdSec** reads your proxy logs and blocks malicious IPs automatically. It's free and takes about 20 minutes to set up.

---

## The Honest Cost

**Hardware:** $300–600 for a capable used workstation. More if you want ECC RAM or enterprise storage.

**Drives:** $10–15/TB for enterprise SATA/SAS pulls from Facebook Marketplace. A 30TB usable pool costs around $400 in drives.

**Power:** A homelab server draws 50–150W depending on load. At average US electricity rates, a 100W machine costs roughly $7–10/month to run continuously.

**Your time:** The first 3 months will involve more troubleshooting than you expect. After that, a mature homelab mostly runs itself.

**What you stop paying:** Depending on which services you replace — cloud storage, photo backup, password manager subscription, streaming — most people break even within 6–18 months.

---

## A Note on Kubernetes

Browse homelab repos on GitHub and you'll find a lot of Kubernetes. It can look like the "serious" choice. It isn't, for most home setups.

**K8s makes sense when you have:**

- 3+ nodes across multiple physical locations with genuine HA requirements
- A job that uses it and you're deliberately practicing
- Workloads that actually need cluster scheduling

**K8s doesn't make sense when you have:**

- One machine, or multiple machines in the same house on the same circuit — one power event takes everything down regardless of how many nodes you have
- Personal services: media, photos, passwords, knowledge archive
- A 2AM incident you need to debug without a control plane in the way

Even the multi-node case is questionable. Three nodes in the same house isn't real high availability — it's complexity theater. For actual multi-site resilience, Tailscale between two physical locations with replicated data beats a home Kubernetes cluster on every practical metric: simpler, faster to recover, and a power outage at site A doesn't cascade.

A lot of k8s homelab content exists because people needed to learn it for work, or because it signals a certain kind of seriousness. That's a legitimate reason — but own that it's a training environment, not an optimal infrastructure choice for running Jellyfin and Vaultwarden.

Docker Compose is the right tool here. Readable configs, trivial to debug, nothing to install beyond Docker, and fast to recover when something goes wrong. This repo runs 40+ services on it without drama.

---

## Resources That Actually Helped

- **r/homelab** and **r/selfhosted** — the communities where most of this knowledge lives
- **Serve the Home (STH)** — the best source for used server hardware research
- **TechnoTim** on YouTube — practical homelab guides, well-produced
- **Wolfgang's Channel** on YouTube — excellent on networking and security
- **The Proxmox forums** — direct answers for virtualization-specific problems
- **LinuxServer.io** — the Docker images that make running most of these services straightforward

# Lessons Learned the Hard Way

Things that cost hours and only make sense in hindsight. Written after the fact so you don't have to live them first.

---

## Boot & Startup

**Docker starts before your NVMe zvol is mounted.**
ZFS zvols use `_netdev` in fstab, which mounts with `remote-fs.target`. Docker starts with `multi-user.target` and doesn't wait. If the mount is even one second late, Docker sees an empty directory, marks every container's overlay layer as missing, and nothing starts. Fix: a systemd drop-in on the docker service with `RequiresMountsFor=/your/mount/path`. Not fstab ordering — a systemd override.

**Never `docker start` containers after a hard reboot. Always recreate from compose.**
A forced VM shutdown or hard power cycle corrupts Docker overlay2 layer metadata. Stopped containers fail with `RWLayer is unexpectedly nil`. The fix is `docker container prune -f` then `docker compose up -d` to recreate clean. This is why all compose files should live in git — it's the fastest path back.

**Starting 50 containers simultaneously bricks SSH for minutes.**
containerd extracting image layers + databases initializing + GPU driver loading all at once causes a massive I/O spike. SSH times out, watchdog scripts false-alarm, and nothing looks healthy. Solution: staged startup script that brings services up in groups with sleeps between. Worth building before you need it.

**Rebooting mid-I/O corrupts the containerd blob store.**
Kernel upgrades, large model downloads, RAG ingestion — if any of these are running when the VM reboots, containerd can be mid-write. Docker comes back and finds missing blobs. Recovery means wiping the overlay2 and image directories and re-pulling everything. Prevention: have your reboot scripts stop all containers with a grace period before systemd gets there.

**`at` uses UTC, not your local time.**
If you're scheduling maintenance windows with `at`, your server clock is almost certainly UTC. `at 04:00` fires at 4 AM UTC. If you're in CDT, that's 11 PM the night before. Convert before scheduling.

---

## Storage & Mounts

**`nofail` on every NFS fstab entry is non-negotiable.**
Without it, if the NFS server is even slightly slow to respond at boot, the VM hangs indefinitely. Use `nfs4 rw,sync,hard,_netdev,nofail 0 0`. `hard` keeps retrying if the server drops mid-operation. `nofail` keeps boot from blocking if the server isn't ready. You will forget this and learn it the hard way exactly once.

**Add a base NFS mount before any sub-path mounts in fstab.**
Without a base `/mnt/storage` mount entry, any path that doesn't match an explicit sub-mount writes silently to the root partition. This is how a transcode directory filled a 30GB root disk to 100% overnight, took down all healthchecks, and corrupted two SQLite databases before anyone noticed.

**`no_subtree_check` is a server-side NFS export option. Don't put it in client fstab.**
It belongs in `/etc/exports` on the server. Putting it in fstab generates 20+ kernel warning lines per boot and does nothing useful.

**SQLite on NFS is unreliable for high-write apps.**
NFS advisory locking doesn't work well with SQLite WAL mode. Apps that write frequently to SQLite — Jellyfin watch progress, any database-backed service — need their config on local storage, not NFS. High-write app configs belong on a local zvol.

**Full disk during runtime corrupts SQLite WAL files — a restart alone won't fix it.**
When a volume hits 100%, SQLite WAL files get corrupted mid-write. The container keeps running but every request throws a database error. Fix: stop the container, delete the cache database files, restart. The cache rebuilds. Prevention: put transcodes, caches, and large working data on the large volume, not the appdata mount.

**ZFS ARC starvation looks identical to a full disk.**
High I/O wait, load average of 20+, everything unresponsive — it can be ARC starvation, not disk full. Check `zpool list` (pool usage) and `arc_summary` (ARC hit rate) before assuming disk. If ARC is getting evicted aggressively, NFS read amplification tanks everything. The fix is more RAM dedicated to ARC, not more disk.

**`zfs set volsize` is a three-step process.**
1. `zfs set volsize=Xg pool/zvol` on the host
2. Update the VM config to match the new size
3. Cold reboot the VM, then `resize2fs` inside the guest

Running `resize2fs` before the reboot says "nothing to do." That's how you know you skipped step 3. Hot reboot doesn't work — the guest kernel needs to re-read the block device size.

**NFS cache is stale after large deletes.**
After deleting many gigabytes, `df` still shows 100% full for 30+ seconds. Run `sync` and wait a few seconds before checking. Otherwise writes fail even though space exists.

---

## Docker

**Watchtower's default Docker API version is too old for modern daemons.**
Watchtower defaults to API version 1.25. Docker daemon 29+ dropped support for API ≤1.39. Without `DOCKER_API_VERSION=1.45` in your Watchtower environment, it errors every 60 seconds. Set it and forget it.

**GPU containers must be excluded from Watchtower.**
If Watchtower updates a container that uses GPU passthrough (Jellyfin, Ollama), the userspace NVML library version inside the new container won't match the kernel driver outside it. GPU operations fail until you reboot the VM to reload the driver. Always exclude GPU containers from auto-updates.

**Database containers need a longer stop grace period.**
Docker's default SIGTERM → SIGKILL timeout is 15 seconds. Postgres, Elasticsearch, and similar databases may not flush to disk in time, causing corruption. Set `shutdown-timeout: 60` in `daemon.json` and `stop_grace_period: 60s` on individual DB containers.

**Don't pull multiple large models simultaneously.**
Parallel model downloads compete for I/O, can saturate ZFS ARC, stall NFS, and make the whole system unresponsive. Pull one at a time.

**qBittorrent API calls must use container-internal paths.**
qBit runs in Docker where `/mnt/storage/downloads` is mounted as `/downloads`. If scripts call the qBit API with host paths, every torrent errors with "File error alert." Use container-internal paths in all API calls.

---

## Networking

**If your homelab box is your only DNS, a reboot kills your whole home network.**
If your router hands out only your server's IP as DNS and the server goes down, every device on the network loses DNS resolution immediately. Always configure a fallback DNS (1.1.1.1, 8.8.8.8) as a secondary in your DHCP config. Server outages should degrade ad-blocking, not kill all internet access.

**Ubuntu 24.04 has a DNS stub that blocks port 53.**
`systemd-resolved` listens on `127.0.0.53:53` by default. Any container trying to bind `0.0.0.0:53` (AdGuard, Pi-hole) will fail. Set `DNSStubListener=no` in `/etc/systemd/resolved.conf` and restart the service before deploying a DNS container.

**Gluetun VPN containers: always use `localhost`, never the host IP, for inter-container connections.**
Services sharing Gluetun's network namespace are on the same loopback interface. Using the host VM IP for connections between them routes out and back in through the VPN tunnel. Use `localhost:port` for all container-to-container connections inside the Gluetun stack.

---

## ZFS

**CMR drives only. No exceptions.**
SMR drives (certain WD Red, some budget Seagate) have severely degraded RAIDZ write performance. The performance difference isn't marginal — it's unusable for a NAS workload. Confirmed CMR: Seagate Exos series, WD Ultrastar DC series, WD Gold, Toshiba N300.

**IT mode HBA required. IR mode breaks ZFS.**
ZFS must control drives directly. IR (RAID) mode presents volumes, not drives, to the OS. ZFS can't do its own checksumming and error correction through IR mode. Every HBA in the ZFS stack must be flashed to IT mode and verified before use.

**Dedicated ARC RAM is the most impactful ZFS tuning.**
Rule of thumb: 1GB ARC per TB of pool. A 30TB pool wants ~30GB of RAM dedicated to ARC. On a shared VM host, decide how to split RAM between the hypervisor/ARC and the VM before you buy RAM.

**Never exceed 80% pool utilization.**
ZFS write performance degrades significantly above 80% full due to fragmentation. If you're at 75%, start planning expansion now.

---

## Proxmox / Virtualization

**GPU passthrough: use VFIO, not `runtime: nvidia`.**
Passing a GPU to a VM via VFIO gives the VM full, direct hardware access. Using Docker's `runtime: nvidia` on the host is a different and less clean approach when you're already virtualizing. VFIO + `NVIDIA_VISIBLE_DEVICES=all` in container environment variables is the correct pattern.

**After any hard power event, check containers before assuming the stack is healthy.**
Hard resets during high I/O are the single most common source of corruption in this stack — not power failures, not software bugs. The root cause of most corruption incidents here was forcing a reset while the system was under load. Let things finish or use the proper shutdown sequence.

---

## The Lesson That Paid for Everything

**Keep your compose files in git.**

When Docker's overlay2 store got corrupted after a forced reboot, every single container was gone. Because all compose files were in git, the entire stack was back up in about 20 minutes. Without that, it would have been days of reconstructing configurations from memory.

Git your compose files. Git them before you need them.

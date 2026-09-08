#!/usr/bin/env python3
"""
Post-boot container health check — runs once after every VM restart.

Waits 90s for containers to settle, then:
  - Sends "VM is back online" to Discord with a per-container status table
  - Calls out any container that failed to start (watchdog will retry separately)

Runs as a systemd oneshot service (boot-check.service).
"""

import json
import os
import subprocess
import time
from datetime import datetime, timezone

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
NTFY_URL   = os.environ.get("NTFY_URL", "")
NTFY_USER  = os.environ.get("NTFY_USER", "")
NTFY_PASS  = os.environ.get("NTFY_PASS", "")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "homelab")
SETTLE_SECONDS = 90


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def discord(msg: str, color: int) -> None:
    if not DISCORD_WEBHOOK_URL:
        return
    payload = json.dumps({
        "embeds": [{
            "description": msg,
            "color": color,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }]
    })
    try:
        subprocess.run(
            ["curl", "-sS", "-X", "POST", DISCORD_WEBHOOK_URL,
             "-H", "Content-Type: application/json", "-d", payload],
            timeout=10, check=True, capture_output=True,
        )
    except Exception as e:
        log(f"Discord notify failed: {e}")


def ntfy(msg: str, priority: str = "high") -> None:
    if not NTFY_URL:
        return
    try:
        subprocess.run(
            ["curl", "-fsS", "-u", f"{NTFY_USER}:{NTFY_PASS}",
             "-H", f"Priority: {priority}", "-d", msg,
             f"{NTFY_URL}/{NTFY_TOPIC}"],
            timeout=10, check=True, capture_output=True,
        )
    except Exception as e:
        log(f"ntfy notify failed: {e}")


def restart_policy(name: str) -> str:
    try:
        r = subprocess.run(
            ["docker", "inspect", "--format", "{{.HostConfig.RestartPolicy.Name}}", name],
            capture_output=True, text=True, timeout=10,
        )
        return r.stdout.strip()
    except Exception:
        return ""


def main() -> None:
    log(f"Boot check: waiting {SETTLE_SECONDS}s for containers to settle…")
    time.sleep(SETTLE_SECONDS)

    r = subprocess.run(
        ["docker", "ps", "-a", "--format", "{{json .}}"],
        capture_output=True, text=True, timeout=30,
    )

    running: list[str] = []
    failed: list[tuple[str, str]] = []

    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        c = json.loads(line)
        name = c.get("Names", "").lstrip("/")
        state = c.get("State", "")

        policy = restart_policy(name)
        if policy not in ("unless-stopped", "always", "on-failure"):
            continue

        if state == "running":
            running.append(name)
        else:
            failed.append((name, state))

    total = len(running) + len(failed)
    log(f"Boot check complete: {len(running)}/{total} containers running")

    if not failed:
        msg = (
            f":white_check_mark: **VM back online.** "
            f"All {total} containers running."
        )
        color = 0x22CC44
    else:
        failed_lines = "\n".join(f"`{name}` — {state}" for name, state in failed)
        msg = (
            f":warning: **VM back online.** {len(running)}/{total} containers running.\n\n"
            f"**Not running:**\n{failed_lines}\n\n"
            f"Watchdog will retry automatically."
        )
        color = 0xFFA500

    discord(msg, color)
    ntfy(msg, priority="default" if not failed else "high")
    log(msg)


if __name__ == "__main__":
    main()

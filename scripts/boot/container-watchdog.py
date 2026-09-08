#!/usr/bin/env python3
"""
Container watchdog: monitors Docker events and auto-restarts failed containers.

Two complementary mechanisms:
  1. docker events listener — catches die/oom events in real time
  2. Periodic sweep (every 5 min) — catches containers that failed to start
     (OCI runtime errors, NVML mismatches) and never emitted a die event

Retries with exponential backoff: 30s → 60s → 120s, then gives up and pages Discord.
Skips exit code 0 (clean shutdown) and 143 (SIGTERM — intentional Watchtower stop).

Install:
  sudo cp scripts/container-watchdog.py /opt/scripts/container-watchdog.py
  sudo chmod +x /opt/scripts/container-watchdog.py
  sudo cp scripts/container-watchdog.service /etc/systemd/system/
  sudo nano /etc/container-watchdog.env          # add DISCORD_WEBHOOK_URL=https://...
  sudo systemctl daemon-reload
  sudo systemctl enable --now container-watchdog

Discord webhook URL (derive from watchtower.yml shoutrrr token):
  shoutrrr:  discord://TOKEN@CHANNEL_ID
  webhook:   https://discord.com/api/webhooks/CHANNEL_ID/TOKEN
"""

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
NTFY_URL   = os.environ.get("NTFY_URL", "")
NTFY_USER  = os.environ.get("NTFY_USER", "")
NTFY_PASS  = os.environ.get("NTFY_PASS", "")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "homelab")
MAX_RETRIES = 3
RETRY_DELAYS = [30, 60, 120]       # seconds between attempts
PERIODIC_INTERVAL = 300            # sweep every 5 minutes
DOWN_GRACE_PERIOD = 90             # seconds a container must be down before sweep acts
EXHAUSTED_COOLDOWN = 3600          # 1 hour cooldown after all retries fail

_lock = threading.Lock()
_retry_counts: dict[str, int] = {}
_cooldown_until: dict[str, float] = {}
_active_restarts: set[str] = set()
_first_seen_down: dict[str, float] = {}


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def discord(msg: str, color: int = 0xFF4444) -> None:
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


def container_exists(name: str) -> bool:
    """True if a container with this name still exists (any state).
    A planned `docker rm -f` removes it entirely; a real crash leaves it as 'exited'.
    On inspect error we assume it exists, so a genuine failure is never suppressed."""
    try:
        r = subprocess.run(
            ["docker", "inspect", "--type=container", name],
            capture_output=True, timeout=10,
        )
        return r.returncode == 0
    except Exception:
        return True


def run_restart_sequence(name: str, exit_code: int, source: str) -> None:
    """Retry loop for one container — runs in its own thread."""
    with _lock:
        if name in _active_restarts:
            return
        _active_restarts.add(name)
        start_attempt = _retry_counts.get(name, 0)

    try:
        # A planned `docker rm -f <name>` emits a die event (exit 137) a moment before
        # the container is actually removed. Wait for the removal to land; if the
        # container is gone, the takedown was intentional (e.g. an image migration) —
        # abort silently instead of paging about a name that no longer exists.
        time.sleep(3)
        if not container_exists(name):
            log(f"{name}: gone after die event — intentional removal, not paging")
            with _lock:
                _retry_counts.pop(name, None)
                _first_seen_down.pop(name, None)
            return

        for i in range(start_attempt, MAX_RETRIES):
            delay = RETRY_DELAYS[i]
            label = f"exit {exit_code}" if exit_code >= 0 else "failed start"
            log(f"{name}: restart attempt {i + 1}/{MAX_RETRIES} in {delay}s ({label}, src={source})")

            if i == 0:
                discord(
                    f":warning: **{name}** went down ({label}).\n"
                    f"Restarting in {delay}s… (attempt 1/{MAX_RETRIES})",
                    color=0xFFA500,
                )
                ntfy(f"{name} went down ({label}). Restarting in {delay}s…")

            time.sleep(delay)

            r = subprocess.run(
                ["docker", "start", name],
                capture_output=True, text=True, timeout=30,
            )

            if r.returncode == 0:
                log(f"{name}: restart succeeded on attempt {i + 1}")
                discord(
                    f":white_check_mark: **{name}** restarted successfully (attempt {i + 1}/{MAX_RETRIES}).",
                    color=0x22CC44,
                )
                ntfy(f"{name} restarted OK (attempt {i + 1}/{MAX_RETRIES}).", priority="default")
                with _lock:
                    _retry_counts.pop(name, None)
                    _first_seen_down.pop(name, None)
                return

            log(f"{name}: attempt {i + 1} failed — {r.stderr.strip()}")
            # Backstop: if the container was removed between attempts, the takedown was
            # intentional — stop retrying and don't fire the manual-intervention page.
            if not container_exists(name):
                log(f"{name}: removed during retries — intentional, aborting without page")
                with _lock:
                    _retry_counts.pop(name, None)
                    _first_seen_down.pop(name, None)
                return
            with _lock:
                _retry_counts[name] = i + 1

        log(f"{name}: all {MAX_RETRIES} attempts exhausted — manual intervention required")
        discord(
            f":skull: **{name}** failed to restart after {MAX_RETRIES} attempts.\n"
            f"Manual intervention required.",
        )
        ntfy(f"{name} FAILED after {MAX_RETRIES} attempts. Manual intervention required.", priority="urgent")
        with _lock:
            _retry_counts.pop(name, None)
            _cooldown_until[name] = time.time() + EXHAUSTED_COOLDOWN
            _first_seen_down.pop(name, None)
    finally:
        with _lock:
            _active_restarts.discard(name)


def maybe_restart(name: str, exit_code: int, source: str) -> None:
    """Gate checks before spawning a restart thread."""
    with _lock:
        if name in _active_restarts:
            return
        if time.time() < _cooldown_until.get(name, 0):
            log(f"{name}: in cooldown, skipping ({source})")
            return

    policy = restart_policy(name)
    if policy not in ("unless-stopped", "always", "on-failure"):
        log(f"{name}: restart policy='{policy}', skipping ({source})")
        return

    threading.Thread(
        target=run_restart_sequence,
        args=(name, exit_code, source),
        daemon=True,
    ).start()


def handle_die(event: dict) -> None:
    attrs = event.get("Actor", {}).get("Attributes", {})
    name = attrs.get("name", "")
    exit_code = int(attrs.get("exitCode", "0"))

    # 0 = clean shutdown, 143 = SIGTERM (Watchtower update stop) — both intentional
    if exit_code in (0, 143):
        log(f"{name}: clean exit (code {exit_code}), skipping")
        return

    maybe_restart(name, exit_code, "die-event")


def handle_oom(event: dict) -> None:
    attrs = event.get("Actor", {}).get("Attributes", {})
    name = attrs.get("name", "unknown")
    log(f"{name}: OOM kill detected")
    discord(
        f":skull: **{name}** was OOM killed.\n"
        f"Consider adding `mem_limit` to its compose file.",
    )
    ntfy(f"{name} was OOM killed.", priority="urgent")
    # OOM also triggers a die event, so restart is handled there


def periodic_sweep() -> None:
    """Catch containers stuck in 'created'/'exited' that never emitted a die event.
    The 90s grace period prevents fighting with Watchtower mid-update."""
    while True:
        time.sleep(PERIODIC_INTERVAL)
        try:
            r = subprocess.run(
                ["docker", "ps", "-a", "--format", "{{json .}}"],
                capture_output=True, text=True, timeout=30,
            )
            now = time.time()

            for line in r.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                c = json.loads(line)
                name = c.get("Names", "").lstrip("/")
                state = c.get("State", "")

                if state == "running":
                    with _lock:
                        _first_seen_down.pop(name, None)
                    continue

                # Only chase containers that are supposed to self-heal
                policy = restart_policy(name)
                if policy not in ("unless-stopped", "always", "on-failure"):
                    continue

                with _lock:
                    if name in _active_restarts:
                        continue
                    if time.time() < _cooldown_until.get(name, 0):
                        continue
                    if name not in _first_seen_down:
                        _first_seen_down[name] = now
                        continue
                    if now - _first_seen_down[name] < DOWN_GRACE_PERIOD:
                        continue

                log(f"{name}: periodic sweep — down >{DOWN_GRACE_PERIOD}s (state={state}), acting")
                maybe_restart(name, -1, "periodic-sweep")

        except Exception as e:
            log(f"Periodic sweep error: {e}")


def main() -> None:
    if not DISCORD_WEBHOOK_URL:
        log("WARNING: DISCORD_WEBHOOK_URL not set — Discord notifications disabled")

    log("Container watchdog started")
    discord(":satellite: Container watchdog started.", color=0x5865F2)
    ntfy("Container watchdog started.", priority="default")

    threading.Thread(target=periodic_sweep, daemon=True).start()

    proc = subprocess.Popen(
        ["docker", "events", "--format", "{{json .}}",
         "--filter", "type=container",
         "--filter", "event=die",
         "--filter", "event=oom"],
        stdout=subprocess.PIPE,
        text=True,
    )

    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
            action = event.get("Action", "")
            if action == "die":
                handle_die(event)
            elif action == "oom":
                handle_oom(event)
        except json.JSONDecodeError as e:
            log(f"JSON parse error: {e} | line: {line}")
        except Exception as e:
            log(f"Event handler error: {e}")

    log("docker events stream ended — exiting (systemd will restart)")
    sys.exit(1)


if __name__ == "__main__":
    main()

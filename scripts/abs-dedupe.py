#!/usr/bin/env python3
"""Find and remove duplicate audiobooks between AudioBookshelf's curated
library folder and the read-only qBittorrent-backlog folder.

A duplicate is an item in /downloads-books whose normalized (title, author)
matches an item in /audiobooks. The /audiobooks copy is removed from ABS's
database (soft delete — no files touched), since the same book remains
accessible via the /downloads-books mount.

Usage:
    python3 abs-dedupe.py --dry-run
    python3 abs-dedupe.py --apply

Requires ABS_URL and ABS_TOKEN env vars (or edit the defaults below).
"""

import argparse
import os
import re
import sys
import urllib.request
import json

ABS_URL = os.environ.get("ABS_URL", "http://localhost:13378")
ABS_TOKEN = os.environ.get("ABS_TOKEN")

CURATED_PATH = "/audiobooks"
BACKLOG_PATH = "/downloads-books"


def api(path, method="GET"):
    req = urllib.request.Request(
        f"{ABS_URL}{path}",
        method=method,
        headers={"Authorization": f"Bearer {ABS_TOKEN}"},
    )
    with urllib.request.urlopen(req) as resp:
        body = resp.read()
    if method == "DELETE":
        return None
    return json.loads(body) if body.strip() else None


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    if not args.dry_run and not args.apply:
        print("Specify --dry-run or --apply")
        sys.exit(1)

    if not ABS_TOKEN:
        print("Set ABS_TOKEN env var")
        sys.exit(1)

    libs = api("/api/libraries")["libraries"]
    lib = next(l for l in libs if any(f["fullPath"] == BACKLOG_PATH for f in l["folders"]))
    curated_folder = next(f["id"] for f in lib["folders"] if f["fullPath"] == CURATED_PATH)
    backlog_folder = next(f["id"] for f in lib["folders"] if f["fullPath"] == BACKLOG_PATH)

    items = api(f"/api/libraries/{lib['id']}/items?limit=3000")["results"]

    backlog_keys = set()
    for it in items:
        if it["folderId"] == backlog_folder:
            m = it["media"]["metadata"]
            backlog_keys.add((norm(m["title"]), norm(m.get("authorName"))))

    dupes = []
    for it in items:
        if it["folderId"] == curated_folder:
            m = it["media"]["metadata"]
            key = (norm(m["title"]), norm(m.get("authorName")))
            if key in backlog_keys:
                dupes.append(it)

    print(f"Found {len(dupes)} curated-library duplicates (also present in {BACKLOG_PATH}):")
    for it in dupes:
        print(f"  - {it['media']['metadata']['title']} | {it['media']['metadata'].get('authorName')}")
        print(f"      curated: {it['path']}")

    if args.apply:
        for it in dupes:
            api(f"/api/items/{it['id']}", method="DELETE")
        print(f"\nRemoved {len(dupes)} entries from ABS (curated files left on disk untouched).")


if __name__ == "__main__":
    main()

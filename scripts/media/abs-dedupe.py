#!/usr/bin/env python3
"""Find and remove duplicate audiobooks between AudioBookshelf's curated
library folder and the read-only qBittorrent-backlog folder.

A duplicate is an item in /audiobooks (curated) whose normalized (title, author)
matches an item in /downloads-books (backlog). The curated copy is removed from
ABS's database. With --delete-files, the actual files on disk are also removed.

Usage:
    python3 abs-dedupe.py --dry-run
    python3 abs-dedupe.py --apply
    python3 abs-dedupe.py --apply --delete-files

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

PAGE_SIZE = 500


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


def fetch_all_items(lib_id):
    items = []
    page = 0
    while True:
        data = api(f"/api/libraries/{lib_id}/items?limit={PAGE_SIZE}&page={page}")
        batch = data["results"]
        items.extend(batch)
        if len(items) >= data["total"] or not batch:
            break
        page += 1
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--delete-files", action="store_true",
                    help="Also delete curated files on disk (hard delete)")
    args = ap.parse_args()

    if not args.dry_run and not args.apply:
        print("Specify --dry-run or --apply")
        sys.exit(1)

    if not ABS_TOKEN:
        print("Set ABS_TOKEN env var")
        sys.exit(1)

    libs = api("/api/libraries")["libraries"]
    lib = None
    for l in libs:
        folders = {f["fullPath"] for f in l["folders"]}
        if CURATED_PATH in folders and BACKLOG_PATH in folders:
            lib = l
            break

    if not lib:
        print(f"No library found containing both {CURATED_PATH} and {BACKLOG_PATH}")
        print(f"Libraries: {[(l['name'], [f['fullPath'] for f in l['folders']]) for l in libs]}")
        sys.exit(1)

    folder_map = {f["fullPath"]: f["id"] for f in lib["folders"]}
    curated_folder = folder_map[CURATED_PATH]
    backlog_folder = folder_map[BACKLOG_PATH]

    print(f"Library: {lib['name']}")
    print(f"  Curated: {CURATED_PATH} (folder {curated_folder})")
    print(f"  Backlog: {BACKLOG_PATH} (folder {backlog_folder})")

    items = fetch_all_items(lib["id"])
    print(f"  Total items: {len(items)}")
    print()

    backlog_keys = {}
    for it in items:
        if it["folderId"] == backlog_folder:
            m = it["media"]["metadata"]
            key = (norm(m["title"]), norm(m.get("authorName", "")))
            backlog_keys[key] = it

    dupes = []
    for it in items:
        if it["folderId"] == curated_folder:
            m = it["media"]["metadata"]
            key = (norm(m["title"]), norm(m.get("authorName", "")))
            if key in backlog_keys:
                dupes.append((it, backlog_keys[key]))

    if not dupes:
        print("No duplicates found.")
        return

    print(f"Found {len(dupes)} curated items also present in backlog:\n")
    for curated, backlog in dupes:
        cm = curated["media"]["metadata"]
        print(f"  {cm['title']} -- {cm.get('authorName', 'Unknown')}")
        print(f"    curated: {curated['path']}")
        print(f"    backlog: {backlog['path']}")

    if args.apply:
        deleted = 0
        errors = 0
        for curated, _ in dupes:
            try:
                endpoint = f"/api/items/{curated['id']}"
                if args.delete_files:
                    endpoint += "?hard=1"
                api(endpoint, method="DELETE")
                deleted += 1
            except Exception as e:
                cm = curated["media"]["metadata"]
                print(f"  ERROR deleting '{cm['title']}': {e}")
                errors += 1

        mode = "hard-deleted (files removed)" if args.delete_files else "soft-deleted (files kept on disk)"
        print(f"\n{mode}: {deleted}/{len(dupes)} curated entries.")
        if errors:
            print(f"  {errors} errors.")
    else:
        print(f"\nDry run -- re-run with --apply to remove {len(dupes)} curated entries.")
        if not args.delete_files:
            print("  Add --delete-files to also remove curated files from disk.")


if __name__ == "__main__":
    main()

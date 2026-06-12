#!/usr/bin/env python3
"""Delete all TubeArchivist videos of a given type (shorts, streams).

Usage:
    python ta-delete-type.py --type streams
    python ta-delete-type.py --type shorts
    python ta-delete-type.py --type streams --dry-run
"""

import argparse
import os
import sys
import time
import requests

TA_URL = os.environ.get("TA_URL", "http://localhost:8000")


def get_args():
    p = argparse.ArgumentParser()
    p.add_argument("--type", required=True, choices=["shorts", "streams"])
    p.add_argument("--token", help="API token (or set TA_TOKEN env var)")
    p.add_argument("--dry-run", action="store_true", help="List but don't delete")
    return p.parse_args()


def get_token(args):
    import os
    token = args.token or os.environ.get("TA_TOKEN")
    if not token:
        print("Provide --token or set TA_TOKEN env var")
        print("Find it in TubeArchivist → Settings → User → API Token")
        sys.exit(1)
    return token


def fetch_page(session, vid_type, page):
    r = session.get(f"{TA_URL}/api/video/", params={"vid_type": vid_type, "page": page})
    r.raise_for_status()
    return r.json()


def delete_video(session, youtube_id, title, dry_run):
    if dry_run:
        print(f"  [dry-run] would delete: {youtube_id}  {title}")
        return
    r = session.delete(f"{TA_URL}/api/video/{youtube_id}/")
    if r.status_code == 204:
        print(f"  deleted: {youtube_id}  {title}")
    else:
        print(f"  ERROR {r.status_code} deleting {youtube_id}: {r.text}")
    time.sleep(0.3)


def main():
    args = get_args()
    token = get_token(args)

    session = requests.Session()
    session.headers["Authorization"] = f"Token {token}"

    vid_type = args.type
    page = 1
    total_deleted = 0

    print(f"Fetching {vid_type}...")

    while True:
        data = fetch_page(session, vid_type, page)
        videos = data.get("data", [])
        if not videos:
            break

        pagination = data.get("paginate", {})
        print(f"Page {page}/{pagination.get('page_range', '?')} — {len(videos)} videos")

        for v in videos:
            delete_video(session, v["youtube_id"], v.get("title", ""), args.dry_run)
            total_deleted += 1

        if not pagination.get("next_pages"):
            break
        page += 1

    action = "would delete" if args.dry_run else "deleted"
    print(f"\nDone — {action} {total_deleted} {vid_type}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
batch_queue.py — Add prowl-validated books to qBit in weekly batches.
Prioritizes single books over collections/omnibuses so ratio stays healthy.

Week 1 (singles, first 100):
    python3 /root/batch_queue.py --dry-run
    python3 /root/batch_queue.py

Week 2 (singles, next 100):
    python3 /root/batch_queue.py --offset 100

Week N with collections included:
    python3 /root/batch_queue.py --offset N --all

Reads all /root/*_validated.json files produced by prowl_validate.py.
"""

import argparse
import glob
import json
import os
import re
import sys
import requests

QBIT_URL = "http://localhost:8082"
STAGING  = {
    "audiobook": "/downloads/prowl/audiobooks",
    "ebook":     "/downloads/prowl/ebooks",
}
CATEGORY = {
    "audiobook": "prowl-audiobooks",
    "ebook":     "prowl-ebooks",
}

COLLECTION_WORDS = [
    "series", "collection", "omnibus", "anthology", "complete series",
    "books 1", "books 01", "parts 1", "vol 1-", "volumes 1",
    "box set", "boxset", "#1-", "1 to ", "01-",
]

FMT_AUDIO = {"m4b": 10, "mp3": 8, "m4a": 7, "flac": 6, "ogg": 4}
FMT_EBOOK = {"epub": 10, "azw3": 9, "mobi": 7, "pdf": 5}


def is_collection(title: str) -> bool:
    tl = title.lower()
    return any(w in tl for w in COLLECTION_WORDS)


def fmt_rank(title: str, itype: str) -> int:
    tl = title.lower()
    fmap = FMT_AUDIO if itype == "audiobook" else FMT_EBOOK
    for fmt, rank in fmap.items():
        if f"/ {fmt}" in tl or f"[{fmt}]" in tl or f" {fmt}]" in tl:
            return rank
    return 0


def norm(title: str) -> str:
    t = re.sub(r"\[ENG[^\]]*\]", "", title, flags=re.IGNORECASE)
    t = re.sub(r"\[VIP\]", "", t, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", t).strip().lower()


def load_all() -> list:
    items = []
    seen = set()
    for f in sorted(glob.glob("/root/*_validated.json")):
        d = json.load(open(f))
        query = d.get("query", f)
        for bucket in ("confident", "review", "mam_only"):
            for item in d.get(bucket, []):
                r = item.get("result", item)
                url = r.get("download_url", "")
                if not url:
                    continue
                title = r.get("title", "")
                itype = r.get("type", "ebook")
                key = (norm(title), itype)
                if key in seen:
                    continue
                seen.add(key)
                items.append({
                    "title":      title,
                    "type":       itype,
                    "url":        url,
                    "seeds":      r.get("seeders", 0) or 0,
                    "size":       r.get("size_bytes", 0) or 0,
                    "collection": is_collection(title),
                    "fmt":        fmt_rank(title, itype),
                    "query":      query,
                })
    return items


def qlogin() -> requests.Session:
    s = requests.Session()
    s.post(
        f"{QBIT_URL}/api/v2/auth/login",
        data={"username": "admin", "password": os.environ["QBIT_PASS"]},
        headers={"Referer": QBIT_URL},
    )
    return s


def qadd(sess: requests.Session, item: dict) -> str:
    url   = item["url"]
    itype = item["type"]
    title = item["title"]
    seeds = item["seeds"]

    try:
        tr = sess.get(url, timeout=15)
        if tr.status_code != 200 or not tr.content.startswith(b"d"):
            raise ValueError(f"bad response: {tr.status_code} {tr.content[:40]}")
        torrent_bytes = tr.content
    except Exception as e:
        print(f"  [FAIL] {itype[:5]}: {title[:60]} — fetch err: {e}")
        return "fail"

    r = sess.post(
        f"{QBIT_URL}/api/v2/torrents/add",
        data={"savepath": STAGING[itype], "category": CATEGORY[itype]},
        files={"torrents": ("t.torrent", torrent_bytes, "application/x-bittorrent")},
        headers={"Referer": QBIT_URL},
    )
    ok4    = r.status_code == 200 and r.text.lower().startswith("ok")
    ok_dup = r.status_code == 409
    try:
        ok5 = r.status_code == 202 and r.json().get("failure_count", 1) == 0
    except Exception:
        ok5 = False

    if ok_dup:
        print(f"  [DUP]  {itype[:5]}: {title[:60]} ({seeds}s)")
        return "dup"
    elif ok4 or ok5:
        print(f"  [OK]   {itype[:5]}: {title[:60]} ({seeds}s)")
        return "ok"
    else:
        print(f"  [FAIL] {itype[:5]}: {title[:60]} — qBit {r.status_code} {r.text[:40]!r}")
        return "fail"


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--limit",   type=int, default=100, metavar="N",
                    help="Max torrents to add this batch (default 100)")
    ap.add_argument("--offset",  type=int, default=0,   metavar="N",
                    help="Skip first N singles (default 0, use 100 for week 2)")
    ap.add_argument("--all",     action="store_true",
                    help="Include collections/omnibuses after singles are exhausted")
    ap.add_argument("--dry-run", action="store_true",
                    help="Preview what would be added, don't touch qBit")
    args = ap.parse_args()

    all_items   = load_all()
    singles     = sorted(
        [i for i in all_items if not i["collection"]],
        key=lambda i: (i["fmt"], i["seeds"]),
        reverse=True,
    )
    collections = sorted(
        [i for i in all_items if i["collection"]],
        key=lambda i: (i["fmt"], i["seeds"]),
        reverse=True,
    )

    pool  = singles + (collections if args.all else [])
    batch = pool[args.offset: args.offset + args.limit]

    HR = "─" * 70
    print(HR)
    print(f"  batch_queue  limit={args.limit}  offset={args.offset}  all={args.all}")
    print(HR)
    print(f"  Singles    : {len(singles)}")
    print(f"  Collections: {len(collections)} {'(included)' if args.all else '(held back — use --all)'}")
    print(f"  Pool size  : {len(pool)}  |  remaining after offset: {max(0, len(pool)-args.offset)}")
    print(f"  This batch : {len(batch)}")
    print()

    if not batch:
        print("  Nothing to add — offset exceeds available items.")
        sys.exit(0)

    if args.dry_run:
        for i, item in enumerate(batch, 1):
            tag = "[COL]" if item["collection"] else "[SGL]"
            print(f"  {i:3d} {tag} {item['type'][:5]}  {item['seeds']:4d}s  {item['title'][:62]}")
        print(f"\n  (dry-run — nothing added)")
        next_offset = args.offset + args.limit
        print(f"  Next week: python3 /root/batch_queue.py --offset {next_offset}")
        return

    sess = qlogin()
    counts = {"ok": 0, "dup": 0, "fail": 0}
    for item in batch:
        result = qadd(sess, item)
        counts[result] = counts.get(result, 0) + 1

    print()
    print(HR)
    print(f"  Done: {counts['ok']} added  |  {counts['dup']} already in qBit  |  {counts['fail']} failed")
    next_offset = args.offset + args.limit
    remaining   = max(0, len(singles) - next_offset)
    print(f"  Singles remaining after this batch: {remaining}")
    print(f"  Next week: python3 /root/batch_queue.py --offset {next_offset}")
    if remaining == 0 and not args.all:
        print(f"  All singles done — add --all next week to include {len(collections)} collections")
    print(HR)


if __name__ == "__main__":
    main()

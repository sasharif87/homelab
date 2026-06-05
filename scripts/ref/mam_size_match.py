#!/usr/bin/env python3
"""
Size-based matching for paused mam-* torrents.
Matches content files by exact size, disambiguates by name similarity.
Dry-run by default. Pass --apply to make changes.

Usage:
  python3 mam_size_match.py           # dry run - show what would change
  python3 mam_size_match.py --apply   # apply changes (setLocation + rename + recheck)
"""
import json, os, sys, urllib.request, urllib.parse, re

QBIT_URL = "http://localhost:8082"
QBIT_USER = "admin"
QBIT_PASS = "ctzlyLkXPSOX3r"

DRY_RUN = "--apply" not in sys.argv

SCAN_DIRS = [
    ("/mnt/storage/media/audiobooks", "/audiobooks"),
    ("/mnt/storage/media/ebooks", "/ebooks"),
]

CONTENT_EXTS = {
    '.m4b', '.mp3', '.epub', '.pdf', '.mobi', '.azw3',
    '.cbz', '.cbr', '.flac', '.ogg', '.aac', '.opus', '.m4a', '.mp4'
}


def login():
    data = urllib.parse.urlencode({'username': QBIT_USER, 'password': QBIT_PASS}).encode()
    req = urllib.request.Request(f"{QBIT_URL}/api/v2/auth/login", data=data)
    req.add_header('Content-Type', 'application/x-www-form-urlencoded')
    resp = urllib.request.urlopen(req)
    cookies = resp.headers.get_all('Set-Cookie') or [resp.headers.get('Set-Cookie', '')]
    for cookie in cookies:
        for part in cookie.split(';'):
            part = part.strip()
            if '=' in part and 'SID' in part.upper():
                n, v = part.split('=', 1)
                return n, v
    raise RuntimeError("Login failed - no SID cookie found")


def api_get(cn, cv, path, params=None):
    url = f"{QBIT_URL}{path}"
    if params:
        url += '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url)
    req.add_header('Cookie', f'{cn}={cv}')
    resp = urllib.request.urlopen(req)
    return json.loads(resp.read())


def api_post(cn, cv, path, data):
    encoded = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(f"{QBIT_URL}{path}", data=encoded)
    req.add_header('Cookie', f'{cn}={cv}')
    req.add_header('Content-Type', 'application/x-www-form-urlencoded')
    try:
        resp = urllib.request.urlopen(req)
        return resp.read().decode()
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise RuntimeError(f"HTTP {e.code} from {path}: {body}") from e


def build_size_index():
    """Returns: {size_bytes: [(host_path, container_path, filename, container_dir)]}"""
    idx = {}
    for host_dir, container_dir in SCAN_DIRS:
        for root, dirs, files in os.walk(host_dir):
            dirs.sort()
            for fname in sorted(files):
                ext = os.path.splitext(fname)[1].lower()
                if ext not in CONTENT_EXTS:
                    continue
                fpath = os.path.join(root, fname)
                try:
                    sz = os.path.getsize(fpath)
                except OSError:
                    continue
                rel_root = os.path.relpath(root, host_dir).replace('\\', '/')
                if rel_root == '.':
                    cdir = container_dir
                else:
                    cdir = f"{container_dir}/{rel_root}"
                entry = (fpath, f"{cdir}/{fname}", fname, cdir)
                idx.setdefault(sz, []).append(entry)
    return idx


def tokenize(s):
    return set(re.sub(r'[^a-z0-9]', ' ', s.lower()).split())


def best_candidate(candidates, name_hint):
    """Pick best candidate by name similarity. Returns (candidate, score 0-1)."""
    if len(candidates) == 1:
        return candidates[0], 1.0
    hint_tokens = tokenize(name_hint)
    best, best_score = None, -1.0
    for cand in candidates:
        cand_tokens = tokenize(cand[2])
        if hint_tokens and cand_tokens:
            overlap = len(hint_tokens & cand_tokens)
            score = overlap / max(len(hint_tokens), len(cand_tokens))
        else:
            score = 0.0
        if score > best_score:
            best_score = score
            best = cand
    return best, best_score


def main():
    print(f"Mode: {'DRY RUN (pass --apply to make changes)' if DRY_RUN else 'APPLY'}\n")

    cn, cv = login()
    print("Logged in to qBittorrent")

    print("Building file size index...")
    size_idx = build_size_index()
    total_files = sum(len(v) for v in size_idx.values())
    unique_sizes = len(size_idx)
    print(f"Indexed {total_files} content files ({unique_sizes} unique sizes)\n")

    torrents = api_get(cn, cv, '/api/v2/torrents/info', {'filter': 'paused'})
    mam = [t for t in torrents if t.get('category', '').startswith('mam')]
    print(f"Found {len(mam)} paused mam-* torrents\n")
    print("=" * 70)

    matched = []
    unmatched = []

    for t in sorted(mam, key=lambda x: x['name']):
        thash = t['hash']
        tname = t['name']

        tfiles = api_get(cn, cv, '/api/v2/torrents/files', {'hash': thash})
        content_files = [
            f for f in tfiles
            if os.path.splitext(f['name'].split('/')[-1])[1].lower() in CONTENT_EXTS
        ]

        if not content_files:
            print(f"SKIP (no content files): {tname}")
            unmatched.append((tname, "no content files in torrent"))
            continue

        # Match each content file by size
        file_maps = []  # [(tf_name_in_torrent, disk_entry)]
        fail_reason = None

        for tf in content_files:
            tf_fname = tf['name'].split('/')[-1]
            tf_size = tf['size']
            candidates = size_idx.get(tf_size, [])

            if not candidates:
                fail_reason = f"no file on disk with size {tf_size} for '{tf_fname}'"
                break

            cand, score = best_candidate(candidates, tf_fname)
            # Accept any size match (size fingerprint is strong); name score just for logging
            file_maps.append((tf['name'], cand, score))

        if fail_reason:
            print(f"UNMATCHED: {tname}")
            print(f"  Reason: {fail_reason}")
            unmatched.append((tname, fail_reason))
            continue

        # If multiple content files, verify they all land in the same container dir
        dirs_used = set(m[1][3] for m in file_maps)
        if len(dirs_used) > 1:
            # Ambiguous - files spread across different dirs, unusual
            print(f"UNMATCHED: {tname}")
            print(f"  Reason: matched files land in multiple dirs: {dirs_used}")
            unmatched.append((tname, "matched files in multiple dirs"))
            continue

        # Determine if this is a folder-based torrent
        is_folder = any('/' in f['name'] for f in tfiles)

        first_cdir = file_maps[0][1][3]  # container dir containing matched file

        if is_folder:
            # Torrent top-level folder name
            torrent_folder = tfiles[0]['name'].split('/')[0]
            # Actual folder on disk
            actual_folder = first_cdir.rsplit('/', 1)[-1]
            # savepath = parent of the actual folder
            savepath_parts = first_cdir.rsplit('/', 1)
            savepath = savepath_parts[0] if len(savepath_parts) > 1 else '/'
        else:
            torrent_folder = None
            actual_folder = None
            savepath = first_cdir

        # Print what we'll do
        print(f"MATCH: {tname}")
        print(f"  category:  {t.get('category', '')}")
        print(f"  savepath:  {savepath}")
        if is_folder:
            folder_changed = torrent_folder != actual_folder
            print(f"  folder:    {torrent_folder!r} -> {actual_folder!r}" + (" (rename)" if folder_changed else " (same)"))
        for tf_path, disk, score in file_maps:
            tf_fname = tf_path.split('/')[-1]
            fname_changed = tf_fname != disk[2]
            print(f"  file:      {tf_fname!r} -> {disk[2]!r} (size match, name score={score:.2f})" + (" (rename)" if fname_changed else " (same)"))

        if not DRY_RUN:
            # 1. Set save location
            api_post(cn, cv, '/api/v2/torrents/setLocation', {
                'hashes': thash,
                'location': savepath
            })

            # 2. Rename folder if needed (do before file renames)
            if is_folder and torrent_folder != actual_folder:
                try:
                    api_post(cn, cv, '/api/v2/torrents/renameFolder', {
                        'hash': thash,
                        'oldPath': torrent_folder,
                        'newPath': actual_folder
                    })
                except Exception as e:
                    print(f"  WARNING renameFolder: {e}")

            # 3. Rename individual files if needed
            for tf_path, disk, _ in file_maps:
                tf_fname = tf_path.split('/')[-1]
                actual_fname = disk[2]
                if tf_fname != actual_fname:
                    if is_folder:
                        # After folder rename, paths use actual_folder
                        file_subpath = tf_path.split('/', 1)[-1]  # part after original folder
                        old_path = f"{actual_folder}/{file_subpath}"
                        new_path = f"{actual_folder}/{actual_fname}"
                    else:
                        old_path = tf_path
                        new_path = actual_fname
                    try:
                        api_post(cn, cv, '/api/v2/torrents/renameFile', {
                            'hash': thash,
                            'oldPath': old_path,
                            'newPath': new_path
                        })
                    except Exception as e:
                        print(f"  WARNING renameFile '{old_path}': {e}")

            # 4. Trigger recheck
            api_post(cn, cv, '/api/v2/torrents/recheck', {'hashes': thash})
            print(f"  -> applied (setLocation + renames + recheck)")

        matched.append(tname)
        print()

    print("=" * 70)
    print(f"\nSUMMARY")
    print(f"  Matched:   {len(matched)}")
    print(f"  Unmatched: {len(unmatched)}")
    if unmatched:
        print("\nUnmatched torrents:")
        for name, reason in unmatched:
            print(f"  [{reason}] {name}")

    if DRY_RUN and matched:
        print(f"\nRun with --apply to apply {len(matched)} matches.")


if __name__ == '__main__':
    main()

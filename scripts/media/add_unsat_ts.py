#!/usr/bin/env python3
"""
add_unsat_ts.py — Add MAM bulk-downloaded .torrent files to qBittorrent.

Reads .torrent files from a local directory, parses their info_name,
detects audiobook vs ebook, then adds to qBit via a remote script
(the .torrent files are first SCP'd to the VM).

Usage (from local Windows machine):
    # Preview what would be added vs already in qBit
    python3 scripts/media/add_unsat_ts.py --dry-run

    # SCP and add everything (dupes are silently rejected by qBit)
    python3 scripts/media/add_unsat_ts.py
"""

import argparse
import os
import subprocess
import sys

TORRENT_DIR = r"C:\Users\shsh0\Downloads\Unsat_Ts"
VM_HOST     = "root@<server-ip>"
VM_KEY      = r"C:\Users\shsh0\.ssh\id_ed25519"
VM_DEST_DIR = "/root/unsat_ts"

# Audiobook keywords in the info_name / filename
AB_EXTS  = {".m4b", ".mp3", ".m4a", ".flac", ".ogg", ".opus"}
AB_WORDS = [
    "audiobook", "audio book", "unabridged", "narrat",
    # extension patterns: bracketed, parenthetical, or spaced
    " m4b", "[m4b]", "(m4b)",
    " mp3", "[mp3]", "(mp3)",
    " m4a", "[m4a]", "(m4a)",
    " flac", "[flac]", "(flac)",
    # audio quality / bitrate indicators
    "kbps", " 64k", " 128k", " 32k",
    # radio / podcast
    "bbc r4", "bbc r3",
    # misc
    "dolby atmos", "immersion tunnel", "audio immersion",
    "audio collection",
]

STAGING = {
    "audiobook": "/downloads/prowl/audiobooks",
    "ebook":     "/downloads/prowl/ebooks",
}
CATEGORY = {
    "audiobook": "prowl-audiobooks",
    "ebook":     "prowl-ebooks",
}

QBIT_URL  = "http://localhost:8082"
QBIT_USER = "admin"
QBIT_PASS = os.environ["QBIT_PASS"]


# ── Minimal bencode parser (info_name only) ───────────────────────────────────

def _bdecode_str(data: bytes, pos: int):
    colon = data.index(b":", pos)
    length = int(data[pos:colon])
    start  = colon + 1
    return data[start: start + length].decode("utf-8", errors="replace"), start + length


def _bdecode_val(data: bytes, pos: int):
    ch = chr(data[pos])
    if ch == "d":
        d, pos = {}, pos + 1
        while chr(data[pos]) != "e":
            k, pos = _bdecode_str(data, pos)
            v, pos = _bdecode_val(data, pos)
            d[k] = v
        return d, pos + 1
    if ch == "l":
        lst, pos = [], pos + 1
        while chr(data[pos]) != "e":
            v, pos = _bdecode_val(data, pos)
            lst.append(v)
        return lst, pos + 1
    if ch == "i":
        end = data.index(b"e", pos + 1)
        return int(data[pos + 1: end]), end + 1
    if ch.isdigit():
        return _bdecode_str(data, pos)
    raise ValueError(f"unexpected token {ch!r} at pos {pos}")


def torrent_info_name(path: str) -> str:
    try:
        with open(path, "rb") as f:
            data = f.read()
        meta, _ = _bdecode_val(data, 0)
        return meta.get("info", {}).get("name", "")
    except Exception:
        return ""


def detect_type(name: str) -> str:
    nl = name.lower()
    _, ext = os.path.splitext(nl)
    if ext in AB_EXTS:
        return "audiobook"
    if any(w in nl for w in AB_WORDS):
        return "audiobook"
    # "Audio" appearing as a capitalized component (e.g. "HeresyAudio", "AudioVol1")
    if "Audio" in name or nl.endswith("audio"):
        return "audiobook"
    return "ebook"


# ── VM add script (rendered and SCP'd) ───────────────────────────────────────

ADD_SCRIPT = '''\
#!/usr/bin/env python3
"""Add all .torrent files in {src_dir} to qBittorrent."""
import os, requests

QBIT_URL = "{qbit_url}"
STAGING  = {staging}
CATEGORY = {category}

def detect_type(name):
    nl = name.lower()
    ab_words = {ab_words}
    ab_exts  = {ab_exts}
    _, ext = os.path.splitext(nl)
    if ext in ab_exts:
        return "audiobook"
    if any(w in nl for w in ab_words):
        return "audiobook"
    return "ebook"

s = requests.Session()
s.post(f"{{QBIT_URL}}/api/v2/auth/login",
       data={{"username": "{user}", "password": "{pw}"}},
       headers={{"Referer": QBIT_URL}})

src_dir = "{src_dir}"
files   = sorted(f for f in os.listdir(src_dir) if f.endswith(".torrent"))
counts  = {{"ok": 0, "dup": 0, "fail": 0}}

for fname in files:
    fpath = os.path.join(src_dir, fname)
    with open(fpath, "rb") as f:
        tb = f.read()
    itype = detect_type(fname)
    r = s.post(
        f"{{QBIT_URL}}/api/v2/torrents/add",
        data={{"savepath": STAGING[itype], "category": CATEGORY[itype]}},
        files={{"torrents": ("t.torrent", tb, "application/x-bittorrent")}},
        headers={{"Referer": QBIT_URL}},
    )
    ok4  = r.status_code == 200 and r.text.lower().startswith("ok")
    dup  = r.status_code == 409
    try:
        ok5 = r.status_code == 202 and r.json().get("failure_count", 1) == 0
    except Exception:
        ok5 = False
    if dup:
        tag = "DUP "
        counts["dup"] += 1
    elif ok4 or ok5:
        tag = "OK  "
        counts["ok"] += 1
    else:
        tag = "FAIL"
        counts["fail"] += 1
    print(f"[{{tag}}] {{itype[:5]}}  {{fname[:70]}}")

print()
print(f"Done: {{counts['ok']}} added  |  {{counts['dup']}} dupes  |  {{counts['fail']}} failed")
'''


def build_add_script() -> str:
    import json
    return ADD_SCRIPT.format(
        src_dir   = VM_DEST_DIR,
        qbit_url  = QBIT_URL,
        staging   = repr(STAGING),
        category  = repr(CATEGORY),
        ab_words  = repr(AB_WORDS),
        ab_exts   = repr(AB_EXTS),
        user      = QBIT_USER,
        pw        = QBIT_PASS,
    )


def ssh(cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-i", VM_KEY, "-o", "StrictHostKeyChecking=no", VM_HOST, cmd],
        capture_output=True, text=True,
    )


def scp_dir(local_dir: str, remote_dest: str) -> None:
    subprocess.run(
        ["scp", "-i", VM_KEY, "-o", "StrictHostKeyChecking=no",
         "-r", local_dir, f"{VM_HOST}:{remote_dest}"],
        check=True,
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="Parse and classify torrents locally; don't touch the VM")
    ap.add_argument("--dir", default=TORRENT_DIR, metavar="PATH",
                    help=f"Local torrent directory (default: {TORRENT_DIR})")
    args = ap.parse_args()

    files = sorted(f for f in os.listdir(args.dir) if f.endswith(".torrent"))
    if not files:
        print(f"No .torrent files found in {args.dir}")
        sys.exit(1)

    print(f"Found {len(files)} .torrent files in {args.dir}")
    print()

    audio_files = []
    ebook_files = []
    for fname in files:
        fpath = os.path.join(args.dir, fname)
        info_name = torrent_info_name(fpath) or fname
        itype = detect_type(info_name)
        if itype == "audiobook":
            audio_files.append((fname, info_name))
        else:
            ebook_files.append((fname, info_name))

    print(f"  Audiobooks : {len(audio_files)}")
    print(f"  Ebooks     : {len(ebook_files)}")
    print()

    def safe(s: str) -> str:
        return s.encode("ascii", errors="replace").decode("ascii")

    if args.dry_run:
        print("-- AUDIOBOOKS ----------------------------------------------------------")
        for fname, name in audio_files:
            print(f"  {safe(fname[:70])}")
            if name != fname:
                print(f"    -> {safe(name[:70])}")
        print()
        print("-- EBOOKS --------------------------------------------------------------")
        for fname, name in ebook_files:
            print(f"  {safe(fname[:70])}")
            if name != fname:
                print(f"    -> {safe(name[:70])}")
        print()
        print("(dry-run -- nothing sent to VM)")
        return

    import tarfile, tempfile

    # Build and write the add script
    script_content = build_add_script()
    script_local = os.path.join(tempfile.gettempdir(), "_vm_add_unsat.py")
    with open(script_local, "w") as f:
        f.write(script_content)

    # Bundle everything into a single tar — avoids scp bracket-glob issues
    tar_local = os.path.join(tempfile.gettempdir(), "unsat_ts.tar")
    print(f"Bundling {len(files)} torrent files into tar ...")
    with tarfile.open(tar_local, "w") as tf:
        for fname in files:
            tf.add(os.path.join(args.dir, fname), arcname=fname)
    tar_mb = os.path.getsize(tar_local) / 1_048_576
    print(f"  tar size: {tar_mb:.1f} MB")

    print(f"Uploading to VM ...")
    subprocess.run(
        ["scp", "-i", VM_KEY, "-o", "StrictHostKeyChecking=no", "-q",
         tar_local, f"{VM_HOST}:/root/unsat_ts.tar"],
        check=True,
    )
    subprocess.run(
        ["scp", "-i", VM_KEY, "-o", "StrictHostKeyChecking=no", "-q",
         script_local, f"{VM_HOST}:/root/_vm_add_unsat.py"],
        check=True,
    )

    r = ssh(f"mkdir -p {VM_DEST_DIR} && cd {VM_DEST_DIR} && tar xf /root/unsat_ts.tar && echo 'Extracted OK'")
    if "Extracted OK" not in r.stdout:
        print(f"ERROR extracting tar: {r.stderr}")
        sys.exit(1)
    print("  extracted on VM")

    print()
    print("Running add script on VM ...")
    print()
    r = ssh("python3 /root/_vm_add_unsat.py")
    print(r.stdout)
    if r.stderr:
        print("STDERR:", r.stderr[:500])


if __name__ == "__main__":
    main()

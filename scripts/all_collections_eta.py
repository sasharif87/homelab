#!/usr/bin/env python3
"""Show every collection's ZIMs, article_count, cap, effective articles, done
status, and estimated embed time. Read-only. Highlights collections NOT in the
current run (e.g. Practical & Reference)."""
import importlib.util, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("ir", "/opt/scripts/ingest-rag.py")
m = importlib.util.module_from_spec(spec); sys.modules["ir"] = m; spec.loader.exec_module(m)
from libzim.reader import Archive

state = m.load_state()
done = set()
for n, c in state["collections"].items():
    done |= set(c.get("zims_done", []))

RATE = 96.0            # measured chunks/s
CPA  = 3.5             # chunks per article (approx)
APH  = RATE / CPA * 3600   # articles per hour

zdir = Path("/mnt/storage/knowledge/kiwix")
allz = sorted(zdir.glob("*.zim"))

def cap_for(stem, col):
    for pat, c in col.get("caps", {}).items():
        if pat in stem:
            return c
    return 0

assigned = {col["name"]: [] for col in m.COLLECTIONS}
unmatched = []
for z in allz:
    placed = False
    for col in m.COLLECTIONS:
        if any(pat in z.stem for pat in col["match"]):
            assigned[col["name"]].append((z, col)); placed = True; break
    if not placed:
        unmatched.append(z)

for col in m.COLLECTIONS:
    name = col["name"]
    rows = assigned[name]
    print("=" * 78)
    print(name)
    tot = 0
    for z, c in rows:
        ac = Archive(str(z)).article_count
        cap = cap_for(z.stem, c)
        eff = min(ac, cap) if cap else ac
        d = z.stem in done
        if not d:
            tot += eff
        print(f"  {z.stem[:44]:44s} art={ac:>8d} cap={str(cap or '-'):>7s} eff={eff:>7d} {'DONE' if d else ''}")
    print(f"  --> remaining {tot:,} articles  ~{tot/APH:.1f} h embed")

if unmatched:
    print("=" * 78)
    print("UNMATCHED (no collection):")
    for z in unmatched:
        print(f"  {z.stem[:44]:44s} art={Archive(str(z)).article_count:>8d}")

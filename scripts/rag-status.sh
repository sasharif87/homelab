#!/bin/bash
echo "=== RAG Status ==="

if pgrep -af "[/]opt/scripts/ingest-rag.py" | grep -v "phase-b" > /dev/null 2>&1; then
    pid=$(pgrep -af "[/]opt/scripts/ingest-rag.py" | grep -v "phase-b" | awk '{print $1}' | head -1)
    echo "script: running (pid $pid)"
    if pgrep -af "phase-b-subprocess" > /dev/null 2>&1; then
        pb_pid=$(pgrep -af "phase-b-subprocess" | awk '{print $1}' | head -1)
        pb_rss=$(ps -o rss= -p "$pb_pid" 2>/dev/null | awk '{print int($1/1024)"MB"}')
        echo "  phase-b: running (pid $pb_pid  rss=$pb_rss)"
    fi
else
    echo "script: NOT RUNNING"
fi

python3 - << 'PYEOF'
import json, sys

try:
    s = json.load(open('/opt/scripts/ingest-rag-state.json'))
except FileNotFoundError:
    print("state: no ingest-rag-state.json found")
    sys.exit(0)

for col in s.get('collections', {}).values():
    name  = col.get('name', '?')
    done  = col.get('zims_done', [])
    total = col.get('total_chunks', 0)
    print(f"  {name}: {len(done)} ZIMs done  {total:,} chunks")
PYEOF

python3 - << 'PYEOF'
import urllib.request, json, sys

try:
    with urllib.request.urlopen("http://<server-ip>:6333/collections", timeout=5) as r:
        cols = json.load(r)["result"]["collections"]
    for c in cols:
        cid = c["name"]
        with urllib.request.urlopen(f"http://<server-ip>:6333/collections/{cid}", timeout=5) as r:
            info = json.load(r)["result"]
        status = info.get("status", "?")
        pts    = info.get("points_count", 0)
        idx    = info.get("indexed_vectors_count", 0)
        print(f"  qdrant {cid[:8]}…: {status}  {pts:,} pts  {idx:,} indexed")
except Exception as e:
    print(f"  qdrant: error — {e}")
PYEOF

curl -s -o /dev/null -w "OW health: %{http_code} %{time_total}s\n" http://<server-ip>:3000/health

echo "last phase completions:"
grep -h "Phase B complete\|ZIM marked complete\|phase complete" /opt/scripts/ingest-rag.log 2>/dev/null | tail -5

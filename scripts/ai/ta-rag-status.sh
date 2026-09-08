#!/bin/bash
# Status for the TubeArchivist → Qdrant → Open WebUI ingester.
# Sibling of rag-status.sh.
echo "=== TubeArchivist RAG Status ==="

if pgrep -af "[i]ngest-tubearchivist.py" > /dev/null 2>&1; then
    pid=$(pgrep -af "[i]ngest-tubearchivist.py" | awk '{print $1}' | head -1)
    rss=$(ps -o rss= -p "$pid" 2>/dev/null | awk '{print int($1/1024)"MB"}')
    echo "script: running (pid $pid  rss=$rss)"
else
    echo "script: NOT RUNNING"
fi

STATE=/opt/scripts/ingest-tubearchivist-state.json
python3 - "$STATE" << 'PYEOF'
import json, sys
try:
    s = json.load(open(sys.argv[1]))
except FileNotFoundError:
    print("state: no ingest-tubearchivist-state.json found")
    sys.exit(0)
for col in s.get('collections', {}).values():
    name  = col.get('name', '?')
    done  = col.get('videos_done', [])
    total = col.get('total_chunks', 0)
    print(f"  {name}: {len(done):,} videos done  {total:,} chunks")
PYEOF

# Live Qdrant collection counts
python3 - << 'PYEOF'
import urllib.request, json
try:
    with urllib.request.urlopen("http://<server-ip>:6333/collections", timeout=5) as r:
        cols = json.load(r)["result"]["collections"]
    for c in cols:
        cid = c["name"]
        with urllib.request.urlopen(f"http://<server-ip>:6333/collections/{cid}", timeout=5) as r:
            info = json.load(r)["result"]
        print(f"  qdrant {cid[:8]}…: {info.get('status','?')}  "
              f"{info.get('points_count',0):,} pts  "
              f"{info.get('indexed_vectors_count',0):,} indexed")
except Exception as e:
    print(f"  qdrant: error — {e}")
PYEOF

curl -s -o /dev/null -w "OW health:  %{http_code} %{time_total}s\n" http://<server-ip>:3000/health
curl -s -o /dev/null -w "TA  health: %{http_code} %{time_total}s\n" http://<server-ip>:8000/api/

echo "last completions:"
grep -h "COLLECTION:\|Run complete\|TubeArchivist RAG" /opt/scripts/ingest-tubearchivist.log 2>/dev/null | tail -5

#!/usr/bin/env bash
# ============================================================================
# presubmit.sh — put the deployment in the state a leaderboard run should find.
#
# Every one of these was, at some point, the reason a run scored badly:
#   - the room had accumulated 77 000 messages from earlier runs, so every feed
#     read was 10.9 MB from the first request (rank 10);
#   - the balancer had been OOM-killed and nothing restarted it (rank 99);
#   - the balancer's own 160 MB access log sat in the cgroup's page cache before
#     the run even started (sys1 peaked at 501 MB of 512).
# Run this, read the summary, then submit.
# ============================================================================
set -uo pipefail
cd "$(dirname "$0")/.."
LB="${LB_URL:-http://10.1.75.53:3269}"
fail=0

echo "== 1. fresh room (nothing is deleted; the routes move to an empty room) =="
PUBLIC_ROOM="room-$(date +%H%M%S)" bash scripts/deploy.sh backends 2>&1 | grep -E "routable|healthy" | sed 's/^/   /'

echo "== 2. archive + truncate the balancer access log (page cache on sys1) =="
ssh -o BatchMode=yes lbsys1 'cd ~/assignment6/logs && if [ -s lb_access.csv ]; then
    n=$(wc -l < lb_access.csv); gzip -c lb_access.csv > "lb_access.$(date -u +%Y%m%dT%H%M%SZ).csv.gz" 2>/dev/null
    : > lb_access.csv; echo "   archived $n lines, truncated"; fi;
  ls -1 lb_access.*.csv.gz 2>/dev/null | head -n -6 | xargs -r rm -f'

echo "== 3. state =="
S=$(curl -sS -m 20 "$LB/lb/stats") || { echo "   balancer unreachable"; exit 1; }
python3 - "$S" <<'PY' || fail=1
import json,sys
d=json.loads(sys.argv[1]); ok=True
act=d['active_backends']; ids=[b['id'] for b in d['backends'] if b['healthy']]
print(f"   backends routable : {act} {ids}");                      ok &= act==3
c=d['feed_cache_detail']; cfg_budget=c['budget_bytes']
print(f"   feed cache budget : {cfg_budget//1048576} MB, over-budget so far {c['proxied_over_budget']}")
a=d['admission']['request']; print(f"   admission         : {a['capacity']} slots, queued now {a['queued']}")
sys.exit(0 if ok else 1)
PY
printf '   supervisor        : '; ssh -o BatchMode=yes lbsys1 'pgrep -f supervise.sh >/dev/null && echo running || { echo NOT RUNNING; exit 1; }' || fail=1
printf '   feed now          : '; curl -sS -m 30 -H 'Accept-Encoding: gzip' -o /dev/null -w '%{size_download} B gzipped (HTTP %{http_code})\n' "$LB/feed" || fail=1
printf '   POST /message     : '; curl -sS -m 20 -o /dev/null -w 'HTTP %{http_code} in %{time_total}s\n' -X POST "$LB/message" -H 'Content-Type: application/json' -d '{"client-name":"presubmit","msg":"ready"}' || fail=1
for h in lbsys1 lbsys2 lbsys3 lbsys4; do
  printf '   %-7s memory    : ' "$h"; ssh -o BatchMode=yes "$h" 'echo "$(( $(cat /sys/fs/cgroup/memory.current)/1048576 )) MB of 512  ($(grep -o "oom_kill [0-9]*" /sys/fs/cgroup/memory.events))"'
done
echo
if [ "$fail" = 0 ]; then echo "READY — submit http://10.1.75.53:3269 now."; else echo "NOT READY — fix the lines above first."; exit 1; fi

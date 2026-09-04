#!/usr/bin/env bash
# ============================================================================
# demo.sh — scripted live demonstration for the evaluation (~6 minutes).
# Shows, on the allotted systems: dynamic backend addition, dynamic selection,
# persistence, duplicate prevention, backend failure and recovery.
#   bash scripts/demo.sh            (press ENTER between steps)
# ============================================================================
set -u
cd "$(dirname "$0")/.."
LB=http://10.1.75.53:3269
pause() { echo; read -p "--- ENTER for next step ---"; echo; }
pool() { curl -sS -m 5 $LB/lb/stats | python3 -c "import json,sys; d=json.load(sys.stdin); print('   active =', d['active_backends'], '|', ', '.join(f\"{b['id']}:{b['state']} ewma={b['ewma_ms']}ms cpu={(b['load'] or {}).get('cpu_pct')}%\" for b in d['backends']))"; }
whoami_burst() { for i in $(seq 1 ${1:-24}); do curl -sS -m 3 $LB/whoami | python3 -c "import json,sys;print(json.load(sys.stdin)['backend'],end=' ')" || echo -n "ERR "; done; echo; }

echo "STEP 1 — whole-system health (LB, DB, backends, previous assignments)"
bash scripts/sanity_check.sh
pause

echo "STEP 2 — start from ONE backend: stop sys3 and sys4 (graceful deregister)"
bash scripts/scale.sh sys3 down; bash scripts/scale.sh sys4 down; sleep 8; pool
echo "   24 requests — all served by sys2:"; whoami_burst
pause

echo "STEP 3 — DYNAMIC ADDITION: start sys3 while a load burst is running; watch the LB pick it up"
python3 loadgen/loadgen.py --url $LB --concurrency 40 --duration 45 --warmup 0 --poll-stats --run-id demo_scale --out-dir results/demo > logs/demo_scale.log 2>&1 &
sleep 15; echo "   t=15s: bash scripts/scale.sh sys3 up"; bash scripts/scale.sh sys3 up; sleep 6; pool
sleep 8;  echo "   t=29s: bash scripts/scale.sh sys4 up"; bash scripts/scale.sh sys4 up; sleep 6; pool
wait
python3 - <<'EOF'
import json
d = json.load(open('results/demo/demo_scale.json'))
for t in d['timeseries_1s'][::5]:
    print(f"   t={t['t']:>3}s  active={t['active_backends']}  req/s={t['ok']:>4}  p50={t['p50']:>7}ms  share={t['by_backend']}")
EOF
echo "   (dashboard: $LB/lb/ shows the 'added' events)"
pause

echo "STEP 4 — DYNAMIC SELECTION: CPU-hog sys3, traffic moves away from it (adaptive), not under round_robin"
bash scripts/cpu_hog.sh sys3 start; sleep 8; pool
echo "   adaptive:";    curl -sS -X POST $LB/lb/config -H 'Content-Type: application/json' -d '{"algorithm":"adaptive"}' >/dev/null; whoami_burst 40 | tr ' ' '\n' | sort | uniq -c | tr '\n' ' '; echo
echo "   round_robin:"; curl -sS -X POST $LB/lb/config -H 'Content-Type: application/json' -d '{"algorithm":"round_robin"}' >/dev/null; whoami_burst 40 | tr ' ' '\n' | sort | uniq -c | tr '\n' ' '; echo
curl -sS -X POST $LB/lb/config -H 'Content-Type: application/json' -d '{"algorithm":"adaptive"}' >/dev/null
bash scripts/cpu_hog.sh sys3 stop
pause

echo "STEP 5 — DUPLICATE PREVENTION: same message id sent 20× sequentially, 20× concurrently across backends"
python3 scripts/dedup_test.py --url $LB --n 20 --out results/demo/dedup_demo.json | tail -9
pause

echo "STEP 6 — PERSISTENCE: restart the DB service and every backend; the data is still there"
BEFORE=$(ssh lbsys1 "curl -sS -m 3 http://127.0.0.1:5270/stats" | python3 -c "import json,sys;print(json.load(sys.stdin)['messages'])")
bash scripts/deploy.sh db | tail -1
for s in sys2 sys3 sys4; do bash scripts/scale.sh $s down > /dev/null; done; sleep 3
for s in sys2 sys3 sys4; do bash scripts/scale.sh $s up | tail -1; done; sleep 6
AFTER=$(ssh lbsys1 "curl -sS -m 3 http://127.0.0.1:5270/stats" | python3 -c "import json,sys;print(json.load(sys.stdin)['messages'])")
echo "   messages before = $BEFORE, after full restart = $AFTER  (SQLite file on sys1: ~/assignment6/data/chat.sqlite)"
pool
pause

echo "STEP 7 — FAILURE + RECOVERY: SIGKILL sys3 mid-traffic, the LB ejects it; restart, the LB re-admits it"
( whoami_burst 40 ) & sleep 1
bash scripts/scale.sh sys3 kill; wait; sleep 4; pool
echo "   restarting sys3 …"; bash scripts/scale.sh sys3 up > /dev/null; sleep 8; pool
curl -sS $LB/lb/events | python3 -c "import json,sys,time; ev=json.load(sys.stdin)['events'][-8:]; [print('  ', time.strftime('%H:%M:%S', time.localtime(e['t'])), e['kind'], e['backend'], e['detail']) for e in ev]"
echo; echo "demo complete."

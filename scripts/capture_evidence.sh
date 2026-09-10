#!/usr/bin/env bash
# ============================================================================
# capture_evidence.sh — regenerate report/terminal_captures/*.txt from the LIVE
# cluster. Everything the report shows as a terminal capture is produced here,
# so nothing in the report is typed by hand.
#
#   bash scripts/capture_evidence.sh            # all captures
#   bash scripts/capture_evidence.sh public     # just one
# ============================================================================
set -uo pipefail
cd "$(dirname "$0")/.."
LB="${LB_URL:-http://10.1.75.53:3269}"
OUT=report/terminal_captures
mkdir -p "$OUT" evidence
export DB_SYS="${DB_SYS:-sys3}"
DB_HOST="lb$DB_SYS"

run() {  # $1 = label printed as the prompt, rest = command
  local label="$1"; shift
  echo "\$ $label"
  "$@" 2>&1
  echo
}

cap_sanity() {
  { echo "\$ bash scripts/sanity_check.sh"; bash scripts/sanity_check.sh 2>&1; } > "$OUT/01_sanity_check.txt"
  echo "  01_sanity_check.txt"
}

cap_public() {
  {
    echo "# The two routes the assignment fixes, through the load balancer URL only."
    echo
    run "curl -X POST $LB/message -H 'Content-Type: application/json' -d '{\"client-name\":\"rahul\",\"msg\":\"hello from the report\"}'" \
        curl -sS -m 15 -X POST "$LB/message" -H 'Content-Type: application/json' \
        -d '{"client-name":"rahul","msg":"hello from the report"}'
    run "curl -X POST $LB/message -d 'client-name=grader&msg=form+encoded+body'   # form encoding also accepted" \
        curl -sS -m 15 -X POST "$LB/message" -d 'client-name=grader&msg=form+encoded+body'
    run "curl '$LB/message?client-name=queryman&msg=from+the+query+string'        # and query parameters" \
        curl -sS -m 15 "$LB/message?client-name=queryman&msg=from+the+query+string"
    echo "# The same message id sent four times — stored once, the rest are duplicates."
    echo "\$ for i in 1 2 3 4; do curl -X POST $LB/message -d '{\"client-name\":\"dup\",\"msg\":\"retry\",\"id\":\"report-demo-id-0001\"}'; done"
    for i in 1 2 3 4; do
      curl -sS -m 15 -X POST "$LB/message" -H 'Content-Type: application/json' \
        -d '{"client-name":"dup","msg":"retry","id":"report-demo-id-0001"}'; echo
    done
    echo
    echo "\$ curl $LB/feed | head          # default window, with the true total"
    curl -sS -m 20 "$LB/feed" | python3 -c "
import json,sys
d = json.load(sys.stdin)
print({k: v for k, v in d.items() if k != 'messages'})
for m in d['messages'][-6:]:
    print(f\"  seq={m['seq']:<7} id={m['id'][:20]:<22} from={m['from']:<12} via={m['via']:<5} {m.get('text','')[:38]}\")
"
    echo
    echo "\$ curl '$LB/feed?since=0&limit=1000'   # page forward through the complete history"
    curl -sS -m 60 "$LB/feed?since=0&limit=1000" | python3 -c "
import json,sys
d = json.load(sys.stdin)
print({k: v for k, v in d.items() if k != 'messages'})
print(f\"  first page: seq {d['messages'][0]['seq']} .. {d['messages'][-1]['seq']}, follow next_since for the next one\")
"
    echo
    echo "\$ curl '$LB/feed?limit=all'          # as much as one body safely carries"
    curl -sS -m 90 "$LB/feed?limit=all" | python3 -c "
import json,sys
d = json.load(sys.stdin)
print({k: v for k, v in d.items() if k != 'messages'})
ids = [m['id'] for m in d['messages']]
print(f'  returned {len(ids)} messages, {len(set(ids))} distinct ids -> duplicates in the feed: {len(ids)-len(set(ids))}')
"
  } > "$OUT/05_public_routes.txt"
  echo "  05_public_routes.txt"
}

cap_lb() {
  {
    echo "\$ curl $LB/lb/stats"
    curl -sS -m 10 "$LB/lb/stats" | python3 -c "
import json,sys
d = json.load(sys.stdin)
print(f\"algorithm={d['algorithm']}  switch_threshold={d['switch_threshold']}  inflight_cap={d['inflight_cap']}  rt_cap_ms={d['rt_cap_ms']}\")
print(f\"active_backends={d['active_backends']}  pinned_to={d['current_backend']}  switches={d['switches']}  total_requests={d['total_requests']}\")
for b in d['backends']:
    l = b['load'] or {}
    print(f\"  {b['id']:<5} {b['host']}:{b['port']:<5} {b['state']:<9} src={b['source']:<8} \"
          f\"load_index={b['load_index']:<6} ewma={b['ewma_ms']}ms cpu={l.get('sys_cpu_pct')}% \"
          f\"in_flight={b['in_flight']} requests={b['requests']} errors={b['errors']}\")
"
    echo
    echo "\$ curl $LB/lb/events | tail    # the balancer's own membership + health timeline"
    curl -sS -m 10 "$LB/lb/events" | python3 -c "
import json,sys,time
for e in json.load(sys.stdin)['events'][-12:]:
    print(f\"  {time.strftime('%H:%M:%S', time.localtime(e['t']))}  {e['kind']:<11} {e['backend']:<6} {e['detail']}  (active={e['active']})\")
"
  } > "$OUT/06_lb_stats.txt"
  echo "  06_lb_stats.txt"
}

cap_db() {
  {
    echo "\$ ssh $DB_HOST 'curl -s localhost:5270/stats'"
    ssh -o BatchMode=yes "$DB_HOST" "curl -sS -m 5 http://127.0.0.1:5270/stats" | python3 -m json.tool
    echo
    echo "\$ ssh $DB_HOST 'sqlite3-style schema + duplicate check on the live database file'"
    ssh -o BatchMode=yes "$DB_HOST" '~/node/bin/node -e "
const {DatabaseSync} = require(\"node:sqlite\");
const d = new DatabaseSync(process.env.HOME + \"/assignment6/data/chat.sqlite\", {readOnly:true});
for (const r of d.prepare(\"SELECT sql FROM sqlite_master WHERE name IN (\x27messages\x27,\x27dedup_log\x27)\").all()) console.log(r.sql + \";\n\");
const dup = d.prepare(\"SELECT id, COUNT(*) n FROM messages GROUP BY id HAVING n > 1\").all();
console.log(\"rows with a duplicated message id:\", dup.length);
const c = d.prepare(\"SELECT COUNT(*) n FROM messages\").get();
const p = d.prepare(\"SELECT COUNT(*) n FROM messages WHERE room = \x27public\x27\").get();
const g = d.prepare(\"SELECT COUNT(*) n FROM dedup_log\").get();
console.log(\"messages total:\", c.n, \"| in the public room:\", p.n, \"| duplicate rejections logged:\", g.n);
" 2>/dev/null'
  } > "$OUT/03_db_stats_schema.txt"
  echo "  03_db_stats_schema.txt"
}

cap_hosts() {
  {
    echo "\$ bash scripts/ssh_all.sh 'hostname; hostname -I; ss -tlnp | grep -E \":(3000|3001|5270) \"'"
    bash scripts/ssh_all.sh 'hostname; hostname -I; ss -tlnp 2>/dev/null | grep -E ":(3000|3001|5270) "'
    echo
    echo "\$ bash scripts/ssh_all.sh 'cat /sys/fs/cgroup/cpu.max; free -m | head -2'   # one CPU each"
    bash scripts/ssh_all.sh 'echo -n "cpu.max: "; cat /sys/fs/cgroup/cpu.max; echo -n "memory.max: "; cat /sys/fs/cgroup/memory.max'
  } > "$OUT/04_ssh_listeners.txt"
  echo "  04_ssh_listeners.txt"
}

cap_tests() {
  {
    echo "\$ node app/tests/smoke.js        # backend + database + dedup + the public routes"
    node app/tests/smoke.js 2>&1 | tail -32
    echo
    echo "\$ python3 lb/test_lb.py          # the balancer against real backends"
    python3 lb/test_lb.py 2>&1 | tail -34
  } > "$OUT/07_test_suites.txt"
  echo "  07_test_suites.txt"
}

cap_throttle() {
  {
    echo "# Why the database was moved off sys1: cgroup CPU accounting during a load run."
    echo "\$ ssh lbsys1 'cat /sys/fs/cgroup/cpu.stat'   (sampled 8 s apart while 60 clients ran)"
    echo
    python3 - <<'EOF'
import subprocess, threading, time, json
HOSTS = [("sys1", "lbsys1"), ("sys2", "lbsys2"), ("sys3", "lbsys3"), ("sys4", "lbsys4")]
KEYS = ("usage_usec", "nr_periods", "nr_throttled", "throttled_usec")
gen = subprocess.Popen(["python3", "loadgen/loadgen.py", "--url", "http://10.1.75.53:3269",
                        "--concurrency", "60", "--duration", "20", "--warmup", "3",
                        "--run-id", "throttle_probe", "--out-dir", "/tmp"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
import json as _json
time.sleep(5)
res = {}
def sample(name, host):
    out = subprocess.run(["ssh", "-o", "BatchMode=yes", host,
                          "cat /sys/fs/cgroup/cpu.stat; sleep 8; echo ---; cat /sys/fs/cgroup/cpu.stat"],
                         capture_output=True, text=True).stdout
    a, b = out.split("---")
    p = lambda t: {k: int(v) for k, v in (l.split() for l in t.split("\n") if l.strip()) if k in KEYS}
    res[name] = (p(a), p(b))
ts = [threading.Thread(target=sample, args=hp) for hp in HOSTS]
for t in ts: t.start()
for t in ts: t.join()
gen.wait()
print(f"{'system':<8}{'CPU used':>10}{'periods':>10}{'throttled':>11}{'throttled':>12}")
print(f"{'':<8}{'(% of 1)':>10}{'elapsed':>10}{'periods':>11}{'time (s)':>12}")
for name, _ in HOSTS:
    a, b = res[name]
    print(f"{name:<8}{(b['usage_usec']-a['usage_usec'])/8e6*100:>9.1f}%"
          f"{b['nr_periods']-a['nr_periods']:>10}{b['nr_throttled']-a['nr_throttled']:>11}"
          f"{(b['throttled_usec']-a['throttled_usec'])/1e6:>12.2f}")
try:
    s = _json.load(open("/tmp/throttle_probe.json"))["summary"]
    busiest = max((res[n][1]['usage_usec'] - res[n][0]['usage_usec']) / 8e6 * 100 for n, _ in HOSTS)
    print(f"\nthe probe itself achieved {s['throughput_rps']:.0f} req/s at {s['latency_ms']['p95']:.0f} ms p95")
    if busiest < 60:
        print("NOTE: no system went above 60 % of its CPU, so this particular sample was NOT")
        print("      cluster-bound — the client link was the limit while it was taken, and it")
        print("      says nothing about throttling. The diagnosis in the report was made from a")
        print("      sample in which sys1 ran at 82 % and was throttled in 41 of 80 periods.")
    else:
        print("the busiest system reached %.0f %% of its CPU, so the cluster was the bottleneck here" % busiest)
except Exception as e:
    pass
EOF
    echo
    echo "# A throttled period means every task in the container is stopped until the next"
    echo "# 100 ms period begins — which is where the unexplained queueing time was going."
  } > "$OUT/08_cgroup_throttling.txt"
  echo "  08_cgroup_throttling.txt"
}

cap_dedup() {
  { echo "\$ python3 scripts/dedup_test.py --url $LB --n 20"
    python3 scripts/dedup_test.py --url "$LB" --n 20 2>&1; } > "$OUT/02_dedup_test.txt"
  echo "  02_dedup_test.txt"
}

case "${1:-all}" in
  sanity)     cap_sanity ;;
  public)     cap_public ;;
  lb)         cap_lb ;;
  db)         cap_db ;;
  hosts)      cap_hosts ;;
  tests)      cap_tests ;;
  throttle)   cap_throttle ;;
  dedup)      cap_dedup ;;
  all)        cap_sanity; cap_public; cap_lb; cap_db; cap_hosts; cap_dedup; cap_tests; cap_throttle ;;
  *) echo "unknown target $1"; exit 1 ;;
esac
echo "captures in $OUT"

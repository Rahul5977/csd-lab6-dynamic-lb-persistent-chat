#!/usr/bin/env bash
# One command that prints the health of the whole Assignment-6 system, plus the
# previous assignments' public endpoints (they must keep working).
set -u
LB_URL="${LB_URL:-http://10.1.75.53:3269}"
check() {
  local name="$1" url="$2" out
  out=$(curl -sS -m 6 "$url" 2>&1)
  if echo "$out" | grep -q '"status":"ok"'; then echo "✅ $name  $url  $(echo "$out" | head -c 110)"; else echo "❌ $name  $url  $(echo "$out" | head -c 110)"; fi
}
echo "== Assignment 6 sanity check $(date) =="
check "LB v2 (public)   " "$LB_URL/lb/health"
check "app via LB       " "$LB_URL/health"
curl -sS -m 6 "$LB_URL/lb/stats" 2>/dev/null | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(f\"   algorithm={d['algorithm']} active_backends={d['active_backends']} total_requests={d['total_requests']}\")
for b in d['backends']:
    print(f\"   - {b['id']:<5} {b['host']}:{b['port']:<5} {b['state']:<9} src={b['source']:<8} ewma={b['ewma_ms']} score={b['score']} cpu={(b['load'] or {}).get('cpu_pct')} reqs={b['requests']}\")
" 2>/dev/null || echo "❌ LB stats unreadable"
ssh -o BatchMode=yes -o ConnectTimeout=6 lbsys1 "curl -sS -m 4 http://127.0.0.1:5270/stats" 2>/dev/null | python3 -c "
import json,sys; d=json.load(sys.stdin); print(f\"✅ DB service sys1:5270  sqlite  messages={d['messages']} users={d['users']} rooms={d['rooms']} dups_rejected={d['duplicates_rejected_total']} subscribers={d['subscribers']}\")" 2>/dev/null || echo "❌ DB service sys1:5270"
for s in 2 3 4; do
  p=3000; [ $s = 4 ] && p=3001
  out=$(ssh -o BatchMode=yes -o ConnectTimeout=6 lbsys$s "curl -sS -m 4 http://127.0.0.1:$p/health" 2>/dev/null)
  echo "$out" | grep -q '"status":"ok"' && echo "✅ backend sys$s :$p  $(echo "$out" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["version"],"cpu",d["load"]["cpu_pct"],"%")' 2>/dev/null)" || echo "⚪ backend sys$s :$p not running (scale.sh sys$s up)"
done
echo "-- previous assignments --"
check "Lab5 direct sys2 " "http://10.1.75.53:3270/health"
check "Lab5 direct sys3 " "http://10.1.75.53:3271/health"
curl -sS -m 6 http://10.1.75.53:3272/ 2>/dev/null | grep -q "Contour" && echo "✅ Project Contour    http://10.1.75.53:3272 (sys4:3000, untouched)" || echo "❌ Project Contour    http://10.1.75.53:3272"
ssh -o BatchMode=yes -o ConnectTimeout=6 lbsys1 "curl -sS -m 4 http://127.0.0.1:5269/health" 2>/dev/null | grep -q '"status":"ok"' && echo "✅ Lab5 state service sys1:5269 (still running, untouched)" || echo "⚪ Lab5 state service sys1:5269 not running"

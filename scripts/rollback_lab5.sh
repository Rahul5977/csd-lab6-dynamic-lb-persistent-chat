#!/usr/bin/env bash
# ============================================================================
# rollback_lab5.sh — put the Assignment-5 stack back exactly as it was.
#
# Stops the Assignment-6 LB (sys1:3000) and backends (sys2:3000, sys3:3000,
# sys4:3001), then restarts Lab 5's LB from ~/assignment5 on sys1 and Lab 5's
# backends from ~/assignment5 on sys2 and sys3 (sys4 stays as it is: its port
# 3000 belongs to the Contour project since Sep 1). Lab 5's state service on
# sys1:5269 was never stopped. Lab 5 files are never modified by Assignment 6.
#
#   bash scripts/rollback_lab5.sh          # takes ~20 s
#   bash scripts/deploy.sh lb && bash scripts/deploy.sh sys2 ...   # to come back to Assignment 6
# ============================================================================
set -u
cd "$(dirname "$0")/.."
echo "== stopping Assignment-6 backends =="
for s in sys2 sys3 sys4; do bash scripts/scale.sh $s down; done
sleep 2
echo "== stopping Assignment-6 LB on sys1 (DB service on 5270 is left running; it is harmless) =="
ssh lbsys1 "tmux kill-session -t lb6 2>/dev/null; pkill -f '^python3 lb/loadbalancer' 2>/dev/null; for i in 1 2 3 4 5; do ss -tln | grep -q ':3000 ' || break; sleep 1; done; echo '   lb6 stopped'"
echo "== restoring Lab 5 backends (sys2, sys3) =="
for s in 2 3; do
  ssh lbsys$s "
    cd ~/assignment5 || exit 1
    export PATH=\"\$HOME/node/bin:\$PATH\"
    [ -f backend.pid ] && kill \$(cat backend.pid) 2>/dev/null || true
    ulimit -n 65536 2>/dev/null || true
    nohup env \$(cat .env | xargs) node app/server.js >> backend.log 2>&1 < /dev/null &
    echo \$! > backend.pid
    for i in 1 2 3 4 5 6 7 8 9 10; do curl -sS -m 2 http://127.0.0.1:3000/health 2>/dev/null | grep -q '\"status\":\"ok\"' && { echo '   sys$s Lab 5 backend healthy'; exit 0; }; sleep 1; done
    echo '   sys$s FAILED'; tail -5 backend.log
  "
done
echo "== restoring Lab 5 LB on sys1:3000 =="
ssh lbsys1 "
  cd ~/assignment5 && tmux kill-session -t lb 2>/dev/null
  tmux new-session -d -s lb 'cd ~/assignment5 && ulimit -n 65536 && python3 lb/loadbalancer.py lb/lb.conf.json >> lb.log 2>&1'
  for i in 1 2 3 4 5 6 7 8 9 10; do curl -sS -m 2 http://127.0.0.1:3000/lb/health 2>/dev/null | grep -q '\"status\":\"ok\"' && { echo '   Lab 5 LB healthy'; exit 0; }; sleep 1; done
  echo '   Lab 5 LB FAILED'; tail -5 lb.log
"
echo "== public check =="
curl -sS -m 5 http://10.1.75.53:3269/lb/stats | head -c 300; echo
echo "Lab 5 restored."

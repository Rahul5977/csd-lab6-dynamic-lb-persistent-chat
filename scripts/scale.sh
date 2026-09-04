#!/usr/bin/env bash
# ============================================================================
# scale.sh — add or remove a backend WHILE THE APPLICATION IS RUNNING.
#
#   bash scripts/scale.sh sys3 up        start the backend on sys3 (it self-registers
#                                        with the LB and is also found by the candidate scan)
#   bash scripts/scale.sh sys3 down      graceful: SIGTERM -> deregister -> drain -> exit
#   bash scripts/scale.sh sys3 kill      hard failure: SIGKILL, the LB must notice by itself
#   bash scripts/scale.sh sys3 status    is it running / healthy?
#
# Used by the dynamic-scaling and failure/recovery experiments and by the demo.
# ============================================================================
set -u
cd "$(dirname "$0")/.."
SYS="${1:?usage: scale.sh sys2|sys3|sys4 up|down|kill|status}"
ACTION="${2:?usage: scale.sh sys2|sys3|sys4 up|down|kill|status}"
host() { case "$1" in sys2) echo lbsys2;; sys3) echo lbsys3;; sys4) echo lbsys4;; esac; }
port() { case "$1" in sys4) echo 3001;; *) echo 3000;; esac; }
H=$(host "$SYS"); P=$(port "$SYS")

case "$ACTION" in
  up)
    ssh "$H" "
      cd ~/assignment6 || exit 1
      export PATH=\"\$HOME/node/bin:\$PATH\"
      if [ -f backend.pid ] && kill -0 \$(cat backend.pid) 2>/dev/null; then echo '$SYS already running'; exit 0; fi
      ulimit -n 65536 2>/dev/null || true
      nohup env \$(cat .env | xargs) node app/server.js >> backend.log 2>&1 < /dev/null &
      echo \$! > backend.pid
      echo \"$SYS started pid \$!\"
    "
    for i in $(seq 1 20); do
      ssh "$H" "curl -sS -m 2 http://127.0.0.1:$P/health" 2>/dev/null | grep -q '"status":"ok"' && { echo "$SYS healthy ✔ ($(date +%T))"; exit 0; }
      sleep 1
    done
    echo "$SYS did not become healthy"; exit 1 ;;
  down)
    ssh "$H" "cd ~/assignment6 && [ -f backend.pid ] && kill -TERM \$(cat backend.pid) 2>/dev/null && echo '$SYS SIGTERM sent (deregister + drain)' || echo '$SYS not running'" ;;
  kill)
    ssh "$H" "cd ~/assignment6 && [ -f backend.pid ] && kill -KILL \$(cat backend.pid) 2>/dev/null && echo \"$SYS KILLED ($(date +%T))\" || echo '$SYS not running'" ;;
  status)
    ssh "$H" "cd ~/assignment6 2>/dev/null && [ -f backend.pid ] && kill -0 \$(cat backend.pid) 2>/dev/null && echo '$SYS running pid '\$(cat backend.pid) || echo '$SYS stopped'; curl -sS -m 2 http://127.0.0.1:$P/health; echo" ;;
  *) echo "unknown action $ACTION"; exit 1 ;;
esac

#!/usr/bin/env bash
# ============================================================================
# deploy.sh — copy + (re)start the Assignment-6 services on the lab systems.
#
#   bash scripts/deploy.sh node22          install user-local Node 22 on sys1 (for node:sqlite)
#   bash scripts/deploy.sh db              SQLite DB service  -> sys1:5270  (tmux "db6")
#   bash scripts/deploy.sh lb              dynamic LB v2      -> sys1:3000  (tmux "lb6", public 3269)
#   bash scripts/deploy.sh sys2|sys3|sys4  backend v3         -> sys2:3000 / sys3:3000 / sys4:3001
#   bash scripts/deploy.sh all             node22 + db + sys2 + lb   (sys3/sys4 join dynamically: scripts/scale.sh)
#
# Everything lives in ~/assignment6 on each box; ~/assignment5 (Lab 5) is never
# touched. Only OUR processes are stopped: Lab 5's LB/backends on the same ports
# (they must yield port 3000) — scripts/rollback_lab5.sh brings them back.
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

REMOTE_DIR='~/assignment6'
SYS1_IP=172.17.0.70                     # Docker-bridge address of sys1 as seen by sys2-4
DB_PORT=5270
LB_PORT=3000                            # container port behind public 10.1.75.53:3269
NODE22="v22.23.2"

# per-system facts (macOS bash 3.2: no associative arrays)
ssh_host()  { case "$1" in sys1) echo lbsys1;; sys2) echo lbsys2;; sys3) echo lbsys3;; sys4) echo lbsys4;; esac; }
app_port()  { case "$1" in sys4) echo 3001;; *) echo 3000;; esac; }   # D-002: sys4:3000 is the Contour project
priv_ip()   { case "$1" in sys2) echo 172.17.0.71;; sys3) echo 172.17.0.72;; sys4) echo 172.17.0.73;; esac; }

# shared registration secret, generated once, kept out of git
[ -f .env ] || printf 'LB_TOKEN=%s\n' "$(python3 -c 'import secrets;print(secrets.token_hex(16))')" > .env
LB_TOKEN=$(grep '^LB_TOKEN=' .env | cut -d= -f2)

push() {  # $1 = ssh alias, $2... = local dirs/files (no rsync on the lab boxes: tar over ssh)
  local host="$1"; shift
  COPYFILE_DISABLE=1 tar --no-xattrs -czf - --exclude data --exclude '*.log' --exclude __pycache__ "$@" | ssh "$host" "mkdir -p $REMOTE_DIR && cd $REMOTE_DIR && tar xzf -"
}

wait_ok() {  # $1 ssh alias, $2 url (inside the box), $3 label
  for i in $(seq 1 25); do
    if ssh "$1" "curl -sS -m 2 $2" 2>/dev/null | grep -q '"status":"ok"'; then echo "   $3 healthy ✔"; return 0; fi
    sleep 1
  done
  echo "   $3 FAILED to become healthy"; return 1
}

install_node22() {
  echo "== Node $NODE22 -> sys1 (~/node) =="
  ssh lbsys1 "
    set -e
    if ~/node/bin/node -e 'require(\"node:sqlite\")' 2>/dev/null; then echo '   node with sqlite already present:' \$(~/node/bin/node -v); exit 0; fi
    echo '   downloading…'
    curl -fsSL -o /tmp/node22.tar.xz https://nodejs.org/dist/$NODE22/node-$NODE22-linux-x64.tar.xz
    rm -rf ~/node-tmp && mkdir -p ~/node-tmp && tar xJf /tmp/node22.tar.xz -C ~/node-tmp --strip-components=1
    rm -rf ~/node && mv ~/node-tmp ~/node && rm -f /tmp/node22.tar.xz
    ~/node/bin/node -v && ~/node/bin/node -e 'require(\"node:sqlite\"); console.log(\"   node:sqlite OK\")'
  "
}

deploy_db() {
  echo "== DB service -> sys1:$DB_PORT (SQLite) =="
  push lbsys1 app
  ssh lbsys1 "
    set -e
    cd $REMOTE_DIR && mkdir -p data logs
    tmux kill-session -t db6 2>/dev/null || true     # (no pkill -f here: it would match this very shell)
    for i in 1 2 3 4 5; do ss -tln | grep -q ':$DB_PORT ' || break; sleep 1; done
    # MIGRATE_FROM imports Lab 5's users/rooms/messages once (marker file prevents repeats)
    tmux new-session -d -s db6 \"cd $REMOTE_DIR && ulimit -n 65536 && PORT=$DB_PORT DATA_DIR=$REMOTE_DIR/data MIGRATE_FROM=\$HOME/assignment5/data ~/node/bin/node app/db_service.js >> db.log 2>&1\"
  "
  wait_ok lbsys1 "http://127.0.0.1:$DB_PORT/health" "db" || { ssh lbsys1 "tail -20 $REMOTE_DIR/db.log"; return 1; }
  ssh lbsys1 "curl -sS -m 3 http://127.0.0.1:$DB_PORT/stats" | python3 -c "import json,sys; d=json.load(sys.stdin); print(f\"   {d['messages']} messages, {d['users']} users, {d['rooms']} rooms, {d['db_bytes']//1024} KB, {d['node']}\")"
}

deploy_backend() {  # $1 = sys2|sys3|sys4
  local sys="$1" host="$(ssh_host "$1")" port="$(app_port "$1")" ip="$(priv_ip "$1")"
  echo "== backend v3 -> $sys ($host, port $port, advertise $ip) =="
  push "$host" app
  ssh "$host" "
    set -e
    cd $REMOTE_DIR
    export PATH=\"\$HOME/node/bin:\$PATH\"
    command -v node >/dev/null || { echo 'no node — run scripts/install_node.sh $host first'; exit 1; }
    printf 'PORT=%s\nBACKEND_ID=%s\nDB_URL=http://%s:%s\nLB_URL=http://%s:%s\nLB_TOKEN=%s\nADVERTISE_HOST=%s\nADVERTISE_PORT=%s\nLB_HEARTBEAT_S=5\nLOG_LEVEL=info\nUV_THREADPOOL_SIZE=2\n' \
        '$port' '$sys' '$SYS1_IP' '$DB_PORT' '$SYS1_IP' '$LB_PORT' '$LB_TOKEN' '$ip' '$port' > .env
    # stop OUR previous instance (pidfile) and, on sys2/sys3, the Lab 5 backend holding port $port
    [ -f backend.pid ] && kill \$(cat backend.pid) 2>/dev/null || true
    if [ -f ~/assignment5/backend.pid ] && [ '$port' = 3000 ]; then kill \$(cat ~/assignment5/backend.pid) 2>/dev/null || true; fi
    for i in 1 2 3 4 5 6; do ss -tln | grep -q \":$port \" || break; sleep 1; done
    ulimit -n 65536 2>/dev/null || true
    nohup env \$(cat .env | xargs) node app/server.js >> backend.log 2>&1 < /dev/null &
    echo \$! > backend.pid
  "
  wait_ok "$host" "http://127.0.0.1:$port/health" "$sys" || { ssh "$host" "tail -20 $REMOTE_DIR/backend.log"; return 1; }
}

deploy_lb() {
  echo "== dynamic LB v2 -> sys1:$LB_PORT (public 10.1.75.53:3269) =="
  ssh lbsys1 "mkdir -p $REMOTE_DIR/lb $REMOTE_DIR/logs"
  push lbsys1 lb
  # render the production config with the token; static pool = sys2 only, sys3/sys4 discovered live
  python3 - "$LB_TOKEN" <<'EOF' | ssh lbsys1 "cat > $REMOTE_DIR/lb/lb.conf.json"
import json, sys
conf = json.load(open('lb/lb.conf.template.json'))
conf['register_token'] = sys.argv[1]
print(json.dumps(conf, indent=2))
EOF
  ssh lbsys1 "
    set -e
    cd $REMOTE_DIR
    tmux kill-session -t lb6 2>/dev/null || true
    pkill -f '^python3 lb/loadbalancer' 2>/dev/null || true       # ours (v2) AND Lab 5's v1 on port $LB_PORT
    tmux kill-session -t lb 2>/dev/null || true                    # Lab 5's tmux session (restored by rollback_lab5.sh)
    for i in 1 2 3 4 5 6; do ss -tln | grep -q ':$LB_PORT ' || break; sleep 1; done
    tmux new-session -d -s lb6 'cd $REMOTE_DIR && ulimit -n 65536 && python3 lb/loadbalancer.py lb/lb.conf.json >> lb.log 2>&1'
  "
  wait_ok lbsys1 "http://127.0.0.1:$LB_PORT/lb/health" "lb" || { ssh lbsys1 "tail -20 $REMOTE_DIR/lb.log"; return 1; }
}

case "${1:?usage: deploy.sh node22|db|lb|sys2|sys3|sys4|all}" in
  node22)          install_node22 ;;
  db)              deploy_db ;;
  lb)              deploy_lb ;;
  sys2|sys3|sys4)  deploy_backend "$1" ;;
  all)             install_node22; deploy_db; deploy_backend sys2; deploy_lb; echo; echo "sys3/sys4 join on demand: bash scripts/scale.sh sys3 up" ;;
  *) echo "unknown target $1"; exit 1 ;;
esac

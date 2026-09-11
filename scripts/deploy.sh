#!/usr/bin/env bash
# ============================================================================
# deploy.sh — copy + (re)start the Assignment-6 services on the lab systems.
#
#   bash scripts/deploy.sh node22          install user-local Node 22 on sys1 (for node:sqlite)
#   bash scripts/deploy.sh db              SQLite DB service  -> $DB_SYS:5270 (tmux "db6")
#   bash scripts/deploy.sh movedb sys3     move the database (data included) to another system
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
# Which system runs the shared database. sys1 is quota-limited to ONE cpu and it
# already terminates every client connection as the load balancer; with the DB
# next to it the two competed for that core and the container was CFS-throttled
# in half of all 100 ms periods (see the report's bottleneck analysis), so the
# database lives on sys3 instead.
DB_SYS="${DB_SYS:-sys3}"
# The room behind the public /message and /feed routes. /feed returns this room in
# full, so it must hold conversation rather than the 739 436 messages my own load
# generators left in the original `public` room. Pointing the routes at a fresh
# room empties the feed without deleting a single row: everything written before
# is still in the database and still reachable through the chat API.
PUBLIC_ROOM="${PUBLIC_ROOM:-room}"
LB_PORT=3000                            # container port behind public 10.1.75.53:3269
NODE22="v22.23.2"

# per-system facts (macOS bash 3.2: no associative arrays)
ssh_host()  { case "$1" in sys1) echo lbsys1;; sys2) echo lbsys2;; sys3) echo lbsys3;; sys4) echo lbsys4;; esac; }
app_port()  { case "$1" in sys4) echo 3001;; *) echo 3000;; esac; }   # D-002: sys4:3000 is the Contour project
priv_ip()   { case "$1" in sys1) echo 172.17.0.70;; sys2) echo 172.17.0.71;; sys3) echo 172.17.0.72;; sys4) echo 172.17.0.73;; esac; }
DB_HOST="$(ssh_host "$DB_SYS")"
DB_IP="$(priv_ip "$DB_SYS")"

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

install_node22() {   # $1 = ssh alias (default lbsys1)
  local host="${1:-lbsys1}"
  echo "== Node $NODE22 -> $host (~/node) =="
  ssh "$host" "
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
  echo "== DB service -> $DB_SYS ($DB_HOST:$DB_PORT, SQLite) =="
  push "$DB_HOST" app
  ssh "$DB_HOST" "
    set -e
    cd $REMOTE_DIR && mkdir -p data logs
    # stop our previous instance (tmux on sys1, pidfile everywhere else)
    command -v tmux >/dev/null && tmux kill-session -t db6 2>/dev/null || true
    [ -f db.pid ] && kill \$(cat db.pid) 2>/dev/null || true
    for i in 1 2 3 4 5 6; do ss -tln | grep -q ':$DB_PORT ' || break; sleep 1; done
    ulimit -n 65536 2>/dev/null || true
    # MIGRATE_FROM imports Lab 5's users/rooms/messages once (marker file prevents repeats)
    nohup env PORT=$DB_PORT DATA_DIR=$REMOTE_DIR/data MIGRATE_FROM=\$HOME/assignment5/data \
        \$HOME/node/bin/node app/db_service.js >> db.log 2>&1 < /dev/null &
    echo \$! > db.pid
  "
  wait_ok "$DB_HOST" "http://127.0.0.1:$DB_PORT/health" "db" || { ssh "$DB_HOST" "tail -20 $REMOTE_DIR/db.log"; return 1; }
  ssh "$DB_HOST" "curl -sS -m 3 http://127.0.0.1:$DB_PORT/stats" | python3 -c "import json,sys; d=json.load(sys.stdin); print(f\"   {d['messages']} messages, {d['users']} users, {d['rooms']} rooms, {d['db_bytes']//1024} KB, {d['node']}\")"
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
    printf 'PORT=%s\nBACKEND_ID=%s\nDB_URL=http://%s:%s\nLB_URL=http://%s:%s\nLB_TOKEN=%s\nADVERTISE_HOST=%s\nADVERTISE_PORT=%s\nLB_HEARTBEAT_S=5\nLOG_LEVEL=info\nUV_THREADPOOL_SIZE=2\nPUBLIC_ROOM=%s\nFEED_MAX=%s\nFEED_BYTES=%s\n' \
        '$port' '$sys' '$DB_IP' '$DB_PORT' '$SYS1_IP' '$LB_PORT' '$LB_TOKEN' '$ip' '$port' '$PUBLIC_ROOM' "${FEED_MAX:-35000}" "${FEED_BYTES:-1048576}" > .env
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

# Move the database, data and all, to another system without losing a row.
move_db() {
  local from="$1" to="$2"
  local fh th; fh="$(ssh_host "$from")"; th="$(ssh_host "$to")"
  echo "== moving the database $from -> $to =="
  ssh "$fh" "tmux kill-session -t db6 2>/dev/null || true; sleep 2"
  ssh "$th" "mkdir -p $REMOTE_DIR/data"
  # checkpoint the WAL into the main file first, then copy the whole data dir
  ssh "$fh" "cd $REMOTE_DIR && \$HOME/node/bin/node -e \"const{DatabaseSync}=require('node:sqlite');const d=new DatabaseSync('data/chat.sqlite');d.exec('PRAGMA wal_checkpoint(TRUNCATE)');d.close()\" >/dev/null 2>&1; tar czf - data 2>/dev/null" \
    | ssh "$th" "cd $REMOTE_DIR && tar xzf - 2>/dev/null"
  ssh "$th" "ls -la $REMOTE_DIR/data | sed 's/^/   /'"
  DB_SYS="$to" DB_HOST="$th" DB_IP="$(priv_ip "$to")" deploy_db
}

case "${1:?usage: deploy.sh node22|db|lb|movedb|sys2|sys3|sys4|all}" in
  node22)          install_node22 "${2:-lbsys1}" ;;
  movedb)          move_db "${2:-sys1}" "${3:-$DB_SYS}" ;;
  db)              deploy_db ;;
  lb)              deploy_lb ;;
  sys2|sys3|sys4)  deploy_backend "$1" ;;
  all)             install_node22 lbsys1; install_node22 "$DB_HOST"; deploy_db; deploy_backend sys2; deploy_lb; echo; echo "sys3/sys4 join on demand: bash scripts/scale.sh sys3 up" ;;
  *) echo "unknown target $1"; exit 1 ;;
esac

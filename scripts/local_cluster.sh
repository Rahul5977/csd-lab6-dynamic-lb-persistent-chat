#!/usr/bin/env bash
# Local dev cluster on 127.0.0.1: DB (15270) + backends b1/b2/b3 (18001-18003) + LB (18000).
#   bash scripts/local_cluster.sh start [n_backends]   |  stop  |  add b3  |  kill b3
set -u
cd "$(dirname "$0")/.."
RUN=.local; mkdir -p $RUN
DB=15270; LB=18000; TOKEN=local-token
bport() { echo $((18000 + ${1#b})); }
start_backend() {
  local id="$1"
  PORT=$(bport $id) BACKEND_ID=$id HOST=127.0.0.1 DB_URL=http://127.0.0.1:$DB LB_URL=http://127.0.0.1:$LB LB_TOKEN=$TOKEN \
    ADVERTISE_HOST=127.0.0.1 LB_HEARTBEAT_S=3 LOG_LEVEL=info ${SLOW:+TEST_SLOW_MS=$SLOW} \
    node app/server.js >> $RUN/$id.log 2>&1 &
  echo $! > $RUN/$id.pid; echo "started $id on $(bport $id)"
}
case "${1:-start}" in
  start)
    n=${2:-3}
    PORT=$DB DATA_DIR=$RUN/data HOST=127.0.0.1 node app/db_service.js >> $RUN/db.log 2>&1 & echo $! > $RUN/db.pid
    sleep 0.7
    python3 - <<EOF > $RUN/lb.conf.json
import json
print(json.dumps({"listen_host":"127.0.0.1","listen_port":$LB,"algorithm":"threshold","switch_threshold":0.55,"inflight_cap":24,"rt_cap_ms":250,
 "backends":[{"id":"b1","host":"127.0.0.1","port":18001,"weight":1}],
 "health_interval_s":2,"health_timeout_s":4,"fail_threshold":2,"rise_threshold":1,"register_token":"$TOKEN",
 "discovery":{"candidates":[{"host":"127.0.0.1","port":18002},{"host":"127.0.0.1","port":18003}],"interval_s":2},
 "access_log":"$RUN/lb_access.csv"}, indent=1))
EOF
    python3 lb/loadbalancer.py $RUN/lb.conf.json >> $RUN/lb.log 2>&1 & echo $! > $RUN/lb.pid
    for i in $(seq 1 $n); do start_backend b$i; done
    sleep 1.5; curl -sS http://127.0.0.1:$LB/lb/stats | python3 -c "import json,sys;d=json.load(sys.stdin);print('LB up:',d['active_backends'],'active',[ (b['id'],b['state']) for b in d['backends']])"
    echo "LB: http://127.0.0.1:$LB  dashboard: http://127.0.0.1:$LB/lb/" ;;
  add)  start_backend "$2" ;;
  kill) kill -9 $(cat $RUN/$2.pid) 2>/dev/null && echo "killed $2" ;;
  down) kill -TERM $(cat $RUN/$2.pid) 2>/dev/null && echo "SIGTERM $2" ;;
  stop) for f in $RUN/*.pid; do kill $(cat $f) 2>/dev/null; done; rm -f $RUN/*.pid; echo stopped ;;
esac

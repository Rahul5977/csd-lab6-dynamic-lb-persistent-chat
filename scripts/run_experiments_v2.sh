#!/usr/bin/env bash
# ============================================================================
# run_experiments_v2.sh — the measurement matrix for the UPDATED assignment.
#
# Everything here drives the two routes the assignment fixes (/message and
# /feed) through the public load-balancer URL, and every run samples the
# utilisation of ALL FOUR systems (scripts/sysmetrics.py) at the same time.
#
#   bash scripts/run_experiments_v2.sh          # everything, in this order
#   bash scripts/run_experiments_v2.sh THR      # threshold sweep at 60 clients -> the optimal threshold
#   bash scripts/run_experiments_v2.sh THRLOW   # the same sweep at 10 clients (where T actually bites)
#   bash scripts/run_experiments_v2.sh ALGO     # threshold vs round_robin / least_conn / adaptive
#   bash scripts/run_experiments_v2.sh PUB      # response time vs load (closed loop)
#   bash scripts/run_experiments_v2.sh OPEN     # throughput vs offered load (open loop)
#   bash scripts/run_experiments_v2.sh UTIL     # ramp 1->200 users: utilisation timeline
#   bash scripts/run_experiments_v2.sh PFAIL    # backend failure + recovery on the public routes
#
# THR runs first because ALGO/PUB/OPEN/UTIL are then run at the threshold it
# picked (written to results/processed/optimal_threshold.txt).
# ============================================================================
set -uo pipefail
cd "$(dirname "$0")/.."

LB_URL="${LB_URL:-http://10.1.75.53:3269}"
THRESHOLDS=(${THRESHOLDS:-0.15 0.30 0.45 0.55 0.70 0.85 1.00})
LEVELS=(${LEVELS:-1 5 10 25 50 100 200})
RATES=(${RATES:-50 100 200 300 400})
REPS=${REPS:-2}
REPS_PUB=${REPS_PUB:-1}      # the load sweep covers 1/2/3 backends x 7 levels, so one rep each
DUR=${DUR:-45}
WARM=${WARM:-8}
COOL=${COOL:-8}
# random message length and random inter-message interval, as the spec asks for
GEN_ARGS=(--api public --msg-min 8 --msg-max 240 --think-min 0 --think-max 0.05 --poll-stats)
MANIFEST=evidence/07_run_manifest.md
mkdir -p results/raw results/sysmetrics results/processed logs evidence

log() { echo "[$(date +%T)] $*"; }
active() { curl -sS -m 5 "$LB_URL/lb/stats" 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin)['active_backends'])" 2>/dev/null || echo -1; }
lbcfg() { curl -sS -m 5 -X POST "$LB_URL/lb/config" -H 'Content-Type: application/json' -d "$1" > /dev/null; sleep 1; }

set_n() {  # $1 = 1|2|3 backends — sys2 always up; sys3 for >=2; sys4 for >=3
  local n="$1"
  [ "$n" -ge 2 ] && bash scripts/scale.sh sys3 up > /dev/null || bash scripts/scale.sh sys3 down > /dev/null
  [ "$n" -ge 3 ] && bash scripts/scale.sh sys4 up > /dev/null || bash scripts/scale.sh sys4 down > /dev/null
  for i in $(seq 1 40); do
    [ "$(active)" = "$n" ] && { log "pool -> $n active backend(s)"; return 0; }
    sleep 1
  done
  log "WARNING: pool did not settle to $n (active=$(active))"
}

# One measured run: load generator on the public routes + a utilisation sampler
# on sys1..sys4 for exactly the same window.
one_run() {  # $1 run_id, $2 config, $3 param, $4 rep, $5... loadgen args
  local rid="$1" cfg="$2" param="$3" rep="$4"; shift 4
  if [ -f "results/raw/${rid}.json" ]; then log "-- $rid exists, skipping"; return 0; fi
  log "== RUN $rid =="
  python3 scripts/sysmetrics.py --run-id "$rid" --duration 0 > "logs/sysmetrics_${rid}.log" 2>&1 &
  local sm=$!
  sleep 2
  if ! python3 loadgen/loadgen.py --url "$LB_URL" "${GEN_ARGS[@]}" --run-id "$rid" "$@" > "logs/loadgen_${rid}.log" 2>&1; then
    log "   RUN FAILED — see logs/loadgen_${rid}.log"
    kill -TERM $sm 2>/dev/null; wait $sm 2>/dev/null
    sleep "$COOL"; return 0
  fi
  kill -TERM $sm 2>/dev/null; wait $sm 2>/dev/null
  python3 - "$rid" "$cfg" "$param" "$rep" "$MANIFEST" <<'EOF'
import json, sys, datetime
rid, cfg, param, rep, manifest = sys.argv[1:6]
s = json.load(open(f'results/raw/{rid}.json'))['summary']
line = (f"| {rid} | {cfg} | {param} | {rep} | {datetime.datetime.now().isoformat(timespec='seconds')} | "
        f"{s['throughput_rps']} rps | {s['latency_ms']['p95']} ms | {s['errors']} ({s['error_rate_pct']}%) | "
        f"{s['active_backends_min']}-{s['active_backends_max']} |\n")
open(manifest, 'a').write(line)
print(f"   {s['throughput_rps']} rps, p95 {s['latency_ms']['p95']} ms, {s['errors']} errors, "
      f"{s['active_backends_min']}-{s['active_backends_max']} backends")
EOF
  sleep "$COOL"
}

# ── THR: which switching threshold is best? ─────────────────────────────────
# Swept at TWO load levels. At 60 clients every backend is over any threshold
# almost immediately, so the sweep mostly measures the tie-break; at 10 clients
# the threshold really decides whether the cluster concentrates or spreads, and
# that is where a badly chosen T shows up.
run_THR() {
  local users="${THR_USERS:-60}" tag="${THR_TAG:-}"
  log "### THRESHOLD SWEEP (3 backends, $users clients, ${#THRESHOLDS[@]} thresholds x $REPS reps)"
  set_n 3
  for rep in $(seq 1 "$REPS"); do
    for T in "${THRESHOLDS[@]}"; do
      lbcfg "{\"algorithm\":\"threshold\",\"switch_threshold\":$T}"
      one_run "THR${tag}_t${T}_rep${rep}" threshold "T=$T" "$rep" \
        --concurrency "$users" --duration "$DUR" --warmup "$WARM" --note "threshold sweep T=$T at $users clients"
    done
  done
  [ -n "$tag" ] && return 0
  .venv/bin/python scripts/pick_threshold.py || python3 scripts/pick_threshold.py
}

run_THRLOW() { THR_USERS=10 THR_TAG=LOW run_THR; }

# ── ALGO: the chosen threshold against the classic algorithms ───────────────
run_ALGO() {
  local T; T=$(cat results/processed/optimal_threshold.txt 2>/dev/null || echo 0.55)
  log "### ALGORITHM COMPARISON (threshold T=$T vs round_robin / least_connections / adaptive)"
  set_n 3
  for rep in $(seq 1 "$REPS"); do
    for algo in threshold round_robin least_connections adaptive; do
      if [ "$algo" = threshold ]; then lbcfg "{\"algorithm\":\"threshold\",\"switch_threshold\":$T}";
      else lbcfg "{\"algorithm\":\"$algo\"}"; fi
      one_run "PALGO_${algo}_rep${rep}" "$algo" "$algo" "$rep" \
        --concurrency 60 --duration "$DUR" --warmup "$WARM" --note "public-API algorithm comparison"
    done
  done
  lbcfg "{\"algorithm\":\"threshold\",\"switch_threshold\":$T}"
}

# ── PUB: response time vs load, 1 / 2 / 3 backends ─────────────────────────
run_PUB() {
  local T; T=$(cat results/processed/optimal_threshold.txt 2>/dev/null || echo 0.55)
  lbcfg "{\"algorithm\":\"threshold\",\"switch_threshold\":$T}"
  log "### RESPONSE TIME vs LOAD on /message + /feed (levels: ${LEVELS[*]})"
  for rep in $(seq 1 "$REPS_PUB"); do
    for c in "${LEVELS[@]}"; do
      for n in $(python3 -c "import random;x=[1,2,3];random.shuffle(x);print(*x)"); do
        set_n "$n"
        one_run "P${n}_c${c}_rep${rep}" "$n backend(s)" "$c" "$rep" \
          --concurrency "$c" --duration "$DUR" --warmup "$WARM" --note "public API, $n backend(s), $c users"
      done
    done
  done
  set_n 3
}

# ── OPEN: throughput vs offered load ───────────────────────────────────────
run_OPEN() {
  log "### THROUGHPUT vs OFFERED LOAD (rates: ${RATES[*]} req/s)"
  for n in 1 3; do
    set_n "$n"
    for r in "${RATES[@]}"; do
      one_run "PO${n}_r${r}_rep1" "$n backend(s)" "$r rps" 1 \
        --concurrency 300 --rate "$r" --duration "$DUR" --warmup "$WARM" --note "open loop $r rps, $n backend(s)"
    done
  done
  set_n 3
}

# ── UTIL: one long ramp — the utilisation-of-all-4-systems figure ───────────
run_UTIL() {
  log "### UTILISATION RAMP 1 -> 200 users on 3 backends"
  set_n 3
  one_run "PUTIL_ramp" ramp "1..200 users" 1 \
    --ramp "1:30,10:30,25:30,50:45,100:45,200:60,25:30" --duration 270 --warmup 0 \
    --note "utilisation of all four systems while the offered load rises"
}

# ── PFAIL: kill a backend mid-run on the public routes ─────────────────────
run_PFAIL() {
  log "### FAILURE + RECOVERY on the public routes"
  set_n 3
  python3 scripts/sysmetrics.py --run-id PFAIL_recovery --duration 0 > logs/sysmetrics_PFAIL_recovery.log 2>&1 &
  local sm=$!
  ( sleep 45; log "   >> killing sys3 (hard)"; bash scripts/scale.sh sys3 kill > /dev/null
    sleep 45; log "   >> restarting sys3"; bash scripts/scale.sh sys3 up > /dev/null ) &
  local killer=$!
  python3 loadgen/loadgen.py --url "$LB_URL" "${GEN_ARGS[@]}" --run-id PFAIL_recovery \
    --concurrency 60 --duration 150 --warmup 0 --note "sys3 killed at t=45s, restarted at t=90s" \
    > logs/loadgen_PFAIL_recovery.log 2>&1
  wait $killer 2>/dev/null
  kill -TERM $sm 2>/dev/null; wait $sm 2>/dev/null
  curl -sS -m 5 "$LB_URL/lb/events" > results/raw/PFAIL_events.json
  log "   failure/recovery run done"
  set_n 3
}

case "${1:-all}" in
  THR)   run_THR ;;
  THRLOW) run_THRLOW ;;
  ALGO)  run_ALGO ;;
  PUB)   run_PUB ;;
  OPEN)  run_OPEN ;;
  UTIL)  run_UTIL ;;
  PFAIL) run_PFAIL ;;
  all)   run_THR; run_THRLOW; run_ALGO; run_PUB; run_OPEN; run_UTIL; run_PFAIL ;;
  *) echo "unknown target $1"; exit 1 ;;
esac
log "done: $(ls results/raw/*.json | wc -l | tr -d ' ') result files"

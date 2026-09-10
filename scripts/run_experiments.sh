#!/usr/bin/env bash
# ============================================================================
# run_experiments.sh — the full measurement matrix, reproducible in one command.
#
#   bash scripts/run_experiments.sh              # everything, in this order:
#   bash scripts/run_experiments.sh SCALE        # dynamic scaling timeline (1 -> 2 -> 3 backends under rising load)
#   bash scripts/run_experiments.sh FAIL         # backend failure + recovery under load
#   bash scripts/run_experiments.sh ALGO         # adaptive vs round_robin vs least_connections with one loaded backend
#   bash scripts/run_experiments.sh L            # response time vs load (closed loop) for 1, 2, 3 backends
#   bash scripts/run_experiments.sh O            # throughput vs OFFERED load (open loop) for 1, 2, 3 backends
#
# Design: L and O interleave the 1/2/3-backend configurations inside every
# (level, rep) round in shuffled order, so cross-configuration comparisons share
# the same multi-tenant host conditions (lesson from Assignment 5, D-010).
# The number of backends is changed the DYNAMIC way — by starting/stopping the
# backend processes (scripts/scale.sh); the LB notices by itself.
# Every run's /lb/stats is sampled once per second (active backends, states,
# scores, backend CPU) into the result JSON.
# ============================================================================
set -uo pipefail
cd "$(dirname "$0")/.."

LB_URL="${LB_URL:-http://10.1.75.53:3269}"
LEVELS=(${LEVELS:-1 10 25 50 100 200})
RATES=(${RATES:-20 50 100 200 300})
REPS=${REPS:-2}
DUR=${DUR:-40}
WARM=${WARM:-5}
COOL=${COOL:-8}
MANIFEST=evidence/07_run_manifest.md
mkdir -p results/raw logs evidence
[ -f "$MANIFEST" ] || printf "# Run manifest\n\n| run_id | config | param | rep | started | throughput | p95 | errors | active |\n|---|---|---|---|---|---|---|---|---|\n" > "$MANIFEST"

log() { echo "[$(date +%T)] $*"; }
active() { curl -sS -m 5 "$LB_URL/lb/stats" 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin)['active_backends'])" 2>/dev/null || echo -1; }
set_algo() { curl -sS -m 5 -X POST "$LB_URL/lb/config" -H 'Content-Type: application/json' -d "{\"algorithm\":\"$1\"}" > /dev/null; sleep 1; }

set_n() {  # $1 = 1|2|3 backends — sys2 always; sys3 for >=2; sys4 for >=3
  local n="$1"
  [ "$n" -ge 2 ] && bash scripts/scale.sh sys3 up > /dev/null || bash scripts/scale.sh sys3 down > /dev/null
  [ "$n" -ge 3 ] && bash scripts/scale.sh sys4 up > /dev/null || bash scripts/scale.sh sys4 down > /dev/null
  for i in $(seq 1 40); do
    [ "$(active)" = "$n" ] && { log "pool -> $n active backend(s)"; return 0; }
    sleep 1
  done
  log "WARNING: pool did not settle to $n (active=$(active))"
}

one_run() {  # $1 run_id, $2 config, $3 param, $4 rep, $5... loadgen args
  local rid="$1" cfg="$2" param="$3" rep="$4"; shift 4
  if [ -f "results/raw/${rid}.json" ]; then log "-- $rid exists, skipping"; return 0; fi
  log "== RUN $rid =="
  if ! python3 loadgen/loadgen.py --url "$LB_URL" --api chat --poll-stats --run-id "$rid" "$@" > "logs/loadgen_${rid}.log" 2>&1; then
    log "   RUN FAILED — see logs/loadgen_${rid}.log"; sleep "$COOL"; return 0
  fi
  python3 - "$rid" "$cfg" "$param" "$rep" "$MANIFEST" <<'EOF'
import json, sys, datetime
rid, cfg, param, rep, manifest = sys.argv[1:6]
s = json.load(open(f'results/raw/{rid}.json'))['summary']
line = f"| {rid} | {cfg} | {param} | {rep} | {datetime.datetime.now().isoformat(timespec='seconds')} | {s['throughput_rps']} rps | {s['latency_ms']['p95']} ms | {s['errors']} ({s['error_rate_pct']}%) | {s['active_backends_min']}-{s['active_backends_max']} |\n"
open(manifest, 'a').write(line)
print(f"   done: {s['throughput_rps']} rps, p50 {s['latency_ms']['p50']} ms, p95 {s['latency_ms']['p95']} ms, errors {s['errors']}, dups {s['duplicates_suppressed']}, retried {s['sends_retried']}")
EOF
  sleep "$COOL"
}

TARGET="${1:-all}"
want() { [ "$TARGET" = all ] || [ "$TARGET" = "$1" ]; }

# ── SCALE: rising load, backends added live ──────────────────────────────────
if want SCALE; then
  log "#### SCALE — start with 1 backend, ramp load, add sys3 @140s and sys4 @200s ####"
  set_algo adaptive; set_n 1
  RID=SCALE_ramp
  if [ ! -f results/raw/$RID.json ]; then
    ( sleep 140; log "t=140s: scale.sh sys3 up"; bash scripts/scale.sh sys3 up; sleep 55; log "t=200s: scale.sh sys4 up"; bash scripts/scale.sh sys4 up ) &
    one_run $RID SCALE ramp 1 --ramp "25:40,50:40,100:180,200:40" --warmup 0 --note "sys3 added at t=140s, sys4 at t=200s"
    wait
    python3 - <<'EOF'
import json
p='results/raw/SCALE_ramp.json'; d=json.load(open(p)); d['summary']['events']=[{'t':140,'label':'sys3 added'},{'t':200,'label':'sys4 added'}]; json.dump(d,open(p,'w'))
EOF
  fi
fi

# ── FAIL: kill a backend under load, then bring it back ─────────────────────
if want FAIL; then
  log "#### FAIL — c=100 on 3 backends; SIGKILL sys3 @40s, restart @90s ####"
  set_algo adaptive; set_n 3
  RID=FAIL_recovery_c100
  if [ ! -f results/raw/$RID.json ]; then
    ( sleep 40; log "t=40s: KILL sys3"; bash scripts/scale.sh sys3 kill; sleep 50; log "t=90s: restart sys3"; bash scripts/scale.sh sys3 up ) &
    one_run $RID FAIL kill 1 --concurrency 100 --duration 150 --warmup 5 --note "sys3 SIGKILL at t=40s, restarted at t=90s"
    wait
    python3 - <<'EOF'
import json
p='results/raw/FAIL_recovery_c100.json'; d=json.load(open(p)); d['summary']['events']=[{'t':40,'label':'sys3 killed'},{'t':90,'label':'sys3 restarted'}]; json.dump(d,open(p,'w'))
EOF
    curl -sS -m 5 "$LB_URL/lb/events" > evidence/05_failover_events.json
  fi
fi

# ── ALGO: dynamic vs static selection with one artificially loaded backend ──
if want ALGO; then
  log "#### ALGO — c=50, 3 backends, CPU hog on sys3: adaptive vs round_robin vs least_connections ####"
  set_n 3
  for rep in $(seq 1 "$REPS"); do
    for algo in adaptive round_robin least_connections; do
      set_algo "$algo"
      bash scripts/cpu_hog.sh sys3 start > /dev/null; sleep 6
      one_run "ALGO_${algo}_hog_c50_rep${rep}" ALGO "$algo+hog" "$rep" --concurrency 50 --duration "$DUR" --warmup "$WARM"
      bash scripts/cpu_hog.sh sys3 stop > /dev/null; sleep 4
    done
  done
  for algo in adaptive round_robin; do
    set_algo "$algo"
    one_run "ALGO_${algo}_nohog_c50_rep1" ALGO "$algo" 1 --concurrency 50 --duration "$DUR" --warmup "$WARM"
  done
  set_algo adaptive
fi

# ── L: response time vs load (closed loop), 1/2/3 backends interleaved ──────
if want L; then
  log "#### L — closed-loop concurrency sweep, interleaved 1/2/3 backends ####"
  set_algo adaptive
  for c in "${LEVELS[@]}"; do
    for rep in $(seq 1 "$REPS"); do
      order=$(python3 -c "import random; l=[1,2,3]; random.shuffle(l); print(' '.join(map(str,l)))")
      log "-- level c=$c rep=$rep order: $order"
      for n in $order; do
        [ -f "results/raw/L${n}_c${c}_rep${rep}.json" ] && continue
        set_n "$n"
        one_run "L${n}_c${c}_rep${rep}" "L$n" "$c" "$rep" --concurrency "$c" --duration "$DUR" --warmup "$WARM"
      done
    done
  done
fi

# ── O: throughput vs offered load (open loop, Poisson arrivals) ─────────────
if want O; then
  log "#### O — open-loop offered-load sweep, interleaved 1/2/3 backends ####"
  set_algo adaptive
  for r in "${RATES[@]}"; do
    order=$(python3 -c "import random; l=[1,2,3]; random.shuffle(l); print(' '.join(map(str,l)))")
    log "-- rate $r req/s order: $order"
    for n in $order; do
      [ -f "results/raw/O${n}_r${r}_rep1.json" ] && continue
      set_n "$n"
      one_run "O${n}_r${r}_rep1" "O$n" "$r" 1 --rate "$r" --concurrency 60 --duration 30 --warmup 5 --max-inflight 800
    done
  done
fi

set_n 3; set_algo adaptive
log "experiment target '$TARGET' complete — pool back to 3 backends, adaptive."

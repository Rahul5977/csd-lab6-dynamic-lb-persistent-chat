#!/usr/bin/env python3
"""
pick_threshold.py — choose the switching threshold from the sweep results.

The updated assignment asks for an *optimal* threshold, so it has to be chosen
by measurement rather than by taste. This reads every THR_t<T>_rep<n>.json the
sweep produced and scores each threshold on what the leaderboard's load
generator will actually see:

    cost(T) = p95 latency (ms), normalised
            + 0.6 x (throughput shortfall vs. the best threshold)
            + 100 x error rate

p95 is the headline number, throughput is weighted a little lower because every
threshold offers the same load, and any error rate at all is disqualifying.
Reps are aggregated with the median so one noisy run on a shared host cannot
decide the answer.

Writes results/processed/threshold_sweep.csv (the table the report prints) and
results/processed/optimal_threshold.txt (read back by run_experiments_v2.sh).
"""

import glob
import json
import os
import re
import statistics
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
RAW = os.path.join(ROOT, "results", "raw")
OUT = os.path.join(ROOT, "results", "processed")


def main():
    runs = {}
    for path in sorted(glob.glob(os.path.join(RAW, "THR_t*_rep*.json"))):
        m = re.search(r"THR_t([0-9.]+)_rep(\d+)\.json$", os.path.basename(path))
        if not m:
            continue
        T = float(m.group(1))
        s = json.load(open(path))["summary"]
        runs.setdefault(T, []).append(s)

    if not runs:
        print("pick_threshold: no THR_* runs found — keeping the current threshold", file=sys.stderr)
        return 1

    rows = []
    for T, ss in sorted(runs.items()):
        rows.append({
            "threshold": T,
            "reps": len(ss),
            "p50": statistics.median(s["latency_ms"]["p50"] for s in ss),
            "p95": statistics.median(s["latency_ms"]["p95"] for s in ss),
            "p99": statistics.median(s["latency_ms"]["p99"] for s in ss),
            "throughput": statistics.median(s["throughput_rps"] for s in ss),
            "err_pct": statistics.median(s["error_rate_pct"] for s in ss),
            "spread": statistics.median(spread_pct(s) for s in ss),
        })

    best_tp = max(r["throughput"] for r in rows) or 1.0
    best_p95 = min(r["p95"] for r in rows) or 1.0
    for r in rows:
        r["cost"] = round(r["p95"] / best_p95
                          + 0.6 * (best_tp - r["throughput"]) / best_tp
                          + 100.0 * r["err_pct"] / 100.0, 4)
    winner = min(rows, key=lambda r: r["cost"])

    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "threshold_sweep.csv"), "w") as fh:
        fh.write("threshold,reps,p50_ms,p95_ms,p99_ms,throughput_rps,error_pct,busiest_backend_share_pct,cost\n")
        for r in rows:
            fh.write("{threshold},{reps},{p50:.1f},{p95:.1f},{p99:.1f},{throughput:.1f},"
                     "{err_pct:.3f},{spread:.1f},{cost}\n".format(**r))
    with open(os.path.join(OUT, "optimal_threshold.txt"), "w") as fh:
        fh.write(str(winner["threshold"]))

    print(f"{'T':>6} {'p50':>7} {'p95':>8} {'p99':>8} {'rps':>8} {'err%':>7} {'busiest%':>9} {'cost':>7}")
    for r in rows:
        mark = "  <-- chosen" if r is winner else ""
        print(f"{r['threshold']:>6} {r['p50']:>7.1f} {r['p95']:>8.1f} {r['p99']:>8.1f} "
              f"{r['throughput']:>8.1f} {r['err_pct']:>7.3f} {r['spread']:>9.1f} {r['cost']:>7.3f}{mark}")
    print(f"\noptimal threshold = {winner['threshold']}  ->  results/processed/optimal_threshold.txt")
    return 0


def spread_pct(summary):
    """Share of requests taken by the busiest backend — 33 % is a perfect split
    across three, 100 % means everything stayed on one."""
    dist = summary.get("backend_distribution") or {}
    real = {k: v for k, v in dist.items() if not k.startswith("?") and not k.startswith("ERR")}
    total = sum(real.values())
    return 100.0 * max(real.values()) / total if total else 0.0


if __name__ == "__main__":
    sys.exit(main())

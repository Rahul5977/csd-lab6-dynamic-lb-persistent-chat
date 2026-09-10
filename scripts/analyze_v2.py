#!/usr/bin/env python3
"""
analyze_v2.py — tables and graphs for the UPDATED assignment.

Reads the runs produced by scripts/run_experiments_v2.sh (public /message and
/feed routes) plus the matching results/sysmetrics/<run_id>.csv utilisation
traces, and writes:

  results/processed/threshold_sweep.csv       chosen by scripts/pick_threshold.py
  results/processed/utilisation.csv           per-run, per-system CPU and memory
  report/tables/threshold_sweep.md            the optimal-threshold table
  report/tables/pub_response_time_vs_load.md  response time vs load, 1/2/3 backends
  report/tables/pub_throughput.md             throughput vs offered load
  report/tables/algorithm_comparison_v2.md    threshold vs the classic algorithms
  report/tables/system_utilisation.md         utilisation of all four systems
  results/charts/pub_response_time_vs_load.png
  results/charts/pub_throughput_vs_offered_load.png
  results/charts/threshold_sweep.png
  results/charts/algorithm_comparison_v2.png
  results/charts/system_utilisation_ramp.png
  results/charts/system_utilisation_vs_load.png
  results/charts/failover_public.png

Run:  .venv/bin/python scripts/analyze_v2.py
"""
import csv
import glob
import json
import os
import re
import statistics
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
RAW = os.path.join(ROOT, "results", "raw")
SYSM = os.path.join(ROOT, "results", "sysmetrics")
OUT = os.path.join(ROOT, "results", "processed")
CHARTS = os.path.join(ROOT, "results", "charts")
TABLES = os.path.join(ROOT, "report", "tables")
for d in (OUT, CHARTS, TABLES):
    os.makedirs(d, exist_ok=True)

SYSTEMS = ["sys1", "sys2", "sys3", "sys4"]
SYSROLE = {"sys1": "load balancer", "sys2": "backend", "sys3": "backend + database", "sys4": "backend"}
SYSCOL = {"sys1": "#7a4fd0", "sys2": "#4f6df5", "sys3": "#0f9d58", "sys4": "#d97706"}
NCOL = {1: "#d97706", 2: "#0f9d58", 3: "#4f6df5"}
NLAB = {1: "1 backend", 2: "2 backends", 3: "3 backends"}


def load(rid):
    path = os.path.join(RAW, rid + ".json")
    return json.load(open(path)) if os.path.exists(path) else None


def all_runs(pattern):
    out = {}
    for path in sorted(glob.glob(os.path.join(RAW, pattern))):
        rid = os.path.basename(path)[:-5]
        out[rid] = json.load(open(path))
    return out


def med(vals):
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else 0.0


def util(rid):
    """Per-system CPU/memory statistics for one run, from its sysmetrics trace."""
    path = os.path.join(SYSM, rid + ".csv")
    if not os.path.exists(path):
        return {}
    rows = list(csv.DictReader(open(path)))
    out = {}
    for s in SYSTEMS:
        cpu = [float(r["cpu_pct"]) for r in rows if r["system"] == s]
        mem = [float(r["mem_mb"]) for r in rows if r["system"] == s]
        if cpu:
            out[s] = {"cpu_mean": round(statistics.mean(cpu), 1), "cpu_max": round(max(cpu), 1),
                      "cpu_p95": round(sorted(cpu)[min(len(cpu) - 1, int(0.95 * len(cpu)))], 1),
                      "mem_mean": round(statistics.mean(mem), 1), "n": len(cpu)}
    return out


def util_series(rid):
    path = os.path.join(SYSM, rid + ".csv")
    if not os.path.exists(path):
        return {}
    rows = list(csv.DictReader(open(path)))
    out = {}
    for s in SYSTEMS:
        pts = [(float(r["t"]), float(r["cpu_pct"])) for r in rows if r["system"] == s]
        if pts:
            out[s] = ([p[0] for p in pts], [p[1] for p in pts])
    return out


def smooth(ys, k=5):
    if k <= 1 or len(ys) < k:
        return ys
    out = []
    for i in range(len(ys)):
        lo, hi = max(0, i - k // 2), min(len(ys), i + k // 2 + 1)
        out.append(sum(ys[lo:hi]) / (hi - lo))
    return out


def write_table(name, header, rows, caption=""):
    path = os.path.join(TABLES, name)
    with open(path, "w") as fh:
        if caption:
            fh.write(caption.rstrip() + "\n\n")
        fh.write("| " + " | ".join(header) + " |\n")
        fh.write("|" + "|".join("---" for _ in header) + "|\n")
        for r in rows:
            fh.write("| " + " | ".join(str(x) for x in r) + " |\n")
    print("  wrote", os.path.relpath(path, ROOT))


def busiest_share(summary):
    dist = {k: v for k, v in (summary.get("backend_distribution") or {}).items()
            if not k.startswith(("?", "ERR"))}
    tot = sum(dist.values())
    return round(100.0 * max(dist.values()) / tot, 1) if tot else 0.0


def share_str(summary):
    dist = {k: v for k, v in (summary.get("backend_distribution") or {}).items()
            if not k.startswith(("?", "ERR"))}
    tot = sum(dist.values()) or 1
    return " / ".join(f"{k} {100.0 * v / tot:.0f}%" for k, v in sorted(dist.items()))


# ═══════════════════════════ 1. threshold sweep ════════════════════════════
def collect_thr(prefix):
    out = {}
    for rid, d in all_runs(prefix + "_t*_rep*.json").items():
        m = re.match(re.escape(prefix) + r"_t([0-9.]+)_rep(\d+)$", rid)
        if m:
            out.setdefault(float(m.group(1)), []).append((rid, d["summary"]))
    return out


def thr_table(thr):
    rows = []
    for T, items in sorted(thr.items()):
        ss = [s for _, s in items]
        u = [util(rid) for rid, _ in items]
        rows.append({
            "T": T, "reps": len(ss),
            "p50": med([s["latency_ms"]["p50"] for s in ss]),
            "p95": med([s["latency_ms"]["p95"] for s in ss]),
            "p99": med([s["latency_ms"]["p99"] for s in ss]),
            "rps": med([s["throughput_rps"] for s in ss]),
            "err": med([s["error_rate_pct"] for s in ss]),
            "busiest": med([busiest_share(s) for s in ss]),
            "p95_lo": min(s["latency_ms"]["p95"] for s in ss),
            "p95_hi": max(s["latency_ms"]["p95"] for s in ss),
            "rps_lo": min(s["throughput_rps"] for s in ss),
            "rps_hi": max(s["throughput_rps"] for s in ss),
            "sys1_cpu": med([x.get("sys1", {}).get("cpu_mean") for x in u]),
            "backend_cpu": med([statistics.mean([x[s]["cpu_mean"] for s in ("sys2", "sys3", "sys4") if s in x])
                                for x in u if any(s in x for s in ("sys2", "sys3", "sys4"))]),
        })
    return rows


thr = collect_thr("THR")
thr_low = collect_thr("THRLOW")
thr_low_rows = thr_table(thr_low)

thr_rows = thr_table(thr)

optimal = None
opt_path = os.path.join(OUT, "optimal_threshold.txt")
if os.path.exists(opt_path):
    optimal = float(open(opt_path).read().strip())
elif thr_rows:
    optimal = min(thr_rows, key=lambda r: r["p95"])["T"]

if thr_rows:
    write_table("threshold_sweep.md",
                ["switch threshold T", "reps", "p50 (ms)", "p95 (ms)", "p99 (ms)", "throughput (req/s)",
                 "errors (%)", "busiest backend share", "sys1 CPU (%)", "backend CPU (%)"],
                [[f"**{r['T']:.2f}**" + (" ← chosen" if r["T"] == optimal else ""), r["reps"],
                  f"{r['p50']:.0f}", f"{r['p95']:.0f}", f"{r['p99']:.0f}", f"{r['rps']:.1f}",
                  f"{r['err']:.2f}", f"{r['busiest']:.0f} %", f"{r['sys1_cpu']:.0f}", f"{r['backend_cpu']:.0f}"]
                 for r in thr_rows],
                "Threshold sweep — 60 concurrent users on the public routes, 3 backends, "
                "median of the repetitions.")

if thr_low_rows:
    write_table("threshold_sweep_low.md",
                ["switch threshold T", "reps", "p50 (ms)", "p95 (ms)", "throughput (req/s)",
                 "busiest backend share", "sys1 CPU (%)", "backend CPU (%)"],
                [[f"**{r['T']:.2f}**" + (" ← chosen" if r["T"] == optimal else ""), r["reps"],
                  f"{r['p50']:.0f}", f"{r['p95']:.0f}", f"{r['rps']:.1f}",
                  f"{r['busiest']:.0f} %", f"{r['sys1_cpu']:.0f}", f"{r['backend_cpu']:.0f}"]
                 for r in thr_low_rows],
                "The same sweep at 10 concurrent clients. At this load the threshold really decides "
                "whether the cluster concentrates traffic on one warm backend or spreads it.")

# ═══════════════════ 2. response time vs load (public API) ═════════════════
pub = {}
for rid, d in all_runs("P[123]_c*_rep*.json").items():
    m = re.match(r"P(\d)_c(\d+)_rep(\d+)$", rid)
    if m:
        pub.setdefault((int(m.group(1)), int(m.group(2))), []).append((rid, d))

pub_rows = []
for (n, c), items in sorted(pub.items()):
    ss = [d["summary"] for _, d in items]
    u = [util(rid) for rid, _ in items]
    pub_rows.append({
        "n": n, "users": c,
        "rps": med([s["throughput_rps"] for s in ss]),
        "p50": med([s["latency_ms"]["p50"] for s in ss]),
        "p95": med([s["latency_ms"]["p95"] for s in ss]),
        "p99": med([s["latency_ms"]["p99"] for s in ss]),
        "err": med([s["error_rate_pct"] for s in ss]),
        "ok": sum(s["success"] for s in ss), "bad": sum(s["errors"] for s in ss),
        "share": share_str(ss[0]),
        "sys1": med([x.get("sys1", {}).get("cpu_mean") for x in u]),
        "sys2": med([x.get("sys2", {}).get("cpu_mean") for x in u]),
        "sys3": med([x.get("sys3", {}).get("cpu_mean") for x in u]),
        "sys4": med([x.get("sys4", {}).get("cpu_mean") for x in u]),
    })

if pub_rows:
    write_table("pub_response_time_vs_load.md",
                ["backends", "users", "throughput (req/s)", "p50 (ms)", "p95 (ms)", "p99 (ms)",
                 "successful", "failed", "errors (%)", "sys1 CPU", "sys2 CPU", "sys3 CPU", "sys4 CPU"],
                [[r["n"], r["users"], f"{r['rps']:.1f}", f"{r['p50']:.0f}", f"{r['p95']:.0f}", f"{r['p99']:.0f}",
                  r["ok"], r["bad"], f"{r['err']:.2f}",
                  f"{r['sys1']:.0f} %", f"{r['sys2']:.0f} %", f"{r['sys3']:.0f} %", f"{r['sys4']:.0f} %"]
                 for r in pub_rows],
                "Response time, throughput and per-system CPU against offered load on /message and /feed.")

# ═══════════════════ 3. throughput vs offered load (open loop) ═════════════
opn = {}
for rid, d in all_runs("PO[13]_r*_rep*.json").items():
    m = re.match(r"PO(\d)_r(\d+)_rep(\d+)$", rid)
    if m:
        opn.setdefault((int(m.group(1)), int(m.group(2))), []).append((rid, d))

opn_rows = []
for (n, rate), items in sorted(opn.items()):
    ss = [d["summary"] for _, d in items]
    opn_rows.append({
        "n": n, "offered": rate,
        "achieved": med([s["throughput_rps"] for s in ss]),
        "p95": med([s["latency_ms"]["p95"] for s in ss]),
        "err": med([s["error_rate_pct"] for s in ss]),
        "dropped": sum(s.get("dropped_arrivals", 0) for s in ss),
    })
if opn_rows:
    write_table("pub_throughput.md",
                ["backends", "offered load (req/s)", "achieved throughput (req/s)", "p95 (ms)",
                 "errors (%)", "arrivals dropped"],
                [[r["n"], r["offered"], f"{r['achieved']:.1f}", f"{r['p95']:.0f}", f"{r['err']:.2f}", r["dropped"]]
                 for r in opn_rows],
                "Open-loop throughput: Poisson arrivals at a fixed offered load, independent of how "
                "slow the answers get.")

# ═══════════════════ 4. algorithm comparison (public API) ══════════════════
algo = {}
for rid, d in all_runs("PALGO_*_rep*.json").items():
    m = re.match(r"PALGO_(.+)_rep(\d+)$", rid)
    if m:
        algo.setdefault(m.group(1), []).append((rid, d))

algo_rows = []
for name, items in sorted(algo.items()):
    ss = [d["summary"] for _, d in items]
    u = [util(rid) for rid, _ in items]
    per_backend = []
    for x in u:
        vals = [x[s]["cpu_mean"] for s in ("sys2", "sys3", "sys4") if s in x]
        if vals:
            per_backend.append(max(vals) - min(vals))
    algo_rows.append({
        "name": name, "reps": len(ss),
        "rps": med([s["throughput_rps"] for s in ss]),
        "p50": med([s["latency_ms"]["p50"] for s in ss]),
        "p95": med([s["latency_ms"]["p95"] for s in ss]),
        "p99": med([s["latency_ms"]["p99"] for s in ss]),
        "err": med([s["error_rate_pct"] for s in ss]),
        "busiest": med([busiest_share(s) for s in ss]),
        "cpu_spread": med(per_backend),
        "share": share_str(ss[0]),
    })
if algo_rows:
    algo_rows.sort(key=lambda r: r["p95"])
    write_table("algorithm_comparison_v2.md",
                ["algorithm", "reps", "throughput (req/s)", "p50 (ms)", "p95 (ms)", "p99 (ms)",
                 "errors (%)", "busiest backend", "backend CPU spread", "traffic split"],
                [[f"**{r['name']}**", r["reps"], f"{r['rps']:.1f}", f"{r['p50']:.0f}", f"{r['p95']:.0f}",
                  f"{r['p99']:.0f}", f"{r['err']:.2f}", f"{r['busiest']:.0f} %",
                  f"{r['cpu_spread']:.0f} pp", r["share"]] for r in algo_rows],
                "The chosen threshold rule against the classic algorithms, same load, same cluster.")

# ═══════════════════ 5. utilisation table across every run ═════════════════
util_rows = []
for path in sorted(glob.glob(os.path.join(SYSM, "*.csv"))):
    rid = os.path.basename(path)[:-4]
    u = util(rid)
    if not u:
        continue
    d = load(rid)
    s = d["summary"] if d else {}
    util_rows.append([rid, s.get("concurrency", "-"), f"{s.get('throughput_rps', 0):.1f}"] +
                     [f"{u[x]['cpu_mean']:.0f} / {u[x]['cpu_max']:.0f}" if x in u else "-" for x in SYSTEMS] +
                     [f"{u[x]['mem_mean']:.0f}" if x in u else "-" for x in SYSTEMS])
with open(os.path.join(OUT, "utilisation.csv"), "w", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["run_id", "users", "rps"] + [f"{s}_cpu_mean/max" for s in SYSTEMS] + [f"{s}_mem_mb" for s in SYSTEMS])
    w.writerows(util_rows)

if pub_rows:
    write_table("system_utilisation.md",
                ["backends", "users", "throughput (req/s)",
                 "sys1 — load balancer", "sys2 — backend", "sys3 — backend + DB", "sys4 — backend"],
                [[r["n"], r["users"], f"{r['rps']:.1f}",
                  f"{r['sys1']:.0f} %", f"{r['sys2']:.0f} %", f"{r['sys3']:.0f} %", f"{r['sys4']:.0f} %"]
                 for r in pub_rows if r["n"] == 3],
                "Mean CPU of each of the four systems while the offered load rises (3 backends). "
                "Each system is a container with a one-CPU quota, so 100 % is its whole allowance.")

# ═════════════════════════════ charts ══════════════════════════════════════
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except ImportError:
    print("matplotlib unavailable — charts skipped (use .venv/bin/python)")
    sys.exit(0)

# 5a. threshold sweep (both load levels), with the rep-to-rep range as whiskers
if thr_rows:
    fig, ax = plt.subplots(1, 3, figsize=(14.5, 4.2))
    xs = [r["T"] for r in thr_rows]
    ax[0].errorbar(xs, [r["p95"] for r in thr_rows],
                   yerr=[[r["p95"] - r["p95_lo"] for r in thr_rows], [r["p95_hi"] - r["p95"] for r in thr_rows]],
                   fmt="s-", color="#c62828", capsize=3, label="p95 (bars = min/max over reps)")
    ax[0].plot(xs, [r["p50"] for r in thr_rows], "o-", color="#0f9d58", label="p50")
    ax[0].set_ylim(0, max(r["p95_hi"] for r in thr_rows) * 1.15)
    ax[0].set_ylabel("response time (ms)")
    ax[0].set_title("Response time vs threshold — 60 clients", fontsize=10)

    ax[1].errorbar(xs, [r["rps"] for r in thr_rows],
                   yerr=[[r["rps"] - r["rps_lo"] for r in thr_rows], [r["rps_hi"] - r["rps"] for r in thr_rows]],
                   fmt="o-", color="#4f6df5", capsize=3, label="throughput")
    ax[1].set_ylim(0, max(r["rps_hi"] for r in thr_rows) * 1.2)
    ax[1].set_ylabel("throughput (successful req/s)")
    ax2 = ax[1].twinx()
    ax2.plot(xs, [r["busiest"] for r in thr_rows], "^--", color="#d97706", label="busiest backend share")
    ax2.set_ylabel("share of the busiest backend (%)"); ax2.set_ylim(0, 105)
    ax[1].set_title("Throughput and spread vs threshold — 60 clients", fontsize=10)

    if thr_low_rows:
        xl = [r["T"] for r in thr_low_rows]
        ax[2].plot(xl, [r["p95"] for r in thr_low_rows], "s-", color="#c62828", label="p95")
        ax[2].plot(xl, [r["p50"] for r in thr_low_rows], "o-", color="#0f9d58", label="p50")
        ax[2].set_ylim(0, max(r["p95"] for r in thr_low_rows) * 1.2)
        ax[2].set_ylabel("response time (ms)")
        axl = ax[2].twinx()
        axl.plot(xl, [r["busiest"] for r in thr_low_rows], "^--", color="#d97706", label="busiest backend share")
        axl.set_ylabel("share of the busiest backend (%)"); axl.set_ylim(0, 105)
        ax[2].set_title("Same sweep at 10 clients", fontsize=10)
        hl1, ll1 = ax[2].get_legend_handles_labels()
        hl2, ll2 = axl.get_legend_handles_labels()
        ax[2].legend(hl1 + hl2, ll1 + ll2, fontsize=7, loc="upper left")
    else:
        ax[2].axis("off")

    for a in ax[:2 if thr_low_rows else 1]:
        a.set_xlabel("switch threshold T (load index)")
    if thr_low_rows:
        ax[2].set_xlabel("switch threshold T (load index)")
    for a in ax:
        a.grid(alpha=.3)
        if optimal is not None and a.get_title():
            a.axvline(optimal, color="#555", ls=":", lw=1)
    ax[0].legend(fontsize=7, loc="lower left")
    h1, l1 = ax[1].get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax[1].legend(h1 + h2, l1 + l2, fontsize=7, loc="lower left")
    plt.tight_layout(); plt.savefig(os.path.join(CHARTS, "threshold_sweep.png"), dpi=150); plt.close()

# 5b. response time vs load, per backend count
if pub_rows:
    fig, ax = plt.subplots(1, 2, figsize=(11, 4.3))
    for n in (1, 2, 3):
        pts = sorted((r["users"], r["p50"], r["p95"]) for r in pub_rows if r["n"] == n)
        if not pts:
            continue
        ax[0].plot([p[0] for p in pts], [p[1] for p in pts], "o-", color=NCOL[n], label=NLAB[n])
        ax[1].plot([p[0] for p in pts], [p[2] for p in pts], "o-", color=NCOL[n], label=NLAB[n])
    for a, t in zip(ax, ("Median (p50) response time vs load", "p95 response time vs load")):
        a.set_xscale("log"); a.set_yscale("log")
        a.set_xlabel("offered load (concurrent clients)"); a.set_ylabel("response time (ms)")
        a.set_title(t + "\n/message + /feed through the load balancer", fontsize=10)
        a.grid(alpha=.3, which="both"); a.legend(fontsize=8)
    plt.tight_layout(); plt.savefig(os.path.join(CHARTS, "pub_response_time_vs_load.png"), dpi=150); plt.close()

    plt.figure(figsize=(7, 4.3))
    for n in (1, 2, 3):
        pts = sorted((r["users"], r["rps"]) for r in pub_rows if r["n"] == n)
        if pts:
            plt.plot([p[0] for p in pts], [p[1] for p in pts], "o-", color=NCOL[n], label=NLAB[n])
    plt.xscale("log"); plt.xlabel("offered load (concurrent clients)")
    plt.ylabel("throughput (successful req/s)")
    plt.title("Throughput vs load, and the effect of adding backends")
    plt.grid(alpha=.3); plt.legend(); plt.tight_layout()
    plt.savefig(os.path.join(CHARTS, "pub_throughput_vs_load.png"), dpi=150); plt.close()

# 5c. throughput vs offered load
if opn_rows:
    plt.figure(figsize=(7, 4.3))
    mx = max(r["offered"] for r in opn_rows)
    for n in (1, 3):
        pts = sorted((r["offered"], r["achieved"]) for r in opn_rows if r["n"] == n)
        if pts:
            plt.plot([p[0] for p in pts], [p[1] for p in pts], "o-", color=NCOL[n], label=NLAB[n])
    plt.plot([0, mx], [0, mx], "--", color="#999", lw=1, label="ideal (achieved = offered)")
    plt.xlabel("offered load (req/s)"); plt.ylabel("achieved throughput (req/s)")
    plt.title("Throughput vs offered load (open loop)")
    plt.grid(alpha=.3); plt.legend(fontsize=8); plt.tight_layout()
    plt.savefig(os.path.join(CHARTS, "pub_throughput_vs_offered_load.png"), dpi=150); plt.close()

# 5d. algorithm comparison
if algo_rows:
    fig, ax = plt.subplots(1, 2, figsize=(11, 4.2))
    names = [r["name"] for r in algo_rows]
    x = range(len(names))
    ax[0].bar([i - 0.2 for i in x], [r["p50"] for r in algo_rows], 0.4, color="#0f9d58", label="p50")
    ax[0].bar([i + 0.2 for i in x], [r["p95"] for r in algo_rows], 0.4, color="#c62828", label="p95")
    ax[0].set_ylabel("response time (ms)"); ax[0].set_title("Response time by algorithm")
    ax[1].bar(list(x), [r["rps"] for r in algo_rows], 0.5, color="#4f6df5")
    ax[1].set_ylabel("throughput (req/s)"); ax[1].set_title("Throughput by algorithm")
    for a in ax:
        a.set_xticks(list(x)); a.set_xticklabels(names, rotation=20, ha="right", fontsize=8)
        a.grid(alpha=.3, axis="y")
    ax[0].legend(fontsize=8)
    plt.tight_layout(); plt.savefig(os.path.join(CHARTS, "algorithm_comparison_v2.png"), dpi=150); plt.close()

# 5e. utilisation of all four systems during the ramp
ramp = load("PUTIL_ramp")
us = util_series("PUTIL_ramp")
if ramp and us:
    ts = ramp["timeseries_1s"]
    fig, ax = plt.subplots(2, 1, figsize=(11, 6.6), sharex=True,
                           gridspec_kw={"height_ratios": [2, 1]})
    for s in SYSTEMS:
        if s not in us:
            continue
        x, y = us[s]
        ax[0].plot(x, smooth(y, 5), color=SYSCOL[s], lw=1.6, label=f"{s} — {SYSROLE[s]}")
    ax[0].axhline(100, color="#c62828", ls=":", lw=1)
    ax[0].text(1, 101, "one full CPU (the container's quota)", fontsize=7, color="#c62828")
    ax[0].set_ylabel("CPU utilisation (% of the system's 1-CPU quota)")
    ax[0].set_title("Utilisation of all four systems while the offered load rises", fontsize=11)
    ax[0].grid(alpha=.3); ax[0].legend(fontsize=8, ncol=2); ax[0].set_ylim(0, 115)

    ax[1].plot([p["t"] for p in ts], [p.get("users", 0) for p in ts], color="#333", lw=1.4, label="virtual users")
    ax[1].set_ylabel("clients")
    axb = ax[1].twinx()
    axb.plot([p["t"] for p in ts], smooth([p["p95"] for p in ts], 5), color="#c62828", lw=1.2, label="p95 response time")
    axb.set_ylabel("p95 response time (ms)", color="#c62828")
    ax[1].set_xlabel("time (s)"); ax[1].grid(alpha=.3)
    h1, l1 = ax[1].get_legend_handles_labels()
    h2, l2 = axb.get_legend_handles_labels()
    ax[1].legend(h1 + h2, l1 + l2, fontsize=8, loc="upper left")
    plt.tight_layout(); plt.savefig(os.path.join(CHARTS, "system_utilisation_ramp.png"), dpi=150); plt.close()

# 5f. utilisation vs load, all four systems
three = [r for r in pub_rows if r["n"] == 3]
if three:
    plt.figure(figsize=(7.5, 4.3))
    xs = [r["users"] for r in sorted(three, key=lambda r: r["users"])]
    for s in SYSTEMS:
        ys = [r[s] for r in sorted(three, key=lambda r: r["users"])]
        plt.plot(xs, ys, "o-", color=SYSCOL[s], label=f"{s} — {SYSROLE[s]}")
    plt.axhline(100, color="#c62828", ls=":", lw=1)
    plt.xscale("log"); plt.xlabel("offered load (concurrent clients)")
    plt.ylabel("mean CPU utilisation (% of a 1-CPU quota)")
    plt.title("System utilisation vs offered load, all four systems")
    plt.grid(alpha=.3, which="both"); plt.legend(fontsize=8); plt.tight_layout()
    plt.savefig(os.path.join(CHARTS, "system_utilisation_vs_load.png"), dpi=150); plt.close()

# 5g. failover on the public routes
fail = load("PFAIL_recovery")
if fail:
    ts = fail["timeseries_1s"]
    t = [p["t"] for p in ts]
    fig, ax = plt.subplots(3, 1, figsize=(11, 8), sharex=True,
                           gridspec_kw={"height_ratios": [2, 1, 1]})
    for b in ("sys2", "sys3", "sys4"):
        ax[0].plot(t, [p["by_backend"].get(b, 0) for p in ts], color=SYSCOL[b], lw=1.3, label=b)
    ax[0].set_ylabel("requests served per second")
    ax[0].set_title("Backend failure and recovery on /message and /feed", fontsize=11)
    ax[0].grid(alpha=.3); ax[0].legend(fontsize=8)

    ax[1].plot(t, smooth([p["p95"] for p in ts], 3), color="#c62828", lw=1.3, label="p95")
    ax[1].plot(t, smooth([p["p50"] for p in ts], 3), color="#0f9d58", lw=1.3, label="p50")
    ax[1].set_ylabel("response time (ms)"); ax[1].grid(alpha=.3); ax[1].legend(fontsize=8)

    ax[2].plot(t, [p["active_backends"] for p in ts], color="#4f6df5", lw=1.6, label="active backends")
    ax[2].plot(t, [p["errors"] for p in ts], color="#c62828", lw=1.3, label="failed requests/s")
    ax[2].set_ylabel("count"); ax[2].set_xlabel("time (s)")
    ax[2].grid(alpha=.3); ax[2].legend(fontsize=8)
    for a in ax:
        a.axvline(45, color="#555", ls="--", lw=1)
        a.axvline(90, color="#555", ls="--", lw=1)
    ax[0].annotate("sys3 killed", xy=(45, ax[0].get_ylim()[1] * 0.92), fontsize=8, color="#555")
    ax[0].annotate("sys3 restarted", xy=(90, ax[0].get_ylim()[1] * 0.92), fontsize=8, color="#555")
    plt.tight_layout(); plt.savefig(os.path.join(CHARTS, "failover_public.png"), dpi=150); plt.close()

print("charts written to", os.path.relpath(CHARTS, ROOT))
if optimal is not None:
    print("optimal switching threshold:", optimal)

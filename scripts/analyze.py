#!/usr/bin/env python3
"""
analyze.py — turn results/raw/*.json into the report's tables and graphs.

  results/processed/summary.csv        one row per run
  results/processed/medians.csv        median over reps per (config, param)
  report/tables/*.md                   comparison tables (markdown)
  results/charts/*.png                 graphs (matplotlib)

Every number traces to a run id; nothing is typed in by hand.
Run:  .venv/bin/python scripts/analyze.py
"""
import csv
import glob
import json
import os
import statistics
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
RAW = os.path.join(ROOT, "results", "raw")
OUT = os.path.join(ROOT, "results", "processed")
CHARTS = os.path.join(ROOT, "results", "charts")
TABLES = os.path.join(ROOT, "report", "tables")
for d in (OUT, CHARTS, TABLES):
    os.makedirs(d, exist_ok=True)

runs, full = [], {}
for path in sorted(glob.glob(os.path.join(RAW, "*.json"))):
    with open(path) as fh:
        d = json.load(fh)
    s = d["summary"]
    rid = s["run_id"]
    if rid.startswith(("local_", "smoke_", "calibration")):
        continue
    cfg = rid.split("_")[0]                         # L1 / O3 / SCALE / FAIL / ALGO
    param = None
    if cfg.startswith("L"):
        param = s["concurrency"]
    elif cfg.startswith("O"):
        param = s["rate"]
    elif cfg == "ALGO":
        param = "_".join(rid.split("_")[1:-2])      # adaptive_hog / round_robin_nohog ...
    full[rid] = d
    runs.append({
        "run_id": rid, "config": cfg, "param": param, "mode": s["mode"],
        "concurrency": s["concurrency"], "rate": s["rate"],
        "total": s["total_requests"], "success": s["success"], "errors": s["errors"],
        "error_pct": s["error_rate_pct"], "rps": s["throughput_rps"],
        "mean": s["latency_ms"]["mean"], "p50": s["latency_ms"]["p50"], "p90": s["latency_ms"]["p90"],
        "p95": s["latency_ms"]["p95"], "p99": s["latency_ms"]["p99"], "max": s["latency_ms"]["max"],
        "dist": s["backend_distribution"], "active_min": s["active_backends_min"], "active_max": s["active_backends_max"],
        "retried": s.get("sends_retried", 0), "dups": s.get("duplicates_suppressed", 0),
        "dropped": s.get("dropped_arrivals", 0),
        "offered_actual": round(s["total_requests"] / max(1, s["duration_s"] - s["warmup_s"]), 1),
    })
if not runs:
    sys.exit("no experiment runs in results/raw/ yet")

with open(os.path.join(OUT, "summary.csv"), "w", newline="") as fh:
    w = csv.writer(fh)
    keys = ["run_id", "config", "param", "mode", "concurrency", "rate", "total", "success", "errors", "error_pct",
            "rps", "mean", "p50", "p90", "p95", "p99", "max", "active_min", "active_max", "retried", "dups", "dropped"]
    w.writerow(keys + ["distribution"])
    for r in runs:
        w.writerow([r[k] for k in keys] + [json.dumps(r["dist"])])

# ── medians over repetitions ────────────────────────────────────────────────
groups = {}
for r in runs:
    groups.setdefault((r["config"], str(r["param"])), []).append(r)
med_rows = []
for (cfg, param), rs in groups.items():
    med = lambda k: round(statistics.median(x[k] for x in rs), 2)
    dist = {}
    for r in rs:
        for b, n in r["dist"].items():
            dist[b] = dist.get(b, 0) + n
    tot = sum(dist.values()) or 1
    med_rows.append({
        "config": cfg, "param": param, "reps": len(rs), "total": med("total"), "success": med("success"),
        "errors": med("errors"), "error_pct": med("error_pct"), "rps": med("rps"), "mean": med("mean"),
        "p50": med("p50"), "p90": med("p90"), "p95": med("p95"), "p99": med("p99"), "max": med("max"),
        "active": f"{min(x['active_min'] for x in rs)}-{max(x['active_max'] for x in rs)}",
        "retried": sum(x["retried"] for x in rs), "dups": sum(x["dups"] for x in rs),
        "dropped": sum(x["dropped"] for x in rs),
        "offered_actual": med("offered_actual"),
        "dist_pct": {b: round(n / tot * 100, 1) for b, n in sorted(dist.items())},
        "run_ids": ",".join(x["run_id"] for x in rs),
    })
with open(os.path.join(OUT, "medians.csv"), "w", newline="") as fh:
    hdr = ["config", "param", "reps", "total", "success", "errors", "error_pct", "rps", "mean", "p50", "p90", "p95",
           "p99", "max", "active", "retried", "dups", "dropped", "dist_pct", "run_ids"]
    w = csv.writer(fh); w.writerow(hdr)
    for m in med_rows:
        w.writerow([json.dumps(m[k]) if k == "dist_pct" else m[k] for k in hdr])


def row(cfg, param):
    return next((m for m in med_rows if m["config"] == cfg and m["param"] == str(param)), None)


def dist_str(m):
    return " / ".join(f"{k}:{v}%" for k, v in m["dist_pct"].items() if not k.startswith("?"))


def detect_events(d):
    """Actual membership-change times from the sampled active-backend count
    (the scripted times are offset by the load generator's setup phase)."""
    ts = d["timeseries_1s"]; labels = [e["label"] for e in d["summary"].get("events", [])]
    out, prev = [], None
    for x in ts:
        a = x["active_backends"]
        if a < 0:
            continue
        if prev is not None and a != prev:
            out.append({"t": x["t"], "label": labels[len(out)] if len(out) < len(labels) else f"active {prev}->{a}", "active": a})
        prev = a
    return out



# ── Table 1: response time vs load, 1/2/3 backends ─────────────────────────
levels = sorted({int(m["param"]) for m in med_rows if m["config"] in ("L1", "L2", "L3")})
lines = ["| Concurrency | Backends | Total | Success | Failed | Err % | Throughput (req/s) | Mean ms | p50 | p90 | p95 | p99 | Active | Distribution | Speed-up vs 1 |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
for c in levels:
    base = row("L1", c)
    for n in (1, 2, 3):
        m = row(f"L{n}", c)
        if not m:
            continue
        sp = f"{m['rps'] / base['rps']:.2f}×" if base and base["rps"] and n > 1 else "—"
        lines.append(f"| {c} | {n} | {m['total']:.0f} | {m['success']:.0f} | {m['errors']:.0f} | {m['error_pct']} | **{m['rps']}** | {m['mean']} | {m['p50']} | {m['p90']} | {m['p95']} | {m['p99']} | {m['active']} | {dist_str(m)} | {sp} |")
open(os.path.join(TABLES, "response_time_vs_load.md"), "w").write("\n".join(lines) + "\n")

# ── Table 2: throughput vs offered load ─────────────────────────────────────
rates = sorted({float(m["param"]) for m in med_rows if m["config"] in ("O1", "O2", "O3")})
lines = ["| Offered load nominal → actual (req/s) | Backends | Achieved (req/s) | Success | Failed (of which dropped) | Err % | p50 ms | p95 ms | p99 ms | Active | Distribution |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
for r_ in rates:
    for n in (1, 2, 3):
        m = row(f"O{n}", r_)
        if not m:
            continue
        lines.append(f"| {r_:g} → {m['offered_actual']} | {n} | **{m['rps']}** | {m['success']:.0f} | {m['errors']:.0f} ({m['dropped']}) | {m['error_pct']} | {m['p50']} | {m['p95']} | {m['p99']} | {m['active']} | {dist_str(m)} |")
lines.append("\n*Actual offered load = arrivals the generator really issued per second (successful + failed + dropped). The thread-per-arrival Python generator on the Mac saturates near 230 arrivals/s, so the nominal 300 req/s setting produced ~220–230 req/s of real offered load.*")
open(os.path.join(TABLES, "throughput_vs_offered_load.md"), "w").write("\n".join(lines) + "\n")

# ── Table 3: scaling phases (from the SCALE timeline) ──────────────────────
def phase_table(rid, phases, extra_cols=()):
    if rid not in full:
        return "*(run pending)*", []
    ts = full[rid]["timeseries_1s"]
    lines = ["| Phase | Window (s) | Users | Active backends | Throughput (req/s) | p50 ms | p95 ms | Errors | Share sys2 / sys3 / sys4 |",
             "|---|---|---|---|---|---|---|---|---|"]
    rows = []
    for name, (a, b) in phases:
        seg = [t for t in ts if a <= t["t"] < b]
        if not seg:
            continue
        ok = sum(t["ok"] for t in seg); err = sum(t["errors"] for t in seg)
        lat = sorted(x for t in seg for x in [t["p50"]] if t["ok"])
        p50 = statistics.median([t["p50"] for t in seg if t["ok"]]) if any(t["ok"] for t in seg) else 0
        p95 = statistics.median([t["p95"] for t in seg if t["ok"]]) if any(t["ok"] for t in seg) else 0
        by = {}
        for t in seg:
            for k, v in t["by_backend"].items():
                by[k] = by.get(k, 0) + v
        tot = sum(by.values()) or 1
        share = " / ".join(f"{by.get(k, 0) / tot * 100:.0f}%" for k in ("sys2", "sys3", "sys4"))
        active = statistics.median([t["active_backends"] for t in seg if t["active_backends"] > 0] or [0])
        users = seg[len(seg) // 2]["users"]
        rows.append({"phase": name, "rps": ok / len(seg), "p50": p50, "p95": p95, "active": active})
        lines.append(f"| {name} | {a}–{b} | {users} | {active:.0f} | **{ok / len(seg):.1f}** | {p50:.0f} | {p95:.0f} | {err} | {share} |")
    return "\n".join(lines) + "\n", rows

def ev_times(rid, n):
    if rid not in full:
        return [0] * n
    t = [e["t"] for e in detect_events(full[rid])]
    return (t + [0] * n)[:n]

t3, t4 = ev_times("SCALE_ramp", 2)
scale_phases = [("A: 1 backend, 25 users", (5, 40)), ("B: 1 backend, 50 users", (45, 80)), ("C: 1 backend, 100 users (saturated)", (85, t3 or 140)),
                ("D: sys3 added → 2 backends", (t3 + 8, t4 or 200)), ("E: sys4 added → 3 backends", (t4 + 8, 260)), ("F: 3 backends, 200 users", (265, 300))]
tbl, scale_rows = phase_table("SCALE_ramp", scale_phases)
open(os.path.join(TABLES, "scaling_phases.md"), "w").write(tbl)

tk, tr = ev_times("FAIL_recovery_c100", 2)
tk = tk or 40; tr = tr or 90
fail_phases = [("healthy, 3 backends", (5, tk - 8)), ("sys3 killed → detection window", (tk - 8, tk + 2)), ("2 backends carry the load", (tk + 2, tr - 2)),
               ("sys3 restarted → re-admitted", (tr - 2, tr + 8)), ("3 backends again", (tr + 8, 150))]
tbl, _ = phase_table("FAIL_recovery_c100", fail_phases)
if "FAIL_recovery_c100" in full:
    s = full["FAIL_recovery_c100"]["summary"]
    tbl += f"\nWhole run: {s['total_requests']} requests, {s['errors']} failed ({s['error_rate_pct']} %), {s['sends_retried']} sends retried with the same id, {s['duplicates_suppressed']} of those answered `duplicate:true` (stored once).\n"
open(os.path.join(TABLES, "failover_phases.md"), "w").write(tbl)

# ── Table 4: algorithm comparison with a loaded backend ────────────────────
lines = ["| Algorithm | sys3 CPU hog | Throughput (req/s) | p50 ms | p95 ms | p99 ms | Errors | Share sys2 / sys3 / sys4 |",
         "|---|---|---|---|---|---|---|---|"]
for param in ("adaptive_hog", "round_robin_hog", "least_connections_hog", "adaptive_nohog", "round_robin_nohog"):
    m = row("ALGO", param)
    if not m:
        continue
    algo, hog = param.rsplit("_", 1)
    d = m["dist_pct"]
    lines.append(f"| {algo} | {'yes' if hog == 'hog' else 'no'} | **{m['rps']}** | {m['p50']} | {m['p95']} | {m['p99']} | {m['errors']:.0f} | {d.get('sys2', 0)}% / **{d.get('sys3', 0)}%** / {d.get('sys4', 0)}% |")
open(os.path.join(TABLES, "algorithm_comparison.md"), "w").write("\n".join(lines) + "\n")

# ── summary table: the headline numbers side by side ───────────────────────
lines = ["| Scenario | Configuration | Throughput (req/s) | p50 ms | p95 ms | Failed | Active backends |",
         "|---|---|---|---|---|---|---|"]
for c in (100, 200):
    for n in (1, 2, 3):
        m = row(f"L{n}", c)
        if m:
            lines.append(f"| L: {c} users, closed loop | {n} backend(s) | **{m['rps']}** | {m['p50']} | {m['p95']} | {m['errors']:.0f} ({m['error_pct']} %) | {m['active']} |")
for r_ in (300.0,):
    for n in (1, 2, 3):
        m = row(f"O{n}", r_)
        if m:
            lines.append(f"| O: {m['offered_actual']} req/s actually offered | {n} backend(s) | **{m['rps']}** achieved | {m['p50']} | {m['p95']} | {m['errors']:.0f} failed (of which {m['dropped']} dropped) | {m['active']} |")
for r in scale_rows:
    lines.append(f"| SCALE phase {r['phase']} | {r['active']:.0f} backend(s) | **{r['rps']:.1f}** | {r['p50']:.0f} | {r['p95']:.0f} | — | {r['active']:.0f} |")
for param in ("adaptive_hog", "round_robin_hog", "least_connections_hog"):
    m = row("ALGO", param)
    if m:
        lines.append(f"| ALGO: 50 users, sys3 CPU-loaded | {param.replace('_hog', '')} | **{m['rps']}** | {m['p50']} | {m['p95']} | {m['errors']:.0f} | share sys3 = {m['dist_pct'].get('sys3', 0)} % |")
if "FAIL_recovery_c100" in full:
    s_ = full["FAIL_recovery_c100"]["summary"]
    lines.append(f"| FAIL: 100 users, sys3 killed + restarted | 3 → 2 → 3 backends | **{s_['throughput_rps']}** (whole run) | {s_['latency_ms']['p50']} | {s_['latency_ms']['p95']} | {s_['errors']} ({s_['error_rate_pct']} %) | 2–3 |")
open(os.path.join(TABLES, "summary.md"), "w").write("\n".join(lines) + "\n")

# ── dedup table ─────────────────────────────────────────────────────────────
dd = os.path.join(ROOT, "results", "dedup_test.json")
if os.path.exists(dd):
    d = json.load(open(dd))
    lines = ["| Test | Scenario | Sent | Stored in DB | duplicate:true responses | Backends involved | Result |", "|---|---|---|---|---|---|---|"]
    names = {"T1": "same id, sequential retries", "T2": "same id, concurrent storm across backends", "T3": "send, drop connection, reconnect, re-send",
             "T4": "Idempotency-Key header", "T5": "control: distinct ids", "P1": "persistence across DB-service restart"}
    for r in d["results"]:
        note = r['note'].split(':', 1)[1].strip() if r['test'] == 'P1' and ':' in r['note'] else ''
        lines.append(f"| {r['test']} | {names.get(r['test'], '')} | {r['sent']} | **{r['stored']}** | {r['duplicate_responses']} | {', '.join(r['backends']) or 'via LB'} | {r['verdict']} {note} |")
    lines.append(f"\nDatabase after the test: {d['db_stats'].get('messages')} messages, {d['db_stats'].get('duplicates_rejected_total')} duplicates rejected in total ({d['db_stats'].get('engine')}).")
    open(os.path.join(TABLES, "dedup.md"), "w").write("\n".join(lines) + "\n")

print(f"tables + csv written ({len(runs)} runs, {len(med_rows)} groups)")

# ── charts ──────────────────────────────────────────────────────────────────
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except ImportError:
    print("matplotlib unavailable — charts skipped (use .venv/bin/python)")
    sys.exit(0)

COL = {1: "#d97706", 2: "#0f9d58", 3: "#4f6df5"}
LAB = {1: "1 backend (sys2)", 2: "2 backends (sys2+sys3)", 3: "3 backends (sys2+sys3+sys4)"}
BCOL = {"sys2": "#4f6df5", "sys3": "#0f9d58", "sys4": "#d97706"}


def series(cfg, key, xs_key="param", numeric=float):
    pts = sorted((numeric(m["param"]), m[key]) for m in med_rows if m["config"] == cfg)
    return [p[0] for p in pts], [p[1] for p in pts]


# 1. response time vs load
if levels:
    fig, ax = plt.subplots(1, 2, figsize=(11, 4.3))
    for n in (1, 2, 3):
        x, y = series(f"L{n}", "p50")
        if x: ax[0].plot(x, y, "o-", color=COL[n], label=LAB[n])
        x, y = series(f"L{n}", "p95")
        if x: ax[1].plot(x, y, "o-", color=COL[n], label=LAB[n])
    for a, t in zip(ax, ("Median (p50) response time vs load", "p95 response time vs load")):
        a.set_xscale("log"); a.set_yscale("log"); a.set_xlabel("load (concurrent virtual users)"); a.set_ylabel("response time (ms)")
        a.set_title(t); a.grid(alpha=.3, which="both"); a.legend(fontsize=8)
    plt.tight_layout(); plt.savefig(os.path.join(CHARTS, "response_time_vs_load.png"), dpi=150); plt.close()

    plt.figure(figsize=(7, 4.3))
    for n in (1, 2, 3):
        x, y = series(f"L{n}", "rps")
        if x: plt.plot(x, y, "o-", color=COL[n], label=LAB[n])
    plt.xscale("log"); plt.xlabel("load (concurrent virtual users)"); plt.ylabel("throughput (successful req/s)")
    plt.title("Throughput vs load (closed loop)"); plt.grid(alpha=.3); plt.legend(); plt.tight_layout()
    plt.savefig(os.path.join(CHARTS, "throughput_vs_load.png"), dpi=150); plt.close()

# 2. throughput vs offered load (open loop)
if rates:
    fig, ax = plt.subplots(1, 2, figsize=(11, 4.3))
    def oseries(cfg, key):
        pts = sorted((m["offered_actual"], m[key]) for m in med_rows if m["config"] == cfg)
        return [p[0] for p in pts], [p[1] for p in pts]
    mx = 0
    for n in (1, 2, 3):
        x, y = oseries(f"O{n}", "rps")
        if x: ax[0].plot(x, y, "o-", color=COL[n], label=LAB[n]); mx = max(mx, max(x))
        x, y = oseries(f"O{n}", "p95")
        if x: ax[1].plot(x, y, "o-", color=COL[n], label=LAB[n])
    ax[0].plot([0, mx], [0, mx], "--", color="#999", label="ideal (achieved = offered)")
    ax[0].set_xlabel("actual offered load (req/s, Poisson arrivals)"); ax[0].set_ylabel("achieved throughput (req/s)")
    ax[0].set_title("Throughput vs offered load"); ax[0].grid(alpha=.3); ax[0].legend(fontsize=8)
    ax[1].set_xlabel("actual offered load (req/s)"); ax[1].set_ylabel("p95 response time (ms)"); ax[1].set_yscale("log")
    ax[1].set_title("p95 response time vs offered load"); ax[1].grid(alpha=.3, which="both"); ax[1].legend(fontsize=8)
    plt.tight_layout(); plt.savefig(os.path.join(CHARTS, "throughput_vs_offered_load.png"), dpi=150); plt.close()


def timeline(rid, fname, title, smooth=5):
    if rid not in full:
        return
    d = full[rid]; ts = d["timeseries_1s"]; ev = detect_events(d)
    t = [x["t"] for x in ts]
    def sm(vals):
        out = []
        for i in range(len(vals)):
            w = [v for v in vals[max(0, i - smooth + 1):i + 1] if v is not None]
            out.append(sum(w) / len(w) if w else 0)
        return out
    fig, ax = plt.subplots(4, 1, figsize=(10, 10), sharex=True, gridspec_kw={"height_ratios": [2, 2, 1.2, 1.4]})
    ax[0].plot(t, sm([x["ok"] for x in ts]), color="#4f6df5", label="successful req/s (5 s avg)")
    ax[0].plot(t, [x["errors"] for x in ts], color="#c62828", alpha=.8, label="failed req/s")
    ax[0].set_ylabel("req / s"); ax[0].legend(loc="upper left", fontsize=8); ax[0].grid(alpha=.3)
    ax[1].plot(t, sm([x["p50"] or None for x in ts]), color="#0f9d58", label="p50 (5 s avg)")
    ax[1].plot(t, sm([x["p95"] or None for x in ts]), color="#d97706", label="p95 (5 s avg)")
    ax[1].set_ylabel("response time (ms)"); ax[1].set_yscale("log"); ax[1].legend(loc="upper left", fontsize=8); ax[1].grid(alpha=.3, which="both")
    ax[2].step(t, [x["active_backends"] if x["active_backends"] >= 0 else 0 for x in ts], where="post", color="#111", label="active backends (LB /lb/stats)")
    ax[2].plot(t, [x["users"] / max(1, max(y["users"] for y in ts)) * 3 for x in ts], ":", color="#888", label="offered load (users, scaled)")
    ax[2].set_ylabel("count"); ax[2].set_ylim(0, 3.5); ax[2].set_yticks([0, 1, 2, 3]); ax[2].legend(loc="upper left", fontsize=8); ax[2].grid(alpha=.3)
    bottoms = [0] * len(ts)
    for b in ("sys2", "sys3", "sys4"):
        vals = [x["by_backend"].get(b, 0) / max(1, x["requests"]) * 100 for x in ts]
        ax[3].bar(t, vals, bottom=bottoms, width=1, color=BCOL[b], label=b)
        bottoms = [a + v for a, v in zip(bottoms, vals)]
    ax[3].set_ylabel("share %"); ax[3].set_ylim(0, 100); ax[3].legend(loc="upper left", fontsize=8, ncol=3); ax[3].set_xlabel("time (s)")
    for e in ev:
        for a in ax:
            a.axvline(e["t"], color="#c62828", ls="--", alpha=.7)
        ax[0].text(e["t"] + 1, ax[0].get_ylim()[1] * .92, e["label"], color="#c62828", fontsize=9)
    ax[0].set_title(title)
    plt.tight_layout(); plt.savefig(os.path.join(CHARTS, fname), dpi=140); plt.close()


timeline("SCALE_ramp", "scaling_timeline.png", "Dynamic scaling — rising load; sys3 then sys4 started while running (dashed = admitted by the LB)")
timeline("FAIL_recovery_c100", "failover_timeline.png", "Failure and recovery — sys3 SIGKILLed then restarted under 100 users (dashed = LB ejected / re-admitted)")

# 3. scaling phases bar chart (effect of adding each backend)
if scale_rows:
    fig, ax1 = plt.subplots(figsize=(9, 4.3))
    names = [r["phase"].split(":")[0] + "\n" + "\n".join(r["phase"].split(":")[1].strip().replace(" (saturated)", "\n(saturated)").split(", ")) for r in scale_rows]
    xs = range(len(scale_rows))
    ax1.bar([x - 0.2 for x in xs], [r["rps"] for r in scale_rows], width=0.4, color="#4f6df5", label="throughput (req/s)")
    ax1.set_ylabel("throughput (req/s)")
    ax2 = ax1.twinx()
    ax2.plot(list(xs), [r["p95"] for r in scale_rows], "o-", color="#d97706", label="p95 ms")
    ax2.plot(list(xs), [r["p50"] for r in scale_rows], "s--", color="#0f9d58", label="p50 ms")
    ax2.set_ylabel("response time (ms)"); ax2.set_yscale("log")
    ax1.set_xticks(list(xs)); ax1.set_xticklabels(names, fontsize=7)
    ax1.set_title("Effect of adding backends (phases of the scaling run)")
    h1, l1 = ax1.get_legend_handles_labels(); h2, l2 = ax2.get_legend_handles_labels()
    ax1.legend(h1 + h2, l1 + l2, fontsize=8, loc="upper left"); ax1.grid(alpha=.3, axis="y")
    plt.tight_layout(); plt.savefig(os.path.join(CHARTS, "scaling_effect.png"), dpi=150); plt.close()

# 4. algorithm comparison with a loaded backend
algo_rows = [(p, row("ALGO", p)) for p in ("adaptive_hog", "round_robin_hog", "least_connections_hog", "adaptive_nohog", "round_robin_nohog")]
algo_rows = [(p, m) for p, m in algo_rows if m]
if algo_rows:
    fig, ax = plt.subplots(1, 2, figsize=(11, 4.3))
    names = [p.replace("_hog", "\n(sys3 loaded)").replace("_nohog", "\n(no hog)") for p, _ in algo_rows]
    bottoms = [0] * len(algo_rows)
    for b in ("sys2", "sys3", "sys4"):
        vals = [m["dist_pct"].get(b, 0) for _, m in algo_rows]
        ax[0].bar(names, vals, bottom=bottoms, color=BCOL[b], label=b)
        bottoms = [a + v for a, v in zip(bottoms, vals)]
    ax[0].set_ylabel("share of requests (%)"); ax[0].set_title("Where the traffic went"); ax[0].legend(fontsize=8); ax[0].tick_params(axis="x", labelsize=8)
    ax[1].bar(names, [m["rps"] for _, m in algo_rows], color="#4f6df5")
    ax1b = ax[1].twinx(); ax1b.plot(names, [m["p95"] for _, m in algo_rows], "o-", color="#d97706", label="p95 ms"); ax1b.set_ylabel("p95 (ms)")
    ax[1].set_ylabel("throughput (req/s)"); ax[1].set_title("Throughput (bars) and p95 (line), c = 50"); ax[1].tick_params(axis="x", labelsize=8); ax1b.legend(fontsize=8)
    plt.tight_layout(); plt.savefig(os.path.join(CHARTS, "algorithm_comparison.png"), dpi=150); plt.close()

print("charts written to results/charts/")

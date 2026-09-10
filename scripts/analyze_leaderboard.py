#!/usr/bin/env python3
"""
analyze_leaderboard.py — tables and charts for the evaluation-load work.

Reads the runs of loadgen/leaderboard_sim.py in results/leaderboard/ and writes:

  report/tables/leaderboard_ladders.md   the two boards, stage by stage
  report/tables/leaderboard_steps.md     what each optimisation was worth
  results/charts/leaderboard_ladder.png  throughput and mean response time vs users
  results/charts/leaderboard_steps.png   the four changes, at 1 000 users

Run:  .venv/bin/python scripts/analyze_leaderboard.py
"""
import json
import os
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
RUNS = os.path.join(ROOT, "results", "leaderboard")
CHARTS = os.path.join(ROOT, "results", "charts")
TABLES = os.path.join(ROOT, "report", "tables")
for d in (CHARTS, TABLES):
    os.makedirs(d, exist_ok=True)


def load(name):
    p = os.path.join(RUNS, name + ".json")
    return json.load(open(p)) if os.path.exists(p) else None


# The four changes, each measured on its own at 1 000 concurrent users.
STEPS = [
    ("SIM_u2", "baseline", "thread-per-connection balancer, fetch() to the database, one commit per message"),
    ("SIM_u3", "asyncio balancer", "the proxy's I/O layer moved onto an event loop"),
    ("SIM_u4", "keep-alive database client", "http.request over a pooled agent instead of global fetch()"),
    ("SIM_u5", "group commit", "appends arriving in one tick share a transaction"),
]

LADDERS = [
    ("SIM_static_full", "static", "before"),
    ("SIM_static_v2", "static", "after"),
    ("SIM_break_full", "breakpoint", "before"),
    ("SIM_break_v2", "breakpoint", "after"),
]


def write_table(name, header, rows, caption=""):
    with open(os.path.join(TABLES, name), "w") as fh:
        if caption:
            fh.write(caption.rstrip() + "\n\n")
        fh.write("| " + " | ".join(header) + " |\n")
        fh.write("|" + "|".join("---" for _ in header) + "|\n")
        for r in rows:
            fh.write("| " + " | ".join(str(x) for x in r) + " |\n")
    print("  wrote", os.path.relpath(os.path.join(TABLES, name), ROOT))


# ── what each change was worth ─────────────────────────────────────────────
step_rows, steps = [], []
for rid, label, detail in STEPS:
    d = load(rid)
    if not d:
        continue
    steps.append((label, d))
    step_rows.append([f"**{label}**", detail, f"{d['peak_rps']:.0f}",
                      f"{d['mean_response_ms']:.0f}",
                      f"{d['err_rate_overall'] * 100:.1f} %"])
if step_rows:
    base = steps[0][1]
    last = steps[-1][1]
    step_rows.append(["**net**", "",
                      f"**×{last['peak_rps'] / base['peak_rps']:.1f}**",
                      f"**−{(1 - last['mean_response_ms'] / base['mean_response_ms']) * 100:.0f} %**", ""])
    write_table("leaderboard_steps.md",
                ["change", "what it does", "throughput (req/s)", "mean response (ms)", "errors"],
                step_rows,
                "Each change measured on its own, 1 000 concurrent users, 8 000 requests, "
                "same cluster and same client.")

# ── the two ladders, stage by stage ────────────────────────────────────────
lad_rows = []
for rid, board, when in LADDERS:
    d = load(rid)
    if not d:
        continue
    for st in d["stages"]:
        lad_rows.append([board, when, st["concurrency"], st["requests"], st["successes"],
                         f"{st['err_rate'] * 100:.1f} %", f"{st['mean_ms']:.0f}", f"{st['rps']:.0f}"])
    lad_rows.append([f"**{board} — {when}**", "**total**",
                     "—", d["total_requests"], f"**{d['total_successes']}**",
                     f"**{d['err_rate_overall'] * 100:.2f} %**",
                     f"**{d['mean_response_ms']:.0f}**", f"{d['peak_rps']:.0f}"])
if lad_rows:
    write_table("leaderboard_ladders.md",
                ["board", "", "users", "requests", "successful", "errors", "mean ms", "req/s"],
                lad_rows,
                "The evaluation's own ladders, reproduced locally, before and after the four changes. "
                "\"before\" already includes the asyncio balancer; the baseline could not finish these "
                "ladders without double-digit error rates.")

# ── charts ─────────────────────────────────────────────────────────────────
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except ImportError:
    print("matplotlib unavailable — charts skipped (use .venv/bin/python)")
    sys.exit(0)

before_s, after_s = load("SIM_static_full"), load("SIM_static_v2")
before_b, after_b = load("SIM_break_full"), load("SIM_break_v2")
if after_s and after_b:
    fig, ax = plt.subplots(1, 2, figsize=(11.5, 4.3))
    series = [(before_b, "before", "#d97706", "--"), (after_b, "after", "#4f6df5", "-")]
    for d, label, col, ls in series:
        if not d:
            continue
        x = [s["concurrency"] for s in d["stages"]]
        ax[0].plot(x, [s["rps"] for s in d["stages"]], "o" + ls, color=col, label=label)
        ax[1].plot(x, [s["mean_ms"] for s in d["stages"]], "o" + ls, color=col, label=label)
    ax[0].set_ylabel("throughput (successful req/s)")
    ax[0].set_title("Throughput vs concurrent users", fontsize=10)
    ax[1].set_ylabel("mean response time (ms)")
    ax[1].set_title("Mean response time vs concurrent users\n(the static board's ranking metric)", fontsize=10)
    for a in ax:
        a.set_xlabel("concurrent users")
        a.grid(alpha=.3)
        a.legend(fontsize=8)
        a.set_ylim(bottom=0)
    plt.tight_layout()
    plt.savefig(os.path.join(CHARTS, "leaderboard_ladder.png"), dpi=150)
    plt.close()

if len(steps) >= 2:
    fig, ax = plt.subplots(1, 2, figsize=(11.5, 4.0))
    labels = [s[0] for s in steps]
    x = range(len(labels))
    ax[0].bar(list(x), [s[1]["peak_rps"] for s in steps], 0.55, color="#4f6df5")
    ax[0].set_ylabel("throughput (req/s)")
    ax[0].set_title("Throughput at 1 000 concurrent users", fontsize=10)
    ax[1].bar(list(x), [s[1]["mean_response_ms"] for s in steps], 0.55, color="#c62828")
    ax[1].set_ylabel("mean response time (ms)")
    ax[1].set_title("Mean response time at 1 000 concurrent users", fontsize=10)
    for a in ax:
        a.set_xticks(list(x))
        a.set_xticklabels(labels, rotation=18, ha="right", fontsize=8)
        a.grid(alpha=.3, axis="y")
    plt.tight_layout()
    plt.savefig(os.path.join(CHARTS, "leaderboard_steps.png"), dpi=150)
    plt.close()

print("charts written to", os.path.relpath(CHARTS, ROOT))

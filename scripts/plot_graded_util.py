#!/usr/bin/env python3
"""Utilisation of all four systems during a graded evaluation run.

The sampler (scripts/sysmetrics.py) read each container's own cgroup v2 accounting
once a second while the course's load generator drove the public URL through both
ladders. This is the load the assignment is judged on, measured on the systems it
runs on, so it is the utilisation figure that matters most."""
import csv, os, sys, collections
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src  = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "results", "sysmetrics", "EVAL_queued_2136.csv")
OUT  = os.path.join(ROOT, "results", "charts", "graded_utilisation.png")

by = collections.defaultdict(list)
for r in csv.DictReader(open(src)):
    by[r["system"]].append((float(r["t"]), float(r["cpu_pct"]), float(r["mem_mb"])))

# Trim to the evaluation itself: the first to last second in which a BACKEND is over
# 20 % CPU. Keying on any system would include the operator's own deploys and log
# analysis on sys1 before the run began, which is not what the figure is about.
busy = [t for s in ("sys2", "sys3", "sys4") for t, c, _ in by.get(s, []) if c > 20]
lo, hi = min(busy) - 5, max(busy) + 5
ROLE = {"sys1": "sys1 — load balancer", "sys2": "sys2 — backend",
        "sys3": "sys3 — backend + database", "sys4": "sys4 — backend"}
COL  = {"sys1": "#8e44ad", "sys2": "#2980b9", "sys3": "#c0392b", "sys4": "#27ae60"}

fig, ax = plt.subplots(2, 1, figsize=(13, 7), sharex=True)
for s in sorted(by):
    v = [(t - lo, c, m) for t, c, m in by[s] if lo <= t <= hi]
    ax[0].plot([t for t, _, _ in v], [c for _, c, _ in v], color=COL[s], lw=1.1, label=ROLE[s])
    ax[1].plot([t for t, _, _ in v], [m for _, _, m in v], color=COL[s], lw=1.1, label=ROLE[s])
ax[0].axhline(100, ls=":", color="k", lw=.8); ax[0].text(2, 102, "100 % = the container's whole CPU quota", fontsize=8)
ax[0].set_ylabel("CPU, % of own 1-core quota"); ax[0].set_ylim(0, 115); ax[0].grid(alpha=.3)
ax[0].set_title("All four systems during a graded evaluation run (static ladder, then breakpoint ladder)")
ax[0].legend(loc="upper right", fontsize=8, ncol=2)
ax[1].axhline(512, ls=":", color="k", lw=.8); ax[1].text(2, 518, "512 MB = the container's memory limit", fontsize=8)
ax[1].set_ylabel("memory, MB (cgroup)"); ax[1].set_ylim(0, 560); ax[1].grid(alpha=.3)
ax[1].set_xlabel("seconds")
fig.tight_layout(); fig.savefig(OUT, dpi=130)
print("wrote", OUT, f"(window {hi-lo:.0f}s)")
for s in sorted(by):
    v = [(c, m) for t, c, m in by[s] if lo <= t <= hi]
    b = [c for c, _ in v if c > 20]
    print(f"  {s}: cpu mean while busy {sum(b)/max(1,len(b)):.0f}%  max {max(c for c,_ in v):.0f}%   mem max {max(m for _,m in v):.0f} MB")

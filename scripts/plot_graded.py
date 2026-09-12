#!/usr/bin/env python3
"""Chart the graded leaderboard runs: throughput and errors per stage, before and
after the admission-control / feed-cache work. The "before" figures are the
leaderboard's own per-stage records for the earlier submission, not a local
reconstruction, so the two series were produced by the same generator on the same
systems and differ only in the code under test."""
import json, os
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT  = os.path.join(ROOT, "results", "charts", "graded_stages.png")

# The earlier graded run (rank 7 on the breakpoint board), from /api/runs.
BEFORE = [(200, 275.9, 0), (350, 205.5, 0), (500, 113.4, 0), (750, 104.5, 149),
          (1000, 87.0, 53), (1500, 122.6, 76), (2000, 78.0, 963)]
after  = json.load(open(os.path.join(ROOT, "results", "leaderboard", "GRADED_final.json")))
AFTER  = [(s["concurrency"], s["rps"], s["errors"]) for s in after["breakpoint"]["stages"]]
STATIC = [(s["concurrency"], s["rps"], s["errors"]) for s in after["static"]["stages"]]

fig, ax = plt.subplots(1, 3, figsize=(15, 4.2))

ax[0].plot([c for c,_,_ in BEFORE], [r for _,r,_ in BEFORE], "o--", color="#c0392b", label="before")
ax[0].plot([c for c,_,_ in AFTER],  [r for _,r,_ in AFTER],  "o-",  color="#27ae60", label="after")
ax[0].set_title("Breakpoint board — throughput per stage")
ax[0].set_xlabel("concurrent users"); ax[0].set_ylabel("requests / s")
ax[0].legend(); ax[0].grid(alpha=.3)
ax[0].set_ylim(60, 300)
ax[0].annotate("broke here", xy=(2000, 78), xytext=(1450, 110),
               arrowprops=dict(arrowstyle="->", color="#c0392b"), color="#c0392b", fontsize=9)

wb = [100*e/5000 for _,_,e in BEFORE]; wa = [100*e/5000 for _,_,e in AFTER]
ax[1].plot([c for c,_,_ in BEFORE], wb, "o--", color="#c0392b", label="before")
ax[1].plot([c for c,_,_ in AFTER],  wa, "o-",  color="#27ae60", label="after")
ax[1].set_ylim(-1, 24)
ax[1].axhline(20, ls=":", color="k")
ax[1].text(260, 20.6, "20 % = the stage fails and the run stops", fontsize=8)
ax[1].set_title("Breakpoint board — error rate per stage")
ax[1].set_xlabel("concurrent users"); ax[1].set_ylabel("errors %")
ax[1].legend(); ax[1].grid(alpha=.3)

ax[2].bar([str(c) for c,_,_ in STATIC], [r for _,r,_ in STATIC], color="#2980b9")
ax[2].set_title("Static board — throughput per stage\n(20 000 / 20 000 requests, 0 errors)")
ax[2].set_xlabel("concurrent users"); ax[2].set_ylabel("requests / s")
ax[2].grid(alpha=.3, axis="y")

fig.tight_layout(); fig.savefig(OUT, dpi=130)
print("wrote", OUT)

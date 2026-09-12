#!/usr/bin/env python3
"""
build_report.py — report/report.md -> report.html -> report/REPORT_12341680.pdf

Fills {{PLACEHOLDERS}} with generated content (tables from scripts/analyze.py,
highlighted source code, screenshots), renders Markdown to a self-contained HTML
(images inlined) and prints it to PDF with headless Chrome.

  .venv/bin/python scripts/build_report.py
"""
import base64
import datetime
import glob
import os
import re
import subprocess
import sys

import markdown
from pygments import highlight
from pygments.lexers import PythonLexer, JavascriptLexer
from pygments.formatters import HtmlFormatter

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
REPORT = os.path.join(ROOT, "report")
ROLL = "12341680"
md_src = open(os.path.join(REPORT, "report.md")).read()

formatter = HtmlFormatter(linenos="inline", cssclass="codehl", style="default")
pygments_css = formatter.get_style_defs(".codehl")


def code_block(path, lexer, start=None, end=None):
    src = open(os.path.join(ROOT, path)).read()
    if start is not None:
        lines = src.split("\n")
        src = "\n".join(lines[start - 1:end])
    return highlight(src, lexer, formatter)


def table(name):
    p = os.path.join(REPORT, "tables", name)
    return open(p).read() if os.path.exists(p) else "*(pending)*"


def chart(name, caption=""):
    p = os.path.join(ROOT, "results", "charts", name)
    if not os.path.exists(p):
        return f"*(chart {name} pending)*"
    return f'<div class="fig"><img src="../results/charts/{name}" alt="{name}"><div class="cap">{caption}</div></div>'


SHOTS = [
    ("11_dashboard_threshold.png",
     "Live balancer dashboard — algorithm <b>threshold</b>, switch threshold <b>0.15</b>, the load index of "
     "each backend, which backend traffic is pinned to, the switch counter, and an event log showing the "
     "threshold firing, a backend degrading, being ejected and being re-admitted"),
    ("12_feed_response.png",
     "<b>GET /feed</b> through the load balancer URL — the true room total, how many were returned, the "
     "truncation flag, and every message with its unique id, sequence number, sender and serving backend"),
    ("02_dashboard_scaling.png", "Dashboard during the scaling run — sys3 and sys4 admitted while load was running (§12.1)"),
    ("03_chat_login.png", "The secure chat still served through the same URL — login screen"),
    ("04_chat_conversation.png", "Conversation: message ids, seq numbers, serving backend, cluster panel"),
    ("05_duplicate_suppressed.png", "'Resend last' — same message id re-sent, server answers duplicate:true, nothing stored twice"),
]
shots = []
for fn, cap in SHOTS:
    if os.path.exists(os.path.join(REPORT, "screenshots", fn)):
        shots.append(f'<div class="shot"><strong>{cap}</strong><br><img src="screenshots/{fn}" alt="{cap}"></div>')
shots_md = "\n\n".join(shots) or "*(screenshots pending)*"

import html as _html
terms = []
for fn in sorted(glob.glob(os.path.join(REPORT, "terminal_captures", "*.txt"))):
    if os.path.basename(fn).startswith("02_"):      # dedup results are already the table in §8.4
        continue
    txt = open(fn).read().strip()
    terms.append(f'<div class="term"><strong>{os.path.basename(fn)}</strong><pre>{_html.escape(txt)}</pre></div>')
terms_md = "\n\n".join(terms) or "*(none)*"

repl = {
    "{{DATE}}": datetime.date.today().strftime("%d %B %Y"),
    "{{LB_CODE}}": "\x00LBCODE\x00",
    "{{LB_CODE2}}": "\x00LBCODE2\x00",
    "{{LB_CODE3}}": "\x00LBCODE3\x00",
    "{{DB_CODE}}": "\x00DBCODE\x00",
    "{{T_DEDUP}}": table("dedup.md"),
    "{{C_SCALE}}": chart("scaling_timeline.png", "Figure 8 — Backends admitted while the application was running: throughput, response time, active backends and per-backend share; dashed lines = a backend added."),
    "{{SCREENSHOTS}}": shots_md,
    "{{TERMINALS}}": terms_md,
    # --- updated task -------------------------------------------------------
    "{{T_THR}}": table("threshold_sweep.md"),
    "{{T_THRLOW}}": table("threshold_sweep_low.md"),
    "{{T_PUBRT}}": table("pub_response_time_vs_load.md"),
    "{{T_PUBTP}}": table("pub_throughput.md"),
    "{{T_UTIL}}": table("system_utilisation.md"),
    "{{T_ALGO2}}": table("algorithm_comparison_v2.md"),
    "{{C_THR}}": chart("threshold_sweep.png",
        "Figure 1 — Threshold sweep. Left and centre: 60 clients, whiskers show the min/max over the "
        "repetitions. Right: the same sweep at 10 clients, where the threshold decides whether traffic "
        "concentrates or spreads. Dotted line = the chosen T."),
    "{{C_PUBRT}}": chart("pub_response_time_vs_load.png",
        "Figure 2 — Response time vs load on /message and /feed (p50 left, p95 right), 1 / 2 / 3 "
        "backends, log-log."),
    "{{C_PUBTPUT}}": chart("pub_throughput_vs_load.png",
        "Figure 3 — Throughput vs load, and the effect of adding backends."),
    "{{C_PUBOFF}}": chart("pub_throughput_vs_offered_load.png",
        "Figure 4 — Throughput vs offered load (open loop, Poisson arrivals); dashed = ideal."),
    "{{C_UTILRAMP}}": chart("system_utilisation_ramp.png",
        "Figure 5 — Utilisation of all four systems during the 1 → 200 client ramp, with the client "
        "count and p95 response time underneath. 100 % is one whole CPU, which is each container's quota."),
    "{{C_UTILLOAD}}": chart("system_utilisation_vs_load.png",
        "Figure 6 — Mean CPU of each of the four systems against offered load, 3 backends."),
    "{{C_ALGO2}}": chart("algorithm_comparison_v2.png",
        "Figure 7 — The threshold rule against round robin, least connections and the adaptive score, "
        "same load and same cluster."),
    "{{T_LBSTEPS}}": table("leaderboard_steps.md"),
    "{{T_GRADED}}": table("graded_stages.md"),
    "{{T_GRADED_UTIL}}": table("graded_utilisation.md"),
    "{{C_GRADED_UTIL}}": chart("graded_utilisation.png",
        "Figure 11 — CPU and memory of all four systems, sampled once a second from each container's "
        "cgroup, while the course's own load generator ran both ladders against the public URL. "
        "100 % is one whole CPU, each container's quota; 512 MB is its memory limit."),
    "{{C_GRADED}}": chart("graded_stages.png",
        "Figure 9 — The graded leaderboard runs, per stage. Left: breakpoint throughput before and "
        "after admission control and the shared feed cache — flat where it used to collapse. Centre: "
        "the error rate that ends a run at 20 %. Right: the static board, 20 000 of 20 000 requests "
        "with zero errors."),
    "{{T_LADDERS}}": table("leaderboard_ladders.md"),
    "{{C_LBSTEPS}}": chart("leaderboard_steps.png",
        "Figure 10 — What each change was worth at 1 000 concurrent users."),
    "{{C_LADDER}}": chart("leaderboard_ladder.png",
        "Figure 11 — The evaluation's breakpoint ladder before and after: throughput and mean "
        "response time against concurrent users."),
    "{{C_FAILPUB}}": chart("failover_public.png",
        "Figure 9 — Failure and recovery on the public routes: per-backend throughput, response time, "
        "active backends and failed requests. sys3 was SIGKILLed at 45 s and restarted at 90 s."),
}
for k, v in repl.items():
    md_src = md_src.replace(k, v)

body = markdown.markdown(md_src, extensions=["tables", "fenced_code", "toc"])
def span(path, first, last):
    """Line range of a function, located by its `def`/`const` line so the excerpt
    cannot drift when the file is edited."""
    lines = open(os.path.join(ROOT, path)).read().split("\n")
    a = next(i for i, l in enumerate(lines) if first in l)
    b = next(i for i, l in enumerate(lines) if last in l)
    return a + 1, b


body = body.replace("\x00LBCODE\x00",
                    code_block("lb/loadbalancer.py", PythonLexer(), *span("lb/loadbalancer.py",
                               "def cpu_load(self)", "def snapshot(self, cfg)")))       # load index + score
body = body.replace("\x00LBCODE2\x00",
                    code_block("lb/loadbalancer.py", PythonLexer(), *span("lb/loadbalancer.py",
                               "def _pick_threshold(self, pool)", "def pick_sticky(self")))  # the threshold rule
body = body.replace("\x00LBCODE3\x00",
                    code_block("lb/loadbalancer.py", PythonLexer(), *span("lb/loadbalancer.py",
                               "    async def acquire(self):", "    def snapshot(self):")))   # the bulkhead
body = body.replace("\x00DBCODE\x00",
                    code_block("app/db_service.js", JavascriptLexer(), *span("app/db_service.js",
                               "function appendOne(room, entry)", "// ── One-time migration")))


def inline_img(m):
    src = m.group(1)
    p = os.path.normpath(os.path.join(REPORT, src))
    if not os.path.exists(p):
        return m.group(0)
    b64 = base64.b64encode(open(p, "rb").read()).decode()
    return m.group(0).replace(src, f"data:image/png;base64,{b64}")


body = re.sub(r'<img[^>]*src="([^"]+)"', inline_img, body)

html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Assignment 6 Report — Rahul Raj {ROLL}</title>
<style>
@page {{ size: A4; margin: 16mm 14mm; }}
body {{ font: 10.3pt/1.48 -apple-system, "Segoe UI", Roboto, sans-serif; color: #1a1a1e; max-width: 178mm; margin: 0 auto; }}
h1 {{ font-size: 21pt; margin: .2em 0; }}
h2 {{ font-size: 14pt; margin-top: 1.3em; border-bottom: 2px solid #4f6df5; padding-bottom: 3px; page-break-after: avoid; }}
h3 {{ font-size: 11.5pt; page-break-after: avoid; }}
table {{ border-collapse: collapse; width: 100%; font-size: 7.6pt; margin: 8px 0; page-break-inside: auto; }}
tr {{ page-break-inside: avoid; }}
td, th {{ border: 1px solid #ccc; padding: 2px 4px; text-align: left; vertical-align: top; }}
th {{ background: #eef1fe; }}
code {{ background: #f4f4f6; padding: 1px 4px; border-radius: 3px; font-size: 8.5pt; }}
pre {{ background: #f7f7f9; border: 1px solid #e2e2e8; border-radius: 6px; padding: 10px; font-size: 7.8pt; line-height: 1.35; white-space: pre-wrap; }}
pre code {{ background: none; padding: 0; }}
.codehl {{ font-size: 6.6pt; line-height: 1.28; }}
.codehl pre {{ border: 1px solid #e2e2e8; background: #fafafa; padding: 8px; white-space: pre-wrap; }}
.codehl table, .codehl td {{ border: none; }}
.codehl .linenos {{ color: #999; padding-right: 8px; user-select: none; }}
img {{ max-width: 100%; page-break-inside: avoid; }}
.fig {{ text-align: center; margin: 10px 0 14px; page-break-inside: avoid; }}
.fig img {{ max-width: 96%; border: 1px solid #e4e4e9; }}
.cap {{ font-size: 8.5pt; color: #444; margin-top: 4px; }}
.shot {{ page-break-inside: avoid; text-align: center; margin: 12px 0; }}
.shot img {{ max-width: 100%; margin-top: 4px; border: 1px solid #ddd; }}
.term {{ page-break-inside: avoid; margin: 10px 0; }}
.term pre {{ font-size: 6.9pt; }}
.cover {{ text-align: center; padding-top: 55mm; }}
.cover table {{ width: 72%; margin: 26px auto; font-size: 11pt; }}
.cover td {{ padding: 5px 10px; }}
.pagebreak {{ page-break-after: always; }}
blockquote {{ border-left: 3px solid #4f6df5; margin-left: 0; padding-left: 14px; color: #444; }}
{pygments_css}
</style></head><body>{body}</body></html>"""

html_path = os.path.join(REPORT, "report.html")
open(html_path, "w").write(html)
print(f"wrote {html_path} ({len(html)//1024} KB)")
chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
pdf_path = os.path.join(REPORT, f"REPORT_{ROLL}.pdf")
r = subprocess.run([chrome, "--headless", "--disable-gpu", f"--print-to-pdf={pdf_path}", "--no-pdf-header-footer",
                    "--virtual-time-budget=15000", html_path], capture_output=True, text=True, timeout=180)
if os.path.exists(pdf_path):
    print(f"wrote {pdf_path} ({os.path.getsize(pdf_path)//1024} KB)")
else:
    print("Chrome PDF failed:", r.stderr[-400:]); sys.exit(1)

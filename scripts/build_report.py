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
    ("01_dashboard_3_backends.png", "Live LB dashboard — three backends UP, adaptive scores, event log"),
    ("02_dashboard_scaling.png", "Dashboard during the scaling run — sys3 and sys4 admitted while load was running"),
    ("03_chat_login.png", "Chat login screen served through the load balancer"),
    ("04_chat_conversation.png", "Conversation: message ids, seq numbers, serving backend, cluster panel"),
    ("05_duplicate_suppressed.png", "'Resend last' — same message id re-sent, server answers duplicate:true, nothing stored twice"),
    ("06_dashboard_failover.png", "Dashboard with sys3 DOWN during the failure test"),
    ("07_sanity_check.png", "scripts/sanity_check.sh — whole system + previous assignments green"),
    ("08_dedup_test.png", "scripts/dedup_test.py through the public URL"),
    ("09_db_stats.png", "SQLite database service statistics"),
    ("10_ssh_processes.png", "Backend processes and listeners on the assigned systems"),
]
shots = []
for fn, cap in SHOTS:
    if os.path.exists(os.path.join(REPORT, "screenshots", fn)):
        shots.append(f'<div class="shot"><strong>{cap}</strong><br><img src="screenshots/{fn}" alt="{cap}"></div>')
shots_md = "\n\n".join(shots) or "*(screenshots pending)*"

import html as _html
terms = []
for fn in sorted(glob.glob(os.path.join(REPORT, "terminal_captures", "*.txt"))):
    if os.path.basename(fn).startswith("02_"):      # dedup results are already the table in §6.4
        continue
    txt = open(fn).read().strip()
    terms.append(f'<div class="term"><strong>{os.path.basename(fn)}</strong><pre>{_html.escape(txt)}</pre></div>')
terms_md = "\n\n".join(terms) or "*(none)*"

repl = {
    "{{DATE}}": datetime.date.today().strftime("%d %B %Y"),
    "{{LB_CODE}}": "\x00LBCODE\x00",
    "{{LB_CODE2}}": "\x00LBCODE2\x00",
    "{{DB_CODE}}": "\x00DBCODE\x00",
    "{{T_RT}}": table("response_time_vs_load.md"),
    "{{T_OFFERED}}": table("throughput_vs_offered_load.md"),
    "{{T_SCALE}}": table("scaling_phases.md"),
    "{{T_FAIL}}": table("failover_phases.md"),
    "{{T_ALGO}}": table("algorithm_comparison.md"),
    "{{T_DEDUP}}": table("dedup.md"),
    "{{C_RT}}": chart("response_time_vs_load.png", "Figure 1 — Response time vs load (p50 left, p95 right), 1 / 2 / 3 backends, log-log."),
    "{{C_TPUT_LOAD}}": chart("throughput_vs_load.png", "Figure 2 — Throughput vs load (closed loop)."),
    "{{C_OFFERED}}": chart("throughput_vs_offered_load.png", "Figure 3 — Throughput vs offered load (open loop, Poisson arrivals); dashed = ideal."),
    "{{C_SCALE}}": chart("scaling_timeline.png", "Figure 4 — Dynamic scaling timeline: throughput, response time, active backends and per-backend share; dashed lines = backend added."),
    "{{C_SCALE_EFFECT}}": chart("scaling_effect.png", "Figure 5 — Effect of adding each backend, per phase of the scaling run."),
    "{{C_FAIL}}": chart("failover_timeline.png", "Figure 6 — Failure and recovery timeline under 100 users (sys3 SIGKILLed at ~37 s, restarted at ~92 s; dashed = LB ejected / re-admitted)."),
    "{{C_ALGO}}": chart("algorithm_comparison.png", "Figure 7 — Adaptive vs round robin vs least connections when sys3 is CPU-loaded."),
    "{{SCREENSHOTS}}": shots_md,
    "{{TERMINALS}}": terms_md,
    "{{T_SUMMARY}}": table("summary.md"),
}
for k, v in repl.items():
    md_src = md_src.replace(k, v)

body = markdown.markdown(md_src, extensions=["tables", "fenced_code", "toc"])
body = body.replace("\x00LBCODE\x00", code_block("lb/loadbalancer.py", PythonLexer(), 129, 151))   # cpu_load + score
body = body.replace("\x00LBCODE2\x00", code_block("lb/loadbalancer.py", PythonLexer(), 270, 305))  # pick()
body = body.replace("\x00DBCODE\x00", code_block("app/db_service.js", JavascriptLexer(), 132, 166))  # appendTx


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

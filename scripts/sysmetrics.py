#!/usr/bin/env python3
"""
sysmetrics.py — sample the utilisation of ALL FOUR lab systems during a run.

The updated assignment asks the report to plot "system utilization of all 4
systems", so every experiment now runs one of these alongside the load
generator. sys1 carries the load balancer and the SQLite database service;
sys2/sys3/sys4 carry the chat backends.

The four systems are containers on one physical host: /proc/stat, uptime and
the load average are shared by all of them and say nothing about an individual
system. The per-system truth is the cgroup v2 accounting each container has of
its own:

    /sys/fs/cgroup/cpu.max        quota / period  -> cores this system may use
    /sys/fs/cgroup/cpu.stat       usage_usec      -> CPU microseconds consumed
    /sys/fs/cgroup/memory.current bytes in use
    /sys/fs/cgroup/memory.max     bytes allowed

cpu_pct is the delta of usage_usec over the sampling interval, expressed as a
percentage of the system's own quota — 100 % means "this container is using the
whole CPU it was given", which is exactly the signal the load balancer's
threshold rule reads out of /health.

One persistent SSH connection per system runs a tiny remote sampler; nothing is
installed on the boxes and the sampler costs well under 1 % of a core.

Usage
    python3 scripts/sysmetrics.py --run-id L1_c50 --duration 60
    python3 scripts/sysmetrics.py --run-id demo --duration 0     # until Ctrl-C

Output
    results/sysmetrics/<run_id>.csv    t,system,cpu_pct,mem_mb,mem_pct,quota_cores
"""

import argparse
import os
import signal
import subprocess
import sys
import threading
import time

HOSTS = [("sys1", "lbsys1", "LB + database"),
         ("sys2", "lbsys2", "backend"),
         ("sys3", "lbsys3", "backend"),
         ("sys4", "lbsys4", "backend")]

# Runs on the lab box. Prints one CSV line per interval, forever, unbuffered.
REMOTE = r'''
import time, sys
def rd(p, d=""):
    try:
        with open(p) as f: return f.read()
    except OSError: return d
def usage_us():
    for line in rd("/sys/fs/cgroup/cpu.stat").splitlines():
        if line.startswith("usage_usec"): return int(line.split()[1])
    return -1
def quota_cores():
    parts = rd("/sys/fs/cgroup/cpu.max", "max 100000").split()
    if len(parts) != 2 or parts[0] == "max": return 0.0
    return int(parts[0]) / int(parts[1])
def mem():
    cur = int(rd("/sys/fs/cgroup/memory.current", "0").strip() or 0)
    mx = rd("/sys/fs/cgroup/memory.max", "max").strip()
    mx = 0 if mx in ("max", "") else int(mx)
    return cur, mx
INTERVAL = %(interval)s
cores = quota_cores() or 1.0
prev, prevt = usage_us(), time.time()
while True:
    time.sleep(INTERVAL)
    cur, curt = usage_us(), time.time()
    dt = curt - prevt
    cpu = 0.0
    if cur >= 0 and prev >= 0 and dt > 0:
        cpu = (cur - prev) / 1e6 / dt / cores * 100.0
    prev, prevt = cur, curt
    m, mmax = mem()
    print("%%.3f,%%.2f,%%.1f,%%.2f,%%.3f" %% (curt, max(0.0, min(cpu, 100.0 * 8)),
          m / 1048576.0, (m / mmax * 100.0) if mmax else 0.0, cores), flush=True)
'''


def sampler(sysname, ssh_host, interval, t0, rows, lock, stop):
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", ssh_host,
           "python3 -u -c " + shell_quote(REMOTE % {"interval": interval})]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        for line in proc.stdout:
            if stop.is_set():
                break
            parts = line.strip().split(",")
            if len(parts) != 5:
                continue
            ts, cpu, mem_mb, mem_pct, cores = parts
            with lock:
                rows.append((round(float(ts) - t0, 2), sysname, cpu, mem_mb, mem_pct, cores))
    finally:
        try: proc.send_signal(signal.SIGTERM)
        except Exception: pass
        try: proc.wait(timeout=3)
        except Exception: proc.kill()


def shell_quote(s):
    return "'" + s.replace("'", "'\"'\"'") + "'"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--duration", type=float, default=0, help="seconds (0 = until Ctrl-C / SIGTERM)")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--out-dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                      "..", "results", "sysmetrics"))
    args = ap.parse_args()

    rows, lock, stop = [], threading.Lock(), threading.Event()
    t0 = time.time()
    threads = [threading.Thread(target=sampler, args=(s, h, args.interval, t0, rows, lock, stop), daemon=True)
               for s, h, _ in HOSTS]
    for t in threads:
        t.start()
    print(f"[sysmetrics] {args.run_id}: sampling sys1..sys4 every {args.interval}s", flush=True)

    def finish(*_):
        stop.set()

    signal.signal(signal.SIGTERM, finish)
    signal.signal(signal.SIGINT, finish)
    end = t0 + args.duration if args.duration else None
    while not stop.is_set() and (end is None or time.time() < end):
        time.sleep(0.25)
    stop.set()
    time.sleep(0.5)

    os.makedirs(args.out_dir, exist_ok=True)
    path = os.path.join(args.out_dir, f"{args.run_id}.csv")
    with lock:
        rows.sort(key=lambda r: (r[0], r[1]))
        with open(path, "w") as fh:
            fh.write("t,system,cpu_pct,mem_mb,mem_pct,quota_cores\n")
            for r in rows:
                fh.write(",".join(str(x) for x in r) + "\n")
    got = {s: sum(1 for r in rows if r[1] == s) for s, _, _ in HOSTS}
    print(f"[sysmetrics] wrote {path}  ({len(rows)} samples: " +
          ", ".join(f"{k}={v}" for k, v in got.items()) + ")")
    if min(got.values()) == 0:
        print("[sysmetrics] WARNING: a system produced no samples", file=sys.stderr)


if __name__ == "__main__":
    main()

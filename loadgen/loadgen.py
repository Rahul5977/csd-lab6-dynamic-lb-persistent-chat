#!/usr/bin/env python3
"""
loadgen.py — custom HTTP load generator for the chat app (Assignment 6, v2).

Runs on the local Mac and drives the load balancer. Pure Python 3 stdlib.

Modes
  closed-loop (default)  N virtual users in a request→response loop
  open-loop  --rate R    Poisson arrivals at a fixed offered load (req/s),
                         independent of how slow responses get — this is what
                         the "throughput vs offered load" graph needs
  ramp       --ramp "25:40,50:40,100:180"   closed-loop concurrency schedule
                         (users:seconds, ...) for the dynamic-scaling timeline:
                         load rises in steps while backends are added live

Request mix per virtual user (weighted): 10 % login (scrypt), 55 % fetch,
30 % send, 5 % health. Every SEND carries a client-minted UUID message id and
is retried (same id) on a transport error, exactly like the browser client —
so failover experiments show retries being deduplicated, not duplicated.

Per request: start time, latency, HTTP status, error class, X-Backend-Id,
X-LB-Active-Backends, duplicate flag. With --poll-stats the LB's /lb/stats is
sampled every second (active backends, per-backend state/score/in-flight) —
the "number of active backends" series the assignment asks for.

Output: results/raw/<run_id>.json with summary, 1-second timeseries, records.
"""

import argparse
import http.client
import json
import os
import random
import sys
import threading
import time
import uuid
from urllib.parse import urlparse

MIX = [("login", 10), ("fetch", 55), ("send", 30), ("health", 5)]
MIX_EXPANDED = [name for name, w in MIX for _ in range(w)]
WORDS = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet "
         "kilo lima mike november oscar papa quebec romeo sierra tango").split()


class VUser:
    """One virtual user: own account, own session cookie, own connection."""

    def __init__(self, idx, base, room):
        self.idx = idx
        self.base = urlparse(base)
        self.name = f"lg_{idx}"
        self.password = "loadgen-pass-12345"
        self.cookie = None
        self.conn = None
        self.room = room

    def connect(self):
        if self.conn:
            try: self.conn.close()
            except Exception: pass
        self.conn = http.client.HTTPConnection(self.base.hostname, self.base.port or 80, timeout=15)

    def raw(self, method, path, body=None, extra=None):
        payload = json.dumps(body).encode() if body is not None else None
        headers = dict(extra or {})
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if self.cookie:
            headers["Cookie"] = self.cookie
        self.conn.request(method, path, payload, headers)
        resp = self.conn.getresponse()
        data = resp.read()
        sc = resp.getheader("Set-Cookie")
        if sc:
            self.cookie = sc.split(";")[0]
        return resp.status, resp, data

    def request(self, method, path, body=None, extra=None):
        """One request with a single silent reconnect for idempotent calls."""
        try:
            return self.raw(method, path, body, extra)
        except (http.client.HTTPException, OSError):
            self.connect()
            return self.raw(method, path, body, extra)

    def setup(self):
        self.connect()
        self.conn.timeout = 30
        st, _, _ = self.request("POST", "/api/register", {"username": self.name, "password": self.password})
        if st == 409:
            st, _, _ = self.request("POST", "/api/login", {"username": self.name, "password": self.password})
        if st != 200:
            raise RuntimeError(f"user {self.name} setup failed: HTTP {st}")
        self.request("POST", "/api/rooms", {"id": self.room})
        self.conn.timeout = 15

    def one(self, kind, retries):
        """Execute one request of the mix. Returns (status, backend, active, dup, attempts)."""
        if kind == "login":
            st, resp, _ = self.request("POST", "/api/login", {"username": self.name, "password": self.password})
            return st, resp, False, 1
        if kind == "fetch":
            st, resp, _ = self.request("GET", f"/api/messages?room={self.room}&since={random.randint(0, 50)}")
            return st, resp, False, 1
        if kind == "health":
            st, resp, _ = self.request("GET", "/health")
            return st, resp, False, 1
        # send: client-minted id, retried with the SAME id on transport failure
        mid = str(uuid.uuid4())
        body = {"id": mid, "room": self.room, "text": " ".join(random.choices(WORDS, k=random.randint(3, 12)))}
        attempt, last_exc = 0, None
        while attempt <= retries:
            attempt += 1
            try:
                st, resp, data = self.raw("POST", "/api/messages", body)
                if st >= 500 and attempt <= retries:      # LB 502/503: retry same id
                    time.sleep(0.05 * attempt)
                    continue
                dup = False
                if st == 200:
                    try: dup = bool(json.loads(data).get("duplicate"))
                    except ValueError: pass
                return st, resp, dup, attempt
            except (http.client.HTTPException, OSError) as e:
                last_exc = e
                self.connect()
                time.sleep(0.05 * attempt)
        raise last_exc or RuntimeError("send failed")


def percentile(sorted_arr, p):
    if not sorted_arr:
        return 0.0
    return sorted_arr[min(len(sorted_arr) - 1, int(p / 100 * len(sorted_arr)))]


def parse_ramp(spec):
    """'25:40,50:40' -> [(25,40),(50,40)]"""
    out = []
    for part in spec.split(","):
        users, secs = part.split(":")
        out.append((int(users), float(secs)))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--concurrency", type=int, default=10)
    ap.add_argument("--duration", type=int, default=60)
    ap.add_argument("--warmup", type=int, default=10, help="seconds discarded from statistics")
    ap.add_argument("--rate", type=float, default=0, help="open-loop offered load req/s (0 = closed loop)")
    ap.add_argument("--ramp", default="", help="closed-loop schedule users:seconds,... (overrides --concurrency/--duration)")
    ap.add_argument("--think", type=float, default=0)
    ap.add_argument("--retries", type=int, default=2, help="send retries with the same message id")
    ap.add_argument("--poll-stats", action="store_true", help="sample <url>/lb/stats every second")
    ap.add_argument("--room", default="loadtest")
    ap.add_argument("--max-inflight", type=int, default=1500, help="open loop: drop arrivals beyond this")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--note", default="")
    ap.add_argument("--out-dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results", "raw"))
    args = ap.parse_args()
    run_id = args.run_id or f"run_{int(time.time())}"

    ramp = parse_ramp(args.ramp) if args.ramp else []
    if ramp:
        args.concurrency = max(u for u, _ in ramp)
        args.duration = int(sum(s for _, s in ramp))
    mode = "open" if args.rate else ("ramp" if ramp else "closed")
    print(f"[loadgen] {run_id}: target={args.url} c={args.concurrency} dur={args.duration}s "
          f"warmup={args.warmup}s mode={mode}{'@' + str(args.rate) + 'rps' if args.rate else ''}"
          f"{' ramp=' + args.ramp if ramp else ''}", flush=True)

    # ── setup phase (not measured, staggered: scrypt is expensive on 1 core) ──
    users = [VUser(i, args.url, args.room) for i in range(args.concurrency)]
    setup_errors = 0
    sem = threading.Semaphore(8)

    def setup_worker(u):
        nonlocal setup_errors
        with sem:
            for attempt in (1, 2, 3):
                try:
                    u.setup(); return
                except Exception as e:
                    if attempt == 3:
                        setup_errors += 1
                        print(f"[loadgen] setup failed for {u.name}: {e}", file=sys.stderr)
                    else:
                        time.sleep(1)
    threads = [threading.Thread(target=setup_worker, args=(u,)) for u in users]
    for t in threads: t.start()
    for t in threads: t.join()
    if setup_errors > args.concurrency // 4:
        print("[loadgen] too many setup failures, aborting", file=sys.stderr)
        sys.exit(1)
    print(f"[loadgen] {len(users) - setup_errors}/{len(users)} users ready", flush=True)

    # ── measurement phase ────────────────────────────────────────────────────
    records = []                 # (t_rel, ms, status, backend, err, active, dup, attempts, kind)
    rec_lock = threading.Lock()
    stop = threading.Event()
    t_begin = time.time()
    inflight = [0]
    dropped = [0]

    def record(t0, ms, status, backend, err, active, dup, attempts, kind):
        with rec_lock:
            records.append((round(t0 - t_begin, 3), round(ms, 2), status, backend, err, active, dup, attempts, kind))

    def do_one(u, kind):
        t0 = time.time()
        try:
            st, resp, dup, attempts = u.one(kind, args.retries)
            record(t0, (time.time() - t0) * 1000, st, resp.getheader("X-Backend-Id") or "?", "",
                   int(resp.getheader("X-LB-Active-Backends") or 0), dup, attempts, kind)
        except Exception as e:
            record(t0, (time.time() - t0) * 1000, 0, "?", type(e).__name__, 0, False, args.retries + 1, kind)
            try: u.connect()
            except Exception: time.sleep(0.5)

    active_users = [args.concurrency]     # ramp: how many closed-loop workers may run

    def closed_worker(u):
        while not stop.is_set():
            if u.idx >= active_users[0]:
                time.sleep(0.2); continue
            do_one(u, random.choice(MIX_EXPANDED))
            if args.think:
                time.sleep(args.think)

    def open_loop_dispatcher():
        i = 0
        while not stop.is_set():
            time.sleep(random.expovariate(args.rate))
            u = users[i % len(users)]; i += 1
            if inflight[0] >= args.max_inflight:
                dropped[0] += 1
                record(time.time(), 0, 0, "?", "Dropped", 0, False, 0, "drop")
                continue
            kind = random.choice(MIX_EXPANDED)
            def fire(u=u, kind=kind):
                inflight[0] += 1
                try:
                    # each open-loop request needs its own connection (a user may be busy)
                    v = VUser(u.idx, args.url, args.room); v.cookie = u.cookie; v.connect()
                    do_one(v, kind)
                    try: v.conn.close()
                    except Exception: pass
                finally:
                    inflight[0] -= 1
            threading.Thread(target=fire, daemon=True).start()

    stats_samples = []           # (t_rel, active, {backend: state}, {backend: in_flight}, {backend: score})

    def stats_poller():
        p = urlparse(args.url)
        while not stop.is_set():
            t0 = time.time()
            try:
                c = http.client.HTTPConnection(p.hostname, p.port or 80, timeout=3)
                c.request("GET", "/lb/stats"); r = c.getresponse(); d = json.loads(r.read()); c.close()
                stats_samples.append((round(t0 - t_begin, 1), d.get("active_backends", 0),
                                      {b["id"]: b["state"] for b in d["backends"]},
                                      {b["id"]: b["in_flight"] for b in d["backends"]},
                                      {b["id"]: b.get("score") for b in d["backends"]},
                                      {b["id"]: (b.get("load") or {}).get("cpu_pct") for b in d["backends"]}))
            except Exception:
                stats_samples.append((round(t0 - t_begin, 1), -1, {}, {}, {}, {}))
            time.sleep(max(0.0, 1.0 - (time.time() - t0)))

    if args.rate:
        workers = [threading.Thread(target=open_loop_dispatcher, daemon=True)]
    else:
        workers = [threading.Thread(target=closed_worker, args=(u,), daemon=True) for u in users]
    if args.poll_stats:
        workers.append(threading.Thread(target=stats_poller, daemon=True))
    for w in workers:
        w.start()

    # ramp schedule + live ticker
    phases = []                  # (t_start, users)
    if ramp:
        t = 0.0
        for u_count, secs in ramp:
            phases.append((t, u_count)); t += secs
        active_users[0] = ramp[0][0]
    t_end = t_begin + args.duration
    phase_i = 0
    last_print = time.time()
    while time.time() < t_end:
        time.sleep(0.5)
        now = time.time() - t_begin
        if ramp and phase_i + 1 < len(phases) and now >= phases[phase_i + 1][0]:
            phase_i += 1
            active_users[0] = phases[phase_i][1]
            print(f"[loadgen] t+{now:4.0f}s  ramp -> {active_users[0]} users", flush=True)
        if time.time() - last_print >= 5:
            last_print = time.time()
            with rec_lock:
                n = len(records)
            print(f"[loadgen] t+{now:4.0f}s  {n} requests so far", flush=True)
    stop.set()
    time.sleep(2)

    # ── statistics ───────────────────────────────────────────────────────────
    sample = [r for r in records if r[0] >= args.warmup]
    lat = sorted(r[1] for r in sample if r[2] == 200)
    okc = sum(1 for r in sample if r[2] == 200)
    errc = len(sample) - okc
    span = args.duration - args.warmup
    dist, errors_by_class = {}, {}
    retried = sum(1 for r in sample if r[8] == "send" and r[7] > 1)
    dups = sum(1 for r in sample if r[6])
    for r in sample:
        dist[r[3]] = dist.get(r[3], 0) + 1
        if r[2] != 200:
            key = r[4] or f"HTTP{r[2]}"
            errors_by_class[key] = errors_by_class.get(key, 0) + 1
    mean = sum(lat) / len(lat) if lat else 0
    stddev = (sum((x - mean) ** 2 for x in lat) / len(lat)) ** 0.5 if lat else 0

    # 1-second buckets: requests, ok, errors, p50, p95, active backends, per-backend share
    buckets = {}
    for r in records:
        b = int(r[0])
        bk = buckets.setdefault(b, {"requests": 0, "ok": 0, "errors": 0, "lat": [], "by": {}, "dups": 0})
        bk["requests"] += 1
        if r[2] == 200:
            bk["ok"] += 1; bk["lat"].append(r[1])
        else:
            bk["errors"] += 1
        bk["by"][r[3]] = bk["by"].get(r[3], 0) + 1
        if r[6]: bk["dups"] += 1
    stats_by_sec = {int(s[0]): s for s in stats_samples}
    timeseries = []
    for sec in range(0, args.duration + 1):
        bk = buckets.get(sec, {"requests": 0, "ok": 0, "errors": 0, "lat": [], "by": {}, "dups": 0})
        l = sorted(bk["lat"])
        s = stats_by_sec.get(sec)
        active_hdr = max((r[5] for r in records if int(r[0]) == sec), default=0)
        timeseries.append({
            "t": sec, "requests": bk["requests"], "ok": bk["ok"], "errors": bk["errors"], "dups": bk["dups"],
            "p50": round(percentile(l, 50), 1), "p95": round(percentile(l, 95), 1),
            "mean": round(sum(l) / len(l), 1) if l else 0,
            "by_backend": bk["by"],
            "active_backends": s[1] if s else active_hdr,
            "states": s[2] if s else {}, "in_flight": s[3] if s else {}, "scores": s[4] if s else {}, "cpu": s[5] if s else {},
            "users": next((u for t, u in reversed(phases) if sec >= t), args.concurrency) if ramp else args.concurrency,
        })

    summary = {
        "run_id": run_id, "url": args.url, "concurrency": args.concurrency, "duration_s": args.duration,
        "warmup_s": args.warmup, "mode": mode, "rate": args.rate, "ramp": args.ramp, "note": args.note,
        "started_at": t_begin, "total_requests": len(sample), "success": okc, "errors": errc,
        "error_rate_pct": round(errc / len(sample) * 100, 3) if sample else 0,
        "errors_by_class": errors_by_class, "dropped_arrivals": dropped[0],
        "throughput_rps": round(okc / span, 2) if span else 0,
        "offered_rps": args.rate, "sends_retried": retried, "duplicates_suppressed": dups,
        "latency_ms": {"mean": round(mean, 2), "stddev": round(stddev, 2), "min": round(lat[0], 2) if lat else 0,
                       "p50": round(percentile(lat, 50), 2), "p90": round(percentile(lat, 90), 2),
                       "p95": round(percentile(lat, 95), 2), "p99": round(percentile(lat, 99), 2),
                       "max": round(lat[-1], 2) if lat else 0},
        "backend_distribution": dist,
        "active_backends_min": min((r[5] for r in sample if r[5]), default=0),
        "active_backends_max": max((r[5] for r in sample if r[5]), default=0),
    }
    os.makedirs(args.out_dir, exist_ok=True)
    out_path = os.path.join(args.out_dir, f"{run_id}.json")
    with open(out_path, "w") as fh:
        json.dump({"summary": summary, "timeseries_1s": timeseries,
                   "records": [{"t": r[0], "ms": r[1], "status": r[2], "backend": r[3], "err": r[4],
                                "active": r[5], "dup": r[6], "attempts": r[7], "kind": r[8]} for r in sample]}, fh)
    print(json.dumps(summary, indent=2))
    print(f"[loadgen] wrote {out_path}")


if __name__ == "__main__":
    main()

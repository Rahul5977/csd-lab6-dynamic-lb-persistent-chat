#!/usr/bin/env python3
"""
leaderboard_sim.py — a local copy of the evaluation load tester.

The course leaderboard runs two boards against a submitted load-balancer URL,
back to back, using only /message and /feed:

  static      250 -> 500 -> 750 -> 1000 concurrent users, each stage sending
              exactly 5 000 requests. Ranked on the MEAN response time of the
              successful requests (lower is better).
  breakpoint  200 -> 350 -> 500 -> 750 -> 1000 -> 1500 concurrent users, each
              stage up to 5 000 requests, stopping as soon as a stage exceeds
              20 % errors. Ranked on TOTAL SUCCESSFUL requests before breaking.

Reconstructed from the published run records: a stage issues
`budget + concurrency` requests, i.e. every virtual user posts
`budget / concurrency` messages and then reads /feed once. The run also reports
"message completeness" — of the messages that were accepted with a 2xx, how
many are found in /feed afterwards — so this script checks that too.

asyncio, one keep-alive connection per virtual user, no third-party packages:
a thread-per-user client cannot itself reach 1 500 concurrent users.

  python3 loadgen/leaderboard_sim.py --url http://10.1.75.53:3269
  python3 loadgen/leaderboard_sim.py --url ... --board breakpoint
  python3 loadgen/leaderboard_sim.py --url ... --stages 250 --budget 1000

Output: a per-stage table plus the two ranking metrics, and
results/raw/<run_id>.json for the report.
"""

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from urllib.parse import urlparse

STATIC_STAGES = [250, 500, 750, 1000]
BREAK_STAGES = [200, 350, 500, 750, 1000, 1500, 2000]   # the real ladder goes to 2000
BREAK_PCT = 0.20
# The evaluation's messages are ~400 characters of RANDOM printable text: a tagged
# prefix, a timestamp, then noise. That matters more than it sounds. A fixed string
# compresses ninefold and made every feed measurement here optimistic by an order of
# magnitude; random text does not compress at all. Reproduced from the real traffic.
import random as _random
import string as _string
_ALPHA = _string.ascii_letters + _string.digits + " !#$%&()*+,-./:;<=>?@[]^_{|}~"
_RUN_TAG = "%08x" % _random.getrandbits(32)


def make_msg(n):
    body = "".join(_random.choice(_ALPHA) for _ in range(_random.randint(300, 450)))
    return f"#{_RUN_TAG}-{n}# timestamp={1700000000000 + n} {body}"
ACCEPT_GZIP = False                            # --gzip: advertise gzip, as most HTTP clients do


class Stat:
    def __init__(self, concurrency, budget):
        self.concurrency = concurrency
        self.budget = budget
        self.requests = 0
        self.successes = 0
        self.errors = 0
        self.timeouts = 0
        self.latencies = []
        self.accepted = set()        # message ids the server answered 2xx for
        self.ambiguous = 0           # posts that failed: excluded from the loss count
        self.started = 0.0
        self.ended = 0.0

    @property
    def err_rate(self):
        return self.errors / self.requests if self.requests else 0.0

    @property
    def mean_ms(self):
        return statistics.mean(self.latencies) if self.latencies else 0.0

    @property
    def rps(self):
        span = self.ended - self.started
        return self.successes / span if span > 0 else 0.0


class Conn:
    """One virtual user: a keep-alive HTTP/1.1 connection driven by asyncio."""

    def __init__(self, host, port, timeout):
        self.host, self.port, self.timeout = host, port, timeout
        self.r = self.w = None

    async def connect(self):
        await self.close()
        self.r, self.w = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), self.timeout)

    async def close(self):
        if self.w is not None:
            try:
                self.w.close()
                await self.w.wait_closed()
            except Exception:
                pass
        self.r = self.w = None

    async def request(self, method, path, body=None, keep=True):
        """Returns (status, body_bytes). With keep=False the body is read and
        dropped in blocks instead of being assembled, which matters: the feed is
        megabytes and hundreds of virtual users hold one each, which is enough to
        exhaust a 512 MB box and kill the generator rather than the server."""
        if self.w is None:
            await self.connect()
        head = f"{method} {path} HTTP/1.1\r\nHost: {self.host}:{self.port}\r\n"
        if body is not None:
            head += ("Content-Type: application/json\r\n"
                     f"Content-Length: {len(body)}\r\n")
        head += "Connection: keep-alive\r\n"
        if ACCEPT_GZIP:
            head += "Accept-Encoding: gzip\r\n"
        head += "\r\n"
        self.w.write(head.encode() + (body or b""))
        await self.w.drain()

        status_line = await self.r.readuntil(b"\r\n")
        status = int(status_line.split(b" ")[1])
        headers, clen, chunked = {}, None, False
        while True:
            line = await self.r.readuntil(b"\r\n")
            if line in (b"\r\n", b"\n"):
                break
            k, _, v = line.partition(b":")
            k = k.strip().lower()
            if k == b"content-length":
                clen = int(v.strip())
            elif k == b"transfer-encoding" and b"chunked" in v.lower():
                chunked = True
            elif k == b"connection" and b"close" in v.lower():
                headers[b"close"] = True
        if chunked:
            chunks = []
            while True:
                size = int((await self.r.readuntil(b"\r\n")).strip(), 16)
                if size == 0:
                    await self.r.readuntil(b"\r\n")
                    break
                block = await self.r.readexactly(size)
                if keep:
                    chunks.append(block)
                await self.r.readuntil(b"\r\n")
            data = b"".join(chunks)
        elif clen is not None:
            if keep:
                data = await self.r.readexactly(clen)
            else:
                left, data = clen, b""
                while left:
                    n = min(65536, left)
                    await self.r.readexactly(n)
                    left -= n
        else:
            data = await self.r.read(-1)
            await self.close()
        if headers.get(b"close"):
            await self.close()
        return status, data


async def run_stage(cfg, concurrency, budget, stat):
    """`budget` posts spread over `concurrency` users, then one /feed each."""
    remaining = [budget]
    lock = asyncio.Lock()
    p = urlparse(cfg.url)
    host, port = p.hostname, p.port or 80

    async def user(idx):
        c = Conn(host, port, cfg.timeout)
        name = f"user-{idx}"
        try:
            while True:
                async with lock:
                    if remaining[0] <= 0:
                        break
                    remaining[0] -= 1
                mid = f"lbsim-{cfg.run_id}-{concurrency}-{idx}-{remaining[0]}"
                body = json.dumps({"client-name": name, "msg": make_msg(stat.requests), "id": mid}).encode()
                t0 = time.perf_counter()
                try:
                    st, _ = await asyncio.wait_for(
                        c.request("POST", cfg.message_path, body), cfg.timeout)
                    ms = (time.perf_counter() - t0) * 1000
                    stat.requests += 1
                    if 200 <= st < 300:
                        stat.successes += 1
                        stat.latencies.append(ms)
                        stat.accepted.add(mid)
                    else:
                        stat.errors += 1
                        stat.ambiguous += 1
                except asyncio.TimeoutError:
                    stat.requests += 1; stat.errors += 1; stat.timeouts += 1; stat.ambiguous += 1
                    await c.close()
                except Exception:
                    stat.requests += 1; stat.errors += 1; stat.ambiguous += 1
                    await c.close()
            # every user reads the feed once at the end of its stage
            t0 = time.perf_counter()
            try:
                st, _ = await asyncio.wait_for(c.request("GET", cfg.feed_path, keep=False), cfg.timeout)
                ms = (time.perf_counter() - t0) * 1000
                stat.requests += 1
                if 200 <= st < 300:
                    stat.successes += 1; stat.latencies.append(ms)
                else:
                    stat.errors += 1
            except asyncio.TimeoutError:
                stat.requests += 1; stat.errors += 1; stat.timeouts += 1
            except Exception:
                stat.requests += 1; stat.errors += 1
        finally:
            await c.close()

    stat.started = time.time()
    await asyncio.gather(*[user(i) for i in range(concurrency)], return_exceptions=True)
    stat.ended = time.time()


async def completeness(cfg, accepted):
    """Of the messages the server accepted, how many come back in /feed?"""
    p = urlparse(cfg.url)
    c = Conn(p.hostname, p.port or 80, max(60, cfg.timeout * 4))
    seen = set()
    try:
        st, data = await c.request("GET", cfg.feed_path)
        if st != 200:
            return 0, None
        if data[:2] == b"\x1f\x8b":
            import gzip as _gz
            data = _gz.decompress(data)
        d = json.loads(data)
        for m in d.get("messages", []):
            if m.get("id"):
                seen.add(m["id"])
        note = (f"/feed returned {d.get('returned', len(d.get('messages', [])))} of "
                f"{d.get('count', '?')} messages in the room")
    except Exception as e:
        return 0, f"feed read failed: {e}"
    finally:
        await c.close()
    return len(accepted & seen), note


async def main_async(cfg):
    stages = STATIC_STAGES if cfg.board == "static" else BREAK_STAGES
    if cfg.stages:
        stages = cfg.stages
    print(f"[sim] {cfg.board} board against {cfg.url}: stages {stages}, "
          f"{cfg.budget} requests each", flush=True)

    results, accepted, broke_at = [], set(), None
    for i, conc in enumerate(stages):
        stat = Stat(conc, cfg.budget)
        await run_stage(cfg, conc, cfg.budget, stat)
        accepted |= stat.accepted
        results.append(stat)
        print(f"  stage {i + 1}: {conc:>5} users  {stat.requests:>6} req  "
              f"{stat.successes:>6} ok  {stat.errors:>5} err ({stat.err_rate * 100:5.1f} %)  "
              f"{stat.timeouts:>5} timeouts  mean {stat.mean_ms:7.0f} ms  {stat.rps:6.1f} req/s",
              flush=True)
        if cfg.board == "breakpoint" and stat.err_rate > BREAK_PCT:
            broke_at = conc
            print(f"  -> broke at {conc} users (> {BREAK_PCT * 100:.0f} % errors)", flush=True)
            break
        await asyncio.sleep(cfg.rest)

    delivered, note = await completeness(cfg, accepted)
    lat = [x for s in results for x in s.latencies]
    total_req = sum(s.requests for s in results)
    total_ok = sum(s.successes for s in results)
    total_err = sum(s.errors for s in results)
    summary = {
        "run_id": cfg.run_id, "board": cfg.board, "url": cfg.url,
        "stages": [{"concurrency": s.concurrency, "budget": s.budget, "requests": s.requests,
                    "successes": s.successes, "errors": s.errors, "timeouts": s.timeouts,
                    "err_rate": round(s.err_rate, 5), "mean_ms": round(s.mean_ms, 2),
                    "rps": round(s.rps, 2)} for s in results],
        "mean_response_ms": round(statistics.mean(lat), 2) if lat else None,
        "total_requests": total_req, "total_successes": total_ok, "total_errors": total_err,
        "err_rate_overall": round(total_err / total_req, 5) if total_req else 0,
        "peak_rps": round(max((s.rps for s in results), default=0), 2),
        "break_concurrency": broke_at,
        "accepted_msgs": len(accepted), "delivered": delivered,
        "lost": len(accepted) - delivered,
        "completeness": round(delivered / len(accepted), 4) if accepted else None,
        "integrity_note": note,
    }
    print()
    print(f"  RANKING METRIC (static)     mean response time : {summary['mean_response_ms']} ms")
    print(f"  RANKING METRIC (breakpoint) successful requests: {total_ok}"
          + (f"  (broke at {broke_at} users)" if broke_at else "  (held the whole ladder)"))
    print(f"  error rate {summary['err_rate_overall'] * 100:.2f} %   peak {summary['peak_rps']} req/s")
    print(f"  completeness {summary['completeness']}  ({delivered} of {len(accepted)} accepted "
          f"messages found in /feed)")
    if note:
        print(f"  {note}")

    os.makedirs(cfg.out_dir, exist_ok=True)
    path = os.path.join(cfg.out_dir, f"{cfg.run_id}.json")
    with open(path, "w") as fh:
        json.dump(summary, fh, indent=1)
    print(f"[sim] wrote {path}")
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--board", choices=("static", "breakpoint"), default="static")
    ap.add_argument("--stages", type=int, nargs="*", default=None,
                    help="override the concurrency ladder, e.g. --stages 250 500")
    ap.add_argument("--budget", type=int, default=5000, help="requests per stage")
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--rest", type=float, default=2.0, help="seconds between stages")
    ap.add_argument("--message-path", default="/message")
    ap.add_argument("--feed-path", default="/feed")
    ap.add_argument("--gzip", action="store_true",
                    help="send Accept-Encoding: gzip, which most real HTTP clients do by default")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--out-dir", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "results", "raw"))
    cfg = ap.parse_args()
    global ACCEPT_GZIP
    ACCEPT_GZIP = cfg.gzip
    cfg.run_id = cfg.run_id or f"SIM_{cfg.board}_{int(time.time())}"
    try:
        asyncio.run(main_async(cfg))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()

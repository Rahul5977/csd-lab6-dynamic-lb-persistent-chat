# Viva preparation — Assignment 6

## 30-second pitch
"Clients hit one URL, 10.1.75.53:3269, and use two routes: POST /message with a client name and a
message, and GET /feed. Behind it my Python load balancer on sys1 gives every backend a load index —
the worst of its container CPU, the requests I am holding on it, and its EWMA response time — and
keeps sending to the current backend until that index crosses 0.70, then switches to one still under
it. 0.70 came from a sweep, not a guess. Backends on sys2, sys3 and sys4 register themselves every
5 s and the balancer also scans known slots, so a backend started mid-run joins within seconds. All
backends are stateless: every message goes to one SQLite database whose primary key is the message
id, so a retried or duplicated send is stored exactly once. Against fixed round robin the threshold
rule is +18 % throughput and −10 % p95, and the biggest single win in the whole project was finding
that the balancer and the database were sharing sys1's one CPU — moving the database to sys3 was
worth +41 %."

## Likely questions (updated task)
- **Why is round robin not enough?** It splits *requests* equally between systems that are not equally
  able to serve them. sys3 also runs the database, so an equal request share is an unequal work share.
  Measured: threshold 260 rps / 447 ms p95 vs round robin 221 rps / 494 ms.
- **What exactly is "the load" you threshold on?** `load(b) = max(cgroup CPU, in_flight/24, ewma_rt/250 ms)`,
  0 = idle, 1 = saturated. Worst-of-three so no single blind spot hides a busy backend. The in-flight
  term is measured at the balancer, so the rule reacts inside one request instead of one health interval.
- **How did you pick T = 0.70?** Swept 0.15–1.00, two reps each, at 60 clients and again at 10, changing
  it live through POST /lb/config. Scored on p95 + 0.6 × throughput shortfall + 100 × error rate, median
  over reps. T = 0.70 won both metrics. Honest caveat: the whole sweep spans about 7 %, because at 60
  clients every backend crosses every threshold immediately — the 10-client sweep is where T really bites.
- **What stops all requests jumping to the same backend when the threshold trips?** Power-of-two-choices
  among the candidates still under T. Picking the global minimum would push that one over the threshold too.
- **Does /feed really return "all messages"?** It returns the newest 200 by default and always reports the
  true total, whether it was truncated, and the window used; `?limit=all` returns the complete history.
  At 1 412 messages the full feed was already 383 KB / 190 ms and it grows linearly, so an unbounded
  default would measure the size of the history rather than the system. Nothing is unreachable.
- **Did you simplify the app for the leaderboard?** No — /message and /feed are additions. Registration,
  scrypt login, sessions, end-to-end encrypted rooms and the whole /api surface still work through the
  same URL, and the suites that cover them grew from 47 to 60 and 30 to 39 assertions.
- **How do you measure the utilisation of four containers on one host?** /proc describes the whole
  physical machine, so scripts/sysmetrics.py reads each container's own cgroup v2 accounting
  (cpu.max, cpu.stat usage_usec, memory.current) once a second over one persistent SSH connection each.
- **What is the bottleneck now?** sys1. It terminates every connection and copies every response body
  through one Python process on a one-CPU container; it runs at 70–95 % while the backends sit at 40–65 %.
  A fourth backend would not help; a second core for sys1, or two balancer processes behind SO_REUSEPORT,
  would.
- **How did you prove it was throttling and not something else?** /sys/fs/cgroup/cpu.stat showed 41 of 80
  scheduling periods throttled. Ruled out the network (the same 163 rps from inside the lab), the proxy
  code (11–12 k rps in isolation, flat from 12 to 120 connections) and raw CPU speed (only 3.5× slower).

## Likely questions (original task)
- **Why response time × in-flight × load, not just response time?** Response time alone lags (EWMA) and
  herds; in-flight is the instantaneous queue; CPU catches load the balancer cannot see (another tenant).
- **Why power-of-two choices?** Pure minimum herds with light traffic (all in-flight = 0). P2C keeps equal
  backends even and starves a slow one to ~1/N². Used by Finagle/Linkerd/Envoy.
- **How does the LB detect a new backend?** Three ways: backend POSTs /lb/register with a token (5 s heartbeat);
  LB probes candidate slots every 5 s and admits any that answers with the right app version; config file watch.
- **How does it stop sending to a dead backend?** Active probe every 3 s, 2 misses → DOWN (except the last
  one: fail-open); passive: a connect failure mid-proxy ejects at once and retries elsewhere if nothing was sent.
- **Slow vs dead?** DEGRADED state: slow backends stay routable with a doubled score. Fixes Lab 5's churn.
- **Where is the id minted and why?** Client (UUID v4) when composing, reused on retry. Only the client knows
  two sends are the same message. Server mints one only if none given.
- **What if two backends get the same id at once?** SQLite: one process, `BEGIN IMMEDIATE`, `INSERT … ON CONFLICT(id)
  DO NOTHING`; the loser gets the stored row and `duplicate:true`; 20-way storm test → 1 row.
- **Why SQLite?** No root / no package install on the lab boxes; node:sqlite ships in Node 22; real constraints,
  WAL durability, one file. Single point of failure is acknowledged (future: replication/Postgres).
- **Is the LB a bottleneck?** Lab 5 calibration: loadgen 15 900 req/s vs echo; LB is threads + pooled upstream
  sockets on 1 core; measured LB overhead ~3–19 % (Lab 5).
- **Why interleave 1/2/3 backends?** Shared multi-tenant host drifts by the hour; interleaving makes comparisons fair.
- **Previous assignment still works?** Yes — same URL serves the LB (superset), 3270/3271 direct backends answer,
  Lab 5 files untouched, rollback_lab5.sh.

## Demo order (scripts/demo.sh)
sanity → the two required routes (/message in three encodings, the same id twice, /feed) → scale down to 1
→ add sys3, sys4 under load (dashboard events) → CPU-hog sys3: threshold vs round robin, with the live
load index shown → dedup_test → restart DB + backends, count unchanged → kill sys3, LB ejects, restart,
re-admit.

## Numbers worth having ready
| | |
|---|---|
| threshold vs round robin, 60 clients, 3 backends | 260 vs 221 req/s · 447 vs 494 ms p95 |
| chosen threshold | T = 0.70 (index of cpu / in-flight over 24 / EWMA over 250 ms) |
| knee of the 3-backend curve | 50 clients, 259 req/s |
| 200 req/s offered, open loop | 1 backend: 8.7 s p95 and failing · 3 backends: 3.1 s, zero failures |
| moving the database off sys1 | 165 → 233 req/s (+41 %), p50 −29 %, p95 −24 % |
| sys1 throttling before the move | 41 of 80 scheduling periods |
| utilisation at 50 clients | sys1 73 % · sys2 44 % · sys3 58 % · sys4 48 % |
| test suites | 60 backend assertions, 39 balancer assertions, 5 dedup scenarios — all pass |

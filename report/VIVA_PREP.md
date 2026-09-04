# Viva preparation — Assignment 6

## 30-second pitch
"Clients hit one URL, 10.1.75.53:3269, which is my Python load balancer on sys1. It scores every backend
continuously — EWMA response time × (1 + in-flight) × (1 + container CPU) — and picks with power-of-two
choices, so traffic follows real performance instead of a fixed rotation. Backends on sys2, sys3, sys4
register themselves at boot and every 5 s; the balancer also scans known slots, so I can start a backend
while the load generator is running and it joins within seconds. All backends are stateless: every
message goes to one SQLite database on sys1 whose primary key is a client-minted UUID, so a retried or
duplicated send is stored exactly once. Adding backends took throughput from ~100 to ~190 to ~285 req/s;
a killed backend is ejected within seconds with 0.1 % failed requests and re-admitted on restart."

## Likely questions
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
sanity → scale down to 1 → add sys3, sys4 under load (dashboard events) → CPU-hog sys3: adaptive vs RR
→ dedup_test → restart DB + backends, count unchanged → kill sys3, LB ejects, restart, re-admit.

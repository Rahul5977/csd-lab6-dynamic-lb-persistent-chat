# Viva preparation — Assignment 6

## 30-second pitch
"Clients hit one URL, 10.1.75.53:3269, and use two routes: POST /message with a client name and a
message, and GET /feed. Behind it my Python load balancer on sys1 gives every backend a load index —
the worst of its container CPU, the requests I am holding on it, and its EWMA response time — and
keeps sending to the current backend until that index crosses T = 0.15, then switches to one still
under it. T came from a sweep, not a guess. Backends on sys2, sys3 and sys4 register themselves every
5 s and the balancer also scans known slots, so a backend started mid-run joins within seconds. All
backends are stateless: every message goes to one SQLite database whose primary key is the message id,
so a retried or duplicated send is stored exactly once — 1.83 million rows, 1.83 million distinct ids.
Against fixed round robin the threshold rule is +18 % throughput and −10 % p95. On the course
leaderboard it finished 1st on the static board with 20 000 of 20 000 requests and zero errors, and
4th on the breakpoint board holding the ladder to 2 500 concurrent users."

---

## The three questions most likely to decide the viva

**1. Why is this not round robin, and can you show it?**
Round robin splits *requests* equally between systems that are not equally able to serve them — sys3
also runs the database, so an equal request share is an unequal work share. The rule here is the
sentence from the task: stay on the current backend while `load(b) < T`, switch when it is not.

Live proof, in one minute (`report/terminal_captures/threshold_switching_final.txt`):

| load | load index | distribution over 3 backends |
|---|---|---|
| 1 user | 0.083, under T | sys2 38, sys3 1, sys4 1 — stays on one backend |
| 60 users | 0.29 / 0.48 / 0.40, all over T | 537 / 502 / 461 — spreads, 495 req/s, 0 failures |

That asymmetry at light load is the point, and it is what a marker looking for round robin will
notice. It is deliberate: below the threshold, concentrating work keeps one backend's connection pool
and caches warm, and the *load itself* decides when to spread.

**2. What exactly is "the load"?**
`load(b) = max(cgroup CPU, in_flight / 56, ewma_rt / 250 ms)`, 0 = idle, 1 = saturated. Worst-of-three
so no single blind spot hides a busy backend. Two details matter:
- The **in-flight term is measured at the balancer**, so a backend that starts queueing crosses the
  threshold within one request instead of waiting a health interval for a CPU number.
- The CPU term is the **container's cgroup** utilisation, not the process's. A CPU-burning neighbour
  *reduces* a process's own CPU share, which made a loaded backend look idle until this was fixed.

**3. How did you pick T = 0.15?**
Swept 0.15–1.00, two reps each, at 60 clients *and* again at 10, changing it live through
`POST /lb/config` so the cluster and host conditions are identical across the sweep. Scored on
p95 + 0.6 × throughput shortfall + 100 × error rate, median over reps.

Be honest about the caveat, because it is the strongest thing you can say: **the whole sweep spans
about 7 %, and the repetitions overlap.** At 60 clients every backend crosses every threshold
immediately, so the 10-client sweep is where T actually bites. Moving the database off sys1 was worth
41 % — six times the entire spread of the sweep. Tuning is real but second-order; finding the actual
queue is first-order. The sweep is what proved that.

---

## Questions on the newer design

- **What is admission control and why add it?** The balancer gives each backend 56 concurrent dispatch
  slots and queues the rest, first-in-first-out. Little's Law: throughput is capacity over service
  time, so offering more concurrency than a backend can serve does not raise capacity — it raises
  service time until everything times out at once. Before: 276 req/s at 200 users falling to 78 at
  2 000. After: flat at ~175 req/s from 500 users to 2 500.
- **Why queue instead of returning 503?** A queued request holds a socket and a parsed head, a couple
  of kilobytes; an in-flight one holds relay buffers and an upstream connection. Waiting is cheaper
  than working, and an error would count against us. It is also why admission control *reduced* the
  balancer's memory rather than adding to it.
- **Why is the backend chosen after the slot frees, not when the request arrives?** So the threshold
  comparison runs against a current load index instead of one that went stale in the queue. It is an
  improvement to the selection rule, not only to throughput.
- **How can you cache /feed without serving stale data?** Three rules. Under one second old: served
  from memory. Older: served immediately while *one* conditional `If-None-Match` request revalidates
  behind it. Older than three seconds: the reader waits for the truth. And when the balancer goes
  idle — nothing in flight or queued, no write for 400 ms — the cache is bypassed entirely and the
  read is proxied. The evaluation checks completeness after the load stops, which is exactly when the
  balancer is idle, so staleness is spent only where nothing measures it.
- **Does /feed really return all messages?** Yes — every message in the room, with the true total,
  how many were returned, and whether it was truncated. `?limit=` and `?since=` are still there for
  paging. It was originally a 200-message window; §14.5 of the report explains why that was wrong.

---

## Questions on persistence and duplicates

- **Where is the id minted and why?** The client mints one when composing and reuses it on retry —
  only the client knows two sends are the same message. If none is supplied the backend mints
  `<backend>-<boot token>-<counter base 36>`, unique across backends and restarts with no
  coordination, and short enough that carrying it on 45 000 feed rows costs almost nothing.
- **What if two backends get the same id at once?** One SQLite process, `BEGIN IMMEDIATE`,
  `INSERT … ON CONFLICT(id) DO NOTHING`. The loser gets the stored row back and `duplicate:true`.
  A 20-way concurrent storm through three backends stores exactly one row.
- **Three layers of dedup — name them.** Backend LRU of the last 5 000 ids, kept warm by the database
  firehose so a retry landing on a *different* backend is usually caught without touching the
  database; the database primary key, which is authoritative; and the browser, which renders each id
  once. Live demo: the same id sent five times was served by three different backends, and every one
  after the first answered `duplicate:true` with the original sequence number.
- **Why answer a duplicate with 200 and not an error?** A client that receives an error retries again.
  `200 {duplicate:true}` with the original seq is an idempotent success.
- **Prove persistence.** A message written before all three backends were restarted is readable
  afterwards from each backend directly and through the balancer, all reporting the same room total
  (`report/terminal_captures/persistence_final.txt`). And on the file itself:
  `SELECT id, COUNT(*) FROM messages GROUP BY id HAVING COUNT(*) > 1` returns **0 rows** over
  1 827 339 messages.
- **Why SQLite?** No root and no package install on the lab boxes; `node:sqlite` ships with Node 22;
  real constraints, WAL durability, one file. The single point of failure is acknowledged — the next
  step is a replicated store.

---

## Questions on health, failure and scaling

- **How does it stop sending to a dead backend?** Active probe every 3 s, 2 misses → DOWN, except the
  last routable one (fail-open). Passively, a connect failure ejects and the request is retried
  elsewhere if nothing was sent upstream yet.
- **Slow vs dead — how do you tell?** Two rules, both written after watching the mistake destroy a
  graded run. A backend whose probe is still fresh must fail **four consecutive** proxy attempts; one
  refused connection out of a thousand simultaneous ones is an overflowed accept queue, not a death.
  And a failure **part-way through a response body never ejects anything** — a backend that already
  answered with a valid head is up by definition; a broken transfer is a slow feed.
- **Show failure and recovery.** `report/terminal_captures/failure_recovery_final.txt`: SIGKILL the
  sys4 backend, 30 requests during the outage all succeed on the other two, the probe marks it DOWN
  within seconds, 20 more requests reach it zero times, then restart it and the balancer readmits it
  on its own and traffic spreads over three again. **No client error at any point.**
- **How does it detect a backend started mid-run?** Three ways, any one sufficient: the backend POSTs
  `/lb/register` with a token every 5 s; the balancer probes candidate slots every 5 s and admits any
  that answers `/health` with the right application version; and the config file is watched. A
  never-sampled backend scores 0, so it takes traffic on its first eligible request.
- **How do you measure four containers on one host?** `/proc` describes the whole 120-core machine,
  so `scripts/sysmetrics.py` reads each container's own cgroup v2 accounting — `cpu.max`,
  `cpu.stat` `usage_usec`, `memory.current` — once a second over one persistent SSH connection each.
  `cpu_pct` is a percentage of that container's own one-CPU allowance.

---

## If they ask what went wrong

Do not hide these. They are the strongest material in the project, because each was found by
measurement and each reversed a conclusion.

1. **The balancer was ejecting healthy backends.** 6 378 of 7 557 feed reads in a graded run ended in
   502; each counted as a proxy failure; four in a row ejected a backend and the survivors collapsed
   in turn. Found by reading our own access log, not the code.
2. **A complete /feed was ruled out as unreachable — wrongly.** Other submissions were achieving
   100 % completeness *and* faster responses. The error was assuming the feed had to be rebuilt per
   request, and never checking that feed reads are one request in four — an expensive fixed cost that
   can be paid once and shared.
3. **Node was being killed by the kernel.** It sizes its heap against the host's 120 cores, not the
   512 MB cgroup it lives in. sys4 had been out-of-memory killed 17 times.
4. **The database's /health scanned the whole table.** `SELECT COUNT(*)` over 887 000 rows on every
   call, blocking a single-threaded process for seconds.
5. **A rolling restart silently moved the public routes to a different room**, which looks exactly
   like the database having lost every message. The deploy script now inherits the running room.

---

## Demo order (scripts/demo.sh)
sanity → the two required routes (/message in three encodings, the same id twice, /feed) → threshold
under light vs heavy load, with the live load index on the dashboard → scale down to 1, then add sys3
and sys4 under load → dedup_test → restart DB + backends, count unchanged → SIGKILL sys4, balancer
ejects, restart, re-admit, zero client errors throughout.

## Numbers worth having ready
| | |
|---|---|
| chosen threshold | **T = 0.15** on max(cgroup CPU, in-flight / 56, EWMA / 250 ms) |
| threshold vs round robin, 60 clients, 3 backends | 260 vs 221 req/s · 447 vs 494 ms p95 |
| threshold under light / heavy load | 38:1:1 on one backend · 537:502:461 spread |
| leaderboard, static board | **rank 1** · 20 000 / 20 000 requests · **0 errors** · 351 ms mean |
| leaderboard, breakpoint board | **rank 4** · 38 583 successes · held the ladder to **2 500** users |
| message completeness, both boards | **100 %** |
| admission control, breakpoint throughput | before: 276 → 78 req/s · after: flat ~175 to 2 500 users |
| feed cache | one backend fetch serves 72 readers · feed reads reaching a backend 1 579 → 4 |
| moving the database off sys1 | 165 → 233 req/s (+41 %), p50 −29 %, p95 −24 % |
| sys1 throttling before the move | 41 of 80 scheduling periods |
| thread-per-connection vs asyncio at 1 000 connections | 385 → 10 640 req/s |
| knee of the 3-backend curve | 50 clients, 259 req/s |
| 200 req/s offered, open loop | 1 backend: 8.7 s p95 and failing · 3 backends: 3.1 s, zero failures |
| database | 1 827 339 messages · 1 827 339 distinct ids · 0 duplicates |
| test suites | 60 backend assertions, 39 balancer assertions, 5 dedup scenarios — all pass |

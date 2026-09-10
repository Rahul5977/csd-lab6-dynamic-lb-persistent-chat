# Assignment 6 — Dynamic Load Balancing and Persistent Chat

Extends the Assignment-5 load-balanced secure group chat with **performance-threshold backend
selection**, **live backend discovery** (scale up/down while running), a **persistent SQLite
database** shared by every backend, and **duplicate-proof message ids**. Individual assignment,
CSD course — Rahul Raj, 12341680.

## Submission

| | |
|---|---|
| **Load balancer URL** | **http://10.1.75.53:3269** |
| **Submit a message** | `POST /message` with `client-name` and `msg` |
| **Read the feed** | `GET /feed` (newest 200 by default; `?limit=all` for everything) |
| Chat application | `/` — register, log in, end-to-end encrypted rooms |
| Dashboard | `/lb/` · JSON at `/lb/stats`, `/lb/events` |
| Report | [report/REPORT_12341680.pdf](report/REPORT_12341680.pdf) |

```bash
curl -X POST http://10.1.75.53:3269/message -H 'Content-Type: application/json' \
     -d '{"client-name":"rahul","msg":"hello"}'
curl http://10.1.75.53:3269/feed
```

```
 client (browser / loadgen)  ──►  LB  sys1:3000 (public 3269)  ──►  sys2:3000  sys3:3000  sys4:3001
    /message  /feed  /  /api/*        threshold rule: stay while           │          │         │
                                      load(b) < T = 0.70, else switch      │          │         │
                                      load = max(cpu, in-flight/24, rt/250ms)         │         │
                                      register / scan / UP-DEGRADED-DOWN-DRAINING     │         │
                                                                           └──────────┴─────────┘
                                                                                      ▼
                                                              DB service sys3:5270 — SQLite, id PRIMARY KEY
```

The database runs on sys3, not next to the load balancer: with both on sys1 the container was
CFS-throttled in half of all scheduling periods and the cluster stalled at 165 req/s. Moving it
was worth +41 % throughput — see `DECISIONS.md` D-010 and §13 of the report.

## Under the evaluation's load

The course leaderboard drives 250 → 1500 concurrent users through `/message` and `/feed`. Against a
local copy of its ladders (`loadgen/leaderboard_sim.py`):

| | result |
|---|---|
| static ladder (250 → 1000 users, 20 000 requests) | **mean 547 ms, 0 failures**, peak 1 148 req/s |
| breakpoint ladder (200 → 1500 users) | **34 300 successful requests, held to 1 500 users**, 0 failures |
| at 1 000 concurrent users, before → after | 368 → 787 req/s, 2 602 → 1 163 ms mean |

Four changes got it there, each isolated by measurement (report §14): the balancer's I/O layer moved
onto **asyncio** (a thread per connection collapsed from 10 000 req/s to 385 between 500 and 1 000
connections), **conservative passive ejection** so a connection burst is not mistaken for a dead
backend, a **keep-alive HTTP client** to the database instead of `fetch()`, and **group commit** in
SQLite — 20 appends per transaction under load, with the duplicate guard unchanged.

## Layout

| Path | What |
|---|---|
| `app/db_service.js` | SQLite (WAL, `node:sqlite`) database service: users, sessions, rooms, messages with `id PRIMARY KEY` + `UNIQUE(room,seq)`, idempotent append, duplicate audit log, WS firehose |
| `app/server.js` | Backend v3: client message ids / `Idempotency-Key`, per-backend dedup LRU, load-reporting `/health`, self-registration + heartbeat + deregister with the LB; auth + E2E crypto unchanged from Assignments 4/5 |
| `app/static/` | Chat UI: optimistic bubbles, retry with the same id, "Resend last" duplicate demo, live cluster panel |
| `app/tests/smoke.js` | 60 end-to-end assertions (dedup races, persistence across DB restart, registration, the `/message` + `/feed` routes) |
| `lb/loadbalancer.py` | Dynamic LB (Python 3 stdlib, **asyncio**): **threshold** selection on a load index, adaptive P2C scoring, UP/DEGRADED/DOWN/DRAINING, register/discovery/config-watch, dashboard, events |
| `lb/test_lb.py` | 39 LB integration assertions (registration, scan, slow-backend scoring, kill/recover, drain, all algorithms, the threshold rule, `/message` + `/feed` through the LB) |
| `loadgen/loadgen.py` | Load generator: `--api public` (the required routes) or `--api chat`; closed / open / ramp; random message length and random inter-message interval; 1-s timeseries incl. active backends |
| `loadgen/leaderboard_sim.py` | A local copy of the course leaderboard's own two ladders (250→1000 and 200→1500 concurrent users), reporting its three metrics including message completeness |
| `scripts/sysmetrics.py` | Per-container CPU and memory of **all four systems**, once a second (cgroup v2 — `/proc` here describes the whole host) |
| `scripts/` | `deploy.sh`, `scale.sh` (add/remove/kill a backend live), `cpu_hog.sh`, `run_experiments.sh`, `dedup_test.py`, `analyze.py`, `build_report.py`, `sanity_check.sh`, `rollback_lab5.sh`, `demo.sh` |
| `results/` · `evidence/` · `report/` | raw runs (JSON), processed CSV, charts, tables, evidence captures, the PDF report |

## Run

```bash
bash scripts/deploy.sh all               # Node 22, DB on sys3, backend on sys2, LB on sys1
bash scripts/deploy.sh movedb sys1 sys3  # move the database (data included) between systems
bash scripts/scale.sh sys3 up            # add a backend while running (self-registers; also found by the scan)
bash scripts/scale.sh sys4 up
bash scripts/sanity_check.sh             # everything green? (also checks the previous assignments' URLs)

# load generation — variable users, random message lengths, random intervals
python3 loadgen/loadgen.py --url http://10.1.75.53:3269 --api public \
        --concurrency 60 --duration 45 --msg-min 8 --msg-max 240 \
        --think-min 0 --think-max 0.05 --poll-stats
python3 scripts/sysmetrics.py --run-id demo --duration 60   # utilisation of all four systems
python3 scripts/dedup_test.py --url http://10.1.75.53:3269 --n 20

bash scripts/run_experiments_v2.sh    # updated-task matrix (~90 min): THR, THRLOW, ALGO, PUB, OPEN, UTIL, PFAIL
.venv/bin/python scripts/analyze_v2.py
.venv/bin/python scripts/build_report.py
```

Change the switching threshold on the running balancer:

```bash
curl -X POST http://10.1.75.53:3269/lb/config \
     -d '{"algorithm":"threshold","switch_threshold":0.7}'
```

Local development: `node app/tests/smoke.js`, `python3 lb/test_lb.py`, `bash scripts/local_cluster.sh start`.

## Previous assignments are untouched

Lab 5's code and remote directory (`~/assignment5`) are never modified; `scripts/rollback_lab5.sh`
restores its LB and backends in ~20 s. sys4 port 3000 belongs to the Contour project, so the
sys4 backend listens on 3001 (only the LB needs a public port).

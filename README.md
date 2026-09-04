# Assignment 6 — Dynamic Load Balancing and Persistent Chat

Extends the Assignment-5 load-balanced secure group chat with **dynamic backend
selection** (response time + system load), **live backend discovery** (scale up/down while
running), a **persistent SQLite database** shared by every backend, and **duplicate-proof
message ids**. Individual assignment, CSD course — Rahul Raj, 12341680.

**Public endpoint:** http://10.1.75.53:3269 (dynamic LB on sys1 → backends on sys2, sys3, sys4).
Dashboard: http://10.1.75.53:3269/lb/ · stats: `/lb/stats` · events: `/lb/events`.

```
 Mac (browser / loadgen)  ──►  LB v2  sys1:3000 (public 3269)  ──►  sys2:3000  sys3:3000  sys4:3001
                                 adaptive: EWMA rt × (1+in-flight) × (1+cpu)      │          │         │
                                 register / scan / health states                  └──────────┴─────────┘
                                                                                             ▼
                                                                          DB service sys1:5270 — SQLite, id PRIMARY KEY
```

## Layout

| Path | What |
|---|---|
| `app/db_service.js` | SQLite (WAL, `node:sqlite`) database service: users, sessions, rooms, messages with `id PRIMARY KEY` + `UNIQUE(room,seq)`, idempotent append, duplicate audit log, WS firehose |
| `app/server.js` | Backend v3: client message ids / `Idempotency-Key`, per-backend dedup LRU, load-reporting `/health`, self-registration + heartbeat + deregister with the LB; auth + E2E crypto unchanged from Assignments 4/5 |
| `app/static/` | Chat UI: optimistic bubbles, retry with the same id, "Resend last" duplicate demo, live cluster panel |
| `app/tests/smoke.js` | 48 end-to-end assertions (dedup races, persistence across DB restart, registration) |
| `lb/loadbalancer.py` | Dynamic LB v2 (Python 3 stdlib): adaptive P2C scoring, UP/DEGRADED/DOWN/DRAINING, register/discovery/config-watch, dashboard, events |
| `lb/test_lb.py` | 33 LB integration assertions (registration, scan, slow-backend scoring, kill/recover, drain, all algorithms) |
| `loadgen/loadgen.py` | Load generator v2: closed loop, open loop (offered load), ramp schedule, 1-s timeseries incl. active backends |
| `scripts/` | `deploy.sh`, `scale.sh` (add/remove/kill a backend live), `cpu_hog.sh`, `run_experiments.sh`, `dedup_test.py`, `analyze.py`, `build_report.py`, `sanity_check.sh`, `rollback_lab5.sh`, `demo.sh` |
| `results/` · `evidence/` · `report/` | raw runs (JSON), processed CSV, charts, tables, evidence captures, the PDF report |

## Run

```bash
bash scripts/deploy.sh all            # Node 22 + DB on sys1, backend on sys2, LB v2 on sys1
bash scripts/scale.sh sys3 up         # add a backend while running (self-registers; also found by the scan)
bash scripts/scale.sh sys4 up
bash scripts/sanity_check.sh          # everything green? (also checks the previous assignments' URLs)
python3 loadgen/loadgen.py --url http://10.1.75.53:3269 --concurrency 50 --duration 30 --poll-stats
python3 scripts/dedup_test.py --url http://10.1.75.53:3269 --n 20
bash scripts/run_experiments.sh       # full matrix (~80 min)  → .venv/bin/python scripts/analyze.py
.venv/bin/python scripts/build_report.py
```

Local development: `node app/tests/smoke.js`, `python3 lb/test_lb.py`, `bash scripts/local_cluster.sh start`.

## Previous assignments are untouched

Lab 5's code and remote directory (`~/assignment5`) are never modified; `scripts/rollback_lab5.sh`
restores its LB and backends in ~20 s. sys4 port 3000 belongs to the Contour project, so the
sys4 backend listens on 3001 (only the LB needs a public port).

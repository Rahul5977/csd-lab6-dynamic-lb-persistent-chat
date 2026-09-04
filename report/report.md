<div class="cover">
<h1>Dynamic Load Balancing and Persistent Chat</h1>
<h3>CSD Course — Lab Assignment 6</h3>
<table>
<tr><td><strong>Student Name</strong></td><td>Rahul Raj</td></tr>
<tr><td><strong>Roll Number</strong></td><td>12341680</td></tr>
<tr><td><strong>Date</strong></td><td>{{DATE}}</td></tr>
<tr><td><strong>Public URL</strong></td><td>http://10.1.75.53:3269 (dynamic load balancer on sys1; dashboard at /lb/)</td></tr>
<tr><td><strong>Source code</strong></td><td>https://github.com/Rahul5977/csd-lab6-dynamic-lb-persistent-chat</td></tr>
</table>
</div>

<div class="pagebreak"></div>

## 1. Assigned Systems and Ports

| Role | Alias | SSH | Container IP | Service port(s) | External port |
|---|---|---|---|---|---|
| Load balancer + database service | stu78_sys1 | `ssh -p 2269 student@10.1.75.53` | 172.17.0.70 | LB 3000 · SQLite DB service 5270 | **3269 → LB (the only URL clients use)** |
| Backend A | stu78_sys2 | `ssh -p 2270 student@10.1.75.53` | 172.17.0.71 | app 3000 | 3270 (direct, kept for the previous assignment) |
| Backend B | stu78_sys3 | `ssh -p 2271 student@10.1.75.53` | 172.17.0.72 | app 3000 | 3271 (direct, previous assignment) |
| Backend C | stu78_sys4 | `ssh -p 2272 student@10.1.75.53` | 172.17.0.73 | app **3001** | — (port 3000 / external 3272 belongs to my course project) |

The four systems are Ubuntu 24.04 containers on one shared lab host, each limited by cgroup to
**exactly one CPU core**. The lab NAT forwards only the first external port of each system to
container port 3000. Clients — the browser and the load generator on my Mac — talk only to
`10.1.75.53:3269`; the load balancer reaches the backends over the private bridge network, so a
backend does not need a public port at all (which is why sys4's backend can live on 3001).
The load generator runs on my local machine.

## 2. Objective

> Extend the previous secure group-chat application and deploy its backend on at least three allotted
> systems behind a load balancer. The load balancer must select a backend **dynamically** using
> response time and/or current system load, monitor backend health and stop sending to unavailable
> backends, and **automatically detect backends started while the application is running**. All backends
> must share **persistent** chat data, every message must have a **unique id**, and duplicate insertion
> must be prevented on retries/reconnections. Generate increasing load, record response time,
> throughput, successful/failed requests and the number of active backends, and analyse the results.

Everything above is implemented, deployed on the allotted systems, measured, and demonstrated in this
report. The base is my Assignment-5 system (Python load balancer, Node.js chat backend with the
Assignment-4 end-to-end encryption); Assignment 6 adds a second-generation balancer, a real database,
and idempotent messaging. The previous assignment keeps working: its files and directories on the lab
systems are untouched and `scripts/rollback_lab5.sh` restores its processes in twenty seconds.

## 3. Architecture

```
   my Mac: browser tabs + loadgen/loadgen.py  ──── only http://10.1.75.53:3269 ────┐
                                                                                   ▼
 ┌────────────────────────────────────────────────────────────────────────────────────────┐
 │  DYNAMIC LOAD BALANCER  sys1:3000  (lb/loadbalancer.py — Python 3 stdlib, ~640 lines)   │
 │  adaptive selection: score = EWMA(response time) × (1 + in-flight) × (1 + system load)  │
 │  health monitor UP / DEGRADED / DOWN / DRAINING · passive ejection · fail-open           │
 │  membership: POST /lb/register (heartbeat) · candidate scan · config watch              │
 │  /lb/stats · /lb/events · /lb/ dashboard · CSV access log                               │
 └──────────┬──────────────────────────────┬───────────────────────────────┬──────────────┘
            ▼                              ▼                               ▼   (172.17.0.0/16)
   backend sys2:3000              backend sys3:3000                backend sys4:3001
   app/server.js v3 (Node 20): stateless; client message ids; per-backend dedup LRU;
   /health reports CPU / cgroup CPU / in-flight / event-loop lag; registers itself with the LB
            │                              │                               │
            └──────────────────────────────┼───────────────────────────────┘
                                           ▼
 ┌────────────────────────────────────────────────────────────────────────────────────────┐
 │  DATABASE SERVICE  sys1:5270  (app/db_service.js — Node 22, node:sqlite, WAL)           │
 │  users · sessions · rooms · messages(id PRIMARY KEY, UNIQUE(room,seq)) · dedup_log       │
 │  POST /messages is idempotent (INSERT … ON CONFLICT(id) DO NOTHING in one transaction)  │
 │  WebSocket firehose → every backend delivers new messages to its own connected clients  │
 └────────────────────────────────────────────────────────────────────────────────────────┘
```

**Request lifecycle.** A request arrives at the balancer, which picks a backend with the adaptive
algorithm (§4), adds `X-Forwarded-For` / `X-Real-IP` and proxies it over a pooled keep-alive
connection. The backend validates the session (30 s local cache, source of truth in the database),
performs the operation against the database service and answers with `X-Backend-Id` (which
instance served it) — the balancer adds `X-LB-Active-Backends` (how many backends were routable at
that moment); the load generator records both on every request. A new message is appended once
to SQLite; the database broadcasts it on a WebSocket firehose and every backend pushes it to its own
connected browsers, so three stateless backends behave as one application. Browser WebSockets are
pinned to a backend by IP-hash and tunnelled byte-for-byte.

**What is inherited from Assignments 4/5 unchanged:** scrypt password hashing with constant-time
compare and login rate limiting; end-to-end encrypted "locked" rooms (PBKDF2-HMAC-SHA-256 with
250 000 iterations → AES-256-GCM, fresh IV per message, AAD binding room and key epoch, key
derived in the browser — the server stores only ciphertext); the WebSocket delivery path; the
clean single-page UI.

## 4. Load Balancer Design (sys1)

`lb/loadbalancer.py` — pure Python 3 standard library (full source in §5). It is the Assignment-5
balancer extended in four areas.

### 4.1 Dynamic backend selection

Every backend carries live bookkeeping: an **exponentially weighted moving average of its response
time** (`ewma_ms`, α = 0.2, fed by every proxied request *and* by the health probe's own latency, so
an idle backend keeps a fresh estimate), its **in-flight request count**, and the **system load it
reports** in `/health` (process CPU %, container-wide cgroup CPU % against its quota, 1-minute load
average, event-loop lag). The score is

```
score(b) = EWMA_rt(b) × (1 + in_flight(b)) × (1 + load_weight × cpu_load(b)) / weight(b)
           (× 2 while the backend is DEGRADED; 0 for a backend that has never been sampled)
```

and the `adaptive` algorithm (default) uses **power-of-two-choices**: sample two backends at random,
keep the lower score. This is the standard remedy for the herding problem of a pure "pick the
minimum" rule — with sequential traffic every backend has zero in-flight requests and a pure minimum
sends everything to whichever backend happened to have the lowest EWMA; with two random choices,
equal backends split evenly while a slower or more loaded backend loses every pairing it is drawn
into and is left with about 1/N² of the traffic until its score improves. A never-sampled backend
scores 0, so a **newly added backend receives traffic immediately**; a 5 % exploration share keeps
every backend measured. `least_response_time` (pure minimum, no load term) and the four
Assignment-5 algorithms (`round_robin`, `least_connections`, `weighted_round_robin`, `ip_hash`)
remain selectable — the algorithm comparison in §9 uses that.

### 4.2 Health monitoring

A background thread probes every backend's `/health` every 3 s (6 s timeout) and reads the load
JSON. States instead of a boolean:

| State | Meaning | Routable? |
|---|---|---|
| UP | answering in time | yes |
| DEGRADED | answering, but probe slower than 2 s or reported CPU ≥ 95 % | yes, score doubled |
| DOWN | connection refused / timeouts for 2 consecutive checks; back to UP after 2 successes | no |
| DRAINING | asked to leave (`/lb/deregister`): finishes in-flight, gets nothing new | no |

*Passive* checks eject a backend immediately when a live proxy attempt cannot connect; the request
is then retried on another backend (only if nothing was sent upstream yet — a POST can never be
duplicated by the balancer, and even if it were, the message id makes it harmless). **Fail-open:**
the last routable backend is never ejected — a slow-but-alive backend beats an empty pool.
"Slow ≠ dead" fixes the ejection churn I diagnosed in Assignment 5 (a saturated 1-core backend
answered its health check late, was ejected, and the survivors collapsed in turn).

### 4.3 Dynamic membership — detecting backends started while running

Three mechanisms, any one of which is sufficient:

1. **Self-registration (push).** Each backend POSTs `/lb/register {id, host, port, weight}` with a
   shared token at boot and every 5 s as a heartbeat. An unknown backend is admitted on the spot
   (`added` event). On SIGTERM the backend POSTs `/lb/deregister` and drains — graceful scale-down.
2. **Candidate scan (pull).** `lb.conf.json` lists candidate slots (sys3:3000, sys4:3001). Every 5 s
   the balancer probes the ones it does not know; any that answers `/health` with the expected
   application version is admitted (`source = scan`). This found a backend even when the
   backend's own registration was disabled — and, during development, it correctly *refused* the
   old Assignment-5 backend still running on sys3 because it reports a different version.
3. **Config watch.** The configuration file is re-read whenever its modification time changes (also
   `SIGHUP` and `POST /lb/reload`); `POST /lb/config` switches the algorithm or tuning at runtime.

Dynamically discovered backends that stay DOWN for 15 min are pruned; statically configured ones
are kept as DOWN.

### 4.4 Observability

`/lb/stats` (per-backend state, source, score, EWMA, probe latency, load, in-flight, counts,
percentiles, plus `active_backends`), `/lb/events` (timeline of added / degraded / ejected /
readmitted / draining / removed / config events), the auto-refreshing dashboard at `/lb/`
(screenshot §11), and a CSV access log with the active-backend count on every line.

## 5. Load Balancer — Full Source Code

{{LB_CODE}}

## 6. Database Persistence and Duplicate Prevention

### 6.1 Persistent shared storage

All backends are stateless; every user, session, room and message lives in **one SQLite database
(WAL mode)** owned by `app/db_service.js` on sys1 and reached over HTTP by every backend. The
database file (`~/assignment6/data/chat.sqlite`) survives restarts of any backend, of the database
service, and of the whole cluster — verified in the smoke test (48 assertions, including a database
restart mid-test) and live (§9.5). Assignment 5's data (717 users, 5 rooms, 111 180 messages) was
migrated in on first boot, so the old accounts still work. SQLite was chosen because the lab boxes
allow no root and no package installation; Node 22's built-in `node:sqlite` needs nothing but a
user-local Node binary, and a real relational engine gives real constraints.

### 6.2 Unique message ids

Every message has a **UUID v4 id minted by the client when the message is composed** and reused on
every retry (body field `id` or `Idempotency-Key` header). Only the client knows that two sends are
"the same message" — a server-minted id cannot recognise a retry — so this is the right place for
the id. A client that sends none gets one minted by the backend (still unique; but then a retry is,
by definition, a new message).

### 6.3 How duplicates are prevented — three layers

| Layer | Where | Mechanism |
|---|---|---|
| 1 · fast path | each backend | LRU of the last 5 000 stored ids (also fed by the firehose, so a retry that lands on a *different* backend is usually caught here too) → `200 {duplicate:true}` without touching the database |
| 2 · authoritative | database | `messages.id TEXT PRIMARY KEY`; `POST /messages` runs `SELECT … ; INSERT … ON CONFLICT(id) DO NOTHING` inside one `BEGIN IMMEDIATE` transaction. Two backends sending the same id at the same instant are serialised; the loser gets the stored row back with `duplicate:true` and the rejection is written to `dedup_log` |
| 3 · presentation | browser | renders each id once; a retried send that the server deduplicated never appears twice |

A duplicate is answered with **HTTP 200, `duplicate:true`, the original seq and id, and header
`X-Duplicate: 1`** — an idempotent success, never an error the client would retry again. Per-room
ordering uses `UNIQUE(room, seq)` so sequences are gap-free. Schema excerpt and the append
transaction:

{{DB_CODE}}

### 6.4 Verification through the public URL

`scripts/dedup_test.py` verifies against the **database row count**, not against what a backend
said:

{{T_DEDUP}}

The 20-way concurrent storm is the important one: twenty connections, spread by the balancer over
all three backends, POST the same id at the same instant; exactly one row exists afterwards and all
twenty responses carry the same seq. The smoke test repeats this locally and additionally proves that
a duplicate of an id stored *before* a database restart is still rejected (the guard is durable, not
in memory), and that the browser's "Resend last" button — which re-POSTs the previous message with
the same id — is answered with `duplicate:true` (screenshot §11).

## 7. Load Generator and Experimental Method

`loadgen/loadgen.py` (Python 3 stdlib, on my Mac) drives the public URL with a realistic mix per
virtual user — 10 % login (scrypt, the expensive one), 55 % fetch history, 30 % send, 5 % health.
Every send carries a client-minted id and is retried with the same id on a transport error, exactly
like the browser. Modes: **closed loop** (N users in a request→response loop), **open loop** (Poisson
arrivals at a fixed *offered* rate, independent of how slow responses get), and **ramp** (a schedule
of concurrency steps). Per request it records latency, status, error class, `X-Backend-Id`,
`X-LB-Active-Backends` and the duplicate flag; with `--poll-stats` it samples `/lb/stats` once a
second, giving the number of active backends, per-backend state, score and CPU as a time series.
The first 5 s of every run are discarded; percentiles are computed over the full sample.

| Experiment | What varies | Held constant |
|---|---|---|
| **L** response time vs load | concurrency 1, 10, 25, 50, 100, 200 × backends 1, 2, 3 (2 reps, 40 s) | adaptive algorithm; configurations interleaved in shuffled order inside every (level, rep) round so 1/2/3-backend comparisons share the same host conditions |
| **O** throughput vs offered load | offered 20, 50, 100, 200, 300 req/s (Poisson) × backends 1, 2, 3 (30 s) | as above |
| **SCALE** dynamic scaling | one 300 s run: 25 → 50 → 100 → 200 users; sys3 started at ~128 s and sys4 at ~184 s *while the run was going* | adaptive |
| **FAIL** failure / recovery | 150 s at 100 users on 3 backends; sys3 SIGKILLed at ~30 s, restarted at ~90 s | adaptive |
| **ALGO** dynamic vs static | adaptive vs round_robin vs least_connections at 50 users while a CPU-burning process runs on sys3 (2 reps) | 3 backends |

The number of backends is always changed the *dynamic* way — by starting or stopping the backend
process on the system (`scripts/scale.sh sysN up|down|kill`) — never by editing the balancer's
configuration; the balancer must notice by itself.

## 8. Results — Response Time vs Load and Throughput vs Offered Load

### 8.1 Response time vs load (closed loop)

{{T_RT}}

{{C_RT}}

{{C_TPUT_LOAD}}

{{A_RT}}

### 8.2 Throughput vs offered load (open loop)

{{T_OFFERED}}

{{C_OFFERED}}

{{A_OFFERED}}

<div class="pagebreak"></div>

## 9. Results — Dynamic Scaling, Failure Recovery, Algorithm Comparison

### 9.1 Dynamic scaling: performance after each backend is added

The system started with **one** backend (sys2). Load was raised in steps (25 → 50 → 100 users) until
the single backend was saturated; then `bash scripts/scale.sh sys3 up` and later `… sys4 up` were
executed on the other systems *while the load generator kept running*. The balancer admitted each
new backend by itself (registration heartbeat and candidate scan both fired) and the load generator's
per-second samples of `/lb/stats` show exactly when.

{{T_SCALE}}

{{C_SCALE}}

{{C_SCALE_EFFECT}}

{{A_SCALE}}

### 9.2 Backend failure and recovery under load

{{T_FAIL}}

{{C_FAIL}}

{{A_FAIL}}

### 9.3 Dynamic selection vs fixed rotation with one loaded backend

To show that selection really follows load, a CPU-burning process (`python3 -c 'while True: pass'`)
was started on sys3 — on a 1-core container it takes roughly half of the core away from the backend.
The same 50-user load was then run under each algorithm.

{{T_ALGO}}

{{C_ALGO}}

{{A_ALGO}}

### 9.4 Comparison summary

{{T_SUMMARY}}

### 9.5 Persistence and duplicate prevention on the allotted systems

{{A_PERSIST}}

<div class="pagebreak"></div>

## 10. Analysis and Discussion

{{A_DISCUSSION}}

## 11. Screenshots and Evidence

{{SCREENSHOTS}}

### Terminal captures

{{TERMINALS}}

## 12. Challenges Faced and How They Were Solved

{{A_CHALLENGES}}

## 13. Conclusion

{{A_CONCLUSION}}

## Appendix A — Reproduction

```bash
git clone https://github.com/Rahul5977/csd-lab6-dynamic-lb-persistent-chat && cd csd-lab6-dynamic-lb-persistent-chat
node app/tests/smoke.js && python3 lb/test_lb.py        # local tests (48 + 33 assertions)
bash scripts/deploy.sh all                              # Node 22 + DB on sys1, backend on sys2, LB v2 on sys1
bash scripts/scale.sh sys3 up; bash scripts/scale.sh sys4 up
bash scripts/sanity_check.sh
bash scripts/run_experiments.sh && .venv/bin/python scripts/analyze.py && .venv/bin/python scripts/build_report.py
bash scripts/demo.sh                                    # scripted live demonstration
```

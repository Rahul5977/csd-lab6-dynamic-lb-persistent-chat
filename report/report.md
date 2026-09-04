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
service, and of the whole cluster — verified in the smoke test (47 assertions, including a database
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

**Reading the table.** At one user the three configurations are identical (44–46 req/s, p50 ≈ 12 ms):
a single sequential client is bound by its own round trip Mac → lab → Mac, and no number of backends
can shorten one request. From ten users upward the backends are the bottleneck and the balancer's
value is plain: at 10 users **95 → 164 → 225 req/s** (1.7× / 2.4×) with p95 falling from 555 ms to
197 ms; at 50 users **89 → 177 → 275 req/s** (2.0× / 3.1×) with p95 5.2 s → 1.0 s; at 200 users
**85 → 189 → 268 req/s** (2.2× / 3.2×) and median response time 772 ms → 217 ms. A single 1-core
backend saturates at ~90–105 req/s regardless of how many users push it — everything beyond that
becomes queueing delay (p95 reaches 9 s at 100 users) — while two and three backends move the ceiling
to ~180 and ~270 req/s. The speed-ups slightly above 3× at 50 and 200 users are within run-to-run
noise on the shared host (the single-backend runs at those levels happened to land in busier
minutes); the honest summary is *near-linear scaling, 2.4–3.2× for three backends*. Every run had
**zero failed requests**, the adaptive algorithm split traffic 49/51 % and 33/34/33 % (it favours
nobody when the backends are equal), and `Active` confirms the balancer saw exactly the intended
number of backends throughout each run.

### 8.2 Throughput vs offered load (open loop)

{{T_OFFERED}}

{{C_OFFERED}}

**Reading the table.** In open-loop mode arrivals come at a fixed rate whether or not the system keeps
up, so the *achieved* curve bends away from the ideal line exactly where the system saturates
(Figure 3, left) and response time explodes just before it (right). Up to ~80 req/s of real offered
load all three configurations keep up — throughput follows the ideal line and p95 stays at
110–215 ms. At ~140 req/s the single backend is already over its knee: p50 352 ms and **p95 8.9 s**
(a queue that never drains) while two and three backends answer in 231 ms and 181 ms at p95. At
~230 req/s the single backend collapses — **119 req/s achieved, 41 % of arrivals failed** (2 038,
of which 797 were shed by the generator's in-flight cap because responses no longer came back), p95
7.4 s — two backends just cope (205 req/s, 0 failed, but p95 4.5 s: at capacity, queue building) and
three backends absorb it comfortably (**221 req/s, 0 failed, p95 268 ms**). The offered-load axis
uses what the generator actually issued: its thread-per-arrival Python implementation tops out at
~230 arrivals/s, which is why the nominal 300 req/s point is plotted at ~230. Successful/failed
request counts per point are in the table; the number of active backends was constant within each
run (`Active` column).

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

**How performance changed after each backend was added.** With one backend the system was already
at its ceiling at 25 users (~95 req/s, p95 ≈ 2.1 s): raising the load to 50 and then 100 users bought
*no* throughput (105 → 99 req/s) and only pushed queueing delay up — p95 climbed to 4.5 s and then 9 s,
the classic closed-loop saturation signature (Figure 4, phases A–C). At ~128 s `scale.sh sys3 up` was
run on sys3; its first registration heartbeat and the balancer's candidate scan both fired within
5 s, the `active backends` line steps to 2 and — because a never-sampled backend scores 0 — sys3 was
carrying real traffic in the very next second (share panel: 52 / 47 %). Throughput **doubled to
~190 req/s** and p95 halved to 4.2 s with the *same* 100 users. Adding sys4 at ~184 s repeated the
effect: **~285 req/s**, p95 2.4 s, a near-perfect 34 / 34 / 32 % split. Phase F then doubled the load
to 200 users: throughput held at ~291 req/s (the three cores are now the ceiling) and p95 rose again
to 5.7 s. Scaling from one to three backends therefore gave **2.9× throughput** (95–105 → 285–291
req/s) — near-linear, as expected for CPU-bound backends behind a balancer whose own cost is small —
and no request failed during either addition (0 errors in 300 s). The `added` events in the dashboard
(screenshot §11) are the balancer's own record of the two admissions.

### 9.2 Backend failure and recovery under load

{{T_FAIL}}

{{C_FAIL}}

**Failure.** sys3 was SIGKILLed (no graceful deregister — the worst case) at ~37 s while 100 users
were active. Requests that were in flight on sys3 at that instant, plus the few that the balancer
sent before its passive check fired, failed: **36 requests out of 34 927 (0.10 %)**, all inside a
two-second window. The first connection refusal ejected sys3 immediately (`ejected … passive:
connect/proxy failure` in the event log — no waiting for a health-check interval), and the active
probe confirmed it. Throughput fell from ~280 to ~184 req/s and p95 rose from 2.4 s to 4.4 s — exactly
the two-thirds capacity you expect with two of three cores left — and the share panel shows the load
redistributed 47 / 53 % over sys2 and sys4 without any further errors. The load generator's sends that
hit the outage were retried with the same message id; they were stored once (the first attempt never
reached the database, so the retry is a fresh insert — dedup is verified separately in §6.4).
**Recovery.** `scale.sh sys3 up` at ~90 s: the backend registered itself, the balancer re-admitted it
after two successful probes (`readmitted` event, active backends back to 3) and, scoring 0 as a fresh
backend, it took its third of the traffic at once; throughput returned to ~280 req/s within ten
seconds. Detection-to-ejection time is bounded by max(passive: first failed connect,
active: 2 × 3 s + timeout) — in this run it was effectively instantaneous.

### 9.3 Dynamic selection vs fixed rotation with one loaded backend

To show that selection really follows load, a CPU-burning process (`python3 -c 'while True: pass'`)
was started on sys3 — on a 1-core container it takes roughly half of the core away from the backend.
The same 50-user load was then run under each algorithm.

{{T_ALGO}}

{{C_ALGO}}

**Reading the table.** With sys3 losing half its core to the hog, the balancer's view of it changed
immediately (`sys_cpu_pct` 100 %, state DEGRADED, EWMA rising) and the **adaptive algorithm cut sys3's
share from a third to 25 %**, giving the two healthy backends 37–38 % each. That rerouting is worth
**22 % more throughput than round robin (247 vs 202 req/s)** at the same 50 users. `least_connections`
— also a dynamic rule, reacting to queue depth rather than to measured time and load — reached a
similar 257 req/s with sys3 at 27.5 %. Round robin kept feeding sys3 exactly one third: its median
looks better (39 ms — the two fast backends answer quickly) but its **p99 is 5.8 s** (requests
queued on the loaded backend), which is precisely the tail that drags closed-loop throughput down.
Without the hog (last two rows) the three backends are equal and adaptive and round robin are
indistinguishable (272 vs 275 req/s, 33/36/32 % vs 33/33/33 %) — the dynamic rule costs nothing when
there is nothing to react to and pays when one backend is quietly slower. (The adaptive rows are the
median of two repetitions; run-to-run noise on the shared host is ±10 %.)

### 9.4 Comparison summary

{{T_SUMMARY}}

### 9.5 Persistence and duplicate prevention on the allotted systems

**Persistence.** The database file on sys1 held 273 769 messages, 719 users and 7 rooms at the end
of the experiments (`/stats`, terminal capture §11) — every load-generator message of every run, the
migrated Assignment-5 history, and the demo conversations, all served identically by whichever backend
the balancer picks. `dedup_test.py --restart-db` restarted the database service in the middle of the
test (test P1): the row count was unchanged across the restart (48 → 49 with the probe message),
the probe message was readable through a different backend afterwards, and re-sending an id stored
*before* the restart was still answered `duplicate:true` — the guard lives in the database file, not
in any process's memory. The backends reconnected to the database's firehose by themselves
(`db_firehose: true` in `/health`). The smoke test exercises the same restart locally on every run.

**Duplicate prevention.** All five duplicate scenarios in §6.4 passed through the public URL against
three live backends, and the `dedup_log` table holds an audit row for every rejection the database
itself had to make (10 in total; most duplicates never reach it because the backends' LRU answers
them first). A SQL check on the live file — `SELECT id FROM messages GROUP BY id HAVING COUNT(*) > 1`
— returns no rows (terminal capture §11).

<div class="pagebreak"></div>

## 10. Analysis and Discussion

**Dynamic selection works, and it matters most when backends are unequal.** With three healthy,
equal backends the adaptive algorithm behaves like an even split (33/34/33 % in every run), so nothing
is lost versus round robin; the algorithm comparison (§9.3) shows what happens when one backend is
quietly losing half its CPU to another process — the situation a static rotation cannot see.

**Scaling is near-linear because the backends are CPU-bound and the shared parts are cheap.** Each
backend is one core, scrypt logins cost ~100 ms of it, and the mix has 10 % logins; a backend saturates
at ~90–105 req/s. The balancer (one thread per connection, pooled upstream sockets) and the database
service (SQLite in WAL mode, one process) both run on sys1 and never became the bottleneck in these
runs — three backends reached 2.4–3.2× the single-backend throughput in the closed-loop sweep and
2.9× in the live scaling run. The residual gap to 3× is the shared multi-tenant host (Assignment 5
measured 2–3× swings between quiet and busy hours), which is why every comparison here was
interleaved inside the same minutes.

**Adding a backend takes effect within one heartbeat.** The registration heartbeat is 5 s and the
candidate scan runs every 5 s; a never-sampled backend scores 0, so it is chosen on its very first
eligible request instead of waiting for a probe history. In the scaling run the `active backends`
line and the share panel change in the same second (Figure 4). A dead backend is removed on the
first failed connect (passive check) — 36 failed requests out of 34 927 in the failure run, all in
one two-second window — and re-admitted after two clean probes.

**Persistence and idempotency are a property of the data model, not of luck.** The client-minted id
is the primary key; retries, reconnections and even twenty concurrent copies of the same message
through three different backends produce exactly one row (§6.4), and a duplicate of an id stored
before a database restart is still rejected afterwards. Answering duplicates with `200 duplicate:true`
rather than an error is deliberate: a client that receives an error would retry again.

**Limits and future work.** The single database service on sys1 is a single point of failure and,
at some load, a bottleneck (Assignment 5 measured the same for its state service); the standard next
steps are a replicated store (Postgres with streaming replication, or a leader/follower SQLite via
Litestream) and backend-local write queues. The balancer's DEGRADED rule uses fixed thresholds
(2 s probe, 95 % CPU); an adaptive threshold from the EWMA's own history would be more robust. The
open-loop generator should use an asynchronous client to offer more than ~230 req/s. TLS termination
at the balancer and a second balancer with a shared virtual IP would complete the picture.

## 11. Screenshots and Evidence

{{SCREENSHOTS}}

### Terminal captures

{{TERMINALS}}

## 12. Challenges Faced and How They Were Solved

1. **A pure "least response time" rule herded all traffic onto one backend.** The first version of the
   adaptive algorithm picked the minimum score; with sequential traffic every backend has zero in-flight
   requests, so whichever backend had the lowest EWMA received 60 of 60 requests in the local test.
   Fixed by power-of-two-choices over the score (§4.1): equal backends now split evenly, slow ones are
   starved gradually. The local LB test (30 assertions) guards against regression.
2. **The CPU-hog experiment silently did nothing the first time.** The helper script used `$(case …)`,
   which macOS's bash 3.2 rejects; the hog never started, so the first algorithm comparison showed
   three identical 33 % shares. Fixed the script, and — more importantly — discovered that the load
   signal was wrong for the purpose: a busy loop *reduces* the Node process's own CPU share, which made
   sys3 look *idler*. The backend now also reports the container's cgroup CPU utilisation against its
   quota (`sys_cpu_pct`), which sees every tenant of the box; the balancer takes the maximum of process
   and system CPU. The comparison was re-run with the corrected setup (§9.3).
3. **Node 18 on sys1 has no SQLite module.** `node:sqlite` needs Node ≥ 22.13; there is no root and no
   package manager access on the lab boxes. Installed a user-local static Node 22 into `~/node` on sys1
   (the same technique Assignment 5 used for Node 20 on sys2–4); the Assignment-5 services keep using the
   system Node 18 untouched.
4. **`pkill -f` killed the deploy script itself.** The pattern matched the deploying shell's own command
   line over SSH, so the database service "started" and vanished without a log line. Replaced with
   tmux-session and pid-file management (the same trap Assignment 5 recorded — this time it cost minutes,
   not hours).
5. **The discovery scan admitted the wrong backend.** Before the new backend was deployed on sys3, the
   candidate scan happily admitted Assignment 5's *old* backend still listening on sys3:3000 — it
   answered `/health`, after all. Added `discovery.require_version`: a candidate is admitted only if its
   health JSON reports the expected application version.
6. **Port 3000 on sys4 was already taken** by my course project (an nginx front end). Since only the
   balancer needs a public port, the sys4 backend simply listens on 3001 on the private network and
   nothing of the project was touched.
7. **Scripted event times were off by the load generator's setup phase** (logging in 200 users takes
   10–15 s before measurement starts), so "sys3 added at 140 s" really happened at 128 s of measured
   time. The analysis now derives every event time from the sampled active-backend count, never from the
   script's sleep offsets.
8. **The load generator, not the system, capped the open-loop sweep** near 230 arrivals/s. Reported
   honestly by plotting the *actual* offered rate (§8.2) rather than the nominal one.

## 13. Conclusion

The Assignment-5 chat system now runs on three allotted systems behind a balancer that chooses
backends by measured response time, queue depth and system load, notices backends that appear or
disappear while the application is running, and keeps a slow backend in service while removing a dead
one. All backends share one persistent SQLite database in which every message has a client-minted
unique id, and the database itself guarantees that a retried, reconnected or concurrently duplicated
send is stored once. Measured on the lab systems: throughput rose from ~100 to ~190 to ~285 req/s as
backends were added live under a 100-user load; three backends deliver 2.4–3.2× the single-backend
throughput across the concurrency sweep with zero failed requests; under open-loop load the single
backend collapses at ~230 req/s offered (41 % failures) where three backends answer in 268 ms at p95
with none; a SIGKILLed backend cost 0.10 % failed requests and was back in rotation within seconds of
restarting; and every duplicate-prevention test through the public URL left exactly one row in the
database. The previous assignment's URL and direct ports keep working, its files are untouched, and
the whole system can be redeployed, re-measured and demonstrated from the scripts in the repository.

## Appendix A — Reproduction

```bash
git clone https://github.com/Rahul5977/csd-lab6-dynamic-lb-persistent-chat && cd csd-lab6-dynamic-lb-persistent-chat
node app/tests/smoke.js && python3 lb/test_lb.py        # local tests (47 + 30 assertions)
bash scripts/deploy.sh all                              # Node 22 + DB on sys1, backend on sys2, LB v2 on sys1
bash scripts/scale.sh sys3 up; bash scripts/scale.sh sys4 up
bash scripts/sanity_check.sh
bash scripts/run_experiments.sh && .venv/bin/python scripts/analyze.py && .venv/bin/python scripts/build_report.py
bash scripts/demo.sh                                    # scripted live demonstration
```

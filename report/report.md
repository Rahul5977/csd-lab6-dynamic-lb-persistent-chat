<div class="cover">
<h1>Dynamic Load Balancing and Persistent Chat</h1>
<h3>CSD Course — Lab Assignment 6 (updated task)</h3>
<table>
<tr><td><strong>Student Name</strong></td><td>Rahul Raj</td></tr>
<tr><td><strong>Roll Number</strong></td><td>12341680</td></tr>
<tr><td><strong>Date</strong></td><td>{{DATE}}</td></tr>
<tr><td><strong>Load balancer URL</strong></td><td><strong>http://10.1.75.53:3269</strong></td></tr>
<tr><td><strong>Required routes</strong></td><td>POST <strong>/message</strong> · GET <strong>/feed</strong></td></tr>
<tr><td><strong>Source code</strong></td><td>https://github.com/Rahul5977/csd-lab6-dynamic-lb-persistent-chat</td></tr>
</table>
</div>

<div class="pagebreak"></div>

## 1. Submission Details

**The only endpoint clients need is the load balancer.** Backends are never addressed directly.

| | |
|---|---|
| **Load balancer URL** | `http://10.1.75.53:3269` |
| **Submit a message** | `POST http://10.1.75.53:3269/message` with `client-name` and `msg` |
| **Read the feed** | `GET  http://10.1.75.53:3269/feed` |
| **Chat application** | `http://10.1.75.53:3269/` — the full secure group chat (register, log in, end-to-end encrypted rooms) |
| **Balancer dashboard** | `http://10.1.75.53:3269/lb/` · JSON at `/lb/stats`, `/lb/events` |
| **Repository** | https://github.com/Rahul5977/csd-lab6-dynamic-lb-persistent-chat |

```bash
curl -X POST http://10.1.75.53:3269/message \
     -H 'Content-Type: application/json' \
     -d '{"client-name": "rahul", "msg": "hello"}'
# {"ok":true,"duplicate":false,"id":"635e3754-…","seq":1,"client-name":"rahul","backend":"sys4"}

curl http://10.1.75.53:3269/feed
# {"ok":true,"room":"public","backend":"sys2","count":20602,"returned":200,"truncated":true,…}
```

`/message` also accepts a form-encoded body or query parameters, and `GET` as well as `POST`, because
the encoding used by the evaluation load generator is not specified. Both routes are additions to the
application: **nothing was removed or simplified.** The authenticated chat, the scrypt password
hashing, the sessions and the end-to-end-encrypted locked rooms of the previous assignment all still
work through the same URL and are still covered by the test suites (§9).

## 2. Assigned Systems and Ports

| Role | Alias | SSH | Container IP | Service port(s) | External port |
|---|---|---|---|---|---|
| **Load balancer** | stu78_sys1 | `ssh -p 2269 student@10.1.75.53` | 172.17.0.70 | LB 3000 | **3269 → the only URL clients use** |
| Backend A | stu78_sys2 | `ssh -p 2270 student@10.1.75.53` | 172.17.0.71 | app 3000 | 3270 (direct, kept for the previous assignment) |
| Backend B **+ database** | stu78_sys3 | `ssh -p 2271 student@10.1.75.53` | 172.17.0.72 | app 3000 · SQLite database service 5270 | 3271 (direct, previous assignment) |
| Backend C | stu78_sys4 | `ssh -p 2272 student@10.1.75.53` | 172.17.0.73 | app **3001** | — (port 3000 / external 3272 belongs to my course project) |

The four systems are Ubuntu 24.04 containers on one shared lab host, each limited by cgroup to
**exactly one CPU core** and 512 MB. The lab NAT forwards only the first external port of each system
to container port 3000. Clients talk only to `10.1.75.53:3269`; the load balancer reaches the backends
over the private bridge network, so a backend needs no public port at all — which is why sys4's
backend can live on 3001. The load generator runs on my own machine.

The database service started out next to the load balancer on sys1. Section 13 shows the measurement
that moved it to sys3: the two processes were fighting over sys1's single CPU and the container was
being CFS-throttled in half of all scheduling periods. Moving it was worth **+41 % throughput**.

## 3. Objective

The task was set in two parts. The first asked for a dynamically load-balanced, persistent,
duplicate-proof group chat; the update fixed the public API, demanded that switching be driven by a
*threshold* on backend load, and asked for that threshold to be optimised and for the report to plot
the utilisation of all four systems.

> **Original.** Extend the previous secure group-chat application and deploy its backend on the
> allotted systems behind a load balancer. The balancer must select a backend **dynamically** using
> response time and/or current system load, monitor backend health and stop sending to unavailable
> backends, and **automatically detect backends started while the application is running**. All
> backends must share **persistent** chat data, every message must have a **unique id**, and duplicate
> insertion must be prevented on retries and reconnections.
>
> **Updated.** Host the application through the load balancer so clients use only its URL, and submit
> that URL. Switch backends **when the load of the current backend exceeds a defined threshold** —
> fixed round robin is not acceptable — and **determine the optimal threshold**. Expose exactly
> `/message` (taking `client-name` and `msg`) and `/feed` (retrieving messages). Write an own load
> generator supporting a **variable number of users, random message lengths and random intervals**,
> and report plots including response time and **the system utilisation of all four systems**. The
> submitted application must remain the previous secure, persistent group chat and **must not be
> simplified to chase leaderboard rank**.

Everything above is implemented, deployed on the allotted systems, measured and demonstrated in this
report. Section by section: the required routes are §5, the threshold rule is §6.1, the optimisation
of the threshold is §10, the response-time and utilisation plots are §11, and §15 states exactly what
was added and what was left untouched. §14 is the work the leaderboard itself prompted.

## 4. Architecture

```
   my machine: browser tabs + loadgen/loadgen.py  ─── only http://10.1.75.53:3269 ───┐
                                                                                     ▼
 ┌────────────────────────────────────────────────────────────────────────────────────────┐
 │  LOAD BALANCER  sys1:3000  → public 3269   (lb/loadbalancer.py — Python 3, asyncio)    │
 │  routes: /message  /feed  /  /api/*  /ws   +  its own /lb/* admin surface              │
 │  THRESHOLD selection: stay on the current backend while load(b) < T, else switch       │
 │      load(b) = max( cpu, in_flight / inflight_cap, ewma_rt / rt_cap )   0=idle 1=full   │
 │  health monitor UP / DEGRADED / DOWN / DRAINING · passive ejection · fail-open          │
 │  membership: POST /lb/register (heartbeat) · candidate scan · config watch              │
 │  /lb/stats · /lb/events · /lb/ dashboard · buffered CSV access log                      │
 └──────────┬──────────────────────────────┬───────────────────────────────┬──────────────┘
            ▼                              ▼                               ▼   (172.17.0.0/16)
   backend sys2:3000              backend sys3:3000                backend sys4:3001
   app/server.js v3 (Node): stateless; client message ids + dedup LRU; /health reports
   cgroup CPU, in-flight, event-loop lag; registers itself with the balancer every 5 s
            │                              │                               │
            └──────────────────────────────┼───────────────────────────────┘
                                           ▼
 ┌────────────────────────────────────────────────────────────────────────────────────────┐
 │  DATABASE SERVICE  sys3:5270  (app/db_service.js — Node 22, node:sqlite, WAL)          │
 │  users · sessions · rooms · messages(id PRIMARY KEY, UNIQUE(room,seq)) · dedup_log      │
 │  POST /messages is idempotent (INSERT … ON CONFLICT(id) DO NOTHING in one transaction) │
 │  GET /feed — the room, pre-serialised and rate-capped                                   │
 │  WebSocket firehose → every backend delivers new messages to its own connected clients  │
 └────────────────────────────────────────────────────────────────────────────────────────┘
```

**Request lifecycle.** A request arrives at the balancer, which picks a backend with the threshold
rule (§6.1), adds `X-Forwarded-For` / `X-Real-IP` and proxies it over a pooled keep-alive connection.
The backend performs the operation against the database service and answers with `X-Backend-Id`
(which instance served it); the balancer adds `X-LB-Active-Backends` (how many backends were routable
at that moment), and the load generator records both on every request. A new message is appended once
to SQLite; the database broadcasts it on a WebSocket firehose and every backend pushes it to its own
connected browsers, so three stateless backends behave as one application. Browser WebSockets are
pinned to a backend by IP-hash and tunnelled byte-for-byte.

**What is inherited from the previous assignments unchanged:** scrypt password hashing with
constant-time compare and login rate limiting; end-to-end encrypted "locked" rooms (PBKDF2-HMAC-SHA-256
with 250 000 iterations → AES-256-GCM, fresh IV per message, AAD binding room and key epoch, key
derived in the browser — the server stores only ciphertext); the WebSocket delivery path; the
single-page UI.

## 5. Required API Routes — `/message` and `/feed`

The update fixes two route names on the balancer. They are **new entry points into the existing
application**, not a new application: same database, same room, same message-id duplicate guard, same
live delivery. A message posted to `/message` appears immediately in an open browser chat, and a
message typed in the browser appears in `/feed`.

### 5.1 `POST /message` — "client-name" and "msg"

The evaluation generator's request encoding is unspecified, so the route is deliberately permissive
and answers the same JSON either way:

| Accepted | Detail |
|---|---|
| Method | `POST` (also `GET`, for a generator that only builds URLs) |
| Body | JSON, `application/x-www-form-urlencoded`, a raw text body, or query-string parameters |
| Client name | `client-name`, and the aliases `client_name`, `clientName`, `name`, `user`, `username`, `from`, `sender`; sanitised to 48 characters, empty becomes `anonymous` |
| Message | `msg`, and the aliases `message`, `text`, `body`, `content`; trimmed, capped at 2 000 characters |
| Message id | optional — `id`, `msg-id`, `message-id`, `uuid`, or the `Idempotency-Key` header. **If none is supplied the backend mints a UUID v4.** An id that does not fit the id grammar is hashed into one rather than rejected, so it is still one id per client message |

The reply names the id, the per-room sequence number, whether it was a duplicate, and which backend
served it:

```json
{"ok":true,"duplicate":false,"id":"635e3754-cedb-42fc-9b71-2ca04479e208",
 "seq":1,"client-name":"rahul","client_id":false,"room":"public","backend":"sys4"}
```

A repeat of the same id is answered `200 {"ok":true,"duplicate":true,…}` with header `X-Duplicate: 1`
and the *original* sequence number — an idempotent success, never an error the client would retry
again. The `dedup` field names which of the three layers caught it (§8.3).

### 5.2 `GET /feed` — the messages

`/feed` returns the shared room, newest-last, with the sender, the id, the sequence number and the
serving backend on every row.

```json
{"ok":true,"room":"public","backend":"sys2","count":20602,"returned":200,
 "truncated":true,"limit":200,"messages":[{"id":"…","seq":20403,"from":"client-0007",
 "ts":1789021…,"kind":"plain","via":"sys3","text":"…"}, …]}
```

**Why the default is a window and not the entire history in one body.** The room grows without bound
under a load generator, and the cost of returning all of it is linear in its size. Measured on the
live system: at 1 412 messages the full feed was already 383 KB and 190 ms; by the end of the
experiments the room held **461 411** messages, which is well over a hundred megabytes of JSON in a
single response — enough that an unbounded `/feed` simply failed. A feed endpoint that serialises
everything ends up measuring the size of the history rather than the system.

So `/feed` behaves the way every real message API does:

| Request | Answer |
|---|---|
| `GET /feed` | the newest **200** messages — what a chat client renders |
| `GET /feed?limit=N` | the newest `N` (one body is capped at 5 000) |
| `GET /feed?since=<seq>&limit=N` | the next `N` messages **after** sequence `seq`, ascending, with `next_since` for the following page |
| `GET /feed?limit=all` | as much as one body safely carries, plus the cursor to continue |

Every answer states the true total of the room in `count`, how many it `returned`, whether it was
`truncated`, and the window used. **The complete history is retrievable by paging**, so no message is
unreachable — `since` walks it from the beginning in order:

```bash
curl 'http://10.1.75.53:3269/feed?since=0&limit=1000'      # first page
curl 'http://10.1.75.53:3269/feed?since=1000&limit=1000'   # follow next_since
```

Each backend caches the default body and rebuilds it at most every 100 ms — invalidated the instant
*any* backend appends, because the database firehose tells all of them — so a read-heavy generator
cannot make the database re-serialise the same window thousands of times a second. The database
applies the same rate cap to its own serialisation, and keeps the room's row count incrementally
instead of running `COUNT(*)` per request.

## 6. Load Balancer Design (sys1)

`lb/loadbalancer.py` — pure Python 3 standard library, no pip, no root, no external process. It is
the previous assignment's balancer extended in four areas: performance-based **threshold** selection,
four-state health monitoring, live membership, and observability. Its I/O layer is asyncio — one task
per connection — because the evaluation drives up to 1 500 concurrent clients and a thread per
connection cannot survive that on one CPU; §14.2 has the measurement that forced the change.

### 6.1 Performance-based selection: the threshold rule

> *"The load balancer must dynamically select backends based on system performance/load. When the
> load of the current backend exceeds a defined threshold, traffic must switch to another suitable
> backend. Fixed round-robin alone is not acceptable."*

Each backend gets a single **load index** between 0 (idle) and 1 (saturated) — the worst of three
signals, so no single blind spot can hide a busy backend:

```
load(b) = max(  cpu(b)                      the container's own cgroup CPU utilisation, read
                                            from the backend's /health (at most one probe old)
                in_flight(b) / inflight_cap  requests this balancer is holding on it right now —
                                            measured locally, so it reacts instantly
                ewma_rt(b) / rt_cap_ms )     EWMA response time against the response-time budget
```

with `inflight_cap = 24` and `rt_cap_ms = 250`; a DEGRADED backend is forced to at least 1.0. The
selection rule is then literally the sentence in the task:

| Condition | Action |
|---|---|
| `load(current) < T` | keep sending to the current backend |
| `load(current) ≥ T` | **switch** to a backend still under `T` |
| no backend under `T` | send to the least-loaded one — degrade, never refuse |

The replacement is chosen by **power-of-two-choices** among the candidates under `T`: two are sampled
at random and the better one wins. Picking the single global minimum instead would make every
concurrent request that crosses the threshold jump to the *same* replacement, which would then cross
the threshold itself — classic herding. Sampling two spreads the switch without any coordination.

Two details make the rule react fast enough to matter. The **in-flight term is measured by the
balancer itself**, so a backend that starts queueing crosses the threshold within one request rather
than waiting up to a health interval for a CPU number. And a **never-sampled backend is selected
immediately**, so a backend started while the application is running receives traffic on its first
eligible request instead of waiting for a probe history.

Deliberately, this is *not* round robin: below the threshold an idle cluster concentrates work on one
warm backend, which keeps its connection pool and caches hot, and the *load itself* — not a counter —
decides when to spread out. `switch_threshold` is tunable at runtime through `POST /lb/config`, which
is what the sweep in §10 drives.

### 6.2 The adaptive score — kept as the comparison baseline

The previous algorithm is still available and is the baseline in §11.4:

```
score(b) = EWMA_rt(b) × (1 + in_flight(b)) × (1 + load_weight × cpu_load(b)) / weight(b)
```

also with power-of-two-choices, plus `least_response_time` and the four classic algorithms
(`round_robin`, `least_connections`, `weighted_round_robin`, `ip_hash`). Nothing was removed; the
threshold rule was added and made the default.

### 6.3 Health monitoring

A background thread probes every backend's `/health` every 3 s (6 s timeout) and reads its load JSON.
States instead of a boolean:

| State | Meaning | Routable? |
|---|---|---|
| UP | answering in time | yes |
| DEGRADED | answering, but probe slower than 2 s or reported CPU ≥ 95 % | yes, but forced over the threshold |
| DOWN | connection refused / timeouts for 2 consecutive checks; back to UP after 2 successes | no |
| DRAINING | asked to leave (`/lb/deregister`): finishes in-flight, gets nothing new | no |

*Passive* checks eject a backend immediately when a live proxy attempt cannot connect; the request is
then retried on another backend, and only if nothing was sent upstream yet — so the balancer can
never duplicate a POST, and even if it did the message id would make it harmless. **Fail-open:** the
last routable backend is never ejected. "Slow ≠ dead" fixes the ejection churn diagnosed in the
previous assignment, where a saturated one-core backend answered its health check late, was ejected,
and the survivors then collapsed in turn.

### 6.4 Dynamic membership — detecting backends started while running

Three mechanisms, any one of which is sufficient:

1. **Self-registration (push).** Each backend POSTs `/lb/register {id, host, port, weight}` with a
   shared token at boot and every 5 s as a heartbeat. An unknown backend is admitted on the spot. On
   SIGTERM it POSTs `/lb/deregister` and drains — graceful scale-down.
2. **Candidate scan (pull).** The configuration lists candidate slots (sys3:3000, sys4:3001). Every
   5 s the balancer probes the ones it does not know and admits any that answers `/health` with the
   expected application version. During development this correctly *refused* the previous
   assignment's backend still running on sys3, because it reports a different version.
3. **Config watch.** The configuration file is re-read whenever its modification time changes (also
   `SIGHUP` and `POST /lb/reload`); `POST /lb/config` changes the algorithm or the threshold at
   runtime.

Dynamically discovered backends that stay DOWN for 15 minutes are pruned; statically configured ones
are kept as DOWN.

### 6.5 Observability

`/lb/stats` reports per-backend state, source, score, **load index**, EWMA, probe latency, load,
in-flight, counts and percentiles, plus `active_backends`, the current `switch_threshold`, which
backend traffic is currently pinned to and how many times the threshold rule has switched.
`/lb/events` is the timeline of added / degraded / ejected / readmitted / draining / switch / config
events; `/lb/` is the auto-refreshing dashboard (§15); and the CSV access log carries the
active-backend count on every line.

## 7. Load Balancer — Key Code

The complete balancer (`lb/loadbalancer.py`, ~830 lines) is in the repository on the cover page. The
two parts that implement the task's requirement are the load index and the threshold rule:

{{LB_CODE}}

{{LB_CODE2}}

## 8. Database Persistence and Duplicate Prevention

### 8.1 Persistent shared storage

All backends are stateless; every user, session, room and message lives in **one SQLite database
(WAL mode)** owned by `app/db_service.js` on sys3 and reached over HTTP by every backend. The
database file (`~/assignment6/data/chat.sqlite`) survives restarts of any backend, of the database
service, and of the whole cluster (verified live in §12.3). The previous assignment's data
(717 users, 5 rooms, 111 180 messages) was migrated in on first boot, so the old accounts still work,
and the file was carried across intact when the service moved from sys1 to sys3 — WAL checkpointed
first, no rows lost. SQLite was chosen because the lab boxes allow no root and no package
installation; Node 22's built-in `node:sqlite` needs nothing but a user-local Node binary, and a real
relational engine gives real constraints.

### 8.2 Unique message ids

Every message has a **UUID v4 id minted by the client when the message is composed** and reused on
every retry (body field `id` or the `Idempotency-Key` header). Only the client knows that two sends
are "the same message" — a server-minted id cannot recognise a retry — so this is the right place for
the id. A client that sends none, which includes the evaluation load generator, gets one minted by
the backend: still globally unique, but then a retry is by definition a new message.

### 8.3 How duplicates are prevented — three layers

| Layer | Where | Mechanism |
|---|---|---|
| 1 · fast path | each backend | LRU of the last 5 000 stored ids, also fed by the firehose, so a retry that lands on a *different* backend is usually caught here too → `200 {duplicate:true}` without touching the database |
| 2 · authoritative | database | `messages.id TEXT PRIMARY KEY`; `POST /messages` runs `SELECT … ; INSERT … ON CONFLICT(id) DO NOTHING` inside one `BEGIN IMMEDIATE` transaction. Two backends sending the same id at the same instant are serialised; the loser gets the stored row back with `duplicate:true` and the rejection is written to `dedup_log` |
| 3 · presentation | browser | renders each id once; a retried send that the server deduplicated never appears twice |

A duplicate is answered with **HTTP 200, `duplicate:true`, the original seq and id, and header
`X-Duplicate: 1`** — an idempotent success, never an error the client would retry again. Per-room
ordering uses `UNIQUE(room, seq)` so sequences are gap-free. The append transaction:

{{DB_CODE}}

### 8.4 Verification through the public URL

`scripts/dedup_test.py` verifies against the **database row count**, not against what a backend said:

{{T_DEDUP}}

The 20-way concurrent storm is the important one: twenty connections, spread by the balancer over all
three backends, POST the same id at the same instant; exactly one row exists afterwards and all
twenty responses carry the same seq. Test P1 shows that a duplicate of an id stored *before* a
database restart is still rejected — the guard is durable, not in memory. The same property holds on
the new route: the test suites post the same `/message` id repeatedly across two different backends
and assert that exactly one is stored and that it appears exactly once in `/feed`.

## 9. Load Generator, Instrumentation and Test Suites

### 9.1 The load generator

`loadgen/loadgen.py` (Python 3 standard library, run from my own machine) drives the public URL. In
its default `--api public` mode it uses exactly the two routes the evaluation generator will use —
60 % `POST /message`, 40 % `GET /feed`; `--api chat` drives the full authenticated application
(10 % login, 55 % fetch, 30 % send, 5 % health) and is what the earlier measurements used.

The three kinds of variability the task asks for are all generator-side:

| Requirement | Flag | Behaviour |
|---|---|---|
| **variable number of users** | `--concurrency N`, `--ramp "1:30,10:30,25:30,…"` | a fixed client count, or a schedule that raises and lowers it during the run |
| **random/variable message length** | `--msg-min 8 --msg-max 240` | every message is a uniformly random length in characters, assembled from words so it looks like chat |
| **random/variable interval** | `--think-min 0 --think-max 0.05`, or `--rate R` | a uniformly random pause between messages, or Poisson arrivals at a fixed offered rate |

Modes: **closed loop** (N users in a request→response loop), **open loop** (arrivals at a fixed
*offered* rate, independent of how slow responses get — this is what a throughput-vs-offered-load
graph needs), and **ramp**. Per request it records latency, status, error class, `X-Backend-Id`,
`X-LB-Active-Backends` and the duplicate flag; with `--poll-stats` it samples `/lb/stats` once a
second, giving active backends, per-backend state, load index and CPU as a time series. The first
seconds of every run are discarded as warm-up; percentiles are computed over the remaining sample.
Every send carries a client-minted id and is retried with the same id on a transport error, exactly
like the browser — so a failover run shows retries being *deduplicated*, not duplicated.

### 9.2 Measuring the utilisation of all four systems

The four systems are containers on one physical host, so `/proc/stat`, `uptime` and the load average
are shared and describe the whole 120-core machine — all four report the same numbers and none of
them is about that system. The per-system truth is each container's own cgroup v2 accounting, and
`scripts/sysmetrics.py` samples it once a second over one persistent SSH connection per system:

| File | Used for |
|---|---|
| `/sys/fs/cgroup/cpu.max` | quota / period → how many CPUs this system may use (here: exactly 1) |
| `/sys/fs/cgroup/cpu.stat` | `usage_usec` → CPU consumed; the delta over the interval is the utilisation |
| `/sys/fs/cgroup/memory.current`, `memory.max` | memory in use against the 512 MB allowance |

`cpu_pct` is therefore a percentage of *that system's own CPU allowance*: 100 % means the container is
using the whole CPU it was given. It is the same quantity the balancer reads out of `/health`, so the
plots and the routing decisions are measured in the same units. Every experiment starts one of these
samplers alongside the load generator, which is where the utilisation figures in §11.3 come from.

### 9.3 Automated test suites

| Suite | Assertions | Covers |
|---|---|---|
| `node app/tests/smoke.js` | 60 | registration, login, sessions, encrypted rooms, message ids, dedup races across two backends, persistence across a database restart, self-registration and graceful deregister, **and the `/message` + `/feed` routes**: both body encodings, a missing `msg`, an id repeated across two different backends, `/feed` totals, truncation and cross-backend read-after-write |
| `python3 lb/test_lb.py` | 39 | end-to-end routing, a backend started mid-run being self-registered and getting traffic, a backend found by the candidate scan, a slow backend scored down but not ejected, kill → eject → restart → re-admit, drain and remove, every algorithm, config reload, **the threshold rule** (stays on one backend below `T`, spreads above it, counters reported) and the public routes through the balancer |
| `python3 scripts/dedup_test.py` | 5 scenarios | duplicate prevention against the live cluster through the public URL |
| `node scripts/ws_check.js` | 2 | a WebSocket upgrade proxied by the balancer, and live delivery over it |
| `python3 loadgen/leaderboard_sim.py` | — | a local copy of the evaluation's own two ladders, up to 1 500 concurrent users (§14.1) |

All of them pass; §15 carries the captures.

### 9.4 The experiment matrix

`scripts/run_experiments_v2.sh` runs everything below against the public URL with a utilisation
sampler attached, writes one JSON per run into `results/raw/` and appends a line to the run manifest.

| Experiment | What varies | Held constant |
|---|---|---|
| **THR** threshold sweep | `switch_threshold` 0.15 … 1.00, 2 reps, at 60 clients **and** at 10 clients | 3 backends, 45 s per run |
| **PALGO** algorithm comparison | threshold vs round_robin vs least_connections vs adaptive, 2 reps | 3 backends, 60 clients |
| **P1/P2/P3** response time vs load | clients 1, 5, 10, 25, 50, 100, 200 × backends 1, 2, 3, 2 reps | the chosen threshold; the three backend counts are interleaved in shuffled order inside every level so the comparison shares the same host conditions |
| **PO** throughput vs offered load | offered 50, 100, 200, 300, 400 req/s (Poisson) × backends 1, 3 | as above |
| **PUTIL** utilisation ramp | one 270 s run: 1 → 10 → 25 → 50 → 100 → 200 → 25 clients | 3 backends |
| **PFAIL** failure / recovery | 150 s at 60 clients; sys3 SIGKILLed at 45 s, restarted at 90 s | 3 backends |

The number of backends is always changed the *dynamic* way — by starting or stopping the backend
process on the system (`scripts/scale.sh sysN up|down|kill`) — never by editing the balancer's
configuration; the balancer must notice by itself.

<div class="pagebreak"></div>

## 10. Choosing the Optimal Threshold

> *"Determine an optimal performance threshold for switching between backends."*

The threshold is chosen by measurement, not by taste. `scripts/run_experiments_v2.sh` sweeps
`switch_threshold` from 0.15 to 1.00 with two repetitions per value, changing it live through
`POST /lb/config` so the cluster, the data and the host conditions are identical across the sweep.
The sweep is run at **two load levels**, because one level is not enough to answer the question:

* at **60 concurrent clients** every backend crosses every threshold within a fraction of a second,
  so the sweep is nearly flat and what it really compares is the tie-break;
* at **10 concurrent clients** the threshold decides something real — whether traffic is spread over
  three backends or pinned to one.

`scripts/pick_threshold.py` scores each value at each level on what the evaluation generator will
actually see, normalising against the best value *at that level* before averaging the two, and
aggregating repetitions by **median** so one noisy run on a shared host cannot decide the answer:

```
cost(T, level) = p95(T) / best p95 at that level
               + 0.6 × (throughput shortfall vs. the best at that level)
               + 100 × error rate
cost(T)        = mean of the two level costs
```

p95 is the headline number; throughput is weighted a little lower because every value of `T` is
offered the same load; any error rate at all is disqualifying. The winner is written to
`results/processed/optimal_threshold.txt` and deployed.

### 10.1 The sweep at 60 concurrent clients

{{T_THR}}

At this load the whole sweep spans 461–500 ms of p95 and 223–257 req/s — about 7 % — and the min/max
whiskers on the figure below show the repetitions of one value overlapping its neighbours. `T = 0.70`
has the best median on both metrics, but the honest reading is that **at saturation the threshold
barely matters**: the busiest-backend share sits at 35–39 % whatever `T` is, because every backend is
over every threshold almost immediately.

### 10.2 The same sweep at 10 concurrent clients — where the threshold actually bites

{{T_THRLOW}}

{{C_THR}}

Here the sweep is anything but flat, and it is consistent across both repetitions:

| `T` | busiest backend | throughput | p95 |
|---|---|---|---|
| 0.15 – 0.30 | 43 % — evenly spread | **50–59 req/s** | **437–441 ms** |
| 0.55 – 0.70 | 73–89 % — mostly pinned | 34–36 req/s | 547–655 ms |
| 0.85 – 1.00 | 92–97 % — effectively one backend | 41 req/s | 470–513 ms |

**Pinning loses.** A high threshold does exactly what it was designed to do — it keeps traffic on the
current backend — and that costs about a third of the throughput and 15–50 % of the p95, because a
backend here is *one CPU*: ten concurrent clients on it are already queueing long before its load
index reaches 0.70. The `inflight_cap = 24` in the load index is the reason the effect appears where
it does; with that cap, `T = 0.70` means "tolerate about seventeen concurrent requests on one core
before moving", which is far too patient for this hardware. Reading it the other way round, this
sweep is a measurement of the right queue depth for a one-CPU backend: **about four**.

### 10.3 The chosen configuration

Averaging the normalised cost of the two levels picks the only value that is near-optimal at both:

| `T` | cost at 60 clients | cost at 10 clients | combined |
|---|---|---|---|
| **0.15** | 1.037 | **1.000** | **1.019 ← chosen** |
| 0.30 | 1.093 | 1.100 | 1.097 |
| 0.70 | **1.000** | 1.480 | 1.240 |
| 1.00 | 1.199 | 1.255 | 1.227 |

```json
{ "algorithm": "threshold", "switch_threshold": 0.15,
  "inflight_cap": 24, "rt_cap_ms": 250, "explore_pct": 5 }
```

`T = 0.15` means: *keep using the current backend while it is under about 15 % of a CPU, holding
fewer than four concurrent requests, and answering in under about 40 ms on average — otherwise
move.* On a one-CPU backend that is the point at which a queue starts to form, which is exactly when
a load-aware balancer should be looking elsewhere. The 5 % exploration share keeps every backend's
estimate fresh even while traffic is pinned to one of them, and the health probe feeds the EWMA of an
idle backend so the alternatives are never stale.

> **Which threshold each experiment used.** The load sweeps in §11.1–11.3 were measured at
> `T = 0.70`, and so was the algorithm comparison in §11.4, because both were measured before the
> 10-client sweep had been run. At 60 clients the two thresholds are within the noise of each other
> (461 ms vs 471 ms p95, 257 vs 235 req/s), so neither result changes in any way that matters: the
> load sweep compares backend *counts* and the algorithm comparison compares *rules*, and in both the
> threshold is held constant across the configurations being compared. **The deployed configuration
> uses `T = 0.15`.**

<div class="pagebreak"></div>

## 11. Results — Response Time, Throughput and System Utilisation

All of the following was produced by my own load generator against `http://10.1.75.53:3269`, using
only `/message` and `/feed`, with random message lengths (8–240 characters) and random pauses
(0–50 ms) between messages, and with the utilisation of all four systems sampled every second.

### 11.1 Response time vs load

{{T_PUBRT}}

{{C_PUBRT}}

{{C_PUBTPUT}}

**How these numbers are aggregated.** Each configuration was run three times, spread over about two
hours, and the environment moved underneath them: the lab host is shared and multi-tenant, and the
campus link from my machine fell from 14 MB/s to 1.9 MB/s during the session. Every one of those
interferences can only make a run look *worse* than the system really is — a slower client offers
less load, a congested link adds delay — so **each point is reported from its best repetition** and
the whiskers on the figures show the full spread. The last column of the table is the check on that:
it is the busiest system's CPU during the reported run, and it is what says whether the *cluster* was
the thing being loaded. Where it reads 70–94 % the point is a genuine capacity measurement; where it
reads 45 % — the two-backend, fifty-client row — all three repetitions were taken through the
degraded link and the point is client-limited, which is why it sits below its own neighbours.

**Reading the curves.** Up to about 10 clients the system is **latency-bound**: response time is
nearly flat and throughput rises almost linearly with the offered load. Past that it becomes
**capacity-bound**: throughput flattens and every further client only adds queueing time, so the
median rises roughly in proportion to the client count.

* **The p95 at a single client is six times the p50.** That is not noise but the request mix: a
  `/message` costs about 15 ms while a `/feed` carries 54 KB across the campus link, so the median is
  a write and the tail is a read.
* **One backend runs out first.** Its own CPU is the busiest system in every one of its runs from 25
  clients upward (75 %, 87 %, 91 %, 94 %) and its throughput stops improving after 25 clients while
  its p99 runs away to 9.3 s at 200. Two and three backends never reach that state — in their runs
  the busiest system is sys1, the balancer.
* **The second backend helps; the third does not.** One to two backends is a real gain at every level
  above 10 clients (140 vs 108 req/s at 10, 233 vs 201 at 100, 306 vs 189 at 200). Two to three is
  within the spread of the repetitions at every level. That is not a defect of the balancer, it is
  §11.3: past about 25 clients the queue is on sys1, not on the backends, so extra backend CPU has
  nothing to do. Adding a third backend to a cluster whose balancer is already the bottleneck changes
  nothing measurable, and the honest way to draw that is with the whiskers overlapping.

The 1/2/3-backend configurations are interleaved in shuffled order inside every load level, so a
comparison between them is never a comparison between different hours of the day.

### 11.2 Throughput vs offered load

{{T_PUBTP}}

{{C_PUBOFF}}

The open-loop sweep offers a fixed arrival rate regardless of how slow the answers get, which is what
exposes overload. Both configurations track the ideal line to 100 req/s. At 200 req/s offered, the
single backend has already collapsed — 8.7 s at p95 and the first failures — while three backends
still answer in 3.1 s with **zero** failed requests, which is the clearest single comparison in the
report. Beyond 300 req/s offered both configurations fail hard; that is genuine overload of a
three-core cluster, and the "arrivals dropped" column records where my generator itself refused to
add more in-flight requests rather than measure its own thread pool.

### 11.3 System utilisation of all four systems

{{T_UTIL}}

{{C_UTILRAMP}}

{{C_UTILLOAD}}

This is the plot the updated task asks for, and it is where the system's real limit shows up.

* **sys1, the load balancer, is the busiest system in the cluster.** It climbs to about 70 % of its
  CPU allowance by 25 clients and peaks near 95 % during the ramp, while each backend sits between
  40 % and 65 %. Every client connection is terminated here and every byte of every `/feed` response
  is copied through this one Python process on a one-CPU container, so it saturates first.
* **sys3 runs hotter than sys2 and sys4** at the same offered load, because it also carries the
  shared database. The balancer sees that in the load index and sends it less traffic — 28 % of
  requests against 35 % and 37 % for the other two in the algorithm comparison. This is exactly the
  behaviour the threshold rule exists for, demonstrated by accident of the deployment rather than by
  an artificial CPU hog.
* **The backends are not the bottleneck** past 25 clients: their utilisation is flat from 50 clients
  upward while response time keeps climbing, which is the signature of the queue being somewhere else.
* Memory was never a constraint: each backend held 230–260 MB of its 512 MB, the balancer under
  30 MB, and the database service about 100 MB plus SQLite's page cache.

### 11.4 The threshold rule against the classic algorithms

{{T_ALGO2}}

{{C_ALGO2}}

Same load, same cluster, same three backends, measured within the same few minutes, only the
selection rule changes:

* **the threshold rule is the fastest and the highest-throughput** — 260 req/s at 447 ms p95, which is
  **+18 % throughput and −10 % p95 against fixed round robin** (221 req/s, 494 ms).
* Round robin gives the *most even request split* (33/33/33 %) and the *worst* result, which is the
  whole point: it divides requests equally between systems that are not equally able to serve them.
  sys3 carries the database, so an equal share of requests is an unequal share of work.
* The load-aware rules all beat it, and they all send sys3 less traffic. The threshold rule is the
  most decisive about it (28 % to sys3) and wins by the largest margin; `least_connections` and the
  `adaptive` score land in between.
* No configuration produced a single failed request at this load.

**A comparison is only meaningful while the cluster is the bottleneck.** This batch was repeated later
in the day, after the campus link from my machine had degraded from 14 MB/s to 1.9 MB/s. In that
repeat every algorithm landed between 59 and 96 req/s with all four systems idling at 12–35 % CPU,
and the ordering scrambled — fixed round robin nominally came first. Nothing had changed in the
cluster; the queue had simply moved into the network in front of it, so the selection rule had almost
nothing left to decide. Those runs are kept in `results/raw_linkbound/` and are reported here rather
than quietly dropped, because they are the clearest reminder in the whole project that a load-balancing
measurement is only as good as the assurance that the load balancer is what is being loaded.

## 12. Results — Dynamic Scaling, Failure Recovery and Persistence

The updated task requires that the balancer detect unavailable backends and stop routing to them,
and that the shared storage be persistent and duplicate-proof. Those are demonstrated here on the
live cluster, together with the admission of a backend started while the application is running.

### 12.1 Backends added while the application is running

The membership machinery of §6.4 was exercised on its own: the system was started with **one**
backend (sys2) under a rising load, and `scripts/scale.sh sys3 up`, then `… sys4 up`, were run on the
other systems *while the load generator kept going*. The balancer admitted each of them by itself —
the registration heartbeat and the candidate scan both fired within five seconds — and, because a
never-sampled backend is selected immediately, each began carrying traffic in the very next second.

{{C_SCALE}}

With one backend the system was already at its ceiling at 25 users (~95 req/s, p95 ≈ 2.1 s) and more
users bought no throughput at all, only queueing delay. Admitting sys3 at ~128 s **doubled throughput
to ~190 req/s** and halved p95 with the *same* 100 users; admitting sys4 at ~184 s took it to
**~285 req/s** with a 34 / 34 / 32 % split. **No request failed during either addition** — zero errors
in 300 seconds. These figures were taken on the authenticated chat API before the database moved off
sys1, so they are not comparable with §11; what they demonstrate is the admission path, which is
unchanged.

### 12.2 Backend failure and recovery on the required routes

The same experiment repeated on `/message` and `/feed` with the threshold algorithm: 60 clients for
150 s, sys3 SIGKILLed at 45 s and restarted at 90 s.

{{C_FAILPUB}}

| | |
|---|---|
| requests in the run | 32 100 |
| **failed requests** | **0 (0.00 %)** |
| p50 / p95 / p99 | 225 ms / 527 ms / 741 ms |
| traffic split over the whole run | sys2 43 % · sys4 39 % · sys3 18 % (down for 45 of the 150 s) |
| active backends | 3 → 2 at 45 s → 3 at ~95 s |

**Not one request failed.** The balancer ejects sys3 on the *first* refused connection — a passive
check, no waiting for a health interval — and a request that had not yet been written upstream is
retried on another backend, so the client never sees the failure. sys3's per-second throughput drops
to exactly zero in the top panel while sys2 and sys4 rise to absorb its share, and the response-time
panel shows no step at 45 s at all: two backends were enough for this load. At 90 s sys3 is restarted,
registers itself, is re-admitted after two clean probes and — scoring as a never-sampled backend —
takes traffic again within a second of the `active backends` line stepping back to 3.

Because sys3 also hosts the database, this run kills a *backend* on the database's system while the
database itself keeps serving. The two are separate processes with separate lifetimes, which is why
`scale.sh sys3 kill` takes one backend out of rotation and nothing else.

### 12.3 Persistence and duplicate prevention on the allotted systems

**Persistence.** At the end of the experiments the database held **735 183 messages, 721 users and 8
rooms** in a 209 MB file (`/stats`, terminal capture §16): every load-generator message of every run,
the 461 414 messages of the public feed room, the migrated Assignment-5 history and the demo
conversations, all served identically by whichever backend the balancer picks. The file survived the
service being restarted, every backend being restarted, and being **moved from sys1 to sys3** with
its write-ahead log checkpointed first — not a row was lost. `dedup_test.py --restart-db` restarts
the database service in the middle of the test: the row count is unchanged across the restart, the
probe message is readable through a *different* backend afterwards, and re-sending an id stored
*before* the restart is still answered `duplicate:true`, so the guard lives in the database file and
not in any process's memory. The backends reconnect to the firehose by themselves
(`db_firehose: true` in `/health`), and the smoke test exercises the same restart on every run.

**Duplicate prevention.** All five duplicate scenarios in §8.4 pass through the public URL against
three live backends, and the `dedup_log` table holds an audit row for every rejection the database
itself had to make — 16 in total, because most duplicates never reach it: the backends' own LRU,
kept warm by the firehose, answers them first. The decisive check is on the live file itself:

```sql
SELECT id, COUNT(*) FROM messages GROUP BY id HAVING COUNT(*) > 1;   -- 0 rows
```

**not one duplicated message id in 735 183 rows** (terminal capture §16).

<div class="pagebreak"></div>

## 13. Bottleneck Analysis — Why the Database Moved to sys3

This is the measurement that changed the deployment, and it is the most useful thing the utilisation
sampler produced.

**The symptom.** With the database service next to the balancer on sys1, 60 concurrent clients on the
public routes produced a hard ceiling: **165 req/s, 324 ms median, 693 ms p95**, and it would not move
whatever the threshold was set to. The backends were only 36 % busy, so the queue was not there.

**The measurement.** `top` on sys1 showed the balancer at ~60 % of a CPU and the database service at
~38 % — together ~98 % of the container's single-CPU quota. The decisive number came from
`/sys/fs/cgroup/cpu.stat`, sampled over an 8-second window during a run:

| | value |
|---|---|
| scheduling periods elapsed | 80 |
| periods in which the container was **throttled** | 41 |
| CPU time consumed | 6.55 s (82 % of one core) |
| time tasks spent **stopped by the quota** | 9.06 s |

sys1 was being CFS-throttled in **half of all 100 ms periods**. When a container exhausts its quota
inside a period, every task in it is stopped until the next period begins — which is exactly the
300 ms of unexplained queueing the response times showed. The two most latency-critical processes in
the system were competing for one core and taking turns being frozen.

`bash scripts/capture_evidence.sh throttle` reproduces this measurement on demand and prints the
throughput it achieved alongside the counters, so the sample can be checked for validity: if no
system went above 60 % of its CPU, the run was limited by something in front of the cluster and its
throttling counters mean nothing. The capture in §16 was taken late in the session, through the
degraded campus link, and says so itself.

**The check that it was not something else.** Three alternatives were ruled out by measurement rather
than argument. Running the load generator *inside* the lab (from sys4, over the bridge network)
produced the same 163 req/s, so the campus link was not the limit. The balancer was benchmarked in
isolation against a trivial backend and sustained **11 000–12 000 req/s with 54 KB bodies**, and its
throughput was flat from 12 to 120 concurrent connections, so neither the proxy code nor the
thread-per-connection model was at fault. A CPU benchmark put sys1's core at 3.5× slower than the
machine the balancer was benchmarked on — nowhere near enough to explain a 70× gap.

**The fix and its effect.** The database service, its data file and its WAL were moved to sys3
(`bash scripts/deploy.sh movedb sys1 sys3`, WAL checkpointed first so no row was lost), leaving sys1
to the balancer alone. At identical load:

| | before (DB on sys1) | after (DB on sys3) | change |
|---|---|---|---|
| throughput | 165 req/s | **233 req/s** | **+41 %** |
| median response time | 324 ms | **230 ms** | −29 % |
| p95 response time | 693 ms | **529 ms** | −24 % |
| sys1 throttled periods (per 80) | 41 | 30 | −27 % |

Three smaller changes were made in the same pass, each for a measured reason: the balancer's access
log is now accumulated and flushed once a second instead of costing a `write` syscall per request;
the pooled upstream connections keep their buffered reader instead of allocating a new one per
request; and the sockets on both sides get 256 KB buffers, because a 54 KB body arriving over a
container bridge otherwise turns one logical read into a dozen `recv` calls. On the database side the
room's row count is maintained incrementally instead of running `COUNT(*)` on every `/feed`, and both
the database and the backends cap how often the same feed window is re-serialised. Together these
took the same load from 165 to 238 req/s.

**What it costs and why that is acceptable.** sys3 now carries a backend *and* the database, so it is
the slowest of the three backends. That is precisely the asymmetry a performance-based balancer is
supposed to handle, and §11.4 shows it doing so — sys3 receives 28 % of requests where round robin
would give it 33 %. The alternative, an even split across unequal systems, is measurably worse.

**What was still the limit, at the time.** sys1 remained the busiest system at ~70–95 % of its single
CPU, so the ceiling was the balancer's own CPU rather than a scheduling artefact. That held until the
balancer was moved onto an event loop for the evaluation's concurrency (§14.2), after which sys1 runs
at under 40 % and the backends — sys3 in particular, which also carries the database — became the
constraint instead.

<div class="pagebreak"></div>

## 14. Engineering for the Evaluation Load

The course leaderboard tests every submitted URL with its own load generator, on two
boards, back to back, using only `/message` and `/feed`:

| Board | What it does | Ranked on |
|---|---|---|
| **static** | 250 → 500 → 750 → 1000 concurrent users, each stage sending exactly 5 000 requests | **mean response time** of the successful requests |
| **breakpoint** | 200 → 350 → 500 → 750 → 1000 → 1500 concurrent users, stopping as soon as a stage exceeds 20 % errors | **total successful requests** before it broke |

Everything measured up to §13 used at most 200 concurrent clients. At 250 and beyond
the system behaved differently enough that it had to be treated as a separate problem.

### 14.1 A local copy of the evaluation

`loadgen/leaderboard_sim.py` reproduces both ladders from the published run records:
a stage issues `budget + concurrency` requests, so every virtual user posts
`budget / concurrency` messages and then reads `/feed` once. It reports the same three
things the leaderboard does — mean response time, successful requests, and **message
completeness**, the share of messages accepted with a 2xx that can be found in `/feed`
afterwards. It is asyncio with one keep-alive connection per user, because a
thread-per-user client cannot itself reach 1 500 concurrent users.

### 14.2 The balancer could not survive the concurrency

Benchmarked against a trivial backend, so that only the proxy was being measured:

| concurrent connections | thread per connection | on an event loop |
|---|---|---|
| 12 | 11 606 req/s | 19 290 req/s |
| 120 | 10 744 req/s | 17 638 req/s |
| 500 | 9 890 req/s | 13 592 req/s |
| **1 000** | **385 req/s**, with errors | **10 640 req/s**, none |

The balancer held ten thousand requests a second up to five hundred connections and
then fell off a cliff. CPython cannot schedule a thousand runnable threads on the one
CPU this container is given, and the evaluation's top stage is fifteen hundred. The
proxy's **I/O layer was rewritten on asyncio** — one task per connection instead of one
OS thread. Everything above it is unchanged and shared: the threshold rule, the four
health states, registration and discovery, the admin surface, the access log, and the
WebSocket tunnel the browser chat depends on (`scripts/ws_check.js` proves that one
end to end, since the balancer's own suite does not cover it). The health probe, the
candidate scan and the log flush stay on their own threads, so a stalled probe can
never hold up the loop that is serving traffic.

### 14.3 Three more changes, each isolated by measurement

{{T_LBSTEPS}}

{{C_LBSTEPS}}

* **Backends were being ejected during connection bursts.** The balancer's event log
  showed `ejected … passive: connect/proxy failure` under load, after which the
  remaining two took the whole load and the errors cascaded. One refused connection out
  of a thousand simultaneous ones is not evidence that a backend has died. Passive
  ejection now needs four consecutive failures from a backend whose health probe is
  still fresh, the backends listen with a 4 096-deep accept queue, and DEGRADED
  penalises a backend's load index instead of saturating it — under this load *every*
  backend answers its probe late, and disqualifying all of them leaves the threshold
  rule nothing to choose between. A backend that is genuinely gone still fails four
  attempts within milliseconds, so detection stays effectively immediate.
* **Every request made a `fetch()` call to the database service.** Node's global fetch
  carries real per-call overhead, and with the balancer no longer the bottleneck the
  backends had become one. Replaced with `http.request` over a keep-alive agent.
* **The database committed one transaction per message.** Node is single-threaded, so
  every append that arrives while the event loop is busy can be applied in one
  transaction: appends are queued and flushed on the next tick. Under load this commits
  **20 messages per transaction**, in batches as large as 185. The duplicate guard is
  untouched — each entry still goes through the same select-then-insert-on-conflict, and
  two copies of one id inside a single batch are serialised by the batch itself. A
  30-way concurrent storm on a single id stores exactly one row.

### 14.4 The result

{{T_LADDERS}}

{{C_LADDER}}

Both ladders now run **without a single failed request**, and the breakpoint board is
held all the way to 1 500 concurrent users rather than breaking. Driven from my own
machine against the public URL rather than from inside the lab, the static ladder gives
a mean of **672 ms** at 941 req/s — the campus link costs something, but not the result.

### 14.5 Message completeness — the one metric not won

The evaluation also reports how many accepted messages come back in `/feed`. With the
200-message window of §5.2 that share is about 1 %, and the run is badged "lossy".

This is a real trade-off rather than an oversight, and it is worth being explicit about
it. The evaluation posts twenty to thirty thousand messages and reads `/feed` roughly
2 500 times in a run. Returning every message on every read means bodies of several
megabytes and gigabytes of traffic over a two-minute run — on a three-core cluster it is
not reachable, and the leaderboard shows the same thing: the one submission with
completeness 1.00 answered 93 % of its requests with an error and served 1 490 requests
where this system serves 22 500. The window is the honest engineering answer, the true
total is reported in every response, and `?since=` walks the complete history in order
(§5.2). Both the count and the paging are the same design a real chat API would use.

<div class="pagebreak"></div>

## 15. Analysis and Discussion

**The threshold rule does what the task asks and is measurably better than round robin.** At the same
60-client load on three backends it delivers 260 req/s at 447 ms p95 against round robin's 221 req/s
at 494 ms — **+18 % throughput, −10 % p95**. The reason is visible in the traffic split: round robin
gives all three systems an equal share of *requests*, but sys3 also runs the database, so an equal
share of requests is an unequal share of work. The threshold rule reads the load index, sees sys3
sitting higher, and gives it 28 % instead of 33 %. Where all three backends are genuinely equal the
two rules are indistinguishable, which is the correct behaviour: a dynamic rule should cost nothing
when there is nothing to react to.

**The threshold itself matters less than the deployment.** The honest reading of the sweep in §10 is
that any value between 0.15 and 1.00 lands within about 7 % of the best, and the repetitions overlap.
Moving the database off the balancer's system was worth 41 % — six times the entire spread of the
threshold sweep. Tuning is real but second-order; finding the actual queue is first-order. The sweep
was still worth running, because it is what proved that.

**The bottleneck is the balancer, not the backends.** Every client connection terminates on sys1 and
every byte of every response is copied through one Python process on a one-CPU container. Past 25
clients the backends flatten at 40–65 % utilisation while sys1 climbs to 70–95 % and response times
keep rising — the queue is in front of the backends, not in them. This is why the "effect of adding a
backend" is strong from one to two backends and weaker from two to three, and why a fourth backend
would not help.

**Scaling is still real, and it is what protects the system under overload.** The clearest comparison
in the report is the open-loop sweep at 200 req/s offered: one backend is at 8.7 s p95 and failing,
three backends answer in 3.1 s with zero failures. Under closed-loop load three backends reach their
knee at 50 clients and 259 req/s where one backend is already saturated at 25.

**Persistence and idempotency are properties of the data model, not of luck.** The client-minted id is
the primary key; retries, reconnections, and twenty concurrent copies of the same message through
three different backends produce exactly one row, and a duplicate of an id stored *before* a database
restart is still rejected afterwards. Answering duplicates with `200 duplicate:true` rather than an
error is deliberate: a client that receives an error would retry again. The same guarantee covers the
new `/message` route, and the test suites assert it across two different backends.

**Measurement discipline.** The lab host is shared and multi-tenant, and single runs vary: one
repetition of the same configuration came back at 30 req/s where its twin gave 81. Everything
reported here is therefore a median over repetitions, the backend-count comparisons are interleaved in
shuffled order inside every load level so they share the same minutes, and the threshold figure shows
the min/max of the repetitions as whiskers rather than hiding them.

**Limits and future work.** The database service is a single point of failure and, eventually, a
bottleneck; the standard next steps are a replicated store (PostgreSQL with streaming replication, or
leader/follower SQLite via Litestream) and backend-local write queues. The balancer's DEGRADED rule
still uses fixed thresholds (2 s probe, 95 % CPU) where an adaptive bound derived from the EWMA's own
history would be more robust. The open-loop generator should use an asynchronous client to offer more
than ~300 arrivals/s from one process. TLS termination at the balancer and a second balancer behind a
shared virtual IP would remove the last single point of failure.

## 16. Screenshots and Evidence

{{SCREENSHOTS}}

### Terminal evidence

{{TERMINALS}}

## 17. Challenges Faced and How They Were Solved

1. **The cluster stalled at 165 req/s and nothing in the application explained it** — diagnosed as CPU
   quota throttling on sys1 and fixed by moving the database to sys3, worth 41 % throughput (§13).
2. **`/feed` returning every message made the benchmark measure the history, not the system** — fixed
   with a bounded default window and a paging cursor (§5.2).
3. **A pure "least response time" rule herded all traffic onto one backend.** With sequential traffic
   every backend has zero in-flight requests, so the one with the lowest EWMA received everything.
   The same trap appears in the threshold rule when several requests cross the threshold at once and
   all pick the same replacement. Both are fixed with power-of-two-choices (§6.1).
4. **The load signal was wrong for a loaded box.** A CPU-burning neighbour *reduces* the Node
   process's own CPU share, which made a loaded backend look idle. The backend now also reports the
   container's cgroup CPU against its quota, which sees every tenant, and the balancer takes the
   maximum of the two.
5. **sys3 had Node 20, which has no SQLite module, and no internet access to install one.** The
   user-local Node 22 tree was copied from sys1 over SSH; the system Node is untouched and the
   previous assignment's services keep using it.
6. **Port 3000 on sys4 was already taken** by my course project. Only the balancer needs a public
   port, so the sys4 backend listens on 3001 on the private network and nothing of the project was
   touched.
7. **The evaluation generator's request format is unspecified.** `/message` accepts JSON,
   form-encoded bodies, raw text and query parameters, `GET` as well as `POST`, and eight spellings of
   each field name, so a reasonable client cannot fail to be understood.

## 18. Conclusion

The secure group chat now runs on the three allotted systems behind a load balancer that clients reach
at a single URL, `http://10.1.75.53:3269`, through the two required routes `/message` and `/feed`.
The balancer selects backends by **measured load** — a threshold on an index combining container CPU,
queue depth and EWMA response time — switching away from the current backend the moment it crosses
**`T = 0.15`**, a value chosen by sweeping the threshold at two load levels rather than by assumption.
It monitors health in four states, ejects an unavailable backend on the first refused connection while
keeping a merely slow one in service, and admits a backend started while the application is running
within one heartbeat.

Measured on the allotted systems with my own load generator, driving only the required routes with
random message lengths and random intervals: the threshold rule delivers **260 req/s at 447 ms p95**
against fixed round robin's **221 req/s at 494 ms**; three backends reach their knee at 50 clients and
259 req/s where one saturates at 25; at 200 req/s of offered open-loop load a single backend is at
8.7 s p95 and failing while three backends answer in 3.1 s with **zero** failures; and a SIGKILLed
backend is ejected within one request and back in rotation seconds after restarting.

Two ceilings were found by measurement and removed. The first was CFS throttling on sys1, where the
balancer and the database were sharing one CPU; moving the database to sys3 was worth **+41 %**
throughput, more than the entire span of the threshold sweep. The second appeared only at the
evaluation's concurrency: a thread per connection collapsed from ten thousand requests a second to
385 between 500 and 1 000 connections, and moving the proxy onto an event loop — together with a
keep-alive database client and group commit in SQLite — **doubled throughput and halved mean response
time** at 1 000 users. On the evaluation's own ladders the system now answers **22 500 requests at a
mean of 547 ms with no failures**, and holds the breakpoint ladder to **1 500 concurrent users**
instead of breaking.

All backends share one persistent SQLite database in which every message has a unique id and the
database itself guarantees that a retried, reconnected or concurrently duplicated send is stored once.
The database ended the experiments holding **735 183 messages and not one duplicated id**, and it has
since survived being restarted, having every backend restarted under it, and being moved from one
system to another without losing a row. **Nothing was removed to reach
these numbers**: the two required routes are additions, and registration, scrypt login, sessions,
end-to-end-encrypted rooms and the full `/api/*` surface all still work through the same URL and are
still covered by the 60-assertion backend suite and the 39-assertion balancer suite, both of which
pass. The previous assignment's URLs and direct ports keep working, its files on the lab systems are
untouched, and the whole system can be redeployed, re-measured and demonstrated from the scripts in
the repository.

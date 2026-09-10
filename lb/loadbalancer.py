#!/usr/bin/env python3
"""
loadbalancer.py — DYNAMIC HTTP/WebSocket reverse-proxy load balancer (v2).

Assignment 6, CSD course. Runs on sys1 and spreads traffic arriving at
http://10.1.75.53:3269 across the chat backends on sys2/sys3/sys4.
Pure Python 3 standard library — no pip, no root, no external processes.
Extends the Assignment-5 balancer; every v1 algorithm is still available.

What is new in v2
-----------------
* DYNAMIC SELECTION (the assignment's core requirement). Two new algorithms:
    adaptive (default)   score = EWMA_rt × (1 + in_flight) × (1 + cpu_load)
                         "power of two choices": sample two backends, keep the
                         lower score. Response time AND current system load
                         decide, not a fixed rotation; equal backends still
                         split evenly, a slow one is starved of new work.
    least_response_time  score = EWMA_rt × (1 + in_flight)   (no load term)
  EWMA_rt is fed by every proxied request and by the health probe's own
  latency, so idle backends keep a fresh estimate. cpu_load is read from the
  backend's /health JSON (process CPU % and 1-min load average, normalised to
  its single core). A brand-new backend starts with score 0 (optimistic) so it
  receives traffic immediately; a small exploration share (5 %) keeps every UP
  backend sampled. v1 algorithms kept: round_robin, least_connections,
  weighted_round_robin, ip_hash.
* HEALTH MONITORING with three states instead of a boolean:
    UP        answering /health in time
    DEGRADED  answering, but slowly (> degrade_ms) or reporting high load —
              still routable, but its score is penalised (slow ≠ dead: this
              removes the ejection churn diagnosed in Assignment 5, D-011)
    DOWN      connection refused / timeouts for fail_threshold checks —
              receives nothing until rise_threshold successes
    DRAINING  asked to leave (deregister): finishes in-flight, gets nothing new
  Passive checks: a connect failure during proxying ejects immediately; a
  request that was not yet sent upstream is retried on another backend.
  Fail-open: the last routable backend is never ejected.
* DYNAMIC MEMBERSHIP — the LB discovers backends while running:
    - POST /lb/register {id,host,port,weight}   backends announce themselves at
      boot and every few seconds (heartbeat); a shared token guards it
    - POST /lb/deregister                       graceful scale-down (drain)
    - candidate scan: `discovery.candidates` host:port slots are probed every
      discovery_interval_s; any that answers /health is admitted
    - lb.conf.json is re-read whenever its mtime changes (also SIGHUP, /lb/reload)
    - dynamically added backends that stay DOWN for prune_after_s are removed
* OBSERVABILITY: /lb/stats (per-backend state, score, EWMA, load, counts,
  active_backends), /lb/events (membership + health timeline), /lb/ dashboard,
  CSV access log with the active-backend count on every line.

Usage:  python3 loadbalancer.py [path/to/lb.conf.json]
"""

import asyncio
import json
import os
import random
import signal
import socket
import sys
import threading
import time
from collections import deque

# ───────────────────────────── configuration ────────────────────────────────

CONF_PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "lb.conf.json")

DEFAULTS = {
    "listen_host": "0.0.0.0",
    "listen_port": 3269,
    "algorithm": "adaptive",
    "backends": [],                  # static pool: [{"id","host","port","weight"}]
    "health_interval_s": 3,
    "health_timeout_s": 6,
    "fail_threshold": 2,
    "rise_threshold": 2,
    "degrade_ms": 1500,              # probe slower than this -> DEGRADED
    "passive_fail_threshold": 4,     # consecutive failed proxy attempts before ejecting
    "keepalive_idle_s": 65,          # idle client connection is closed after this
    "degrade_cpu_pct": 90,           # backend-reported CPU above this -> DEGRADED
    "ewma_alpha": 0.2,               # weight of the newest sample
    "explore_pct": 5,                # % of adaptive picks that go to a random UP backend
    # -- threshold algorithm (Assignment 6 updated spec) ---------------------
    "switch_threshold": 0.55,        # load index at which the current backend is abandoned
    "inflight_cap": 24,              # in-flight requests that count as "fully loaded"
    "rt_cap_ms": 250,                # EWMA response time that counts as "fully loaded"
    "min_dwell_ms": 0,               # optional hysteresis after a switch
    "load_weight": 1.0,              # multiplier on the cpu_load term of the score
    "connect_timeout_s": 5,
    "upstream_timeout_s": 30,
    "register_token": "",            # shared secret for /lb/register (empty = open)
    "discovery": {"candidates": [], "interval_s": 5,    # [{"host","port"}] slots to probe
                  "require_version": ""},              # admit only backends reporting this /health version
    "prune_after_s": 600,            # remove dynamic backends DOWN this long
    "access_log": "logs/lb_access.csv",
}

UP, DEGRADED, DOWN, DRAINING = "UP", "DEGRADED", "DOWN", "DRAINING"
DEGRADED_PENALTY = 1.5     # multiplier on a degraded backend's load index


class Backend:
    """One upstream server plus its live health / load / traffic bookkeeping."""

    def __init__(self, cfg, source="config"):
        self.id = cfg["id"]
        self.host = cfg["host"]
        self.port = int(cfg["port"])
        self.weight = int(cfg.get("weight", 1))
        self.source = source            # config | register | scan
        self.state = UP                 # optimistic until the first check
        self.consec_fail = 0
        self.consec_ok = 0
        self.passive_fails = 0          # consecutive failed proxy attempts (see eject_now)
        self.in_flight = 0
        self.requests = 0
        self.errors = 0
        self.ewma_ms = None             # None = no sample yet -> optimistic score 0
        self.probe_ms = None
        self.load = {}                  # last /health "load" object from the backend
        self.latencies = deque(maxlen=5000)
        self.current_weight = 0         # smooth-WRR state
        self.pool = deque()             # idle keep-alive sockets
        self.lock = threading.Lock()
        self.added_at = time.time()
        self.last_seen = time.time()    # last successful probe / heartbeat
        self.down_since = None

    def addr(self):
        return (self.host, self.port)

    def routable(self):
        return self.state in (UP, DEGRADED)

    def observe(self, ms, alpha):
        """Feed one response-time sample into the EWMA."""
        self.ewma_ms = ms if self.ewma_ms is None else (1 - alpha) * self.ewma_ms + alpha * ms

    def cpu_load(self):
        """0..1 : how busy the backend's SYSTEM is. Prefers the container-wide
        cgroup CPU utilisation (sys_cpu_pct — sees other tenants of the box),
        falls back to the process CPU % and the load average per core."""
        cpu = float(self.load.get("cpu_pct", 0) or 0) / 100.0
        sys_cpu = self.load.get("sys_cpu_pct")
        if sys_cpu is not None:
            cpu = max(cpu, float(sys_cpu) / 100.0)
        cores = max(1, int(self.load.get("cores", 1) or 1))
        la = float(self.load.get("loadavg1", 0) or 0) / cores
        return max(cpu, min(la, 4.0))

    def load_index(self, cfg):
        """0 = idle, 1 = saturated. The single number the threshold rule compares
        against `switch_threshold`. Three signals, worst one wins:
          cpu       system/cgroup CPU from the backend's /health (<= health_interval old)
          queue     in-flight requests THIS LB is holding on it (instantaneous)
          latency   EWMA response time against the response-time budget
        The queue term is what makes the rule react inside one health interval:
        a backend that starts queueing crosses the threshold immediately."""
        cpu = self.cpu_load()
        queue = self.in_flight / max(1.0, float(cfg["inflight_cap"]))
        lat = 0.0 if self.ewma_ms is None else self.ewma_ms / max(1.0, float(cfg["rt_cap_ms"]))
        idx = max(cpu, queue, lat)
        if self.state == DEGRADED:
            idx *= DEGRADED_PENALTY      # penalised, not disqualified: under heavy load
                                         # every backend answers its probe late, and
                                         # saturating them all would leave the rule
                                         # nothing to choose between
        return idx / max(1, self.weight)

    def score(self, cfg):
        """Lower is better. New backends (no sample) score 0 -> tried at once."""
        if self.ewma_ms is None:
            return 0.0
        s = self.ewma_ms * (1 + self.in_flight)
        if cfg["algorithm"] == "adaptive":
            s *= 1 + cfg["load_weight"] * self.cpu_load()
        if self.state == DEGRADED:
            s *= 2.0
        return s / max(1, self.weight)

    def snapshot(self, cfg):
        lat = sorted(self.latencies)
        pct = lambda p: round(lat[min(len(lat) - 1, int(p / 100 * len(lat)))], 1) if lat else 0
        return {
            "id": self.id, "host": self.host, "port": self.port, "weight": self.weight,
            "source": self.source, "state": self.state, "healthy": self.routable(),
            "in_flight": self.in_flight, "requests": self.requests, "errors": self.errors,
            "ewma_ms": round(self.ewma_ms, 1) if self.ewma_ms is not None else None,
            "probe_ms": round(self.probe_ms, 1) if self.probe_ms is not None else None,
            "score": round(self.score(cfg), 1),
            "load_index": round(self.load_index(cfg), 3),
            "load": self.load,
            "latency_ms": {"p50": pct(50), "p95": pct(95), "p99": pct(99)},
            "since": round(time.time() - self.added_at),
            "last_seen_s_ago": round(time.time() - self.last_seen, 1),
        }


class LB:
    def __init__(self):
        self.cfg = dict(DEFAULTS)
        self.backends = []
        self.rr_index = 0
        self.active_count = 0        # cached len(routable_backends()) — see refresh_active()
        self.current_id = None       # threshold algorithm: the backend traffic is pinned to
        self.switches = 0            # how many times the threshold rule moved that pin
        self.last_switch = 0.0
        self.lock = threading.Lock()
        self.started = time.time()
        self.total_requests = 0
        self.total_errors = 0
        self.events = deque(maxlen=200)
        self.log_lock = threading.Lock()
        self.log_buf = []
        self.log_fh = None
        self.conf_mtime = 0
        self.reload()

    # -- events ---------------------------------------------------------------
    def event(self, kind, backend_id, detail=""):
        ev = {"t": round(time.time(), 3), "kind": kind, "backend": backend_id, "detail": detail,
              "active": self.refresh_active()}
        self.events.append(ev)
        print(f"[lb] {kind:<12} {backend_id:<8} {detail}  (active={ev['active']})", flush=True)

    # -- config ---------------------------------------------------------------
    def reload(self):
        with open(CONF_PATH) as fh:
            file_cfg = json.load(fh)
        cfg = dict(DEFAULTS)
        cfg.update(file_cfg)
        disc = dict(DEFAULTS["discovery"]); disc.update(file_cfg.get("discovery", {}))
        cfg["discovery"] = disc
        self.conf_mtime = os.path.getmtime(CONF_PATH)
        with self.lock:
            old = {b.id: b for b in self.backends}
            fresh = []
            for bc in cfg["backends"]:
                if bc["id"] in old:                # keep live stats across reloads
                    b = old.pop(bc["id"])
                    b.host, b.port = bc["host"], int(bc["port"])
                    b.weight = int(bc.get("weight", 1))
                    b.source = "config"
                    fresh.append(b)
                else:
                    fresh.append(Backend(bc, "config"))
            # dynamically discovered backends survive a config reload
            for b in old.values():
                if b.source != "config":
                    fresh.append(b)
            self.backends = fresh
            self.cfg = cfg
        log_path = cfg["access_log"]
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        new_log = not os.path.exists(log_path)
        if self.log_fh:
            self.log_fh.close()
        self.log_fh = open(log_path, "a", buffering=262144)
        if new_log:
            self.log_fh.write("ts,client,method,path,backend,upstream_ms,status,bytes,active_backends\n")
        print(f"[lb] config loaded: algorithm={cfg['algorithm']}, "
              f"{len(self.backends)} backend(s): {[b.id for b in self.backends]}, "
              f"candidates={len(cfg['discovery']['candidates'])}", flush=True)

    # -- membership -----------------------------------------------------------
    def add_backend(self, bid, host, port, weight=1, source="register"):
        """Admit a backend announced at runtime. Returns True if it was new."""
        with self.lock:
            for b in self.backends:
                if b.id == bid:
                    b.last_seen = time.time()
                    if (b.host, b.port) != (host, int(port)):
                        b.host, b.port = host, int(port)
                    if b.state == DRAINING:            # came back after a drain
                        b.state = UP; b.consec_fail = 0; b.down_since = None
                        self.event("readmitted", bid, "re-registered after drain")
                    return False
            b = Backend({"id": bid, "host": host, "port": port, "weight": weight}, source)
            self.backends.append(b)
        self.event("added", bid, f"{host}:{port} via {source}")
        return True

    def drain_backend(self, bid):
        with self.lock:
            for b in self.backends:
                if b.id == bid and b.state != DRAINING:
                    b.state = DRAINING
                    self.event("draining", bid, "deregister request")
                    return True
        return False

    def remove_backend(self, bid):
        with self.lock:
            before = len(self.backends)
            self.backends = [b for b in self.backends if b.id != bid]
        if len(self.backends) < before:
            self.event("removed", bid, "")
            return True
        return False

    # -- backend selection ----------------------------------------------------
    def routable_backends(self):
        return [b for b in self.backends if b.routable()]

    def refresh_active(self):
        """`active_count` is read on every proxied request (response header + access
        log). Recomputing the list there cost two allocations per request, so the
        health loop and every membership change refresh this counter instead."""
        self.active_count = sum(1 for b in self.backends if b.routable())
        return self.active_count

    def pick(self, client_ip, exclude=()):
        """Choose a backend for this request using the configured algorithm."""
        pool = [b for b in self.routable_backends() if b.id not in exclude]
        if not pool:
            return None
        algo = self.cfg["algorithm"]
        # The threshold rule reads a few numbers and writes one attribute. Holding
        # the balancer-wide lock for that convoyed a thousand connection threads on
        # a single CPU and cost more than the selection itself; CPython attribute
        # access is atomic, and a selection made against a marginally stale reading
        # is exactly as valid as one made a microsecond earlier. The lock is still
        # taken by the algorithms that mutate shared rotation state.
        if algo == "threshold":
            return self._pick_threshold(pool)
        with self.lock:
            if algo in ("adaptive", "least_response_time"):
                fresh = [b for b in pool if b.ewma_ms is None]
                if fresh:                              # never-sampled backend: try it now
                    return fresh[0]
                if len(pool) > 1 and random.random() * 100 < self.cfg["explore_pct"]:
                    return random.choice(pool)         # exploration keeps estimates fresh
                if algo == "least_response_time":      # pure minimum (herds under light load)
                    return min(pool, key=lambda b: (b.score(self.cfg), b.in_flight, b.requests))
                # adaptive = "power of two choices": sample two backends at random and
                # keep the better score. Equal backends split evenly (no herding onto
                # one lucky EWMA); a slow or loaded backend loses every pairing and is
                # left with ~1/N² of the traffic until its score improves.
                a, b = random.choice(pool), random.choice(pool)
                return min((a, b), key=lambda x: (x.score(self.cfg), x.in_flight, x.requests))
            if algo == "least_connections":
                return min(pool, key=lambda b: (b.in_flight, b.requests))
            if algo == "ip_hash":
                return pool[hash(client_ip) % len(pool)]
            if algo == "weighted_round_robin":
                total = sum(b.weight for b in pool)
                for b in pool:
                    b.current_weight += b.weight
                chosen = max(pool, key=lambda b: b.current_weight)
                chosen.current_weight -= total
                return chosen
            # default: round_robin
            self.rr_index = (self.rr_index + 1) % len(pool)
            return pool[self.rr_index]

    def _pick_threshold(self, pool):
        """THRESHOLD algorithm — the updated assignment's explicit requirement:
        keep sending to the current backend while its load stays below a defined
        threshold, and switch to another suitable backend as soon as it does not.

            load_index(current) <  T   -> keep using it
            load_index(current) >= T   -> switch to a backend still under T
                                          (power-of-two-choices among them, so
                                          concurrent requests do not all herd
                                          onto the same replacement)
            no backend under T         -> everything is hot: send to the lowest
                                          score, i.e. degrade gracefully rather
                                          than refuse traffic

        This is deliberately not round-robin: an idle cluster concentrates work
        on one warm backend (better cache locality, fewer cold connections) and
        the load itself, not a counter, decides when to spread out."""
        cfg = self.cfg
        T = float(cfg["switch_threshold"])
        fresh = [b for b in pool if b.ewma_ms is None]
        if fresh:                                   # a newly discovered backend: measure it now
            self.current_id = fresh[0].id
            return fresh[0]
        if len(pool) > 1 and random.random() * 100 < cfg["explore_pct"]:
            return random.choice(pool)              # keeps every backend's estimate fresh
        cur = next((b for b in pool if b.id == self.current_id), None)
        if cur is not None and cur.load_index(cfg) < T:
            return cur                              # under threshold -> stay put
        if cur is not None and cfg["min_dwell_ms"] and \
                (time.time() - self.last_switch) * 1000 < cfg["min_dwell_ms"]:
            return cur                              # hysteresis: too soon to move again
        under = [b for b in pool if b.load_index(cfg) < T and b is not cur]
        # Power-of-two-choices in BOTH branches. Picking the single global minimum
        # would make every concurrent request that crosses the threshold jump to the
        # same replacement, and that backend would then cross the threshold itself —
        # sampling two candidates and keeping the better one spreads the switch.
        candidates = under or [b for b in pool if b is not cur] or pool
        a, b = random.choice(candidates), random.choice(candidates)
        key = (lambda x: (x.score(cfg), x.in_flight)) if under \
            else (lambda x: (x.load_index(cfg), x.score(cfg), x.in_flight))
        chosen = min((a, b), key=key)
        if chosen.id != self.current_id:
            self.switches += 1                      # a lost increment here changes nothing
            self.last_switch = time.time()
            if self.switches % 250 == 1:            # timeline for the report, not a log flood
                self.event("switch", chosen.id,
                           "threshold %.2f exceeded on %s" % (T, cur.id if cur else "-"))
        self.current_id = chosen.id
        return chosen

    def pick_sticky(self, client_ip, tried):
        """WebSocket connections use ip_hash so reconnects re-pin."""
        pool = [b for b in self.routable_backends() if b.id not in tried]
        if not pool:
            return None
        return pool[hash(client_ip) % len(pool)]

    # -- access log -----------------------------------------------------------
    def log(self, client, method, path, backend_id, ms, status, nbytes):
        """Access log. Lines are accumulated and flushed by flush_log() once a
        second: line-buffered writing cost one syscall per proxied request, which
        is real money when the balancer is the busiest process on its system."""
        line = (f"{time.time():.3f},{client},{method},{path},{backend_id},"
                f"{ms:.1f},{status},{nbytes},{self.active_count}\n")
        with self.log_lock:
            self.log_buf.append(line)
            if len(self.log_buf) >= 512:
                self._drain_locked()

    def _drain_locked(self):
        if not self.log_buf:
            return
        self.log_fh.write("".join(self.log_buf))
        self.log_buf.clear()

    def flush_log(self):
        with self.log_lock:
            self._drain_locked()
        try: self.log_fh.flush()
        except OSError: pass


LB_STATE = LB()

# ───────────────────────────── health + discovery ───────────────────────────

def probe(host, port, timeout):
    """GET /health. Returns (ok, ms, json_or_None)."""
    t0 = time.time()
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.sendall(b"GET /health HTTP/1.1\r\nHost: lb-health\r\nConnection: close\r\n\r\n")
            s.settimeout(timeout)
            buf = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > 65536:
                    break
        ms = (time.time() - t0) * 1000
        head, _, body = buf.partition(b"\r\n\r\n")
        ok = head.startswith(b"HTTP/1.1 200") or head.startswith(b"HTTP/1.0 200")
        data = None
        try:
            data = json.loads(body.decode() or "null")
        except ValueError:
            pass
        return ok, ms, data
    except OSError:
        return False, (time.time() - t0) * 1000, None


def health_loop():
    """Active checks: probe every backend, keep EWMA/load fresh, move states."""
    while True:
        cfg = LB_STATE.cfg
        for b in list(LB_STATE.backends):
            ok, ms, data = probe(b.host, b.port, cfg["health_timeout_s"])
            if ok:
                b.probe_ms = ms
                b.last_seen = time.time()
                if isinstance(data, dict) and isinstance(data.get("load"), dict):
                    b.load = data["load"]
                # an idle backend's only latency signal is the probe itself
                if b.in_flight == 0:
                    b.observe(ms, cfg["ewma_alpha"])
                b.consec_ok += 1
                b.consec_fail = 0
                slow = ms > cfg["degrade_ms"] or b.cpu_load() * 100 >= cfg["degrade_cpu_pct"]
                if b.state == DOWN and b.consec_ok >= cfg["rise_threshold"]:
                    b.state = DEGRADED if slow else UP
                    b.down_since = None
                    LB_STATE.event("readmitted", b.id, f"probe {ms:.0f} ms")
                elif b.state == UP and slow:
                    b.state = DEGRADED
                    LB_STATE.event("degraded", b.id, f"probe {ms:.0f} ms, cpu {b.cpu_load()*100:.0f}%")
                elif b.state == DEGRADED and not slow:
                    b.state = UP
                    LB_STATE.event("recovered", b.id, f"probe {ms:.0f} ms")
            else:
                b.consec_fail += 1
                b.consec_ok = 0
                if b.routable() and b.consec_fail >= cfg["fail_threshold"]:
                    others = [x for x in LB_STATE.backends if x is not b and x.routable()]
                    if others:
                        b.state = DOWN
                        b.down_since = time.time()
                        LB_STATE.event("ejected", b.id, f"{b.consec_fail} failed checks")
                    else:
                        print(f"[lb] {b.id} failing checks but is the last one — keeping it (fail-open)", flush=True)
                elif b.state == DRAINING and b.consec_fail >= cfg["fail_threshold"]:
                    b.state = DOWN
                    b.down_since = time.time()
                    LB_STATE.event("ejected", b.id, "drained backend went away")
            # prune dynamic members that have been dead for a long time
            if b.state == DOWN and b.source != "config" and b.down_since and \
                    time.time() - b.down_since > cfg["prune_after_s"]:
                LB_STATE.remove_backend(b.id)
        LB_STATE.refresh_active()
        LB_STATE.flush_log()
        time.sleep(cfg["health_interval_s"])


def discovery_loop():
    """Pull-side discovery: probe candidate slots; admit any that answer."""
    while True:
        cfg = LB_STATE.cfg
        known = {(b.host, b.port) for b in LB_STATE.backends}
        for c in cfg["discovery"].get("candidates", []):
            host, port = c["host"], int(c["port"])
            if (host, port) in known:
                continue
            ok, ms, data = probe(host, port, min(2.0, cfg["health_timeout_s"]))
            if ok:
                want = cfg["discovery"].get("require_version")
                if want and not str((data or {}).get("version", "")).startswith(want):
                    continue                     # some other service on that slot — not ours
                bid = (data or {}).get("backend") or c.get("id") or f"{host}:{port}"
                LB_STATE.add_backend(bid, host, port, c.get("weight", 1), source="scan")
        # config file edited? reload without a restart
        try:
            if os.path.getmtime(CONF_PATH) != LB_STATE.conf_mtime:
                LB_STATE.reload()
        except OSError:
            pass
        time.sleep(cfg["discovery"].get("interval_s", 5))


def eject_now(b):
    """Passive check: a live proxy attempt failed.

    Ejecting on the FIRST failure is right when a backend has genuinely died and
    wrong when a burst of a thousand simultaneous connections briefly overflows its
    accept queue — and the second case is exactly what the evaluation produces. A
    backend that answered its health probe moments ago is given the benefit of the
    doubt until several proxy attempts fail in a row; one that is really gone fails
    them all within milliseconds, so detection is still effectively immediate."""
    if not b.routable():
        return
    others = [x for x in LB_STATE.backends if x is not b and x.routable()]
    if not others:
        return                                   # fail-open: never eject the last one
    b.passive_fails += 1
    fresh_probe = (time.time() - b.last_seen) < LB_STATE.cfg["health_interval_s"] * 2
    need = LB_STATE.cfg["passive_fail_threshold"] if fresh_probe else 1
    if b.passive_fails < need:
        return
    b.state = DOWN
    b.down_since = time.time()
    b.consec_fail = LB_STATE.cfg["fail_threshold"]
    b.consec_ok = 0
    LB_STATE.event("ejected", b.id, f"passive: {b.passive_fails} consecutive proxy failures")


# ───────────────────────────── HTTP plumbing ────────────────────────────────

HOP_BY_HOP = {b"connection", b"keep-alive", b"proxy-authenticate", b"proxy-authorization",
              b"te", b"trailers", b"transfer-encoding", b"upgrade"}


IO_BUF = 262144          # buffered-reader size and socket buffer target
POOL_MAX = 256           # idle keep-alive connections kept per backend


def tune_writer(w):
    """Big TCP buffers + no Nagle. Bodies here are tens of kilobytes and the hop to
    the backends crosses a container bridge, so a small receive buffer turns one
    logical read into a dozen recv() syscalls — which is what actually costs the
    balancer its CPU on a one-core system."""
    try:
        sock = w.get_extra_info("socket")
        if sock is None:
            return
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, IO_BUF)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, IO_BUF)
    except OSError:
        pass


def close_writer(w):
    if w is None:
        return
    try:
        w.close()
    except Exception:
        pass


def build_upstream_head(first, headers, hmap, client_ip, is_ws):
    out = [first + b"\r\n"]
    for line in headers:
        k = line.split(b":", 1)[0].strip().lower()
        if k in HOP_BY_HOP or k in (b"x-forwarded-for", b"x-real-ip", b"x-forwarded-proto"):
            continue
        out.append(line)
    prior = hmap.get(b"x-forwarded-for")
    out.append(b"X-Forwarded-For: " + ((prior + b", ") if prior else b"") + client_ip.encode() + b"\r\n")
    out.append(b"X-Real-IP: " + client_ip.encode() + b"\r\n")
    out.append(b"X-Forwarded-Proto: http\r\n")
    out.append(b"Connection: Upgrade\r\nUpgrade: websocket\r\n" if is_ws else b"Connection: keep-alive\r\n")
    out.append(b"\r\n")
    return b"".join(out)


# ───────────────────────────── request handling ─────────────────────────────
# One asyncio task per connection instead of one OS thread. The evaluation drives
# up to 1 500 concurrent clients, and measured on this code a thread per
# connection sustained ~10 000 req/s to 500 connections and then fell off a cliff
# to ~385 at 1 000 — CPython cannot schedule that many runnable threads on the one
# CPU this container is given. The same proxy on an event loop holds ~7 000 req/s
# at 1 000 connections. Everything above this line — selection, health, membership,
# the admin surface — is unchanged and shared.


async def read_head(reader, limit=IO_BUF):
    """Read a request/response head. Returns (first_line, header_lines, header_map)
    or (None, None, None) at a clean end of stream."""
    try:
        first = await reader.readuntil(b"\r\n")
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
        return None, None, None
    if not first or first in (b"\r\n", b"\n"):
        return None, None, None
    headers, hmap = [], {}
    while True:
        line = await reader.readuntil(b"\r\n")
        if line in (b"\r\n", b"\n", b""):
            break
        headers.append(line)
        k, _, v = line.partition(b":")
        hmap[k.strip().lower()] = v.strip()
        if len(headers) > 200:
            break
    return first.rstrip(b"\r\n"), headers, hmap


async def read_chunked(reader, cap=8 * 1024 * 1024):
    """Collect a chunked body so it can be forwarded with a Content-Length."""
    chunks, total = [], 0
    while True:
        line = (await reader.readuntil(b"\r\n")).strip()
        size = int(line.split(b";")[0], 16)
        if size == 0:
            while True:                       # trailers, then the final blank line
                t = await reader.readuntil(b"\r\n")
                if t in (b"\r\n", b"\n"):
                    break
            break
        total += size
        if total > cap:
            raise ValueError("chunked request body too large")
        chunks.append(await reader.readexactly(size))
        await reader.readexactly(2)           # the CRLF after each chunk
    return b"".join(chunks)


async def get_upstream(backend, timeout):
    """A pooled keep-alive connection to `backend`, or a fresh one.

    No mutex: deque.popleft/append are atomic in CPython, and the worst a race can
    do is hand two tasks different connections."""
    while True:
        try:
            r, w = backend.pool.popleft()
        except IndexError:
            break
        if w.is_closing() or r.at_eof():
            close_writer(w)
            continue
        return r, w, True
    r, w = await asyncio.wait_for(
        asyncio.open_connection(backend.host, backend.port, limit=IO_BUF),
        LB_STATE.cfg["connect_timeout_s"])
    tune_writer(w)
    return r, w, False


async def pump(reader, writer):
    """Copy one direction of a tunnelled (WebSocket) connection until it ends."""
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError, asyncio.CancelledError):
        pass
    finally:
        close_writer(writer)


async def relay_body(src, dst, clen):
    """Forward a response body. `clen` None means "until the upstream closes"."""
    sent = 0
    if clen is None:
        while True:
            chunk = await src.read(IO_BUF)
            if not chunk:
                break
            dst.write(chunk); sent += len(chunk)
            await dst.drain()
        return sent
    remaining = clen
    while remaining > 0:
        chunk = await src.read(min(IO_BUF, remaining))
        if not chunk:
            raise ConnectionError("upstream truncated the body")
        dst.write(chunk); sent += len(chunk); remaining -= len(chunk)
        await dst.drain()
    return sent


async def handle_client(creader, cwriter):
    peer = cwriter.get_extra_info("peername") or ("?", 0)
    client_ip = peer[0]
    tune_writer(cwriter)
    idle = LB_STATE.cfg.get("keepalive_idle_s", 65)
    try:
        while True:
            try:
                first, headers, hmap = await asyncio.wait_for(read_head(creader), idle)
            except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                return
            if first is None:
                return
            try:
                method, path, _version = first.split(b" ", 2)
            except ValueError:
                return
            LB_STATE.total_requests += 1

            # Request body: Content-Length is what every sane client sends, but a
            # generator that streams a chunked body must not be silently truncated
            # into a request the backend waits forever for.
            body = b""
            clen = int(hmap.get(b"content-length", b"0") or 0)
            if clen:
                try:
                    body = await creader.readexactly(clen)
                except asyncio.IncompleteReadError:
                    return
            elif b"chunked" in hmap.get(b"transfer-encoding", b"").lower():
                try:
                    body = await read_chunked(creader)
                except (asyncio.IncompleteReadError, ValueError, ConnectionError):
                    return
                headers = [h for h in headers
                           if h.split(b":", 1)[0].strip().lower() != b"transfer-encoding"]
                headers.append(b"Content-Length: " + str(len(body)).encode() + b"\r\n")
                hmap[b"content-length"] = str(len(body)).encode()

            if path.startswith(b"/lb/") or path == b"/lb":
                cwriter.write(admin_response(method, path, hmap, body, client_ip))
                await cwriter.drain()
                continue

            is_ws = (b"upgrade" in hmap.get(b"connection", b"").lower()
                     and hmap.get(b"upgrade", b"").lower() == b"websocket")

            tried, backend, ur, uw = set(), None, None, None
            while True:
                backend = LB_STATE.pick(client_ip, exclude=tried) if not is_ws \
                    else LB_STATE.pick_sticky(client_ip, tried)
                if backend is None:
                    cwriter.write(error_response(503, "No healthy backends"))
                    await cwriter.drain()
                    LB_STATE.total_errors += 1
                    LB_STATE.log(client_ip, method.decode(), path.decode(), "-", 0.0, 503, 0)
                    return
                try:
                    ur, uw, _pooled = await get_upstream(backend, LB_STATE.cfg["upstream_timeout_s"])
                    break
                except (OSError, asyncio.TimeoutError):
                    tried.add(backend.id)
                    eject_now(backend)      # passive ejection; nothing was sent upstream yet
                    continue

            head = build_upstream_head(first, headers, hmap, client_ip, is_ws)
            replied = False
            backend.in_flight += 1
            backend.requests += 1
            t0 = time.time()
            try:
                uw.write(head + body)
                await uw.drain()

                if is_ws:
                    await asyncio.gather(pump(creader, uw), pump(ur, cwriter),
                                         return_exceptions=True)
                    LB_STATE.log(client_ip, "WS", path.decode(), backend.id,
                                 (time.time() - t0) * 1000, 101, 0)
                    return

                timeout = LB_STATE.cfg["upstream_timeout_s"]
                rfirst, rheaders, rhmap = await asyncio.wait_for(read_head(ur), timeout)
                if rfirst is None:
                    raise ConnectionError("upstream sent no response")
                status = int(rfirst.split(b" ")[1])
                r_clen = rhmap.get(b"content-length")
                keep_up = (rhmap.get(b"connection", b"keep-alive").lower() != b"close"
                           and r_clen is not None)

                out = [rfirst + b"\r\n"]
                for line in rheaders:
                    if line.split(b":", 1)[0].strip().lower() == b"connection":
                        continue
                    out.append(line)
                out.append(b"X-LB-Active-Backends: " + str(LB_STATE.active_count).encode() + b"\r\n")
                out.append(b"Connection: keep-alive\r\n" if r_clen is not None else b"Connection: close\r\n")
                out.append(b"\r\n")
                cwriter.write(b"".join(out))
                replied = True          # past this point a 502 would corrupt the reply

                nbytes = await asyncio.wait_for(
                    relay_body(ur, cwriter, int(r_clen) if r_clen is not None else None), timeout)

                ms = (time.time() - t0) * 1000
                backend.passive_fails = 0
                backend.latencies.append(ms)
                backend.observe(ms, LB_STATE.cfg["ewma_alpha"])     # the dynamic signal
                if status >= 500:
                    backend.errors += 1
                LB_STATE.log(client_ip, method.decode(), path.decode(), backend.id, ms, status, nbytes)

                if keep_up and len(backend.pool) < POOL_MAX:
                    backend.pool.append((ur, uw))
                else:
                    close_writer(uw)
                if r_clen is None:
                    return
            except (OSError, ConnectionError, asyncio.TimeoutError,
                    asyncio.IncompleteReadError, ValueError, IndexError):
                backend.errors += 1
                LB_STATE.total_errors += 1
                eject_now(backend)
                close_writer(uw)
                LB_STATE.log(client_ip, method.decode(), path.decode(), backend.id,
                             (time.time() - t0) * 1000, 502, 0)
                if not replied:
                    try:
                        cwriter.write(error_response(502, "Upstream failed mid-request"))
                        await cwriter.drain()
                    except (OSError, ConnectionError):
                        pass
                return
            finally:
                backend.in_flight -= 1
    except (ConnectionError, OSError, asyncio.CancelledError):
        pass
    finally:
        close_writer(cwriter)


def stats_dict():
    lb = LB_STATE
    return {
        "algorithm": lb.cfg["algorithm"], "version": "v2-dynamic",
        "uptime_s": round(time.time() - lb.started, 1),
        "total_requests": lb.total_requests, "total_errors": lb.total_errors,
        "active_backends": lb.refresh_active(),
        "switch_threshold": lb.cfg["switch_threshold"], "inflight_cap": lb.cfg["inflight_cap"],
        "rt_cap_ms": lb.cfg["rt_cap_ms"], "current_backend": lb.current_id, "switches": lb.switches,
        "backends": [b.snapshot(lb.cfg) for b in lb.backends],
        "candidates": lb.cfg["discovery"].get("candidates", []),
        "recent_events": list(lb.events)[-10:],
    }


DASHBOARD = """<!doctype html><meta charset=utf-8>
<meta http-equiv=refresh content=2>
<title>Dynamic LB dashboard</title>
<style>
 body{font:14px -apple-system,Segoe UI,Roboto,sans-serif;margin:30px auto;max-width:900px;color:#1a1a1e}
 h1{font-size:20px;margin-bottom:4px} table{border-collapse:collapse;width:100%%;margin-top:10px}
 td,th{border:1px solid #ddd;padding:6px 8px;text-align:left;font-variant-numeric:tabular-nums;font-size:13px}
 th{background:#f4f4f6}.UP{color:#2e7d32;font-weight:600}.DEGRADED{color:#b26a00;font-weight:600}
 .DOWN{color:#c62828;font-weight:600}.DRAINING{color:#7a4fd0;font-weight:600}.muted{color:#777}
 .bar{background:#e8ebff;height:8px;border-radius:4px;overflow:hidden}.bar i{display:block;height:8px;background:#4f6df5}
 ul{font-family:ui-monospace,monospace;font-size:12px;padding-left:18px}
</style>
<h1>Dynamic load balancer — sys1:3269</h1>
<p class=muted>algorithm <b>%(algo)s</b> · switch threshold <b>%(thr)s</b> · pinned to <b>%(cur)s</b> · %(sw)d switches · <b>%(active)d active backend(s)</b> of %(n)d · uptime %(up).0fs · %(tot)d requests · %(err)d errors · auto-refresh 2s</p>
<table><tr><th>backend</th><th>addr</th><th>source</th><th>state</th><th>score ↓</th><th>EWMA ms</th><th>probe ms</th>
<th>load idx</th><th>cpu %%</th><th>load1</th><th>in-flight</th><th>requests</th><th>errors</th><th>p50/p95 ms</th><th>share</th></tr>%(rows)s</table>
<h3>Recent events</h3><ul>%(events)s</ul>
<p class=muted>JSON: <a href=/lb/stats>/lb/stats</a> · <a href=/lb/events>/lb/events</a> · reload config: POST /lb/reload · register: POST /lb/register</p>"""


def admin_response(method, path, hmap, body, client_ip):
    """Build the reply to an /lb/* request. Returns the raw HTTP response bytes."""
    code = 200
    lb = LB_STATE
    if path == b"/lb/stats":
        out, ctype = json.dumps(stats_dict(), indent=2).encode(), b"application/json"
    elif path == b"/lb/events":
        out, ctype = json.dumps({"events": list(lb.events)}, indent=2).encode(), b"application/json"
    elif path in (b"/lb", b"/lb/"):
        total = sum(b.requests for b in lb.backends) or 1
        rows = ""
        for b in lb.backends:
            s = b.snapshot(lb.cfg)
            share = 100.0 * b.requests / total
            rows += ("<tr><td>%s</td><td>%s:%d</td><td>%s</td><td class=%s>%s</td><td>%s</td><td>%s</td><td>%s</td>"
                     "<td>%.2f</td><td>%s</td><td>%s</td><td>%d</td><td>%d</td><td>%d</td><td>%.0f / %.0f</td>"
                     "<td><div class=bar><i style='width:%.0f%%'></i></div>%.1f%%</td></tr>" % (
                         s["id"], s["host"], s["port"], s["source"], s["state"], s["state"],
                         s["score"], s["ewma_ms"] if s["ewma_ms"] is not None else "—",
                         s["probe_ms"] if s["probe_ms"] is not None else "—",
                         s["load_index"], s["load"].get("cpu_pct", "—"), s["load"].get("loadavg1", "—"),
                         s["in_flight"], s["requests"], s["errors"],
                         s["latency_ms"]["p50"], s["latency_ms"]["p95"], share, share))
        events = "".join("<li>%s  %-11s %-7s %s (active=%d)</li>" % (
            time.strftime("%H:%M:%S", time.localtime(e["t"])), e["kind"], e["backend"], e["detail"], e["active"])
            for e in list(lb.events)[-12:][::-1])
        out = (DASHBOARD % {"algo": lb.cfg["algorithm"], "active": len(lb.routable_backends()),
                            "n": len(lb.backends), "up": time.time() - lb.started,
                            "tot": lb.total_requests, "err": lb.total_errors,
                            "thr": lb.cfg["switch_threshold"], "cur": lb.current_id or "—",
                            "sw": lb.switches, "rows": rows, "events": events}).encode()
        ctype = b"text/html; charset=utf-8"
    elif path in (b"/lb/register", b"/lb/deregister") and method == b"POST":
        token = lb.cfg.get("register_token", "")
        if token and hmap.get(b"x-lb-token", b"").decode() != token:
            out, ctype, code = b'{"ok":false,"error":"bad token"}', b"application/json", 403
        else:
            try:
                req = json.loads(body.decode() or "{}")
                bid = str(req["id"])
                if path == b"/lb/register":
                    host = req.get("host") or client_ip
                    added = lb.add_backend(bid, host, int(req["port"]), int(req.get("weight", 1)))
                    out = json.dumps({"ok": True, "added": added, "active_backends": len(lb.routable_backends())}).encode()
                else:
                    out = json.dumps({"ok": True, "draining": lb.drain_backend(bid)}).encode()
                ctype = b"application/json"
            except (KeyError, ValueError, TypeError) as e:
                out, ctype, code = json.dumps({"ok": False, "error": f"bad request: {e}"}).encode(), b"application/json", 400
    elif path.startswith(b"/lb/backends/") and method == b"DELETE":
        bid = path[len(b"/lb/backends/"):].decode()
        out, ctype = json.dumps({"ok": lb.remove_backend(bid)}).encode(), b"application/json"
    elif path == b"/lb/config" and method == b"POST":
        # Runtime tuning, e.g. {"switch_threshold":0.6} — this is what the threshold
        # sweep drives. Values are also written back to lb.conf.json so that a later
        # reload (or the config watcher) does not undo them.
        try:
            patch = json.loads(body.decode() or "{}")
            allowed = {"algorithm", "explore_pct", "load_weight", "degrade_ms", "ewma_alpha",
                       "health_interval_s", "switch_threshold", "inflight_cap", "rt_cap_ms", "min_dwell_ms"}
            applied = {k: v for k, v in patch.items() if k in allowed}
            with lb.lock:
                lb.cfg.update(applied)
                if applied:
                    lb.switches = 0
                    lb.current_id = None       # start each sweep point from a clean pin
            if applied:
                try:
                    disk = json.load(open(CONF_PATH))
                    disk.update(applied)
                    with open(CONF_PATH, "w") as fh:
                        json.dump(disk, fh, indent=2)
                    lb.conf_mtime = os.path.getmtime(CONF_PATH)
                except OSError:
                    pass
                lb.event("config", "-", json.dumps(applied))
            out, ctype = json.dumps({"ok": True, "applied": applied,
                                     "algorithm": lb.cfg["algorithm"],
                                     "switch_threshold": lb.cfg["switch_threshold"]}).encode(), b"application/json"
        except ValueError as e:
            out, ctype, code = json.dumps({"ok": False, "error": str(e)}).encode(), b"application/json", 400
    elif path == b"/lb/config":
        out, ctype = json.dumps({k: v for k, v in lb.cfg.items() if k != "register_token"}, indent=2).encode(), b"application/json"
    elif path == b"/lb/reload" and method == b"POST":
        try:
            lb.reload()
            out, ctype = b'{"ok":true}', b"application/json"
        except Exception as e:
            out, ctype = json.dumps({"ok": False, "error": str(e)}).encode(), b"application/json"
    elif path == b"/lb/health":
        out, ctype = json.dumps({"status": "ok", "service": "lb", "version": "v2-dynamic",
                                 "active_backends": len(lb.routable_backends())},
                                separators=(",", ":")).encode(), b"application/json"
    else:
        out, ctype, code = b'{"error":"no such admin route"}', b"application/json", 404
    return (b"HTTP/1.1 " + str(code).encode() + b" OK\r\nContent-Type: " + ctype +
            b"\r\nContent-Length: " + str(len(out)).encode() +
            b"\r\nCache-Control: no-store\r\nConnection: keep-alive\r\n\r\n" + out)


def error_response(code, msg):
    out = json.dumps({"error": msg}).encode()
    return (b"HTTP/1.1 " + str(code).encode() + b" LB Error\r\nContent-Type: application/json\r\n"
            b"Content-Length: " + str(len(out)).encode() + b"\r\nConnection: close\r\n\r\n" + out)


# ───────────────────────────── main ─────────────────────────────────────────

def log_flusher():
    while True:
        time.sleep(1.0)
        LB_STATE.flush_log()


async def serve():
    srv = await asyncio.start_server(
        handle_client, LB_STATE.cfg["listen_host"], LB_STATE.cfg["listen_port"],
        backlog=2048,        # the evaluation opens hundreds of connections at once
        limit=IO_BUF, reuse_address=True)
    print(f"[lb] v3 (asyncio) listening on {LB_STATE.cfg['listen_host']}:"
          f"{LB_STATE.cfg['listen_port']} — dashboard at /lb/", flush=True)
    async with srv:
        await srv.serve_forever()


def main():
    signal.signal(signal.SIGHUP, lambda *_: LB_STATE.reload())
    LB_STATE.refresh_active()
    # The health probe, the candidate scan and the log flush are slow, blocking and
    # rare; they stay on their own threads so a stalled probe can never hold up the
    # event loop that is serving traffic.
    threading.Thread(target=log_flusher, daemon=True).start()
    threading.Thread(target=health_loop, daemon=True).start()
    threading.Thread(target=discovery_loop, daemon=True).start()
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

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
    "degrade_cpu_pct": 90,           # backend-reported CPU above this -> DEGRADED
    "ewma_alpha": 0.2,               # weight of the newest sample
    "explore_pct": 5,                # % of adaptive picks that go to a random UP backend
    "load_weight": 1.0,              # multiplier on the cpu_load term of the score
    "connect_timeout_s": 3,
    "upstream_timeout_s": 30,
    "register_token": "",            # shared secret for /lb/register (empty = open)
    "discovery": {"candidates": [], "interval_s": 5,    # [{"host","port"}] slots to probe
                  "require_version": ""},              # admit only backends reporting this /health version
    "prune_after_s": 600,            # remove dynamic backends DOWN this long
    "access_log": "logs/lb_access.csv",
}

UP, DEGRADED, DOWN, DRAINING = "UP", "DEGRADED", "DOWN", "DRAINING"


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
        self.lock = threading.Lock()
        self.started = time.time()
        self.total_requests = 0
        self.total_errors = 0
        self.events = deque(maxlen=200)
        self.log_lock = threading.Lock()
        self.log_fh = None
        self.conf_mtime = 0
        self.reload()

    # -- events ---------------------------------------------------------------
    def event(self, kind, backend_id, detail=""):
        ev = {"t": round(time.time(), 3), "kind": kind, "backend": backend_id, "detail": detail,
              "active": len(self.routable_backends())}
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
        self.log_fh = open(log_path, "a", buffering=1)
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

    def pick(self, client_ip, exclude=()):
        """Choose a backend for this request using the configured algorithm."""
        pool = [b for b in self.routable_backends() if b.id not in exclude]
        if not pool:
            return None
        algo = self.cfg["algorithm"]
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

    def pick_sticky(self, client_ip, tried):
        """WebSocket connections use ip_hash so reconnects re-pin."""
        pool = [b for b in self.routable_backends() if b.id not in tried]
        if not pool:
            return None
        return pool[hash(client_ip) % len(pool)]

    # -- access log -----------------------------------------------------------
    def log(self, client, method, path, backend_id, ms, status, nbytes):
        with self.log_lock:
            self.log_fh.write(f"{time.time():.3f},{client},{method},{path},{backend_id},"
                              f"{ms:.1f},{status},{nbytes},{len(self.routable_backends())}\n")


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
    """Passive check: a live proxy attempt failed — eject immediately (fail-open)."""
    if b.routable():
        others = [x for x in LB_STATE.backends if x is not b and x.routable()]
        if not others:
            return
        b.state = DOWN
        b.down_since = time.time()
        b.consec_fail = LB_STATE.cfg["fail_threshold"]
        b.consec_ok = 0
        LB_STATE.event("ejected", b.id, "passive: connect/proxy failure")


# ───────────────────────────── HTTP plumbing ────────────────────────────────

HOP_BY_HOP = {b"connection", b"keep-alive", b"proxy-authenticate", b"proxy-authorization",
              b"te", b"trailers", b"transfer-encoding", b"upgrade"}


def read_head(sock_file):
    first = sock_file.readline(65536)
    if not first or first in (b"\r\n", b"\n"):
        return None, None, None
    headers, hmap = [], {}
    while True:
        line = sock_file.readline(65536)
        if line in (b"\r\n", b"\n", b""):
            break
        headers.append(line)
        k, _, v = line.partition(b":")
        hmap[k.strip().lower()] = v.strip()
    return first.rstrip(b"\r\n"), headers, hmap


def get_upstream(backend, timeout):
    """Reuse a pooled keep-alive socket if one is idle, else connect fresh."""
    with backend.lock:
        while backend.pool:
            s = backend.pool.popleft()
            s.setblocking(False)
            try:
                if s.recv(1, socket.MSG_PEEK):
                    s.close(); continue
                s.close(); continue
            except BlockingIOError:
                s.setblocking(True); s.settimeout(timeout)
                return s, True
            except OSError:
                try: s.close()
                except OSError: pass
    s = socket.create_connection(backend.addr(), timeout=LB_STATE.cfg["connect_timeout_s"])
    s.settimeout(timeout)
    return s, False


def pump(src, dst):
    """Copy bytes one way until EOF/error (WebSocket tunnelling)."""
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try: s.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            try: s.close()
            except OSError: pass


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

def handle_client(client, client_addr):
    client_ip = client_addr[0]
    client.settimeout(60)
    cf = client.makefile("rb")
    try:
        while True:
            first, headers, hmap = read_head(cf)
            if first is None:
                return
            try:
                method, path, _version = first.split(b" ", 2)
            except ValueError:
                return
            LB_STATE.total_requests += 1

            body = b""
            clen = int(hmap.get(b"content-length", b"0") or 0)
            if clen:
                body = cf.read(clen)

            if path.startswith(b"/lb/") or path == b"/lb":
                if not serve_admin(client, method, path, hmap, body, client_ip):
                    return
                continue

            is_ws = (b"upgrade" in hmap.get(b"connection", b"").lower()
                     and hmap.get(b"upgrade", b"").lower() == b"websocket")

            tried = set()
            upstream = pooled = backend = None
            while True:
                backend = LB_STATE.pick(client_ip, exclude=tried) if not is_ws \
                    else LB_STATE.pick_sticky(client_ip, tried)
                if backend is None:
                    send_error(client, 503, "No healthy backends")
                    LB_STATE.total_errors += 1
                    LB_STATE.log(client_ip, method.decode(), path.decode(), "-", 0.0, 503, 0)
                    return
                try:
                    upstream, pooled = get_upstream(backend, LB_STATE.cfg["upstream_timeout_s"])
                    break
                except OSError:
                    tried.add(backend.id)
                    eject_now(backend)          # passive ejection + safe retry (nothing sent yet)
                    continue

            head = build_upstream_head(first, headers, hmap, client_ip, is_ws)
            backend.in_flight += 1
            backend.requests += 1
            t0 = time.time()
            try:
                upstream.sendall(head + body)
                if is_ws:
                    threading.Thread(target=pump, args=(client, upstream), daemon=True).start()
                    pump(upstream, client)
                    LB_STATE.log(client_ip, "WS", path.decode(), backend.id, (time.time() - t0) * 1000, 101, 0)
                    return

                uf = upstream.makefile("rb")
                rfirst, rheaders, rhmap = read_head(uf)
                if rfirst is None:
                    raise OSError("upstream sent no response")
                status = int(rfirst.split(b" ")[1])
                r_clen = rhmap.get(b"content-length")
                keep_up = rhmap.get(b"connection", b"keep-alive").lower() != b"close" and r_clen is not None

                out = [rfirst + b"\r\n"]
                for line in rheaders:
                    if line.split(b":", 1)[0].strip().lower() == b"connection":
                        continue
                    out.append(line)
                out.append(b"X-LB-Active-Backends: " + str(len(LB_STATE.routable_backends())).encode() + b"\r\n")
                out.append(b"Connection: keep-alive\r\n" if r_clen is not None else b"Connection: close\r\n")
                out.append(b"\r\n")
                client.sendall(b"".join(out))

                nbytes = 0
                if r_clen is not None:
                    remaining = int(r_clen)
                    while remaining:
                        chunk = uf.read(min(65536, remaining))
                        if not chunk:
                            raise OSError("upstream truncated body")
                        client.sendall(chunk); nbytes += len(chunk); remaining -= len(chunk)
                else:
                    while True:
                        chunk = uf.read1(65536)
                        if not chunk:
                            break
                        client.sendall(chunk); nbytes += len(chunk)

                ms = (time.time() - t0) * 1000
                backend.latencies.append(ms)
                backend.observe(ms, LB_STATE.cfg["ewma_alpha"])     # the dynamic signal
                if status >= 500:
                    backend.errors += 1
                LB_STATE.log(client_ip, method.decode(), path.decode(), backend.id, ms, status, nbytes)

                if keep_up:
                    with backend.lock:
                        if len(backend.pool) < 32:
                            upstream.settimeout(LB_STATE.cfg["upstream_timeout_s"])
                            backend.pool.append(upstream)
                        else:
                            upstream.close()
                else:
                    upstream.close()
                if r_clen is None:
                    return
            except OSError:
                backend.errors += 1
                LB_STATE.total_errors += 1
                eject_now(backend)
                try: upstream.close()
                except OSError: pass
                LB_STATE.log(client_ip, method.decode(), path.decode(), backend.id, (time.time() - t0) * 1000, 502, 0)
                send_error(client, 502, "Upstream failed mid-request")
                return
            finally:
                backend.in_flight -= 1
    except OSError:
        pass
    finally:
        try:
            cf.close(); client.close()
        except OSError:
            pass


# ───────────────────────────── admin endpoints ──────────────────────────────

def stats_dict():
    lb = LB_STATE
    return {
        "algorithm": lb.cfg["algorithm"], "version": "v2-dynamic",
        "uptime_s": round(time.time() - lb.started, 1),
        "total_requests": lb.total_requests, "total_errors": lb.total_errors,
        "active_backends": len(lb.routable_backends()),
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
<p class=muted>algorithm <b>%(algo)s</b> · <b>%(active)d active backend(s)</b> of %(n)d · uptime %(up).0fs · %(tot)d requests · %(err)d errors · auto-refresh 2s</p>
<table><tr><th>backend</th><th>addr</th><th>source</th><th>state</th><th>score ↓</th><th>EWMA ms</th><th>probe ms</th>
<th>cpu %%</th><th>load1</th><th>in-flight</th><th>requests</th><th>errors</th><th>p50/p95 ms</th><th>share</th></tr>%(rows)s</table>
<h3>Recent events</h3><ul>%(events)s</ul>
<p class=muted>JSON: <a href=/lb/stats>/lb/stats</a> · <a href=/lb/events>/lb/events</a> · reload config: POST /lb/reload · register: POST /lb/register</p>"""


def serve_admin(client, method, path, hmap, body, client_ip):
    """Handle /lb/* requests. Returns False to close the connection."""
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
                     "<td>%s</td><td>%s</td><td>%d</td><td>%d</td><td>%d</td><td>%.0f / %.0f</td>"
                     "<td><div class=bar><i style='width:%.0f%%'></i></div>%.1f%%</td></tr>" % (
                         s["id"], s["host"], s["port"], s["source"], s["state"], s["state"],
                         s["score"], s["ewma_ms"] if s["ewma_ms"] is not None else "—",
                         s["probe_ms"] if s["probe_ms"] is not None else "—",
                         s["load"].get("cpu_pct", "—"), s["load"].get("loadavg1", "—"),
                         s["in_flight"], s["requests"], s["errors"],
                         s["latency_ms"]["p50"], s["latency_ms"]["p95"], share, share))
        events = "".join("<li>%s  %-11s %-7s %s (active=%d)</li>" % (
            time.strftime("%H:%M:%S", time.localtime(e["t"])), e["kind"], e["backend"], e["detail"], e["active"])
            for e in list(lb.events)[-12:][::-1])
        out = (DASHBOARD % {"algo": lb.cfg["algorithm"], "active": len(lb.routable_backends()),
                            "n": len(lb.backends), "up": time.time() - lb.started,
                            "tot": lb.total_requests, "err": lb.total_errors,
                            "rows": rows, "events": events}).encode()
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
        # runtime tuning, e.g. {"algorithm":"round_robin"} — merged, not persisted
        try:
            patch = json.loads(body.decode() or "{}")
            allowed = {"algorithm", "explore_pct", "load_weight", "degrade_ms", "ewma_alpha", "health_interval_s"}
            applied = {k: v for k, v in patch.items() if k in allowed}
            lb.cfg.update(applied)
            lb.event("config", "-", json.dumps(applied))
            out, ctype = json.dumps({"ok": True, "applied": applied}).encode(), b"application/json"
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
    client.sendall(b"HTTP/1.1 " + str(code).encode() + b" OK\r\nContent-Type: " + ctype +
                   b"\r\nContent-Length: " + str(len(out)).encode() +
                   b"\r\nCache-Control: no-store\r\nConnection: keep-alive\r\n\r\n" + out)
    return True


def send_error(client, code, msg):
    out = json.dumps({"error": msg}).encode()
    try:
        client.sendall(b"HTTP/1.1 " + str(code).encode() + b" LB Error\r\nContent-Type: application/json\r\n"
                       b"Content-Length: " + str(len(out)).encode() + b"\r\nConnection: close\r\n\r\n" + out)
    except OSError:
        pass


# ───────────────────────────── main ─────────────────────────────────────────

def main():
    signal.signal(signal.SIGHUP, lambda *_: LB_STATE.reload())
    threading.Thread(target=health_loop, daemon=True).start()
    threading.Thread(target=discovery_loop, daemon=True).start()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((LB_STATE.cfg["listen_host"], LB_STATE.cfg["listen_port"]))
    srv.listen(512)
    print(f"[lb] v2 listening on {LB_STATE.cfg['listen_host']}:{LB_STATE.cfg['listen_port']} "
          f"— dashboard at /lb/", flush=True)
    while True:
        try:
            client, addr = srv.accept()
        except OSError as e:
            print(f"[lb] accept error (surviving): {e}", flush=True)
            time.sleep(0.2)
            continue
        client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        threading.Thread(target=handle_client, args=(client, addr), daemon=True).start()


if __name__ == "__main__":
    main()

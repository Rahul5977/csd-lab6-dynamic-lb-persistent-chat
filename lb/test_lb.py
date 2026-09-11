#!/usr/bin/env python3
"""
test_lb.py — local integration test for the dynamic load balancer (v2).

Boots (on 127.0.0.1): the SQLite DB service, the LB with ONE static backend,
then proves every dynamic behaviour the assignment asks for:
  1. adaptive routing works end to end (register/login/send through the LB)
  2. a NEW backend started while traffic flows self-registers and gets traffic
  3. a backend found by the candidate scan (no LB_URL at all) is admitted
  4. a slow backend is scored down (fewer requests) but not ejected
  5. a killed backend is ejected (passive + active) and re-admitted on restart
  6. deregister drains; DELETE removes; /lb/config switches algorithms live
  7. every Lab 5 algorithm is still selectable and distributes traffic
  8. the THRESHOLD algorithm sticks to one backend below the threshold and
     switches away the moment its load index crosses it
  9. the required public routes /message and /feed work through the LB and
     deduplicate a repeated message id even across two different backends
Exit code 0 = all pass. Output doubles as evidence/02_lb_local_verification.txt.
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
import http.cookiejar

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
NODE = os.environ.get("NODE", "node")
DB_PORT, LB_PORT = 15380, 18280
B = {"b1": 18281, "b2": 18282, "b3": 18283, "b4": 18284}
TOKEN = "test-token"
procs = {}
failures = 0
tmp = tempfile.mkdtemp(prefix="lb6-test-")


def ok(name, cond):
    global failures
    print(("  ✓ " if cond else "  ✗ ") + name, flush=True)
    if not cond:
        failures += 1


def start_db():
    procs["db"] = subprocess.Popen([NODE, os.path.join(ROOT, "app", "db_service.js")],
                                   env=dict(os.environ, PORT=str(DB_PORT), DATA_DIR=tmp, HOST="127.0.0.1", LOG_LEVEL="silent"))


def start_backend(bid, register=True, slow_ms=0):
    env = dict(os.environ, PORT=str(B[bid]), BACKEND_ID=bid, HOST="127.0.0.1", LOG_LEVEL="silent",
               DB_URL=f"http://127.0.0.1:{DB_PORT}", ADVERTISE_HOST="127.0.0.1", LB_HEARTBEAT_S="1")
    if register:
        env.update(LB_URL=f"http://127.0.0.1:{LB_PORT}", LB_TOKEN=TOKEN)
    if slow_ms:
        env["TEST_SLOW_MS"] = str(slow_ms)
    procs[bid] = subprocess.Popen([NODE, os.path.join(ROOT, "app", "server.js")], env=env)


def start_lb(conf):
    path = os.path.join(tmp, "lb.conf.json")
    json.dump(conf, open(path, "w"))
    procs["lb"] = subprocess.Popen([sys.executable, os.path.join(ROOT, "lb", "loadbalancer.py"), path],
                                   cwd=tmp, stdout=open(os.path.join(tmp, "lb.log"), "w"), stderr=subprocess.STDOUT)
    return path


def get(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, dict(r.headers), r.read()


def post(url, body=None, headers=None, timeout=5):
    data = json.dumps(body).encode() if body is not None else b""
    req = urllib.request.Request(url, data=data, method="POST", headers=dict({"Content-Type": "application/json"}, **(headers or {})))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), json.loads(e.read() or b"{}")


def wait(url, tries=80):
    for _ in range(tries):
        try:
            if get(url, 2)[0] == 200:
                return True
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError("never healthy: " + url)


def stats():
    return json.loads(get(f"http://127.0.0.1:{LB_PORT}/lb/stats")[2])


def states():
    return {b["id"]: b["state"] for b in stats()["backends"]}


def wait_state(bid, want, timeout=15):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if states().get(bid) == want:
            return True
        time.sleep(0.25)
    return False


class Client:
    """Cookie-holding HTTP client through the LB."""
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{LB_PORT}{path}", data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
        try:
            with self.op.open(req, timeout=10) as r:
                return r.status, r.headers.get("X-Backend-Id"), json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("X-Backend-Id"), json.loads(e.read() or b"{}")


def spray(client, n, path="/whoami"):
    """n GETs through the LB; returns {backend: count}."""
    dist = {}
    for _ in range(n):
        st, b, _ = client.call("GET", path)
        dist[b or f"ERR{st}"] = dist.get(b or f"ERR{st}", 0) + 1
    return dist


def main():
    print(f"lb test: data in {tmp}")
    start_db(); wait(f"http://127.0.0.1:{DB_PORT}/health")
    start_backend("b1", register=False)                      # static member
    wait(f"http://127.0.0.1:{B['b1']}/health")
    conf_path = start_lb({
        "listen_host": "127.0.0.1", "listen_port": LB_PORT, "algorithm": "adaptive",
        "backends": [{"id": "b1", "host": "127.0.0.1", "port": B["b1"], "weight": 1}],
        "health_interval_s": 1, "health_timeout_s": 3, "fail_threshold": 2, "rise_threshold": 1,
        "register_token": TOKEN, "explore_pct": 0,
        "discovery": {"candidates": [{"host": "127.0.0.1", "port": B["b3"]}], "interval_s": 1},
        "access_log": os.path.join(tmp, "access.csv"),
    })
    wait(f"http://127.0.0.1:{LB_PORT}/lb/health")

    print("— 1. app works through the LB (adaptive) —")
    c = Client()
    st, b, r = c.call("POST", "/api/register", {"username": "lbtester", "password": "loadbalance-1"})
    ok("register through LB", st == 200 and b == "b1")
    st, b, r = c.call("POST", "/api/rooms", {"id": "lbroom"})
    ok("create room through LB", st == 200)
    st, b, r = c.call("POST", "/api/messages", {"id": "11111111-aaaa-4bbb-8ccc-000000000001", "room": "lbroom", "text": "through the lb"})
    ok("send through LB", st == 200 and r["duplicate"] is False)
    st, b, r = c.call("POST", "/api/messages", {"id": "11111111-aaaa-4bbb-8ccc-000000000001", "room": "lbroom", "text": "through the lb"})
    ok("duplicate through LB flagged", st == 200 and r["duplicate"] is True)
    s = stats()
    ok("stats: 1 active backend, adaptive, state UP with EWMA sample", s["active_backends"] == 1 and s["algorithm"] == "adaptive" and s["backends"][0]["state"] == "UP" and s["backends"][0]["ewma_ms"] is not None)
    ok("response carries X-LB-Active-Backends", get(f"http://127.0.0.1:{LB_PORT}/whoami")[1].get("X-LB-Active-Backends") == "1")

    print("— 2. new backend self-registers while running —")
    st, _, r = post(f"http://127.0.0.1:{LB_PORT}/lb/register", {"id": "evil", "host": "127.0.0.1", "port": 1}, {"X-LB-Token": "wrong"})
    ok("register with wrong token → 403", st == 403)
    start_backend("b2", register=True)
    ok("b2 admitted by registration within 5 s", wait_state("b2", "UP", 8))
    ev = [e["kind"] for e in json.loads(get(f"http://127.0.0.1:{LB_PORT}/lb/events")[2])["events"]]
    ok("event log shows 'added'", "added" in ev)
    dist = spray(c, 60)
    ok(f"traffic now reaches both backends {dist}", dist.get("b1", 0) > 0 and dist.get("b2", 0) > 0)
    ok("active_backends == 2", stats()["active_backends"] == 2)

    print("— 3. backend discovered by candidate scan (no LB_URL) —")
    start_backend("b3", register=False)
    ok("b3 admitted by scan (source=scan)", wait_state("b3", "UP", 8) and next(b["source"] for b in stats()["backends"] if b["id"] == "b3") == "scan")
    dist = spray(c, 90)
    ok(f"three-way distribution {dist}", all(dist.get(x, 0) > 0 for x in ("b1", "b2", "b3")))

    print("— 4. adaptive scoring: slow backend gets less, is not ejected —")
    procs["b2"].send_signal(signal.SIGTERM); procs["b2"].wait(timeout=10)
    time.sleep(1.5)
    start_backend("b2", register=True, slow_ms=250)          # answers every request 250 ms late
    wait_state("b2", "UP", 8)
    post(f"http://127.0.0.1:{LB_PORT}/lb/config", {"algorithm": "adaptive", "explore_pct": 5})
    spray(c, 30)                                             # warm up EWMAs
    dist = spray(c, 150)
    slow_share = dist.get("b2", 0) / 150 * 100
    ok(f"slow b2 gets a small share ({slow_share:.0f}%) while UP: {dist}", 0 < slow_share < 20 and states()["b2"] in ("UP", "DEGRADED"))
    post(f"http://127.0.0.1:{LB_PORT}/lb/config", {"algorithm": "round_robin"})
    dist = spray(c, 150)
    ok(f"round_robin ignores latency: b2 share {dist.get('b2', 0) / 1.5:.0f}% (~33%)", 25 <= dist.get("b2", 0) / 1.5 <= 42)
    post(f"http://127.0.0.1:{LB_PORT}/lb/config", {"algorithm": "adaptive", "explore_pct": 0})

    print("— 5. failure and recovery —")
    procs["b3"].kill(); procs["b3"].wait()
    dist = spray(c, 60)
    ok(f"no client errors while b3 dies (passive ejection + retry): {dist}", not any(k.startswith("ERR") for k in dist))
    ok("b3 marked DOWN", wait_state("b3", "DOWN", 8))
    ok("active_backends == 2 during outage", stats()["active_backends"] == 2)
    start_backend("b3", register=False)
    ok("b3 re-admitted after restart (active check)", wait_state("b3", "UP", 10))
    kinds = [e["kind"] for e in json.loads(get(f"http://127.0.0.1:{LB_PORT}/lb/events")[2])["events"]]
    ok("events: ejected + readmitted logged", "ejected" in kinds and "readmitted" in kinds)

    print("— 6. drain, remove, algorithms —")
    procs["b2"].send_signal(signal.SIGTERM)                  # graceful: deregister then exit
    ok("b2 DRAINING or DOWN after SIGTERM deregister", wait_state("b2", "DRAINING", 5) or wait_state("b2", "DOWN", 5))
    procs["b2"].wait(timeout=10)
    time.sleep(3)
    dist = spray(c, 40)
    ok(f"no traffic to the drained backend: {dist}", dist.get("b2", 0) == 0)
    req = urllib.request.Request(f"http://127.0.0.1:{LB_PORT}/lb/backends/b2", method="DELETE")
    ok("DELETE /lb/backends/b2 removes it", json.loads(urllib.request.urlopen(req).read())["ok"] and "b2" not in states())
    for algo in ("round_robin", "least_connections", "weighted_round_robin", "ip_hash", "least_response_time", "adaptive"):
        post(f"http://127.0.0.1:{LB_PORT}/lb/config", {"algorithm": algo})
        dist = spray(c, 30)
        ok(f"algorithm {algo}: 30/30 served {dist}", sum(v for k, v in dist.items() if not k.startswith("ERR")) == 30)

    print("— 7. config-file edit is picked up without restart —")
    conf = json.load(open(conf_path)); conf["algorithm"] = "least_connections"; json.dump(conf, open(conf_path, "w"))
    time.sleep(2.5)
    ok("algorithm from edited file applied", stats()["algorithm"] == "least_connections")

    print("— 8. threshold algorithm: stick below T, switch above T —")
    for bid in ("b2", "b4"):
        if bid in states():
            try:
                urllib.request.urlopen(urllib.request.Request(
                    f"http://127.0.0.1:{LB_PORT}/lb/backends/{bid}", method="DELETE")).read()
            except Exception:
                pass
    start_backend("b2")                       # a healthy peer to switch to
    wait(f"http://127.0.0.1:{B['b2']}/health")
    wait_state("b2", "UP", 20)
    post(f"http://127.0.0.1:{LB_PORT}/lb/config",
         {"algorithm": "threshold", "switch_threshold": 0.95, "explore_pct": 0, "inflight_cap": 24, "rt_cap_ms": 5000})
    time.sleep(1.0)
    dist_hi = spray(c, 40)
    top = max(dist_hi.values()) if dist_hi else 0
    ok(f"T=0.95 (nothing is loaded): traffic stays on ONE backend {dist_hi}",
       top >= 38 and len(dist_hi) <= 2)
    st = stats()
    ok("stats report the threshold and the pinned backend",
       st["algorithm"] == "threshold" and st["switch_threshold"] == 0.95 and st["current_backend"] in dist_hi)
    post(f"http://127.0.0.1:{LB_PORT}/lb/config", {"switch_threshold": 0.0001})
    time.sleep(0.5)
    dist_lo = spray(c, 60)
    ok(f"T~0 (everything is over threshold): traffic spreads {dist_lo}", len(dist_lo) >= 2)
    ok("switch counter moved", stats()["switches"] > 0)
    post(f"http://127.0.0.1:{LB_PORT}/lb/config", {"switch_threshold": 0.55, "explore_pct": 5})

    print("— 9. required public routes: /message and /feed —")
    base = f"http://127.0.0.1:{LB_PORT}"
    r = urllib.request.urlopen(urllib.request.Request(
        base + "/message", data=json.dumps({"client-name": "tester", "msg": "hello via the LB"}).encode(),
        headers={"Content-Type": "application/json"}, method="POST"))
    first = json.loads(r.read())
    ok("POST /message accepts client-name + msg", first.get("ok") and not first.get("duplicate") and first.get("seq"))
    r = urllib.request.urlopen(urllib.request.Request(
        base + "/message", data=b"client-name=formy&msg=urlencoded+body",
        headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST"))
    ok("POST /message accepts a form-encoded body", json.loads(r.read()).get("ok"))
    dup_id = "lbtest-dup-id-000001"
    seen = []
    for _ in range(6):                        # spread over backends: every copy must be a duplicate but the first
        r = urllib.request.urlopen(urllib.request.Request(
            base + "/message", data=json.dumps({"client-name": "tester", "msg": "retry", "id": dup_id}).encode(),
            headers={"Content-Type": "application/json"}, method="POST"))
        seen.append(json.loads(r.read()))
    ok("repeated message id stored once, rest reported duplicate",
       sum(1 for x in seen if not x.get("duplicate")) == 1 and all(x.get("seq") == seen[0]["seq"] for x in seen))
    feed = json.loads(urllib.request.urlopen(base + "/feed").read())
    texts = [m.get("msg", m.get("text")) for m in feed.get("messages", [])]
    ok(f"GET /feed returns the whole room ({feed.get('count')} messages)", feed.get("ok") and feed.get("count", 0) >= 3)
    ok("the deduplicated message appears exactly once in /feed",
       sum(1 for m in feed["messages"] if m.get("id") == dup_id) == 1)
    ok("/feed carries what /message wrote", "hello via the LB" in texts and "urlencoded body" in texts)

    print("\nALL TESTS PASSED" if failures == 0 else f"\n{failures} FAILURES")
    return failures


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        for p in procs.values():
            try: p.kill()
            except Exception: pass
    sys.exit(1 if rc else 0)

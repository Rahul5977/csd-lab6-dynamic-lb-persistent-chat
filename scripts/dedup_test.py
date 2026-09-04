#!/usr/bin/env python3
"""
dedup_test.py — duplicate-prevention and persistence experiment through the LB.

Runs from the Mac against the public LB URL and produces a small table
(printed + JSON) that goes straight into the report:

  T1 sequential retry     the same message id POSTed N times in a row
  T2 concurrent storm     N threads POST the same id at once (spread by the
                          LB across all backends — the cross-backend race)
  T3 reconnect retry      send, drop the TCP connection, reconnect, re-send
  T4 idempotency header   Idempotency-Key header instead of body id
  T5 different ids        control: N distinct ids MUST all be stored
  P1 persistence          row count + a probe message survive a DB restart
                          (only with --restart-db, needs ssh access to sys1)

Verification is done against the DATABASE via /api/messages/count and
/api/messages/<id>, not against what the backend said.
"""
import argparse
import http.client
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from urllib.parse import urlparse


class C:
    def __init__(self, url):
        self.p = urlparse(url); self.cookie = None; self.conn = None; self.connect()

    def connect(self):
        if self.conn:
            try: self.conn.close()
            except Exception: pass
        self.conn = http.client.HTTPConnection(self.p.hostname, self.p.port or 80, timeout=20)

    def call(self, method, path, body=None, headers=None):
        h = dict(headers or {})
        payload = json.dumps(body).encode() if body is not None else None
        if payload: h["Content-Type"] = "application/json"
        if self.cookie: h["Cookie"] = self.cookie
        for attempt in (1, 2):
            try:
                self.conn.request(method, path, payload, h)
                r = self.conn.getresponse(); data = r.read()
                sc = r.getheader("Set-Cookie")
                if sc: self.cookie = sc.split(";")[0]
                try: j = json.loads(data)
                except ValueError: j = {}
                return r.status, j, r.getheader("X-Backend-Id")
            except (http.client.HTTPException, OSError):
                if attempt == 2: raise
                self.connect()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://10.1.75.53:3269")
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--room", default="dedup-test")
    ap.add_argument("--restart-db", action="store_true", help="P1: restart the DB service on sys1 over ssh")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results", "dedup_test.json"))
    a = ap.parse_args()
    results = []
    def row(test, sent, stored, dups, backends, note=""):
        okk = "PASS" if stored == 1 or test in ("T5", "P1") and stored == sent else "FAIL"
        if test == "P1": okk = note.split()[0]
        results.append({"test": test, "sent": sent, "stored": stored, "duplicate_responses": dups, "backends": backends, "verdict": okk, "note": note})
        print(f"  {test:<3} sent={sent:<3} stored={stored:<3} duplicate_responses={dups:<3} backends={backends}  {okk}  {note}", flush=True)

    c = C(a.url)
    user = "dedup_" + uuid.uuid4().hex[:6]
    st, _, _ = c.call("POST", "/api/register", {"username": user, "password": "dedup-pass-12345"})
    assert st == 200, f"register failed {st}"
    c.call("POST", "/api/rooms", {"id": a.room})
    count = lambda: c.call("GET", f"/api/messages/count?room={a.room}")[1]["count"]
    print(f"dedup test against {a.url}, room {a.room}, N={a.n}")

    # T1 sequential retry
    mid = str(uuid.uuid4()); before = count(); dups = 0; seen = set()
    for i in range(a.n):
        st, j, b = c.call("POST", "/api/messages", {"id": mid, "room": a.room, "text": "T1 same id"})
        dups += 1 if j.get("duplicate") else 0; seen.add(b)
    row("T1", a.n, count() - before, dups, sorted(x for x in seen if x), "same id sequentially")

    # T2 concurrent storm across backends (each thread: its own connection → LB spreads them)
    mid = str(uuid.uuid4()); before = count(); out = []; lock = threading.Lock()
    def fire():
        cc = C(a.url); cc.cookie = c.cookie
        st, j, b = cc.call("POST", "/api/messages", {"id": mid, "room": a.room, "text": "T2 storm"})
        with lock: out.append((j.get("duplicate"), b, j.get("seq")))
    ts = [threading.Thread(target=fire) for _ in range(a.n)]
    for t in ts: t.start()
    for t in ts: t.join()
    row("T2", a.n, count() - before, sum(1 for d, _, _ in out if d), sorted({b for _, b, _ in out if b}),
        f"concurrent, {len({s for _, _, s in out})} distinct seq")

    # T3 reconnect retry: send, kill the connection, re-send from a new socket
    mid = str(uuid.uuid4()); before = count()
    st, j1, b1 = c.call("POST", "/api/messages", {"id": mid, "room": a.room, "text": "T3 reconnect"})
    c.connect()
    st, j2, b2 = c.call("POST", "/api/messages", {"id": mid, "room": a.room, "text": "T3 reconnect"})
    row("T3", 2, count() - before, int(bool(j2.get("duplicate"))), sorted({b1, b2}), f"seq {j1.get('seq')} == {j2.get('seq')}")

    # T4 Idempotency-Key header
    mid = str(uuid.uuid4()); before = count(); dups = 0
    for i in range(5):
        st, j, b = c.call("POST", "/api/messages", {"room": a.room, "text": "T4 header"}, {"Idempotency-Key": mid})
        dups += 1 if j.get("duplicate") else 0
    row("T4", 5, count() - before, dups, [b], "Idempotency-Key header")

    # T5 control: distinct ids all stored
    before = count(); dups = 0
    for i in range(a.n):
        st, j, b = c.call("POST", "/api/messages", {"id": str(uuid.uuid4()), "room": a.room, "text": f"T5 distinct {i}"})
        dups += 1 if j.get("duplicate") else 0
    row("T5", a.n, count() - before, dups, [b], "control: distinct ids")

    # lookup by id proves the row exists once and carries the client id
    st, m, _ = c.call("GET", f"/api/messages/{mid}")
    print(f"  lookup /api/messages/{mid[:8]}… → HTTP {st}, seq={m.get('seq')}, via={m.get('via')}")

    # P1 persistence across a DB-service restart
    if a.restart_db:
        probe = str(uuid.uuid4()); before = count()
        c.call("POST", "/api/messages", {"id": probe, "room": a.room, "text": "P1 survives restart"})
        print("  restarting DB service on sys1 …", flush=True)
        subprocess.run(["bash", os.path.join(os.path.dirname(os.path.abspath(__file__)), "deploy.sh"), "db"], check=True, capture_output=True)
        time.sleep(4)
        after = count()
        st, m, _ = c.call("GET", f"/api/messages/{probe}")
        st2, j, _ = c.call("POST", "/api/messages", {"id": probe, "room": a.room, "text": "P1 survives restart"})
        okp = after == before + 1 and st == 200 and j.get("duplicate")
        row("P1", before + 1, after, int(bool(j.get("duplicate"))), [],
            f"{'PASS' if okp else 'FAIL'}: count {before}→{after} across restart, probe readable={st == 200}, dup-of-old-id still rejected={bool(j.get('duplicate'))}")

    st, dbs, _ = c.call("GET", "/api/db/stats")
    print(f"  db: {dbs.get('messages')} messages, {dbs.get('duplicates_rejected_total')} duplicates rejected in total ({dbs.get('engine')})")
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    json.dump({"url": a.url, "n": a.n, "at": time.time(), "results": results, "db_stats": dbs}, open(a.out, "w"), indent=2)
    print(f"wrote {a.out}")
    sys.exit(0 if all(r["verdict"] == "PASS" for r in results) else 1)


if __name__ == "__main__":
    main()

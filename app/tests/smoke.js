#!/usr/bin/env node
'use strict';
// ============================================================================
// smoke.js — end-to-end test against real processes (Assignment 6, v3):
//   1. spawn the SQLite DB service + TWO backends + a fake LB registration sink
//   2. everything Lab 5 asserted: auth, shared sessions, plaintext + E2E rooms,
//      ciphertext-only storage, cross-instance WS delivery, health/metrics
//   3. NEW — duplicate prevention: same id sent twice, Idempotency-Key header,
//      20 concurrent sends of one id split across both backends, bad ids
//   4. NEW — persistence: restart the DB service, everything is still there
//   5. NEW — backends self-register with the LB (token checked) and deregister on SIGTERM
// Exit code 0 = all pass.
// ============================================================================
const { spawn } = require('child_process');
const http = require('http');
const path = require('path');
const fs = require('fs');
const os = require('os');
const { webcrypto, randomUUID } = require('crypto');
const subtle = webcrypto.subtle;
const { WebSocket } = require(path.join(__dirname, '..', 'vendor', 'ws'));

const APP = path.join(__dirname, '..');
const DATA = fs.mkdtempSync(path.join(os.tmpdir(), 'chat6-smoke-'));
const DPORT = 15370, B1 = 18191, B2 = 18193, LBPORT = 18190;
const TOKEN = 'smoke-token';
let procs = [];
let failures = 0;
function ok(name, cond) { console.log((cond ? '  ✓ ' : '  ✗ ') + name); if (!cond) failures++; }
function boot(script, env) {
  const p = spawn(process.execPath, [path.join(APP, script)], { env: Object.assign({}, process.env, env), stdio: ['ignore', 'pipe', 'pipe'] });
  p.stderr.on('data', d => process.stderr.write(`[${env.BACKEND_ID || 'db'}] ${d}`));
  p.tag = env.BACKEND_ID || 'db';
  procs.push(p);
  return p;
}
async function waitHealthy(url, tries = 60) {
  for (let i = 0; i < tries; i++) { try { const r = await fetch(url); if (r.ok) return true; } catch (e) {} await new Promise(r => setTimeout(r, 100)); }
  throw new Error('never became healthy: ' + url);
}
function jar() {
  let cookie = '';
  return {
    async api(base, method, p, body, headers) {
      const res = await fetch(base + p, {
        method, headers: Object.assign(body ? { 'Content-Type': 'application/json' } : {}, cookie ? { Cookie: cookie } : {}, headers || {}),
        body: body ? JSON.stringify(body) : undefined,
      });
      const sc = res.headers.get('set-cookie'); if (sc) cookie = sc.split(';')[0];
      const data = await res.json();
      return { status: res.status, data, backend: res.headers.get('x-backend-id'), dupHeader: res.headers.get('x-duplicate') };
    },
    get cookie() { return cookie; },
  };
}
// Assignment-4 room crypto, reimplemented independently
const enc = new TextEncoder(), dec = new TextDecoder();
const b64 = buf => Buffer.from(buf).toString('base64');
const unb64 = s => new Uint8Array(Buffer.from(s, 'base64'));
async function makeRoomKey(passphrase, roomId, epoch) {
  const salt = await subtle.digest('SHA-256', enc.encode('ChatFat-room-v1|' + roomId));
  const base = await subtle.importKey('raw', enc.encode(passphrase), 'PBKDF2', false, ['deriveBits']);
  const raw = await subtle.deriveBits({ name: 'PBKDF2', salt, iterations: 250000, hash: 'SHA-256' }, base, 256);
  const key = await subtle.importKey('raw', raw, { name: 'AES-GCM' }, false, ['encrypt', 'decrypt']);
  const hmacKey = await subtle.importKey('raw', raw, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  const verifier = b64(await subtle.sign('HMAC', hmacKey, enc.encode('ChatFat-room-verify|' + roomId + '|' + epoch)));
  return { key, raw, epoch, verifier };
}
const aadFor = (roomId, n, kid) => enc.encode(roomId + '|' + n + '|' + kid);
async function encryptRoom(entry, roomId, payload) {
  const iv = webcrypto.getRandomValues(new Uint8Array(12));
  const n = b64(webcrypto.getRandomValues(new Uint8Array(16)));
  const ct = await subtle.encrypt({ name: 'AES-GCM', iv, additionalData: aadFor(roomId, n, entry.epoch) }, entry.key, enc.encode(JSON.stringify(payload)));
  return { alg: 'A256GCM', kid: entry.epoch, n, iv: b64(iv), ct: b64(ct), aadv: 1 };
}
async function decryptRoom(entry, roomId, env) {
  const pt = await subtle.decrypt({ name: 'AES-GCM', iv: unb64(env.iv), additionalData: aadFor(roomId, env.n, env.kid) }, entry.key, unb64(env.ct));
  return JSON.parse(dec.decode(pt));
}

// Fake LB: records /lb/register and /lb/deregister calls (with token check).
const lbCalls = [];
const fakeLB = http.createServer((req, res) => {
  let body = ''; req.on('data', c => body += c);
  req.on('end', () => {
    lbCalls.push({ path: req.url, token: req.headers['x-lb-token'], body: JSON.parse(body || '{}') });
    res.writeHead(200, { 'Content-Type': 'application/json' }); res.end(JSON.stringify({ ok: true, added: true }));
  });
});

(async function main() {
  console.log('smoke: booting db + 2 backends + fake LB (data in ' + DATA + ')');
  await new Promise(r => fakeLB.listen(LBPORT, '127.0.0.1', r));
  boot('db_service.js', { PORT: String(DPORT), DATA_DIR: DATA, HOST: '127.0.0.1', LOG_LEVEL: 'silent' });
  await waitHealthy(`http://127.0.0.1:${DPORT}/health`);
  const benv = id => ({ BACKEND_ID: id, DB_URL: `http://127.0.0.1:${DPORT}`, HOST: '127.0.0.1', LOG_LEVEL: 'silent', LB_URL: `http://127.0.0.1:${LBPORT}`, LB_TOKEN: TOKEN, ADVERTISE_HOST: '127.0.0.1', LB_HEARTBEAT_S: '1' });
  boot('server.js', Object.assign({ PORT: String(B1) }, benv('test-b1')));
  boot('server.js', Object.assign({ PORT: String(B2) }, benv('test-b2')));
  await waitHealthy(`http://127.0.0.1:${B1}/health`);
  await waitHealthy(`http://127.0.0.1:${B2}/health`);
  const base1 = `http://127.0.0.1:${B1}`, base2 = `http://127.0.0.1:${B2}`, dbase = `http://127.0.0.1:${DPORT}`;

  console.log('— auth —');
  const alice = jar(), bob = jar();
  let r = await alice.api(base1, 'POST', '/api/register', { username: 'alice', password: 'correct-horse-9' });
  ok('register alice → 200 + cookie', r.status === 200 && alice.cookie.startsWith('session='));
  ok('X-Backend-Id header present', r.backend === 'test-b1');
  r = await alice.api(base1, 'POST', '/api/register', { username: 'alice', password: 'correct-horse-9' });
  ok('duplicate register → 409', r.status === 409);
  r = await bob.api(base2, 'POST', '/api/register', { username: 'bob', password: 'hunter2hunter2' });
  ok('register bob on backend 2', r.status === 200);
  r = await jar().api(base1, 'POST', '/api/login', { username: 'alice', password: 'wrong-password' });
  ok('wrong password → 401', r.status === 401);
  r = await jar().api(base2, 'POST', '/api/login', { username: 'alice', password: 'correct-horse-9' });
  ok('login on b2 with account made on b1 (shared users in SQLite)', r.status === 200);
  r = await bob.api(base1, 'GET', '/api/me');
  ok('bob session made on b2 is valid on b1 (shared sessions)', r.status === 200 && r.data.username === 'bob');

  console.log('— plaintext room —');
  r = await alice.api(base1, 'POST', '/api/rooms', { id: 'lobby' });
  ok('create room lobby', r.status === 200 && r.data.id === 'lobby');
  r = await alice.api(base1, 'POST', '/api/messages', { room: 'lobby', text: 'hello world' });
  ok('send plain message (server mints id when client sends none)', r.status === 200 && r.data.seq === 1 && r.data.duplicate === false && /^[0-9a-f-]{36}$/.test(r.data.id));
  r = await bob.api(base2, 'GET', '/api/messages?room=lobby&since=0');
  ok('bob fetches it from backend 2 (shared messages)', r.status === 200 && r.data.messages.length === 1 && r.data.messages[0].text === 'hello world');

  console.log('— duplicate prevention —');
  const id1 = randomUUID();
  r = await alice.api(base1, 'POST', '/api/messages', { id: id1, room: 'lobby', text: 'sent once' });
  const seq1 = r.data.seq;
  ok('client-supplied id accepted → stored', r.status === 200 && r.data.duplicate === false && r.data.id === id1 && r.data.client_id === true);
  r = await alice.api(base1, 'POST', '/api/messages', { id: id1, room: 'lobby', text: 'sent once' });
  ok('same id again on SAME backend → 200 duplicate:true, same seq (backend LRU)', r.status === 200 && r.data.duplicate === true && r.data.seq === seq1 && r.data.dedup === 'backend-lru' && r.dupHeader === '1');
  r = await alice.api(base2, 'POST', '/api/messages', { id: id1, room: 'lobby', text: 'sent once' });
  ok('same id on the OTHER backend → duplicate:true, same seq (DB or firehose LRU)', r.status === 200 && r.data.duplicate === true && r.data.seq === seq1);
  const id2 = randomUUID();
  r = await alice.api(base2, 'POST', '/api/messages', { room: 'lobby', text: 'via header' }, { 'Idempotency-Key': id2 });
  ok('Idempotency-Key header works as message id', r.status === 200 && r.data.id === id2 && r.data.duplicate === false);
  r = await alice.api(base1, 'POST', '/api/messages', { room: 'lobby', text: 'via header' }, { 'Idempotency-Key': id2 });
  ok('header retry → duplicate:true', r.data.duplicate === true);
  r = await alice.api(base1, 'POST', '/api/messages', { id: 'x', room: 'lobby', text: 'bad id' });
  ok('malformed id → 400', r.status === 400);
  // 20 concurrent sends of ONE id, alternating backends — the race the DB must win.
  const id3 = randomUUID();
  const before = (await fetch(`${dbase}/messages/count?room=lobby`).then(x => x.json())).count;
  const results = await Promise.all(Array.from({ length: 20 }, (_, i) =>
    alice.api(i % 2 ? base1 : base2, 'POST', '/api/messages', { id: id3, room: 'lobby', text: 'race' })));
  const after = (await fetch(`${dbase}/messages/count?room=lobby`).then(x => x.json())).count;
  const stored = results.filter(x => x.status === 200 && x.data.duplicate === false).length;
  const seqs = new Set(results.map(x => x.data.seq));
  ok(`20 concurrent sends of one id across 2 backends → exactly 1 stored (got ${stored}), 19 flagged duplicate`, stored === 1 && results.filter(x => x.data.duplicate === true).length === 19);
  ok('all 20 responses report the same seq', seqs.size === 1);
  ok(`database row count grew by exactly 1 (${before} → ${after})`, after === before + 1);
  r = await alice.api(base2, 'GET', '/api/messages/' + id3);
  ok('lookup by id returns the single stored row', r.status === 200 && r.data.id === id3 && r.data.text === 'race');
  const dstats = await fetch(`${dbase}/stats`).then(x => x.json());
  const m1 = await fetch(`${base1}/metrics`).then(x => x.json()), m2 = await fetch(`${base2}/metrics`).then(x => x.json());
  const layered = dstats.duplicates_rejected_total + m1.duplicates_suppressed_local + m2.duplicates_suppressed_local;
  ok(`every duplicate was caught by a layer: db audit ${dstats.duplicates_rejected_total} + backend LRU ${m1.duplicates_suppressed_local}+${m2.duplicates_suppressed_local} = 22 (3 earlier + 19 race)`, layered === 22);
  const recent = await fetch(`${dbase}/dedup/recent?limit=5`).then(x => x.json());
  ok('dedup audit log lists the id', recent.duplicates.some(d => d.id === id3));
  r = await bob.api(base2, 'GET', '/api/messages?room=lobby&since=0');
  const ids = r.data.messages.map(m => m.id);
  ok('history contains each id exactly once', new Set(ids).size === ids.length && ids.filter(x => x === id3).length === 1);
  const seqList = r.data.messages.map(m => m.seq);
  ok('per-room seq is gap-free and ascending', seqList.every((s, i) => s === i + 1));

  console.log('— locked room (Assignment-4 E2E scheme, unchanged) —');
  await alice.api(base1, 'POST', '/api/rooms', { id: 'vault' });
  const keyA = await makeRoomKey('a very strong passphrase', 'vault', 1);
  r = await alice.api(base1, 'POST', '/api/rooms/vault/lock', { epoch: 1, verifier: keyA.verifier });
  ok('lock room', r.status === 200 && r.data.locked === true && r.data.epoch === 1);
  const secret = 'the launch code is 0000';
  const env = await encryptRoom(keyA, 'vault', { text: secret });
  const encId = randomUUID();
  r = await alice.api(base1, 'POST', '/api/messages', { id: encId, room: 'vault', env });
  ok('send encrypted message', r.status === 200 && r.data.duplicate === false);
  r = await alice.api(base2, 'POST', '/api/messages', { id: encId, room: 'vault', env });
  ok('re-sent ciphertext with same id → duplicate', r.data.duplicate === true);
  r = await alice.api(base1, 'POST', '/api/messages', { room: 'vault', text: 'plaintext should be refused' });
  ok('plaintext into locked room → 400', r.status === 400);
  r = await alice.api(base1, 'POST', '/api/messages', { room: 'vault', env: Object.assign({}, env, { iv: b64(new Uint8Array(5)) }) });
  ok('malformed envelope (bad IV) → 400', r.status === 400);
  r = await bob.api(base2, 'GET', '/api/messages?room=vault&since=0');
  const encMsg = r.data.messages.find(m => m.kind === 'enc');
  ok('fetch returns ciphertext envelope', !!encMsg && encMsg.env.alg === 'A256GCM');
  const keyB = await makeRoomKey('a very strong passphrase', 'vault', 1);
  ok('bob derives same verifier from passphrase', keyB.verifier === keyA.verifier);
  ok('decrypt round-trip matches', (await decryptRoom(keyB, 'vault', encMsg.env)).text === secret);

  console.log('— ciphertext-only store proof (SQLite file) —');
  const disk = fs.readFileSync(path.join(DATA, 'chat.sqlite'), 'latin1') + (fs.existsSync(path.join(DATA, 'chat.sqlite-wal')) ? fs.readFileSync(path.join(DATA, 'chat.sqlite-wal'), 'latin1') : '');
  ok('plaintext secret NOT in the database file', !disk.includes('launch code'));
  ok('ciphertext IS in the database file', disk.includes(env.ct.slice(0, 24)));

  console.log('— cross-instance delivery (WS on b1, POST on b2) —');
  const received = new Promise((resolve, reject) => {
    const sock = new WebSocket(`ws://127.0.0.1:${B1}/ws`, { headers: { Cookie: alice.cookie } });
    const timer = setTimeout(() => reject(new Error('timeout waiting for WS delivery')), 5000);
    sock.on('message', d => {
      const m = JSON.parse(d);
      if (m.type === 'hello') sock.send(JSON.stringify({ type: 'join', room: 'lobby' }));
      if (m.type === 'joined') bob.api(base2, 'POST', '/api/messages', { room: 'lobby', text: 'crossing instances' });
      if (m.type === 'msg' && m.entry.text === 'crossing instances') { clearTimeout(timer); sock.close(); resolve(m); }
    });
    sock.on('error', reject);
  });
  try { const m = await received; ok('message POSTed to b2 arrived over WS on b1', true); ok('delivery tagged with serving backend', m.via === 'test-b1'); }
  catch (e) { ok('cross-instance WS delivery (' + e.message + ')', false); }

  console.log('— health / load reporting / registration —');
  r = await fetch(`${base1}/health`).then(x => x.json());
  ok('/health ok + db firehose up', r.status === 'ok' && r.db_firehose === true);
  ok('/health reports load (cpu_pct, loadavg1, in_flight, loop_lag_ms)', r.load && typeof r.load.cpu_pct === 'number' && typeof r.load.loadavg1 === 'number' && typeof r.load.in_flight === 'number');
  r = await fetch(`${base2}/metrics`).then(x => x.json());
  ok('/metrics has dedup counters', r.requests_total > 0 && typeof r.duplicates_rejected_by_db === 'number' && typeof r.duplicates_suppressed_local === 'number');
  await new Promise(r => setTimeout(r, 1500));
  const regs = lbCalls.filter(c => c.path === '/lb/register');
  ok('both backends registered with the LB (heartbeats seen)', new Set(regs.map(c => c.body.id)).size === 2 && regs.length >= 4);
  ok('registration carries token, host, port, weight', regs.every(c => c.token === TOKEN && c.body.host === '127.0.0.1' && c.body.port > 0 && c.body.weight === 1));

  console.log('— persistence across a DB service restart —');
  const countBefore = (await fetch(`${dbase}/stats`).then(x => x.json())).messages;
  const dbProc = procs.find(p => p.tag === 'db');
  await new Promise(res => { dbProc.on('exit', res); dbProc.kill('SIGTERM'); });
  boot('db_service.js', { PORT: String(DPORT), DATA_DIR: DATA, HOST: '127.0.0.1', LOG_LEVEL: 'silent' });
  await waitHealthy(`${dbase}/health`);
  await new Promise(r => setTimeout(r, 2500));                 // backends reconnect their firehose
  const countAfter = (await fetch(`${dbase}/stats`).then(x => x.json())).messages;
  ok(`message count identical after DB restart (${countBefore} = ${countAfter})`, countBefore === countAfter && countBefore > 0);
  r = await bob.api(base1, 'GET', '/api/messages?room=lobby&since=0');
  ok('history readable after restart', r.status === 200 && r.data.messages.some(m => m.id === id3));
  r = await jar().api(base2, 'POST', '/api/login', { username: 'alice', password: 'correct-horse-9' });
  ok('users persisted (login works after restart)', r.status === 200);
  r = await alice.api(base2, 'POST', '/api/messages', { id: id3, room: 'lobby', text: 'race' });
  ok('duplicate of a PRE-restart id is still rejected (dedup is durable, not in-memory)', r.data.duplicate === true);
  r = await fetch(`${base2}/health`).then(x => x.json());
  ok('backends reconnected to the restarted DB firehose', r.db_firehose === true);

  console.log('— required public routes: /message and /feed —');
  const post1 = async (b, body, ctype) => {
    const res = await fetch(`${b}/message`, { method: 'POST',
      headers: { 'Content-Type': ctype || 'application/json' },
      body: ctype === 'application/x-www-form-urlencoded' ? body : JSON.stringify(body) });
    return { status: res.status, data: await res.json() };
  };
  r = await post1(base1, { 'client-name': 'grader', msg: 'hello public api' });
  ok('POST /message {client-name, msg} accepted', r.status === 200 && r.data.ok && r.data.duplicate === false);
  const pubSeq = r.data.seq;
  r = await post1(base1, 'client-name=formy&msg=form+body', 'application/x-www-form-urlencoded');
  ok('POST /message accepts a form-encoded body', r.status === 200 && r.data.ok);
  r = await fetch(`${base1}/message?client-name=queryman&msg=from+the+query+string`).then(x => x.json());
  ok('GET /message with query parameters accepted', r.ok === true);
  r = await post1(base1, { 'client-name': 'nameless' });
  ok('POST /message without msg is rejected', r.status === 400);
  const pubId = 'smoke-public-dup-000001';
  const copies = [];
  for (const b of [base1, base2, base1, base2]) copies.push((await post1(b, { 'client-name': 'dupper', msg: 'retry me', id: pubId })).data);
  ok('a repeated id is stored once even across two backends',
     copies.filter(c => !c.duplicate).length === 1 && copies.every(c => c.seq === copies[0].seq));
  ok('duplicates name the layer that caught them', copies.slice(1).every(c => ['backend-lru', 'database'].includes(c.dedup)));
  let feed = await fetch(`${base2}/feed`).then(x => x.json());
  ok(`GET /feed returns the whole public room (${feed.count} messages)`, feed.ok && feed.count >= 4);
  ok('/feed is served by the backend that was asked', feed.backend === 'test-b2');
  ok('the deduplicated message appears exactly once in /feed', feed.messages.filter(m => m.id === pubId).length === 1);
  const bodyOf = (m) => m.msg ?? m.text;
  ok('/feed shows a message written through the OTHER backend',
     feed.messages.some(m => m.seq === pubSeq && bodyOf(m) === 'hello public api'));
  ok('/feed rows carry id, sender and sequence', feed.messages.every(m => m.id && m.from && Number.isInteger(m.seq)));
  const fresh = await post1(base1, { 'client-name': 'reader', msg: 'read after write' });
  await new Promise(res => setTimeout(res, 400));
  feed = await fetch(`${base2}/feed`).then(x => x.json());
  ok('a message written on one backend is visible in /feed on the other',
     feed.messages.some(m => m.id === fresh.data.id));

  console.log('— graceful deregister on SIGTERM —');
  const b2 = procs.find(p => p.tag === 'test-b2');
  await new Promise(res => { b2.on('exit', res); b2.kill('SIGTERM'); });
  ok('backend POSTed /lb/deregister before exiting', lbCalls.some(c => c.path === '/lb/deregister' && c.body.id === 'test-b2'));

  console.log(failures === 0 ? '\nALL TESTS PASSED' : `\n${failures} FAILURES`);
  cleanup();
  process.exit(failures === 0 ? 0 : 1);
})().catch(e => { console.error('smoke crashed:', e); cleanup(); process.exit(1); });

function cleanup() { for (const p of procs) try { p.kill('SIGTERM'); } catch (e) {} try { fakeLB.close(); } catch (e) {} }

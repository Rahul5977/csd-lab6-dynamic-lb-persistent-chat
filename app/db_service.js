#!/usr/bin/env node
'use strict';
// ============================================================================
// db_service.js — persistent, shared database for the load-balanced chat.
//
// Assignment 6 replaces Lab 5's append-only JSONL "state service" with a real
// database: SQLite (WAL mode) through Node's built-in `node:sqlite` (Node ≥ 22.13).
// Every backend (sys2/sys3/sys4) talks to this one service, so all of them see
// identical users, sessions, rooms and messages, and the data survives restarts
// of any backend, of this service, or of the whole cluster.
//
// Duplicate prevention (the assignment's "no duplicates" requirement):
//   · every message carries a globally unique id (UUID v4, minted by the client
//     when the message is composed — see D-004) and `messages.id` is the PRIMARY KEY;
//   · POST /messages runs INSERT ... ON CONFLICT(id) DO NOTHING inside one
//     transaction. If nothing was inserted the stored row is returned with
//     duplicate:true — the same message received twice (retry, reconnection,
//     two backends racing) can only ever exist once;
//   · UNIQUE(room, seq) keeps per-room ordering gap-free;
//   · every rejected duplicate is counted and written to dedup_log for audit.
//
// HTTP surface (same shape as Lab 5's state service so backends change little):
//   GET/PUT/DELETE /kv/{users|sessions|rooms}/<key>
//   GET  /rooms                       room directory
//   POST /messages {room, entry}      idempotent append → {ok, seq, id, duplicate}
//   GET  /messages?room=&since=&limit=
//   GET  /messages/<id>               lookup by id (dedup proof)
//   GET  /messages/count?room=        row count (persistence proof)
//   GET  /stats                       counts, duplicates rejected, db file size
//   GET  /health
//   WS   /subscribe                   firehose: every new message → all backends
//
// ENV: PORT (5270), DATA_DIR (./data), DB_FILE (chat.sqlite), MIGRATE_FROM (dir with
//      Lab 5 kv.json + messages.jsonl to import once), HOST, LOG_LEVEL
// ============================================================================

const http = require('http');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { WebSocketServer } = require(path.join(__dirname, 'vendor', 'ws'));

// node:sqlite prints an ExperimentalWarning on every boot; keep logs clean.
const _emitWarning = process.emitWarning;
process.emitWarning = (w, ...a) => (String(w).includes('SQLite') ? undefined : _emitWarning.call(process, w, ...a));
let DatabaseSync;
try { ({ DatabaseSync } = require('node:sqlite')); } catch (e) {
  console.error('[db] node:sqlite is not available — need Node >= 22.13 (have ' + process.version + ')');
  process.exit(2);
}

const PORT = parseInt(process.env.PORT || '5270', 10);
const FEED_TAIL_TTL_MS = parseInt(process.env.FEED_TAIL_TTL_MS || '100', 10);
const FEED_PAGE_MAX = parseInt(process.env.FEED_PAGE_MAX || '5000', 10);   // biggest single /feed body
const tailCache = new Map();   // room#limit -> serialised body (short TTL)
const HOST = process.env.HOST || '0.0.0.0';
const DATA_DIR = process.env.DATA_DIR || path.join(__dirname, 'data');
const DB_FILE = path.join(DATA_DIR, process.env.DB_FILE || 'chat.sqlite');
const MIGRATE_FROM = process.env.MIGRATE_FROM || '';
const started = Date.now();
fs.mkdirSync(DATA_DIR, { recursive: true });

function log(...a) { if (process.env.LOG_LEVEL !== 'silent') console.log('[db]', ...a); }

// ── Schema ──────────────────────────────────────────────────────────────────
const db = new DatabaseSync(DB_FILE);
db.exec(`
  PRAGMA journal_mode = WAL;
  PRAGMA synchronous = NORMAL;
  PRAGMA foreign_keys = ON;
  CREATE TABLE IF NOT EXISTS users    (username TEXT PRIMARY KEY, hash TEXT NOT NULL, created INTEGER NOT NULL);
  CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, username TEXT NOT NULL, created INTEGER NOT NULL, expires INTEGER NOT NULL);
  CREATE TABLE IF NOT EXISTS rooms    (id TEXT PRIMARY KEY, locked INTEGER NOT NULL DEFAULT 0, epoch INTEGER NOT NULL DEFAULT 0,
                                       verifier TEXT, created INTEGER NOT NULL, created_by TEXT);
  CREATE TABLE IF NOT EXISTS messages (
      id        TEXT PRIMARY KEY,            -- globally unique message id (UUID v4): THE duplicate guard
      room      TEXT NOT NULL,
      seq       INTEGER NOT NULL,            -- per-room sequence number, gap-free
      sender    TEXT NOT NULL,
      ts        INTEGER NOT NULL,            -- client/backend timestamp (ms)
      kind      TEXT NOT NULL,               -- plain | enc | system
      text      TEXT,                        -- plaintext rooms only
      env       TEXT,                        -- E2E envelope JSON (ciphertext) for locked rooms
      via       TEXT,                        -- backend that accepted it
      stored_at INTEGER NOT NULL,            -- server receive time (ms)
      extra     TEXT,                        -- optional JSON (e.g. roomMeta on system messages)
      UNIQUE(room, seq)
  );
  CREATE INDEX IF NOT EXISTS idx_messages_room_seq ON messages(room, seq);
  CREATE TABLE IF NOT EXISTS dedup_log (                -- audit trail of rejected duplicates
      n INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL, room TEXT, via TEXT, at INTEGER NOT NULL);
  CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires);
`);

const q = {
  getUser: db.prepare('SELECT username, hash, created FROM users WHERE username = ?'),
  putUser: db.prepare('INSERT INTO users(username, hash, created) VALUES(?, ?, ?) ON CONFLICT(username) DO UPDATE SET hash = excluded.hash'),
  delUser: db.prepare('DELETE FROM users WHERE username = ?'),
  getSess: db.prepare('SELECT token, username, created, expires FROM sessions WHERE token = ?'),
  putSess: db.prepare('INSERT INTO sessions(token, username, created, expires) VALUES(?, ?, ?, ?) ON CONFLICT(token) DO UPDATE SET expires = excluded.expires'),
  delSess: db.prepare('DELETE FROM sessions WHERE token = ?'),
  getRoom: db.prepare('SELECT id, locked, epoch, verifier, created, created_by FROM rooms WHERE id = ?'),
  putRoom: db.prepare(`INSERT INTO rooms(id, locked, epoch, verifier, created, created_by) VALUES(?, ?, ?, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET locked = excluded.locked, epoch = excluded.epoch, verifier = excluded.verifier`),
  delRoom: db.prepare('DELETE FROM rooms WHERE id = ?'),
  allRooms: db.prepare('SELECT id, locked, epoch, verifier, created, created_by FROM rooms ORDER BY id'),
  nextSeq: db.prepare('SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM messages WHERE room = ?'),
  insMsg: db.prepare(`INSERT INTO messages(id, room, seq, sender, ts, kind, text, env, via, stored_at, extra)
                      VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING`),
  getMsg: db.prepare('SELECT * FROM messages WHERE id = ?'),
  listMsgs: db.prepare('SELECT * FROM messages WHERE room = ? AND seq > ? ORDER BY seq DESC LIMIT ?'),
  feedMsgs: db.prepare('SELECT * FROM messages WHERE room = ? ORDER BY seq ASC'),
  feedTail: db.prepare('SELECT * FROM messages WHERE room = ? ORDER BY seq DESC LIMIT ?'),
  feedPage: db.prepare('SELECT * FROM messages WHERE room = ? AND seq > ? ORDER BY seq ASC LIMIT ?'),
  countRoom: db.prepare('SELECT COUNT(*) AS n, COALESCE(MAX(seq), 0) AS last FROM messages WHERE room = ?'),
  countAll: db.prepare('SELECT COUNT(*) AS n FROM messages'),
  countUsers: db.prepare('SELECT COUNT(*) AS n FROM users'),
  countRooms: db.prepare('SELECT COUNT(*) AS n FROM rooms'),
  countDups: db.prepare('SELECT COUNT(*) AS n FROM dedup_log'),
  logDup: db.prepare('INSERT INTO dedup_log(id, room, via, at) VALUES(?, ?, ?, ?)'),
  recentDups: db.prepare('SELECT id, room, via, at FROM dedup_log ORDER BY n DESC LIMIT ?'),
  purgeSess: db.prepare('DELETE FROM sessions WHERE expires < ?'),
};

const stats = { requests: 0, appended: 0, duplicates_rejected: 0, started };

function rowToEntry(r) {
  const e = { id: r.id, seq: r.seq, room: r.room, from: r.sender, ts: r.ts, kind: r.kind, via: r.via };
  if (r.kind === 'enc') e.env = JSON.parse(r.env);
  else e.text = r.text;
  if (r.extra) Object.assign(e, JSON.parse(r.extra));
  return e;
}
function roomToRec(r) {
  return r ? { id: r.id, locked: !!r.locked, epoch: r.epoch, verifier: r.verifier, created: r.created, by: r.created_by } : null;
}

const feedCache = new Map();   // room -> serialised /feed body, dropped on every append
// COUNT(*) per /feed request is O(rows in the room) and the load generator makes
// that room grow without bound, so the count is read once and then maintained.
const roomCount = new Map();   // room -> rows
function countOf(room) {
  let n = roomCount.get(room);
  if (n === undefined) { n = q.countRoom.get(room).n; roomCount.set(room, n); }
  return n;
}

// ── Idempotent append: the heart of the "no duplicates" guarantee ───────────
// Runs as ONE transaction. Node is single-threaded and DatabaseSync is
// synchronous, so two backends POSTing the same id at the same instant are
// serialised here; the second one finds the row and is reported as duplicate.
const appendTx = (room, entry) => {
  db.exec('BEGIN IMMEDIATE');
  try {
    const existing = q.getMsg.get(entry.id);
    if (existing) {
      q.logDup.run(entry.id, room, entry.via || null, Date.now());
      db.exec('COMMIT');
      return { duplicate: true, rec: rowToEntry(existing) };
    }
    const seq = q.nextSeq.get(room).seq;
    const extra = {};
    for (const k of Object.keys(entry)) {
      if (!['id', 'seq', 'room', 'from', 'ts', 'kind', 'text', 'env', 'via'].includes(k)) extra[k] = entry[k];
    }
    const res = q.insMsg.run(entry.id, room, seq, entry.from, entry.ts || Date.now(), entry.kind,
      entry.kind === 'enc' ? null : (entry.text ?? null),
      entry.kind === 'enc' ? JSON.stringify(entry.env) : null,
      entry.via || null, Date.now(), Object.keys(extra).length ? JSON.stringify(extra) : null);
    if (res.changes === 0) {                      // lost a race inside the same tx window
      const row = q.getMsg.get(entry.id);
      q.logDup.run(entry.id, room, entry.via || null, Date.now());
      db.exec('COMMIT');
      return { duplicate: true, rec: rowToEntry(row) };
    }
    db.exec('COMMIT');
    feedCache.delete(room);                       // /feed snapshot for this room is stale now
    if (roomCount.has(room)) roomCount.set(room, roomCount.get(room) + 1);
    return { duplicate: false, rec: rowToEntry(q.getMsg.get(entry.id)) };
  } catch (e) {
    db.exec('ROLLBACK');
    throw e;
  }
};

// ── One-time migration from the Lab 5 store (users, rooms, messages) ────────
function migrateFromLab5(dir) {
  const marker = path.join(DATA_DIR, '.migrated-from-lab5');
  if (fs.existsSync(marker)) return;
  const kvFile = path.join(dir, 'kv.json'), msgFile = path.join(dir, 'messages.jsonl');
  if (!fs.existsSync(kvFile) && !fs.existsSync(msgFile)) { log('migration: nothing found in', dir); return; }
  let users = 0, rooms = 0, msgs = 0, dups = 0;
  db.exec('BEGIN');
  try {
    if (fs.existsSync(kvFile)) {
      const kv = JSON.parse(fs.readFileSync(kvFile, 'utf8'));
      for (const u of Object.values(kv.users || {})) { q.putUser.run(u.username, u.hash, u.created || Date.now()); users++; }
      for (const r of Object.values(kv.rooms || {})) { q.putRoom.run(r.id, r.locked ? 1 : 0, r.epoch || 0, r.verifier || null, r.created || Date.now(), r.by || null); rooms++; }
    }
    if (fs.existsSync(msgFile)) {
      for (const line of fs.readFileSync(msgFile, 'utf8').split('\n')) {
        if (!line.trim()) continue;
        let m; try { m = JSON.parse(line); } catch (e) { continue; }
        const id = m.id || crypto.randomUUID();
        const extra = m.roomMeta ? JSON.stringify({ roomMeta: m.roomMeta }) : null;
        const r = q.insMsg.run(id, m.room, m.seq, m.from || 'unknown', m.ts || Date.now(), m.kind || 'plain',
          m.kind === 'enc' ? null : (m.text ?? null), m.kind === 'enc' ? JSON.stringify(m.env) : null,
          m.via || null, Date.now(), extra);
        if (r.changes) msgs++; else dups++;
      }
    }
    db.exec('COMMIT');
  } catch (e) { db.exec('ROLLBACK'); throw e; }
  fs.writeFileSync(marker, JSON.stringify({ at: new Date().toISOString(), from: dir, users, rooms, msgs, dups }));
  log(`migrated from Lab 5 store: ${users} users, ${rooms} rooms, ${msgs} messages (${dups} duplicate ids skipped)`);
}
if (MIGRATE_FROM) migrateFromLab5(MIGRATE_FROM);

// ── Pub/sub firehose to backends ────────────────────────────────────────────
const subscribers = new Set();
function broadcast(event) {
  const data = JSON.stringify(event);
  for (const ws of subscribers) if (ws.readyState === ws.OPEN) ws.send(data);
}

// ── HTTP helpers ────────────────────────────────────────────────────────────
function json(res, code, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(code, { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) });
  res.end(body);
}
function readBody(req) {
  return new Promise((resolve, reject) => {
    let size = 0; const chunks = [];
    req.on('data', c => { size += c.length; if (size > 65536) { reject(new Error('too large')); req.destroy(); } else chunks.push(c); });
    req.on('end', () => { try { resolve(chunks.length ? JSON.parse(Buffer.concat(chunks)) : {}); } catch (e) { reject(e); } });
    req.on('error', reject);
  });
}
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const ID_RE = /^[A-Za-z0-9._:-]{8,80}$/;   // UUIDs or any opaque client id

// ── Routes ──────────────────────────────────────────────────────────────────
const server = http.createServer(async (req, res) => {
  stats.requests++;
  const u = new URL(req.url, 'http://x');
  const parts = u.pathname.split('/').filter(Boolean);
  try {
    // /kv/<ns>/<key> — users | sessions | rooms (kept for backend compatibility)
    if (parts[0] === 'kv' && parts.length === 3) {
      const ns = parts[1], key = decodeURIComponent(parts[2]);
      if (ns === 'users') {
        if (req.method === 'GET') { const r = q.getUser.get(key); return r ? json(res, 200, r) : json(res, 404, { error: 'not found' }); }
        if (req.method === 'PUT') { const b = await readBody(req); q.putUser.run(key, b.hash, b.created || Date.now()); return json(res, 200, { ok: true }); }
        if (req.method === 'DELETE') { q.delUser.run(key); return json(res, 200, { ok: true }); }
      }
      if (ns === 'sessions') {
        if (req.method === 'GET') { const r = q.getSess.get(key); return r ? json(res, 200, r) : json(res, 404, { error: 'not found' }); }
        if (req.method === 'PUT') { const b = await readBody(req); q.putSess.run(key, b.username, b.created || Date.now(), b.expires); return json(res, 200, { ok: true }); }
        if (req.method === 'DELETE') { q.delSess.run(key); return json(res, 200, { ok: true }); }
      }
      if (ns === 'rooms') {
        if (req.method === 'GET') { const r = roomToRec(q.getRoom.get(key)); return r ? json(res, 200, r) : json(res, 404, { error: 'not found' }); }
        if (req.method === 'PUT') { const b = await readBody(req); q.putRoom.run(key, b.locked ? 1 : 0, b.epoch || 0, b.verifier || null, b.created || Date.now(), b.by || null); return json(res, 200, { ok: true }); }
        if (req.method === 'DELETE') { q.delRoom.run(key); return json(res, 200, { ok: true }); }
      }
      return json(res, 404, { error: 'no such namespace' });
    }
    if (u.pathname === '/rooms' && req.method === 'GET') {
      return json(res, 200, { rooms: q.allRooms.all().map(roomToRec) });
    }
    // POST /messages {room, entry{id, from, ts, kind, text|env, via}} — idempotent
    if (u.pathname === '/messages' && req.method === 'POST') {
      const { room, entry } = await readBody(req);
      if (!room || !entry || !entry.from || !entry.kind) return json(res, 400, { error: 'room and entry{from,kind} required' });
      if (!entry.id) entry.id = crypto.randomUUID();
      if (!ID_RE.test(entry.id)) return json(res, 400, { error: 'bad message id' });
      const out = appendTx(room, entry);
      if (out.duplicate) {
        stats.duplicates_rejected++;
        return json(res, 200, { ok: true, duplicate: true, seq: out.rec.seq, id: out.rec.id });
      }
      stats.appended++;
      broadcast({ type: 'msg', room, entry: out.rec });
      return json(res, 200, { ok: true, duplicate: false, seq: out.rec.seq, id: out.rec.id });
    }
    if (u.pathname === '/messages/count' && req.method === 'GET') {
      const room = u.searchParams.get('room');
      if (room) { const c = q.countRoom.get(room); return json(res, 200, { room, count: c.n, last_seq: c.last }); }
      return json(res, 200, { count: q.countAll.get().n });
    }
    if (parts[0] === 'messages' && parts.length === 2 && req.method === 'GET') {
      const row = q.getMsg.get(decodeURIComponent(parts[1]));
      return row ? json(res, 200, rowToEntry(row)) : json(res, 404, { error: 'no such message' });
    }
    if (u.pathname === '/messages' && req.method === 'GET') {
      const room = u.searchParams.get('room');
      const since = parseInt(u.searchParams.get('since') || '0', 10) || 0;
      const limit = Math.min(parseInt(u.searchParams.get('limit') || '100', 10) || 100, 500);
      const rows = q.listMsgs.all(room, since, limit).reverse();   // newest `limit` after `since`, ascending
      const list = rows.map(rowToEntry);
      return json(res, 200, { messages: list, last: list.length ? list[list.length - 1].seq : since });
    }
    // GET /feed?room=&limit=&since=  — the room's messages.
    //   no `since`  -> the newest `limit` messages, ascending (the chat view)
    //   `since=N`   -> the messages after sequence N, ascending (forward paging,
    //                  which is how the COMPLETE history is retrieved)
    // Backends call this for the public /feed route. The tail is cached and the
    // whole-room snapshot is dropped on every append, so a read-heavy load
    // generator costs one SQLite scan per new message, not one per request.
    if (u.pathname === '/feed' && req.method === 'GET') {
      const room = u.searchParams.get('room') || '';
      const limitRaw = u.searchParams.get('limit');
      const total = countOf(room);
      const sinceRaw = u.searchParams.get('since');
      if (sinceRaw !== null) {
        const since = Math.max(0, parseInt(sinceRaw, 10) || 0);
        const pageSize = Math.min(FEED_PAGE_MAX, Math.max(1, parseInt(limitRaw || '1000', 10) || 1000));
        const rows = q.feedPage.all(room, since, pageSize).map(rowToEntry);
        const last = rows.length ? rows[rows.length - 1].seq : since;
        return json(res, 200, { ok: true, room, total, count: rows.length, since,
                                next_since: rows.length === pageSize ? last : null, messages: rows });
      }
      const limit = limitRaw && limitRaw !== 'all' ? Math.max(1, parseInt(limitRaw, 10) || 0) : 0;
      if (limit) {
        const key = room + '#' + limit;
        const hit = tailCache.get(key);
        if (hit && Date.now() - hit.at < FEED_TAIL_TTL_MS) {
          res.writeHead(200, { 'Content-Type': 'application/json', 'Content-Length': hit.body.length });
          return res.end(hit.body);
        }
        const rows = q.feedTail.all(room, limit).reverse().map(rowToEntry);
        const body = Buffer.from(JSON.stringify({ ok: true, room, total, count: rows.length, messages: rows }));
        tailCache.set(key, { body, at: Date.now() });
        if (tailCache.size > 32) tailCache.delete(tailCache.keys().next().value);
        res.writeHead(200, { 'Content-Type': 'application/json', 'Content-Length': body.length });
        return res.end(body);
      }
      if (total > FEED_PAGE_MAX) {
        // Serialising half a million messages into one body would take hundreds of
        // megabytes; hand back the newest page and the cursor to walk the rest.
        const rows = q.feedTail.all(room, FEED_PAGE_MAX).reverse().map(rowToEntry);
        return json(res, 200, { ok: true, room, total, count: rows.length,
                                truncated: true, page_max: FEED_PAGE_MAX,
                                since: rows.length ? rows[0].seq - 1 : 0, next_since: null,
                                hint: 'page the full history with ?since=<seq>&limit=<n>',
                                messages: rows });
      }
      let hit = feedCache.get(room);
      if (!hit) {
        const rows = q.feedMsgs.all(room).map(rowToEntry);
        hit = Buffer.from(JSON.stringify({ ok: true, room, total: rows.length, count: rows.length, messages: rows }));
        feedCache.set(room, hit);
        if (feedCache.size > 32) feedCache.delete(feedCache.keys().next().value);
      }
      res.writeHead(200, { 'Content-Type': 'application/json', 'Content-Length': hit.length });
      return res.end(hit);
    }
    if (u.pathname === '/dedup/recent' && req.method === 'GET') {
      return json(res, 200, { duplicates: q.recentDups.all(Math.min(parseInt(u.searchParams.get('limit') || '20', 10), 200)) });
    }
    if (u.pathname === '/stats') {
      let size = 0; try { size = fs.statSync(DB_FILE).size; } catch (e) {}
      return json(res, 200, {
        service: 'db', engine: 'sqlite (node:sqlite, WAL)', file: DB_FILE, db_bytes: size,
        users: q.countUsers.get().n, rooms: q.countRooms.get().n, messages: q.countAll.get().n,
        duplicates_rejected_total: q.countDups.get().n, duplicates_rejected_since_boot: stats.duplicates_rejected,
        appended_since_boot: stats.appended, requests: stats.requests, subscribers: subscribers.size,
        uptime: Math.round((Date.now() - started) / 1000), node: process.version,
      });
    }
    if (u.pathname === '/health') {
      return json(res, 200, { status: 'ok', service: 'db', engine: 'sqlite', uptime: Math.round((Date.now() - started) / 1000), subscribers: subscribers.size, messages: q.countAll.get().n, requests: stats.requests });
    }
    json(res, 404, { error: 'no route' });
  } catch (e) {
    json(res, 500, { error: String(e.message || e) });
  }
});

const wss = new WebSocketServer({ server, path: '/subscribe' });
wss.on('connection', ws => {
  subscribers.add(ws);
  ws.on('close', () => subscribers.delete(ws));
  ws.on('error', () => subscribers.delete(ws));
});

setInterval(() => { try { q.purgeSess.run(Date.now()); } catch (e) {} }, 10 * 60 * 1000).unref();

server.keepAliveTimeout = 65000;
server.headersTimeout = 70000;
server.listen(PORT, HOST, () => log(`listening on ${HOST}:${PORT}, sqlite file ${DB_FILE} (${process.version}), ${q.countAll.get().n} messages`));

function shutdown() {
  log('shutting down');
  for (const ws of subscribers) ws.close(1001, 'server going away');
  server.close(() => { try { db.close(); } catch (e) {} process.exit(0); });
  setTimeout(() => process.exit(0), 2000).unref();
}
process.on('SIGTERM', shutdown); process.on('SIGINT', shutdown);

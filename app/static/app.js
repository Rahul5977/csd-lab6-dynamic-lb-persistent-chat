// ============================================================================
// app.js — chat client v3. Sends over HTTP POST with a CLIENT-MINTED message id
// that is reused on every retry (so the server can recognise duplicates);
// receives over a WebSocket the load balancer pins to one backend. Locked rooms
// encrypt/decrypt in the browser (Assignment-4 scheme) — the server never sees text.
// ============================================================================
(function () {
  'use strict';
  var $ = function (id) { return document.getElementById(id); };
  var me = null, currentRoom = null, roomRec = null, roomKey = null;
  var lastSeq = 0, ws = null, pollTimer = null, clusterTimer = null;
  var seenIds = {};          // id -> true for every message already rendered
  var pending = {};          // id -> DOM element of an optimistic bubble
  var lastSend = null;       // {id, room, body} — for the "resend" demo button

  // ── tiny fetch wrapper ────────────────────────────────────────────────────
  async function api(method, path, body) {
    var res = await fetch(path, {
      method: method,
      headers: body ? { 'Content-Type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined,
      credentials: 'same-origin',
    });
    var backend = res.headers.get('X-Backend-Id');
    if (backend) setBadge(backend);
    var data = await res.json().catch(function () { return {}; });
    if (!res.ok) { var err = new Error(data.error || (res.status + '')); err.status = res.status; throw err; }
    return data;
  }
  function setBadge(b) { $('backend-badge').textContent = b; }
  function uuid() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    var b = crypto.getRandomValues(new Uint8Array(16)); b[6] = (b[6] & 0x0f) | 0x40; b[8] = (b[8] & 0x3f) | 0x80;
    var h = Array.prototype.map.call(b, function (x) { return x.toString(16).padStart(2, '0'); }).join('');
    return h.slice(0, 8) + '-' + h.slice(8, 12) + '-' + h.slice(12, 16) + '-' + h.slice(16, 20) + '-' + h.slice(20);
  }

  // ── auth ──────────────────────────────────────────────────────────────────
  function showApp() {
    $('auth').hidden = true; $('app').hidden = false;
    $('me').textContent = me;
    loadRooms(); connectWS(); startCluster();
  }
  async function tryResume() {
    try { var r = await api('GET', '/api/me'); me = r.username; showApp(); } catch (e) {}
  }
  $('auth-form').addEventListener('submit', function (ev) { ev.preventDefault(); auth('/api/login'); });
  $('btn-register').addEventListener('click', function () { auth('/api/register'); });
  async function auth(path) {
    $('auth-error').hidden = true;
    try {
      var r = await api('POST', path, { username: $('auth-user').value.trim(), password: $('auth-pass').value });
      me = r.username; showApp();
    } catch (e) { $('auth-error').textContent = e.message; $('auth-error').hidden = false; }
  }
  $('btn-logout').addEventListener('click', async function () { try { await api('POST', '/api/logout'); } catch (e) {} location.reload(); });

  // ── rooms ─────────────────────────────────────────────────────────────────
  async function loadRooms() {
    try {
      var r = await api('GET', '/api/rooms');
      var nav = $('rooms'); nav.innerHTML = '';
      r.rooms.sort(function (a, b) { return a.id < b.id ? -1 : 1; }).forEach(function (room) {
        var b = document.createElement('button');
        b.textContent = room.id;
        if (room.locked) { var lock = document.createElement('span'); lock.textContent = '🔒'; b.appendChild(lock); }
        if (currentRoom === room.id) b.classList.add('active');
        b.addEventListener('click', function () { enterRoom(room.id); });
        nav.appendChild(b);
      });
    } catch (e) {}
  }
  $('btn-new-room').addEventListener('click', createRoom);
  $('new-room-name').addEventListener('keydown', function (ev) { if (ev.key === 'Enter') { ev.preventDefault(); createRoom(); } });
  async function createRoom() {
    var id = $('new-room-name').value.trim().toLowerCase();
    if (!id) return;
    try { await api('POST', '/api/rooms', { id: id }); $('new-room-name').value = ''; await loadRooms(); enterRoom(id); }
    catch (e) { alert(e.message); }
  }
  async function enterRoom(id) {
    currentRoom = id; lastSeq = 0; roomKey = null; seenIds = {}; pending = {};
    $('messages').innerHTML = '';
    $('composer').hidden = false;
    try { roomRec = await api('POST', '/api/rooms', { id: id }); } catch (e) { return; }
    updateRoomHead();
    if (roomRec.locked) {
      roomKey = await RoomCrypto.recall(id, roomRec.epoch);
      if (!roomKey) askPassphrase('unlock'); else updateRoomHead();
    }
    if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: 'join', room: id }));
    await loadHistory();
    loadRooms(); refreshCount();
  }
  function updateRoomHead() {
    $('room-title').textContent = currentRoom || 'Pick a room';
    var lockInfo = '';
    if (roomRec && roomRec.locked) lockInfo = roomKey ? ('🔒 encrypted · key ' + roomKey.fingerprint) : '🔒 encrypted · key needed';
    $('room-lock').textContent = lockInfo;
    $('btn-lock').hidden = !roomRec || roomRec.locked;
  }
  async function refreshCount() {
    if (!currentRoom) return;
    try { var c = await api('GET', '/api/messages/count?room=' + currentRoom); $('room-count').textContent = ' · ' + c.count + ' stored'; } catch (e) {}
  }

  // ── lock / unlock ─────────────────────────────────────────────────────────
  var modalMode = 'unlock';
  $('btn-lock').addEventListener('click', function () { askPassphrase('lock'); });
  function askPassphrase(mode) {
    modalMode = mode;
    $('key-title').textContent = mode === 'lock' ? 'Lock this room' : 'Unlock room';
    $('key-hint').textContent = mode === 'lock'
      ? 'Pick a shared passphrase (≥ 10 chars). Members need it to read this room. Locking is permanent.'
      : 'This room is end-to-end encrypted. Enter the shared passphrase.';
    $('key-ok').textContent = mode === 'lock' ? 'Lock' : 'Unlock';
    $('key-error').hidden = true; $('key-pass').value = '';
    $('key-modal').showModal();
  }
  $('key-cancel').addEventListener('click', function () { $('key-modal').close(); });
  $('key-form').addEventListener('submit', async function (ev) {
    ev.preventDefault();
    var pass = $('key-pass').value;
    try {
      if (modalMode === 'lock') {
        var epoch = roomRec.epoch + 1;
        var entry = await RoomCrypto.makeRoomKey(pass, currentRoom, epoch);
        roomRec = await api('POST', '/api/rooms/' + currentRoom + '/lock', { epoch: epoch, verifier: entry.verifier });
        roomKey = entry;
      } else {
        var entry2 = await RoomCrypto.makeRoomKey(pass, currentRoom, roomRec.epoch);
        if (entry2.verifier !== roomRec.verifier) throw new Error('Wrong passphrase for this room.');
        roomKey = entry2;
      }
      RoomCrypto.remember(currentRoom, roomKey.epoch, roomKey.raw);
      $('key-modal').close();
      updateRoomHead();
      $('messages').innerHTML = ''; lastSeq = 0; seenIds = {}; await loadHistory();
    } catch (e) { $('key-error').textContent = e.message; $('key-error').hidden = false; }
  });

  // ── messages ──────────────────────────────────────────────────────────────
  async function loadHistory() {
    try {
      var r = await api('GET', '/api/messages?room=' + currentRoom + '&since=' + lastSeq);
      for (var i = 0; i < r.messages.length; i++) await renderMessage(r.messages[i]);
      scrollDown();
    } catch (e) {}
  }
  function metaText(m, extra) {
    var parts = [];
    if (m.kind !== 'system') parts.push(m.from);
    parts.push(new Date(m.ts).toLocaleTimeString());
    if (m.seq) parts.push('#' + m.seq);
    if (m.via) parts.push('via ' + m.via);
    if (extra) parts.push(extra);
    return parts.join(' · ');
  }
  async function renderMessage(m) {
    // Dedupe twice: by seq (WS vs poll overlap) and by id (a retried send that the
    // server deduplicated still arrives once over the firehose).
    if (m.seq && m.seq <= lastSeq && !pending[m.id]) return;
    if (m.id && seenIds[m.id]) return;
    if (m.seq) lastSeq = Math.max(lastSeq, m.seq);
    if (m.id) seenIds[m.id] = true;
    if (m.roomMeta) { roomRec = m.roomMeta; updateRoomHead(); }
    var wrap = pending[m.id];
    if (wrap) { delete pending[m.id]; wrap.classList.remove('pending'); wrap.innerHTML = ''; }
    else { wrap = document.createElement('div'); }
    var mine = m.from === me;
    wrap.className = 'msg' + (mine ? ' mine' : '') + (m.kind === 'system' ? ' system' : '');
    var meta = document.createElement('div'); meta.className = 'meta'; meta.textContent = metaText(m);
    var bubble = document.createElement('div'); bubble.className = 'bubble';
    if (m.kind === 'enc') {
      var out = roomKey ? await RoomCrypto.decryptRoom(roomKey, m.room || currentRoom, m.env) : { ok: false, reason: 'no-key' };
      if (out.ok) bubble.textContent = out.payload.text;
      else { bubble.textContent = out.reason === 'integrity' ? '⚠ integrity check failed' : '🔒 encrypted (no key)'; bubble.classList.add('undecryptable'); }
    } else bubble.textContent = m.text;
    if (m.kind !== 'system') wrap.appendChild(meta);
    wrap.appendChild(bubble);
    var box = $('messages');
    var empty = box.querySelector('.empty'); if (empty) empty.remove();
    if (!wrap.parentNode) box.appendChild(wrap);
    refreshCount();
  }
  function scrollDown() { var box = $('messages'); box.scrollTop = box.scrollHeight; }

  // Optimistic bubble shown while a send is in flight (status in the meta line).
  function showPending(id, text) {
    var wrap = document.createElement('div'); wrap.className = 'msg mine pending';
    var meta = document.createElement('div'); meta.className = 'meta'; meta.textContent = 'sending…';
    var bubble = document.createElement('div'); bubble.className = 'bubble'; bubble.textContent = text;
    wrap.appendChild(meta); wrap.appendChild(bubble);
    var box = $('messages'); var empty = box.querySelector('.empty'); if (empty) empty.remove();
    box.appendChild(wrap); pending[id] = wrap; scrollDown();
    return wrap;
  }
  function setPendingStatus(id, status, cls) {
    var wrap = pending[id]; if (!wrap) return;
    var meta = wrap.querySelector('.meta'); if (meta) meta.textContent = status;
    if (cls) wrap.classList.add(cls);
  }

  // Send with retries that REUSE the same message id. A network error mid-send
  // (LB failover, reconnection) is retried up to 3×; the server stores it once.
  async function sendWithRetry(id, room, body, display) {
    var attempt = 0, delay = 400, res = null;
    while (attempt < 4) {
      attempt++;
      try {
        res = await api('POST', '/api/messages', Object.assign({ id: id, room: room }, body));
        break;
      } catch (e) {
        if (e.status && e.status >= 400 && e.status < 500) throw e;   // our fault — do not retry
        setPendingStatus(id, 'retrying (' + attempt + ')…');
        await new Promise(function (r) { setTimeout(r, delay); }); delay *= 2;
      }
    }
    if (!res) throw new Error('send failed after retries');
    if (res.duplicate) {
      setPendingStatus(id, 'already stored as #' + res.seq + ' — duplicate suppressed (' + (res.dedup || 'db') + ')', 'dup');
      var w = pending[id]; if (w) { delete pending[id]; w.classList.remove('pending'); setTimeout(function () { w.remove(); }, 12000); }
    } else if (!pending[id]) {
      // WS delivered it before the POST returned — nothing to do.
    } else {
      setPendingStatus(id, 'stored #' + res.seq + ' via ' + res.backend);
    }
    return res;
  }

  $('composer').addEventListener('submit', async function (ev) {
    ev.preventDefault();
    var text = $('msg-input').value.trim();
    if (!text || !currentRoom) return;
    $('msg-input').value = '';
    var id = uuid();
    try {
      var body;
      if (roomRec && roomRec.locked) {
        if (!roomKey) { askPassphrase('unlock'); return; }
        body = { env: await RoomCrypto.encryptRoom(roomKey, currentRoom, { text: text }) };
      } else body = { text: text };
      showPending(id, text);
      lastSend = { id: id, room: currentRoom, body: body, text: text };
      $('btn-resend').disabled = false;
      await sendWithRetry(id, currentRoom, body, text);
    } catch (e) { setPendingStatus(id, 'failed: ' + e.message, 'failed'); }
  });
  // Duplicate-prevention demo: re-POST the last message with the SAME id.
  $('btn-resend').disabled = true;
  $('btn-resend').addEventListener('click', async function () {
    if (!lastSend) return;
    var id = lastSend.id;
    showPending(id, lastSend.text + '  (resend with same id)');
    try { await sendWithRetry(id, lastSend.room, lastSend.body, lastSend.text); }
    catch (e) { setPendingStatus(id, 'failed: ' + e.message, 'failed'); }
  });

  // ── live delivery: WS primary, polling fallback ───────────────────────────
  function setConn(on, label) { var el = $('conn'); el.className = 'conn ' + (on ? 'on' : 'off'); el.textContent = label; }
  function connectWS() {
    var proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
    ws = new WebSocket(proto + location.host + '/ws');
    ws.onopen = function () { setConn(true, 'Connected'); stopPolling(); if (currentRoom) ws.send(JSON.stringify({ type: 'join', room: currentRoom })); };
    ws.onmessage = async function (ev) {
      var m = JSON.parse(ev.data);
      if (m.type === 'hello' || m.type === 'joined') setBadge(m.backend);
      if (m.type === 'msg' && m.room === currentRoom) { await renderMessage(m.entry); scrollDown(); }
      if (m.type === 'room') loadRooms();
    };
    ws.onclose = function () { setConn(false, 'Reconnecting…'); startPolling(); setTimeout(connectWS, 2000); };
    ws.onerror = function () { ws.close(); };
  }
  function startPolling() { if (!pollTimer) pollTimer = setInterval(function () { if (currentRoom) loadHistory(); }, 3000); }
  function stopPolling() { clearInterval(pollTimer); pollTimer = null; }

  // ── cluster panel: live pool from the load balancer ───────────────────────
  function startCluster() { refreshCluster(); if (!clusterTimer) clusterTimer = setInterval(refreshCluster, 3000); }
  async function refreshCluster() {
    try {
      var res = await fetch('/lb/stats', { credentials: 'same-origin' });
      if (!res.ok) throw new Error('no lb');
      var s = await res.json();
      var list = $('cluster-list'); list.innerHTML = '';
      var up = 0;
      (s.backends || []).forEach(function (b) {
        if (b.state === 'UP' || b.state === 'DEGRADED') up++;
        var row = document.createElement('div'); row.className = 'cl-row ' + String(b.state || '').toLowerCase();
        var name = document.createElement('span'); name.textContent = b.id;
        var st = document.createElement('span'); st.className = 'cl-state';
        st.textContent = (b.state || '?') + (b.ewma_ms != null ? ' · ' + Math.round(b.ewma_ms) + 'ms' : '') + (b.load && b.load.cpu_pct != null ? ' · cpu ' + b.load.cpu_pct + '%' : '');
        row.appendChild(name); row.appendChild(st); list.appendChild(row);
      });
      $('cluster-count').textContent = up + ' active · ' + (s.algorithm || '');
      $('cluster').hidden = false;
    } catch (e) { $('cluster').hidden = true; }
  }

  tryResume();
})();

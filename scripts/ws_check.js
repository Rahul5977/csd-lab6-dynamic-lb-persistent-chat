// WebSocket delivery through the load balancer: log in over HTTP, open /ws through
// the LB, join a room, post a message, and require it to arrive on the socket.
const path = require('path');
const ROOT = process.argv[2];
const { WebSocket } = require(path.join(ROOT, 'app', 'vendor', 'ws'));
const LB = process.argv[3] || 'http://127.0.0.1:18000';
const user = 'wsprobe' + Date.now().toString().slice(-6);

(async () => {
  let cookie = '';
  const api = async (m, p, b) => {
    const r = await fetch(LB + p, { method: m, headers: Object.assign(
      b ? { 'Content-Type': 'application/json' } : {}, cookie ? { Cookie: cookie } : {}),
      body: b ? JSON.stringify(b) : undefined });
    const sc = r.headers.get('set-cookie'); if (sc) cookie = sc.split(';')[0];
    return { status: r.status, data: await r.json().catch(() => ({})) };
  };
  let r = await api('POST', '/api/register', { username: user, password: 'ws-probe-pass-1' });
  if (r.status !== 200) throw new Error('register failed ' + r.status + ' ' + JSON.stringify(r.data));
  await api('POST', '/api/rooms', { id: 'wsroom' });

  const ws = new WebSocket(LB.replace('http', 'ws') + '/ws', { headers: { Cookie: cookie } });
  const got = [];
  const done = new Promise((res, rej) => {
    const t = setTimeout(() => rej(new Error('timed out waiting for the message')), 12000);
    ws.on('message', (d) => {
      const m = JSON.parse(d);
      got.push(m.type);
      if (m.type === 'joined') api('POST', '/api/messages', { room: 'wsroom', text: 'hello over the socket' });
      if (m.type === 'msg' && m.entry && m.entry.text === 'hello over the socket') { clearTimeout(t); res(m); }
    });
    ws.on('error', (e) => { clearTimeout(t); rej(new Error(e.message + ' [frames: ' + got.join(',') + ']')); });
    ws.on('close', (c, r) => { clearTimeout(t); rej(new Error('closed ' + c + ' ' + r + ' [frames: ' + got.join(',') + ']')); });
  });
  ws.on('open', () => ws.send(JSON.stringify({ type: 'join', room: 'wsroom' })));
  const msg = await done;
  console.log('  ✓ WebSocket upgrade proxied by the LB (frames seen: ' + got.join(', ') + ')');
  console.log('  ✓ message delivered over the socket, via backend ' + msg.via + ', seq ' + msg.entry.seq);
  ws.close();
  process.exit(0);
})().catch(e => { console.error('  ✗ WebSocket through the LB FAILED:', e.message); process.exit(1); });

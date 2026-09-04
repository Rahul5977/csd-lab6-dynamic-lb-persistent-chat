// ============================================================================
// crypto.js — client-side E2E room encryption.
//
// This is the Assignment-4 ("ChatFat") scheme, preserved EXACTLY:
//   salt     = SHA-256("ChatFat-room-v1|" + roomId)          (deterministic)
//   K        = PBKDF2-HMAC-SHA256(passphrase, salt, 250 000 iterations, 256 bit)
//   cipher   = AES-256-GCM, fresh 96-bit IV per message
//   AAD      = utf8(roomId + "|" + b64(nonce16) + "|" + keyEpoch)   (aadv 1)
//   envelope = { alg:"A256GCM", kid:epoch, n, iv, ct, aadv:1 }
//   verifier = b64(HMAC-SHA-256(K, "ChatFat-room-verify|" + roomId + "|" + epoch))
//   fingerprint = hex(SHA-256(K))[0..8)
// The passphrase never leaves the browser; the server stores ciphertext only.
// ============================================================================
(function () {
  'use strict';
  var subtle = window.crypto.subtle;
  var enc = new TextEncoder();
  var dec = new TextDecoder();
  var KDF_ITERATIONS = 250000;

  function b64(buf) {
    var bytes = new Uint8Array(buf), s = '';
    for (var i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
    return btoa(s);
  }
  function unb64(str) {
    var s = atob(str), out = new Uint8Array(s.length);
    for (var i = 0; i < s.length; i++) out[i] = s.charCodeAt(i);
    return out;
  }
  function hex(buf) {
    var bytes = new Uint8Array(buf), s = '';
    for (var i = 0; i < bytes.length; i++) s += bytes[i].toString(16).padStart(2, '0');
    return s;
  }
  function rand(n) { return window.crypto.getRandomValues(new Uint8Array(n)); }

  // Deterministic salt: a later joiner derives the key from the passphrase
  // alone; the room id inside makes the same passphrase differ across rooms.
  function roomSalt(roomId) {
    return subtle.digest('SHA-256', enc.encode('ChatFat-room-v1|' + roomId));
  }

  async function deriveRoomKey(passphrase, roomId) {
    var salt = await roomSalt(roomId);
    var base = await subtle.importKey('raw', enc.encode(passphrase), 'PBKDF2', false, ['deriveBits']);
    return subtle.deriveBits({ name: 'PBKDF2', salt: salt, iterations: KDF_ITERATIONS, hash: 'SHA-256' }, base, 256);
  }

  async function verifierFor(rawKey, roomId, keyEpoch) {
    var hmacKey = await subtle.importKey('raw', rawKey, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
    var sig = await subtle.sign('HMAC', hmacKey, enc.encode('ChatFat-room-verify|' + roomId + '|' + keyEpoch));
    return b64(sig);
  }

  async function fingerprintFor(rawKey) {
    var digest = await subtle.digest('SHA-256', rawKey);
    return hex(digest).slice(0, 8);
  }

  async function makeRoomKey(passphrase, roomId, keyEpoch) {
    var raw = await deriveRoomKey(passphrase, roomId);
    var key = await subtle.importKey('raw', raw, { name: 'AES-GCM' }, false, ['encrypt', 'decrypt']);
    return {
      key: key, raw: raw, epoch: keyEpoch,
      verifier: await verifierFor(raw, roomId, keyEpoch),
      fingerprint: await fingerprintFor(raw),
    };
  }

  // AAD binds ciphertext to room + client nonce + key epoch: no cross-room replay.
  function aadFor(roomId, nonceB64, kid) {
    return enc.encode(roomId + '|' + nonceB64 + '|' + kid);
  }

  async function encryptRoom(entry, roomId, payload) {
    var iv = rand(12);
    var n = b64(rand(16));
    var ct = await subtle.encrypt(
      { name: 'AES-GCM', iv: iv, additionalData: aadFor(roomId, n, entry.epoch) },
      entry.key, enc.encode(JSON.stringify(payload)));
    return { alg: 'A256GCM', kid: entry.epoch, n: n, iv: b64(iv), ct: b64(ct), aadv: 1 };
  }

  // → { ok:true, payload } | { ok:false, reason:'no-key'|'integrity' }
  async function decryptRoom(entry, roomId, envelope) {
    if (!entry || entry.epoch !== envelope.kid) return { ok: false, reason: 'no-key' };
    try {
      var pt = await subtle.decrypt(
        { name: 'AES-GCM', iv: unb64(envelope.iv), additionalData: aadFor(roomId, envelope.n, envelope.kid) },
        entry.key, unb64(envelope.ct));
      return { ok: true, payload: JSON.parse(dec.decode(pt)) };
    } catch (e) {
      return { ok: false, reason: 'integrity' };
    }
  }

  // Browser key store (localStorage) — same convenience as Assignment 4.
  var STORE = 'chat-room-keys-v1';
  function loadAll() { try { return JSON.parse(localStorage.getItem(STORE)) || {}; } catch (e) { return {}; } }
  function remember(roomId, epoch, rawKey) {
    var all = loadAll(); all[roomId] = { epoch: epoch, k: b64(rawKey) };
    try { localStorage.setItem(STORE, JSON.stringify(all)); } catch (e) {}
  }
  async function recall(roomId, epoch) {
    var rec = loadAll()[roomId];
    if (!rec || rec.epoch !== epoch) return null;
    var raw = unb64(rec.k).buffer;
    var key = await subtle.importKey('raw', raw, { name: 'AES-GCM' }, false, ['encrypt', 'decrypt']);
    return { key: key, raw: raw, epoch: epoch, fingerprint: await fingerprintFor(raw) };
  }
  function forget(roomId) {
    var all = loadAll(); delete all[roomId];
    try { localStorage.setItem(STORE, JSON.stringify(all)); } catch (e) {}
  }

  window.RoomCrypto = {
    KDF_ITERATIONS: KDF_ITERATIONS,
    makeRoomKey: makeRoomKey, encryptRoom: encryptRoom, decryptRoom: decryptRoom,
    verifierFor: verifierFor, remember: remember, recall: recall, forget: forget,
  };
})();

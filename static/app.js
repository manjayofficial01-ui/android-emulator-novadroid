/* NovaDroid frontend — vanilla JS, offline-safe (no CDN).
   Split hosting: same-origin '' locally; set a tunnel URL + token in the
   backend bar when served from Vercel (persisted in localStorage).
   v4.0.0: toasts, theme, tab nav, keymap layer, drag-drop APK, ports,
   AVD tools, quick ops, batch install, macro loops, sync-input. */
const $ = s => document.querySelector(s);
const API_BASE = () => (localStorage.getItem('nd_api') || '').replace(/\/$/, '');
const API_TOK = () => localStorage.getItem('nd_token') || '';
const apiHdrs = () => ({'Content-Type': 'application/json',
  ...(API_TOK() ? {Authorization: 'Bearer ' + API_TOK()} : {})});

async function api(m, p, b) {
  const r = await fetch(API_BASE() + p,
    {method: m, headers: apiHdrs(), body: b ? JSON.stringify(b) : undefined});
  let d = {};
  try { d = await r.json(); } catch (e) { d = {ok: false, err: 'bad JSON from agent'}; }
  if (r.status === 401) {
    d.err = 'unauthorized — paste the backend token in the 🔌 bar';
    sessionTicket = null;   // force a fresh ticket before the next binary GET
  }
  return d;
}
/* Never let a dead agent break the page: every call site gets a dict back. */
async function safeApi(m, p, b) {
  try { return await api(m, p, b); }
  catch (e) { return {ok: false, err: 'backend unreachable — is the agent running? (' + e.message + ')'}; }
}

const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const kb = n => (n < 1024 ? n + ' KB' : (n / 1024).toFixed(1) + ' MB');

/* ======== v5 short-lived session tickets (binary GETs only) ==============
   Query-string tokens are removed. A header-authenticated POST /api/session
   mints a 300 s ticket valid ONLY on /api/screen, /api/screen/stream and
   /api/record/download via ?ticket=. Cached in memory (never localStorage)
   with its acquisition time; refreshed during long-lived activity and after
   any 401. Local mode (no token) stays completely headerless / ticketless. */
let sessionTicket = null;          // {ticket, ts}
let ticketInFlight = null;         // de-dupe concurrent /api/session calls
const TICKET_REFRESH_MS = 120000;  // refresh well before the 300 s expiry

async function getTicket(force) {
  if (!API_TOK()) return '';
  const now = Date.now();
  if (!force && sessionTicket && now - sessionTicket.ts < TICKET_REFRESH_MS)
    return sessionTicket.ticket;
  if (ticketInFlight) return ticketInFlight;
  ticketInFlight = (async () => {
    try {
      const r = await fetch(API_BASE() + '/api/session',
        {method: 'POST', headers: apiHdrs(), body: '{}'});
      if (r.status === 401) { sessionTicket = null; return ''; }
      let d = {};
      try { d = await r.json(); } catch (e) { d = {}; }
      if (d.ok && d.ticket) {
        sessionTicket = {ticket: d.ticket, ts: Date.now()};
        return d.ticket;
      }
      sessionTicket = null;
      return '';
    } catch (e) { sessionTicket = null; return ''; }
    finally { ticketInFlight = null; }
  })();
  return ticketInFlight;
}

async function ticketParam(force) {
  const t = await getTicket(force);
  return t ? '&ticket=' + encodeURIComponent(t) : '';
}

/* ============================== TOASTS =================================== */
/* alert() blocks the single thread — the old build froze the live view every
   time it told you something worked. Toasts don't. */
function toast(msg, kind = 'info', title = '', ms = 0) {
  const root = $('#toast-root'); if (!root) return;
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.innerHTML = (title ? `<div class="t-title">${esc(title)}</div>` : '') +
                 esc(String(msg ?? '')).replace(/\n/g, '<br>');
  root.appendChild(el);
  requestAnimationFrame(() => el.classList.add('show'));
  const life = ms || (kind === 'err' ? 7000 : 4200);
  setTimeout(() => { el.classList.remove('show'); setTimeout(() => el.remove(), 300); }, life);
}
const okT = (m, t) => toast(m, 'ok', t || '✓');
const errT = (m, t) => toast(m, 'err', t || '✗');
const infoT = (m, t) => toast(m, 'info', t);

/* Copy a short string to the host clipboard. `navigator.clipboard` is only
   exposed in secure contexts — a plain-HTTP tunnel is not one, so fall back
   to the legacy selection path, and finally to showing the text in a toast.
   Never throws: a failed copy must not take down the Engine card. */
async function copyText(text) {
  if (!text) return false;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (e) { /* permission denied / no clipboard — use the fallback */ }
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.cssText = 'position:fixed;top:-9999px;opacity:0';
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } finally { ta.remove(); }
    return ok;
  } catch (e) { return false; }
}

/* ============================== THEME ==================================== */
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  localStorage.setItem('nd_theme', t);
  const b = $('#btn-theme'); if (b) b.textContent = t === 'light' ? '🌙' : '☀';
}
applyTheme(localStorage.getItem('nd_theme') || 'dark');
$('#btn-theme').onclick = () =>
  applyTheme(document.documentElement.dataset.theme === 'light' ? 'dark' : 'light');

/* ============================== TABS ===================================== */
$('#tabbar').onclick = e => {
  const b = e.target.closest('.tab'); if (!b) return;
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t === b));
  $('.grid').dataset.filter = b.dataset.tab;
  if (b.dataset.tab === 'display') loadEnv();   // refresh camera-environment state
};
$('.grid').dataset.filter = 'system';

/* ===================== STATUS RIBBON & SECURITY ========================= */
/* Compact, always-visible state line: connection, version, auth, channel.
   Refreshed from /api/health (conn/version/auth) + /api/engine (channel). */
const ribbon = {conn: '● connecting…', connCls: '', ver: 'agent v—',
                auth: '—', channel: '—', channelCls: ''};
function paintRibbon() {
  const c = $('#nd-conn'); if (!c) return;
  c.textContent = ribbon.conn; c.className = 'pill nd-pill ' + ribbon.connCls;
  const v = $('#nd-ver'); if (v) v.textContent = ribbon.ver;
  const a = $('#nd-auth'); if (a) a.textContent = 'auth ' + ribbon.auth;
  const ch = $('#nd-channel');
  if (ch) { ch.textContent = 'engine ' + ribbon.channel; ch.className = 'pill nd-pill ' + ribbon.channelCls; }
}
function paintSecure() {
  const el = $('#conn-secure'); if (!el) return;
  const locked = !!API_TOK();
  el.className = 'nd-secure' + (locked ? ' locked' : '');
  el.textContent = locked ? '🔒 token' : '○ local';
  el.title = locked ? 'Authenticated with a Bearer token — binary GETs use short-lived tickets'
                    : 'Local / open mode — no token configured';
}
function applyHealth(h, ok) {
  ribbon.ver = 'agent v' + (h.version || '?');
  ribbon.auth = h.auth || (h.remote ? 'token' : 'open');
  ribbon.conn = ok ? '● online' : '● offline';
  ribbon.connCls = ok ? 'ok' : 'bad';
  if (h.version) $('#agent-v').textContent = h.version;
  paintRibbon();
}
async function loadHealth() {
  const h = await safeApi('GET', '/api/health');
  applyHealth(h || {}, !!(h && h.ok));
}

/* One active device drives every panel (the old build had three separate
   selects that silently reset each other on every 15s refresh). */
const dev = () => $('#device').value || '';
function needDev() {
  if (!dev()) { errT('No device selected — Quickboot an AVD, then pick it from the Device list.'); return false; }
  return true;
}

let macroSteps = [];
let liveOn = false, liveTimer = null, liveTarget = 2000;
let streamOn = false;
let drag = null;
let logcatTimer = null;
let recTimer = null;
let smoothTimer = null;

async function refreshAll() {
  await Promise.all([loadInfo(), loadProfiles(), loadAvds(), loadDevices(),
                     loadMacros(), loadDisplayMeta(), loadSensors(), loadNetwork(),
                     loadHealth(), loadEngine()]);
  loadShared(); loadHost(); renderKeymap(); loadGpuState();
}

/* ============================== INFO / HEALTH ============================= */
async function loadInfo() {
  try {
    const d = await api('GET', '/api/info');
    $('#health').textContent = d.ok ? '● engine online' : '● engine issue';
    $('#health').className = 'pill ' + (d.ok ? 'ok' : 'bad');
    $('#sdk').textContent = 'SDK: ' + (d.sdk || 'not found') + ' · agent v' + (d.version || '?');
    $('#agent-v').textContent = d.version || '?';
    $('#info').textContent = JSON.stringify({
      sdk: d.sdk, adb: !!d.adb, emulator: !!d.emulator,
      accel: d.accel?.accel, ready: d.accel?.ready,
      host: d.host, defaults: d.defaults}, null, 1);
    const b = $('#accel-banner');
    if (!d.accel?.ready) {
      b.className = 'banner warn';
      b.innerHTML = '⚠ Hardware acceleration not ready — enable <b>Windows Hypervisor Platform</b> + BIOS VT-x/AMD-V. AEHD sunsets Dec 31 2026; WHPX is mandatory. ' + esc(d.accel?.detail || '');
    } else {
      b.className = 'banner ok';
      b.textContent = '✓ ' + (d.accel?.detail || 'WHPX ready — hardware-accelerated execution (8–12× faster than TCG).');
    }
  } catch (e) {
    $('#health').textContent = '● backend offline';
    $('#health').className = 'pill bad';
  }
}

/* ================================ AVD FLEET =============================== */
async function loadProfiles() {
  const d = await api('GET', '/api/profiles');
  $('#profile').innerHTML = (d.profiles || []).map(p => `<option>${esc(p)}</option>`).join('');
}

async function loadAvds() {
  const d = await api('GET', '/api/avds');
  const box = $('#avds'); box.innerHTML = '';
  if (!d.avds?.length) {
    box.innerHTML = '<div class="mono">No AVDs yet — create one above. First boot provisions the Play image (minutes); later quickboots take seconds.</div>';
    return;
  }
  d.avds.forEach(a => {
    const c = a.config || {};
    const state = a.running ? '<span class="dot on"></span>running'
      : (a.job?.state && a.job.state !== 'ready' ? `<span class="dot booting"></span>${esc(a.job.state)}` : '');
    const el = document.createElement('div'); el.className = 'item';
    el.innerHTML = `<div class="grow">
        <div class="name"><label class="chk"><input type="checkbox" class="fleet-chk" value="${esc(a.name)}"> ${esc(a.name)}</label>
          ${state ? `<span class="state">${state}</span>` : ''}</div>
        <div class="meta">cpu ${esc(c['hw.cpu.ncore'] || '?')} · ram ${esc(c['hw.ramSize'] || '?')}MB · gpu ${esc(c['hw.gpu.mode'] || '?')} · ${esc(c['image.sysdir.1'] || '')} ${a.serial ? '· ' + esc(a.serial) : ''}</div>
      </div>
      <div class="row tight">
        <button class="btn small primary" data-act="start">▶ Quickboot</button>
        <button class="btn small" data-act="cold">❄ Cold</button>
        <button class="btn small" data-act="stop">⏹ Stop</button>
        <button class="btn small" data-act="winfix">⌖ Fix window</button>
        <button class="btn small" data-act="clone">⧉ Clone</button>
        <button class="btn small" data-act="wipe">🧹 Wipe</button>
        <button class="btn small danger" data-act="del">✕</button>
      </div>`;
    el.querySelectorAll('button').forEach(btn => btn.onclick = () => avdAct(a.name, btn.dataset.act));
    box.appendChild(el);
  });
}

async function avdAct(name, act) {
  if (act === 'del' && !confirm(`Delete ${name}? This removes its disk images permanently.`)) return;
  if (act === 'wipe' && !confirm(`Wipe ${name}? User data AND quickboot snapshots are deleted — the next boot is a clean first boot (minutes).`)) return;
  if (act === 'clone') {
    const dst = prompt(`Clone ${name} into which new AVD name?`, name + '_2');
    if (!dst) return;
    const d = await safeApi('POST', '/api/avds/clone', {src: name, name: dst.trim()});
    d.ok ? okT(`Cloned → ${d.name}. ${d.note || ''}`, 'Clone') : errT('Clone failed: ' + (d.err || '?'));
    loadAvds(); return;
  }
  if (act === 'winfix') {
    const d = await safeApi('POST', '/api/avds/window/fix', {name});
    infoT((d.saved?.ok ? `Window position reset → (${d.saved.x}, ${d.saved.y}). ` : 'Reset failed. ') +
      (d.live?.ok ? 'Running window moved on screen.' : 'Not running — takes effect on next boot.'), 'Fix window');
    return;
  }
  const map = {
    start: ['/api/avds/start', {name}],
    cold: ['/api/avds/start', {name, cold: true}],
    stop: ['/api/avds/stop', {name}],
    wipe: ['/api/avds/wipe', {name}]
  };
  if (act === 'del') {
    await fetch(API_BASE() + '/api/avds?name=' + encodeURIComponent(name),
      {method: 'DELETE', headers: apiHdrs()});
  } else {
    const d = await safeApi('POST', map[act][0], map[act][1]);
    if (!d.ok) errT('Failed: ' + (d.err || 'unknown'));
    else if (act === 'start' || act === 'cold') infoT(d.note || 'Starting…', name);
  }
  setTimeout(() => { loadAvds(); loadDevices(); }, 1500);
}

function checkedAvds() {
  return [...document.querySelectorAll('.fleet-chk:checked')].map(c => c.value);
}
$('#btn-fleet-all').onclick = () => {
  const boxes = [...document.querySelectorAll('.fleet-chk')];
  const on = boxes.some(b => !b.checked);
  boxes.forEach(b => b.checked = on);
};
$('#btn-fleet-start').onclick = async () => {
  const names = checkedAvds();
  if (!names.length) return errT('Check at least one AVD first.');
  const d = await safeApi('POST', '/api/fleet', {names, action: 'start', stagger: 8});
  d.ok ? okT(`Started: ${d.note}`, 'Fleet') : errT('Failed: ' + (d.err || '?'));
  loadAvds(); setTimeout(loadDevices, 4000);
};
$('#btn-fleet-stop').onclick = async () => {
  const names = checkedAvds();
  if (!names.length) return errT('Check at least one AVD first.');
  if (!confirm(`Stop ${names.length} AVD(s)?`)) return;
  const d = await safeApi('POST', '/api/fleet', {names, action: 'stop'});
  d.ok ? okT(`Stopped: ${d.note}`, 'Fleet') : errT('Failed: ' + (d.err || '?'));
  loadAvds(); loadDevices();
};

$('#btn-create').onclick = async e => {
  e.currentTarget.disabled = true; e.currentTarget.textContent = 'Creating… (1–5 min)';
  const d = await safeApi('POST', '/api/avds', {profile: $('#profile').value});
  d.ok ? okT('Created: ' + d.name + ' (4 CPU / 4GB / gpu auto applied)', 'Create AVD')
       : errT('Failed: ' + (d.out || d.err || 'unknown'));
  e.currentTarget.disabled = false; e.currentTarget.textContent = '＋ Create AVD';
  loadAvds();
};
$('#btn-raw').onclick = async () => {
  const body = {name: $('#raw-name').value.trim(), extra: $('#raw-flags').value};
  if ($('#raw-affinity') && $('#raw-affinity').checked) {
    // Mask comes from /api/cpu (P-core detection); let the agent resolve it.
    const cpu = await safeApi('GET', '/api/cpu');
    if (cpu && cpu.hybrid && cpu.pcore_mask) {
      body.affinity = cpu.pcore_mask;
      okT(`Pinning to P-cores (mask 0x${cpu.pcore_mask.toString(16)}, ${cpu.pcore_count} of ${cpu.logical_cpus} logical)`, 'Hybrid CPU');
    } else {
      errT('No hybrid CPU / P-core mask detected — booting without affinity.');
    }
  }
  const d = await safeApi('POST', '/api/avds/start-raw', body);
  d.ok ? okT('Launched: ' + d.cmd, 'Raw boot') : errT('Failed: ' + d.err);
  setTimeout(() => { loadAvds(); loadDevices(); }, 3000);
};

/* Hybrid-CPU info line under Advanced launch */
(async () => {
  const cpu = await api('GET', '/api/cpu').catch(() => null);
  const el = $('#hybrid-info'); if (!el) return;
  if (cpu && cpu.hybrid && cpu.pcore_mask) {
    el.textContent = `hybrid CPU: ${cpu.pcore_count} P-cores / ${cpu.ecore_count} E-cores — P-core pin available`;
  } else if (cpu && cpu.hybrid) {
    el.textContent = 'hybrid CPU: P/E split not resolvable — update the emulator to 37.3.6+ for the throttling fix';
  } else if (cpu && cpu.logical_cpus) {
    el.textContent = `${cpu.logical_cpus} logical CPUs (uniform)`;
  }
})();

/* ================================ DEVICES ================================= */
async function loadDevices() {
  const d = await api('GET', '/api/devices');
  const prev = $('#device').value;
  const sel = $('#device'); sel.innerHTML = '';
  const list = d.devices || [];
  sel.innerHTML = list.map(x =>
    `<option value="${esc(x.serial)}">${esc(x.serial)} — ${esc(x.model || x.state)}</option>`).join('');
  if (!list.length) {
    sel.innerHTML = '<option value="">(no devices — Quickboot an AVD)</option>';
  } else if (list.some(x => x.serial === prev)) {
    sel.value = prev;                       // keep the operator's selection
  } else {
    const online = list.find(x => x.state === 'device');
    if (online) sel.value = online.serial;
  }
  $('#drop-target').textContent = dev() || 'device';
  const box = $('#devices'); box.innerHTML = '';
  if (!list.length) {
    box.innerHTML = '<div class="mono">No devices online. Quickboot an AVD and wait for boot (adb shows emulator-XXXX).</div>';
    return;
  }
  list.forEach(x => {
    const el = document.createElement('div'); el.className = 'item';
    el.innerHTML = `<div class="grow"><div class="name">${esc(x.serial)}
        <span class="state">${x.state === 'device' ? '<span class="dot on"></span>online' : esc(x.state)}</span></div>
      <div class="meta">${esc(x.model || '')} · Android ${esc(x.android || '?')} (sdk ${esc(x.sdk || '?')}) · ${esc(x.abi || '')}${x.bridge ? ' · bridge ' + esc(x.bridge) : ''}</div></div>`;
    box.appendChild(el);
  });
}

/* ============================ LIVE SCREEN ================================= */
async function screenUrl() {
  const u = API_BASE() + '/api/screen?serial=' + encodeURIComponent(dev()) + '&t=' + Date.now();
  // <img> cannot send an Authorization header. A short-lived session ticket
  // (POST /api/session, header-authenticated) rides in the query string for
  // the three binary GETs only. Local mode stays headerless / ticketless.
  return u + await ticketParam();
}

async function loadScreen() {
  const url = await screenUrl();
  if (streamOn) return;            // a stream took over while the ticket was fetched
  return new Promise(res => {
    const img = $('#screen');
    let done = false;
    const finish = () => { if (!done) { done = true; res(); } };
    img.onload = finish;
    img.onerror = finish;
    setTimeout(finish, 10000);      // never let one bad frame hang the loop
    img.src = url;
  });
}

async function shot() {
  if (!needDev()) return;
  // A one-off capture would replace the stream URL and kill the stream.
  if (streamOn) {
    $('#live-info').textContent = 'streaming ✓ (already live)';
    return;
  }
  $('#live-info').textContent = 'capture…';
  await loadScreen();
  if (!liveOn) $('#live-info').textContent = 'idle';
}

/* Download the CURRENT frame as a PNG file — works mid-stream too (a fresh
   one-off capture is fetched so the file never contains the multipart URL). */
async function saveFrame() {
  if (!needDev()) return;
  try {
    const url = await screenUrl();
    const r = await fetch(url);
    if (r.status === 401) { sessionTicket = null; return errT('Capture unauthorized — retry to get a fresh ticket'); }
    if (!r.ok) return errT('Capture failed (HTTP ' + r.status + ')');
    const b = await r.blob();
    const u = URL.createObjectURL(b);
    const a = document.createElement('a');
    a.href = u;
    a.download = `novadroid-${dev()}-${new Date().toISOString().replace(/[:.]/g,'-')}.png`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(u), 5000);
    okT('Frame saved to your downloads.', 'Save frame');
  } catch (e) { errT('Capture failed: ' + e.message); }
}
$('#btn-shot-save').onclick = saveFrame;

function scheduleLive(ms) {
  clearTimeout(liveTimer);
  if (liveOn) liveTimer = setTimeout(tickLive, ms);
}

async function tickLive() {
  if (!liveOn) return;
  if (document.hidden || drag) { scheduleLive(500); return; }   // don't waste adb calls
  const t0 = performance.now();
  await loadScreen();
  const dt = performance.now() - t0;
  // Self-scheduling + adaptive: the next frame waits at least as long as the
  // last one took, so a slow device can never pile up requests.
  const wait = Math.max(liveTarget, Math.round(dt * 1.3));
  $('#live-info').textContent = `${Math.round(dt)}ms/frame · ${(wait / 1000).toFixed(1)}s`;
  scheduleLive(wait);
}

$('#btn-shot').onclick = shot;
$('#btn-live').onclick = e => {
  const b = e.currentTarget;
  if (!dev()) { errT('Quickboot an AVD first — live view needs a device.'); return; }
  if (streamOn) { stopStream(); }          // the two modes fight over img.src
  liveOn = !liveOn;
  b.classList.toggle('on', liveOn);
  b.textContent = liveOn ? '⏸ Live' : '▶ Live';
  if (liveOn) { liveTarget = +$('#live-fps').value || 2000; tickLive(); }
  else { clearTimeout(liveTimer); $('#live-info').textContent = 'idle'; }
};
$('#live-fps').onchange = () => { liveTarget = +$('#live-fps').value || 2000; };

/* ---- MJPEG stream: one connection, continuous frames ---- */
async function streamUrl() {
  const u = API_BASE() + '/api/screen/stream?serial=' + encodeURIComponent(dev()) +
    '&fps=10&t=' + Date.now();
  return u + await ticketParam(true);   // fresh ticket before a long-lived stream
}
function stopStream() {
  streamOn = false;
  $('#btn-stream').classList.remove('on');
  $('#btn-stream').textContent = '⚡ Stream';
  $('#screen').src = '';
  $('#live-info').textContent = 'stream stopped';
  clearTimeout(liveTimer); liveOn = false;
  $('#btn-live').classList.remove('on');
  $('#btn-live').textContent = '▶ Live';
}
$('#btn-stream').onclick = async e => {
  const b = e.currentTarget;
  if (streamOn) { stopStream(); return; }
  if (!dev()) { errT('Quickboot an AVD first — streaming needs a device.'); return; }
  // Stop the polling loop first, or it will keep overwriting img.src.
  liveOn = false; clearTimeout(liveTimer);
  $('#btn-live').classList.remove('on'); $('#btn-live').textContent = '▶ Live';
  streamOn = true;
  b.classList.add('on'); b.textContent = '⏸ Stream';
  $('#live-info').textContent = 'streaming…';
  const img = $('#screen');
  img.onload = () => { if (streamOn) $('#live-info').textContent = 'streaming ✓'; };
  // Not every browser can do multipart/x-mixed-replace (Safari cannot); if no
  // frame ever arrives, fall back to the polling loop instead of a dead img.
  img.onerror = () => {
    if (!streamOn) return;
    sessionTicket = null;          // a rejected/expired ticket: re-acquire before polling
    streamOn = false;
    b.classList.remove('on'); b.textContent = '⚡ Stream';
    $('#live-info').textContent = 'stream unsupported — fell back to polling';
    liveOn = true; $('#btn-live').classList.add('on'); $('#btn-live').textContent = '⏸ Live';
    tickLive();
  };
  const url = await streamUrl();
  if (!streamOn) return;          // stopped while the ticket was being fetched
  img.src = url;
};

/* ------------------------- interactive touch ---------------------------- */
function toDevice(clientX, clientY) {
  const img = $('#screen');
  const nw = img.naturalWidth, nh = img.naturalHeight;
  if (!nw || !nh) return null;
  const r = img.getBoundingClientRect();
  if (!r.width || !r.height) return null;
  return {x: Math.round((clientX - r.left) / r.width * nw),
          y: Math.round((clientY - r.top) / r.height * nh)};
}

function showDot(e) {
  const stage = $('.stage'), dot = $('#touch-dot');
  const r = stage.getBoundingClientRect();
  dot.classList.remove('hidden');
  dot.style.left = (e.clientX - r.left) + 'px';
  dot.style.top = (e.clientY - r.top) + 'px';
}
const hideDot = () => $('#touch-dot').classList.add('hidden');

async function sendTap(x, y) {
  await safeApi('POST', '/api/input/tap', {serial: dev(), x, y});
  $('#input-log').textContent = `tap ${x},${y}`;
  scheduleLive(250);
}
async function sendSwipe(x1, y1, x2, y2, ms) {
  await safeApi('POST', '/api/input/swipe', {serial: dev(), x1, y1, x2, y2, ms});
  $('#input-log').textContent = `swipe ${x1},${y1} → ${x2},${y2} (${Math.round(ms)}ms)`;
  scheduleLive(400);
}

const screenEl = $('#screen');
screenEl.addEventListener('dragstart', e => e.preventDefault());
screenEl.addEventListener('pointerdown', e => {
  if (!needDev()) return;
  const p = toDevice(e.clientX, e.clientY);
  if (!p) { errT('Screenshot first — the device resolution is read from the last frame.'); return; }
  e.preventDefault();
  try { screenEl.setPointerCapture(e.pointerId); } catch (err) { /* ignore */ }
  drag = {...p, t: performance.now()};
  showDot(e);
});
screenEl.addEventListener('pointermove', e => { if (drag) showDot(e); });
screenEl.addEventListener('pointerup', e => {
  if (!drag) return;
  const start = drag;
  drag = null;
  hideDot();
  const end = toDevice(e.clientX, e.clientY);
  if (!end) return;
  // Keymap capture mode: this click DEFINES a mapping instead of tapping.
  if (kmCapture && kmCapture.key) {
    addKeymap({key: kmCapture.key, type: 'tap', x: end.x, y: end.y});
    toast(`Mapped key "${kmCapture.key}" → tap ${end.x},${end.y}`, 'ok', 'Keymap');
    kmCapture = null;
    return;
  }
  const dt = performance.now() - start.t;
  const dist = Math.hypot(end.x - start.x, end.y - start.y);
  // A short, still press is a tap; anything else is a swipe whose duration
  // matches how long the finger actually travelled.
  if (dist < 12 && dt < 500) sendTap(end.x, end.y);
  else sendSwipe(start.x, start.y, end.x, end.y, Math.max(120, Math.min(dt, 3000)));
});
screenEl.addEventListener('pointercancel', () => { drag = null; hideDot(); });

document.querySelectorAll('.navkey').forEach(b => b.onclick = async () => {
  if (!needDev()) return;
  await safeApi('POST', '/api/input/key', {serial: dev(), code: +b.dataset.key});
  scheduleLive(500);
});
$('#btn-rotate').onclick = async () => {
  if (!needDev()) return;
  await safeApi('POST', '/api/rotate', {serial: dev()});
  scheduleLive(900);
};
$('#btn-type').onclick = async () => {
  if (!needDev()) return;
  const t = $('#type-text').value;
  if (!t) return;
  const d = await safeApi('POST', '/api/input/text', {serial: dev(), text: t});
  d.ok ? $('#input-log').textContent = `sent ${t.length} chars`
       : errT('type failed: ' + d.err);
  $('#type-text').value = '';
  scheduleLive(500);
};
$('#type-text').addEventListener('keydown', e => { if (e.key === 'Enter') $('#btn-type').click(); });

$('#btn-uia').onclick = async () => {
  if (!needDev()) return;
  const d = await safeApi('GET', '/api/layout?serial=' + encodeURIComponent(dev()));
  const el = $('#layout'); el.classList.remove('hidden');
  el.textContent = typeof d.tree === 'string'
    ? d.tree.slice(0, 4000) : JSON.stringify(d.tree, null, 1).slice(0, 4000);
};

/* ============================ KEYBOARD CONTROL ============================ */
/* BlueStacks-class keymapping, done as a pure manager layer: host keys are
   mapped to tap/swipe/keyevent and dispatched through the same input API the
   interactive screen uses. Persisted per browser in localStorage. */
let kmCapture = null;   // {key} while waiting for the next screen tap
const kmLoad = () => { try { return JSON.parse(localStorage.getItem('nd_keymaps') || '[]'); }
                       catch (e) { return []; } };
const kmSave = m => localStorage.setItem('nd_keymaps', JSON.stringify(m));
function addKeymap(m) { const ms = kmLoad(); ms.push(m); kmSave(ms); renderKeymap(); }
function renderKeymap() {
  const box = $('#keymap-list'); if (!box) return;
  const ms = kmLoad();
  box.innerHTML = ms.length ? '' : '<div class="mono">No mappings. "Map next key" → press a key → click the screen point. Keys fire while ⌨ Keys is checked.</div>';
  ms.forEach((m, i) => {
    const el = document.createElement('div'); el.className = 'item';
    const desc = m.type === 'tap' ? `tap ${m.x},${m.y}`
      : m.type === 'swipe' ? `swipe ${m.x1},${m.y1}→${m.x2},${m.y2}`
      : `key ${m.code}`;
    el.innerHTML = `<div class="grow"><div class="name"><kbd>${esc((m.key || '').toUpperCase())}</kbd></div>
      <div class="meta">${esc(desc)}</div></div>
      <button class="btn small danger" data-i="${i}">✕</button>`;
    el.querySelector('button').onclick = () => { const a = kmLoad(); a.splice(i, 1); kmSave(a); renderKeymap(); };
    box.appendChild(el);
  });
}
$('#btn-km-add').onclick = () => {
  infoT('Press any key, then click the point on the device screen it should tap. Esc cancels.', 'Keymap capture');
  kmCapture = {key: null};
};
$('#btn-km-clear').onclick = () => { kmSave([]); renderKeymap(); okT('Keymaps cleared.'); };
$('#btn-km-wasd').onclick = () => {
  const ms = kmLoad().filter(m => !'wasd'.includes(m.key));
  ms.push(
    {key: 'w', type: 'swipe', x1: 540, y1: 1600, x2: 540, y2: 900, ms: 260},
    {key: 's', type: 'swipe', x1: 540, y1: 900,  x2: 540, y2: 1600, ms: 260},
    {key: 'a', type: 'swipe', x1: 900, y1: 1200, x2: 300, y2: 1200, ms: 260},
    {key: 'd', type: 'swipe', x1: 300, y1: 1200, x2: 900, y2: 1200, ms: 260});
  kmSave(ms); renderKeymap();
  okT('W/A/S/D mapped to screen swipes — hold ⌨ Keys and press them while a game runs.', 'WASD preset');
};
document.addEventListener('keydown', e => {
  if (kmCapture && !kmCapture.key) {
    if (e.key === 'Escape') { kmCapture = null; infoT('Keymap capture cancelled.'); return; }
    if (e.key.length === 1 || e.key.startsWith('Arrow') || e.key.startsWith('F')) {
      kmCapture.key = e.key.toLowerCase();
      infoT(`Key "${kmCapture.key}" captured — now click the target point on the screen.`, 'Keymap capture');
      e.preventDefault();
    }
    return;
  }
  if (!$('#keymap-on')?.checked) return;
  const tag = document.activeElement?.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
  if (e.ctrlKey || e.altKey || e.metaKey) return;
  const m = kmLoad().find(x => x.key === e.key.toLowerCase());
  if (!m) return;
  e.preventDefault();
  if (!dev()) return;
  if (m.type === 'tap') safeApi('POST', '/api/input/tap', {serial: dev(), x: m.x, y: m.y});
  else if (m.type === 'swipe') safeApi('POST', '/api/input/swipe',
    {serial: dev(), x1: m.x1, y1: m.y1, x2: m.x2, y2: m.y2, ms: m.ms || 300});
  else safeApi('POST', '/api/input/key', {serial: dev(), code: m.code});
  scheduleLive(300);
});

/* ============================== APP & WORLD ============================== */
$('#btn-apk').onclick = async () => {
  if (!needDev()) return;
  const p = $('#apk').value.trim();
  if (!p) return errT('Enter an APK path');
  const d = await safeApi('POST', '/api/apk/install', {serial: dev(), apk_path: p});
  d.ok ? okT('Installed via ' + d.tool, 'APK') : errT('Install failed: ' + (d.log || d.err || '').slice(-400));
  loadApps();
};
$('#btn-apk-all').onclick = async () => {
  const p = $('#apk').value.trim();
  if (!p) return errT('Enter an APK path first (top field).');
  if (!confirm(`Install ${p} on EVERY online emulator, in parallel?`)) return;
  const d = await safeApi('POST', '/api/apk/install-all', {apk_path: p});
  d.ok ? okT(`Installed on ${d.count}/${d.total} instance(s): ${(d.installed || []).join(', ')}`, 'Fleet install')
       : errT('Batch install: ' + (d.err || `${d.count}/${d.total} succeeded — ${(d.failed || []).map(f => f.serial).join(', ')}`));
  loadApps();
};
$('#btn-geo').onclick = async () => {
  if (!needDev()) return;
  await safeApi('POST', '/api/geo', {serial: dev(), lat: +$('#lat').value, lng: +$('#lng').value});
  $('#input-log').textContent = `GPS → ${$('#lat').value}, ${$('#lng').value}`;
};
document.querySelectorAll('.geo').forEach(b => b.onclick = () => {
  $('#lat').value = b.dataset.lat; $('#lng').value = b.dataset.lng; $('#btn-geo').click();
});
$('#btn-batt').onclick = async () => {
  if (!needDev()) return;
  await safeApi('POST', '/api/battery',
    {serial: dev(), level: +$('#batt').value, status: $('#batt-status').value});
  $('#input-log').textContent = `battery ${$('#batt').value}% (${$('#batt-status').value})`;
};
$('#btn-batt-reset').onclick = async () => {
  if (!needDev()) return;
  await safeApi('POST', '/api/battery/reset', {serial: dev()});
  $('#input-log').textContent = 'battery mocking off';
};
$('#btn-shell').onclick = async () => {
  const d = await safeApi('POST', '/api/shell', {serial: dev(), cmd: $('#shell').value});
  $('#shell-out').textContent = (d.out || '') + (d.err ? '\nERR: ' + d.err : '');
  // Shell is disabled by default on v5 unless started with --allow-shell:
  // surface that error rather than leaving the operator guessing.
  if (!d.ok && d.err) errT(d.err, 'ADB shell');
};
$('#shell').addEventListener('keydown', e => { if (e.key === 'Enter') $('#btn-shell').click(); });
$('#btn-perf').onclick = async () => {
  if (!needDev()) return;
  const d = await safeApi('GET', '/api/perf?serial=' + encodeURIComponent(dev()));
  const p = d.perf || {};
  $('#perf').textContent = 'sampled ✓';
  $('#perf-out').textContent =
    `load  ${p.load?.load1 ?? '?'} ${p.load?.load5 ?? '?'} ${p.load?.load15 ?? '?'}\n` +
    `mem   ${p.mem?.used_mb ?? '?'} / ${p.mem?.total_mb ?? '?'} MB (${p.mem?.pct ?? '?'}%)\n` +
    `up    ${p.uptime || '?'}\n\n${p.top || ''}`;
  if (p.mem?.pct != null) { setBar('gmem', p.mem.pct); $('#val-gmem').textContent = p.mem.pct + '%'; }
  if (p.load?.load1 != null) {
    const cpu = p.load.load1 / (hostCpus || 4) * 100;
    setBar('load', Math.min(100, cpu)); $('#val-load').textContent = p.load.load1.toFixed(2);
  }
};

/* ---- drag & drop an APK anywhere on the page → upload → install ---- */
let dragDepth = 0;
window.addEventListener('dragenter', e => {
  if (![...(e.dataTransfer?.types || [])].includes('Files')) return;
  dragDepth++; $('#dropveil').classList.remove('hidden');
  $('#drop-target').textContent = dev() || 'device';
});
window.addEventListener('dragleave', () => { if (--dragDepth <= 0) { dragDepth = 0; $('#dropveil').classList.add('hidden'); } });
window.addEventListener('dragover', e => e.preventDefault());
window.addEventListener('drop', async e => {
  e.preventDefault(); dragDepth = 0; $('#dropveil').classList.add('hidden');
  const f = [...(e.dataTransfer?.files || [])].find(f => f.name.toLowerCase().endsWith('.apk'));
  const any = [...(e.dataTransfer?.files || [])][0];
  if (!any) return;
  if (!f) { errT(any.name + ' is not an .apk — use the Files tab to upload it.'); return; }
  if (!dev()) return errT('Pick a device first, then drop the APK again.');
  infoT('Uploading ' + f.name + '…', 'Drop install');
  const b64 = await new Promise(res => {
    const r = new FileReader(); r.onload = () => res(r.result.split(',')[1]); r.readAsDataURL(f);
  });
  const up = await safeApi('POST', '/api/shared/upload', {name: f.name, b64});
  if (!up.ok) return errT('Upload failed: ' + (up.err || ''));
  if (!up.host_path) return errT('Agent is older than the dashboard — update novadroid.py.');
  $('#apk').value = up.host_path;
  $('#apk-check').value = up.host_path;
  const d = await safeApi('POST', '/api/apk/install', {serial: dev(), apk_path: up.host_path});
  d.ok ? okT(`${f.name} (${kb(up.bytes)}) uploaded and installed via ${d.tool}.`, 'Drop install ✓')
       : errT('Uploaded, but install failed: ' + (d.log || d.err || '').slice(-300));
  loadApps();
});

/* ============================= SYSTEM HEALTH ============================= */
let hostCpus = 0;
function setBar(id, pct) {
  const el = $('#bar-' + id);
  if (!el) return;
  const v = Math.max(0, Math.min(100, Number(pct) || 0));
  el.style.width = v + '%';
  el.classList.toggle('hot', v >= 85);
  el.classList.toggle('warm', v >= 65 && v < 85);
}
async function loadHost() {
  const d = await api('GET', '/api/host');
  if (!d.ok) return;
  hostCpus = d.cpu_count || 0;
  setBar('cpu', d.cpu_pct || 0);
  $('#val-cpu').textContent = (d.cpu_pct == null ? '–' : d.cpu_pct + '%');
  setBar('ram', d.mem?.pct || 0);
  $('#val-ram').textContent = d.mem?.pct != null
    ? `${d.mem.used_mb} / ${d.mem.total_mb} MB` : '–';
  const dk = d.disk || {};
  if (dk.total_mb) {
    const usedPct = 100 * (1 - dk.free_mb / dk.total_mb);
    setBar('disk', usedPct);
    $('#val-disk').textContent = `${(dk.free_mb / 1024).toFixed(0)} GB free`;
  }
  $('#host-head').textContent =
    `${d.cpu_count || '?'} cores · ${d.emulator_count || 0} emulator process(es)` +
    (d.emulator_mem_mb ? ` · ${d.emulator_mem_mb} MB` : '');
  $('#host-out').textContent = (d.emulators || [])
    .map(e => `${e.name}  pid ${e.pid}  ${e.mem_mb} MB`).join('\n') || '(no emulator processes)';
}

/* ============================ APPS & LOGCAT ============================== */
let appCache = [];
function renderApps() {
  const q = ($('#app-filter').value || '').toLowerCase().trim();
  const box = $('#apps'); box.innerHTML = '';
  const items = appCache.filter(a => !q || a.package.toLowerCase().includes(q));
  if (!items.length) {
    box.innerHTML = '<div class="mono">(none — press Load with a device online)</div>';
    return;
  }
  items.slice(0, 300).forEach(a => {
    const el = document.createElement('div'); el.className = 'item';
    el.innerHTML = `<div class="grow"><div class="name">${esc(a.package)}</div>
        <div class="meta">${a.versionCode ? 'v' + esc(a.versionCode) + ' · ' : ''}${esc((a.apk || '').split('/').pop() || '')}</div></div>
      <div class="row tight">
        <button class="btn small primary" data-act="launch" data-pkg="${esc(a.package)}">▶ Launch</button>
        <button class="btn small" data-act="stop" data-pkg="${esc(a.package)}">⏹ Stop</button>
        <button class="btn small" data-act="grant" data-pkg="${esc(a.package)}">🔑 Grant</button>
        <button class="btn small" data-act="clear" data-pkg="${esc(a.package)}">🧹 Data</button>
        <button class="btn small" data-act="archive" data-pkg="${esc(a.package)}" title="Smart App Archive: remove the APK, keep app data — reinstall later restores it with its state">📦</button>
        <button class="btn small danger" data-act="uninstall" data-pkg="${esc(a.package)}">✕</button>
      </div>`;
    box.appendChild(el);
  });
}
$('#apps').onclick = async e => {
  const b = e.target.closest('button'); if (!b) return;
  const pkg = b.dataset.pkg; if (!needDev()) return;
  if (b.dataset.act === 'uninstall' && !confirm(`Uninstall ${pkg}?`)) return;
  if (b.dataset.act === 'clear' && !confirm(`Wipe app data for ${pkg}? (keeps it installed)`)) return;
  if (b.dataset.act === 'archive' && !confirm(`Smart-archive ${pkg}? The APK is saved to NovaDroid-Shared\\Archived and removed from the device — app DATA stays.`)) return;
  const url = {launch: '/api/apps/launch', stop: '/api/apps/stop',
               clear: '/api/apps/clear', uninstall: '/api/apps/uninstall',
               grant: '/api/apps/grant', archive: '/api/apps/archive'}[b.dataset.act];
  const d = await safeApi('POST', url, {serial: dev(), package: pkg});
  $('#input-log').textContent = `${b.dataset.act} ${pkg}: ${d.ok ? 'ok' : (d.err || 'failed')}`;
  if (b.dataset.act === 'grant') d.ok ? okT(d.note || 'granted', 'Grant ' + pkg) : errT(d.err || 'grant failed');
  if (b.dataset.act === 'archive') d.ok ? okT(d.note || 'archived', 'Smart Archive ' + pkg) : errT(d.err || 'archive failed');
  if (b.dataset.act === 'uninstall' || b.dataset.act === 'archive') loadApps();
  scheduleLive(400);
};
$('#btn-archived').onclick = async () => {
  const d = await safeApi('GET', '/api/apps/archived');
  const box = $('#apps'); box.innerHTML = '';
  const items = d.archived || [];
  if (!items.length) {
    box.innerHTML = '<div class="mono">Nothing archived yet — press 📦 on an installed app: the APK is saved to NovaDroid-Shared\\Archived and removed from the device while its data stays.</div>';
    return;
  }
  items.forEach(a => {
    const el = document.createElement('div'); el.className = 'item';
    el.innerHTML = `<div class="grow"><div class="name">${esc(a.package)}</div>
        <div class="meta">${(a.bytes / 1e6).toFixed(1)} MB · ${esc(a.file)}</div></div>
      <div class="row tight"><button class="btn small primary" data-act="restore" data-pkg="${esc(a.package)}">⤓ Restore</button></div>`;
    el.querySelector('button').onclick = async () => {
      if (!needDev()) return;
      const r = await safeApi('POST', '/api/apps/restore-archive', {serial: dev(), package: a.package});
      r.ok ? okT(`${a.package} reinstalled with its data.`, 'Archive restore') : errT(r.err || 'restore failed');
    };
    box.appendChild(el);
  });
};
$('#btn-apps').onclick = loadApps;
$('#app-filter').oninput = renderApps;
$('#app-scope').onchange = loadApps;
async function loadApps() {
  if (!dev()) return;
  const d = await safeApi('GET', `/api/apps?serial=${encodeURIComponent(dev())}&third_party=${$('#app-scope').value}`);
  appCache = d.packages || [];
  renderApps();
}

$('#btn-logcat').onclick = fetchLogcat;
async function fetchLogcat() {
  if (!needDev()) return;
  const d = await safeApi('GET', `/api/logcat?serial=${encodeURIComponent(dev())}` +
    `&lines=${$('#logcat-lines').value}&spec=${encodeURIComponent($('#logcat-spec').value)}`);
  $('#logcat-out').textContent = (d.lines || []).join('\n') || (d.err || '(empty)');
  $('#logcat-out').scrollTop = 0;
}
$('#btn-logcat-live').onclick = e => {
  const b = e.currentTarget;
  if (logcatTimer) { clearInterval(logcatTimer); logcatTimer = null; b.classList.remove('on'); b.textContent = '▶ Tail'; }
  else if (needDev()) { b.classList.add('on'); b.textContent = '⏸ Tail'; fetchLogcat(); logcatTimer = setInterval(fetchLogcat, 3000); }
};
$('#btn-logcat-clear').onclick = async () => {
  if (!needDev()) return;
  await safeApi('POST', '/api/logcat/clear', {serial: dev()});
  $('#logcat-out').textContent = '(cleared)';
};

/* ========================== DEVICE SIMULATION ============================ */
async function loadSensors() {
  const d = await api('GET', '/api/sensors?serial=' + encodeURIComponent(dev()));
  const kinds = d.sensors || {};
  $('#sensor-kind').innerHTML = Object.entries(kinds)
    .map(([k, v]) => `<option value="${esc(k)}" title="${esc(v)}">${esc(k)}</option>`).join('')
    || '<option value="acceleration">acceleration</option>';
}
async function loadNetwork() {
  const d = await api('GET', '/api/network?serial=' + encodeURIComponent(dev()));
  const fill = (el, arr, cur) => {
    $(el).innerHTML = (arr || []).map(s =>
      `<option value="${esc(s)}"${s === cur ? ' selected' : ''}>${esc(s)}</option>`).join('');
  };
  fill('#net-speed', d.speeds, 'full');
  fill('#net-delay', d.delays, 'none');
}
$('#btn-sensor').onclick = async () => {
  if (!needDev()) return;
  const vals = [$('#sv1').value, $('#sv2').value, $('#sv3').value].filter(v => v !== '');
  if (!vals.length) return errT('Enter at least one value');
  const d = await safeApi('POST', '/api/sensor',
    {serial: dev(), kind: $('#sensor-kind').value, values: vals});
  d.ok ? $('#input-log').textContent = `${$('#sensor-kind').value} = ${vals.join(':')}`
       : errT('failed: ' + d.err);
};
document.querySelectorAll('.sensor-preset').forEach(b => b.onclick = async () => {
  if (!needDev()) return;
  await safeApi('POST', '/api/sensor',
    {serial: dev(), kind: b.dataset.kind, values: b.dataset.v.split(':')});
  $('#input-log').textContent = `${b.dataset.kind} = ${b.dataset.v}`;
});
$('#btn-net').onclick = async () => {
  if (!needDev()) return;
  const d = await safeApi('POST', '/api/network',
    {serial: dev(), speed: $('#net-speed').value, delay: $('#net-delay').value});
  d.ok ? $('#input-log').textContent = `network ${$('#net-speed').value} / ${$('#net-delay').value}`
       : errT('failed: ' + d.err);
};
$('#btn-finger').onclick = async () => {
  if (!needDev()) return;
  const d = await safeApi('POST', '/api/biometric', {serial: dev(), press: true});
  d.ok ? $('#input-log').textContent = 'fingerprint sent' : errT('failed: ' + d.err);
};
$('#btn-power').onclick = async () => {
  if (!needDev()) return;
  await safeApi('POST', '/api/power', {serial: dev()});
  scheduleLive(700);
};
$('#btn-call').onclick = async () => {
  if (!needDev()) return;
  const d = await safeApi('POST', '/api/call', {serial: dev(), number: $('#tel-num').value});
  d.ok ? $('#input-log').textContent = `calling ${$('#tel-num').value}` : errT('failed: ' + d.err);
};
$('#btn-sms').onclick = async () => {
  if (!needDev()) return;
  const d = await safeApi('POST', '/api/sms',
    {serial: dev(), number: $('#tel-num').value, text: $('#sms-text').value});
  d.ok ? $('#input-log').textContent = 'sms sent' : errT('failed: ' + d.err);
};
$('#btn-sig').onclick = async () => {
  if (!needDev()) return;
  await safeApi('POST', '/api/signal', {serial: dev(), level: +$('#sig-level').value});
  $('#input-log').textContent = `signal ${$('#sig-level').value}`;
};
$('#btn-snap-save').onclick = () => snapAct('save');
$('#btn-snap-load').onclick = () => snapAct('load');
$('#btn-snap-del').onclick = () => {
  if (confirm(`Delete snapshot "${$('#snap-name').value || 'default_boot'}"?`)) snapAct('del');
};
async function snapAct(action) {
  if (!needDev()) return;
  const d = await safeApi('POST', '/api/snapshots',
    {serial: dev(), action, name: $('#snap-name').value || 'default_boot'});
  d.ok ? okT(`snapshot ${action} ok`, 'Snapshots') : errT('failed: ' + (d.err || d.log || ''));
}

$('#btn-clip-get').onclick = async () => {
  if (!needDev()) return;
  const d = await safeApi('GET', '/api/clipboard?serial=' + encodeURIComponent(dev()));
  if (!d.ok) { errT(d.err); return; }
  $('#clip').value = d.text || (d.note || '');
  infoT(d.note || 'clipboard read');
};
$('#btn-clip-set').onclick = async () => {
  if (!needDev()) return;
  const text = $('#clip').value;
  const d = await safeApi('POST', '/api/clipboard', {serial: dev(), text});
  if (d.ok) { okT('clipboard sent'); return; }
  // Image without a clipboard console: fall back to typing into the field
  // that currently has focus on the device.
  if (confirm('This image has no clipboard console.\n\nType the text into the '
    + 'field that has focus on the device instead?')) {
    const r = await safeApi('POST', '/api/input/text', {serial: dev(), text});
    r.ok ? okT(`typed ${text.length} chars into focused field`) : errT('failed: ' + r.err);
    scheduleLive(500);
  } else errT(d.err);
};

$('#btn-rec-start').onclick = async () => {
  if (!needDev()) return;
  const d = await safeApi('POST', '/api/record/start',
    {serial: dev(), seconds: +$('#rec-secs').value || 30});
  $('#rec-state').textContent = d.ok ? d.note : ('failed: ' + d.err);
  if (d.ok) { clearInterval(recTimer); recTimer = setInterval(pollRec, 1500); }
};
$('#btn-rec-stop').onclick = async () => {
  if (!needDev()) return;
  const d = await safeApi('POST', '/api/record/stop', {serial: dev()});
  const dl = d.ok ? await ticketParam(true) : '';   // fresh ticket for the download link
  $('#rec-state').innerHTML = d.ok
    ? 'saved → ' + esc(d.file) + '<br><a class="dl" href="' + API_BASE() + '/api/record/download?file='
      + encodeURIComponent((d.file || '').split(/[\\/]/).pop())
      + dl + '">⬇ download mp4</a>'
    : esc('failed: ' + (d.err || '?'));
  clearInterval(recTimer); recTimer = null;
};
async function pollRec() {
  const d = await api('GET', '/api/record?serial=' + encodeURIComponent(dev()));
  if (!d.ok) return;
  $('#rec-state').textContent = d.state === 'recording'
    ? `⏺ recording… ${d.elapsed_s}s elapsed${d.remaining_s ? ', ' + d.remaining_s + 's left' : ''}`
    : (d.state || d.err || '');
  if (d.state !== 'recording') { clearInterval(recTimer); recTimer = null; }
}

/* =========================== TABLET & DISPLAY ============================ */
async function loadDisplayMeta() {
  try {
    const p = await api('GET', '/api/display/presets');
    const box = $('#presets'); box.innerHTML = '';
    [...(p.tablets || []), ...(p.phones || [])].forEach(t => {
      const b = document.createElement('button'); b.className = 'btn small';
      b.textContent = t.label; b.title = `${t.w}x${t.h} @ ${t.dpi}dpi — ${t.note || ''}`;
      b.onclick = async () => {
        $('#dw').value = t.w; $('#dh').value = t.h; $('#ddpi').value = t.dpi;
        if (!dev()) return infoT('Preset staged below — Quickboot a device, then Apply live.');
        const r = await safeApi('POST', '/api/display/apply',
          {serial: dev(), w: t.w, h: t.h, dpi: t.dpi});
        $('#disp-cur').textContent = JSON.stringify(r, null, 1);
        scheduleLive(600);
      };
      box.appendChild(b);
    });
    const s = await api('GET', '/api/skins');
    $('#skin').innerHTML = (s.tablet_skins || []).map(x => `<option>${esc(x)}</option>`).join('') +
      `<option disabled>── all frames ──</option>` +
      (s.skins || []).map(x => `<option>${esc(x)}</option>`).join('');
    $('#skins-note').textContent =
      `${(s.skins || []).length} frames shipped · tablet-class: ${(s.tablet_skins || []).join(', ')}`;
  } catch (e) { /* offline */ }
}
$('#btn-disp-get').onclick = async () => {
  if (!needDev()) return;
  const d = await safeApi('GET', '/api/display?serial=' + encodeURIComponent(dev()));
  $('#disp-cur').textContent = JSON.stringify(d, null, 1);
};
$('#btn-disp-apply').onclick = async () => {
  if (!needDev()) return;
  const d = await safeApi('POST', '/api/display/apply',
    {serial: dev(), w: +$('#dw').value, h: +$('#dh').value, dpi: +$('#ddpi').value});
  $('#disp-cur').textContent = JSON.stringify(d, null, 1);
  scheduleLive(600);
};
$('#btn-disp-reset').onclick = async () => {
  if (!needDev()) return;
  const d = await safeApi('POST', '/api/display/reset', {serial: dev()});
  $('#disp-cur').textContent = JSON.stringify(d, null, 1);
  scheduleLive(600);
};
$('#btn-disp-save').onclick = async () => {
  const name = prompt('Save display into which AVD?', 'medium_tablet'); if (!name) return;
  const d = await safeApi('POST', '/api/display/save',
    {name, w: +$('#dw').value, h: +$('#dh').value, dpi: +$('#ddpi').value});
  d.ok ? okT(`Saved ${d.display} → ${d.avd} (stop it first if running)`, 'Save display') : errT('Failed: ' + d.err);
  loadAvds();
};
document.querySelectorAll('.ori').forEach(b => b.onclick = async () => {
  if (!needDev()) return;
  const d = await safeApi('POST', '/api/display/orientation', {serial: dev(), mode: b.dataset.mode});
  $('#disp-cur').textContent = JSON.stringify(d, null, 1);
  scheduleLive(900);
});
document.querySelectorAll('.hz').forEach(b => b.onclick = async () => {
  if (!needDev()) return;
  document.querySelectorAll('.hz').forEach(x => x.classList.toggle('active', x === b));
  const d = await safeApi('POST', '/api/display/refresh', {serial: dev(), hz: +b.dataset.hz});
  d.ok ? okT(`${d.hz} Hz set (${d.verified_hz} verified). ${d.note || ''}`, 'High-FPS')
       : errT('Refresh rate: ' + (d.err || 'image refused the setting'));
});
$('#btn-hz-save').onclick = async () => {
  const name = prompt('Persist the selected refresh rate into which AVD?', 'medium_phone');
  if (!name) return;
  const active = document.querySelector('.hz.active');
  const hz = active ? +active.dataset.hz : 60;
  const d = await safeApi('POST', '/api/display/refresh/save', {name, hz});
  d.ok ? okT(`${hz} Hz will be the default for ${name}.`, 'Save rate') : errT('Failed: ' + d.err);
};
$('#btn-skin').onclick = async () => {
  const name = prompt('Apply frame to which AVD? (must be stopped)', 'medium_tablet'); if (!name) return;
  const d = await safeApi('POST', '/api/avds/skin', {name, skin: $('#skin').value});
  d.ok ? okT(`Frame ${d.skin} → ${d.avd}`) : errT('Failed: ' + d.err);
};

/* ---- camera environment (emulator 36.6.11+ environment.ini) ---- */
function envAvdName() {
  const n = ($('#avdtool-name')?.value || '').trim();
  if (n) return n;
  const chk = document.querySelector('.fleet-chk:checked');
  if (chk) return chk.value;
  const first = document.querySelector('.fleet-chk');
  return first ? first.value : '';
}
async function loadEnv() {
  const st = $('#env-state'); if (!st) return;
  const name = envAvdName();
  if (!name) { st.textContent = 'enter an AVD name first'; return; }
  const d = await safeApi('GET', '/api/avds/environment?name=' + encodeURIComponent(name));
  if (!d || !d.ok) { st.textContent = (d && d.err) || 'unavailable'; return; }
  if (d.mode && $('#env-mode')) $('#env-mode').value = d.mode;
  st.textContent = d.exists
    ? 'mode: ' + (d.mode || 'none') + (d.hint ? ' · ' + d.hint : '')
    : (d.hint || 'no environment.ini');
}
$('#btn-env-apply').onclick = async () => {
  const name = envAvdName();
  if (!name) { errT('Enter an AVD name (AVD tools) or check an AVD first.'); return; }
  const mode = $('#env-mode') ? $('#env-mode').value : 'environment';
  const d = await safeApi('POST', '/api/avds/environment', {name, mode});
  if (d.ok) okT('Environment mode set to ' + mode + ' for ' + name + '.', 'Camera environment');
  else errT(d.err || 'Failed to set environment mode', 'Camera environment');
  loadEnv();
};

/* ======================= SHARED FOLDER + APK COMPAT ====================== */
let autosyncOn = false;
function fileRow(name, meta, btn) {
  const el = document.createElement('div'); el.className = 'item';
  el.innerHTML = `<div class="grow"><div class="name">${esc(name)}</div>
    <div class="meta">${esc(meta)}</div></div><div class="row tight">${btn}</div>`;
  return el;
}
async function loadShared() {
  try {
    const s = await api('GET', '/api/shared');
    $('#shared-path').textContent = s.dir || '';
    const pc = $('#shared-pc'); pc.innerHTML = '';
    const desk = s.desktop || s.files || [];
    desk.forEach(f => pc.appendChild(fileRow(f.name, kb(f.bytes),
      `<button class="btn small" data-push="${esc(f.name)}">⬆ Push</button>
       <button class="btn small" data-exppc="${esc(f.name)}">🖥 Export</button>
       <button class="btn small danger" data-delpc="${esc(f.name)}">🗑</button>`)));
    if (!desk.length) pc.innerHTML = '<div class="mono">(empty — drop files in the Desktop folder to share them)</div>';
    const dev2 = $('#shared-dev'); dev2.innerHTML = '';
    (s.deviceFolder || []).forEach(f => dev2.appendChild(fileRow(f.name, kb(f.bytes),
      `<button class="btn small" data-expdev="${esc(f.name)}">🖥 Export</button>
       <button class="btn small danger" data-deldev="${esc(f.name)}">🗑</button>`)));
    if (!(s.deviceFolder || []).length) dev2.innerHTML = '<div class="mono">(empty — Pull device to fetch device files here)</div>';
    const st = await api('GET', '/api/shared/autosync');
    const j = (st.jobs || {})[dev()];
    $('#sync-state').textContent = j
      ? `auto-sync every ${j.interval}s · ${j.waiting ? '⏳ ' : ''}${j.last_result || ''}`
      : (autosyncOn ? 'auto-sync on · pick a device' : 'auto-sync idle');
  } catch (e) { /* backend offline — pill already shows it */ }
}
async function deleteBoth(name) {
  const s = dev();
  if (!confirm(`Permanently delete "${name}" from EVERYWHERE (Desktop folder, Device folder${s ? ' and device' : ''})?`)) return;
  const d = await safeApi('POST', '/api/shared/delete', {serial: s, files: [name]});
  if (d.ok) {
    const dd = d.deleted_device?.length ? 'device ✓' : (d.device_note || 'device: n/a');
    okT(`Deleted "${name}" — desktop folders ✓, ${dd}. It will NOT come back on sync.`, 'Trash');
  } else errT('Delete failed: ' + (d.err || d.device_note || ''));
  loadShared();
}
$('#shared-pc').onclick = async e => {
  const t = e.target?.dataset || {};
  if (t.push) {
    if (!needDev()) return;
    const d = await safeApi('POST', '/api/shared/push', {serial: dev(), files: [t.push]});
    if (d.ok) { okT(`Pushed ${t.push} to device.`); loadShared(); } else errT('Push failed: ' + (d.err || ''));
  } else if (t.exppc) {
    const d = await safeApi('POST', '/api/shared/export', {source: 'pc', files: [t.exppc]});
    if (d.ok) { loadShared(); okT(`Exported "${t.exppc}" to your Desktop ✓\n${d.desktop}`, 'Export'); }
    else errT('Export failed: ' + (d.err || ''));
  } else if (t.delpc) { deleteBoth(t.delpc); }
};
$('#shared-dev').onclick = async e => {
  const t = e.target?.dataset || {};
  if (t.expdev) {
    if (!needDev()) return;
    const d = await safeApi('POST', '/api/shared/export', {source: 'device', serial: dev(), files: [t.expdev]});
    if (d.ok) { loadShared(); okT(`Exported "${t.expdev}" to your Desktop ✓\n${d.desktop}`, 'Export'); }
    else errT('Export failed: ' + (d.err || ''));
  } else if (t.deldev) { deleteBoth(t.deldev); }
};
$('#btn-reveal').onclick = async () => {
  const d = await safeApi('POST', '/api/shared/reveal', {});
  // GUI reveal is disabled by default on v5 unless started with --allow-gui.
  if (!d.ok && d.err) errT(d.err, 'Open in Explorer');
};
$('#btn-shared-refresh').onclick = async () => { await loadDevices(); await loadShared(); };
$('#btn-syncnow').onclick = async () => {
  if (!needDev()) return;
  $('#sync-state').textContent = '⇄ syncing…';
  const d = await safeApi('POST', '/api/shared/sync', {serial: dev()});
  d.ok ? okT('Synced ✓ — Desktop pushed up, Device folder mirrors down.', 'Sync') : errT('Sync failed: ' + (d.err || d.result || ''));
  loadShared();
};
$('#btn-exact').onclick = async () => {
  if (!needDev()) return;
  if (!confirm('Make the DEVICE exactly match the Desktop folder? Files on the device that are not in Desktop/ will be DELETED.')) return;
  const d = await safeApi('POST', '/api/shared/mirror', {serial: dev(), direction: 'push'});
  const changes = [...(d.deleted_from_device || []), ...(d.copied_to_device || [])];
  d.ok ? okT(`Device now matches Desktop ✓\n${changes.length ? changes.join('\n') : '(already identical)'}`, 'Exact mirror')
       : errT('Failed: ' + (d.err || ''));
  loadShared();
};
$('#btn-push').onclick = async () => {
  if (!needDev()) return;
  const d = await safeApi('POST', '/api/shared/push', {serial: dev()});
  d.ok ? okT(`Desktop pushed to device ✓ (${(d.pushed || []).length} files)`, 'Push') : errT('Push failed: ' + (d.err || ''));
  loadShared();
};
$('#btn-pull').onclick = async () => {
  if (!needDev()) return;
  const d = await safeApi('POST', '/api/shared/pull', {serial: dev()});
  d.ok ? okT(`Device folder refreshed ✓ (${(d.pulled || []).length} new, ${(d.skipped || []).length} already had)`, 'Pull') : errT('Pull failed: ' + (d.err || ''));
  loadShared();
};
$('#btn-export-pc').onclick = async () => {
  const s = await api('GET', '/api/shared');
  const names = (s.desktop || []).map(f => f.name);
  if (!names.length) return infoT('Nothing staged in the Desktop folder to export.');
  const d = await safeApi('POST', '/api/shared/export', {source: 'pc', files: names});
  d.ok ? okT(`Exported ${d.copied.length} file(s) to your Desktop ✓\n${d.desktop}`, 'Export')
       : errT('Export failed: ' + (d.err || ''));
};
$('#btn-export-dev').onclick = async () => {
  if (!needDev()) return;
  const s = await api('GET', '/api/shared');
  const names = (s.deviceFolder || []).map(f => f.name);
  if (!names.length) return infoT('No device files pulled yet — run Pull device first.');
  const d = await safeApi('POST', '/api/shared/export', {source: 'device', serial: dev(), files: names});
  d.ok ? okT(`Exported ${d.copied.length} file(s) to your Desktop ✓\n${d.desktop}`, 'Export')
       : errT('Export failed: ' + (d.err || ''));
};
$('#btn-autosync').onclick = async e => {
  const s = dev();
  if (!autosyncOn && !s) { errT('Pick a device first — auto-sync needs a sync partner.'); return; }
  const want = !autosyncOn;
  const d = await safeApi('POST', '/api/shared/autosync', {serial: s, on: want, interval: 15});
  if (!d.ok) { errT(d.err || 'failed'); return; }
  autosyncOn = want;
  e.currentTarget.textContent = autosyncOn ? '🔁 Auto-sync ON' : '🔁 Auto-sync off';
  e.currentTarget.classList.toggle('on', autosyncOn);
  loadShared();
};
$('#btn-upload').onclick = async () => {
  const f = $('#up-file').files[0]; if (!f) return errT('Choose a file first');
  const b64 = await new Promise(res => {
    const r = new FileReader(); r.onload = () => res(r.result.split(',')[1]); r.readAsDataURL(f);
  });
  const d = await safeApi('POST', '/api/shared/upload', {name: f.name, b64, push_serial: dev()});
  if (!d.ok) { errT('Upload failed: ' + (d.err || '')); return; }
  okT(`Saved ${d.file} (${kb(d.bytes)})${d.pushed ? ' + pushed to device ✓' : ''}`, 'Upload');
  $('#up-file').value = '';
  loadShared();
};
$('#btn-analyze').onclick = async () => {
  const p = $('#apk-check').value.trim(); if (!p) return errT('Enter an APK path');
  const d = await safeApi('POST', '/api/apk/analyze', {apk_path: p, serial: dev()});
  if (!d.ok) { $('#apk-out').textContent = 'ERR: ' + d.err; return; }
  $('#apk').value = p;
  $('#apk-out').textContent =
    `${d.apk.label || d.apk.package} ${d.apk.versionName || ''}\npackage ${d.apk.package}\n` +
    `minSdk ${d.apk.minSdk} · target ${d.apk.targetSdk} · native ${d.apk.abis?.length ? d.apk.abis.join(', ') : 'none (pure Java)'}\n` +
    `device: ${d.device.model || d.device.note || d.device.serial} (API ${d.device.sdk || '?'}, bridge ${d.device.bridge || 'n/a'})\n\n` +
    `VERDICT [${d.level}]: ${d.reason}` +
    (d.level !== 'BLOCKED' ? '\n\n→ Press Install in App & World Ops to sideload it now.' : '');
};

/* ================== ENGINE & COMPATIBILITY (v5.0.0) ===================== */
/* Renders GET /api/engine. Every API-derived string is passed through esc()
   before touching innerHTML. Safe against a missing / older backend because
   loadEngine goes through safeApi and bails on a non-ok response. */
function ndKv(k, v) {
  return '<div class="nd-kv-item"><span class="nd-kv-k">' + esc(k) +
         '</span><span class="nd-kv-v">' + v + '</span></div>';
}
function stageFlags(flags, note, title) {
  const f = (flags || []).join(' ');
  if (!f) { infoT(note || 'No suggested flags available.'); return; }
  const el = $('#raw-flags'); if (el) el.value = f;
  okT((note ? note + '\n' : '') + 'Flags staged in Advanced launch.', title);
}

function renderEngine(d) {
  const box = $('#engine-out'); if (!box) return;
  const e = d.engine || {};
  const avds = Array.isArray(d.avds) ? d.avds : [];
  const flags = Array.isArray(d.flags) ? d.flags : [];
  const chan = e.channel || 'unknown';
  const chanCls = chan === 'current' ? 'ok' : (chan === 'outdated' ? 'warn' : '');
  const acc = e.accel || {};
  let html = '';

  html += '<div class="nd-engine-head">' +
    '<span class="nd-badge ' + chanCls + '">' + esc(chan) + '</span>' +
    '<span class="nd-pill-ver">emulator v' + esc(e.emulator_version || '?') + '</span></div>';

  html += '<div class="nd-kv">';
  html += ndKv('Emulator', esc(e.emulator_version || '?'));
  html += ndKv('Latest stable', esc(e.latest_stable || '?'));
  if (e.latest_patch) html += ndKv('Latest patch', esc(e.latest_patch));
  html += ndKv('Channel', '<span class="nd-badge ' + chanCls + '">' + esc(chan) + '</span>');
  html += ndKv('Acceleration', esc(acc.accel || 'unknown') + (acc.ready ? ' · ready' : ''));
  html += '</div>';
  if (acc.detail) html += '<div class="nd-muted">' + esc(acc.detail) + '</div>';
  if (Array.isArray(acc.features_enabled) && acc.features_enabled.length)
    html += '<div class="nd-muted">Windows features: ' +
            acc.features_enabled.map(x => esc(x)).join(', ') + '</div>';

  // A warning must be actionable: give the exact command instead of a vague
  // "use SDK Manager". Purely informational — NovaDroid never mutates the SDK
  // over HTTP (that would be a privileged write endpoint).
  const upd = e.update;
  if (upd && upd.available) {
    html += '<div class="nd-callout warn"><b>Update available</b> — ' +
      esc(upd.installed || '?') + ' → ' + esc(upd.target || 'latest') + '. ' +
      '<code class="nd-code">' + esc(upd.command || '') + '</code>' +
      '<button id="btn-engine-update" class="btn small">⧉ Copy command</button>' +
      (upd.note ? '<div class="nd-muted">' + esc(upd.note) + '</div>' : '') +
      '</div>';
  }

  const aehd = e.aehd;
  if (aehd && aehd.deprecated) {
    html += '<div class="nd-callout warn"><b>AEHD sunset</b> — ' +
      esc(aehd.sunset || '') +
      (aehd.days_left != null ? ' · ' + esc(aehd.days_left) + ' day(s) left' : '') +
      '. Migrate to WHPX acceleration.</div>';
  }

  const checks = Array.isArray(e.checks) ? e.checks : [];
  if (checks.length) {
    html += '<h3>Compatibility checks</h3><div class="nd-checks">';
    checks.forEach(c => {
      html += '<div class="nd-check ' + (c.ok ? 'ok' : 'bad') + '">' +
        '<span class="nd-mark">' + (c.ok ? '✓' : '!') + '</span>' +
        '<span class="nd-check-label">' + esc(c.label || c.id || '') + '</span>' +
        (c.detail ? '<span class="nd-muted">' + esc(c.detail) + '</span>' : '') +
        '</div>';
    });
    html += '</div>';
  }

  const notes = Array.isArray(e.notes) ? e.notes : [];
  notes.forEach(n => {
    // The structured `update` callout already shows this one — with a copy
    // button — so don't print the same warning twice.
    if (upd && upd.available && n.command) return;
    const lvl = n.level === 'good' ? 'good' : (n.level === 'warn' ? 'warn' : 'info');
    html += '<div class="nd-callout ' + lvl + '">' +
      (n.title ? '<b>' + esc(n.title) + '</b> ' : '') + esc(n.detail || '') + '</div>';
  });

  html += '<h3>Per-AVD readiness <span class="sub">4 GB / 4-core floor for API 37</span></h3>';
  if (!avds.length) {
    html += '<div class="nd-empty">No AVDs reported by the engine check.</div>';
  } else {
    html += '<div class="nd-table-wrap"><table class="nd-table"><thead><tr>' +
      '<th>AVD</th><th>API</th><th>RAM</th><th>Cores</th><th>GPU</th><th>Vulkan / snapshot</th>' +
      '</tr></thead><tbody>';
    avds.forEach(a => {
      const ramOk = !!a.ram_ok, coresOk = !!a.cores_ok;
      html += '<tr>' +
        '<td>' + esc(a.name || '?') + '</td>' +
        '<td>' + esc(a.api == null ? '?' : a.api) + '</td>' +
        '<td class="' + (ramOk ? 'nd-ok' : 'nd-bad') + '">' +
          esc(a.ram_mb == null ? '?' : a.ram_mb) + ' MB ' + (ramOk ? '✓' : '!') + '</td>' +
        '<td class="' + (coresOk ? 'nd-ok' : 'nd-bad') + '">' +
          esc(a.cores == null ? '?' : a.cores) + (coresOk ? ' ✓' : ' !') + '</td>' +
        '<td>' + esc(a.gpu_mode || '?') + '</td>' +
        '<td>' + (a.vulkan_snapshot_risk
            ? '<span class="nd-badge warn">risk</span>' : '<span class="nd-ok">ok</span>') + '</td>' +
        '</tr>';
    });
    html += '</tbody></table></div>';
  }

  html += '<div class="nd-flags"><span class="nd-flags-label">Recommended launch flags</span>' +
    '<code>' + (flags.length ? flags.map(f => esc(f)).join(' ') : '(none)') + '</code>' +
    '<button id="btn-engine-flags" class="btn small">⤴ Use in raw boot</button></div>';

  box.innerHTML = html;
  const fb = $('#btn-engine-flags');
  if (fb) fb.onclick = () => stageFlags(flags, '', 'Engine flags');
  const ub = $('#btn-engine-update');
  if (ub) ub.onclick = async () => {
    const cmd = (upd && upd.command) || '';
    if (await copyText(cmd)) okT(cmd, 'Command copied');
    else if (cmd) toast('Copy unavailable — run this manually:\n' + cmd,
                        'info', 'Engine update', 9000);
  };
}

async function loadEngine(manual) {
  const d = await safeApi('GET', '/api/engine');
  if (!d || !d.ok || !d.engine) {
    if (manual) {
      const out = $('#engine-out');
      if (out) out.innerHTML = '<div class="nd-empty">Engine endpoint unavailable — the agent may be older than v5.0.0 or offline.</div>';
      errT('Engine check failed: ' + ((d && d.err) || 'no data'), 'Engine');
    }
    return;   // boot-time / old backend: leave the card in its empty state
  }
  renderEngine(d);
  const ch = d.engine.channel || 'unknown';
  ribbon.channel = ch;
  ribbon.channelCls = ch === 'current' ? 'ok' : (ch === 'outdated' ? 'warn' : '');
  paintRibbon();
  const up = $('#engine-updated');
  if (up) up.textContent = 'checked ' + new Date().toLocaleTimeString();
}
$('#btn-engine').onclick = async () => {
  const out = $('#engine-out');
  if (out) out.innerHTML = '<div class="nd-empty">Running engine check…</div>';
  await loadEngine(true);
};

/* ======================= SMOOTHNESS ENGINE (v3.0.0) ====================== */
function scoreClass(v) { return v >= 85 ? '' : (v >= 60 ? 'warn' : 'bad'); }

function paintSmooth(d) {
  const s = d.sample || {}, sc = d.score;
  const el = $('#smooth-score');
  el.textContent = (sc == null ? '–' : sc);
  el.className = 'score ' + scoreClass(sc == null ? 100 : sc);
  $('#smooth-verdict').textContent = d.verdict || 'not measured';
  $('#smooth-renderer').textContent =
    `renderer: ${s.renderer || '?'} · janky ${s.janky_pct ?? '?'}% · ` +
    `p95 ${s.p95_ms ?? '?'}ms · ${s.frames ?? '?'} frames` +
    (s.refresh_hz ? ` · ${s.refresh_hz}Hz` : '');
  $('#smooth-out').textContent = d.summary || JSON.stringify(d, null, 1);

  const box = $('#bottlenecks'); box.innerHTML = '';
  const list = d.bottlenecks || [];
  if (!list.length) {
    box.innerHTML = '<div class="bl">✓ No bottleneck detected — host, guest and ' +
      'render path all have headroom.</div>';
  } else {
    list.forEach(b => {
      const el2 = document.createElement('div');
      el2.className = 'bl sev-' + (b.severity || 'medium');
      el2.innerHTML = `<div class="bt">${esc(b.what)}</div>
        <div class="bw">${esc(b.why)}</div><div class="bf">→ ${esc(b.fix)}</div>`;
      box.appendChild(el2);
    });
  }
}

async function diagnose() {
  if (!needDev()) return;
  $('#smooth-out').textContent = 'measuring…';
  const d = await safeApi('GET', '/api/smoothness/diagnose?serial=' +
    encodeURIComponent(dev()));
  if (d.ok) paintSmooth(d);
  else $('#smooth-out').textContent = 'Diagnose failed: ' + (d.err || '?');
  paintHistory();
}

async function paintHistory() {
  const h = await api('GET', '/api/smoothness/history?limit=40');
  const box = $('#smooth-hist'); box.innerHTML = '';
  const items = h.samples || [];
  if (!items.length) return;
  items.forEach(x => {
    const i = document.createElement('i');
    const v = x.score == null ? 0 : x.score;
    i.style.height = Math.max(2, Math.round(v * 0.42)) + 'px';
    i.className = scoreClass(v);
    i.title = `${v} — janky ${x.janky_pct ?? '?'}% · ${x.renderer || '?'}`;
    box.appendChild(i);
  });
}

$('#btn-smooth').onclick = diagnose;
$('#btn-smooth-flags').onclick = async () => {
  const d = await safeApi('GET', '/api/smoothness/flags?serial=' + encodeURIComponent(dev()));
  if (!d || !d.ok) { errT('Suggest flags failed: ' + ((d && d.err) || 'no data')); return; }
  stageFlags(d.flags, d.note, 'Smoothness flags');
};
$('#btn-tune').onclick = async () => {
  const name = prompt('Check tuning recommendations for which AVD?', 'medium_phone');
  if (!name) return;
  const d = await safeApi('GET', '/api/tune?name=' + encodeURIComponent(name));
  if (!d.ok) return errT('Failed: ' + (d.err || '?'));
  if (!d.count) return okT('Already optimal — nothing to change.', 'Tune ' + name);
  alert(d.recommendations.map(r =>
    `${r.key}: ${r.current} → ${r.suggested}\n   ${r.why}`).join('\n\n'));
};
$('#btn-tune-apply').onclick = async () => {
  const name = prompt('Apply tuning to which AVD? (stop it first)', 'medium_phone');
  if (!name) return;
  if (!confirm(`Write the recommended config into ${name}? Stop the AVD first — ` +
               'the emulator rewrites config.ini on exit.')) return;
  const d = await safeApi('POST', '/api/tune/apply', {name});
  d.ok ? okT(`Applied ${d.count} change(s) to ${name}:\n` +
         (d.applied || []).map(r => `• ${r.key}: ${r.current} → ${r.suggested}`).join('\n'), 'Tune applied')
       : errT('Failed: ' + (d.err || '?'));
};
$('#smooth-auto').onchange = e => {
  clearInterval(smoothTimer); smoothTimer = null;
  if (e.currentTarget.checked) {
    diagnose();
    smoothTimer = setInterval(() => { if (dev()) diagnose(); }, 10000);
  }
};

/* ========================= AI ASSISTANT (v3.0.0) ========================= */
async function runAiCommand(text) {
  if (!text.trim()) return;
  const needs = !/^(ai status|optimise|optimize|tune)/i.test(text);
  if (needs && !dev()) return;
  $('#ai-out').textContent = 'running…';
  const d = await safeApi('POST', '/api/ai/command', {serial: dev(), text});
  if (d.err && !d.intent) { $('#ai-out').textContent = '✗ ' + d.err; return; }
  const lines = [
    `» ${d.text}`,
    `intent  ${d.intent}  (confidence ${d.confidence})`,
    `${d.ok && d.ran ? '✓' : '✗'} ${d.reply}`,
  ];
  if (d.missing && d.missing.length) {
    lines.push(`missing: ${d.missing.join(', ')} — add them and try again.`);
  }
  if (d.result) {
    const r = {...d.result};
    // A screenshot blob is useless as text; say what happened instead.
    if (r.png_b64) r.png_b64 = `<${r.bytes} bytes PNG>`;
    lines.push('', JSON.stringify(r, null, 1).slice(0, 2500));
  }
  $('#ai-out').textContent = lines.join('\n');
  if (/rotate|battery|gps|tap|swipe|launch|type/i.test(d.intent || '')) scheduleLive(400);
}
$('#btn-ai-run').onclick = () => runAiCommand($('#ai-cmd').value);
$('#ai-cmd').addEventListener('keydown', e => {
  if (e.key === 'Enter') $('#btn-ai-run').click();
});
document.querySelectorAll('.aiq').forEach(b => b.onclick = () => {
  $('#ai-cmd').value = b.dataset.q;
  $('#btn-ai-run').click();
});

$('#btn-ai-status').onclick = async () => {
  $('#ai-status-out').textContent = 'probing AICore / NNAPI / ML runtimes…';
  const d = await safeApi('GET', '/api/ai?serial=' + encodeURIComponent(dev()));
  if (!d.ok) {
    $('#ai-status-out').textContent = 'Failed: ' + (d.err || '?');
    return;
  }
  $('#ai-level').textContent = d.level;
  $('#ai-level').className = 'pill ' +
    (d.level === 'ready' ? 'ok' : (d.level === 'partial' ? '' : 'bad'));
  const layers = (d.layers || []).map(l =>
    `${l.ok ? '✓' : '✗'} ${l.layer}\n    ${l.detail}`).join('\n');
  $('#ai-status-out').textContent =
    `${d.headline}\n\n${layers}\n\n` +
    (d.guidance || []).map(g => '• ' + g).join('\n');
};

/* ============================== AUTOMATION =============================== */
document.querySelectorAll('[data-macro]').forEach(b => b.onclick = () => {
  macroSteps.push(JSON.parse(b.dataset.macro));
  $('#macro-steps').textContent = JSON.stringify(macroSteps, null, 1);
});
$('#btn-macro-clear').onclick = () => { macroSteps = []; $('#macro-steps').textContent = '[]'; };
$('#btn-macro-save').onclick = async () => {
  const name = $('#macro-name').value.trim() || 'macro';
  if (!macroSteps.length) macroSteps = [{type: 'tap', x: 540, y: 1600}, {type: 'sleep', s: 1},
    {type: 'swipe', x1: 540, y1: 1400, x2: 540, y2: 400, ms: 400}];
  const d = await safeApi('POST', '/api/macros', {name, steps: macroSteps});
  d.ok ? okT('Saved ' + d.file + (d.rejected?.length ? ` (skipped ${d.rejected.length} bad step(s))` : ''), 'Macro')
       : errT('save failed');
  loadMacros();
};
$('#btn-macro-run').onclick = async () => {
  if (!needDev()) return;
  const loops = Math.max(1, Math.min(+$('#macro-loops').value || 1, 500));
  const d = await safeApi('POST', '/api/macros/run',
    {serial: dev(), name: $('#macro-name').value.trim() || 'macro',
     loops, loop_delay_s: +$('#macro-delay').value || 0});
  d.ok ? okT((d.log || []).slice(-8).join('\n'), `Macro ${d.macro} ×${d.loops}`) : errT(d.err);
  scheduleLive(400);
};
$('#btn-macro-del').onclick = async () => {
  const name = $('#macro-name').value.trim();
  if (!name || !confirm(`Delete macro "${name}"?`)) return;
  await fetch(API_BASE() + '/api/macros?name=' + encodeURIComponent(name),
    {method: 'DELETE', headers: apiHdrs()});
  loadMacros();
};
async function loadMacros() {
  const d = await api('GET', '/api/macros');
  $('#macros').textContent = 'saved: ' + (d.macros || []).map(m => `${m.name}(${m.steps})`).join(', ');
}

/* ---- multi-instance synchronizer: tap / swipe / text / key ---- */
$('#btn-sync').onclick = async () => {
  const d = await api('GET', '/api/devices');
  const online = (d.devices || []).filter(x => x.state === 'device');
  if (!online.length) return errT('No online devices');
  const action = $('#sync-action').value;
  const base = {action};
  if (action === 'tap') { base.x = +$('#sync-x').value; base.y = +$('#sync-y').value; }
  else if (action === 'swipe') {
    base.x1 = +$('#sync-x').value; base.y1 = +$('#sync-y').value;
    base.x2 = +$('#sync-x2').value || +$('#sync-x').value;
    base.y2 = +$('#sync-y2').value || +$('#sync-y').value;
  } else if (action === 'text') { base.text = $('#sync-text').value; if (!base.text) return errT('Enter text to mirror'); }
  else { base.code = parseInt($('#sync-text').value, 10); if (isNaN(base.code)) return errT('Enter a keycode (e.g. 3)'); }
  const inputs = online.map(x => ({serial: x.serial, ...base}));
  const r = await safeApi('POST', '/api/sync-input', {inputs});
  $('#sync-out').textContent = JSON.stringify(r.results || r, null, 1);
  r.ok ? okT(`${action} mirrored to ${r.count} device(s) in parallel.`, 'Synchronizer')
       : errT('Some targets failed — see output.');
};

/* ======================= AVD TOOLS (v4.0.0) ============================== */
function toolName() {
  const n = $('#avdtool-name').value.trim() || $('#raw-name').value.trim();
  if (!n) { errT('Enter an AVD name (or pick one from the Virtual Devices list).'); return ''; }
  return n;
}
$('#btn-disk').onclick = async () => {
  const name = toolName(); if (!name) return;
  const d = await safeApi('GET', '/api/avds/disk?name=' + encodeURIComponent(name));
  if (!d.ok) { $('#disk-out').textContent = 'ERR: ' + d.err; return; }
  $('#disk-out').textContent =
    `${name}: ${d.mb} MB total on disk\n` +
    Object.entries(d.breakdown_mb || {}).map(([k, v]) =>
      `  ${k.padEnd(10)} ${String(v).padStart(9)} MB`).join('\n') +
    `\n  reclaimable (snapshots + cache): ${d.reclaimable_mb} MB`;
  $('#avdtool-name').value = name;
};
$('#btn-cleanup').onclick = async () => {
  const name = toolName(); if (!name) return;
  if (!confirm(`Reclaim disk for ${name}?\n\nDeletes quickboot snapshots + cache images (userdata, apps and their data are KEPT). Next boot is a cold boot.`)) return;
  const d = await safeApi('POST', '/api/avds/cleanup', {name, serial: dev()});
  d.ok ? okT(`Freed ${d.freed_mb} MB${d.guest_trimmed ? ' + guest storage trimmed' : ''}. ${d.note}`, 'Reclaim')
       : errT('Failed: ' + d.err);
};
$('#btn-backup').onclick = async () => {
  const name = toolName(); if (!name) return;
  infoT('Backing up ' + name + '… (zip, can take a minute)');
  const d = await safeApi('POST', '/api/avds/backup', {name});
  d.ok ? okT(`${d.file} (${kb(d.bytes / 1024)})\n${d.note}`, 'Backup ✓')
       : errT('Backup failed: ' + d.err);
};
$('#btn-restore').onclick = async () => {
  const zip = $('#restore-zip').value.trim();
  if (!zip) return errT('Paste the path to a backup zip (NovaDroid-Shared\\Backups\\…).');
  const name = $('#restore-name').value.trim();
  if (!confirm(`Restore ${name || 'the backup'} from\n${zip}?`)) return;
  const d = await safeApi('POST', '/api/avds/restore', {zip, name});
  d.ok ? okT(d.note, 'Restore ✓') : errT('Restore failed: ' + d.err);
  if (d.ok) { loadAvds(); $('#avdtool-name').value = d.avd; }
};
$('#btn-rename').onclick = async () => {
  const src = toolName(); if (!src) return;
  const dst = prompt(`Rename ${src} to…?`, src + '_new');
  if (!dst) return;
  const d = await safeApi('POST', '/api/avds/rename', {src, dst: dst.trim()});
  d.ok ? okT(`${src} → ${d.name}`, 'Rename') : errT('Rename failed: ' + d.err);
  if (d.ok) { loadAvds(); $('#avdtool-name').value = d.name; }
};
$('#btn-root').onclick = async () => {
  if (!needDev()) return;
  const d = await safeApi('POST', '/api/avds/root', {serial: dev()});
  d.ok ? okT(`adbd switched (${d.was} → ${d.requested} requested). ${d.log || ''}`, 'Root')
       : errT((d.note || 'Root refused.') + ' ' + (d.log || ''));
};

/* =================== PORTS + POWER OPS (v4.0.0) ========================== */
async function loadPorts() {
  if (!needDev()) return;
  const [f, r] = await Promise.all([
    safeApi('GET', '/api/ports?serial=' + encodeURIComponent(dev())),
    safeApi('GET', '/api/ports/reverse?serial=' + encodeURIComponent(dev())),
  ]);
  const box = $('#ports-list'); box.innerHTML = '';
  const rows = [...(f.rules || []).map(x => ({...x, direction: 'forward'})),
                ...(r.rules || []).map(x => ({...x, direction: 'reverse'}))];
  if (!rows.length) {
    box.innerHTML = '<div class="mono">No forward/reverse rules on this device.</div>';
    return;
  }
  rows.forEach(x => {
    const el = document.createElement('div'); el.className = 'item';
    el.innerHTML = `<div class="grow"><div class="name">${x.direction === 'forward' ? '→' : '←'} ${esc(x.local)} ⇄ ${esc(x.remote)}</div>
      <div class="meta">${esc(x.direction)} · ${esc(x.serial)}</div></div>
      <button class="btn small danger" data-dir="${esc(x.direction)}" data-local="${esc(x.local)}">✕</button>`;
    el.querySelector('button').onclick = async () => {
      // forward removes by the PC-side port; reverse removes by the
      // DEVICE-side port (adb reverse --remove takes the remote spec).
      const portOf = s => parseInt((String(s).split(':')[1] || '0'), 10);
      const body = {serial: dev(), direction: x.direction};
      if (x.direction === 'forward') body.local = portOf(x.local);
      else body.remote = portOf(x.remote);
      const d = await safeApi('POST', '/api/ports/remove', body);
      d.ok ? okT(`Removed ${x.direction} rule ${x.local} ⇄ ${x.remote}`) : errT(d.err || 'remove failed');
      loadPorts();
    };
    box.appendChild(el);
  });
}
$('#btn-ports-refresh').onclick = loadPorts;
$('#btn-fwd-add').onclick = async () => {
  if (!needDev()) return;
  const d = await safeApi('POST', '/api/ports/add',
    {serial: dev(), direction: 'forward', local: +$('#port-local').value,
     remote: +$('#port-remote').value, abstract: $('#port-abstract').value.trim()});
  d.ok ? okT(`${d.local} → ${d.remote} on ${dev()}`, 'Forward added') : errT(d.err || 'failed');
  loadPorts();
};
$('#btn-rev-add').onclick = async () => {
  if (!needDev()) return;
  const d = await safeApi('POST', '/api/ports/add',
    {serial: dev(), direction: 'reverse', local: +$('#port-local').value,
     remote: +$('#port-remote').value});
  d.ok ? okT(`${d.remote} reaches this PC via ${d.local}`, 'Reverse added') : errT(d.err || 'failed');
  loadPorts();
};
$('#btn-ports-clear').onclick = async () => {
  if (!needDev()) return;
  if (!confirm('Remove ALL forward rules for this device?')) return;
  const d = await safeApi('POST', '/api/ports/remove', {serial: dev(), direction: 'forward', all: true});
  d.ok ? okT('All forwards removed.') : errT(d.err || 'failed');
  loadPorts();
};

/* =================== v5.1: scrcpy · GPU pref · agent skill ================ */
$('#btn-scrcpy').onclick = async () => {
  if (!needDev()) return;
  const st = await safeApi('GET', '/api/scrcpy');
  if (!st.available) return errT(st.note || 'scrcpy not found on this PC.');
  const d = await safeApi('POST', '/api/scrcpy/mirror', {serial: dev(), max_fps: 60});
  d.ok ? okT(`scrcpy mirror opened (pid ${d.pid}). Close the window like any app.`, 'scrcpy')
       : errT(d.err || 'scrcpy failed to launch');
};
$('#btn-gpu-apply').onclick = async () => {
  const mode = $('#gpu-mode').value;
  if (!mode) return errT('Pick a GPU mode first (auto / power / performance).');
  const d = await safeApi('POST', '/api/gpu', {mode});
  d.ok ? okT(d.note || `GPU preference: ${mode}.`, 'GPU preference') : errT(d.err || 'GPU preference failed');
  loadGpuState();
};
async function loadGpuState() {
  const d = await safeApi('GET', '/api/gpu');
  const el = $('#gpu-state');
  if (!el) return;
  if (!d.ok) { el.textContent = 'n/a'; return; }
  const e0 = (d.entries || [])[0];
  el.textContent = e0 ? `emulator: ${e0.mode}` : '';
}
$('#btn-agent-skill').onclick = async () => {
  try {
    // /api/agent-skill returns markdown, not JSON — raw fetch with auth.
    const r = await fetch(API_BASE() + '/api/agent-skill', {headers: apiHdrs()});
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const text = await r.text();
    const blob = new Blob([text], {type: 'text/markdown'});
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'novadroid-agent-skill.md';
    a.click();
    URL.revokeObjectURL(a.href);
    okT('Skill file downloaded — paste its contents into Claude/Codex or save it as a project skill. Give the agent the backend URL + token from the 🔌 bar.', 'Agent skill');
  } catch (e) {
    errT('Could not fetch the agent skill file: ' + e.message);
  }
};

$('#btn-open-url').onclick = async () => {
  if (!needDev()) return;
  const url = $('#url-input').value.trim();
  if (!url) return errT('Enter a URL (https://… , myapp://… , market://…).');
  const d = await safeApi('POST', '/api/open-url', {serial: dev(), url});
  d.ok ? okT('Opened on device: ' + url, 'Deep link') : errT('Open failed: ' + (d.err || d.log || ''));
};
$('#url-input').addEventListener('keydown', e => { if (e.key === 'Enter') $('#btn-open-url').click(); });
$('#btn-proxy').onclick = async () => {
  if (!needDev()) return;
  const d = await safeApi('POST', '/api/proxy', {serial: dev(), proxy: $('#proxy-input').value.trim()});
  d.ok ? okT(`Proxy ${d.proxy} set (verified: ${d.verified}).`, 'Proxy') : errT('Proxy failed: ' + (d.err || ''));
};
$('#btn-proxy-clear').onclick = async () => {
  if (!needDev()) return;
  const d = await safeApi('POST', '/api/proxy/clear', {serial: dev()});
  d.ok ? okT('Proxy cleared.') : errT('Clear failed: ' + (d.err || ''));
};
$('#btn-eco-on').onclick = async () => {
  if (!needDev()) return;
  const d = await safeApi('POST', '/api/eco', {serial: dev(), on: true});
  d.ok ? okT('Eco on — animations off, caches trimmed.', 'Eco mode') : errT('Eco failed: ' + (d.err || ''));
};
$('#btn-eco-off').onclick = async () => {
  if (!needDev()) return;
  const d = await safeApi('POST', '/api/eco', {serial: dev(), on: false});
  d.ok ? okT('Eco off — animations restored.', 'Eco mode') : errT('Eco failed: ' + (d.err || ''));
};
$('#btn-tcpip').onclick = async () => {
  if (!needDev()) return;
  const d = await safeApi('POST', '/api/adb/tcpip', {serial: dev(), port: 5555});
  d.ok ? okT(d.note || 'ADB over TCP on.', 'Wireless ADB') : errT('tcpip failed: ' + (d.err || ''));
};

/* ======================== BACKEND CONNECTION ============================= */
function paintConn(state) {
  $('#conn-state').textContent = state;
  const remote = location.hostname !== 'localhost' && location.hostname !== '127.0.0.1';
  if (remote && !API_BASE()) $('#conn-state').textContent = '☁ Vercel mode: paste tunnel URL + token, Connect';
}
$('#btn-conn').onclick = async () => {
  localStorage.setItem('nd_api', $('#cfg-api').value.trim());
  localStorage.setItem('nd_token', $('#cfg-tok').value.trim());
  sessionTicket = null;                 // new credentials void any cached ticket
  paintSecure();
  try {
    const h = await api('GET', '/api/health');
    paintConn(h.ok ? `✓ connected → ${(h.app || 'agent')} v${h.version || '?'} ${h.remote ? '(token-secured)' : '(local)'}`
                   : '✗ backend error: ' + (h.err || 'unknown'));
    applyHealth(h, !!h.ok);
  } catch (e) {
    paintConn('✗ unreachable — is the agent + tunnel running?');
    ribbon.conn = '● offline'; ribbon.connCls = 'bad'; paintRibbon();
    return;
  }
  refreshAll();
};
$('#cfg-api').value = API_BASE(); $('#cfg-tok').value = API_TOK();
paintConn(API_BASE() ? 'saved backend → ' + API_BASE() : 'local mode (same origin)');

/* ================================ BOOT =================================== */
$('#btn-refresh').onclick = refreshAll;
$('#device').onchange = () => { loadApps(); loadShared(); loadSensors(); loadNetwork(); loadPorts(); };
paintSecure(); paintRibbon();
refreshAll();
setInterval(loadDevices, 15000);
setInterval(loadHost, 3000);
setInterval(() => { if (liveOn) loadAvds(); }, 10000);
/* keep the in-memory ticket fresh while a stream / screenshot loop is active */
setInterval(() => { if ((liveOn || streamOn) && API_TOK()) getTicket(true); }, 120000);

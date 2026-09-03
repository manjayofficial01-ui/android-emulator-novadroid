/* NovaDroid frontend — vanilla JS, offline-safe (no CDN).
   Split hosting: same-origin '' locally; set a tunnel URL + token in the
   backend bar when served from Vercel (persisted in localStorage). */
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
  if (r.status === 401) { d.err = 'unauthorized — paste the backend token in the 🔌 bar'; }
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

/* One active device drives every panel (the old build had three separate
   selects that silently reset each other on every 15s refresh). */
const dev = () => $('#device').value || '';
function needDev() {
  if (!dev()) { alert('No device selected — Quickboot an AVD, then pick it from the Device list.'); return false; }
  return true;
}

let macroSteps = [];
let liveOn = false, liveTimer = null, liveTarget = 2000;
let drag = null;
let logcatTimer = null;
let recTimer = null;

async function refreshAll() {
  await Promise.all([loadInfo(), loadProfiles(), loadAvds(), loadDevices(),
                     loadMacros(), loadDisplayMeta(), loadSensors(), loadNetwork()]);
  loadShared(); loadHost();
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
    alert(d.ok ? `Cloned → ${d.name}\n\n${d.note}` : 'Clone failed: ' + (d.err || '?'));
    loadAvds(); return;
  }
  if (act === 'winfix') {
    const d = await safeApi('POST', '/api/avds/window/fix', {name});
    alert((d.saved?.ok ? `Window position reset → (${d.saved.x}, ${d.saved.y}). ` : 'Reset failed. ') +
      (d.live?.ok ? 'Running window moved on screen. ' : 'Not running — takes effect on next boot. ') +
      (d.tip || ''));
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
    if (!d.ok) alert('Failed: ' + (d.err || 'unknown'));
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
  if (!names.length) return alert('Check at least one AVD first.');
  const d = await safeApi('POST', '/api/fleet', {names, action: 'start', stagger: 8});
  alert(d.ok ? `Started: ${d.note}` : 'Failed: ' + (d.err || '?'));
  loadAvds(); setTimeout(loadDevices, 4000);
};
$('#btn-fleet-stop').onclick = async () => {
  const names = checkedAvds();
  if (!names.length) return alert('Check at least one AVD first.');
  if (!confirm(`Stop ${names.length} AVD(s)?`)) return;
  const d = await safeApi('POST', '/api/fleet', {names, action: 'stop'});
  alert(d.ok ? `Stopped: ${d.note}` : 'Failed: ' + (d.err || '?'));
  loadAvds(); loadDevices();
};

$('#btn-create').onclick = async e => {
  e.currentTarget.disabled = true; e.currentTarget.textContent = 'Creating… (1–5 min)';
  const d = await safeApi('POST', '/api/avds', {profile: $('#profile').value});
  alert(d.ok ? 'Created: ' + d.name + ' (4 CPU / 4GB / gpu auto applied)'
             : 'Failed: ' + (d.out || d.err || 'unknown'));
  e.currentTarget.disabled = false; e.currentTarget.textContent = '＋ Create AVD';
  loadAvds();
};
$('#btn-raw').onclick = async () => {
  const d = await safeApi('POST', '/api/avds/start-raw',
    {name: $('#raw-name').value.trim(), extra: $('#raw-flags').value});
  alert(d.ok ? 'Launched: ' + d.cmd : 'Failed: ' + d.err);
  setTimeout(() => { loadAvds(); loadDevices(); }, 3000);
};

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
function screenUrl() {
  const u = API_BASE() + '/api/screen?serial=' + encodeURIComponent(dev()) + '&t=' + Date.now();
  // <img> cannot send an Authorization header, so the token rides in the
  // query string — otherwise the remote dashboard could never show a frame.
  return API_TOK() ? u + '&token=' + encodeURIComponent(API_TOK()) : u;
}

function loadScreen() {
  return new Promise(res => {
    const img = $('#screen');
    let done = false;
    const finish = () => { if (!done) { done = true; res(); } };
    img.onload = finish;
    img.onerror = finish;
    setTimeout(finish, 10000);      // never let one bad frame hang the loop
    img.src = screenUrl();
  });
}

async function shot() {
  if (!needDev()) return;
  $('#live-info').textContent = 'capture…';
  await loadScreen();
  if (!liveOn) $('#live-info').textContent = 'idle';
}

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
  if (!dev()) { alert('Quickboot an AVD first — live view needs a device.'); return; }
  liveOn = !liveOn;
  b.classList.toggle('on', liveOn);
  b.textContent = liveOn ? '⏸ Live' : '▶ Live';
  if (liveOn) { liveTarget = +$('#live-fps').value || 2000; tickLive(); }
  else { clearTimeout(liveTimer); $('#live-info').textContent = 'idle'; }
};
$('#live-fps').onchange = () => { liveTarget = +$('#live-fps').value || 2000; };

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
  if (!p) { alert('Screenshot first — the device resolution is read from the last frame.'); return; }
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
  $('#input-log').textContent = d.ok ? `sent ${t.length} chars` : ('failed: ' + d.err);
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

/* ============================== APP & WORLD ============================== */
$('#btn-apk').onclick = async () => {
  if (!needDev()) return;
  const p = $('#apk').value.trim();
  if (!p) return alert('Enter an APK path');
  const d = await safeApi('POST', '/api/apk/install', {serial: dev(), apk_path: p});
  alert(d.ok ? 'Installed via ' + d.tool : 'Install failed: ' + (d.log || d.err || '').slice(-400));
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
};
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
  const url = {launch: '/api/apps/launch', stop: '/api/apps/stop',
               clear: '/api/apps/clear', uninstall: '/api/apps/uninstall',
               grant: '/api/apps/grant'}[b.dataset.act];
  const d = await safeApi('POST', url, {serial: dev(), package: pkg});
  $('#input-log').textContent = `${b.dataset.act} ${pkg}: ${d.ok ? 'ok' : (d.err || 'failed')}`;
  if (b.dataset.act === 'uninstall') loadApps();
  scheduleLive(400);
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
  if (!vals.length) return alert('Enter at least one value');
  const d = await safeApi('POST', '/api/sensor',
    {serial: dev(), kind: $('#sensor-kind').value, values: vals});
  $('#input-log').textContent = d.ok ? `${$('#sensor-kind').value} = ${vals.join(':')}` : ('failed: ' + d.err);
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
  $('#input-log').textContent = d.ok ? `network ${$('#net-speed').value} / ${$('#net-delay').value}` : ('failed: ' + d.err);
};
$('#btn-finger').onclick = async () => {
  if (!needDev()) return;
  const d = await safeApi('POST', '/api/biometric', {serial: dev(), press: true});
  $('#input-log').textContent = d.ok ? 'fingerprint sent' : ('failed: ' + d.err);
};
$('#btn-power').onclick = async () => {
  if (!needDev()) return;
  await safeApi('POST', '/api/power', {serial: dev()});
  scheduleLive(700);
};
$('#btn-call').onclick = async () => {
  if (!needDev()) return;
  const d = await safeApi('POST', '/api/call', {serial: dev(), number: $('#tel-num').value});
  $('#input-log').textContent = d.ok ? `calling ${$('#tel-num').value}` : ('failed: ' + d.err);
};
$('#btn-sms').onclick = async () => {
  if (!needDev()) return;
  const d = await safeApi('POST', '/api/sms',
    {serial: dev(), number: $('#tel-num').value, text: $('#sms-text').value});
  $('#input-log').textContent = d.ok ? 'sms sent' : ('failed: ' + d.err);
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
  $('#input-log').textContent = d.ok ? `snapshot ${action} ok` : ('failed: ' + (d.err || d.log || ''));
}

$('#btn-clip-get').onclick = async () => {
  if (!needDev()) return;
  const d = await safeApi('GET', '/api/clipboard?serial=' + encodeURIComponent(dev()));
  if (!d.ok) { $('#input-log').textContent = d.err; return; }
  $('#clip').value = d.text || (d.note || '');
  $('#input-log').textContent = d.note || 'clipboard read';
};
$('#btn-clip-set').onclick = async () => {
  if (!needDev()) return;
  const text = $('#clip').value;
  const d = await safeApi('POST', '/api/clipboard', {serial: dev(), text});
  if (d.ok) { $('#input-log').textContent = 'clipboard sent'; return; }
  // Image without a clipboard console: fall back to typing into the field
  // that currently has focus on the device.
  if (confirm('This image has no clipboard console.\n\nType the text into the '
    + 'field that has focus on the device instead?')) {
    const r = await safeApi('POST', '/api/input/text', {serial: dev(), text});
    $('#input-log').textContent = r.ok ? `typed ${text.length} chars into focused field`
                                       : ('failed: ' + r.err);
    scheduleLive(500);
  } else $('#input-log').textContent = d.err;
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
  $('#rec-state').innerHTML = d.ok
    ? 'saved → ' + esc(d.file) + '<br><a class="dl" href="' + API_BASE() + '/api/record/download?file='
      + encodeURIComponent((d.file || '').split(/[\\/]/).pop())
      + (API_TOK() ? '&token=' + encodeURIComponent(API_TOK()) : '') + '">⬇ download mp4</a>'
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
        if (!dev()) return alert('Preset staged below — Quickboot a device, then Apply live.');
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
  alert(d.ok ? `Saved ${d.display} → ${d.avd} (stop it first if running)` : 'Failed: ' + d.err);
  loadAvds();
};
document.querySelectorAll('.ori').forEach(b => b.onclick = async () => {
  if (!needDev()) return;
  const d = await safeApi('POST', '/api/display/orientation', {serial: dev(), mode: b.dataset.mode});
  $('#disp-cur').textContent = JSON.stringify(d, null, 1);
  scheduleLive(900);
});
$('#btn-skin').onclick = async () => {
  const name = prompt('Apply frame to which AVD? (must be stopped)', 'medium_tablet'); if (!name) return;
  const d = await safeApi('POST', '/api/avds/skin', {name, skin: $('#skin').value});
  alert(d.ok ? `Frame ${d.skin} → ${d.avd}` : 'Failed: ' + d.err);
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
    const j = (st.jobs || {})[dev()];          // <- was `se` (undefined): threw
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
    alert(`Deleted "${name}" — desktop folders ✓, ${dd}. It will NOT come back on sync.`);
  } else alert('Delete failed: ' + (d.err || d.device_note || ''));
  loadShared();
}
$('#shared-pc').onclick = async e => {
  const t = e.target?.dataset || {};
  if (t.push) {
    if (!needDev()) return;
    const d = await safeApi('POST', '/api/shared/push', {serial: dev(), files: [t.push]});
    if (d.ok) loadShared(); else alert('Import failed: ' + (d.err || ''));
  } else if (t.exppc) {
    const d = await safeApi('POST', '/api/shared/export', {source: 'pc', files: [t.exppc]});
    if (d.ok) loadShared();
    alert(d.ok ? `Exported "${t.exppc}" to your Desktop ✓\n${d.desktop}` : 'Export failed: ' + (d.err || ''));
  } else if (t.delpc) { deleteBoth(t.delpc); }
};
$('#shared-dev').onclick = async e => {
  const t = e.target?.dataset || {};
  if (t.expdev) {
    if (!needDev()) return;
    const d = await safeApi('POST', '/api/shared/export', {source: 'device', serial: dev(), files: [t.expdev]});
    if (d.ok) loadShared();
    alert(d.ok ? `Exported "${t.expdev}" to your Desktop ✓\n${d.desktop}` : 'Export failed: ' + (d.err || ''));
  } else if (t.deldev) { deleteBoth(t.deldev); }
};
$('#btn-reveal').onclick = async () => { await safeApi('POST', '/api/shared/reveal', {}); };
$('#btn-shared-refresh').onclick = async () => { await loadDevices(); await loadShared(); };
$('#btn-syncnow').onclick = async () => {
  if (!needDev()) return;
  $('#sync-state').textContent = '⇄ syncing…';
  const d = await safeApi('POST', '/api/shared/sync', {serial: dev()});
  alert(d.ok ? 'Synced ✓ — Desktop pushed up, Device folder mirrors down.' : 'Sync failed: ' + (d.err || d.result || ''));
  loadShared();
};
$('#btn-exact').onclick = async () => {
  if (!needDev()) return;
  if (!confirm('Make the DEVICE exactly match the Desktop folder? Files on the device that are not in Desktop/ will be DELETED.')) return;
  const d = await safeApi('POST', '/api/shared/mirror', {serial: dev(), direction: 'push'});
  const changes = [...(d.deleted_from_device || []), ...(d.copied_to_device || [])];
  alert(d.ok ? `Device now matches Desktop ✓${changes.length ? '\n' + changes.join('\n') : '\n(already identical)'}` : 'Failed: ' + (d.err || ''));
  loadShared();
};
$('#btn-push').onclick = async () => {
  if (!needDev()) return;
  const d = await safeApi('POST', '/api/shared/push', {serial: dev()});
  alert(d.ok ? `Desktop pushed to device ✓ (${(d.pushed || []).length} files)` : 'Push failed: ' + (d.err || ''));
  loadShared();
};
$('#btn-pull').onclick = async () => {
  if (!needDev()) return;
  const d = await safeApi('POST', '/api/shared/pull', {serial: dev()});
  alert(d.ok ? `Device folder refreshed ✓ (${(d.pulled || []).length} new, ${(d.skipped || []).length} already had)` : 'Pull failed: ' + (d.err || ''));
  loadShared();
};
$('#btn-export-pc').onclick = async () => {
  const s = await api('GET', '/api/shared');
  const names = (s.desktop || []).map(f => f.name);
  if (!names.length) return alert('Nothing staged in the Desktop folder to export.');
  const d = await safeApi('POST', '/api/shared/export', {source: 'pc', files: names});
  alert(d.ok ? `Exported ${d.copied.length} file(s) to your Desktop ✓\n${d.desktop}`
             : 'Export failed: ' + (d.err || ''));
};
$('#btn-export-dev').onclick = async () => {
  if (!needDev()) return;
  const s = await api('GET', '/api/shared');
  const names = (s.deviceFolder || []).map(f => f.name);
  if (!names.length) return alert('No device files pulled yet — run Pull device first.');
  const d = await safeApi('POST', '/api/shared/export', {source: 'device', serial: dev(), files: names});
  alert(d.ok ? `Exported ${d.copied.length} file(s) to your Desktop ✓\n${d.desktop}`
             : 'Export failed: ' + (d.err || ''));
};
$('#btn-autosync').onclick = async e => {
  const s = dev();
  if (!autosyncOn && !s) { alert('Pick a device first — auto-sync needs a sync partner.'); return; }
  const want = !autosyncOn;
  const d = await safeApi('POST', '/api/shared/autosync', {serial: s, on: want, interval: 15});
  if (!d.ok) { alert(d.err || 'failed'); return; }
  autosyncOn = want;
  e.currentTarget.textContent = autosyncOn ? '🔁 Auto-sync ON' : '🔁 Auto-sync off';
  e.currentTarget.classList.toggle('on', autosyncOn);
  loadShared();
};
$('#btn-upload').onclick = async () => {
  const f = $('#up-file').files[0]; if (!f) return alert('Choose a file first');
  const b64 = await new Promise(res => {
    const r = new FileReader(); r.onload = () => res(r.result.split(',')[1]); r.readAsDataURL(f);
  });
  const d = await safeApi('POST', '/api/shared/upload', {name: f.name, b64, push_serial: dev()});
  if (!d.ok) { alert('Upload failed: ' + (d.err || '')); return; }
  alert(`Saved ${d.file} (${kb(d.bytes)})${d.pushed ? ' + pushed to device ✓' : ''}`);
  $('#up-file').value = '';
  loadShared();
};
$('#btn-analyze').onclick = async () => {
  const p = $('#apk-check').value.trim(); if (!p) return alert('Enter an APK path');
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
  alert(d.ok ? 'Saved ' + d.file + (d.rejected?.length ? `\nskipped ${d.rejected.length} bad step(s)` : '') : 'save failed');
  loadMacros();
};
$('#btn-macro-run').onclick = async () => {
  if (!needDev()) return;
  const d = await safeApi('POST', '/api/macros/run',
    {serial: dev(), name: $('#macro-name').value.trim() || 'macro'});
  alert(d.ok ? d.log.join('\n') : d.err);
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
$('#btn-sync').onclick = async () => {
  const d = await api('GET', '/api/devices');
  const taps = (d.devices || []).filter(x => x.state === 'device')
    .map(x => ({serial: x.serial, x: +$('#sync-x').value, y: +$('#sync-y').value}));
  if (!taps.length) return alert('No online devices');
  const r = await safeApi('POST', '/api/sync-tap', {taps});
  $('#sync-out').textContent = JSON.stringify(r.results || r, null, 1);
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
  try {
    const h = await api('GET', '/api/health');
    paintConn(h.ok ? `✓ connected → ${(h.app || 'agent')} v${h.version || '?'} ${h.remote ? '(token-secured)' : '(local)'}`
                   : '✗ backend error: ' + (h.err || 'unknown'));
  } catch (e) { paintConn('✗ unreachable — is the agent + tunnel running?'); return; }
  refreshAll();
};
$('#cfg-api').value = API_BASE(); $('#cfg-tok').value = API_TOK();
paintConn(API_BASE() ? 'saved backend → ' + API_BASE() : 'local mode (same origin)');

/* ================================ BOOT =================================== */
$('#btn-refresh').onclick = refreshAll;
$('#device').onchange = () => { loadApps(); loadShared(); loadSensors(); loadNetwork(); };
refreshAll();
setInterval(loadDevices, 15000);
setInterval(loadHost, 3000);
setInterval(() => { if (liveOn) loadAvds(); }, 10000);

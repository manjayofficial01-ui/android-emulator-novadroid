/* NovaDroid frontend — vanilla JS, offline-safe (no CDN).
   Split hosting: same-origin '' locally; set a tunnel URL + token in the
   backend bar when served from Vercel (persisted in localStorage). */
const $ = s => document.querySelector(s);
const API_BASE = () => (localStorage.getItem('nd_api') || '').replace(/\/$/, '');
const API_TOK = () => localStorage.getItem('nd_token') || '';
const apiHdrs = () => ({'Content-Type': 'application/json', ...(API_TOK() ? {Authorization: 'Bearer ' + API_TOK()} : {})});
const api = async (m, p, b) => (await fetch(API_BASE() + p, {method: m, headers: apiHdrs(), body: b ? JSON.stringify(b) : undefined})).json();
let macroSteps = [];
let liveTimer = null;
const targetSerial = () => ($('#shot-device').value || '').split(' ')[0];

async function refreshAll(){ await Promise.all([loadInfo(), loadProfiles(), loadAvds(), loadDevices(), loadMacros(), loadDisplayMeta()]); loadShared(); }

async function loadInfo(){
  try{
    const d = await api('GET','/api/info');
    $('#health').textContent = d.ok ? '● engine online' : '● engine issue';
    $('#health').className = 'pill ' + (d.ok ? 'ok' : 'bad');
    $('#sdk').textContent = 'SDK: ' + (d.sdk || 'not found');
    $('#info').textContent = JSON.stringify({sdk:d.sdk, adb:!!d.adb, emulator:!!d.emulator, accel:d.accel?.accel, ready:d.accel?.ready}, null, 1);
    const b = $('#accel-banner');
    if(!d.accel?.ready){ b.className='banner warn'; b.innerHTML='⚠ Hardware acceleration not ready — enable <b>Windows Hypervisor Platform</b> + BIOS VT-x/AMD-V. AEHD sunsets Dec 31 2026; WHPX is mandatory. '+(d.accel?.detail||''); }
    else { b.className='banner ok'; b.textContent='✓ '+(d.accel?.detail||'WHPX ready — hardware-accelerated execution (8–12× faster than TCG).'); }
  }catch(e){ $('#health').textContent='● backend offline'; $('#health').className='pill bad'; }
}
async function loadProfiles(){
  const d = await api('GET','/api/profiles');
  $('#profile').innerHTML = (d.profiles||[]).map(p=>`<option>${p}</option>`).join('');
}
async function loadAvds(){
  const d = await api('GET','/api/avds');
  const box = $('#avds'); box.innerHTML='';
  if(!d.avds?.length){ box.innerHTML='<div class="mono">No AVDs yet — create one above. First boot provisions the Play image (minutes); later quickboots take seconds.</div>'; return; }
  d.avds.forEach(a=>{
    const c = a.config||{};
    const el = document.createElement('div'); el.className='item';
    el.innerHTML = `<div><div class="name">${a.name}</div><div class="meta">cpu ${c['hw.cpu.ncore']||'?'} · ram ${c['hw.ramSize']||'?'}MB · gpu ${c['hw.gpu.mode']||'?'} · ${c['image.sysdir.1']||''}</div></div>
      <div class="row"><button class="btn small primary" data-act="start">▶ Quickboot</button><button class="btn small" data-act="cold">❄ Cold</button><button class="btn small" data-act="stop">⏹ Stop</button><button class="btn small" data-act="winfix">⌖ Fix window</button><button class="btn small" data-act="wipe">🧹 Wipe</button><button class="btn small danger" data-act="del">✕</button></div>`;
    el.querySelectorAll('button').forEach(btn=>btn.onclick=()=>avdAct(a.name, btn.dataset.act));
    box.appendChild(el);
  });
}
async function avdAct(name, act){
  if(act==='del' && !confirm('Delete '+name+'?')) return;
  if(act==='winfix'){
    const d = await api('POST','/api/avds/window/fix',{name});
    alert((d.saved?.ok ? `Window position reset → (${d.saved.x}, ${d.saved.y}). ` : 'Reset failed. ') +
      (d.live?.ok ? 'Running window moved on screen. ' : 'Not running — takes effect on next boot. ') + (d.tip||''));
    return;
  }
  const map = {start:['/api/avds/start',{name}], cold:['/api/avds/start',{name,cold:true}], stop:['/api/avds/stop',{name}], wipe:['/api/avds/wipe',{name}]};
  if(act==='del'){ await fetch(API_BASE()+'/api/avds?name='+encodeURIComponent(name), {method:'DELETE', headers: apiHdrs()}); }
  else { await api('POST', map[act][0], map[act][1]); }
  setTimeout(()=>{loadAvds(); loadDevices();}, 1500);
}
async function loadDevices(){
  const d = await api('GET','/api/devices');
  const box = $('#devices'); box.innerHTML='';
  const sel = $('#shot-device'); sel.innerHTML='';
  const dsel = $('#disp-device'); if(dsel) dsel.innerHTML='';
  const ssel = $('#shared-device'); if(ssel) ssel.innerHTML='';
  if(!d.devices?.length){ box.innerHTML='<div class="mono">No devices online. Quickboot an AVD and wait for boot (adb shows emulator-XXXX).</div>'; return; }
  d.devices.forEach(x=>{
    sel.innerHTML += `<option value="${x.serial}">${x.serial} — ${x.model||x.state}</option>`;
    if(dsel) dsel.innerHTML += `<option value="${x.serial}">${x.serial} — ${x.model||x.state}</option>`;
    if(ssel) ssel.innerHTML += `<option value="${x.serial}">${x.serial} — ${x.model||x.state}</option>`;
    const el=document.createElement('div'); el.className='item';
    el.innerHTML=`<div><div class="name">${x.serial}</div><div class="meta">${x.model||''} · Android ${x.android||'?'} (sdk ${x.sdk||'?'}) · ${x.abi||''} · ${x.state}</div></div>`;
    box.appendChild(el);
  });
}
async function shot(){
  const s = targetSerial(); if(!s) return alert('No device selected');
  $('#screen').src = API_BASE() + '/api/screen?serial='+encodeURIComponent(s)+'&t='+Date.now();
}
$('#btn-shot').onclick = shot;
$('#btn-live').onclick = e=>{
  const b = e.currentTarget;
  if(liveTimer){ clearInterval(liveTimer); liveTimer=null; b.classList.remove('on'); b.textContent='▶ Live (3s)'; }
  else { b.classList.add('on'); b.textContent='⏸ Live'; shot(); liveTimer=setInterval(shot, 3000); }
};
document.querySelectorAll('[data-key]').forEach(b=>b.onclick=async()=>{
  const s=targetSerial(); if(!s) return;
  await api('POST','/api/shell',{serial:s, cmd:'input keyevent '+b.dataset.key}); shot();
});
$('#btn-uia').onclick = async()=>{
  const s=targetSerial(); const d=await api('GET','/api/layout?serial='+encodeURIComponent(s));
  const el=$('#layout'); el.classList.remove('hidden');
  el.textContent = typeof d.tree==='string' ? d.tree.slice(0,4000) : JSON.stringify(d.tree,null,1).slice(0,4000);
};
$('#btn-create').onclick = async e=>{
  e.currentTarget.disabled=true; e.currentTarget.textContent='Creating… (1–3 min)';
  const d = await api('POST','/api/avds',{profile:$('#profile').value});
  alert(d.ok ? 'Created: '+d.name+' (4 CPU / 4GB / gpu auto applied)' : 'Failed: '+(d.out||d.err||'unknown'));
  e.currentTarget.disabled=false; e.currentTarget.textContent='＋ Create AVD';
  loadAvds();
};
$('#btn-raw').onclick = async()=>{
  const d = await api('POST','/api/avds/start-raw',{name:$('#raw-name').value.trim(), extra:$('#raw-flags').value});
  alert(d.ok ? 'Launched: '+d.cmd : 'Failed: '+d.err);
};
$('#btn-apk').onclick = async()=>{
  const s=targetSerial(); const p=$('#apk').value.trim();
  if(!s||!p) return alert('Pick a device and enter APK path');
  const d = await api('POST','/api/apk/install',{serial:s, apk_path:p});
  alert(d.ok ? 'Installed via '+d.tool : 'Install failed: '+(d.log||'').slice(-400));
};
$('#btn-geo').onclick = async()=>{ await api('POST','/api/geo',{serial:targetSerial(), lat:+$('#lat').value, lng:+$('#lng').value}); alert('GPS fix sent'); };
document.querySelectorAll('.geo').forEach(b=>b.onclick=()=>{ $('#lat').value=b.dataset.lat; $('#lng').value=b.dataset.lng; $('#btn-geo').click(); });
$('#btn-batt').onclick = async()=>{ await api('POST','/api/battery',{serial:targetSerial(), level:+$('#batt').value}); alert('Battery set'); };
$('#btn-shell').onclick = async()=>{
  const d = await api('POST','/api/shell',{serial:targetSerial(), cmd:$('#shell').value});
  $('#shell-out').textContent = (d.out||'') + (d.err?'\nERR: '+d.err:'');
};
$('#btn-perf').onclick = async()=>{
  const d = await api('GET','/api/perf?serial='+encodeURIComponent(targetSerial()));
  $('#perf').textContent = 'sampled ✓';
  $('#perf-out').textContent = (d.perf?.raw||'') + '\n' + (d.perf?.cpuinfo||'');
};
document.querySelectorAll('[data-macro]').forEach(b=>b.onclick=()=>{
  macroSteps.push(JSON.parse(b.dataset.macro)); $('#macro-steps').textContent = JSON.stringify(macroSteps,null,1);
});
$('#btn-macro-clear').onclick = ()=>{ macroSteps=[]; $('#macro-steps').textContent='[]'; };
$('#btn-macro-save').onclick = async()=>{
  const name = $('#macro-name').value.trim()||'macro';
  if(!macroSteps.length) macroSteps=[{type:'tap',x:540,y:1600},{type:'sleep',s:1},{type:'swipe',x1:540,y1:1400,x2:540,y2:400,ms:400}];
  const d = await api('POST','/api/macros',{name, steps:macroSteps});
  alert(d.ok?'Saved '+d.file:'save failed'); loadMacros();
};
$('#btn-macro-run').onclick = async()=>{
  const d = await api('POST','/api/macros/run',{serial:targetSerial(), name:$('#macro-name').value.trim()||'macro'});
  alert(d.ok ? d.log.join('\n') : d.err);
};
async function loadMacros(){
  const d = await api('GET','/api/macros');
  $('#macros').textContent = 'saved: ' + (d.macros||[]).map(m=>`${m.name}(${m.steps})`).join(', ');
}
$('#btn-sync').onclick = async()=>{
  const d = await api('GET','/api/devices');
  const taps = (d.devices||[]).filter(x=>x.state==='device').map(x=>({serial:x.serial, x:+$('#sync-x').value, y:+$('#sync-y').value}));
  if(!taps.length) return alert('No online devices');
  const r = await api('POST','/api/sync-tap',{taps});
  $('#sync-out').textContent = JSON.stringify(r.results,null,1);
};
$('#btn-refresh').onclick = refreshAll;

/* ---- Backend connection (local vs Vercel+tunnel) ---- */
function paintConn(state){
  $('#conn-state').textContent = state;
  const remote = location.hostname !== 'localhost' && location.hostname !== '127.0.0.1';
  if(remote && !API_BASE()) $('#conn-state').textContent = '☁ Vercel mode: paste tunnel URL + token, Connect';
}
$('#btn-conn').onclick = async()=>{
  localStorage.setItem('nd_api', $('#cfg-api').value.trim());
  localStorage.setItem('nd_token', $('#cfg-tok').value.trim());
  try{
    const h = await api('GET','/api/health');
    paintConn(h.ok ? `✓ connected → ${(h.app||'agent')} ${h.remote?'(token-secured)':'(local)'}` : '✗ backend error: '+(h.err||'unknown'));
  }catch(e){ paintConn('✗ unreachable — is the agent + tunnel running?'); return; }
  refreshAll();
};
$('#cfg-api').value = API_BASE(); $('#cfg-tok').value = API_TOK();
paintConn(API_BASE() ? 'saved backend → ' + API_BASE() : 'local mode (same origin)');

/* ---- Shared folder + APK compat ---- */
const sharedSerial = () => ($('#shared-device')?.value || '').split(' ')[0];
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let autosyncOn = false;
function needSharedSerial(){
  const s = sharedSerial();
  if(!s){ alert('No device picked — Quickboot an AVD first, then select it above.'); return ''; }
  return s;
}
async function safeApi(m, p, b){
  try{ return await api(m, p, b); }
  catch(e){ alert('Backend unreachable — is the agent running? (' + e.message + ')'); return {ok:false, err:String(e)}; }
}
function fileRow(name, meta, btn){
  const el = document.createElement('div'); el.className = 'item';
  el.innerHTML = `<div><div class="name">${esc(name)}</div><div class="meta">${esc(meta)}</div></div><div class="row">${btn}</div>`;
  return el;
}
async function loadShared(){
  try{
    const s = await api('GET','/api/shared');
    $('#shared-path').textContent = s.dir || '';
    const pc = $('#shared-pc'); pc.innerHTML = '';
    (s.files || []).forEach(f => pc.appendChild(fileRow(f.name, (f.bytes/1024).toFixed(0) + ' KB',
      `<button class="btn small" data-push="${esc(f.name)}">⬆ Import</button><button class="btn small danger" data-delpc="${esc(f.name)}">🗑</button>`)));
    if(!s.files?.length) pc.innerHTML = '<div class="mono">(empty — drop APKs/photos here)</div>';
    const se = sharedSerial();
    const dev = $('#shared-dev'); dev.innerHTML = '';
    if(se){
      const d = await api('GET','/api/shared/device?serial=' + encodeURIComponent(se));
      (d.files || []).forEach(n => dev.appendChild(fileRow(n, 'on device',
        `<button class="btn small" data-pull="${esc(n)}">⬇ Export</button><button class="btn small danger" data-deldev="${esc(n)}">🗑</button>`)));
      if(!d.files?.length) dev.innerHTML = `<div class="mono">${esc(d.note || '(empty)')}</div>`;
    } else dev.innerHTML = '<div class="mono">(Quickboot a device to see its side)</div>';
    const st = await api('GET','/api/shared/autosync');
    const jobs = st.jobs || {}, j = jobs[se];
    $('#sync-state').textContent = j
      ? `auto-sync every ${j.interval}s · ${j.waiting ? '⏳ ' : ''}${j.last_result || ''}`
      : (autosyncOn ? 'auto-sync on · pick a device' : 'auto-sync idle');
  }catch(e){ /* backend offline — pill already shows it */ }
}
async function deleteBoth(name){
  const se = sharedSerial();
  if(!confirm(`Permanently delete "${name}" from BOTH desktop and device${se ? '' : ' (no device picked — desktop only)'}?`)) return;
  const d = await safeApi('POST','/api/shared/delete',{serial:se, files:[name]});
  if(d.ok){
    const dev = d.deleted_device?.length ? `device ✓` : (d.device_note || 'device: n/a');
    alert(`Deleted "${name}" — desktop ✓, ${dev}. It will NOT come back on sync.`);
  } else alert('Delete failed: ' + (d.err || d.device_note || ''));
  loadShared();
}
$('#shared-pc').onclick = async e => {
  const t = e.target?.dataset || {};
  if(t.push){
    const se = needSharedSerial(); if(!se) return;
    const d = await safeApi('POST','/api/shared/push',{serial:se, files:[t.push]});
    if(d.ok) loadShared(); else alert('Import failed: ' + (d.err || ''));
  } else if(t.delpc){
    deleteBoth(t.delpc);
  }
};
$('#shared-dev').onclick = async e => {
  const t = e.target?.dataset || {};
  if(t.pull){
    const se = needSharedSerial(); if(!se) return;
    const d = await safeApi('POST','/api/shared/pull',{serial:se, files:[t.pull]});
    alert(d.ok ? `Exported ${t.pull} to the desktop folder ✓` : 'Export failed: ' + (d.err || ''));
    loadShared();
  } else if(t.deldev){
    deleteBoth(t.deldev);
  }
};
$('#btn-reveal').onclick = async()=>{ await safeApi('POST','/api/shared/reveal',{}); };
$('#btn-shared-refresh').onclick = async()=>{ await loadDevices(); await loadShared(); };
$('#btn-syncnow').onclick = async()=>{
  const se = needSharedSerial(); if(!se) return;
  const mode = $('#sync-mode').value;
  if(mode !== 'merge' && !confirm(mode === 'push'
    ? 'Make the DEVICE exactly match the desktop? Extra device files will be DELETED.'
    : 'Make the DESKTOP exactly match the device? Extra desktop files will be DELETED.')) return;
  $('#sync-state').textContent = '⇄ syncing…';
  const d = mode === 'merge'
    ? await safeApi('POST','/api/shared/sync',{serial:se})
    : await safeApi('POST','/api/shared/mirror',{serial:se, direction:mode});
  if(mode === 'merge'){
    alert(d.ok ? 'Synced both ways ✓' : 'Sync failed: ' + (d.err || d.result || ''));
  } else {
    const changes = [...(d.deleted_from_device || d.deleted_from_pc || []), ...(d.copied_to_device || d.copied_to_pc || [])];
    alert(d.ok ? `Mirror ${mode} done ✓${changes.length ? '\n' + changes.join('\n') : '\n(already identical)'}` : 'Mirror failed: ' + (d.err || ''));
  }
  loadShared();
};
$('#btn-push').onclick = async()=>{
  const se = needSharedSerial(); if(!se) return;
  const d = await safeApi('POST','/api/shared/push',{serial:se});
  alert(d.ok ? 'Imported all to /sdcard/NovaDroid-Shared ✓' : 'Import failed: ' + (d.err || ''));
  loadShared();
};
$('#btn-pull').onclick = async()=>{
  const se = needSharedSerial(); if(!se) return;
  const d = await safeApi('POST','/api/shared/pull',{serial:se});
  alert(d.ok ? 'Exported all into shared folder ✓' : 'Export failed: ' + (d.err || ''));
  loadShared();
};
$('#btn-autosync').onclick = async e=>{
  const se = sharedSerial();
  if(!autosyncOn && !se){ alert('Pick a device first — auto-sync needs a sync partner.'); return; }
  const want = !autosyncOn;
  const d = await safeApi('POST','/api/shared/autosync',{serial:se, on:want, interval:15});
  if(!d.ok){ alert(d.err || 'failed'); return; }
  autosyncOn = want;
  e.currentTarget.textContent = autosyncOn ? '🔁 Auto-sync ON' : '🔁 Auto-sync off';
  e.currentTarget.classList.toggle('on', autosyncOn);
  loadShared();
};
$('#btn-upload').onclick = async()=>{
  const f = $('#up-file').files[0]; if(!f) return alert('Choose a file first');
  const b64 = await new Promise(res=>{ const r=new FileReader(); r.onload=()=>res(r.result.split(',')[1]); r.readAsDataURL(f); });
  const d = await safeApi('POST','/api/shared/upload',{name:f.name, b64, push_serial:sharedSerial()});
  if(!d.ok){ alert('Upload failed: ' + (d.err || '')); return; }
  alert(d.ok ? `Saved ${d.file} (${(d.bytes/1024).toFixed(0)} KB)${d.pushed?' + pushed to device ✓':''}` : 'Upload failed: '+d.err);
  $('#apk-check').value = $('#apk-check').value || '';
  loadShared();
};
$('#btn-analyze').onclick = async()=>{
  let p = $('#apk-check').value.trim(); if(!p) return alert('Enter an APK path');
  const d = await safeApi('POST','/api/apk/analyze',{apk_path:p, serial:sharedSerial()});
  if(!d.ok){ $('#apk-out').textContent = 'ERR: '+d.err; return; }
  $('#apk-out').textContent =
    `${d.apk.label||d.apk.package} ${d.apk.versionName||''}\npackage ${d.apk.package}\nminSdk ${d.apk.minSdk} · target ${d.apk.targetSdk} · native ${d.apk.abis?.length?d.apk.abis.join(', '):'none (pure Java)'}\ndevice: ${d.device.model||d.device.note||d.device.serial} (API ${d.device.sdk||'?'}, bridge ${d.device.bridge||'n/a'})\n\nVERDICT [${d.level}]: ${d.reason}` +
    (d.level!=='BLOCKED' ? `\n\n→ Press Install in App & World Ops to sideload it now.` : '');
};

/* ---- Tablet & Display ---- */
const dispSerial = () => ($('#disp-device')?.value || '').split(' ')[0];
async function loadDisplayMeta(){
  try{
    const p = await api('GET','/api/display/presets');
    const box = $('#presets'); box.innerHTML='';
    [...(p.tablets||[]), ...(p.phones||[])].forEach(t=>{
      const b = document.createElement('button'); b.className='btn small';
      b.textContent = t.label; b.title = `${t.w}x${t.h} @ ${t.dpi}dpi — ${t.note||''}`;
      b.onclick = async()=>{
        $('#dw').value=t.w; $('#dh').value=t.h; $('#ddpi').value=t.dpi;
        const s = dispSerial(); if(!s) return alert('Preset staged below — Quickboot a device, then Apply live.');
        const r = await api('POST','/api/display/apply',{serial:s, w:t.w, h:t.h, dpi:t.dpi});
        $('#disp-cur').textContent = JSON.stringify(r,null,1); shot();
      };
      box.appendChild(b);
    });
    const s = await api('GET','/api/skins');
    $('#skin').innerHTML = (s.tablet_skins||[]).map(x=>`<option>${x}</option>`).join('') +
      `<option disabled>── all frames ──</option>` + (s.skins||[]).map(x=>`<option>${x}</option>`).join('');
    $('#skins-note').textContent = `${(s.skins||[]).length} frames shipped · tablet-class: ${(s.tablet_skins||[]).join(', ')}`;
  }catch(e){ /* offline */ }
}
$('#btn-disp-get').onclick = async()=>{
  const s = dispSerial(); if(!s) return alert('No device selected');
  const d = await api('GET','/api/display?serial='+encodeURIComponent(s));
  $('#disp-cur').textContent = JSON.stringify(d,null,1);
};
$('#btn-disp-apply').onclick = async()=>{
  const s = dispSerial(); if(!s) return alert('Quickboot an AVD first');
  const d = await api('POST','/api/display/apply',{serial:s, w:+$('#dw').value, h:+$('#dh').value, dpi:+$('#ddpi').value});
  $('#disp-cur').textContent = JSON.stringify(d,null,1); shot();
};
$('#btn-disp-reset').onclick = async()=>{
  const d = await api('POST','/api/display/reset',{serial:dispSerial()});
  $('#disp-cur').textContent = JSON.stringify(d,null,1); shot();
};
$('#btn-disp-save').onclick = async()=>{
  const name = prompt('Save display into which AVD?', 'medium_tablet'); if(!name) return;
  const d = await api('POST','/api/display/save',{name, w:+$('#dw').value, h:+$('#dh').value, dpi:+$('#ddpi').value});
  alert(d.ok ? `Saved ${d.display} → ${d.avd} (stop it first if running)` : 'Failed: '+d.err);
  loadAvds();
};
document.querySelectorAll('.ori').forEach(b=>b.onclick=async()=>{
  const d = await api('POST','/api/display/orientation',{serial:dispSerial(), mode:b.dataset.mode});
  $('#disp-cur').textContent = JSON.stringify(d,null,1); setTimeout(shot, 800);
});
$('#btn-skin').onclick = async()=>{
  const name = prompt('Apply frame to which AVD? (must be stopped)', 'medium_tablet'); if(!name) return;
  const d = await api('POST','/api/avds/skin',{name, skin:$('#skin').value});
  alert(d.ok ? `Frame ${d.skin} → ${d.avd}` : 'Failed: '+d.err);
};

refreshAll(); setInterval(loadDevices, 15000);

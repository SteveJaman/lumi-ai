/* --------------------------------------------------------------
   Lumi UI extras for the web app:
     - the theme engine (web/theme-engine.js) and the Appearance picker
     - the Cloud Escalation switch and the "100% Local Privacy" badge
     - a sync socket: theme_update, hybrid_mode_update, knowledge_ingest_*
       from any device (the island included) apply here live
     - drag and drop: drop files anywhere to teach Lumi ("Feed me data!")
     - toasts
   -------------------------------------------------------------- */

let ctx = null;                  // { api, state }
let ui = { theme: 'default', themes: [], hybrid: null };
let sock = null;
const hybridListeners = new Set();

if (window.LumiThemes) {
  window.LumiThemes.init({ base: '/web/themes/', surface: 'web', host: document.body });
}

// ── toasts ──────────────────────────────────────────────────────
function toastHost() {
  let h = document.getElementById('lumi-toasts');
  if (!h) {
    h = document.createElement('div');
    h.id = 'lumi-toasts';
    h.className = 'lumi-toasts';
    h.setAttribute('role', 'status');
    h.setAttribute('aria-live', 'polite');
    document.body.appendChild(h);
  }
  return h;
}

export function toast(text, { kind = 'info', ms = 4200, progress = null } = {}) {
  const el = document.createElement('div');
  el.className = `lumi-toast t-${kind}`;
  const msg = document.createElement('div');
  msg.className = 'lumi-toast-msg';
  msg.textContent = text;
  el.appendChild(msg);
  let bar = null;
  if (progress !== null) {
    bar = document.createElement('div');
    bar.className = 'lumi-toast-bar';
    bar.appendChild(document.createElement('i'));
    el.appendChild(bar);
  }
  toastHost().appendChild(el);
  const api = {
    set(t, k) { msg.textContent = t; if (k) el.className = `lumi-toast t-${k}`; },
    progress(p) { if (bar) bar.firstChild.style.width = `${Math.round(Math.max(0, Math.min(1, p)) * 100)}%`; },
    close(after = 0) { setTimeout(() => { el.classList.add('out'); setTimeout(() => el.remove(), 300); }, after); },
  };
  if (ms) api.close(ms);
  return api;
}

// ── state + sync ────────────────────────────────────────────────
function applyHybrid(h) {
  if (!h) return;
  ui.hybrid = h;
  const pill = document.getElementById('lumi-privacy');
  if (pill) {
    pill.textContent = h.badge;
    pill.className = `lumi-privacy ${h.fully_local ? 'is-local' : 'is-cloud'}`;
    pill.title = h.fully_local
      ? 'Every model runs on this PC. Nothing goes to a cloud AI.'
      : h.enabled
        ? `Hard prompts may go to ${h.cloud_model}.${h.cloud_problem ? ` (Not ready: ${h.cloud_problem})` : ''}`
        : 'Cloud Escalation is off, but a model in constants.py is a cloud model.';
  }
  hybridListeners.forEach((fn) => { try { fn(h); } catch (_) {} });
}

function mountPrivacyPill() {
  if (document.getElementById('lumi-privacy')) return;
  const pills = document.querySelector('.status-pills');
  if (!pills) return;
  const el = document.createElement('div');
  el.id = 'lumi-privacy';
  el.className = 'lumi-privacy';
  el.textContent = '…';
  pills.appendChild(el);
}

const ingests = new Map();
function onIngestStart(e) {
  if (e.source === 'web' && ingests.has(`web:${e.file}`)) return;   // our own upload has its toast
  ingests.set(e.id, toast(`Lumi is reading ${e.file}…`, { ms: 0, progress: 0.4, kind: 'busy' }));
}
function onIngestFinish(e) {
  const t = ingests.get(e.id);
  ingests.delete(e.id);
  const text = e.ok ? `Learned ${e.file} · ${e.chunks} passages` : `Couldn't learn ${e.file}: ${e.error}`;
  if (t) { t.set(text, e.ok ? 'ok' : 'error'); t.progress(1); t.close(3500); }
  else if (e.source !== 'web') toast(text, { kind: e.ok ? 'ok' : 'error' });
}

/* The page's ONE socket.io connection. Voice, the Pipeline page and the
   sync events all share it, so moving between pages never reconnects
   (each connect/disconnect is a line in Lumi's terminal). */
export function socket() {
  if (!sock && typeof io !== 'undefined') {
    sock = io({ transports: ['websocket', 'polling'], withCredentials: true });
  }
  return sock;
}

function connectSync() {
  if (sock && sock._lumiSync) return;
  if (!socket()) return;
  sock._lumiSync = true;
  sock.on('theme_update', (d) => {
    if (d && d.theme && d.theme !== ui.theme) { ui.theme = d.theme; window.LumiThemes && window.LumiThemes.apply(d.theme); }
    document.querySelectorAll('.theme-card').forEach((c) => c.classList.toggle('on', c.dataset.theme === ui.theme));
  });
  sock.on('hybrid_mode_update', applyHybrid);
  sock.on('knowledge_ingest_start', onIngestStart);
  sock.on('knowledge_ingest_finish', onIngestFinish);
  sock.on('connect', refreshState);           // missed broadcasts while away: re-read
}

async function refreshState() {
  try {
    const s = await ctx.api('/api/ui/state');
    ui = { ...ui, ...s };
    if (s.theme && window.LumiThemes && window.LumiThemes.current() !== s.theme) window.LumiThemes.apply(s.theme);
    applyHybrid(s.hybrid);
  } catch (_) { /* signed out or offline: keep the last look */ }
}

export function initUi(c) {
  ctx = c;
  mountPrivacyPill();
  refreshState();
  connectSync();
  if (ctx.state.isLocal) initDropzone();
}

// ── Settings → Appearance ───────────────────────────────────────
export function buildAppearanceCard() {
  const wrap = document.createElement('div');
  wrap.className = 'settings-field lumi-appearance';
  wrap.innerHTML = `
    <div class="settings-label">Theme</div>
    <div class="settings-field-help">Restyles the web app and every Lumi Island set to “Sync with Desktop”. Pipeline lighting follows it.</div>
    <div class="theme-cards" role="radiogroup" aria-label="Theme"></div>`;
  const grid = wrap.querySelector('.theme-cards');
  const draw = async () => {
    grid.innerHTML = '';
    for (const t of ui.themes.length ? ui.themes : [{ id: 'default', name: 'Lumi' }]) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = `theme-card${t.id === ui.theme ? ' on' : ''}`;
      b.dataset.theme = t.id;
      b.setAttribute('role', 'radio');
      b.setAttribute('aria-checked', String(t.id === ui.theme));
      b.innerHTML = '<span class="theme-swatches"></span><span class="theme-name"></span><span class="theme-desc"></span>';
      b.querySelector('.theme-name').textContent = t.name;
      b.querySelector('.theme-desc').textContent = t.description || '';
      try {
        const m = await window.LumiThemes.manifestOf(t.id);
        for (const k of ['bg', 'primary', 'secondary', 'accent']) {
          const s = document.createElement('i');
          s.style.background = (m.palette || {})[k] || 'transparent';
          b.querySelector('.theme-swatches').appendChild(s);
        }
      } catch (_) {}
      b.addEventListener('click', async () => {
        const prev = ui.theme;
        ui.theme = t.id;
        window.LumiThemes.apply(t.id);
        grid.querySelectorAll('.theme-card').forEach((c) => {
          c.classList.toggle('on', c.dataset.theme === t.id);
          c.setAttribute('aria-checked', String(c.dataset.theme === t.id));
        });
        try {
          await ctx.api('/api/ui/theme', { method: 'PUT', csrf: true, body: { theme: t.id } });
        } catch (e) {
          ui.theme = prev; window.LumiThemes.apply(prev);
          toast(`Couldn't save the theme: ${e.message}`, { kind: 'error' });
        }
      });
      grid.appendChild(b);
    }
  };
  if (ui.themes.length) draw(); else refreshState().then(draw);
  return wrap;
}

// ── Settings → Cloud Escalation ─────────────────────────────────
export function buildHybridCard() {
  const wrap = document.createElement('div');
  wrap.className = 'settings-field settings-toggle lumi-hybrid';
  wrap.innerHTML = `
    <div>
      <div class="settings-label" id="hybrid-label">Cloud Escalation <span class="lumi-privacy-inline"></span></div>
      <div class="settings-field-help">Off: Lumi runs 100% on this PC (Ollama), and Claude Code isn't offered.
        On: hard (opus-tier) prompts go to <code class="hybrid-model"></code>; everything else stays local.</div>
      <div class="settings-field-help hybrid-problem" hidden></div>
    </div>
    <label class="switch"><input type="checkbox" aria-labelledby="hybrid-label" disabled><span class="switch-slider"></span></label>`;
  const input = wrap.querySelector('input');
  const render = (h) => {
    input.checked = !!h.enabled;
    input.disabled = false;
    wrap.querySelector('.hybrid-model').textContent = h.cloud_model || 'the cloud model';
    const badge = wrap.querySelector('.lumi-privacy-inline');
    badge.textContent = h.badge;
    badge.className = `lumi-privacy-inline ${h.fully_local ? 'is-local' : 'is-cloud'}`;
    const prob = wrap.querySelector('.hybrid-problem');
    prob.hidden = !(h.enabled && h.cloud_problem);
    prob.textContent = h.cloud_problem ? `Not ready: ${h.cloud_problem} Until then Lumi stays local.` : '';
  };
  hybridListeners.add(render);
  if (ui.hybrid) render(ui.hybrid);
  input.addEventListener('change', async () => {
    input.disabled = true;
    try {
      const r = await ctx.api('/api/ui/hybrid', { method: 'PUT', csrf: true, body: { enabled: input.checked } });
      applyHybrid(r.hybrid);
    } catch (e) {
      input.checked = !input.checked;
      toast(`Couldn't change Cloud Escalation: ${e.message}`, { kind: 'error' });
    } finally { input.disabled = false; }
  });
  return wrap;
}

// ── drag and drop: "Feed me data!" ──────────────────────────────
function initDropzone() {
  if (document.getElementById('lumi-drop')) return;
  const ov = document.createElement('div');
  ov.id = 'lumi-drop';
  ov.className = 'lumi-drop';
  ov.hidden = true;
  ov.innerHTML = `
    <div class="lumi-drop-panel">
      <div class="lumi-drop-burst" aria-hidden="true"></div>
      <div class="lumi-drop-title">Feed me data!</div>
      <div class="lumi-drop-sub">Drop PDFs, Word docs, text or code. Lumi reads them right away.</div>
    </div>`;
  document.body.appendChild(ov);
  let depth = 0;
  const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes('Files');
  window.addEventListener('dragenter', (e) => { if (!hasFiles(e)) return; depth++; ov.hidden = false; });
  window.addEventListener('dragleave', (e) => { if (!hasFiles(e)) return; depth = Math.max(0, depth - 1); if (!depth) ov.hidden = true; });
  window.addEventListener('dragover', (e) => { if (hasFiles(e)) e.preventDefault(); });
  window.addEventListener('drop', (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth = 0; ov.hidden = true;
    const files = [...e.dataTransfer.files];
    if (files.length) uploadFiles(files);
  });
}

export function uploadFiles(files) {
  const names = files.map((f) => f.name);
  names.forEach((n) => ingests.set(`web:${n}`, true));
  const t = toast(`Feeding Lumi ${files.length === 1 ? files[0].name : `${files.length} files`}…`,
                  { ms: 0, progress: 0, kind: 'busy' });
  const fd = new FormData();
  files.forEach((f) => fd.append('files', f, f.name));
  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/api/knowledge/upload');
  xhr.withCredentials = true;
  if (ctx.state.csrf) xhr.setRequestHeader('X-CSRF-Token', ctx.state.csrf);
  xhr.upload.onprogress = (e) => { if (e.lengthComputable) t.progress(0.85 * e.loaded / e.total); };
  xhr.upload.onload = () => { t.set('Lumi is reading…'); t.progress(0.9); };
  xhr.onload = () => {
    names.forEach((n) => ingests.delete(`web:${n}`));
    let d = null;
    try { d = JSON.parse(xhr.responseText); } catch (_) {}
    if (xhr.status >= 400 || !d) {
      t.set(`Upload failed: ${(d && d.error) || `HTTP ${xhr.status}`}`, 'error'); t.close(6000); return;
    }
    const ok = d.files.filter((f) => f.ok), bad = d.files.filter((f) => !f.ok);
    t.progress(1);
    if (ok.length) t.set(`Learned ${ok.length} file${ok.length === 1 ? '' : 's'} · ${d.chunks} passages indexed`, bad.length ? 'warn' : 'ok');
    else t.set(`Nothing learned: ${bad.map((f) => `${f.file}: ${f.error}`).join('; ')}`, 'error');
    if (ok.length && bad.length) toast(`Skipped: ${bad.map((f) => `${f.file} (${f.error})`).join(', ')}`, { kind: 'warn', ms: 8000 });
    t.close(5000);
  };
  xhr.onerror = () => { names.forEach((n) => ingests.delete(`web:${n}`)); t.set('Upload failed: network error', 'error'); t.close(6000); };
  xhr.send(fd);
}

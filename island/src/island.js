// Lumi Island: hidden sliver -> peek on hover -> open on click, with three
// mini-apps (Home, Chat, Calendar) and tool approvals that take over until
// answered. Everything arrives over Lumi's local socket (modules/hitl_server.py
// + modules/island_bridge.py): status, chat messages and calendar changes are
// PUSHED, so nothing here polls. Timers that exist: a reconnect timer while
// Lumi is unreachable, per-request timeouts while a request is in flight, and
// short UI debounces.
(() => {
  'use strict';

  const DEFAULT_WS = 'ws://127.0.0.1:5090/hitl';
  const MAX_BACKOFF = 10000;
  const MORPH_MS = 360;
  const LEAVE_MS = 450;
  const BLUR_CLOSE_MS = 1500;
  const SIZES = { hidden: [200, 8], peek: [330, 58], open: [480, 590], approval: [440, 310],
                  capture: [440, 96], flash: [330, 46] };   // Quick Capture bar; a notice line
  const HUB_OPEN_H = 640;          // the Hub grows the open island a little (Rust fit allows up to 700)
  // The window keeps one width (the widest state) and only its height changes.
  // Tauri resizes and then re-centres a window in two separate steps; with the
  // width changing too, the island (centred in the window) jumped sideways for a
  // frame on every hover. The width morph is all CSS inside this fixed window.
  const WIN_W = Math.max(...Object.values(SIZES).map(([w]) => w));
  const STATUS_TEXT = {
    idle: 'Here with you', thinking: 'Thinking…', speaking: 'Speaking…',
    gaming: 'Gaming mode', offline: 'Offline', connecting: 'Connecting…',
  };
  const DANGER = /^(run_command|run_in_terminal|write_file|delete_file|send_email|delete_event)$/;
  const MAX_UNREAD_SHOWN = 3;
  const HOUR_PX = 44;

  const $ = (id) => document.getElementById(id);
  const island = $('island');
  const pad = (n) => String(n).padStart(2, '0');

  // ── Window sizing (Rust: island/src-tauri/src/main.rs) ─────────
  function fit(w, h) {
    const invoke = window.__TAURI__ && window.__TAURI__.core && window.__TAURI__.core.invoke;
    if (invoke) invoke('fit', { width: w, height: h }).catch(() => {});
  }
  // Another PC (your desktop, over Tailscale): saved from the gear panel. When set,
  // it wins over everything else and the hello carries the pairing code.
  const REMOTE_KEY = 'lumi-island-remote';
  function loadRemote() {
    try { const r = JSON.parse(localStorage.getItem(REMOTE_KEY) || 'null'); return r && r.host ? r : null; } catch (_) { return null; }
  }
  function saveRemote(r) {
    try { if (r) localStorage.setItem(REMOTE_KEY, JSON.stringify(r)); else localStorage.removeItem(REMOTE_KEY); } catch (_) {}
  }
  let remote = loadRemote();
  const hostForUrl = (h) => (h.includes(':') && !h.startsWith('[') ? `[${h}]` : h);

  // ?ws=... is for opening the page in a browser during development; the app
  // itself gets the address from Lumi through its environment (island_ws_url).
  async function getWsUrl() {
    if (remote) return `ws://${hostForUrl(remote.host)}:${remote.port || 5090}/hitl`;
    const q = new URLSearchParams(location.search).get('ws');
    if (q) return q;
    const invoke = window.__TAURI__ && window.__TAURI__.core && window.__TAURI__.core.invoke;
    if (!invoke) return DEFAULT_WS;
    try { return (await invoke('island_ws_url')) || DEFAULT_WS; } catch (_) { return DEFAULT_WS; }
  }
  async function getToken() {
    const invoke = window.__TAURI__ && window.__TAURI__.core && window.__TAURI__.core.invoke;
    if (!invoke) return '';
    try { return await invoke('island_token'); } catch (_) { return ''; }
  }

  // ── State ──────────────────────────────────────────────────────
  let state = 'hidden';            // hidden | peek | open | approval
  let app = 'home';                // home | hub | media | chat | calendar | pipeline
  let hovering = false;
  let beforeApproval = 'hidden';
  let leaveTimer = null, blurTimer = null;

  let ws = null, ready = false, retry = 500, reconnectTimer = null, lastClose = 0;
  let conn = 'connecting';         // connecting | online | offline
  let lumiState = 'idle';          // idle | thinking | speaking
  let gaming = false;
  let queue = [];                  // pending approvals
  let home = { snapshot: null, today: null };
  // Settings → General → Display in the web client; pushed to us when changed.
  let prefs = { time_format: '12h', units: 'imperial', location: '', place: '', has_location: false };
  let wx = { weather: null, error: null, code: null };
  let clockTimer = null;

  const effStatus = () => (conn !== 'online' ? conn : (gaming && lumiState === 'idle' ? 'gaming' : lumiState));

  const heightOf = (st, a = app) => (st === 'open' && a === 'hub' ? HUB_OPEN_H : SIZES[st][1]);
  const islandClass = (st) => `island ${st}${app === 'hub' ? ' tall' : ''}${island.classList.contains('danger') ? ' danger' : ''}`;

  function setState(next) {
    if (next === state) return;
    const h = heightOf(next);
    const ch = heightOf(state);
    const grows = h >= ch;
    if (grows) fit(WIN_W, h);                     // room first, then animate into it
    state = next;
    island.className = islandClass(next);
    $('approval').inert = next !== 'approval';
    $('capture').inert = next !== 'capture';
    if (!grows) setTimeout(() => { if (state === next) fit(WIN_W, h); }, MORPH_MS);
    syncVideo();
    syncClock();
    if (next === 'open' && app === 'chat') focusComposer();
  }

  function collapse() { setState(queue.length ? 'approval' : hovering ? 'peek' : 'hidden'); }

  // ── Status, video ──────────────────────────────────────────────
  function render() {
    const s = effStatus();
    island.dataset.status = s;
    const authFailed = conn === 'offline' && lastClose === 4401;
    $('status').textContent = authFailed ? (remote ? 'Pairing code rejected' : 'Restart from Lumi settings')
      : conn === 'offline' && remote ? `Can't reach ${remote.host}` : STATUS_TEXT[s];
    updateTyping();
    syncVideo();
  }

  function syncVideo() {
    const v = $('vid');
    const want = state !== 'hidden' && effStatus() !== 'gaming' && !document.hidden;
    if (want) { const p = v.play(); if (p && p.catch) p.catch(() => {}); } else if (!v.paused) v.pause();
  }
  document.addEventListener('visibilitychange', () => { syncVideo(); });

  // ── Request / response over the socket ─────────────────────────
  let seq = 0;
  const pending = new Map();
  function req(type, data = {}, timeout = 10000) {
    return new Promise((resolve, reject) => {
      if (!ws || !ready) return reject(new Error('offline'));
      const id = ++seq;
      const t = setTimeout(() => { pending.delete(id); reject(new Error('timeout')); }, timeout);
      pending.set(id, { resolve, reject, t });
      ws.send(JSON.stringify({ type, id, ...data }));
    });
  }
  function failAll(err) {
    for (const [, p] of pending) { clearTimeout(p.t); p.reject(err); }
    pending.clear();
  }

  // ── Connection ─────────────────────────────────────────────────
  async function connect() {
    reconnectTimer = null;
    conn = 'connecting'; render();
    const [token, url] = await Promise.all([getToken(), getWsUrl()]);
    let sock;
    try { sock = new WebSocket(url); } catch (_) { scheduleReconnect(); return; }
    ws = sock; ready = false;

    sock.onopen = () => sock.send(JSON.stringify(remote ? { type: 'hello', pair: remote.code || '' } : { type: 'hello', token }));
    sock.onmessage = (e) => {
      let msg;
      try { msg = JSON.parse(e.data); } catch (_) { return; }
      if (msg && typeof msg === 'object') onMessage(msg);
    };
    sock.onclose = (ev) => {
      if (ws === sock) ws = null;
      ready = false; lastClose = ev.code;
      failAll(new Error('offline'));
      conn = 'offline'; lumiState = 'idle';
      if (queue.length) { queue = []; renderApproval(); }     // Lumi fell back to the terminal
      render();
      // A rejected pairing code won't get better by retrying (and five tries
      // lock this device out): wait until a new code is saved in the gear panel.
      if (remote && ev.code === 4401) return;
      scheduleReconnect();
    };
    sock.onerror = () => { try { sock.close(); } catch (_) {} };
  }
  function scheduleReconnect() {
    if (reconnectTimer) return;
    reconnectTimer = setTimeout(connect, retry);
    retry = Math.min(retry * 2, MAX_BACKOFF);
  }

  function onMessage(msg) {
    if (msg.id != null && pending.has(msg.id)) {
      const p = pending.get(msg.id);
      pending.delete(msg.id); clearTimeout(p.t);
      return msg.type === 'error' ? p.reject(new Error(msg.message || 'error')) : p.resolve(msg);
    }
    switch (msg.type) {
      case 'hello_ok':
        ready = true; retry = 500; lastClose = 0; conn = 'online';
        gaming = !!msg.gaming;
        render(); loadHome(); loadChat(); loadUiState();
        if (app === 'calendar') loadCalendar();
        onFocusOnline();
        flushCaptures();
        loadShortcuts();
        flushNotes();
        island.dataset.gaming = String(gaming);
        if (app === 'media') mediaOpen();
        break;
      case 'shortcuts_update':                     // added or removed on another island or the web API
        setShortcuts(msg.shortcuts);
        break;
      case 'media_state':                          // now playing changed (modules/media_bridge.py)
        setMedia(msg.state);
        break;
      case 'theme_update':
        ui.theme = msg.theme || ui.theme; applyTheme();
        break;
      case 'hybrid_mode_update':
        ui.hybrid = msg; renderBrain();
        break;
      case 'mood_update':                          // Lumi's mood, for the Pipeline's Lumis
        applyMood(msg.mood);
        break;
      case 'knowledge_ingest_start':
        if (msg.source !== 'island' && !focus.running) feed(`Lumi is reading ${msg.file}…`, 0.5);
        break;
      case 'knowledge_ingest_finish':
        if (msg.source !== 'island' && !focus.running) feed(msg.ok ? `Lumi learned ${msg.file} · ${msg.chunks} passages` : `Couldn't learn ${msg.file}: ${msg.error}`, 1, msg.ok ? 'ok' : 'error');
        break;
      case 'lumi_status':
        if (STATUS_TEXT[msg.state]) { lumiState = msg.state; render(); }
        break;
      case 'agent_update':
        agents = msg.agents || agents; renderPipeline();
        break;
      case 'lumi_mode':
        gaming = !!msg.gaming; render();
        island.dataset.gaming = String(gaming);   // the Media tab drops blur and animation
        if (gaming && focus.running) endFocus('gaming');
        break;
      case 'snapshot':
        home.snapshot = msg.snapshot; renderHome();
        cal.cache.clear();                       // calendar may have changed
        if (state === 'open' && app === 'calendar') loadCalendar(true);
        break;
      case 'prefs': applyPrefs(msg.prefs); break;
      case 'chat_message': addMessage(msg.message); break;
      case 'chat_reset': loadChat(); break;
      case 'approval_request':
        if (msg.id && !queue.some((r) => r.id === msg.id)) {   // re-sent on reconnect
          queue.push({ id: String(msg.id), tool: String(msg.tool || ''), preview: String(msg.preview || ''),
                       agent: ['main', 'minecraft', 'office', 'system'].includes(msg.agent) ? msg.agent : 'main' });
          renderApproval();
        }
        break;
      case 'approval_resolved':                    // answered from another screen (web app)
      case 'approval_expired': {
        const n = queue.length;
        queue = queue.filter((r) => r.id !== msg.id);
        if (queue.length !== n) renderApproval();
        break;
      }
    }
  }

  // ── Header and tabs ────────────────────────────────────────────
  function setApp(name) {
    // The gear's panel covers the apps: picking any tab (even the current one)
    // closes it, so a tab always takes one click to show its app.
    if (!$('settings').hidden) closeSettings();
    const before = heightOf(state);
    app = name;
    const after = heightOf(state);
    if (after !== before) {                       // the Hub is a little taller than the other apps
      if (after > before) fit(WIN_W, after);
      else setTimeout(() => { if (heightOf(state) === after) fit(WIN_W, after); }, MORPH_MS);
    }
    island.className = islandClass(state);
    document.querySelectorAll('.tab').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.app === name)));
    document.querySelectorAll('.app').forEach((s) => s.classList.toggle('on', s.id === `app-${name}`));
    syncClock();
    if (name === 'home') loadHome();
    if (name === 'chat') { scrollMsgs(true); if (state === 'open') focusComposer(); }
    if (name === 'calendar') loadCalendar();
    if (name === 'pipeline') renderPipeline();
    if (name === 'hub') { renderHub(); loadShortcuts(); }
    if (name === 'media') mediaOpen(); else mediaClose();
  }
  function openApp(name) { setApp(name); setState(queue.length ? 'approval' : 'open'); }

  // ── Lumi's brain: theme sync, Cloud Escalation, drag-and-drop learning ──
  // Theme: follows the desktop ("Sync with Desktop", the default) or keeps its own
  // choice; both live in this island's localStorage. Cloud Escalation and the
  // privacy badge mirror the desktop (hybrid_mode_update). Files dropped on the
  // island are sent in chunks over this socket (knowledge_upload_*), so a Surface
  // paired over Tailscale teaches the desktop's Lumi just like the desktop does.
  const SYNC_KEY = 'lumi-island-theme-sync', LOCAL_KEY = 'lumi-island-theme-local';
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch (_) { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (_) {} },
  };
  let ui = { theme: 'default', themes: [], hybrid: null };
  const themeSync = () => store.get(SYNC_KEY, '1') === '1';
  if (window.LumiThemes) {
    window.LumiThemes.init({ base: 'themes/', surface: 'island', host: island,
                             initial: themeSync() ? null : store.get(LOCAL_KEY, null) });
  }

  // Lumi's mood (modules/mood_sync.py): kept on window so the Pipeline tab starts with
  // it when it mounts, and announced to it while it's on screen (pipeline.js listens).
  function applyMood(m) {
    window.lumiMood = m || null;
    window.dispatchEvent(new CustomEvent('lumi-mood', { detail: window.lumiMood }));
  }

  function wantedTheme() { return themeSync() ? ui.theme : store.get(LOCAL_KEY, ui.theme); }
  function applyTheme() {
    const id = wantedTheme();
    if (window.LumiThemes && id && window.LumiThemes.current() !== id) window.LumiThemes.apply(id);
    renderBrain();
  }
  function renderBrain() {
    const pick = $('theme-pick');
    if (pick && ui.themes.length && pick.options.length !== ui.themes.length) {
      pick.textContent = '';
      for (const t of ui.themes) { const o = el('option', null, t.name); o.value = t.id; pick.append(o); }
    }
    if (pick) pick.value = wantedTheme();
    const sync = $('theme-sync');
    if (sync) sync.checked = themeSync();
    const h = ui.hybrid;
    const sw = $('hybrid'), badge = $('privacy'), sub = $('hybrid-sub');
    if (h && sw) {
      sw.checked = !!h.enabled;
      sw.disabled = !(ws && ready);
      badge.textContent = h.badge;
      badge.className = `privacy ${h.fully_local ? 'is-local' : 'is-cloud'}`;
      sub.textContent = h.enabled
        ? (h.cloud_problem ? `Not ready: ${h.cloud_problem}` : `Hard prompts go to ${h.cloud_model}`)
        : 'Off: everything runs on your PC';
    }
  }
  async function loadUiState() {
    try {
      const s = await req('ui_state');
      ui = { theme: s.theme, themes: s.themes || [], hybrid: s.hybrid };
      applyTheme();
      if ('mood' in s) applyMood(s.mood);
    } catch (_) { /* older Lumi without themes: keep the default */ }
  }
  $('theme-pick').addEventListener('change', async (e) => {
    const id = e.target.value;
    if (themeSync()) {
      try { const s = await req('theme_set', { theme: id }); ui.theme = s.theme; } catch (_) {}
    } else {
      store.set(LOCAL_KEY, id);
    }
    applyTheme();
  });
  $('theme-sync').addEventListener('change', (e) => {
    store.set(SYNC_KEY, e.target.checked ? '1' : '0');
    if (!e.target.checked) store.set(LOCAL_KEY, window.LumiThemes ? window.LumiThemes.current() : ui.theme);
    applyTheme();
  });
  $('hybrid').addEventListener('change', async (e) => {
    const on = e.target.checked;
    e.target.disabled = true;
    try { const h = await req('hybrid_set', { enabled: on }); ui.hybrid = h; } catch (_) { e.target.checked = !on; }
    renderBrain();
  });

  // Feed progress (this island's uploads and anyone else's ingests).
  let feedTimer = null;
  function feed(text, pct, kind) {
    const f = $('feed');
    if (!f) return;
    clearTimeout(feedTimer);
    f.hidden = false;
    f.className = `feed${kind ? ' f-' + kind : ''}`;
    f.querySelector('.feed-text').textContent = text;
    f.querySelector('.feed-bar i').style.width = `${Math.round(Math.max(0, Math.min(1, pct)) * 100)}%`;
    if (kind === 'ok' || kind === 'error') feedTimer = setTimeout(() => { f.hidden = true; }, 6000);
  }

  const CHUNK = 36 * 1024;                     // raw bytes per message: base64 stays under the 64 KB socket cap
  function b64(buf) {
    let s = '';
    const bytes = new Uint8Array(buf);
    for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return btoa(s);
  }
  async function uploadOne(file) {
    const begin = await req('knowledge_upload_begin', { name: file.name, size: file.size });
    if (!begin.ok) throw new Error(begin.error || 'refused');
    for (let off = 0; off < file.size; off += CHUNK) {
      const part = await file.slice(off, off + CHUNK).arrayBuffer();
      const r = await req('knowledge_upload_chunk', { upload: begin.upload, data: b64(part) }, 30000);
      if (!r.ok) throw new Error(r.error || 'upload failed');
      feed(`Feeding Lumi ${file.name}…`, 0.85 * (off + part.byteLength) / file.size);
    }
    feed(`Lumi is reading ${file.name}…`, 0.92);
    const done = await req('knowledge_upload_end', { upload: begin.upload }, 300000);
    if (!done.ok) throw new Error(done.error || 'indexing failed');
    return done.result;
  }
  async function feedFiles(files) {
    if (!(ws && ready)) { feed('Not connected to Lumi', 0, 'error'); return; }
    let learned = 0, chunks = 0;
    const errors = [];
    for (const f of files) {
      try { const r = await uploadOne(f); learned++; chunks += r.chunks; } catch (e) { errors.push(`${f.name}: ${e.message}`); }
    }
    if (learned) feed(`Lumi learned ${learned} file${learned === 1 ? '' : 's'} · ${chunks} passages${errors.length ? ` (${errors.length} skipped)` : ''}`, 1, 'ok');
    else feed(`Nothing learned: ${errors.join('; ')}`, 1, 'error');
  }
  let dragDepth = 0;
  const hasFiles = (e) => [...((e.dataTransfer && e.dataTransfer.types) || [])].includes('Files');
  island.addEventListener('dragenter', (e) => {
    if (!hasFiles(e)) return;
    dragDepth++;
    island.classList.add('dropping');
    if (state !== 'open') openApp('home');
  });
  island.addEventListener('dragleave', (e) => {
    if (!hasFiles(e)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) island.classList.remove('dropping');
  });
  island.addEventListener('dragover', (e) => { if (hasFiles(e)) e.preventDefault(); });
  island.addEventListener('drop', (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    dragDepth = 0;
    island.classList.remove('dropping');
    const files = [...e.dataTransfer.files];
    if (files.length) { setApp('home'); feedFiles(files); }
  });

  $('avatar').addEventListener('click', () => (state === 'open' ? collapse() : openApp(app)));
  document.querySelectorAll('.tab').forEach((b) => b.addEventListener('click', () => openApp(b.dataset.app)));
  $('collapse').addEventListener('click', collapse);
  // A click on the sliver or on the peek card (not one of its buttons) opens it.
  island.addEventListener('click', (e) => {
    if ((state === 'hidden' || state === 'peek') && !e.target.closest('button, input, select, textarea, a')) openApp(app);
  });

  island.addEventListener('mouseenter', () => {
    hovering = true; clearTimeout(leaveTimer);
    if (state === 'hidden') setState('peek');
  });
  island.addEventListener('mouseleave', () => {
    hovering = false; clearTimeout(leaveTimer);
    leaveTimer = setTimeout(() => { if (state === 'peek' && !hovering) setState('hidden'); }, LEAVE_MS);
  });
  // An open island stays put while you work in it; it tucks away on Esc, the
  // collapse button, or once you click back into your game.
  window.addEventListener('blur', () => {
    clearTimeout(blurTimer);
    if (state === 'capture') { closeCapture(); return; }       // clicked into another app
    if (state !== 'open') return;
    blurTimer = setTimeout(() => {
      if (state === 'open' && !document.hasFocus() && !$('input').value.trim() && $('settings').hidden) collapse();
    }, BLUR_CLOSE_MS);
  });
  window.addEventListener('focus', () => clearTimeout(blurTimer));
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (state === 'capture') closeCapture();
    else if (state === 'open') collapse();
  });

  // ── Home ───────────────────────────────────────────────────────
  async function loadHome() {
    try {
      const res = await req('get_home');
      home.snapshot = res.snapshot;
      if (res.prefs) prefs = { ...prefs, ...res.prefs };
      if (STATUS_TEXT[res.state]) lumiState = res.state;
      render();
    } catch (_) { /* offline: keep what we have */ }
    renderHome();
    syncClock();
    loadToday();
    loadWeather(false);
  }

  // ── Now: time, day, weather ────────────────────────────────────
  const is12 = () => prefs.time_format !== '24h';
  const timeOpts = () => (is12() ? { hour: 'numeric', minute: '2-digit', hour12: true }
                                 : { hour: '2-digit', minute: '2-digit', hour12: false, hourCycle: 'h23' });
  function renderNow() {
    const d = new Date();
    const parts = new Intl.DateTimeFormat([], timeOpts()).formatToParts(d);
    $('clock-t').textContent = parts.filter((p) => p.type !== 'dayPeriod').map((p) => p.value).join('').trim();
    $('clock-ap').textContent = (parts.find((p) => p.type === 'dayPeriod') || {}).value || '';
    $('date').textContent = d.toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric' });
  }
  // One tick a minute, and only while Home is actually on screen.
  function syncClock() {
    clearTimeout(clockTimer); clockTimer = null;
    if (state !== 'open' || app !== 'home') return;
    renderNow();
    clockTimer = setTimeout(syncClock, 60000 - (Date.now() % 60000) + 50);
  }
  async function loadWeather(force) {
    try {
      const res = await req('get_weather', { refresh: !!force });
      wx = { weather: res.weather, error: res.error || null, code: res.code || null };
    } catch (_) { return; }
    renderWeather();
  }
  function renderWeather() {
    const w = wx.weather;
    if (!w) {
      const none = wx.code === 'not_configured';
      $('wx-icon').textContent = none ? '' : '\u2601\uFE0F';
      $('wx-temp').textContent = none ? 'No weather' : 'Weather offline';
      $('wx-text').textContent = '';
      $('wx-sub').textContent = none ? 'Add your city in Lumi: Settings, then General, then Display.' : (wx.error || '');
      return;
    }
    $('wx-icon').textContent = w.icon;
    $('wx-temp').textContent = `${w.temp}${w.unit}`;
    $('wx-text').textContent = w.text;
    const bits = [];
    if (w.high != null && w.low != null) bits.push(`H ${w.high}\u00B0  L ${w.low}\u00B0`);
    if (w.precip != null) bits.push(`Rain ${w.precip}%`);
    if (w.feels != null) bits.push(`Feels ${w.feels}\u00B0`);
    bits.push((w.place || '').split(', ').slice(0, 2).join(', '));      // "Kennesaw, Georgia", not the country too
    $('wx-sub').textContent = bits.filter(Boolean).join('  \u00B7  ') + (w.stale ? '  (old reading)' : '');
  }
  function applyPrefs(p) {
    if (!p) return;
    const placeChanged = p.place !== prefs.place || p.units !== prefs.units || p.has_location !== prefs.has_location;
    prefs = { ...prefs, ...p };
    syncClock(); renderHome(); drawCal();
    if (placeChanged) loadWeather(true);
  }

  async function loadToday() {
    const t = startOfDay(new Date());
    try {
      const res = await req('get_calendar', { start: ymd(t), end: ymd(addDays(t, 1)) });
      home.today = res.error ? null : res.events;
    } catch (_) { return; }
    renderHome();
  }

  function untilText(start, allDay) {
    const mins = Math.round((start - Date.now()) / 60000);
    if (allDay) return 'All day';
    if (mins <= 0) return 'Happening now';
    if (mins < 60) return `In ${mins} min`;
    if (mins < 1440) return `In ${Math.floor(mins / 60)} h ${mins % 60} min`;
    return start.toLocaleDateString([], { weekday: 'long', month: 'short', day: 'numeric' });
  }

  function renderHome() {
    const s = home.snapshot;
    const note = $('home-note');
    const ul = $('unread');
    ul.replaceChildren();
    const todayList = $('today');
    todayList.replaceChildren();
    $('today-card').hidden = !(s && s.connected && s.connected.google);
    if (home.today) {
      const left = home.today.filter((e) => evBounds(e)[1] > new Date() || e.all_day);
      if (!left.length) todayList.append(li('empty', 'Nothing else today.'));
      for (const e of left.slice(0, 5)) {
        const [st, en] = evBounds(e);
        const row = document.createElement('li');
        const time = document.createElement('time');
        time.textContent = e.all_day ? 'All day' : `${clock(st)} – ${clock(en)}`;
        row.append(time, span('t', e.title));
        todayList.append(row);
      }
    }
    if (!s || !s.connected || !s.connected.google) {
      $('next-title').textContent = 'No calendar yet';
      $('next-sub').textContent = '';
      $('unread-count').textContent = '';
      ul.append(li('empty', 'Nothing to show.'));
      note.textContent = 'Connect Google in Lumi (Settings, then Connections) to see your day here.';
      return;
    }
    note.textContent = '';
    const ev = s.next_event;
    if (ev) {
      const start = ev.all_day ? new Date(`${ev.start.slice(0, 10)}T00:00:00`) : new Date(ev.start);
      $('next-title').textContent = ev.title;
      const when = ev.all_day ? start.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' })
                              : clock(start);
      $('next-sub').textContent = [untilText(start, ev.all_day), when, ev.location].filter(Boolean).join('  ·  ');
    } else {
      $('next-title').textContent = 'Nothing scheduled';
      $('next-sub').textContent = 'Your calendar is clear.';
    }
    $('unread-count').textContent = s.unread_count ? String(s.unread_count) : '';
    if (!s.unread.length) ul.append(li('empty', 'Inbox zero.'));
    for (const m of s.unread.slice(0, MAX_UNREAD_SHOWN)) {
      const item = document.createElement('li');
      item.append(span('from', m.from), span('subj', m.subject));
      ul.append(item);
    }
  }
  function span(cls, text) { const n = document.createElement('span'); n.className = cls; n.textContent = text; return n; }
  function li(cls, text) { const n = document.createElement('li'); n.className = cls; n.textContent = text; return n; }

  // ── Chat ───────────────────────────────────────────────────────
  let msgs = [];
  const TAGS = /\[[A-Za-z_ ]{2,24}\]\s*/g;                 // [happy], [sighing]: Lumi's emotion tags
  const LONG = 420;

  function shownText(m) {
    let t = m.content.replace(TAGS, '').trim();
    if (m.role === 'user' && t.length > LONG) t = t.slice(0, 260).trimEnd() + '…  (long message)';
    return t || '…';
  }
  function bubble(m) {
    const row = document.createElement('div');
    row.className = `m ${m.role}`;
    row.dataset.n = m.n;
    if (m.role === 'assistant') {
      const img = document.createElement('img');
      img.src = 'lumi_poster.jpg'; img.alt = ''; img.draggable = false;
      row.append(img);
    }
    const b = document.createElement('div');
    b.className = 'bub';
    b.textContent = shownText(m);
    row.append(b);
    return row;
  }
  const nearBottom = () => { const el = $('msgs'); return el.scrollHeight - el.scrollTop - el.clientHeight < 80; };
  function scrollMsgs(force) { const el = $('msgs'); if (force || nearBottom()) el.scrollTop = el.scrollHeight; }

  function drawMsgs() {
    const el = $('msgs');
    el.replaceChildren();
    if (!msgs.length) el.append(Object.assign(document.createElement('div'), { className: 'empty-chat', textContent: 'Say hi to Lumi.' }));
    for (const m of msgs) el.append(bubble(m));
    updateTyping();
    scrollMsgs(true);
  }
  async function loadChat() {
    try { msgs = (await req('get_chat', { limit: 40 })).messages; } catch (_) { return; }
    drawMsgs();
  }
  function addMessage(m) {
    if (!m || msgs.some((x) => x.n === m.n)) return;
    const stick = nearBottom();
    msgs.push(m);
    msgs.sort((a, b) => a.n - b.n);
    const el = $('msgs');
    const empty = el.querySelector('.empty-chat');
    if (empty) empty.remove();
    const typing = el.querySelector('.typing');
    el.insertBefore(bubble(m), typing);
    if (stick) scrollMsgs(true);
    updateTyping();
  }
  function updateTyping() {
    const el = $('msgs');
    const has = el.querySelector('.typing');
    const want = lumiState === 'thinking' && conn === 'online';
    if (want && !has) {
      const row = document.createElement('div');
      row.className = 'm assistant typing';
      const img = document.createElement('img'); img.src = 'lumi_poster.jpg'; img.alt = '';
      const b = document.createElement('div'); b.className = 'bub';
      b.append(document.createElement('i'), document.createElement('i'), document.createElement('i'));
      row.append(img, b);
      el.append(row);
      scrollMsgs(false);
    } else if (!want && has) has.remove();
  }
  function focusComposer() { setTimeout(() => { if (app === 'chat' && state === 'open') $('input').focus(); }, MORPH_MS); }

  const input = $('input');
  const sendBtn = $('send');
  function autoGrow() { input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, 96) + 'px'; }
  input.addEventListener('input', () => { autoGrow(); sendBtn.disabled = !input.value.trim(); });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); $('composer').requestSubmit(); }
  });
  $('composer').addEventListener('submit', async (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    sendBtn.disabled = true;
    try {
      const res = await req('chat_send', { text });
      if (!res.ok) throw new Error(res.error || 'Not sent');
      input.value = ''; autoGrow();
    } catch (err) {
      const old = input.placeholder;
      input.placeholder = err.message === 'offline' ? 'Lumi is offline.' : err.message;
      setTimeout(() => { input.placeholder = old; }, 2500);
    }
    sendBtn.disabled = !input.value.trim();
    input.focus();
  });
  sendBtn.disabled = true;

  // ── Calendar ───────────────────────────────────────────────────
  const cal = { view: 'month', cursor: new Date(), seq: 0, cache: new Map(), events: [], error: null };
  const startOfDay = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const addDays = (d, n) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);
  const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const clock = (d) => d.toLocaleTimeString([], timeOpts());
  const sameDay = (a, b) => ymd(a) === ymd(b);
  const MONTHS = Array.from({ length: 12 }, (_, i) => new Date(2000, i, 1).toLocaleDateString([], { month: 'long' }));
  const WEEKDAYS = Array.from({ length: 7 }, (_, i) => new Date(2023, 0, 1 + i).toLocaleDateString([], { weekday: 'short' }));   // Sunday first

  function calRange() {
    const c = cal.cursor, today = startOfDay(new Date());
    switch (cal.view) {
      case 'list': return [today, addDays(today, 14)];
      case 'day': { const s = startOfDay(c); return [s, addDays(s, 1)]; }
      case 'month': { const first = new Date(c.getFullYear(), c.getMonth(), 1); const s = addDays(first, -first.getDay()); return [s, addDays(s, 42)]; }
      default: return [new Date(c.getFullYear(), 0, 1), new Date(c.getFullYear() + 1, 0, 1)];
    }
  }
  function evBounds(ev) {
    if (ev.all_day) return [new Date(`${ev.start.slice(0, 10)}T00:00:00`), new Date(`${(ev.end || ev.start).slice(0, 10)}T00:00:00`)];
    const s = new Date(ev.start);
    return [s, new Date(ev.end || ev.start)];
  }
  function indexByDay(events) {
    const map = new Map();
    for (const ev of events) {
      const [s, e] = evBounds(ev);
      let d = startOfDay(s);
      const last = e > s ? new Date(e.getTime() - 1) : s;       // an end exactly at midnight belongs to the day before
      for (let n = 0; d <= last && n < 400; n++, d = addDays(d, 1)) {
        const k = ymd(d);
        if (!map.has(k)) map.set(k, []);
        map.get(k).push(ev);
      }
    }
    for (const list of map.values()) list.sort((a, b) => (b.all_day - a.all_day) || evBounds(a)[0] - evBounds(b)[0]);
    return map;
  }

  async function loadCalendar(force = false) {
    const [s, e] = calRange();
    const key = `${ymd(s)}:${ymd(e)}`;
    const cached = cal.cache.get(key);
    if (cached && !force && Date.now() - cached.t < 60000) { cal.events = cached.events; cal.error = null; return drawCal(); }
    const mine = ++cal.seq;
    if (cached) { cal.events = cached.events; cal.error = null; } else { cal.events = []; cal.error = { loading: true }; }
    drawCal();
    try {
      const res = await req('get_calendar', { start: ymd(s), end: ymd(e), refresh: force }, 15000);
      if (mine !== cal.seq) return;
      if (res.error) { cal.error = res; cal.events = []; }
      else {
        cal.error = null; cal.events = res.events;
        cal.cache.set(key, { t: Date.now(), events: res.events });
        while (cal.cache.size > 10) cal.cache.delete(cal.cache.keys().next().value);
      }
    } catch (err) {
      if (mine !== cal.seq) return;
      cal.error = { error: err.message === 'offline' ? 'Lumi is offline.' : 'Lumi didn’t answer.', code: 'offline' };
    }
    drawCal();
  }

  function calTitle() {
    const c = cal.cursor;
    if (cal.view === 'list') return 'Next 14 days';
    if (cal.view === 'day') return c.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' });
    if (cal.view === 'month') return `${MONTHS[c.getMonth()]} ${c.getFullYear()}`;
    return String(c.getFullYear());
  }
  function calStep(dir) {
    const c = cal.cursor;
    if (cal.view === 'day') cal.cursor = addDays(c, dir);
    else if (cal.view === 'month') cal.cursor = new Date(c.getFullYear(), c.getMonth() + dir, 1);
    else if (cal.view === 'year') cal.cursor = new Date(c.getFullYear() + dir, c.getMonth(), 1);
    loadCalendar();
  }
  function calGo(view, date) { cal.view = view; if (date) cal.cursor = date; loadCalendar(); }

  function drawCal() {
    $('cal-title').textContent = calTitle();
    document.querySelectorAll('#cal-views button').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.view === cal.view)));
    $('cal-prev').disabled = $('cal-next').disabled = cal.view === 'list';
    const body = $('cal-body');
    const scroll = body.scrollTop;
    const err = cal.error;
    if (err && err.loading) return body.replaceChildren(msg('Loading…'));
    if (err) return body.replaceChildren(errBox(err));
    body.replaceChildren(...(({ list: drawList, day: drawDay, month: drawMonth, year: drawYear })[cal.view])());
    if (cal.view === 'day') body.scrollTop = dayScrollTarget();
    else body.scrollTop = scroll;
  }
  function msg(text, hint) {
    const d = document.createElement('div'); d.className = 'cal-msg'; d.textContent = text;
    if (hint) d.append(Object.assign(document.createElement('span'), { className: 'hint', textContent: hint }));
    return d;
  }
  function errBox(err) {
    const d = msg(err.error, err.code === 'not_connected' ? 'In Lumi: Settings, then Connections, then Connect Google.' : '');
    if (err.code !== 'not_connected') {
      const b = Object.assign(document.createElement('button'), { type: 'button', textContent: 'Try again' });
      b.addEventListener('click', () => loadCalendar(true));
      d.append(document.createElement('br'), b);
    }
    return d;
  }
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };

  // List
  function drawList() {
    const byDay = indexByDay(cal.events);
    const days = [...byDay.keys()].sort();
    if (!days.length) return [msg('Nothing on the calendar for the next 14 days.')];
    const out = [];
    const today = startOfDay(new Date());
    for (const k of days) {
      const d = new Date(`${k}T00:00:00`);
      if (d < today) continue;
      const sec = el('section', 'lday');
      const diff = Math.round((d - today) / 86400000);
      const label = d.toLocaleDateString([], { weekday: 'long', month: 'short', day: 'numeric' });
      sec.append(el('h4', '', diff === 0 ? `Today · ${label}` : diff === 1 ? `Tomorrow · ${label}` : label));
      for (const ev of byDay.get(k)) {
        const [s, e] = evBounds(ev);
        const row = el('div', 'lev' + (ev.all_day ? ' all' : ''));
        row.append(el('time', '', ev.all_day ? 'All day' : `${clock(s)} – ${clock(e)}`));
        const main = el('div');
        main.append(el('div', 't', ev.title));
        if (ev.location) main.append(el('div', 'loc', ev.location));
        row.append(main);
        sec.append(row);
      }
      out.push(sec);
    }
    return out.length ? out : [msg('Nothing on the calendar for the next 14 days.')];
  }

  // Day
  function dayEvents() { return indexByDay(cal.events).get(ymd(cal.cursor)) || []; }
  function dayScrollTarget() {
    const timed = dayEvents().filter((e) => !e.all_day);
    const hour = sameDay(cal.cursor, new Date()) ? new Date().getHours() - 1
      : timed.length ? evBounds(timed[0])[0].getHours() - 1 : 8;
    return Math.max(0, hour) * HOUR_PX;
  }
  function drawDay() {
    const events = dayEvents();
    const wrap = el('div', 'dayview');
    const allDay = events.filter((e) => e.all_day);
    if (allDay.length) {
      const strip = el('div', 'allday');
      for (const ev of allDay) strip.append(el('span', 'chip', ev.title));
      wrap.append(strip);
    }
    const grid = el('div', 'grid');
    for (let h = 0; h < 24; h++) {
      const row = el('div', 'hour');
      row.append(el('span', '', is12() ? new Date(2000, 0, 1, h).toLocaleTimeString([], { hour: 'numeric', hour12: true }) : `${pad(h)}:00`));
      grid.append(row);
    }
    const dayStart = startOfDay(cal.cursor), dayEnd = addDays(dayStart, 1);
    const items = events.filter((e) => !e.all_day).map((ev) => {
      const [s, e] = evBounds(ev);
      return { ev, s: new Date(Math.max(s, dayStart)), e: new Date(Math.min(Math.max(e, new Date(s.getTime() + 20 * 60000)), dayEnd)) };
    }).sort((a, b) => a.s - b.s);
    // side-by-side columns for overlapping events
    let cluster = [], clusterEnd = 0;
    const flush = () => {
      const cols = [];
      for (const it of cluster) {
        let c = cols.findIndex((end) => end <= it.s);
        if (c < 0) { c = cols.length; cols.push(0); }
        cols[c] = it.e; it.col = c;
      }
      for (const it of cluster) it.cols = cols.length;
      cluster = []; clusterEnd = 0;
    };
    for (const it of items) { if (cluster.length && it.s >= clusterEnd) flush(); cluster.push(it); clusterEnd = Math.max(clusterEnd, it.e); }
    flush();
    for (const it of items) {
      const top = ((it.s - dayStart) / 3600000) * HOUR_PX;
      const box = el('div', 'ev');
      box.style.top = `${top}px`;
      box.style.height = `${Math.max(22, ((it.e - it.s) / 3600000) * HOUR_PX - 2)}px`;
      box.style.left = `calc(${(it.col / it.cols) * 100}% + 2px)`;
      box.style.width = `calc(${100 / it.cols}% - 4px)`;
      box.append(el('b', '', it.ev.title), el('small', '', `${clock(it.s)} – ${clock(it.e)}${it.ev.location ? ' · ' + it.ev.location : ''}`));
      grid.append(box);
    }
    if (sameDay(dayStart, new Date())) {
      const now = new Date();
      const line = el('div', 'nowline');
      line.style.top = `${((now - dayStart) / 3600000) * HOUR_PX}px`;
      grid.append(line);
    }
    wrap.append(grid);
    if (!events.length) wrap.prepend(msg('Nothing planned.'));
    return [wrap];
  }

  // Month
  function drawMonth() {
    const byDay = indexByDay(cal.events);
    const [start] = calRange();
    const out = [];
    const head = el('div', 'mhead');
    for (const w of WEEKDAYS) head.append(el('div', '', w.slice(0, 3)));
    const grid = el('div', 'mgrid');
    const today = new Date();
    for (let i = 0; i < 42; i++) {
      const d = addDays(start, i);
      const cell = el('button', 'mcell' + (d.getMonth() !== cal.cursor.getMonth() ? ' other' : '') + (sameDay(d, today) ? ' today' : ''));
      cell.type = 'button';
      cell.setAttribute('aria-label', d.toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric' }));
      cell.append(el('span', 'mnum', String(d.getDate())));
      const list = byDay.get(ymd(d)) || [];
      const shown = list.length > 2 ? 1 : list.length;           // keep every cell the same height
      for (const ev of list.slice(0, shown)) cell.append(el('span', 'mev' + (ev.all_day ? ' all' : ''), ev.title));
      if (list.length > shown) cell.append(el('span', 'more-ev', `+${list.length - shown} more`));
      cell.addEventListener('click', () => calGo('day', d));
      grid.append(cell);
    }
    out.push(head, grid);
    return out;
  }

  // Year
  function drawYear() {
    const byDay = indexByDay(cal.events);
    const y = cal.cursor.getFullYear();
    const grid = el('div', 'ygrid');
    const today = new Date();
    for (let m = 0; m < 12; m++) {
      const box = el('div', 'ym');
      const title = el('h5', '', MONTHS[m].slice(0, 3));
      title.addEventListener('click', () => calGo('month', new Date(y, m, 1)));
      box.append(title);
      const days = el('div', 'yd');
      const first = new Date(y, m, 1);
      for (let i = 0; i < first.getDay(); i++) days.append(el('i'));
      const count = new Date(y, m + 1, 0).getDate();
      for (let d = 1; d <= count; d++) {
        const date = new Date(y, m, d);
        const n = (byDay.get(ymd(date)) || []).length;
        const b = el('button', (n ? `e${Math.min(n, 3)}` : '') + (sameDay(date, today) ? ' today' : ''), String(d));
        b.type = 'button';
        b.title = n ? `${date.toLocaleDateString([], { month: 'short', day: 'numeric' })}: ${n} event${n > 1 ? 's' : ''}` : date.toLocaleDateString([], { month: 'short', day: 'numeric' });
        b.addEventListener('click', () => calGo('day', date));
        days.append(b);
      }
      box.append(days);
      grid.append(box);
    }
    return [grid];
  }

  $('cal-prev').addEventListener('click', () => calStep(-1));
  $('cal-next').addEventListener('click', () => calStep(1));
  $('cal-today').addEventListener('click', () => { cal.cursor = new Date(); loadCalendar(); });
  document.querySelectorAll('#cal-views button').forEach((b) => b.addEventListener('click', () => calGo(b.dataset.view)));

  // ── Approvals ──────────────────────────────────────────────────
  function renderApproval() {
    const cur = queue[0];
    if (!cur) {
      island.classList.remove('danger');
      renderPipeline();
      if (state !== 'approval') return;                      // answered on a Pipeline card: stay where we are
      return setState(beforeApproval === 'open' ? 'open' : hovering ? 'peek' : 'hidden');
    }
    const danger = DANGER.test(cur.tool);
    island.classList.toggle('danger', danger);
    $('eyebrow').textContent = danger ? 'Lumi wants to run · check carefully' : 'Lumi wants to run';
    $('tool').textContent = cur.tool || 'unknown tool';
    const pv = cur.preview || '(no details)';
    $('preview').textContent = pv.trimStart().startsWith('{') ? pv.replace(/\\n/g, '\n') : pv;
    $('preview').scrollTop = 0;
    $('more').hidden = queue.length < 2;
    $('more').textContent = `+${queue.length - 1}`;
    renderPipeline();
    if (state === 'open' && app === 'pipeline') return;      // answered on the agent's card instead
    if (state !== 'approval') beforeApproval = state;
    setState('approval');
  }
  function respond(decision, id) {
    const i = id ? queue.findIndex((r) => r.id === id) : 0;
    const cur = i >= 0 ? queue.splice(i, 1)[0] : null;
    if (cur && ws && ready) ws.send(JSON.stringify({ type: 'approval_response', id: cur.id, decision }));
    renderApproval();
  }

  // ── Pipeline (pixel office: pipeline.js) ───────────────────────
  // The canvas shows each agent walking to its station and animating; an
  // agent that needs approval walks to the front, waves, and gets
  // Approve / Decline buttons above its head (same respond() as the
  // full-screen approval view). The list below keeps each agent's task.
  const AGENTS = [['main', 'Main'], ['minecraft', 'Minecraft'], ['office', 'Office'], ['system', 'System']];
  const STATUS_LABEL = { idle: 'Idle', thinking: 'Thinking', executing: 'Executing', waiting_approval: 'Needs you', error: 'Error' };
  let agents = {};
  let pixel = null;

  function renderPipeline() {
    if (!pixel && window.LumiPipeline && app === 'pipeline') {
      pixel = window.LumiPipeline.mount($('pipe'), {
        base: 'pixel-office/',
        onDecision: (id, decision) => respond(decision, id),
      });
    }
    const view = {};
    for (const [id] of AGENTS) {
      const a = agents[id] || { status: 'idle', task: '', progress: 0 };
      const req = queue.find((r) => r.agent === id);
      view[id] = req ? { ...a, status: 'waiting_approval', visual_state: 'waving' } : a;
    }
    if (pixel) { pixel.update(view); pixel.setApprovals(queue); }
    const list = $('pipe-tasks');
    if (list) {
      list.textContent = '';
      for (const [id, name] of AGENTS) {
        const a = view[id];
        const li = el('li', `pt s-${STATUS_LABEL[a.status] ? a.status : 'idle'}`);
        li.append(el('span', 'pt-name', name), el('span', 'pt-status', STATUS_LABEL[a.status] || 'Idle'),
                  el('span', 'pt-task', a.task || a.model_name || ''));
        list.append(li);
      }
    }
    const open = queue.length;
    $('pipe-note').textContent = conn !== 'online' ? 'Not connected' : open ? `${open} waiting for you` : '';
  }

  // ── Connection settings (the gear) ─────────────────────────────
  const CODE_RE = /^[A-Z2-9]{4}(-?[A-Z2-9]{4}){3}$/;
  function openSettings() {
    $('set-host').value = remote ? remote.host : '';
    $('set-port').value = remote ? String(remote.port || 5090) : '';
    $('set-code').value = remote ? remote.code || '' : '';
    $('set-msg').textContent = remote ? `Connected to ${remote.host}` : 'Using Lumi on this PC.';
    $('set-msg').classList.remove('bad');
    $('settings').hidden = false;
    $('gear').setAttribute('aria-expanded', 'true');
    $('set-host').focus();
  }
  function closeSettings() { $('settings').hidden = true; $('gear').setAttribute('aria-expanded', 'false'); }
  function reconnect() {
    clearTimeout(reconnectTimer); reconnectTimer = null; retry = 500;
    const old = ws; ws = null; ready = false;
    if (old) { old.onclose = null; try { old.close(); } catch (_) {} }
    failAll(new Error('offline'));
    conn = 'connecting'; render(); connect();
  }
  $('gear').addEventListener('click', () => ($('settings').hidden ? openSettings() : closeSettings()));
  $('set-cancel').addEventListener('click', closeSettings);
  $('set-local').addEventListener('click', () => { remote = null; saveRemote(null); closeSettings(); reconnect(); });
  $('set-save').addEventListener('click', () => {
    const host = $('set-host').value.trim().replace(/^wss?:\/\//, '').replace(/\/.*$/, '').replace(/:\d+$/, '');
    const port = parseInt($('set-port').value.trim() || '5090', 10);
    const code = $('set-code').value.trim().toUpperCase().replace(/\s+/g, '');
    const bad = (t) => { $('set-msg').textContent = t; $('set-msg').classList.add('bad'); };
    if (!host) { remote = null; saveRemote(null); closeSettings(); return reconnect(); }
    if (!/^[A-Za-z0-9.:\-\[\]]{1,253}$/.test(host)) return bad('That host doesn\u2019t look right. Use an address like 100.101.102.103.');
    if (!(port > 0 && port < 65536)) return bad('The port should be a number like 5090.');
    if (!CODE_RE.test(code)) return bad('Enter the pairing code from Lumi\u2019s web app (like ABCD-EFGH-JKMN-PQRS).');
    remote = { host, port, code };
    saveRemote(remote);
    closeSettings();
    reconnect();
  });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !$('settings').hidden) { e.stopPropagation(); closeSettings(); } }, true);
  $('approve').addEventListener('click', () => respond('approve'));
  $('decline').addEventListener('click', () => respond('decline'));

  // ── Notices ────────────────────────────────────────────────────
  // A short line, gone after 4 s: in the capture bar's hint while it's open,
  // floating over the app when the island is open, else the island briefly
  // shows just this line. Approvals are never covered by one.
  const NOTICE_MS = 4000;
  let noticeTimer = null;
  function notice(text, kind = 'ok') {
    clearTimeout(noticeTimer);
    if (state === 'capture') {
      const h = $('qc-hint');
      h.textContent = text; h.className = `qc-hint ${kind}`;
      noticeTimer = setTimeout(() => { if (state === 'capture' && !$('qc-input').value.trim()) closeCapture(); }, NOTICE_MS);
      return;
    }
    if (state === 'approval') return;
    const n = $('notice');
    n.textContent = text; n.className = `notice ${kind} on`;
    if (state !== 'open') setState('flash');
    noticeTimer = setTimeout(() => {
      n.className = 'notice';
      if (state === 'flash') setState(hovering ? 'peek' : 'hidden');
    }, NOTICE_MS);
  }

  const tauriInvoke = () => window.__TAURI__ && window.__TAURI__.core && window.__TAURI__.core.invoke;
  const online = () => conn === 'online' && !!ws && ready;

  // ── Quick Capture ──────────────────────────────────────────────
  // The global hotkey (Rust: set_capture_shortcut) focuses the window and
  // fires "quick-capture". A link goes to the library, an existing absolute
  // file path is uploaded like a drag-and-drop, anything else is a memory.
  // Offline, captures wait in localStorage and are sent on the next hello_ok.
  // Captured text is never logged.
  const PENDING_KEY = 'lumi-island-quick-capture-pending';
  const HOTKEY_KEY = 'lumi-island-capture-hotkey';
  const HINT = 'Paste a link, text, or a file path. Enter to send.';
  let capPrev = null, flushing = false;

  function openCapture() {
    if (state === 'approval') return;                    // an approval waits for an answer first
    if (state !== 'capture') {
      capPrev = { state: state === 'flash' ? (hovering ? 'peek' : 'hidden') : state };
      if (!$('settings').hidden) closeSettings();
      clearTimeout(noticeTimer);
      $('qc-input').value = '';
      $('qc-hint').textContent = HINT; $('qc-hint').className = 'qc-hint';
      setState('capture');
    }
    setTimeout(() => $('qc-input').focus(), 30);
  }
  function closeCapture() {
    if (state !== 'capture') return;
    clearTimeout(noticeTimer);
    const prev = (capPrev && capPrev.state) || 'hidden';
    capPrev = null;
    setState(prev === 'open' ? 'open' : hovering || prev === 'peek' ? 'peek' : 'hidden');
    // Back to the tab that was showing, not to whatever app is behind the island.
    const tab = document.querySelector(`.tab[data-app="${app}"]`);
    if (tab && state !== 'hidden') tab.focus({ preventScroll: true });
  }

  const URL_RE = /^https?:\/\/[^\s/$.?#][^\s]*$/i;
  const PATH_RE = /^(?:[A-Za-z]:[\\/]|\\\\|\/)/;
  const unquote = (t) => t.replace(/^(["'])(.*)\1$/, '$2');
  const baseName = (p) => p.split(/[\\/]/).filter(Boolean).pop() || p;

  // {kind: 'link', url} | {kind: 'file', path, name, data} | {kind: 'text', text}
  async function classify(raw) {
    const t = raw.trim();
    if (URL_RE.test(t)) {
      try { const u = new URL(t); if (u.protocol === 'http:' || u.protocol === 'https:') return { kind: 'link', url: t }; } catch (_) {}
    }
    const path = unquote(t);
    const invoke = tauriInvoke();
    if (PATH_RE.test(path) && !/[\r\n]/.test(path) && invoke) {
      try {
        const data = await invoke('read_capture_file', { path });
        return { kind: 'file', path, name: baseName(path), data };
      } catch (e) {
        const why = String((e && e.message) || e);
        if (why !== 'missing') throw new Error(why);    // there is a file, Lumi just can't take it
      }
    }
    return { kind: 'text', text: t };                  // includes paths to nothing
  }

  async function sendCapture(item) {
    if (item.kind === 'file') {
      await uploadOne(new File([item.data], item.name));
      return `Added to library: ${item.name}`;
    }
    const r = item.kind === 'link'
      ? await req('quick_capture', { kind: 'link', url: item.url }, 120000)
      : await req('quick_capture', { kind: 'text', text: item.text }, 30000);
    if (!r.ok) throw new Error(r.error || 'not saved');
    return item.kind === 'link' ? `Added to library: ${r.name}` : 'Saved to memory';
  }

  function stashedCaptures() {
    try { const v = JSON.parse(localStorage.getItem(PENDING_KEY) || '[]'); return Array.isArray(v) ? v : []; } catch (_) { return []; }
  }
  function setStashedCaptures(list) {
    try { if (list.length) localStorage.setItem(PENDING_KEY, JSON.stringify(list)); else localStorage.removeItem(PENDING_KEY); } catch (_) {}
  }
  // A file is stashed by its path and read again when it's sent.
  const stashable = (item) => (item.kind === 'file' ? { kind: 'file', path: item.path }
    : item.kind === 'link' ? { kind: 'link', url: item.url } : { kind: 'text', text: item.text });

  async function submitCapture() {
    const input = $('qc-input'), btn = $('qc-send');
    const raw = input.value.trim();
    if (!raw || btn.disabled) return;
    btn.disabled = true;
    try {
      const item = await classify(raw);
      if (!online()) {
        setStashedCaptures([...stashedCaptures(), { ...stashable(item), at: Date.now() }]);
        input.value = '';
        notice('Lumi is offline — saved locally', 'warn');
        return;
      }
      const text = await sendCapture(item);
      input.value = '';
      notice(text, 'ok');
    } catch (e) {
      notice(`Couldn't save: ${(e && e.message) || e}`, 'error');
    } finally {
      btn.disabled = false;
    }
  }

  // Sent in order, one combined notice. A capture that fails for good is
  // dropped (and counted); one cut off by a disconnect waits for next time.
  async function flushCaptures() {
    if (flushing) return;
    let list = stashedCaptures();
    if (!list.length) return;
    flushing = true;
    let ok = 0, failed = 0;
    try {
      while (list.length && online()) {
        const p = list[0];
        try {
          const item = p.kind === 'file' ? await classify(p.path) : p;
          await sendCapture(item);
          ok++;
        } catch (e) {
          if (!online() || /offline|timeout/.test(String(e && e.message))) break;
          failed++;
        }
        list = list.slice(1);
        setStashedCaptures(list);
      }
    } finally {
      flushing = false;
    }
    if (ok || failed) {
      const n = ok + failed;
      notice(failed ? `Saved ${ok} of ${n} offline capture${n === 1 ? '' : 's'}; ${failed} couldn't be saved`
                    : `Saved ${ok} capture${ok === 1 ? '' : 's'} from while Lumi was offline`, failed ? 'warn' : 'ok');
    }
  }

  $('qc-form').addEventListener('submit', (e) => { e.preventDefault(); submitCapture(); });
  $('qc-input').addEventListener('input', () => { clearTimeout(noticeTimer); });

  // The hotkey: the saved one (or the default), re-registered on every start.
  async function applyHotkey(accel) {
    const invoke = tauriInvoke();
    if (!invoke) return { ok: false, text: 'The shortcut only works in the island app.' };
    try {
      const got = await invoke('set_capture_shortcut', { accel });
      const wanted = accel || 'Alt+Space';
      return { ok: true, got, text: got === wanted ? `Quick Capture: ${got}` : `${wanted} is taken; using ${got}.` };
    } catch (e) { return { ok: false, text: String((e && e.message) || e) }; }
  }
  $('set-hotkey-save').addEventListener('click', async () => {
    const accel = $('set-hotkey').value.trim();
    const r = await applyHotkey(accel);
    if (r.ok) store.set(HOTKEY_KEY, accel);
    $('hotkey-msg').textContent = r.text;
    $('hotkey-msg').classList.toggle('bad', !r.ok);
  });
  $('gear').addEventListener('click', () => {
    $('set-hotkey').value = store.get(HOTKEY_KEY, '');
    $('hotkey-msg').textContent = ''; $('hotkey-msg').classList.remove('bad');
  });
  if (window.__TAURI__ && window.__TAURI__.event) window.__TAURI__.event.listen('quick-capture', openCapture);
  applyHotkey(store.get(HOTKEY_KEY, ''));

  // ── Focus Mode ─────────────────────────────────────────────────
  // Started by the user only. While it runs (not while paused) Lumi's
  // heartbeat and screen sampler are paused (heartbeat_set_paused, which
  // expires on its own a few minutes after the block would end), and
  // others' learning notices are held back. At the end: the day's count
  // (localStorage, one key per day) and a one-line summary card.
  const FOCUS_PREFIX = 'lumi-island-focus-';
  const PAUSE_GRACE_MIN = 5;
  let focus = { running: false, paused: false, minutes: 0, endsAt: 0, left: 0, activeMs: 0, since: 0, tick: null };

  const focusKey = (d = new Date()) => FOCUS_PREFIX + ymd(d);
  const focusCount = () => parseInt(store.get(focusKey(), '0'), 10) || 0;
  function pruneFocusDays() {
    const keep = new Set([...Array(14)].map((_, i) => focusKey(addDays(new Date(), -i))));
    try {
      for (let i = localStorage.length - 1; i >= 0; i--) {
        const k = localStorage.key(i);
        if (k && /^lumi-island-focus-\d{4}-\d{2}-\d{2}$/.test(k) && !keep.has(k)) localStorage.removeItem(k);
      }
    } catch (_) {}
  }
  const mmss = (ms) => { const s = Math.max(0, Math.ceil(ms / 1000)); return `${pad(Math.floor(s / 60))}:${pad(s % 60)}`; };
  const remaining = () => (focus.paused ? focus.left : focus.endsAt - Date.now());

  function backendPause(paused, minutes) {
    if (!online()) return;
    req('heartbeat_set_paused', { paused, minutes: Math.min(180, Math.ceil(minutes || 0) + PAUSE_GRACE_MIN), perception: true })
      .catch(() => {});
  }
  function onFocusOnline() {
    if (gaming && focus.running) return endFocus('gaming');
    if (focus.running && !focus.paused) backendPause(true, remaining() / 60000);
  }

  function renderFocus() {
    const n = focusCount();
    $('focus-today').textContent = `${n} block${n === 1 ? '' : 's'} today.`;
    $('focus-idle').hidden = focus.running;
    $('focus-run').hidden = !focus.running;
    const chip = $('focus-chip');
    chip.hidden = !focus.running;
    if (!focus.running) return;
    const left = mmss(remaining());
    $('focus-time').textContent = left;
    $('focus-pause').textContent = focus.paused ? 'Resume' : 'Pause';
    chip.textContent = focus.paused ? `Focus paused · ${left}` : `Focus · ${left}`;
    chip.classList.toggle('paused', focus.paused);
  }

  function startFocus(minutes) {
    if (focus.running) return;
    if (gaming) return notice("Focus can't start while Gaming Mode is on", 'warn');
    const now = Date.now();
    focus = { running: true, paused: false, minutes, endsAt: now + minutes * 60000, left: 0, activeMs: 0, since: now, tick: null };
    $('focus-result').hidden = true;
    backendPause(true, minutes);
    focus.tick = setInterval(() => { if (remaining() <= 0) endFocus('done'); else renderFocus(); }, 1000);
    renderFocus();
  }
  function pauseFocus() {
    if (!focus.running) return;
    const now = Date.now();
    if (focus.paused) {                                   // resume
      focus.paused = false; focus.endsAt = now + focus.left; focus.since = now;
      backendPause(true, focus.left / 60000);
    } else {                                              // a break: Lumi's proactive side comes back
      focus.paused = true; focus.left = focus.endsAt - now; focus.activeMs += now - focus.since;
      backendPause(false, 0);
    }
    renderFocus();
  }
  async function endFocus(reason) {
    if (!focus.running) return;
    clearInterval(focus.tick);
    const now = Date.now();
    if (!focus.paused) focus.activeMs += now - focus.since;
    const activeMin = focus.activeMs / 60000;
    focus = { ...focus, running: false, paused: false, tick: null };
    backendPause(false, 0);
    if (reason === 'done') store.set(focusKey(), String(focusCount() + 1));
    renderFocus();
    if (reason === 'gaming') notice('Focus stopped — Gaming Mode on.', 'warn');
    const dur = activeMin >= 1 ? `${Math.round(activeMin)} min` : `${Math.max(1, Math.round(focus.activeMs / 1000))} s`;
    const out = $('focus-result');
    out.hidden = false;
    out.textContent = `Focus block ${reason === 'done' ? 'complete' : 'stopped'} — ${dur}.`;
    if (!online()) return;
    try {
      const r = await req('focus_summary', { minutes: activeMin }, 30000);
      if (r.summary) out.textContent = `You were working on: ${r.summary.replace(/[.!?]*$/, '')}.`;
    } catch (_) { /* keep the plain line */ }
  }

  document.querySelectorAll('#focus-idle .fbtn').forEach((b) => b.addEventListener('click', () => startFocus(+b.dataset.min)));
  $('focus-pause').addEventListener('click', pauseFocus);
  $('focus-stop').addEventListener('click', () => endFocus('stopped'));
  pruneFocusDays();
  renderFocus();

  // ── Hub: shortcuts, quick notes, today's checklist ─────────────
  // Shortcuts live on Lumi's PC (modules/shortcuts_manager.py, data/shortcuts.json)
  // and are cached here so the grid still works while Lumi is offline. A tile
  // opens in the default browser through Rust (open_external_url), never in the
  // island. Notes go to Lumi's memory (quick_note); offline they wait in
  // localStorage like Quick Capture. The checklist is this island's own: the
  // items are kept, the ticks reset every day.
  const SC_CACHE_KEY = 'lumi-island-shortcuts';
  const NOTES_PENDING_KEY = 'lumi-island-notes-pending';
  const CHECK_ITEMS_KEY = 'lumi-island-checklist';
  const CHECK_DONE_PREFIX = 'lumi-island-checks-';
  const DEFAULT_CHECKS = ['Drink water', 'Check emails', 'Review today\'s plan', 'Stretch for 5 minutes'];
  const NOTE_LABEL = { idea: 'Idea', homework: 'Homework', grocery: 'Grocery' };
  const TILE_TINTS = ['--teal', '--ice', '--pink', '--violet', '--amber'];
  let hubEditing = false;

  const readJson = (k, d) => { try { const v = JSON.parse(localStorage.getItem(k) || 'null'); return v == null ? d : v; } catch (_) { return d; } };
  const writeJson = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch (_) {} };
  let shortcuts = (readJson(SC_CACHE_KEY, []) || []).filter((s) => s && s.id && s.url);

  function setShortcuts(list) {
    if (!Array.isArray(list)) return;
    shortcuts = list.filter((s) => s && s.id && typeof s.url === 'string');
    writeJson(SC_CACHE_KEY, shortcuts);
    renderGrid();
  }
  async function loadShortcuts() {
    if (!online()) return renderGrid();
    try { setShortcuts((await req('get_shortcuts')).shortcuts); } catch (_) { renderGrid(); }
  }

  const hostOf = (u) => { try { return new URL(u).hostname.replace(/^www\./, ''); } catch (_) { return u; } };
  function tintFor(text) {
    let h = 0;
    for (const ch of String(text)) h = (h * 31 + ch.codePointAt(0)) >>> 0;
    return `var(${TILE_TINTS[h % TILE_TINTS.length]})`;
  }

  async function openLink(url) {
    const invoke = tauriInvoke();
    try {
      if (invoke) await invoke('open_external_url', { url });
      else window.open(url, '_blank', 'noopener,noreferrer');      // opened as a page in a browser (development)
    } catch (e) {
      notice(String((e && e.message) || e), 'error');
    }
  }

  function renderGrid() {
    const ul = $('hub-grid');
    ul.textContent = '';
    ul.classList.toggle('editing', hubEditing);
    for (const s of shortcuts) {
      const li = document.createElement('li');
      li.className = 'hub-cell';
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'hub-tile';
      b.style.setProperty('--tint', tintFor(s.title || s.url));
      b.title = `${s.title} — ${hostOf(s.url)}`;
      b.setAttribute('aria-label', hubEditing ? `Remove ${s.title}` : `Open ${s.title} in your browser`);
      const icon = span('hub-icon', (s.title || hostOf(s.url)).trim().charAt(0).toUpperCase() || '•');
      icon.setAttribute('aria-hidden', 'true');
      b.append(icon, span('hub-name', s.title || hostOf(s.url)));
      b.addEventListener('click', () => (hubEditing ? removeShortcut(s) : openLink(s.url)));
      if (hubEditing) {
        const x = span('hub-x', '×');
        x.setAttribute('aria-hidden', 'true');
        b.append(x);
      }
      li.append(b);
      ul.append(li);
    }
    const li = document.createElement('li');
    li.className = 'hub-cell';
    const add = document.createElement('button');
    add.type = 'button';
    add.className = 'hub-tile hub-tile-add';
    add.setAttribute('aria-label', 'Add a shortcut');
    const plus = span('hub-icon', '+');
    plus.setAttribute('aria-hidden', 'true');
    add.append(plus, span('hub-name', 'Add'));
    add.addEventListener('click', openAddForm);
    li.append(add);
    ul.append(li);
  }

  function openAddForm() {
    if (hubEditing) setEditing(false);
    $('hub-add').hidden = false;
    $('hub-add-msg').textContent = ''; $('hub-add-msg').className = 'hub-msg';
    setTimeout(() => $('hub-add-title').focus(), 30);
  }
  function closeAddForm() {
    $('hub-add').hidden = true;
    $('hub-add-title').value = ''; $('hub-add-url').value = '';
  }
  async function saveShortcut() {
    const title = $('hub-add-title').value.trim();
    const url = $('hub-add-url').value.trim();
    const msg = $('hub-add-msg');
    if (!url) { msg.textContent = 'Enter a link.'; msg.className = 'hub-msg bad'; return; }
    if (!online()) { msg.textContent = 'Lumi is offline. Shortcuts are saved on her PC.'; msg.className = 'hub-msg bad'; return; }
    $('hub-add-save').disabled = true;
    try {
      const r = await req('shortcut_add', { title, url });
      if (!r.ok) throw new Error(r.error || 'not saved');
      setShortcuts(r.shortcuts);
      closeAddForm();
      notice(`Added ${r.shortcut.title}`, 'ok');
    } catch (e) {
      msg.textContent = (e && e.message) || String(e); msg.className = 'hub-msg bad';
    } finally {
      $('hub-add-save').disabled = false;
    }
  }
  async function removeShortcut(s) {
    if (!online()) return notice('Lumi is offline — shortcuts are changed on her PC', 'warn');
    try {
      const r = await req('shortcut_delete', { sid: s.id });
      if (r.shortcuts) setShortcuts(r.shortcuts);
      if (!r.ok) throw new Error(r.error || 'not removed');
      notice(`Removed ${s.title}`, 'ok');
    } catch (e) {
      notice(`Couldn't remove it: ${(e && e.message) || e}`, 'error');
    }
  }
  function setEditing(on) {
    hubEditing = !!on;
    $('hub-edit').textContent = hubEditing ? 'Done' : 'Edit';
    $('hub-edit').setAttribute('aria-pressed', String(hubEditing));
    renderGrid();
  }

  // Quick notes. The kind shown is a guess (the server makes the same one);
  // a "hw:" / "buy:" / "idea:" prefix picks it outright.
  function guessKind(t) {
    const m = /^\s*(idea|hw|homework|todo|grocery|groceries|buy|shopping)\s*[:-]/i.exec(t);
    if (m) { const w = m[1].toLowerCase(); return w === 'idea' ? 'idea' : ['hw', 'homework', 'todo'].includes(w) ? 'homework' : 'grocery'; }
    if (/\b(homework|hw|assignment|essay|quiz|exam|midterm|final|lab \d+|due|canvas|chapter|problem set|pset|study|project)\b/i.test(t)) return 'homework';
    if (/\b(buy|grocer(y|ies)|milk|eggs|bread|rice|chicken|beef|fruit|veggies|vegetables|cheese|butter|coffee|snacks|toilet paper|shopping list|pick up)\b/i.test(t)) return 'grocery';
    return 'idea';
  }
  function showKind() {
    const k = guessKind($('hub-note-input').value);
    const el = $('hub-note-kind');
    el.dataset.kind = k; el.textContent = NOTE_LABEL[k];
  }
  async function sendNote(text) {
    const r = await req('quick_note', { text }, 30000);
    if (!r.ok) throw new Error(r.error || 'not saved');
    return r.kind;
  }
  async function submitNote() {
    const input = $('hub-note-input'), btn = $('hub-note-send');
    const text = input.value.trim();
    if (!text || btn.disabled) return;
    if (!online()) {
      writeJson(NOTES_PENDING_KEY, [...readJson(NOTES_PENDING_KEY, []), { text, at: Date.now() }]);
      input.value = ''; showKind();
      return notice('Lumi is offline — saved here, sending when she\'s back', 'warn');
    }
    btn.disabled = true;
    try {
      const kind = await sendNote(text);
      input.value = ''; showKind();
      notice(`Saved to Lumi's memory · ${NOTE_LABEL[kind] || 'Note'}`, 'ok');
    } catch (e) {
      notice(`Couldn't save: ${(e && e.message) || e}`, 'error');
    } finally {
      btn.disabled = false;
    }
  }
  let flushingNotes = false;
  async function flushNotes() {
    if (flushingNotes) return;
    let list = readJson(NOTES_PENDING_KEY, []);
    if (!Array.isArray(list) || !list.length) return;
    flushingNotes = true;
    let ok = 0;
    try {
      while (list.length && online()) {
        try { await sendNote(String(list[0].text || '')); ok++; } catch (e) {
          if (!online() || /offline|timeout/.test(String(e && e.message))) break;
        }
        list = list.slice(1);
        if (list.length) writeJson(NOTES_PENDING_KEY, list); else localStorage.removeItem(NOTES_PENDING_KEY);
      }
    } finally {
      flushingNotes = false;
    }
    if (ok) notice(`Saved ${ok} note${ok === 1 ? '' : 's'} from while Lumi was offline`, 'ok');
  }

  // Daily checklist
  const checkItems = () => {
    const v = readJson(CHECK_ITEMS_KEY, null);
    return Array.isArray(v) ? v : DEFAULT_CHECKS.map((text, i) => ({ id: `d${i}`, text }));
  };
  const doneKey = () => CHECK_DONE_PREFIX + ymd(new Date());
  const doneSet = () => new Set(readJson(doneKey(), []));
  function pruneCheckDays() {
    const keep = new Set([...Array(7)].map((_, i) => CHECK_DONE_PREFIX + ymd(addDays(new Date(), -i))));
    try {
      for (let i = localStorage.length - 1; i >= 0; i--) {
        const k = localStorage.key(i);
        if (k && k.startsWith(CHECK_DONE_PREFIX) && !keep.has(k)) localStorage.removeItem(k);
      }
    } catch (_) {}
  }
  function renderChecks() {
    const items = checkItems(), done = doneSet();
    const ul = $('hub-list');
    ul.textContent = '';
    for (const it of items) {
      const li = document.createElement('li');
      li.className = `hub-check${done.has(it.id) ? ' done' : ''}`;
      const label = document.createElement('label');
      const box = Object.assign(document.createElement('input'), { type: 'checkbox', checked: done.has(it.id) });
      box.addEventListener('change', () => toggleCheck(it.id, box.checked));
      label.append(box, span('hub-check-text', it.text));
      const del = Object.assign(document.createElement('button'), { type: 'button', className: 'hub-check-del', textContent: '×' });
      del.setAttribute('aria-label', `Remove "${it.text}" from the list`);
      del.addEventListener('click', () => removeCheck(it.id));
      li.append(label, del);
      ul.append(li);
    }
    const n = items.filter((it) => done.has(it.id)).length;
    $('hub-progress').textContent = items.length ? `${n}/${items.length}` : 'empty';
    $('hub-meter').style.width = items.length ? `${Math.round((n / items.length) * 100)}%` : '0';
    $('hub-progress').classList.toggle('all', !!items.length && n === items.length);
  }
  function toggleCheck(id, on) {
    const d = doneSet();
    if (on) d.add(id); else d.delete(id);
    writeJson(doneKey(), [...d]);
    renderChecks();
  }
  function removeCheck(id) {
    writeJson(CHECK_ITEMS_KEY, checkItems().filter((it) => it.id !== id));
    renderChecks();
  }
  function addCheck(text) {
    const t = text.replace(/\s+/g, ' ').trim().slice(0, 80);
    if (!t) return;
    writeJson(CHECK_ITEMS_KEY, [...checkItems(), { id: `c${Date.now().toString(36)}`, text: t }]);
    renderChecks();
  }

  function greeting() {
    const h = new Date().getHours();
    return h < 5 ? 'Up late' : h < 12 ? 'Good morning' : h < 17 ? 'Good afternoon' : 'Good evening';
  }
  function renderHub() {
    $('hub-hello').textContent = greeting();
    $('hub-sub').textContent = new Date().toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric' });
    renderGrid();
    renderChecks();
  }

  $('hub-edit').addEventListener('click', () => setEditing(!hubEditing));
  $('hub-add').addEventListener('submit', (e) => { e.preventDefault(); saveShortcut(); });
  $('hub-add-cancel').addEventListener('click', closeAddForm);
  $('hub-add').addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.stopPropagation(); closeAddForm(); } });
  $('hub-note').addEventListener('submit', (e) => { e.preventDefault(); submitNote(); });
  $('hub-note-input').addEventListener('input', showKind);
  $('hub-check-add').addEventListener('submit', (e) => {
    e.preventDefault();
    addCheck($('hub-check-input').value);
    $('hub-check-input').value = '';
  });
  pruneCheckDays();

  // ── Media: a watch-style player ────────────────────────────────
  // Backend: modules/media_bridge.py (Spotify, or whatever Windows shows in its
  // media flyout). State is PUSHED (media_state) while this tab is open; a
  // media_get every 15 s keeps the bridge polling. Progress is drawn locally
  // between pushes. Three panes: Library | Now playing | Up next, moved with the
  // dots, ←/→, a sideways scroll or a swipe. The mouse wheel over the island is
  // the volume crown. Gaming Mode (data-gaming) drops the blur and animations.
  const RING = 2 * Math.PI * 56;
  let mx = { state: null, art: '', view: 1, shelf: 'playlists', lib: null, heartbeat: null, tick: null,
             volPending: 0, volTimer: null, volShowTimer: null, searchTimer: null };

  const msClock = (ms) => { const s = Math.max(0, Math.floor((ms || 0) / 1000)); return `${Math.floor(s / 60)}:${pad(s % 60)}`; };
  const svgUse = (d) => `<svg viewBox="0 0 24 24" aria-hidden="true">${d}</svg>`;
  const PLAY_D = '<path d="M7 4v16l13-8z"/>', PAUSE_D = '<path d="M6 4h4v16H6zM14 4h4v16h-4z"/>';

  function mediaOpen() {
    mediaHeartbeat();
    clearInterval(mx.heartbeat);
    mx.heartbeat = setInterval(mediaHeartbeat, 15000);
    clearInterval(mx.tick);
    mx.tick = setInterval(drawProgress, 1000);
    mediaView(mx.view, true);
  }
  function mediaClose() {
    clearInterval(mx.heartbeat); mx.heartbeat = null;
    clearInterval(mx.tick); mx.tick = null;
  }
  async function mediaHeartbeat() {
    if (!online()) return drawMedia();
    try { const r = await req('media_get'); if (r.ok !== false) setMedia(r.state); } catch (_) { /* pushes still arrive */ }
  }

  function setMedia(st) {
    if (!st) return;
    if (st.art !== undefined) mx.art = st.art || '';
    mx.state = { ...st, _recv: Date.now() };
    drawMedia();
  }

  function mxMsg(text, bad) {
    const m = $('mx-msg');
    m.textContent = text || '';
    m.classList.toggle('bad', !!bad);
    clearTimeout(mxMsg.t);
    if (text) mxMsg.t = setTimeout(() => { m.textContent = ''; }, 4000);
  }

  function drawMedia() {
    const st = mx.state;
    const has = !!(st && st.source);
    const art = $('mx-art');
    const imgOk = has && /^data:image\/(png|jpeg);base64,/.test(mx.art);
    art.replaceChildren();
    if (imgOk) { const img = document.createElement('img'); img.src = mx.art; img.alt = ''; art.append(img); }
    else art.innerHTML = svgUse('<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>');
    $('mx-bg').style.backgroundImage = imgOk ? `url("${mx.art}")` : 'none';
    $('mx-title').textContent = has ? st.title || 'Unknown' : (online() ? 'Nothing playing' : 'Lumi is offline');
    $('mx-artist').textContent = has ? [st.artists, st.album].filter(Boolean).join(' · ') : '';
    const sp = (st && st.spotify) || {};
    $('mx-src').textContent = !has ? (sp.connected ? 'Spotify · idle' : 'Play something to control it here')
      : `${st.app || 'Media'}${st.device ? ' · ' + st.device : ''}`;
    // ticker: scroll only when the title doesn't fit
    const tk = $('mx-ticker'), title = $('mx-title');
    requestAnimationFrame(() => tk.classList.toggle('scroll', title.scrollWidth > tk.clientWidth + 4));

    const can = (st && st.can) || {};
    const playing = has && st.playing;
    $('mx-play').innerHTML = svgUse(playing ? PAUSE_D : PLAY_D);
    $('mx-play').setAttribute('aria-label', playing ? 'Pause' : 'Play');
    for (const [id, ok] of [['mx-play', can.control], ['mx-prev', can.prev && can.control], ['mx-next', can.next && can.control],
                            ['mx-shuffle', can.shuffle], ['mx-repeat', can.repeat], ['mx-like', can.like]]) {
      $(id).disabled = !ok;
    }
    $('mx-shuffle').setAttribute('aria-pressed', String(!!(st && st.shuffle)));
    const rep = (st && st.repeat) || 'off';
    $('mx-repeat').setAttribute('aria-pressed', String(rep !== 'off'));
    $('mx-repeat-one').hidden = rep !== 'track';
    $('mx-like').setAttribute('aria-pressed', String(!!(st && st.liked)));
    $('mx-like').textContent = st && st.liked ? '✓' : '+';
    $('mx-like').title = st && st.liked ? 'In your Liked Songs' : 'Save to Liked Songs';
    const vol = st && st.volume;
    $('mx-volnum').textContent = vol == null ? '—' : `${vol}%`;
    $('mx-volfill').style.width = vol == null ? '0' : `${vol}%`;
    island.classList.toggle('mx-playing', !!playing && st.source === 'spotify');   // the record spins for music, not a video thumbnail
    drawProgress();
    if (sp.needs_reconnect && !mxMsg.shownReconnect) {
      mxMsg.shownReconnect = true;
      mxMsg('Reconnect Spotify in Settings → Connections to use Liked Songs and podcasts.');
    }
  }

  function drawProgress() {
    const st = mx.state;
    const ring = $('mx-ring');
    $('app-media').classList.toggle('mx-live', !!(st && st.source && !st.duration_ms));
    if (!st || !st.duration_ms) {
      ring.style.strokeDashoffset = String(RING);
      $('mx-pos').textContent = st && st.source && !st.duration_ms ? 'Live' : '0:00';
      $('mx-dur').textContent = st && st.duration_ms ? msClock(st.duration_ms) : '';
      return;
    }
    const pos = Math.min(st.duration_ms, st.progress_ms + (st.playing ? Date.now() - st._recv : 0));
    ring.style.strokeDasharray = String(RING);
    ring.style.strokeDashoffset = String(RING * (1 - pos / st.duration_ms));
    $('mx-pos').textContent = msClock(pos);
    $('mx-dur').textContent = msClock(st.duration_ms);
  }

  async function mediaCmd(action, value) {
    if (!online()) return mxMsg('Lumi is offline.', true);
    try {
      const r = await req('media_cmd', { action, value }, 20000);
      if (r.ok === false) throw new Error(r.error || 'That didn’t work.');
      setMedia(r.state);
      if (r.message) mxMsg(r.message);
      return true;
    } catch (e) { mxMsg(e.message, true); return false; }
  }

  // views
  function mediaView(i, instant) {
    mx.view = Math.max(0, Math.min(2, i));
    const views = $('mx-views');
    views.classList.toggle('instant', !!instant);
    views.style.transform = `translateX(${-mx.view * 100 / 3}%)`;
    document.querySelectorAll('.mx-dot').forEach((d) => {
      const on = +d.dataset.view === mx.view;
      d.classList.toggle('on', on); d.setAttribute('aria-selected', String(on));
    });
    document.querySelectorAll('.mx-pane').forEach((p, n) => { p.inert = n !== mx.view; });
    if (mx.view === 0) loadLibrary();
    if (mx.view === 2) loadUpNext();
  }

  function row(item, actions) {
    const li = document.createElement('li');
    li.className = 'mx-row';
    const art = document.createElement('span');
    art.className = 'mx-row-art';
    art.textContent = (item.title || '?').trim().charAt(0).toUpperCase();
    const text = document.createElement('span');
    text.className = 'mx-row-text';
    text.append(span('mx-row-title', item.title || ''), span('mx-row-sub', item.subtitle || item.kind || ''));
    li.append(art, text);
    for (const [label, glyph, fn] of actions) {
      const b = document.createElement('button');
      b.type = 'button'; b.className = 'mx-row-btn'; b.textContent = glyph;
      b.setAttribute('aria-label', `${label}: ${item.title}`); b.title = label;
      b.addEventListener('click', fn);
      li.append(b);
    }
    return li;
  }
  function emptyLine(ul, text) { ul.replaceChildren(li('mx-empty', text)); }

  async function loadLibrary() {
    const ul = $('mx-lib-list');
    if (!online()) return emptyLine(ul, 'Lumi is offline.');
    const q = $('mx-search').value.trim();
    if (!mx.lib || q) emptyLine(ul, q ? 'Searching…' : 'Loading your library…');
    try {
      const r = await req('media_library', { q }, 20000);
      if (r.ok === false) throw new Error(r.error);
      if (!r.connected) return emptyLine(ul, 'Connect Spotify in Lumi’s Settings → Connections to browse your library.');
      if (!q) mx.lib = r;
      drawLibrary(q ? r.results : null);
    } catch (e) { emptyLine(ul, `Couldn’t load: ${e.message}`); }
  }
  function drawLibrary(results) {
    const ul = $('mx-lib-list');
    const items = results || (mx.lib && mx.lib[mx.shelf]) || [];
    ul.replaceChildren();
    if (!results && mx.shelf === 'liked' && items.length) {
      const all = row({ title: 'Play Liked Songs', subtitle: `${items.length} recent saves`, kind: 'playlist' },
        [['Play', '▶', () => mediaCmd('play_liked')]]);
      all.classList.add('mx-row-feature');
      ul.append(all);
    }
    for (const it of items) {
      const acts = [['Play', '▶', async () => { if (await mediaCmd('play_uri', it.uri)) mediaView(1); }]];
      if (it.kind === 'track' || it.kind === 'episode') acts.push(['Add to queue', '+', () => mediaCmd('queue_uri', it.uri)]);
      ul.append(row(it, acts));
    }
    if (!ul.children.length) emptyLine(ul, results ? 'No matches.' : 'Nothing here yet.');
  }

  async function loadUpNext() {
    const qu = $('mx-queue'), dv = $('mx-devices');
    if (!online()) { emptyLine(qu, 'Lumi is offline.'); dv.replaceChildren(); return; }
    emptyLine(qu, 'Loading…');
    try {
      const r = await req('media_next', {}, 20000);
      if (r.ok === false) throw new Error(r.error);
      if (!r.connected) { emptyLine(qu, 'Spotify isn’t connected.'); dv.replaceChildren(); return; }
      qu.replaceChildren();
      for (const it of (r.queue || []).slice(0, 20)) {
        qu.append(row(it, [['Play now', '▶', async () => { if (await mediaCmd('play_uri', it.uri)) { mediaView(1); } }]]));
      }
      if (!qu.children.length) emptyLine(qu, r.queue_error ? `Couldn’t read the queue: ${r.queue_error}` : 'Nothing queued. Add songs from the library.');
      dv.replaceChildren();
      for (const d of r.devices || []) {
        const item = row({ title: d.name, subtitle: d.active ? `${d.type} · playing here` : d.type, kind: 'device' },
          d.active ? [] : [['Play on this device', '⇄', async () => { if (await mediaCmd('transfer', d.id)) loadUpNext(); }]]);
        if (d.active) item.classList.add('mx-row-feature');
        dv.append(item);
      }
      if (!dv.children.length) emptyLine(dv, 'Open Spotify on a device to see it here.');
    } catch (e) { emptyLine(qu, `Couldn’t load: ${e.message}`); }
  }

  // volume crown: the wheel anywhere on the island, summed so a fast spin is one request
  function showVolBubble(v) {
    const b = $('mx-vol');
    b.textContent = `${v}%`;
    b.classList.add('on');
    clearTimeout(mx.volShowTimer);
    mx.volShowTimer = setTimeout(() => b.classList.remove('on'), 900);
  }
  island.addEventListener('wheel', (e) => {
    if (app !== 'media' || state !== 'open' || mx.view !== 1) return;
    if (Math.abs(e.deltaX) > Math.abs(e.deltaY)) return;       // sideways: the pane swipe below
    const st = mx.state;
    if (!st || st.volume == null) return;
    e.preventDefault();
    mx.volPending += e.deltaY < 0 ? 5 : -5;
    const target = Math.max(0, Math.min(100, st.volume + mx.volPending));
    showVolBubble(target);
    $('mx-volfill').style.width = `${target}%`; $('mx-volnum').textContent = `${target}%`;
    clearTimeout(mx.volTimer);
    mx.volTimer = setTimeout(async () => {
      mx.volPending = 0;
      await mediaCmd('volume', String(target));
    }, 180);
  }, { passive: false });

  // sideways scroll / swipe between panes
  let swipeX = null, swipeLock = 0;
  $('mx-views').addEventListener('wheel', (e) => {
    if (Math.abs(e.deltaX) <= Math.abs(e.deltaY) || Math.abs(e.deltaX) < 12 || Date.now() < swipeLock) return;
    e.preventDefault();
    swipeLock = Date.now() + 450;
    mediaView(mx.view + (e.deltaX > 0 ? 1 : -1));
  }, { passive: false });
  $('mx-views').addEventListener('pointerdown', (e) => { if (e.target.closest('button, input')) return; swipeX = e.clientX; });
  window.addEventListener('pointerup', (e) => {
    if (swipeX == null) return;
    const dx = e.clientX - swipeX; swipeX = null;
    if (Math.abs(dx) > 50) mediaView(mx.view + (dx < 0 ? 1 : -1));
  });
  $('app-media').addEventListener('keydown', (e) => {
    if (e.target.matches('input')) return;
    if (e.key === 'ArrowRight') { e.preventDefault(); mediaView(mx.view + 1); }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); mediaView(mx.view - 1); }
    else if (e.key === ' ' && mx.view === 1 && !e.target.matches('button')) { e.preventDefault(); mediaCmd('toggle'); }
  });
  document.querySelectorAll('.mx-dot').forEach((d) => d.addEventListener('click', () => mediaView(+d.dataset.view)));
  $('mx-play').addEventListener('click', () => mediaCmd('toggle'));
  $('mx-prev').addEventListener('click', () => mediaCmd('previous'));
  $('mx-next').addEventListener('click', () => mediaCmd('next'));
  $('mx-shuffle').addEventListener('click', () => mediaCmd('shuffle'));
  $('mx-repeat').addEventListener('click', () => mediaCmd('repeat'));
  $('mx-like').addEventListener('click', () => mediaCmd('like'));
  document.querySelectorAll('.mx-chip').forEach((c) => c.addEventListener('click', () => {
    mx.shelf = c.dataset.shelf;
    document.querySelectorAll('.mx-chip').forEach((o) => { o.classList.toggle('on', o === c); o.setAttribute('aria-selected', String(o === c)); });
    $('mx-search').value = '';
    if (mx.lib) drawLibrary(); else loadLibrary();
  }));
  $('mx-search').addEventListener('input', () => {
    clearTimeout(mx.searchTimer);
    mx.searchTimer = setTimeout(() => { if ($('mx-search').value.trim()) loadLibrary(); else drawLibrary(); }, 400);
  });
  drawMedia();

  // ── Go ─────────────────────────────────────────────────────────
  setApp('home');
  drawCal();
  renderHome();
  render();
  fit(WIN_W, SIZES.hidden[1]);
  connect();
})();

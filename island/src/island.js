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
  const SIZES = { hidden: [200, 8], peek: [330, 58], open: [480, 590], approval: [440, 310] };
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
  let app = 'home';                // home | chat | calendar
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

  function setState(next) {
    if (next === state) return;
    const [w, h] = SIZES[next];
    const [cw, ch] = SIZES[state];
    const grows = w >= cw && h >= ch;
    if (grows) fit(w, h);                         // room first, then animate into it
    else fit(Math.max(w, cw), Math.max(h, ch));
    state = next;
    island.className = `island ${next}${island.classList.contains('danger') ? ' danger' : ''}`;
    $('approval').inert = next !== 'approval';
    if (!grows) setTimeout(() => { if (state === next) fit(w, h); }, MORPH_MS);
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
  document.addEventListener('visibilitychange', syncVideo);

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
        render(); loadHome(); loadChat();
        if (app === 'calendar') loadCalendar();
        break;
      case 'lumi_status':
        if (STATUS_TEXT[msg.state]) { lumiState = msg.state; render(); }
        break;
      case 'agent_update':
        agents = msg.agents || agents; renderPipeline();
        break;
      case 'lumi_mode':
        gaming = !!msg.gaming; render();
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
                       agent: ['main', 'minecraft', 'office'].includes(msg.agent) ? msg.agent : 'main' });
          renderApproval();
        }
        break;
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
    app = name;
    document.querySelectorAll('.tab').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.app === name)));
    document.querySelectorAll('.app').forEach((s) => s.classList.toggle('on', s.id === `app-${name}`));
    syncClock();
    if (name === 'home') loadHome();
    if (name === 'chat') { scrollMsgs(true); if (state === 'open') focusComposer(); }
    if (name === 'calendar') loadCalendar();
    if (name === 'pipeline') renderPipeline();
  }
  function openApp(name) { setApp(name); setState(queue.length ? 'approval' : 'open'); }

  $('avatar').addEventListener('click', () => (state === 'open' ? collapse() : openApp(app)));
  document.querySelectorAll('.tab').forEach((b) => b.addEventListener('click', () => openApp(b.dataset.app)));
  $('collapse').addEventListener('click', collapse);

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
    if (state !== 'open') return;
    blurTimer = setTimeout(() => {
      if (state === 'open' && !document.hasFocus() && !$('input').value.trim() && $('settings').hidden) collapse();
    }, BLUR_CLOSE_MS);
  });
  window.addEventListener('focus', () => clearTimeout(blurTimer));
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && state === 'open') collapse(); });

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

  // ── Pipeline ───────────────────────────────────────────────────
  const AGENTS = [['main', 'Main'], ['minecraft', 'Minecraft'], ['office', 'Office']];
  const STATUS_LABEL = { idle: 'Idle', thinking: 'Thinking', executing: 'Executing', waiting_approval: 'Needs you', error: 'Error' };
  let agents = {};
  const cards = {};

  function card(id, name) {
    const c = el('article', 'agent-card s-idle');
    c.dataset.agent = id;
    const head = el('header', 'ac-head');
    const badge = el('span', 'badge');
    head.append(el('span', 'ac-name', name), badge);
    const stat = el('div', 'ac-status');
    const task = el('div', 'ac-task');
    const bar = el('div', 'bar');
    const fill = el('i');
    bar.append(fill);
    const model = el('div', 'ac-model');
    const ask = el('div', 'ac-ask');
    const askTool = el('div', 'ac-tool');
    const yes = el('button', 'btn btn-approve', 'Approve'); yes.type = 'button';
    const no = el('button', 'btn btn-decline', 'Decline'); no.type = 'button';
    yes.addEventListener('click', () => c.dataset.req && respond('approve', c.dataset.req));
    no.addEventListener('click', () => c.dataset.req && respond('decline', c.dataset.req));
    ask.append(askTool, no, yes);
    c.append(head, stat, task, bar, model, ask);
    return { c, badge, stat, task, fill, model, ask, askTool };
  }

  function renderPipeline() {
    const host = $('pipe');
    if (!host.childElementCount) {
      for (const [id, name] of AGENTS) { cards[id] = card(id, name); host.append(cards[id].c); }
    }
    for (const [id] of AGENTS) {
      const a = agents[id] || { status: 'idle', task: '', progress: 0, model: null, model_name: null };
      const k = cards[id];
      const req = queue.find((r) => r.agent === id);
      const status = req ? 'waiting_approval' : (STATUS_LABEL[a.status] ? a.status : 'idle');
      k.c.className = `agent-card s-${status}${req ? ' expanded' : ''}`;
      k.badge.className = `badge${a.model ? ' b-' + a.model : ''}`;
      k.badge.textContent = a.model ? a.model.toUpperCase() : '—';
      k.stat.textContent = STATUS_LABEL[status];
      k.task.textContent = req ? `Wants to run ${req.tool}` : (a.task || (status === 'idle' ? 'Nothing to do' : ''));
      const pct = status === 'idle' ? 0 : Math.max(0, Math.min(100, Number(a.progress) || 0));
      k.fill.style.width = `${status === 'executing' && !pct ? 50 : pct}%`;
      k.model.textContent = a.model_name || '';
      k.c.dataset.req = req ? req.id : '';
      k.askTool.textContent = req ? req.tool : '';
      k.ask.hidden = !req;
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
    $('set-host').focus();
  }
  function closeSettings() { $('settings').hidden = true; }
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

  // ── Go ─────────────────────────────────────────────────────────
  setApp('home');
  drawCal();
  renderHome();
  render();
  fit(...SIZES.hidden);
  connect();
})();

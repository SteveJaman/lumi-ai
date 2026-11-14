/* ============================================================
   Workspace — Google Calendar, Gmail and KSU mail.
   Two entry points for app.js:
     renderWorkspace(main)        the Workspace view (Agenda / Inbox)
     renderConnectionsTab(panel)  Settings → Connections
   app.js is the only importer; it calls init() with the helpers
   this file shares with the rest of the client.
   Everything the server sends is written with textContent, never
   as HTML: mail and event titles are written by strangers.
   ============================================================ */

let C = null;       // { $, state, api, navigate, openSettingsTab }
export function init(ctx) { C = ctx; }

const DAYS = 14;
const SEARCH_SOON_MS = 2500;       // re-check once after "refreshing"
const MAX_SOON_TRIES = 3;

let _timers = new Set();
let _seq = { agenda: 0, inbox: 0, reader: 0 };
let _tab = 'agenda';
let _filter = 'all';

function later(fn, ms) {
  const t = setTimeout(() => { _timers.delete(t); fn(); }, ms);
  _timers.add(t);
  return t;
}
/** Run fn every `ms` until it returns true, `maxMs` passes, or `root` leaves the page. */
function poll(root, fn, ms, maxMs) {
  const start = Date.now();
  const t = setInterval(async () => {
    if (!root.isConnected || Date.now() - start > maxMs) { clearInterval(t); _timers.delete(t); return; }
    let done = false;
    try { done = await fn(); } catch { /* keep polling */ }
    if (done) { clearInterval(t); _timers.delete(t); }
  }, ms);
  _timers.add(t);
}
/** Called by app.js when the user leaves the view. */
export function stop() {
  for (const t of _timers) { clearTimeout(t); clearInterval(t); }
  _timers = new Set();
  _seq = { agenda: 0, inbox: 0, reader: 0 };
}

/* ---------------- small helpers ---------------- */

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}
function button(label, cls = 'btn btn-sm', onClick) {
  const b = el('button', cls, label);
  b.type = 'button';
  if (onClick) b.addEventListener('click', onClick);
  return b;
}
function httpsOnly(url) { return typeof url === 'string' && url.startsWith('https://') ? url : ''; }

function ago(epochSec) {
  if (!epochSec) return '';
  const s = Math.max(0, Date.now() / 1000 - epochSec);
  if (s < 45) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}
function startOfDay(d) { const x = new Date(d); x.setHours(0, 0, 0, 0); return x; }
function sameDay(a, b) { return startOfDay(a).getTime() === startOfDay(b).getTime(); }
function dayLabel(d) {
  const today = startOfDay(new Date());
  const diff = Math.round((startOfDay(d) - today) / 86400000);
  const date = d.toLocaleDateString([], { weekday: 'long', month: 'short', day: 'numeric' });
  return diff === 0 ? `Today · ${date}` : diff === 1 ? `Tomorrow · ${date}` : date;
}
function when(s, allDay) { return allDay ? new Date(`${s}T00:00:00`) : new Date(s); }
function clock(d) { return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }); }
function mailDate(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  return sameDay(d, new Date()) ? clock(d) : d.toLocaleDateString([], { month: 'short', day: 'numeric' });
}
function pad(n) { return String(n).padStart(2, '0'); }
function localDate(d) { return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; }
function localTime(d) { return `${pad(d.getHours())}:${pad(d.getMinutes())}`; }

function notice(root, message, { retry, connections } = {}) {
  root.replaceChildren();
  const box = el('div', 'ws-notice');
  box.append(el('div', 'ws-notice-text', message));
  const row = el('div', 'ws-notice-actions');
  if (connections) row.append(button('Open Connections', 'btn btn-sm btn-primary', () => C.openSettingsTab('connections')));
  if (retry) row.append(button('Try again', 'btn btn-sm', retry));
  if (row.childElementCount) box.append(row);
  root.append(box);
}

/* ============================================================
   Workspace view
   ============================================================ */

export async function renderWorkspace(main) {
  stop();
  main.innerHTML = `
    <div class="page">
      <div class="page-header">
        <div>
          <div class="page-title">Workspace</div>
          <div class="page-sub">Your calendar and inbox. Cached, so it opens instantly.</div>
        </div>
        <button class="btn btn-sm" id="ws-refresh">Refresh</button>
      </div>
      <div class="page-scroll">
        <div class="ws" id="ws-root">
          <div class="ws-tabs" role="tablist">
            <button class="ws-tab" role="tab" data-tab="agenda">Agenda</button>
            <button class="ws-tab" role="tab" data-tab="inbox">Inbox</button>
          </div>
          <div id="ws-body"></div>
        </div>
      </div>
    </div>`;
  const root = C.$('#ws-root');
  root.querySelectorAll('.ws-tab').forEach(b => b.addEventListener('click', () => showTab(b.dataset.tab)));
  C.$('#ws-refresh').addEventListener('click', () => (_tab === 'agenda' ? loadAgenda(true) : loadInbox(true)));
  showTab(_tab);
}

function showTab(tab) {
  stop();
  _tab = tab;
  document.querySelectorAll('.ws-tab').forEach(b => b.setAttribute('aria-selected', String(b.dataset.tab === tab)));
  const body = C.$('#ws-body');
  if (!body) return;
  body.replaceChildren();
  if (tab === 'agenda') { buildAgendaShell(body); loadAgenda(false); }
  else { buildInboxShell(body); loadInbox(false); }
}

/* ---------------- Agenda ---------------- */

function buildAgendaShell(body) {
  const now = new Date();
  const startDefault = new Date(now); startDefault.setHours(now.getHours() + 1, 0, 0, 0);
  const endDefault = new Date(startDefault.getTime() + 3600000);

  const form = el('form', 'card ws-quick');
  form.noValidate = true;
  form.innerHTML = `
    <div class="card-header"><div class="card-title">Add an event</div></div>
    <div class="ws-quick-grid">
      <label class="ws-field ws-grow"><span>Title</span><input class="settings-input" name="title" maxlength="200" placeholder="Study group" required></label>
      <label class="ws-field"><span>Date</span><input class="settings-input" name="date" type="date" required></label>
      <label class="ws-field"><span>Start</span><input class="settings-input" name="start" type="time"></label>
      <label class="ws-field"><span>End</span><input class="settings-input" name="end" type="time"></label>
      <label class="ws-field ws-grow"><span>Location</span><input class="settings-input" name="location" maxlength="200" placeholder="Optional"></label>
    </div>
    <div class="ws-quick-foot">
      <label class="ws-check"><input type="checkbox" name="all_day"> All day</label>
      <span class="ws-form-msg" role="status"></span>
      <button class="btn btn-primary btn-sm" type="submit">Add event</button>
    </div>`;
  form.elements.date.value = localDate(startDefault);
  form.elements.start.value = localTime(startDefault);
  form.elements.end.value = localTime(endDefault);
  const toggleTimes = () => {
    const allDay = form.elements.all_day.checked;
    form.elements.start.disabled = form.elements.end.disabled = allDay;
  };
  form.elements.all_day.addEventListener('change', toggleTimes);
  form.addEventListener('submit', (e) => { e.preventDefault(); createEvent(form); });

  const meta = el('div', 'ws-meta', '');
  meta.id = 'ws-agenda-meta';
  const list = el('div', 'ws-agenda');
  list.id = 'ws-agenda-list';
  body.append(form, meta, list);
}

async function createEvent(form) {
  const msg = form.querySelector('.ws-form-msg');
  const submit = form.querySelector('button[type=submit]');
  const f = form.elements;
  const title = f.title.value.trim();
  const allDay = f.all_day.checked;
  const say = (t, bad) => { msg.textContent = t; msg.classList.toggle('bad', !!bad); };
  if (!title) return say('Give the event a title.', true);
  if (!f.date.value) return say('Pick a date.', true);
  let payload;
  if (allDay) {
    payload = { title, start: f.date.value, end: f.date.value, all_day: true };
  } else {
    if (!f.start.value || !f.end.value) return say('Set a start and end time.', true);
    if (f.end.value <= f.start.value) return say('The event must end after it starts.', true);
    payload = { title, start: `${f.date.value}T${f.start.value}`, end: `${f.date.value}T${f.end.value}`, all_day: false };
  }
  payload.location = f.location.value.trim();
  payload.timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  submit.disabled = true;
  say('Adding…');
  try {
    await C.api('/api/workspace/calendar/events', { method: 'POST', body: payload, csrf: true });
    f.title.value = '';
    f.location.value = '';
    say('Added.');
    later(() => { if (msg.isConnected) say(''); }, 2500);
    loadAgenda(true);
  } catch (e) {
    say(e.message, true);
  } finally {
    submit.disabled = false;
  }
}

async function loadAgenda(force, attempt = 0) {
  const list = C.$('#ws-agenda-list');
  const meta = C.$('#ws-agenda-meta');
  if (!list) return;
  const mine = ++_seq.agenda;
  if (!list.childElementCount) list.append(el('div', 'ws-loading', 'Loading…'));
  let res;
  try {
    res = await C.api(`/api/workspace/calendar?days=${DAYS}${force ? '&refresh=1' : ''}`);
  } catch (e) {
    if (mine !== _seq.agenda) return;
    meta.textContent = '';
    return notice(list, e.message, { connections: e.status === 409, retry: e.status === 409 ? null : () => loadAgenda(true) });
  }
  if (mine !== _seq.agenda) return;
  drawAgenda(list, res.events);
  meta.textContent = res.refreshing ? 'Updating…' : `Updated ${ago(res.at)}`;
  if (res.refreshing && attempt < MAX_SOON_TRIES) later(() => loadAgenda(false, attempt + 1), SEARCH_SOON_MS);
}

function drawAgenda(list, events) {
  list.replaceChildren();
  if (!events.length) return list.append(el('div', 'ws-empty', `Nothing on the calendar for the next ${DAYS} days.`));
  const today = startOfDay(new Date());
  const groups = new Map();
  for (const ev of events) {
    const start = when(ev.start, ev.all_day);
    const key = localDate(start < today ? today : start);      // already-started events sit under Today
    if (!groups.has(key)) groups.set(key, { day: start < today ? today : start, items: [] });
    groups.get(key).items.push({ ev, start });
  }
  for (const { day, items } of groups.values()) {
    const sec = el('section', 'ws-day');
    sec.append(el('h3', 'ws-day-title', dayLabel(day)));
    for (const { ev, start } of items) sec.append(eventRow(ev, start));
    list.append(sec);
  }
}

function eventRow(ev, start) {
  const wrap = el('div', 'ws-event');
  const head = el('button', 'ws-event-head');
  head.type = 'button';
  head.setAttribute('aria-expanded', 'false');
  const time = ev.all_day ? 'All day' : `${clock(start)} – ${clock(new Date(ev.end))}`;
  const main = el('div', 'ws-event-main');
  main.append(el('div', 'ws-event-title', ev.title));
  if (ev.location) main.append(el('div', 'ws-event-sub', ev.location));
  head.append(el('div', 'ws-event-time', time), main);

  const details = el('div', 'ws-event-details');
  details.hidden = true;
  if (ev.description) details.append(el('p', 'ws-event-desc', ev.description));
  if (ev.attendees.length) {
    details.append(el('div', 'ws-event-sub', 'Guests: ' + ev.attendees.map(a => a.name || a.email).join(', ')));
  }
  const link = httpsOnly(ev.link);
  if (link) {
    const a = el('a', 'integration-link', 'Open in Google Calendar ↗');
    a.href = link; a.target = '_blank'; a.rel = 'noopener noreferrer';
    details.append(a);
  }
  if (!details.childElementCount) details.append(el('div', 'ws-event-sub', 'No more details.'));
  head.addEventListener('click', () => {
    details.hidden = !details.hidden;
    head.setAttribute('aria-expanded', String(!details.hidden));
  });
  wrap.append(head, details);
  return wrap;
}

/* ---------------- Inbox ---------------- */

let _messages = [];
let _selected = null;       // { provider, id }

function buildInboxShell(body) {
  const bar = el('div', 'ws-inbox-bar');
  const chips = el('div', 'ws-chips');
  chips.id = 'ws-chips';
  for (const [id, label] of [['all', 'All'], ['gmail', 'Gmail'], ['ksu', 'KSU']]) {
    const c = el('button', 'filter-chip' + (id === _filter ? ' active' : ''), label);
    c.type = 'button';
    c.dataset.filter = id;
    c.addEventListener('click', () => {
      _filter = id;
      chips.querySelectorAll('.filter-chip').forEach(x => x.classList.toggle('active', x.dataset.filter === id));
      loadInbox(false);
    });
    chips.append(c);
  }
  const meta = el('div', 'ws-meta');
  meta.id = 'ws-inbox-meta';
  bar.append(chips, meta);

  const errors = el('div', 'ws-errors');
  errors.id = 'ws-inbox-errors';
  const grid = el('div', 'ws-inbox');
  const list = el('div', 'ws-list card');
  list.id = 'ws-list';
  const reader = el('div', 'ws-reader card');
  reader.id = 'ws-reader';
  reader.append(el('div', 'ws-empty', 'Select a message to read it.'));
  grid.append(list, reader);
  body.append(bar, errors, grid);
}

async function loadInbox(force, attempt = 0) {
  const list = C.$('#ws-list');
  if (!list) return;
  const meta = C.$('#ws-inbox-meta');
  const errors = C.$('#ws-inbox-errors');
  const mine = ++_seq.inbox;
  if (!_messages.length) { list.replaceChildren(el('div', 'ws-loading', 'Loading…')); }
  let res;
  try {
    res = await C.api(`/api/workspace/mail?filter=${_filter}${force ? '&refresh=1' : ''}`);
  } catch (e) {
    if (mine !== _seq.inbox) return;
    meta.textContent = '';
    errors.replaceChildren();
    return notice(list, e.message, { connections: e.status === 409, retry: e.status === 409 ? null : () => loadInbox(true) });
  }
  if (mine !== _seq.inbox) return;
  _messages = res.messages;
  errors.replaceChildren(...Object.entries(res.errors || {}).map(([p, m]) =>
    el('div', 'integration-message ws-error', `${p === 'ksu' ? 'KSU' : 'Gmail'}: ${m}`)));
  drawList();
  meta.textContent = res.refreshing ? 'Updating…' : `Updated ${ago(res.at)}`;
  if (res.refreshing && attempt < MAX_SOON_TRIES) later(() => loadInbox(false, attempt + 1), SEARCH_SOON_MS);
}

function drawList() {
  const list = C.$('#ws-list');
  if (!list) return;
  list.replaceChildren();
  if (!_messages.length) return list.append(el('div', 'ws-empty', 'Your inbox is empty.'));
  for (const m of _messages) {
    const row = el('button', 'ws-row' + (m.unread ? ' unread' : '') +
      (_selected && _selected.id === m.id && _selected.provider === m.provider ? ' selected' : ''));
    row.type = 'button';
    row.dataset.id = m.id;
    const top = el('div', 'ws-row-top');
    top.append(el('span', 'ws-dot'), el('span', 'ws-from', m.from_name),
               el('span', `ws-badge ws-badge-${m.provider}`, m.provider === 'ksu' ? 'KSU' : 'Gmail'),
               el('span', 'ws-date', mailDate(m.ts)));
    row.append(top, el('div', 'ws-subject', m.subject), el('div', 'ws-snippet', m.snippet));
    row.addEventListener('click', () => openMessage(m));
    list.append(row);
  }
}

async function openMessage(m) {
  _selected = { provider: m.provider, id: m.id };
  drawList();
  const reader = C.$('#ws-reader');
  const mine = ++_seq.reader;
  reader.replaceChildren(el('div', 'ws-loading', 'Opening…'));
  let full;
  try {
    full = await C.api(`/api/workspace/mail/${m.provider}/${encodeURIComponent(m.id)}`);
  } catch (e) {
    if (mine !== _seq.reader) return;
    return notice(reader, e.message, { retry: () => openMessage(m) });
  }
  if (mine !== _seq.reader) return;
  drawReader(reader, m, full);
}

function drawReader(reader, m, full) {
  reader.replaceChildren();
  const head = el('div', 'ws-reader-head');
  head.append(el('h2', 'ws-reader-subject', full.subject),
              el('div', 'ws-reader-meta', `${full.from} · ${full.provider === 'ksu' ? 'KSU' : 'Gmail'}`));
  const status = el('div', 'ws-reader-status');
  status.setAttribute('role', 'status');

  const actions = el('div', 'ws-reader-actions');
  const readBtn = button(m.unread ? 'Mark read' : 'Mark unread', 'btn btn-sm', async () => {
    readBtn.disabled = true;
    try {
      await C.api(`/api/workspace/mail/${m.provider}/${encodeURIComponent(m.id)}/read`,
                  { method: 'POST', body: { read: m.unread }, csrf: true });
      m.unread = !m.unread;
      readBtn.textContent = m.unread ? 'Mark read' : 'Mark unread';
      drawList();
    } catch (e) { status.textContent = e.message; }
    readBtn.disabled = false;
  });
  const archiveBtn = button('Archive', 'btn btn-sm', async () => {
    archiveBtn.disabled = true;
    try {
      await C.api(`/api/workspace/mail/${m.provider}/${encodeURIComponent(m.id)}/archive`, { method: 'POST', body: {}, csrf: true });
      _messages = _messages.filter(x => !(x.id === m.id && x.provider === m.provider));
      _selected = null;
      drawList();
      reader.replaceChildren(el('div', 'ws-empty', 'Archived.'));
    } catch (e) { status.textContent = e.message; archiveBtn.disabled = false; }
  });

  const askWrap = el('div', 'ws-ask');
  const askBtn = button('Ask Lumi ▾', 'btn btn-sm btn-primary');
  askBtn.setAttribute('aria-haspopup', 'menu');
  askBtn.setAttribute('aria-expanded', 'false');
  const menu = el('div', 'ws-menu');
  menu.setAttribute('role', 'menu');
  menu.hidden = true;
  const ask = async (action) => {
    menu.hidden = true;
    askBtn.setAttribute('aria-expanded', 'false');
    askBtn.disabled = true;
    status.textContent = 'Sending to Lumi…';
    try {
      await C.api(`/api/workspace/mail/${m.provider}/${encodeURIComponent(m.id)}/ask`,
                  { method: 'POST', body: { action }, csrf: true });
      C.navigate('chat');
    } catch (e) { status.textContent = e.message; askBtn.disabled = false; }
  };
  for (const [action, label] of [['summarize', 'Summarize this thread'], ['draft_reply', 'Draft a reply']]) {
    const item = button(label, 'ws-menu-item', () => ask(action));
    item.setAttribute('role', 'menuitem');
    menu.append(item);
  }
  askBtn.addEventListener('click', () => {
    menu.hidden = !menu.hidden;
    askBtn.setAttribute('aria-expanded', String(!menu.hidden));
  });
  askWrap.append(askBtn, menu);
  actions.append(readBtn, archiveBtn, askWrap);

  const thread = el('div', 'ws-thread');
  for (const part of full.messages) {
    const msg = el('article', 'ws-msg');
    msg.append(el('div', 'ws-msg-head', `${part.from}${part.date ? ' · ' + part.date : ''}`), el('div', 'ws-msg-body', part.body || '(empty)'));
    thread.append(msg);
  }
  reader.append(head, actions, status, thread);
}

/* ============================================================
   Settings → Connections
   ============================================================ */

export async function renderConnectionsTab(panel) {
  stop();
  panel.replaceChildren();
  panel.append(el('h2', 'settings-section-title', 'Connections'));
  panel.append(el('div', 'settings-help',
    'Connect your accounts so Lumi can show your calendar and mail and help with them. ' +
    'Sign-ins are stored on this PC, in data/credentials, and never leave it except to talk to Google or Microsoft. ' +
    'Note: "Ask Lumi" and Lumi’s email tools send message text to your chat model. ' +
    'If that model is a cloud provider, the text leaves this PC.'));
  const holder = el('div', 'ws-conn');
  panel.append(holder);
  await refreshConnections(holder);
}

async function refreshConnections(holder) {
  let st;
  try {
    st = await C.api('/api/workspace/status');
  } catch (e) {
    return notice(holder, `Couldn’t load connections: ${e.message}`, { retry: () => refreshConnections(holder) });
  }
  holder.replaceChildren(googleCard(holder, st), ksuCard(holder, st));
}

function card(title, emoji, state, stateLabel, desc) {
  const c = el('div', 'integration-card ws-card');
  const head = el('div', 'integration-header');
  const titleWrap = el('div', 'integration-title-wrap');
  titleWrap.append(el('div', 'integration-name', title), el('span', `integration-status state-${state}`, stateLabel));
  head.append(el('div', 'integration-emoji', emoji), titleWrap);
  c.append(head, el('div', 'integration-desc', desc));
  return c;
}
function msgLine(text, bad) { return el('div', 'integration-message' + (bad ? ' ws-error' : ''), text); }

function googleCard(holder, st) {
  const g = st.google;
  const state = g.connected ? 'ok' : g.client_configured ? 'warn' : 'off';
  const label = g.connected ? 'connected' : g.client_configured ? 'not signed in' : 'needs setup';
  const c = card('Google · Calendar + Gmail', '📅', state, label,
    'Shows your upcoming events and inbox, lets you add events, and gives Lumi calendar and email tools.');
  const actions = el('div', 'integration-actions');
  const status = el('div', 'ws-form-msg', '');
  status.setAttribute('role', 'status');
  const again = () => refreshConnections(holder);

  if (g.error) c.append(msgLine(g.error, true));

  if (g.connected) {
    c.append(msgLine(`Signed in as ${g.email || 'your Google account'}.`));
    const missing = [['calendar', 'Calendar'], ['gmail', 'Gmail'], ['send', 'sending mail']].filter(([k]) => !g.services[k]).map(([, n]) => n);
    if (missing.length) c.append(msgLine(`Some permissions weren’t granted (${missing.join(', ')}). Disconnect and connect again, ticking every box.`, true));
    actions.append(button('Disconnect', 'btn btn-sm btn-danger', async () => {
      if (!confirm('Disconnect Google? Lumi will lose access to your calendar and Gmail until you sign in again.')) return;
      try { await C.api('/api/workspace/google/disconnect', { method: 'POST', body: {}, csrf: true }); } catch (e) { status.textContent = e.message; return; }
      again();
    }));
  } else if (!g.client_configured) {
    const steps = el('ol', 'ws-steps');
    for (const t of [
      'In Google Cloud Console, create a project and turn on the Gmail API and the Google Calendar API.',
      'Under Credentials, create an OAuth client ID of type “Desktop app”. (On the consent screen, add yourself as a test user, or publish the app so sign-ins don’t expire weekly.)',
      'Download the client JSON and paste its contents here.',
    ]) steps.append(el('li', '', t));
    const ta = el('textarea', 'settings-input ws-json');
    ta.rows = 5;
    ta.placeholder = '{"installed": {"client_id": "…", "client_secret": "…", …}}';
    ta.setAttribute('aria-label', 'Google OAuth client JSON');
    const save = button('Save client', 'btn btn-sm btn-primary', async () => {
      save.disabled = true; status.classList.remove('bad'); status.textContent = 'Checking…';
      try {
        await C.api('/api/workspace/google/client', { method: 'POST', body: { json: ta.value }, csrf: true });
        again();
      } catch (e) { status.textContent = e.message; status.classList.add('bad'); save.disabled = false; }
    });
    const link = el('a', 'integration-link', 'Open Google Cloud Console ↗');
    link.href = 'https://console.cloud.google.com/apis/credentials'; link.target = '_blank'; link.rel = 'noopener noreferrer';
    c.append(steps, ta);
    actions.append(save, link);
  } else {
    c.append(msgLine('Your OAuth client is saved. Sign in to finish.'));
    const connect = button('Connect Google', 'btn btn-sm btn-primary', async () => {
      connect.disabled = true; status.classList.remove('bad'); status.textContent = 'Opening Google…';
      let res;
      try { res = await C.api('/api/workspace/google/connect', { method: 'POST', body: {}, csrf: true }); }
      catch (e) { status.textContent = e.message; status.classList.add('bad'); connect.disabled = false; return; }
      if (!res.auth_url.startsWith('https://accounts.google.com/')) { status.textContent = 'Unexpected sign-in address; not opening it.'; return; }
      window.open(res.auth_url, '_blank');
      status.textContent = 'Finish signing in on the Google tab. This page updates by itself.';
      poll(holder, async () => {
        const s = await C.api('/api/workspace/status');
        if (s.google.connected) { again(); return true; }
        return false;
      }, 2000, 180000);
      connect.disabled = false;
    });
    actions.append(connect);
  }
  c.append(actions, status);
  return c;
}

function ksuCard(holder, st) {
  const k = st.ksu;
  const device = k.graph.device || {};
  const pending = device.status === 'pending';
  const state = k.connected ? 'ok' : pending ? 'warn' : 'off';
  const label = k.connected ? 'connected' : pending ? 'waiting for you' : 'not connected';
  const c = card('KSU Student Mail', '🎓', state, label,
    'Adds your KSU inbox next to Gmail. Connect through Microsoft 365, or, if the university blocks outside apps, read KSU mail through a Gmail label.');
  const actions = el('div', 'integration-actions');
  const status = el('div', 'ws-form-msg', '');
  status.setAttribute('role', 'status');
  const again = () => refreshConnections(holder);
  const fail = (e) => { status.textContent = e.message; status.classList.add('bad'); };

  if (k.error && !k.connected) c.append(msgLine(k.error, true));

  if (k.connected) {
    const how = k.mode === 'graph' ? 'Microsoft 365' : `Gmail label “${k.label}”`;
    c.append(msgLine(`Connected${k.account ? ' as ' + k.account : ''} through ${how}.`));
    actions.append(button('Disconnect', 'btn btn-sm btn-danger', async () => {
      if (!confirm('Disconnect KSU mail?')) return;
      try { await C.api('/api/workspace/ksu/disconnect', { method: 'POST', body: {}, csrf: true }); } catch (e) { return fail(e); }
      again();
    }));
  } else if (pending) {
    const box = el('div', 'ws-device');
    box.append(el('div', 'ws-device-help', 'On any device, open the link below and enter this code:'),
               el('div', 'ws-device-code', device.user_code));
    const uri = httpsOnly(device.verification_uri);
    if (uri) {
      const a = el('a', 'integration-link', uri.replace('https://', '') + ' ↗');
      a.href = uri; a.target = '_blank'; a.rel = 'noopener noreferrer';
      box.append(a);
    }
    c.append(box);
    actions.append(button('Cancel', 'btn btn-sm', async () => {
      try { await C.api('/api/workspace/ksu/disconnect', { method: 'POST', body: {}, csrf: true }); } catch (e) { return fail(e); }
      again();
    }));
    poll(holder, async () => {
      const d = await C.api('/api/workspace/ksu/device');
      if (d.status !== 'pending') { again(); return true; }
      return false;
    }, 3000, 900000);
  } else {
    const seg = el('div', 'ws-seg');
    const bodyGraph = el('div', 'ws-seg-body');
    const bodyGmail = el('div', 'ws-seg-body');
    const pick = (which) => {
      bodyGraph.hidden = which !== 'graph';
      bodyGmail.hidden = which !== 'gmail';
      seg.querySelectorAll('.filter-chip').forEach(b => b.classList.toggle('active', b.dataset.which === which));
    };
    for (const [which, text] of [['graph', 'Microsoft 365'], ['gmail', 'Gmail label (fallback)']]) {
      const b = el('button', 'filter-chip', text);
      b.type = 'button'; b.dataset.which = which;
      b.addEventListener('click', () => pick(which));
      seg.append(b);
    }
    // Microsoft 365
    const idIn = field('Application (client) ID', k.graph.client_id, 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx');
    const tenantIn = field('Tenant', k.graph.tenant || 'organizations', 'organizations');
    const go = button('Connect KSU', 'btn btn-sm btn-primary', async () => {
      go.disabled = true; status.classList.remove('bad'); status.textContent = 'Starting…';
      try {
        await C.api('/api/workspace/ksu/connect', { method: 'POST', csrf: true,
          body: { mode: 'graph', client_id: idIn.input.value.trim(), tenant: tenantIn.input.value.trim() } });
        again();
      } catch (e) { fail(e); go.disabled = false; }
    });
    bodyGraph.append(el('p', 'ws-seg-help',
      'Register a free app at entra.microsoft.com (a “public client” with Mail.ReadWrite, Mail.Send and User.Read) and paste its Application ID. ' +
      'If KSU asks for administrator approval, use the Gmail label fallback.'), idIn.wrap, tenantIn.wrap, go);
    // Gmail label
    const labelIn = field('Gmail label that holds your KSU mail', k.label || 'KSU', 'KSU');
    const useLabel = button('Use this label', 'btn btn-sm btn-primary', async () => {
      useLabel.disabled = true; status.classList.remove('bad'); status.textContent = 'Saving…';
      try {
        await C.api('/api/workspace/ksu/connect', { method: 'POST', body: { mode: 'gmail', label: labelIn.input.value.trim() }, csrf: true });
        again();
      } catch (e) { fail(e); useLabel.disabled = false; }
    });
    useLabel.disabled = !st.google.connected;
    bodyGmail.append(el('p', 'ws-seg-help',
      'In KSU mail, forward your messages to your Gmail address. In Gmail, add a filter that applies one label to them, and type that label here. ' +
      'Replying as KSU isn’t possible this way. Lumi can read, archive and summarize.' +
      (st.google.connected ? '' : ' Connect Google first.')), labelIn.wrap, useLabel);
    c.append(seg, bodyGraph, bodyGmail);
    pick(k.mode === 'gmail' ? 'gmail' : 'graph');
  }
  c.append(actions, status);
  return c;
}

function field(label, value, placeholder) {
  const wrap = el('label', 'ws-field');
  wrap.append(el('span', '', label));
  const input = el('input', 'settings-input');
  input.value = value || '';
  input.placeholder = placeholder || '';
  wrap.append(input);
  return { wrap, input };
}

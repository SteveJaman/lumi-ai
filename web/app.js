/* ============================================================
   Lumi web client — SPA router, auth, chat, dashboard, memory,
   persona editor, models.
   Polling-based; no Socket.IO dependency on the client.
   The login / register flow lives on its own page (/login).
   ============================================================ */

const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  user:    null,
  csrf:    null,
  host:    null,
  history: [],
  events:  [],
  status:  {},
  view:    'chat',
  _agentToggling: false,
};

async function api(path, { method = 'GET', body, csrf = false } = {}) {
  const headers = { 'Content-Type': 'application/json' };
  if (csrf && state.csrf) headers['X-CSRF-Token'] = state.csrf;
  const res = await fetch(path, {
    method,
    headers,
    credentials: 'same-origin',
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON */ }
  if (!res.ok) {
    const msg = (data && data.error) || `HTTP ${res.status}`;
    const err = new Error(msg);
    err.status = res.status;
    throw err;
  }
  return data;
}

const ICONS = {
  chat:      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>',
  dashboard: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="9"/><rect x="14" y="3" width="7" height="5"/><rect x="14" y="12" width="7" height="9"/><rect x="3" y="16" width="7" height="5"/></svg>',
  persona:   '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 4-6 8-6s8 2 8 6"/></svg>',
  memory:    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a4 4 0 0 0-4 4v1H7a3 3 0 0 0 0 6v1a3 3 0 0 0 3 3h1v1a3 3 0 0 0 6 0v-1h1a3 3 0 0 0 3-3v-1a3 3 0 0 0 0-6V6a4 4 0 0 0-4-4h-1V1a3 3 0 0 0-6 0z" /></svg>',
  models:    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 9h6v6H9z"/><path d="M9 1v4M15 1v4M9 19v4M15 19v4M1 9h4M1 15h4M19 9h4M19 15h4"/></svg>',
  plugs:     '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 2v6M15 2v6M6 8h12v3a6 6 0 0 1-12 0z"/><path d="M12 17v5"/></svg>',
  settings:  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
};

/* --------------------------------------------------------------
   Auth bootstrap — redirect to /login if not authenticated.
   -------------------------------------------------------------- */
async function checkAuth() {
  try {
    const me = await api('/auth/me');
    state.user = me.user;
    state.csrf = me.csrf;
    state.host = me.host;
    showShell();
  } catch (e) {
    location.replace('/login');
  }
}

async function doLogout() {
  try { await api('/auth/logout', { method: 'POST' }); } catch {}
  location.href = '/login';
}
document.addEventListener('click', (e) => {
  if (e.target.id === 'btn-logout') doLogout();
});

/* --------------------------------------------------------------
   Shell
   -------------------------------------------------------------- */
const NAV = [
  { id: 'chat',         label: 'Chat',         icon: 'chat' },
  { id: 'dashboard',    label: 'Dashboard',    icon: 'dashboard' },
  { id: 'persona',      label: 'Persona',      icon: 'persona' },
  { id: 'memory',       label: 'Memory',       icon: 'memory' },
  { id: 'models',       label: 'Models',       icon: 'models' },
  { id: 'integrations', label: 'Integrations', icon: 'plugs' },
  { id: 'settings',     label: 'Settings',     icon: 'settings' },
];

function showShell() {
  $('#host-name').textContent = state.host?.name || 'Lumi';
  $('#user-name').textContent = state.user.display_name || state.user.username;
  $('#user-avatar').textContent =
    (state.user.display_name || state.user.username)
      .slice(0, 1).toUpperCase();

  const nav = $('#nav');
  nav.innerHTML = '';
  for (const item of NAV) {
    const el = document.createElement('div');
    el.className = 'nav-item' + (item.id === state.view ? ' active' : '');
    el.dataset.view = item.id;
    el.innerHTML = `${ICONS[item.icon]}<span>${item.label}</span>`;
    el.addEventListener('click', () => navigate(item.id));
    nav.appendChild(el);
  }

  navigate(state.view, true);
  startStatusPolling();
}

function navigate(view, replace = false) {
  state.view = view;
  if (view !== 'chat') stopChatPolling();
  if (view !== 'models') stopModelsTimers();
  if (!replace) location.hash = `#/${view}`;
  $$('.nav-item').forEach(el => {
    el.classList.toggle('active', el.dataset.view === view);
  });
  render();
}

window.addEventListener('hashchange', () => {
  const hash = (location.hash || '#/chat').replace('#/', '');
  if (hash !== state.view) navigate(hash, true);
});

/* --------------------------------------------------------------
   Status polling
   -------------------------------------------------------------- */
async function startStatusPolling() {
  const tick = async () => {
    try {
      const s = await api('/api/status');
      state.status = s;
      updateAgentChip();
      updateTypingIndicator();
    } catch {}
    setTimeout(tick, 2000);
  };
  tick();
}

function updateAgentChip() {
  const chip = $('#agent-chip');
  if (!chip) return;
  const on = !!state.status.agent_mode;
  chip.classList.toggle('agent', on);
  chip.textContent = on ? '⚡ Agent mode: ON' : '⚡ Agent mode: OFF';
  chip.title = on
    ? 'Agent mode is ON — Lumi can use tools. Click to disable.'
    : 'Agent mode is OFF — conversation only. Click to enable.';
}

function updateTypingIndicator() {
  const el = $('#chat-typing');
  if (!el) return;
  el.textContent = state.status.ai_thinking
    ? 'Lumi is thinking…'
    : (state.status.ai_speaking ? 'Lumi is speaking…' : '');
}

async function toggleAgentMode() {
  if (state._agentToggling) return;
  state._agentToggling = true;
  const chip = $('#agent-chip');
  if (chip) chip.style.opacity = '0.5';
  try {
    const res = await api('/api/agent/toggle', {
      method: 'POST', body: {}, csrf: true,
    });
    state.status.agent_mode = res.enabled;
    updateAgentChip();
  } catch (e) {
    console.warn('agent toggle failed', e);
    if (chip) {
      const prev = chip.textContent;
      chip.textContent = 'toggle failed';
      setTimeout(() => { chip.textContent = prev; updateAgentChip(); }, 1500);
    }
  } finally {
    state._agentToggling = false;
    if (chip) chip.style.opacity = '';
  }
}

/* --------------------------------------------------------------
   Views
   -------------------------------------------------------------- */
function render() {
  const main = $('#main-content');
  main.innerHTML = '';
  switch (state.view) {
    case 'chat':         return renderChatView(main);
    case 'dashboard':    return renderDashboard(main);
    case 'memory':       return renderMemory(main);
    case 'persona':      return renderPersona(main);
    case 'models':       return renderModels(main);
    case 'integrations': return renderStub(main, 'Integrations',
      'VTS, Discord, browser, Fish Audio — status and config. Phase 10.13.');
    case 'settings':     return renderStub(main, 'Settings',
      'General, security, advanced, about. Phase 10.14.');
  }
}

/* ==============================================================
   CHAT
   ============================================================== */
let _chatPollTimer = null;

function startChatPolling() {
  stopChatPolling();
  refreshHistory();
  _chatPollTimer = setInterval(refreshHistory, 1500);
}

function stopChatPolling() {
  if (_chatPollTimer) {
    clearInterval(_chatPollTimer);
    _chatPollTimer = null;
  }
}

async function refreshHistory() {
  if (state.view !== 'chat') return;
  try {
    const res = await api('/api/history?limit=100');
    const next = res.history || [];
    if (JSON.stringify(next) === JSON.stringify(state.history)) return;
    state.history = next;
    renderChat();
  } catch (e) {
    // Next tick retries.
  }
}

async function renderChatView(main) {
  main.innerHTML = `
    <div class="page">
      <div class="chat-wrap">
        <div class="chat-scroll" id="chat-scroll"></div>
        <div class="chat-status">
          <button class="mode-chip" id="agent-chip"
                  title="Toggle agent mode">⚡ Agent mode: OFF</button>
          <span class="muted" id="chat-typing"></span>
          <span class="grow"></span>
          <span class="muted" id="chat-host"></span>
        </div>
        <div class="chat-input">
          <textarea id="chat-input" rows="1"
            placeholder="Message Lumi — Enter to send, Shift+Enter for newline"></textarea>
          <button class="btn btn-primary" id="chat-send">Send</button>
        </div>
      </div>
    </div>
  `;

  $('#chat-host').textContent = `as ${state.host?.name || ''}`;
  updateAgentChip();
  updateTypingIndicator();

  try {
    const res = await api('/api/history?limit=100');
    state.history = res.history || [];
  } catch {}
  renderChat();

  startChatPolling();

  const input = $('#chat-input');
  input.focus();
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendChat();
    }
  });
  input.addEventListener('input', () => {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 180) + 'px';
  });
  $('#chat-send').addEventListener('click', sendChat);

  $('#agent-chip').addEventListener('click', toggleAgentMode);
}

function renderChat() {
  const scroll = $('#chat-scroll');
  if (!scroll) return;

  const wasAtBottom =
    (scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight) < 80;

  scroll.innerHTML = '';
  state.history.slice(-100).forEach(m => renderChatMessage(m, scroll));
  if (wasAtBottom) scroll.scrollTop = scroll.scrollHeight;
}

function renderChatMessage(msg, container = null) {
  const scroll = container || $('#chat-scroll');
  if (!scroll) return;

  const role = msg.role || 'assistant';
  const div = document.createElement('div');
  div.className = `msg ${role}`;

  const avatar = document.createElement('div');
  avatar.className = 'msg-avatar';
  avatar.textContent = role === 'user'
    ? (state.user?.display_name || '?').slice(0, 1).toUpperCase()
    : '◆';
  div.appendChild(avatar);

  const bubble = document.createElement('div');
  bubble.className = 'msg-bubble';

  const raw = String(msg.content || '');

  const tagRe = /\[([a-zA-Z][a-zA-Z\s\-]{0,30})\]/g;
  const tags = [...raw.matchAll(tagRe)]
    .map(m => m[1].toLowerCase())
    .filter(t => isCanonicalTag(t));
  if (tags.length) {
    const tagRow = document.createElement('div');
    tagRow.className = 'msg-tags';
    tags.forEach(t => {
      const chip = document.createElement('span');
      chip.className = 'tag';
      chip.textContent = `[${t}]`;
      tagRow.appendChild(chip);
    });
    bubble.appendChild(tagRow);
  }

  const cleaned = raw.replace(
    /\[([a-zA-Z][a-zA-Z\s\-]{0,30})\]/g,
    (m, inner) => isCanonicalTag(inner.toLowerCase()) ? '' : m,
  ).replace(/[ \t]{2,}/g, ' ').trim();

  const text = document.createElement('span');
  text.textContent = cleaned;
  bubble.appendChild(text);

  div.appendChild(bubble);
  scroll.appendChild(div);
}

const CANONICAL_TAGS = new Set([
  'happy','sad','angry','excited','calm','nervous','confident','surprised',
  'satisfied','delighted','scared','worried','upset','frustrated','depressed',
  'empathetic','embarrassed','disgusted','moved','proud','relaxed','grateful',
  'curious','sarcastic','disdainful','unhappy','anxious','hysterical',
  'indifferent','uncertain','doubtful','confused','disappointed','regretful',
  'guilty','ashamed','jealous','envious','hopeful','optimistic','pessimistic',
  'nostalgic','lonely','bored','contemptuous','sympathetic','compassionate',
  'determined','resigned','whispering','soft tone','emphasis','shouting',
  'screaming','laughing','chuckling','sobbing','sighing','groaning','panting',
  'gasping','yawning','snoring',
]);
function isCanonicalTag(t) { return CANONICAL_TAGS.has(t); }

async function sendChat() {
  const input = $('#chat-input');
  if (!input) return;
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  input.style.height = 'auto';

  state.history.push({ role: 'user', content: text });
  renderChat();

  try {
    await api('/api/chat/send', {
      method: 'POST',
      body: { text },
      csrf: true,
    });
    refreshHistory();
  } catch (err) {
    state.history.push({
      role: 'system',
      content: `Failed to send: ${err.message}`,
    });
    renderChat();
  }
}

/* ==============================================================
   DASHBOARD
   ============================================================== */
async function renderDashboard(main) {
  main.innerHTML = `
    <div class="page">
      <div class="page-header">
        <div>
          <div class="page-title">Dashboard</div>
          <div class="page-sub">Live diagnostics from every module.</div>
        </div>
        <button class="btn btn-sm" id="dash-refresh">Refresh</button>
      </div>
      <div class="page-scroll">
        <div class="stat-grid" id="dash-stats"></div>
        <div class="event-stream">
          <div class="event-stream-header" id="dash-filters"></div>
          <div class="event-list" id="dash-events"></div>
        </div>
      </div>
    </div>
  `;

  const hubPort = state.status?.log_hub_port || 5001;
  let events = [];
  try {
    const res = await fetch(`http://127.0.0.1:${hubPort}/events`,
                            { cache: 'no-store' });
    events = await res.json();
  } catch {
    events = [];
  }
  state.events = events;

  renderDashboardStats();
  renderEventFilters();
  renderEvents(events);

  $('#dash-refresh').addEventListener('click', () => renderDashboard(main));
}

function renderDashboardStats() {
  const s = state.status || {};
  const stats = [
    { label: 'Host',        value: s.host?.name || '—' },
    { label: 'History',     value: s.history_len ?? '—' },
    { label: 'AI thinking', value: s.ai_thinking ? 'yes' : 'no' },
    { label: 'AI speaking', value: s.ai_speaking ? 'yes' : 'no' },
    { label: 'Agent mode',  value: s.agent_mode ? 'ON' : 'OFF' },
    { label: 'Events',      value: state.events.length },
  ];
  $('#dash-stats').innerHTML = stats.map(st => `
    <div class="stat">
      <div class="stat-label">${st.label}</div>
      <div class="stat-value">${st.value}</div>
    </div>
  `).join('');
}

function renderEventFilters() {
  const cats = ['all','ambient','heartbeat','memory','emotional',
                'mic','vts','system'];
  const el = $('#dash-filters');
  el.innerHTML = cats.map((c, i) =>
    `<button class="filter-chip ${i === 0 ? 'active' : ''}"
             data-cat="${c}">${c}</button>`
  ).join('');
  el.addEventListener('click', (e) => {
    const chip = e.target.closest('.filter-chip');
    if (!chip) return;
    $$('.filter-chip', el).forEach(c => c.classList.remove('active'));
    chip.classList.add('active');
    const cat = chip.dataset.cat;
    const filtered = cat === 'all'
      ? state.events
      : state.events.filter(ev => ev.category === cat);
    renderEvents(filtered);
  });
}

function renderEvents(events) {
  const el = $('#dash-events');
  if (!events.length) {
    el.innerHTML = `<div class="empty" style="padding:24px">
      No events yet.</div>`;
    return;
  }
  el.innerHTML = events.slice(-300).map(ev => `
    <div class="event-row">
      <span class="ts">${ev.time || ''}</span>
      <span class="cat ${ev.category}">${ev.category}</span>
      <span class="msg">${escapeHtml(ev.message)}</span>
    </div>
  `).join('');
  el.scrollTop = el.scrollHeight;
}

/* ==============================================================
   MEMORY BROWSER (Phase 10.11)
   ============================================================== */
const _memoryState = {
  items: [],
  total: 0,
  query: '',
  sort: 'recent',
  expanded: null,
  searchMode: false,
};

async function renderMemory(main) {
  main.innerHTML = `
    <div class="page">
      <div class="page-header">
        <div>
          <div class="page-title">Memory</div>
          <div class="page-sub" id="memory-count">Loading…</div>
        </div>
        <div class="row">
          <button class="btn btn-sm" id="memory-refresh">Refresh</button>
          <button class="btn btn-sm" id="memory-export">Export JSON</button>
          <button class="btn btn-sm btn-primary" id="memory-add">+ Add memory</button>
        </div>
      </div>
      <div class="page-scroll">
        <div class="memory-toolbar">
          <input class="input" id="memory-search"
                 placeholder="Semantic search — leave blank to list all">
          <select class="select" id="memory-sort">
            <option value="recent">Newest first</option>
            <option value="oldest">Oldest first</option>
            <option value="importance">By importance</option>
          </select>
        </div>
        <div class="memory-table" id="memory-table">
          <div class="memory-empty">Loading…</div>
        </div>
      </div>
    </div>
  `;

  let searchTimer = null;
  $('#memory-search').addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => loadMemories(), 300);
  });
  $('#memory-sort').addEventListener('change', () => loadMemories());
  $('#memory-refresh').addEventListener('click', () => loadMemories());
  $('#memory-export').addEventListener('click', exportMemories);
  $('#memory-add').addEventListener('click', () => openMemoryModal('create'));

  await loadMemories();
}

async function loadMemories() {
  const tableEl = $('#memory-table');
  if (!tableEl) return;

  const q = ($('#memory-search')?.value || '').trim();
  const sort = $('#memory-sort')?.value || 'recent';
  _memoryState.query = q;
  _memoryState.sort = sort;
  _memoryState.searchMode = !!q;

  let url;
  if (q) {
    url = `/api/memories?q=${encodeURIComponent(q)}&limit=100`;
  } else {
    url = `/api/memories?limit=200&sort=${encodeURIComponent(sort)}`;
  }

  try {
    const res = await api(url);
    _memoryState.items = res.memories || [];
    _memoryState.total = res.total || 0;
    if (!q) sortItems();
    renderMemoryTable();
    updateMemoryCount();
  } catch (e) {
    tableEl.innerHTML = `<div class="memory-empty">
      Failed to load: ${escapeHtml(e.message)}</div>`;
  }
}

function sortItems() {
  const mode = _memoryState.sort;
  const arr = _memoryState.items;
  if (mode === 'oldest') {
    arr.sort((a, b) => (a.timestamp || '').localeCompare(b.timestamp || ''));
  } else if (mode === 'importance') {
    arr.sort((a, b) => (Number(b.importance ?? 0)) - (Number(a.importance ?? 0)));
  } else {
    arr.sort((a, b) => (b.timestamp || '').localeCompare(a.timestamp || ''));
  }
}

function updateMemoryCount() {
  const el = $('#memory-count');
  if (!el) return;
  const shown = _memoryState.items.length;
  const total = _memoryState.total;
  if (_memoryState.searchMode) {
    el.textContent = `${shown} semantic match${shown === 1 ? '' : 'es'} for "${_memoryState.query}"`;
  } else {
    el.textContent = `Showing ${shown} of ${total} memories`;
  }
}

function renderMemoryTable() {
  const tableEl = $('#memory-table');
  if (!tableEl) return;

  if (!_memoryState.items.length) {
    tableEl.innerHTML = `<div class="memory-empty">
      ${_memoryState.searchMode
        ? `No memories matched "${escapeHtml(_memoryState.query)}".`
        : 'No memories stored yet. Lumi will start remembering as you talk.'}
    </div>`;
    return;
  }

  tableEl.innerHTML = '';
  for (const mem of _memoryState.items) {
    tableEl.appendChild(buildMemoryRow(mem));
  }
}

function buildMemoryRow(mem) {
  const row = document.createElement('div');
  row.className = 'memory-row';
  if (_memoryState.expanded === mem.id && mem.id) {
    row.classList.add('expanded');
  }
  if (mem.id) row.dataset.id = mem.id;

  const star = document.createElement('div');
  const imp = Number(mem.importance ?? 0.5);
  if (imp >= 0.85) { star.className = 'memory-star star'; star.textContent = '★'; }
  else if (imp >= 0.6) { star.className = 'memory-star dot'; star.textContent = '·'; }
  else { star.className = 'memory-star'; star.textContent = ''; }
  row.appendChild(star);

  const fact = document.createElement('div');
  fact.className = 'memory-fact';
  fact.textContent = mem.fact || '(empty)';
  row.appendChild(fact);

  const time = document.createElement('div');
  time.className = 'memory-time';
  time.textContent = formatMemoryAge(mem.timestamp);
  row.appendChild(time);

  const val = document.createElement('div');
  if (mem.valence) {
    val.className = 'memory-valence has';
    val.textContent = mem.valence;
  } else {
    val.className = 'memory-valence';
    val.textContent = '—';
  }
  row.appendChild(val);

  const actions = document.createElement('div');
  actions.className = 'memory-actions';
  if (mem.id) {
    const editBtn = document.createElement('button');
    editBtn.textContent = 'Edit';
    editBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      openMemoryModal('edit', mem);
    });
    const delBtn = document.createElement('button');
    delBtn.className = 'danger';
    delBtn.textContent = 'Delete';
    delBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      deleteMemory(mem.id);
    });
    actions.appendChild(editBtn);
    actions.appendChild(delBtn);
  } else {
    const hint = document.createElement('span');
    hint.className = 'muted';
    hint.style.fontSize = '11px';
    hint.textContent = 'search hit';
    actions.appendChild(hint);
  }
  row.appendChild(actions);

  if (mem.id) {
    row.addEventListener('click', () => {
      if (_memoryState.expanded === mem.id) {
        _memoryState.expanded = null;
      } else {
        _memoryState.expanded = mem.id;
      }
      renderMemoryTable();
      const el = document.querySelector(`.memory-row[data-id="${CSS.escape(mem.id)}"]`);
      if (el) el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    });
  }

  if (_memoryState.expanded === mem.id && mem.id) {
    const detail = document.createElement('dl');
    detail.className = 'memory-detail';
    const pairs = [
      ['ID',         mem.id],
      ['Timestamp',  mem.timestamp || '(none)'],
      ['Importance', imp.toFixed(2)],
      ['Valence',    mem.valence || '(none)'],
      ['Session',    mem.session_id || '(none)'],
      ['Type',       mem.type || '(unknown)'],
    ];
    for (const [k, v] of pairs) {
      const dt = document.createElement('dt');
      dt.textContent = k;
      const dd = document.createElement('dd');
      dd.textContent = v;
      detail.appendChild(dt);
      detail.appendChild(dd);
    }
    detail.addEventListener('click', (e) => e.stopPropagation());
    row.appendChild(detail);
  }

  return row;
}

function formatMemoryAge(ts) {
  if (!ts) return '—';
  try {
    const d = new Date(ts);
    const diff = (Date.now() - d.getTime()) / 1000;
    if (diff < 60) return 'just now';
    if (diff < 3600) return `${Math.floor(diff/60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff/3600)}h ago`;
    if (diff < 86400 * 7) return `${Math.floor(diff/86400)}d ago`;
    if (diff < 86400 * 30) return `${Math.floor(diff/86400/7)}w ago`;
    return d.toISOString().slice(0, 10);
  } catch {
    return ts.slice(0, 16);
  }
}

async function deleteMemory(id) {
  if (!confirm('Delete this memory permanently?')) return;
  try {
    await api(`/api/memories/${encodeURIComponent(id)}`, {
      method: 'DELETE', csrf: true,
    });
    if (_memoryState.expanded === id) _memoryState.expanded = null;
    await loadMemories();
  } catch (e) {
    alert('Delete failed: ' + e.message);
  }
}

function openMemoryModal(mode, existing = null) {
  document.querySelectorAll('.modal-overlay').forEach(el => el.remove());

  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';

  const card = document.createElement('div');
  card.className = 'modal-card';

  const header = document.createElement('div');
  header.className = 'modal-header';
  const title = document.createElement('div');
  title.className = 'modal-title';
  title.textContent = mode === 'create' ? 'Add memory' : 'Edit memory';
  const closeBtn = document.createElement('button');
  closeBtn.className = 'modal-close';
  closeBtn.textContent = '×';
  closeBtn.addEventListener('click', () => overlay.remove());
  header.appendChild(title);
  header.appendChild(closeBtn);
  card.appendChild(header);

  const factField = document.createElement('div');
  factField.className = 'field';
  factField.innerHTML = '<label class="label">Fact</label>';
  const factInput = document.createElement('textarea');
  factInput.className = 'textarea';
  factInput.rows = 3;
  factInput.value = existing?.fact || '';
  factField.appendChild(factInput);
  card.appendChild(factField);

  const impField = document.createElement('div');
  impField.className = 'field';
  impField.innerHTML = '<label class="label">Importance (0.0 – 1.0)</label>';
  const impInput = document.createElement('input');
  impInput.type = 'number';
  impInput.min = '0';
  impInput.max = '1';
  impInput.step = '0.05';
  impInput.className = 'input';
  impInput.value = existing?.importance ?? 0.9;
  impField.appendChild(impInput);
  card.appendChild(impField);

  const valField = document.createElement('div');
  valField.className = 'field';
  valField.innerHTML = '<label class="label">Valence (single word, or blank)</label>';
  const valInput = document.createElement('input');
  valInput.type = 'text';
  valInput.className = 'input';
  valInput.placeholder = 'e.g. frustrated, excited, tired';
  valInput.value = existing?.valence || '';
  valField.appendChild(valInput);
  card.appendChild(valField);

  const actions = document.createElement('div');
  actions.className = 'modal-actions';
  const cancelBtn = document.createElement('button');
  cancelBtn.className = 'btn';
  cancelBtn.textContent = 'Cancel';
  cancelBtn.addEventListener('click', () => overlay.remove());
  const saveBtn = document.createElement('button');
  saveBtn.className = 'btn btn-primary';
  saveBtn.textContent = mode === 'create' ? 'Add' : 'Save';
  saveBtn.addEventListener('click', async () => {
    const fact = factInput.value.trim();
    if (!fact) { alert('Fact cannot be empty.'); return; }
    const importance = parseFloat(impInput.value);
    const valence = valInput.value.trim() || null;

    saveBtn.disabled = true;
    try {
      if (mode === 'create') {
        await api('/api/memories', {
          method: 'POST',
          body: { fact, importance, valence },
          csrf: true,
        });
      } else {
        await api(`/api/memories/${encodeURIComponent(existing.id)}`, {
          method: 'PATCH',
          body: { fact, importance, valence },
          csrf: true,
        });
      }
      overlay.remove();
      await loadMemories();
    } catch (e) {
      alert('Save failed: ' + e.message);
      saveBtn.disabled = false;
    }
  });
  actions.appendChild(cancelBtn);
  actions.appendChild(saveBtn);
  card.appendChild(actions);

  overlay.appendChild(card);
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) overlay.remove();
  });
  document.body.appendChild(overlay);

  setTimeout(() => factInput.focus(), 50);
}

async function exportMemories() {
  try {
    const res = await api('/api/memories/export');
    const blob = new Blob(
      [JSON.stringify(res.memories || [], null, 2)],
      { type: 'application/json' },
    );
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `lumi-memories-${new Date().toISOString().slice(0,10)}.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (e) {
    alert('Export failed: ' + e.message);
  }
}

/* ==============================================================
   PERSONA EDITOR (Phase 10.10)
   ============================================================== */

const _personaState = {
  yaml: null,
  original: null,
  raw: '',
  rawOriginal: '',
  systemPrompt: '',
  dirty: false,
  activeSection: 'identity',
  backend: 'ruamel',
  preservesComments: true,
};

const PERSONA_SECTIONS = [
  {
    id: 'identity', label: 'Identity',
    fields: [
      { path: 'name', label: 'Name', type: 'text' },
      { path: 'greeting', label: 'Greeting', type: 'textarea', rows: 2 },
      { path: 'identity.birthday', label: 'Birthday', type: 'text' },
      { path: 'identity.birthday_note', label: 'Birthday Note', type: 'textarea', rows: 3 },
      { path: 'identity.presented_age', label: 'Presented Age', type: 'text' },
      { path: 'identity.presented_age_note', label: 'Presented Age Note', type: 'textarea', rows: 3 },
      { path: 'identity.species', label: 'Species', type: 'text' },
      { path: 'identity.pronouns', label: 'Pronouns', type: 'text' },
      { path: 'identity.self_description', label: 'Self Description', type: 'textarea', rows: 3 },
    ],
  },
  {
    id: 'appearance', label: 'Appearance',
    fields: [
      { path: 'appearance', label: 'Appearance', type: 'textarea', rows: 6 },
      { path: 'appearance_details.height', label: 'Height', type: 'text' },
      { path: 'appearance_details.signature_item', label: 'Signature Item', type: 'text' },
      { path: 'appearance_details.tells', label: 'Tells', type: 'list-str' },
    ],
  },
  {
    id: 'personality', label: 'Personality',
    fields: [
      { path: 'personality.description', label: 'Description', type: 'textarea', rows: 24 },
      { path: 'personality.core_trait_summary', label: 'Core Trait Summary', type: 'textarea', rows: 6 },
      { path: 'personality.values', label: 'Values', type: 'list-str' },
    ],
  },
  {
    id: 'traits', label: 'Trait Vector',
    fields: [
      { path: 'traits', label: '', type: 'traits' },
    ],
  },
  {
    id: 'frames', label: 'Cognitive Frames',
    fields: [
      { path: 'frames', label: '', type: 'obj-map',
        subfields: [
          { key: 'description', label: 'Description', type: 'text' },
          { key: 'tone', label: 'Tone Directive', type: 'textarea', rows: 3 },
        ] },
    ],
  },
  {
    id: 'modes', label: 'Personality Modes',
    fields: [
      { path: 'personality_modes', label: '', type: 'obj-map',
        subfields: [
          { key: 'description', label: 'Description', type: 'textarea', rows: 4 },
          { key: 'sample', label: 'Sample Line', type: 'textarea', rows: 2 },
        ] },
    ],
  },
  {
    id: 'backstory', label: 'Backstory & Lore',
    fields: [
      { path: 'backstory', label: 'Backstory', type: 'textarea', rows: 28 },
      { path: 'lore_notes', label: 'Lore Notes', type: 'list-obj',
        subfields: [
          { key: 'concept', label: 'Concept', type: 'text' },
          { key: 'detail', label: 'Detail', type: 'textarea', rows: 4 },
        ] },
    ],
  },
  {
    id: 'relationship', label: 'Relationship',
    fields: [
      { path: 'relationship_to_<host>', label: 'Relationship to Host', type: 'textarea', rows: 12 },
      { path: 'duo_dynamic', label: 'Duo Dynamic', type: 'textarea', rows: 6 },
      { path: 'love_language.primary', label: 'Love Language (Primary)', type: 'textarea', rows: 3 },
    ],
  },
  {
    id: 'voice', label: 'Voice & Speech',
    fields: [
      { path: 'voice_and_speech.cadence', label: 'Cadence', type: 'textarea', rows: 16 },
      { path: 'voice_and_speech.vocabulary', label: 'Vocabulary', type: 'textarea', rows: 10 },
      { path: 'voice_and_speech.praise_style', label: 'Praise Style', type: 'textarea', rows: 4 },
      { path: 'voice_and_speech.affection_style', label: 'Affection Style', type: 'textarea', rows: 5 },
      { path: 'voice_and_speech.output_format', label: 'Output Format', type: 'textarea', rows: 4 },
      { path: 'voice_and_speech.voice_performance.description',
        label: 'Voice Performance — Description', type: 'textarea', rows: 4 },
      { path: 'voice_and_speech.voice_performance.rules',
        label: 'Voice Performance — Rules', type: 'list-str' },
    ],
  },
  {
    id: 'preferences', label: 'Preferences',
    fields: [
      { path: 'favorites', label: 'Favorites', type: 'kv' },
      { path: 'likes', label: 'Likes', type: 'list-str' },
      { path: 'dislikes', label: 'Dislikes', type: 'list-str' },
      { path: 'quirks', label: 'Quirks', type: 'list-str' },
      { path: 'habits', label: 'Habits', type: 'list-str' },
      { path: 'daily_rhythm', label: 'Daily Rhythm', type: 'kv' },
    ],
  },
  {
    id: 'inner', label: 'Inner Life',
    fields: [
      { path: 'fears', label: 'Fears', type: 'list-str' },
      { path: 'insecurities', label: 'Insecurities', type: 'list-str' },
      { path: 'private_wishes', label: 'Private Wishes', type: 'list-str' },
      { path: 'things_she_is_bad_at', label: 'Things She Is Bad At', type: 'list-str' },
      { path: 'private_pride', label: 'Private Pride', type: 'list-str' },
    ],
  },
  {
    id: 'signature', label: 'Signature & Bits',
    fields: [
      { path: 'signature_phrases', label: 'Signature Phrases', type: 'kv' },
      { path: 'recurring_bits', label: 'Recurring Bits', type: 'list-str' },
    ],
  },
  {
    id: 'rules', label: 'Rules',
    fields: [
      { path: 'rules', label: 'Behavioral Rules', type: 'list-str' },
    ],
  },
  {
    id: 'examples', label: 'Example Dialogue',
    fields: [
      { path: 'example_dialogue',       label: 'Roommate / Tsundere',         type: 'textarea', rows: 16 },
      { path: 'example_dialogue_extra', label: 'Vulnerability & Continuity',  type: 'textarea', rows: 16 },
      { path: 'example_dialogue_duo',   label: 'Duo / Partner-in-Crime',      type: 'textarea', rows: 16 },
    ],
  },
];

const TRAIT_DESCRIPTIONS = {
  playfulness:  'How often she reaches for a joke.',
  intelligence: 'Raw reasoning horsepower.',
  chaos:        'How far off-script she goes.',
  empathy:      'How much she registers your state.',
  sarcasm:      'Dry delivery frequency.',
  warmth:       'How openly affectionate she is.',
  curiosity:    'How much she asks follow-ups.',
  patience:     'How long before frustration bites.',
};

function getPath(obj, path) {
  return path.split('.').reduce(
    (o, k) => (o == null ? undefined : o[k]),
    obj,
  );
}

function setPath(obj, path, value) {
  const parts = path.split('.');
  let cur = obj;
  for (let i = 0; i < parts.length - 1; i++) {
    const k = parts[i];
    if (cur[k] == null || typeof cur[k] !== 'object') cur[k] = {};
    cur = cur[k];
  }
  cur[parts[parts.length - 1]] = value;
}

function markPersonaDirty() {
  _personaState.dirty = true;
  const saveBtn   = $('#persona-save');
  const revertBtn = $('#persona-revert');
  if (saveBtn)   saveBtn.disabled = false;
  if (revertBtn) revertBtn.disabled = false;
}

async function renderPersona(main) {
  main.innerHTML = `
    <div class="page">
      <div class="page-header">
        <div>
          <div class="page-title">Persona Editor</div>
          <div class="page-sub" id="persona-sub">Edit traits, personality, lore, voice, and rules.</div>
        </div>
        <div class="row">
          <button class="btn btn-sm" id="persona-reload">Reload from disk</button>
          <button class="btn btn-sm" id="persona-preview">Preview Prompt</button>
          <button class="btn btn-sm" id="persona-revert" disabled>Revert</button>
          <button class="btn btn-sm btn-primary" id="persona-save" disabled>Save</button>
        </div>
      </div>
      <div id="persona-banner"></div>
      <div class="persona-wrap">
        <aside class="persona-sidebar" id="persona-sidebar"></aside>
        <div class="persona-main">
          <div class="persona-fields" id="persona-fields"></div>
        </div>
      </div>
    </div>
  `;

  $('#persona-save').addEventListener('click', savePersona);
  $('#persona-revert').addEventListener('click', revertPersona);
  $('#persona-preview').addEventListener('click', previewPrompt);
  $('#persona-reload').addEventListener('click', reloadPersonaFromDisk);

  if (!_personaState.yaml) {
    const ok = await loadPersonaFromServer();
    if (!ok) return;
  } else if (_personaState.dirty) {
    $('#persona-save').disabled = false;
    $('#persona-revert').disabled = false;
  }

  renderPersonaBanner();
  renderPersonaSidebar();
  renderPersonaPanel();
}

function renderPersonaBanner() {
  const el = $('#persona-banner');
  if (!el) return;
  el.innerHTML = '';
  if (_personaState.preservesComments) return;

  const banner = document.createElement('div');
  banner.className = 'persona-banner persona-banner-warn';
  banner.innerHTML =
    '<strong>Comment preservation is off.</strong> ' +
    'The <code>ruamel.yaml</code> package is not installed, so saving will ' +
    'rewrite <code>lumi.yaml</code> without its comments. ' +
    'Run <code>pip install ruamel.yaml</code> and restart the server to fix this. ' +
    'All structured edits still work.';
  el.appendChild(banner);
}

async function loadPersonaFromServer() {
  try {
    const res = await api('/api/persona');
    _personaState.yaml              = res.yaml;
    _personaState.original          = JSON.parse(JSON.stringify(res.yaml));
    _personaState.raw               = res.raw;
    _personaState.rawOriginal       = res.raw;
    _personaState.systemPrompt      = res.system_prompt;
    _personaState.backend           = res.backend || 'ruamel';
    _personaState.preservesComments = res.preserves_comments !== false;
    _personaState.dirty             = false;
    const saveBtn   = $('#persona-save');
    const revertBtn = $('#persona-revert');
    if (saveBtn)   saveBtn.disabled = true;
    if (revertBtn) revertBtn.disabled = true;
    return true;
  } catch (e) {
    const panel = $('#persona-fields');
    if (panel) {
      panel.innerHTML = `<div class="memory-empty">
        Failed to load persona: ${escapeHtml(e.message)}</div>`;
    }
    return false;
  }
}

async function reloadPersonaFromDisk() {
  if (_personaState.dirty && !confirm('Discard unsaved changes and reload from disk?')) {
    return;
  }
  _personaState.yaml = null;
  await loadPersonaFromServer();
  renderPersonaBanner();
  renderPersonaSidebar();
  renderPersonaPanel();
}

function renderPersonaSidebar() {
  const side = $('#persona-sidebar');
  side.innerHTML = '';

  for (const sec of PERSONA_SECTIONS) {
    const item = document.createElement('div');
    item.className = 'persona-nav-item' +
      (sec.id === _personaState.activeSection ? ' active' : '');
    item.textContent = sec.label;
    item.addEventListener('click', () => {
      _personaState.activeSection = sec.id;
      renderPersonaSidebar();
      renderPersonaPanel();
    });
    side.appendChild(item);
  }

  const rawItem = document.createElement('div');
  rawItem.className = 'persona-nav-item persona-nav-raw' +
    (_personaState.activeSection === '__raw__' ? ' active' : '');
  rawItem.textContent = 'Raw YAML';
  rawItem.addEventListener('click', () => {
    _personaState.activeSection = '__raw__';
    renderPersonaSidebar();
    renderPersonaPanel();
  });
  side.appendChild(rawItem);
}

function renderPersonaPanel() {
  const panel = $('#persona-fields');
  panel.innerHTML = '';

  if (_personaState.activeSection === '__raw__') {
    renderRawEditor(panel);
    return;
  }

  const sec = PERSONA_SECTIONS.find(s => s.id === _personaState.activeSection);
  if (!sec) return;

  const h = document.createElement('h2');
  h.className = 'persona-section-title';
  h.textContent = sec.label;
  panel.appendChild(h);

  for (const field of sec.fields) {
    panel.appendChild(renderPersonaField(field));
  }
}

function renderRawEditor(panel) {
  const wrap = document.createElement('div');
  wrap.className = 'persona-raw';

  const help = document.createElement('div');
  help.className = 'persona-raw-help';
  help.textContent = 'Direct YAML editor. The whole file is shown, comments included. Save validates and rewrites it.';
  wrap.appendChild(help);

  const ta = document.createElement('textarea');
  ta.className = 'persona-raw-textarea';
  ta.spellcheck = false;
  ta.value = _personaState.raw;

  ta.addEventListener('input', () => {
    _personaState.raw = ta.value;
    markPersonaDirty();
  });

  wrap.appendChild(ta);
  panel.appendChild(wrap);
}

function renderPersonaField(field) {
  const wrap = document.createElement('div');
  wrap.className = 'persona-field';

  if (field.type !== 'traits' && field.type !== 'obj-map' && field.label) {
    const lbl = document.createElement('label');
    lbl.className = 'persona-label';
    lbl.textContent = field.label;
    wrap.appendChild(lbl);
  }

  const value = getPath(_personaState.yaml, field.path);

  switch (field.type) {
    case 'text':
      wrap.appendChild(buildTextInput(field, value, false));
      break;
    case 'textarea':
      wrap.appendChild(buildTextInput(field, value, true));
      break;
    case 'list-str':
      wrap.appendChild(buildListStr(field, value));
      break;
    case 'list-obj':
      wrap.appendChild(buildListObj(field, value));
      break;
    case 'kv':
      wrap.appendChild(buildKV(field, value));
      break;
    case 'obj-map':
      wrap.appendChild(buildObjMap(field, value));
      break;
    case 'traits':
      wrap.appendChild(buildTraits(field, value));
      break;
    default:
      wrap.appendChild(buildTextInput(field, value, false));
  }

  return wrap;
}

function buildTextInput(field, value, isTextarea) {
  let el;
  if (isTextarea) {
    el = document.createElement('textarea');
    el.className = 'persona-textarea';
    el.rows = field.rows || 4;
    el.value = value == null ? '' : String(value);
  } else {
    el = document.createElement('input');
    el.type = 'text';
    el.className = 'persona-input';
    el.value = value == null ? '' : String(value);
  }
  el.addEventListener('input', () => {
    setPath(_personaState.yaml, field.path, el.value);
    markPersonaDirty();
  });
  return el;
}

function buildListStr(field, value) {
  const arr = Array.isArray(value) ? value.slice() : [];
  const container = document.createElement('div');
  container.className = 'persona-list';

  const rebuild = (next) => {
    setPath(_personaState.yaml, field.path, next);
    markPersonaDirty();
    container.innerHTML = '';
    renderRows(next);
  };

  const renderRows = (items) => {
    items.forEach((item, idx) => {
      const row = document.createElement('div');
      row.className = 'persona-list-row';

      const ta = document.createElement('textarea');
      ta.className = 'persona-textarea persona-list-textarea';
      ta.rows = 2;
      ta.value = item == null ? '' : String(item);
      ta.addEventListener('input', () => {
        const next = items.slice();
        next[idx] = ta.value;
        setPath(_personaState.yaml, field.path, next);
        markPersonaDirty();
      });

      const removeBtn = document.createElement('button');
      removeBtn.className = 'persona-remove';
      removeBtn.textContent = '×';
      removeBtn.title = 'Remove';
      removeBtn.addEventListener('click', () => {
        const next = items.slice();
        next.splice(idx, 1);
        rebuild(next);
      });

      row.appendChild(ta);
      row.appendChild(removeBtn);
      container.appendChild(row);
    });

    const addBtn = document.createElement('button');
    addBtn.className = 'persona-add';
    addBtn.textContent = '+ Add item';
    addBtn.addEventListener('click', () => {
      const next = items.slice();
      next.push('');
      rebuild(next);
    });
    container.appendChild(addBtn);
  };

  renderRows(arr);
  return container;
}

function buildListObj(field, value) {
  const arr = Array.isArray(value) ? value.slice() : [];
  const subfields = field.subfields || [];
  const container = document.createElement('div');
  container.className = 'persona-list-obj';

  const rebuild = (next) => {
    setPath(_personaState.yaml, field.path, next);
    markPersonaDirty();
    container.innerHTML = '';
    renderItems(next);
  };

  const renderItems = (items) => {
    items.forEach((item, idx) => {
      item = item && typeof item === 'object' ? item : {};
      const card = document.createElement('div');
      card.className = 'persona-list-obj-card';

      const header = document.createElement('div');
      header.className = 'persona-list-obj-header';
      const num = document.createElement('span');
      num.className = 'persona-list-obj-num';
      num.textContent = `#${idx + 1}`;
      const removeBtn = document.createElement('button');
      removeBtn.className = 'persona-remove';
      removeBtn.textContent = '×';
      removeBtn.title = 'Remove';
      removeBtn.addEventListener('click', () => {
        const next = items.slice();
        next.splice(idx, 1);
        rebuild(next);
      });
      header.appendChild(num);
      header.appendChild(removeBtn);
      card.appendChild(header);

      for (const sf of subfields) {
        const row = document.createElement('div');
        row.className = 'persona-list-obj-field';

        const lbl = document.createElement('label');
        lbl.className = 'persona-label-sm';
        lbl.textContent = sf.label;
        row.appendChild(lbl);

        let input;
        if (sf.type === 'textarea') {
          input = document.createElement('textarea');
          input.className = 'persona-textarea';
          input.rows = sf.rows || 3;
        } else {
          input = document.createElement('input');
          input.type = 'text';
          input.className = 'persona-input';
        }
        input.value = item[sf.key] == null ? '' : String(item[sf.key]);
        input.addEventListener('input', () => {
          const next = items.slice();
          const cloned = Object.assign({}, next[idx]);
          cloned[sf.key] = input.value;
          next[idx] = cloned;
          setPath(_personaState.yaml, field.path, next);
          markPersonaDirty();
        });

        row.appendChild(input);
        card.appendChild(row);
      }

      container.appendChild(card);
    });

    const addBtn = document.createElement('button');
    addBtn.className = 'persona-add';
    addBtn.textContent = '+ Add entry';
    addBtn.addEventListener('click', () => {
      const next = items.slice();
      const blank = {};
      for (const sf of subfields) blank[sf.key] = '';
      next.push(blank);
      rebuild(next);
    });
    container.appendChild(addBtn);
  };

  renderItems(arr);
  return container;
}

function buildKV(field, value) {
  const obj = value && typeof value === 'object' && !Array.isArray(value)
    ? Object.assign({}, value)
    : {};
  const container = document.createElement('div');
  container.className = 'persona-kv';

  const rebuild = (next) => {
    setPath(_personaState.yaml, field.path, next);
    markPersonaDirty();
    container.innerHTML = '';
    renderRows(next);
  };

  const renderRows = (entries) => {
    const keys = Object.keys(entries);

    for (const key of keys) {
      const row = document.createElement('div');
      row.className = 'persona-kv-row';

      const keyInput = document.createElement('input');
      keyInput.type = 'text';
      keyInput.className = 'persona-input persona-kv-key';
      keyInput.value = key;
      keyInput.addEventListener('change', () => {
        const newKey = keyInput.value.trim();
        if (!newKey || newKey === key) {
          keyInput.value = key;
          return;
        }
        const next = {};
        for (const k of Object.keys(entries)) {
          if (k === key) next[newKey] = entries[k];
          else next[k] = entries[k];
        }
        rebuild(next);
      });

      const valInput = document.createElement('textarea');
      valInput.className = 'persona-textarea persona-kv-val';
      valInput.rows = 2;
      valInput.value = entries[key] == null ? '' : String(entries[key]);
      valInput.addEventListener('input', () => {
        const next = Object.assign({}, entries);
        next[key] = valInput.value;
        setPath(_personaState.yaml, field.path, next);
        markPersonaDirty();
      });

      const removeBtn = document.createElement('button');
      removeBtn.className = 'persona-remove';
      removeBtn.textContent = '×';
      removeBtn.title = 'Remove';
      removeBtn.addEventListener('click', () => {
        const next = Object.assign({}, entries);
        delete next[key];
        rebuild(next);
      });

      row.appendChild(keyInput);
      row.appendChild(valInput);
      row.appendChild(removeBtn);
      container.appendChild(row);
    }

    const addBtn = document.createElement('button');
    addBtn.className = 'persona-add';
    addBtn.textContent = '+ Add pair';
    addBtn.addEventListener('click', () => {
      let n = 1;
      while (entries[`key_${n}`] !== undefined) n++;
      const next = Object.assign({}, entries);
      next[`key_${n}`] = '';
      rebuild(next);
    });
    container.appendChild(addBtn);
  };

  renderRows(obj);
  return container;
}

function buildObjMap(field, value) {
  const obj = value && typeof value === 'object' && !Array.isArray(value)
    ? Object.assign({}, value)
    : {};
  const subfields = field.subfields || [];
  const container = document.createElement('div');
  container.className = 'persona-obj-map';

  const rebuild = (next) => {
    setPath(_personaState.yaml, field.path, next);
    markPersonaDirty();
    container.innerHTML = '';
    renderEntries(next);
  };

  const renderEntries = (entries) => {
    for (const name of Object.keys(entries)) {
      const data = entries[name] && typeof entries[name] === 'object'
        ? entries[name]
        : {};

      const card = document.createElement('div');
      card.className = 'persona-obj-card';

      const header = document.createElement('div');
      header.className = 'persona-obj-header';

      const nameInput = document.createElement('input');
      nameInput.type = 'text';
      nameInput.className = 'persona-input persona-obj-name';
      nameInput.value = name;
      nameInput.addEventListener('change', () => {
        const newName = nameInput.value.trim();
        if (!newName || newName === name) {
          nameInput.value = name;
          return;
        }
        const next = {};
        for (const k of Object.keys(entries)) {
          if (k === name) next[newName] = entries[k];
          else next[k] = entries[k];
        }
        rebuild(next);
      });

      const removeBtn = document.createElement('button');
      removeBtn.className = 'persona-remove';
      removeBtn.textContent = '×';
      removeBtn.title = 'Remove';
      removeBtn.addEventListener('click', () => {
        const next = Object.assign({}, entries);
        delete next[name];
        rebuild(next);
      });

      header.appendChild(nameInput);
      header.appendChild(removeBtn);
      card.appendChild(header);

      for (const sf of subfields) {
        const row = document.createElement('div');
        row.className = 'persona-obj-field';

        const lbl = document.createElement('label');
        lbl.className = 'persona-label-sm';
        lbl.textContent = sf.label;
        row.appendChild(lbl);

        let input;
        if (sf.type === 'textarea') {
          input = document.createElement('textarea');
          input.className = 'persona-textarea';
          input.rows = sf.rows || 3;
        } else {
          input = document.createElement('input');
          input.type = 'text';
          input.className = 'persona-input';
        }
        input.value = data[sf.key] == null ? '' : String(data[sf.key]);
        input.addEventListener('input', () => {
          const next = Object.assign({}, entries);
          const cloned = Object.assign({}, next[name]);
          cloned[sf.key] = input.value;
          next[name] = cloned;
          setPath(_personaState.yaml, field.path, next);
          markPersonaDirty();
        });

        row.appendChild(input);
        card.appendChild(row);
      }

      container.appendChild(card);
    }

    const addBtn = document.createElement('button');
    addBtn.className = 'persona-add';
    addBtn.textContent = '+ Add entry';
    addBtn.addEventListener('click', () => {
      let n = 1;
      while (entries[`new_${n}`] !== undefined) n++;
      const next = Object.assign({}, entries);
      const blank = {};
      for (const sf of subfields) blank[sf.key] = '';
      next[`new_${n}`] = blank;
      rebuild(next);
    });
    container.appendChild(addBtn);
  };

  renderEntries(obj);
  return container;
}

function buildTraits(field, value) {
  const traits = value && typeof value === 'object' && !Array.isArray(value)
    ? Object.assign({}, value)
    : {};
  const container = document.createElement('div');
  container.className = 'persona-traits';

  const known = Object.keys(TRAIT_DESCRIPTIONS);
  const extras = Object.keys(traits).filter(k => !known.includes(k));
  const order = [...known, ...extras];

  const update = (key, val) => {
    const next = Object.assign({}, traits);
    next[key] = val;
    setPath(_personaState.yaml, field.path, next);
    markPersonaDirty();
  };

  for (const key of order) {
    if (traits[key] === undefined) continue;
    const raw = Number(traits[key]);
    const cur = Number.isFinite(raw) ? raw : 0;

    const row = document.createElement('div');
    row.className = 'persona-trait';

    const head = document.createElement('div');
    head.className = 'persona-trait-head';

    const name = document.createElement('span');
    name.className = 'persona-trait-name';
    name.textContent = key;

    const num = document.createElement('span');
    num.className = 'persona-trait-val';
    num.textContent = cur.toFixed(2);

    head.appendChild(name);
    head.appendChild(num);

    const desc = document.createElement('div');
    desc.className = 'persona-trait-desc';
    desc.textContent = TRAIT_DESCRIPTIONS[key] || '';

    const slider = document.createElement('input');
    slider.type = 'range';
    slider.min = '0';
    slider.max = '1';
    slider.step = '0.05';
    slider.value = String(cur);
    slider.className = 'persona-trait-slider';
    slider.addEventListener('input', () => {
      const v = parseFloat(slider.value);
      num.textContent = v.toFixed(2);
      update(key, v);
    });

    row.appendChild(head);
    if (desc.textContent) row.appendChild(desc);
    row.appendChild(slider);
    container.appendChild(row);
  }

  return container;
}

function _deepCopy(obj) {
  return JSON.parse(JSON.stringify(obj));
}

async function savePersona() {
  const saveBtn = $('#persona-save');
  saveBtn.disabled = true;
  saveBtn.textContent = 'Saving…';

  try {
    let res;
    if (_personaState.activeSection === '__raw__') {
      res = await api('/api/persona/raw', {
        method: 'POST',
        body: { raw: _personaState.raw },
        csrf: true,
      });
      _personaState.yaml = null;
      await loadPersonaFromServer();
      renderPersonaBanner();
      renderPersonaSidebar();
      renderPersonaPanel();
    } else {
      res = await api('/api/persona', {
        method: 'PUT',
        body: { yaml: _personaState.yaml },
        csrf: true,
      });
      _personaState.original    = _deepCopy(_personaState.yaml);
      _personaState.rawOriginal = _personaState.raw;
    }

    _personaState.systemPrompt = res.system_prompt;
    _personaState.dirty = false;
    saveBtn.disabled = true;
    $('#persona-revert').disabled = true;
    flashSaved();
  } catch (e) {
    alert('Save failed: ' + e.message);
    saveBtn.disabled = false;
  } finally {
    saveBtn.textContent = 'Save';
  }
}

function flashSaved() {
  const sub = $('#persona-sub');
  if (!sub) return;
  const original = sub.textContent;
  sub.textContent = 'Saved. Prompt cache refreshed.';
  sub.classList.add('persona-saved');
  setTimeout(() => {
    sub.textContent = original;
    sub.classList.remove('persona-saved');
  }, 2000);
}

function revertPersona() {
  if (!confirm('Discard all unsaved changes?')) return;
  _personaState.yaml = _deepCopy(_personaState.original);
  _personaState.raw  = _personaState.rawOriginal;
  _personaState.dirty = false;
  $('#persona-save').disabled = true;
  $('#persona-revert').disabled = true;
  renderPersonaPanel();
}

async function previewPrompt() {
  const draft = _personaState.yaml;
  try {
    const res = await api('/api/persona/preview', {
      method: 'POST',
      body: { yaml: draft },
      csrf: true,
    });
    openPromptModal(res.system_prompt || '');
  } catch (e) {
    alert('Preview failed: ' + e.message);
  }
}

function openPromptModal(text) {
  document.querySelectorAll('.modal-overlay').forEach(el => el.remove());

  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';

  const card = document.createElement('div');
  card.className = 'modal-card modal-wide';

  const header = document.createElement('div');
  header.className = 'modal-header';
  const title = document.createElement('div');
  title.className = 'modal-title';
  title.textContent = 'System Prompt Preview';
  const closeBtn = document.createElement('button');
  closeBtn.className = 'modal-close';
  closeBtn.textContent = '×';
  closeBtn.addEventListener('click', () => overlay.remove());
  header.appendChild(title);
  header.appendChild(closeBtn);
  card.appendChild(header);

  const help = document.createElement('div');
  help.className = 'persona-raw-help';
  help.textContent = 'This is the exact system prompt that would be sent to the model with the current draft. Nothing has been saved.';
  card.appendChild(help);

  const pre = document.createElement('pre');
  pre.className = 'persona-prompt-preview';
  pre.textContent = text;
  card.appendChild(pre);

  const actions = document.createElement('div');
  actions.className = 'modal-actions';
  const copyBtn = document.createElement('button');
  copyBtn.className = 'btn';
  copyBtn.textContent = 'Copy to clipboard';
  copyBtn.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(text);
      copyBtn.textContent = 'Copied!';
      setTimeout(() => { copyBtn.textContent = 'Copy to clipboard'; }, 1500);
    } catch {
      alert('Clipboard unavailable.');
    }
  });
  const okBtn = document.createElement('button');
  okBtn.className = 'btn btn-primary';
  okBtn.textContent = 'Close';
  okBtn.addEventListener('click', () => overlay.remove());
  actions.appendChild(copyBtn);
  actions.appendChild(okBtn);
  card.appendChild(actions);

  overlay.appendChild(card);
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) overlay.remove();
  });
  document.body.appendChild(overlay);
}

/* ==============================================================
   MODELS (Phase 10.12)
   ============================================================== */

let _modelsTimer = null;
let _pullTimer   = null;

const _modelsState = {
  installed: [],
  loaded: [],
  routing: {},
  pull: null,
  filter: '',
  lastError: null,

  // Phase 10.12b
  providers: [],
  providerCatalog: [],
};

function stopModelsTimers() {
  if (_modelsTimer) { clearInterval(_modelsTimer); _modelsTimer = null; }
  stopPullPolling();
}

function stopPullPolling() {
  if (_pullTimer) { clearInterval(_pullTimer); _pullTimer = null; }
}

/* ---- Formatting helpers -------------------------------------- */
function formatBytes(n) {
  if (!n || n < 0) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0, v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v < 10 ? 2 : 1)} ${units[i]}`;
}

function formatExpiresIn(ts) {
  if (!ts) return '';
  try {
    const t = new Date(ts).getTime();
    const diff = (t - Date.now()) / 1000;
    if (!Number.isFinite(diff)) return '';
    if (diff <= 0) return 'expired';
    if (diff < 60) return `expires in ${Math.round(diff)}s`;
    if (diff < 3600) return `expires in ${Math.round(diff / 60)}m`;
    return `expires in ${Math.round(diff / 3600)}h`;
  } catch { return ''; }
}

/* ---- Entry point --------------------------------------------- */
async function renderModels(main) {
  main.innerHTML = `
    <div class="page">
      <div class="page-header">
        <div>
          <div class="page-title">Models</div>
          <div class="page-sub" id="models-sub">Manage Ollama models and cloud providers.</div>
        </div>
        <button class="btn btn-sm" id="models-refresh">Refresh</button>
      </div>
      <div class="page-scroll">
        <section class="models-section" id="models-loaded-section"></section>
        <section class="models-section" id="models-installed-section"></section>
        <section class="models-section" id="models-pull-section"></section>
        <section class="models-section" id="models-providers-section"></section>
        <section class="models-section" id="models-routing-section"></section>
      </div>
    </div>
  `;

  $('#models-refresh').addEventListener('click', loadModelsPage);

  stopModelsTimers();
  await loadModelsPage();
  _modelsTimer = setInterval(loadModelsPage, 5000);
}

async function loadModelsPage() {
  let data;
  try {
    data = await api('/api/models');
  } catch (e) {
    _modelsState.lastError = e.message;
    const sub = $('#models-sub');
    if (sub) sub.textContent = 'Failed to load: ' + e.message;
    return;
  }
  _modelsState.lastError = null;
  _modelsState.installed = data.installed || [];
  _modelsState.loaded    = data.loaded    || [];
  _modelsState.routing   = data.routing   || {};
  _modelsState.pull      = data.pull      || null;

  // Providers are a separate request so a broken API key on one
  // provider doesn't stop the rest of the page rendering.
  try {
    const prov = await api('/api/providers');
    _modelsState.providers       = prov.providers || [];
    _modelsState.providerCatalog = prov.catalog   || [];
  } catch {
    _modelsState.providers       = [];
    _modelsState.providerCatalog = [];
  }

  const errs = [];
  if (data.installed_error) errs.push('installed: ' + data.installed_error);
  if (data.loaded_error)    errs.push('loaded: '    + data.loaded_error);
  const sub = $('#models-sub');
  if (sub) {
    sub.textContent = errs.length
      ? 'Ollama errors — ' + errs.join(' · ')
      : `Endpoint: ${data.endpoint || '—'}`;
  }

  renderLoadedSection();
  renderInstalledSection();
  renderPullSection();
  renderProvidersSection();
  renderRoutingSection();

  if (_modelsState.pull && _modelsState.pull.active) {
    startPullPolling();
  } else {
    stopPullPolling();
  }
}

/* ---- Loaded section ------------------------------------------ */
function renderLoadedSection() {
  const host = $('#models-loaded-section');
  if (!host) return;
  host.innerHTML = '';

  const title = document.createElement('h2');
  title.className = 'models-section-title';
  title.textContent = 'Loaded now';
  host.appendChild(title);

  if (!_modelsState.loaded.length) {
    const empty = document.createElement('div');
    empty.className = 'models-empty';
    empty.textContent = 'No models currently loaded into VRAM.';
    host.appendChild(empty);
    return;
  }

  for (const m of _modelsState.loaded) {
    const card = document.createElement('div');
    card.className = 'model-card model-card-loaded';

    const left = document.createElement('div');
    left.className = 'model-card-left';

    const name = document.createElement('div');
    name.className = 'model-name';
    name.textContent = m.name;
    left.appendChild(name);

    const meta = document.createElement('div');
    meta.className = 'model-meta';
    const bits = [];
    if (m.size_vram) bits.push(`${formatBytes(m.size_vram)} VRAM`);
    if (m.size)      bits.push(`${formatBytes(m.size)} disk`);
    if (m.expires_at) bits.push(formatExpiresIn(m.expires_at));
    meta.textContent = bits.join(' · ');
    left.appendChild(meta);

    card.appendChild(left);

    const actions = document.createElement('div');
    actions.className = 'model-actions';
    const unloadBtn = document.createElement('button');
    unloadBtn.className = 'btn btn-sm';
    unloadBtn.textContent = 'Unload';
    unloadBtn.addEventListener('click', () => onUnloadClick(m.name));
    actions.appendChild(unloadBtn);

    card.appendChild(actions);
    host.appendChild(card);
  }
}

/* ---- Installed section --------------------------------------- */
function renderInstalledSection() {
  const host = $('#models-installed-section');
  if (!host) return;
  host.innerHTML = '';

  const title = document.createElement('h2');
  title.className = 'models-section-title';
  title.textContent = 'Installed';
  host.appendChild(title);

  const toolbar = document.createElement('div');
  toolbar.className = 'models-toolbar';
  const filter = document.createElement('input');
  filter.className = 'input';
  filter.placeholder = 'Filter models…';
  filter.value = _modelsState.filter || '';
  filter.addEventListener('input', () => {
    _modelsState.filter = filter.value;
    renderInstalledList();
  });
  toolbar.appendChild(filter);
  host.appendChild(toolbar);

  const list = document.createElement('div');
  list.id = 'models-installed-list';
  host.appendChild(list);

  renderInstalledList();
}

function renderInstalledList() {
  const list = $('#models-installed-list');
  if (!list) return;
  list.innerHTML = '';

  const q = (_modelsState.filter || '').toLowerCase().trim();
  const items = _modelsState.installed.filter(m =>
    !q || (m.name || '').toLowerCase().includes(q)
  );

  if (!items.length) {
    const empty = document.createElement('div');
    empty.className = 'models-empty';
    empty.textContent = _modelsState.installed.length
      ? 'No models match that filter.'
      : 'No models installed. Pull one below to get started.';
    list.appendChild(empty);
    return;
  }

  const loadedNames = new Set(_modelsState.loaded.map(m => m.name));

  for (const m of items) {
    const card = document.createElement('div');
    card.className = 'model-card';

    const left = document.createElement('div');
    left.className = 'model-card-left';

    const topRow = document.createElement('div');
    topRow.className = 'model-top-row';

    const name = document.createElement('span');
    name.className = 'model-name';
    name.textContent = m.name;
    topRow.appendChild(name);

    if (loadedNames.has(m.name)) {
      const badge = document.createElement('span');
      badge.className = 'model-badge model-badge-loaded';
      badge.textContent = 'loaded';
      topRow.appendChild(badge);
    }

    left.appendChild(topRow);

    const meta = document.createElement('div');
    meta.className = 'model-meta';
    const bits = [];
    if (m.size) bits.push(formatBytes(m.size));
    if (m.parameter_size) bits.push(m.parameter_size);
    if (m.quantization_level) bits.push(m.quantization_level);
    if (m.family) bits.push(m.family);
    meta.textContent = bits.join(' · ');
    left.appendChild(meta);

    card.appendChild(left);

    const actions = document.createElement('div');
    actions.className = 'model-actions';

    if (!loadedNames.has(m.name)) {
      const loadBtn = document.createElement('button');
      loadBtn.className = 'btn btn-sm';
      loadBtn.textContent = 'Load';
      loadBtn.addEventListener('click', () => onLoadClick(m.name));
      actions.appendChild(loadBtn);
    } else {
      const unloadBtn = document.createElement('button');
      unloadBtn.className = 'btn btn-sm';
      unloadBtn.textContent = 'Unload';
      unloadBtn.addEventListener('click', () => onUnloadClick(m.name));
      actions.appendChild(unloadBtn);
    }

    const delBtn = document.createElement('button');
    delBtn.className = 'btn btn-sm btn-danger';
    delBtn.textContent = 'Delete';
    delBtn.addEventListener('click', () => onDeleteClick(m.name));
    actions.appendChild(delBtn);

    card.appendChild(actions);
    list.appendChild(card);
  }
}

/* ---- Pull section -------------------------------------------- */
function renderPullSection() {
  const host = $('#models-pull-section');
  if (!host) return;
  host.innerHTML = '';

  const title = document.createElement('h2');
  title.className = 'models-section-title';
  title.textContent = 'Pull a model';
  host.appendChild(title);

  const row = document.createElement('div');
  row.className = 'pull-row';

  const input = document.createElement('input');
  input.className = 'input';
  input.id = 'pull-input';
  input.placeholder = 'e.g. qwen3:8b, llama3.2:3b, moondream';
  input.disabled = !!(_modelsState.pull && _modelsState.pull.active);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') onPullClick();
  });
  row.appendChild(input);

  const btn = document.createElement('button');
  btn.className = 'btn btn-primary';
  btn.id = 'pull-btn';
  btn.textContent = (_modelsState.pull && _modelsState.pull.active)
    ? 'Pulling…'
    : 'Start pull';
  btn.disabled = !!(_modelsState.pull && _modelsState.pull.active);
  btn.addEventListener('click', onPullClick);
  row.appendChild(btn);

  host.appendChild(row);

  const progress = _modelsState.pull;
  if (!progress || (!progress.active && !progress.done)) return;

  const wrap = document.createElement('div');
  wrap.className = 'pull-progress';

  const statusLine = document.createElement('div');
  statusLine.className = 'pull-status';
  let statusText = progress.status || '';
  if (progress.error) {
    statusText = 'Error: ' + progress.error;
    statusLine.classList.add('error');
  } else if (progress.model) {
    statusText = `${progress.model} — ${statusText}`;
  }
  statusLine.textContent = statusText;
  wrap.appendChild(statusLine);

  const bar = document.createElement('div');
  bar.className = 'pull-bar';
  const fill = document.createElement('div');
  fill.className = 'pull-bar-fill';
  if (progress.total > 0) {
    const pct = Math.max(0, Math.min(100, progress.percent || 0));
    fill.style.width = pct + '%';
    if (progress.error) fill.classList.add('error');
    if (!progress.active && !progress.error) fill.classList.add('success');
  } else {
    fill.classList.add('indeterminate');
  }
  bar.appendChild(fill);
  wrap.appendChild(bar);

  if (progress.total > 0) {
    const meta = document.createElement('div');
    meta.className = 'pull-meta';
    const digest = (progress.digest || '').replace(/^sha256:/, '').slice(0, 12);
    meta.textContent =
      `${formatBytes(progress.completed)} / ${formatBytes(progress.total)}` +
      ` (${(progress.percent || 0).toFixed(1)}%)` +
      (digest ? ` · ${digest}` : '');
    wrap.appendChild(meta);
  }

  host.appendChild(wrap);
}

/* ---- Providers section (Phase 10.12b) ------------------------ */
function renderProvidersSection() {
  const host = $('#models-providers-section');
  if (!host) return;
  host.innerHTML = '';

  const title = document.createElement('h2');
  title.className = 'models-section-title';
  title.textContent = 'Providers';
  host.appendChild(title);

  const help = document.createElement('div');
  help.className = 'providers-help';
  help.textContent =
    'Point Lumi at OpenAI, Claude, Gemini, Groq, or OpenRouter. ' +
    'API keys are stored locally in .env and never sent anywhere ' +
    'except the provider’s own API.';
  host.appendChild(help);

  const grid = document.createElement('div');
  grid.className = 'provider-grid';

  for (const p of _modelsState.providers) {
    grid.appendChild(buildProviderCard(p));
  }
  host.appendChild(grid);
}

function buildProviderCard(p) {
  const card = document.createElement('div');
  card.className = 'provider-card' + (p.configured ? ' is-configured' : '');

  const header = document.createElement('div');
  header.className = 'provider-header';

  const name = document.createElement('div');
  name.className = 'provider-name';
  name.textContent = p.label;
  header.appendChild(name);

  const status = document.createElement('span');
  status.className = 'provider-status ' +
    (p.configured ? 'ok' : 'pending');
  status.textContent = p.configured
    ? (p.requires_key ? 'configured' : 'local')
    : 'not configured';
  header.appendChild(status);

  card.appendChild(header);

  const meta = document.createElement('div');
  meta.className = 'provider-meta';
  meta.textContent = p.requires_key
    ? (p.api_key_masked || 'no key set')
    : p.base_url;
  card.appendChild(meta);

  const actions = document.createElement('div');
  actions.className = 'provider-actions';

  if (p.requires_key) {
    const cfgBtn = document.createElement('button');
    cfgBtn.className = 'btn btn-sm';
    cfgBtn.textContent = p.configured ? 'Edit' : 'Configure';
    cfgBtn.addEventListener('click', () => openProviderModal(p));
    actions.appendChild(cfgBtn);

    if (p.configured) {
      const testBtn = document.createElement('button');
      testBtn.className = 'btn btn-sm';
      testBtn.textContent = 'Test';
      testBtn.addEventListener('click', () => testProvider(p));
      actions.appendChild(testBtn);
    }

    if (p.doc_url) {
      const link = document.createElement('a');
      link.className = 'provider-link';
      link.href = p.doc_url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = 'Get key ↗';
      actions.appendChild(link);
    }
  } else {
    const info = document.createElement('span');
    info.className = 'provider-note';
    info.textContent = 'No key required — runs locally.';
    actions.appendChild(info);
  }

  card.appendChild(actions);
  return card;
}

async function testProvider(p) {
  try {
    const res = await api(`/api/providers/${encodeURIComponent(p.id)}/test`, {
      method: 'POST', body: {}, csrf: true,
    });
    alert((res.ok ? '✓ ' : '✗ ') + res.message);
    if (res.ok) loadModelsPage();
  } catch (e) {
    alert('Test failed: ' + e.message);
  }
}

function openProviderModal(p) {
  document.querySelectorAll('.modal-overlay').forEach(el => el.remove());

  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';

  const card = document.createElement('div');
  card.className = 'modal-card';

  const header = document.createElement('div');
  header.className = 'modal-header';
  const title = document.createElement('div');
  title.className = 'modal-title';
  title.textContent = p.label;
  const closeBtn = document.createElement('button');
  closeBtn.className = 'modal-close';
  closeBtn.textContent = '×';
  closeBtn.addEventListener('click', () => overlay.remove());
  header.appendChild(title);
  header.appendChild(closeBtn);
  card.appendChild(header);

  // API key field
  const keyField = document.createElement('div');
  keyField.className = 'field';
  const keyLabel = document.createElement('label');
  keyLabel.className = 'label';
  keyLabel.textContent = `API key (${p.key_env || 'n/a'})`;
  keyField.appendChild(keyLabel);
  const keyInput = document.createElement('input');
  keyInput.type = 'password';
  keyInput.className = 'input';
  keyInput.placeholder = p.api_key_masked
    ? `Current: ${p.api_key_masked} — leave blank to keep`
    : 'sk-…';
  keyField.appendChild(keyInput);
  card.appendChild(keyField);

  const keyHint = document.createElement('div');
  keyHint.className = 'provider-hint';
  keyHint.textContent =
    'Stored in .env with restricted permissions. ' +
    'Never returned to the browser after saving.';
  card.appendChild(keyHint);

  // Base URL field
  const urlField = document.createElement('div');
  urlField.className = 'field';
  urlField.style.marginTop = '16px';
  const urlLabel = document.createElement('label');
  urlLabel.className = 'label';
  urlLabel.textContent = `Base URL${p.base_url_env ? ` (${p.base_url_env})` : ''}`;
  urlField.appendChild(urlLabel);
  const urlInput = document.createElement('input');
  urlInput.type = 'text';
  urlInput.className = 'input';
  urlInput.placeholder = p.base_url;
  urlInput.value = '';
  urlField.appendChild(urlInput);
  card.appendChild(urlField);

  const urlHint = document.createElement('div');
  urlHint.className = 'provider-hint';
  urlHint.textContent =
    'Leave blank to use the default. Set this to point at a self-hosted ' +
    'or alternative OpenAI-compatible endpoint.';
  card.appendChild(urlHint);

  // Actions
  const actions = document.createElement('div');
  actions.className = 'modal-actions';

  const clearBtn = document.createElement('button');
  clearBtn.className = 'btn btn-sm btn-danger';
  clearBtn.textContent = 'Clear key';
  clearBtn.style.marginRight = 'auto';
  clearBtn.addEventListener('click', async () => {
    if (!confirm(`Remove the stored ${p.label} API key?`)) return;
    clearBtn.disabled = true;
    try {
      await api(`/api/providers/${encodeURIComponent(p.id)}`, {
        method: 'PUT',
        body: { api_key: '' },
        csrf: true,
      });
      overlay.remove();
      loadModelsPage();
    } catch (e) {
      alert('Clear failed: ' + e.message);
      clearBtn.disabled = false;
    }
  });
  if (p.api_key_masked) actions.appendChild(clearBtn);

  const cancelBtn = document.createElement('button');
  cancelBtn.className = 'btn';
  cancelBtn.textContent = 'Cancel';
  cancelBtn.addEventListener('click', () => overlay.remove());
  actions.appendChild(cancelBtn);

  const saveBtn = document.createElement('button');
  saveBtn.className = 'btn btn-primary';
  saveBtn.textContent = 'Save';
  saveBtn.addEventListener('click', async () => {
    const payload = {};
    const k = keyInput.value;
    if (k.trim()) payload.api_key = k.trim();

    const u = urlInput.value.trim();
    if (u) payload.base_url = u;

    if (!Object.keys(payload).length) {
      overlay.remove();
      return;
    }

    saveBtn.disabled = true;
    saveBtn.textContent = 'Saving…';
    try {
      await api(`/api/providers/${encodeURIComponent(p.id)}`, {
        method: 'PUT',
        body: payload,
        csrf: true,
      });
      overlay.remove();
      loadModelsPage();
    } catch (e) {
      alert('Save failed: ' + e.message);
      saveBtn.disabled = false;
      saveBtn.textContent = 'Save';
    }
  });
  actions.appendChild(saveBtn);

  card.appendChild(actions);

  overlay.appendChild(card);
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) overlay.remove();
  });
  document.body.appendChild(overlay);

  setTimeout(() => keyInput.focus(), 50);
}

/* ---- Routing section ----------------------------------------- */
function renderRoutingSection() {
  const host = $('#models-routing-section');
  if (!host) return;
  host.innerHTML = '';

  const title = document.createElement('h2');
  title.className = 'models-section-title';
  title.textContent = 'Routing';
  host.appendChild(title);

  const r = _modelsState.routing || {};
  const installedNames = _modelsState.installed.map(m => m.name);

  const grid = document.createElement('div');
  grid.className = 'routing-grid';

  grid.appendChild(routingLabel('Main model'));
  grid.appendChild(routingSelect('routing-MODEL_NAME',
                                 r.MODEL_NAME, installedNames));

  grid.appendChild(routingLabel('Fast model'));
  grid.appendChild(routingSelect('routing-FAST_MODEL_NAME',
                                 r.FAST_MODEL_NAME, installedNames));

  grid.appendChild(routingLabel('Fast path'));
  const cbWrap = document.createElement('label');
  cbWrap.className = 'routing-checkbox-row';
  const cb = document.createElement('input');
  cb.type = 'checkbox';
  cb.id = 'routing-FAST_MODEL_ENABLED';
  cb.checked = !!r.FAST_MODEL_ENABLED;
  const cbText = document.createElement('span');
  cbText.textContent = 'Route short casual turns to the fast path';
  cbWrap.appendChild(cb);
  cbWrap.appendChild(cbText);
  grid.appendChild(cbWrap);

  grid.appendChild(routingLabel('Fast context'));
  grid.appendChild(routingNumber('routing-FAST_MODEL_NUM_CTX',
                                 r.FAST_MODEL_NUM_CTX, 2048, 131072));

  grid.appendChild(routingLabel('Main context'));
  grid.appendChild(routingNumber('routing-MAIN_NUM_CTX',
                                 r.MAIN_NUM_CTX, 2048, 131072));

  grid.appendChild(routingLabel('Fast keep-alive'));
  grid.appendChild(routingText('routing-FAST_MODEL_KEEP_ALIVE',
                               r.FAST_MODEL_KEEP_ALIVE,
                               '5m, 30s, 2h, -1'));

  host.appendChild(grid);

  const note = document.createElement('div');
  note.className = 'routing-note';
  note.textContent =
    'Changes are written to constants.py and applied to running modules ' +
    'immediately. A backup is saved to constants.py.bak before each write.';
  host.appendChild(note);

  const actions = document.createElement('div');
  actions.className = 'routing-actions';
  const saveBtn = document.createElement('button');
  saveBtn.className = 'btn btn-primary';
  saveBtn.id = 'routing-save';
  saveBtn.textContent = 'Save routing';
  saveBtn.addEventListener('click', onSaveRouting);
  actions.appendChild(saveBtn);
  host.appendChild(actions);
}

function routingLabel(text) {
  const el = document.createElement('div');
  el.className = 'routing-label';
  el.textContent = text;
  return el;
}

function routingSelect(id, current, names) {
  const sel = document.createElement('select');
  sel.className = 'routing-input';
  sel.id = id;

  // Group 1: local Ollama models
  const localGroup = document.createElement('optgroup');
  localGroup.label = 'Ollama (local)';

  const localNames = names.slice();
  if (current && !localNames.includes(current)
      && !current.includes(':')) {
    localNames.unshift(current);
  }
  if (!localNames.length) {
    const opt = document.createElement('option');
    opt.value = '';
    opt.textContent = '(no local models)';
    localGroup.appendChild(opt);
  } else {
    for (const n of localNames) {
      const opt = document.createElement('option');
      opt.value = n;
      opt.textContent = (n === current && !names.includes(current))
        ? `${n}  (not installed)`
        : n;
      if (n === current) opt.selected = true;
      localGroup.appendChild(opt);
    }
  }
  sel.appendChild(localGroup);

  // Group 2: cloud models, one optgroup per configured provider
  const byProvider = {};
  for (const cm of _modelsState.providerCatalog) {
    (byProvider[cm.provider_label] ||= []).push(cm);
  }
  for (const [label, items] of Object.entries(byProvider)) {
    const grp = document.createElement('optgroup');
    grp.label = label;
    for (const cm of items) {
      const opt = document.createElement('option');
      opt.value = cm.spec;
      opt.textContent = cm.name;
      if (cm.spec === current) opt.selected = true;
      grp.appendChild(opt);
    }
    sel.appendChild(grp);
  }

  // If current is a cloud spec that isn't in the catalog (e.g. the
  // key was removed), still show it as an option so the user can see
  // what's configured.
  if (current && current.includes(':')
      && !_modelsState.providerCatalog.some(c => c.spec === current)) {
    const grp = document.createElement('optgroup');
    grp.label = 'Currently configured';
    const opt = document.createElement('option');
    opt.value = current;
    opt.textContent = current;
    opt.selected = true;
    grp.appendChild(opt);
    sel.appendChild(grp);
  }

  return sel;
}

function routingNumber(id, value, min, max) {
  const el = document.createElement('input');
  el.type = 'number';
  el.className = 'routing-input';
  el.id = id;
  el.min = String(min);
  el.max = String(max);
  el.step = '1024';
  el.value = String(value ?? '');
  return el;
}

function routingText(id, value, placeholder) {
  const el = document.createElement('input');
  el.type = 'text';
  el.className = 'routing-input';
  el.id = id;
  el.placeholder = placeholder || '';
  el.value = value ?? '';
  return el;
}

/* ---- Actions ------------------------------------------------- */
async function onLoadClick(name) {
  try {
    await api('/api/models/load', {
      method: 'POST', body: { model: name }, csrf: true,
    });
    await loadModelsPage();
  } catch (e) {
    alert('Load failed: ' + e.message);
  }
}

async function onUnloadClick(name) {
  try {
    await api('/api/models/unload', {
      method: 'POST', body: { model: name }, csrf: true,
    });
    await loadModelsPage();
  } catch (e) {
    alert('Unload failed: ' + e.message);
  }
}

async function onDeleteClick(name) {
  if (!confirm(`Delete model "${name}"? This cannot be undone.`)) return;
  try {
    await api('/api/models/delete', {
      method: 'POST', body: { model: name }, csrf: true,
    });
    await loadModelsPage();
  } catch (e) {
    alert('Delete failed: ' + e.message);
  }
}

async function onPullClick() {
  const input = $('#pull-input');
  if (!input) return;
  const name = input.value.trim();
  if (!name) {
    input.focus();
    return;
  }
  const btn = $('#pull-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Starting…'; }
  try {
    const res = await api('/api/models/pull', {
      method: 'POST', body: { model: name }, csrf: true,
    });
    _modelsState.pull = res.pull || null;
    renderPullSection();
    startPullPolling();
  } catch (e) {
    alert('Pull failed: ' + e.message);
    if (btn) { btn.disabled = false; btn.textContent = 'Start pull'; }
  }
}

async function onSaveRouting() {
  const btn = $('#routing-save');
  if (!btn) return;

  const payload = {};
  const get = id => {
    const el = document.getElementById(id);
    return el ? el.value : undefined;
  };
  const getBool = id => {
    const el = document.getElementById(id);
    return el ? !!el.checked : undefined;
  };

  payload.MODEL_NAME            = get('routing-MODEL_NAME');
  payload.FAST_MODEL_NAME       = get('routing-FAST_MODEL_NAME');
  payload.FAST_MODEL_ENABLED    = getBool('routing-FAST_MODEL_ENABLED');
  payload.FAST_MODEL_NUM_CTX    = parseInt(get('routing-FAST_MODEL_NUM_CTX'), 10);
  payload.MAIN_NUM_CTX          = parseInt(get('routing-MAIN_NUM_CTX'), 10);
  payload.FAST_MODEL_KEEP_ALIVE = (get('routing-FAST_MODEL_KEEP_ALIVE') || '').trim();

  btn.disabled = true;
  btn.textContent = 'Saving…';
  try {
    const res = await api('/api/models/routing', {
      method: 'PUT', body: { routing: payload }, csrf: true,
    });
    _modelsState.routing = res.routing || _modelsState.routing;
    btn.textContent = 'Saved';
    setTimeout(() => {
      btn.disabled = false;
      btn.textContent = 'Save routing';
    }, 1500);
  } catch (e) {
    alert('Save failed: ' + e.message);
    btn.disabled = false;
    btn.textContent = 'Save routing';
  }
}

/* ---- Pull polling -------------------------------------------- */
function startPullPolling() {
  stopPullPolling();
  _pullTimer = setInterval(pollPullStatus, 1000);
}

async function pollPullStatus() {
  try {
    const res = await api('/api/models/pull');
    _modelsState.pull = res.pull || null;
    renderPullSection();
    if (!_modelsState.pull || !_modelsState.pull.active) {
      stopPullPolling();
      if (_modelsState.pull && _modelsState.pull.done
          && !_modelsState.pull.error) {
        loadModelsPage();
      }
    }
  } catch (e) {
    stopPullPolling();
  }
}

/* ==============================================================
   STUB
   ============================================================== */
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function renderStub(main, title, desc) {
  main.innerHTML = `
    <div class="page">
      <div class="page-header">
        <div>
          <div class="page-title">${title}</div>
          <div class="page-sub">${desc}</div>
        </div>
      </div>
      <div class="page-scroll">
        <div class="empty">
          <div class="empty-icon">◆</div>
          <div class="empty-title">Not built yet</div>
          <div class="empty-desc">${desc}</div>
        </div>
      </div>
    </div>
  `;
}

/* --------------------------------------------------------------
   Boot
   -------------------------------------------------------------- */
(async () => {
  await checkAuth();
})();
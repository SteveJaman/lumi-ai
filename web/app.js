/* ============================================================
   Lumi web client — SPA router, auth, chat, dashboard, memory.
   Polling-based; no Socket.IO on the client.
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
   Auth
   -------------------------------------------------------------- */
async function checkAuth() {
  try {
    const me = await api('/auth/me');
    state.user = me.user;
    state.csrf = me.csrf;
    state.host = me.host;
    showShell();
  } catch {
    showAuth();
  }
}

function showAuth() {
  $('#auth-screen').classList.remove('hidden');
  $('#app-shell').classList.add('hidden');
  const form = $('#auth-form');
  form.dataset.mode = 'login';
  updateAuthUI();
}

function updateAuthUI() {
  const mode = $('#auth-form').dataset.mode;
  if (mode === 'register') {
    $('#auth-title').textContent = 'Create account';
    $('#auth-sub').textContent = 'Local to this machine. No cloud.';
    $('#auth-submit').textContent = 'Register';
    $('#auth-switch-text').textContent = 'Already have an account?';
    $('#auth-switch-link').textContent = 'Sign in';
    $('#auth-display-field').style.display = '';
    $('#auth-password').autocomplete = 'new-password';
  } else {
    $('#auth-title').textContent = 'Welcome back';
    $('#auth-sub').textContent = 'Sign in to continue';
    $('#auth-submit').textContent = 'Sign in';
    $('#auth-switch-text').textContent = 'No account?';
    $('#auth-switch-link').textContent = 'Register';
    $('#auth-display-field').style.display = 'none';
    $('#auth-password').autocomplete = 'current-password';
  }
  hideAuthError();
}

function showAuthError(msg) {
  const el = $('#auth-error');
  el.textContent = msg;
  el.classList.add('visible');
}

function hideAuthError() {
  $('#auth-error').classList.remove('visible');
}

$('#auth-switch-link').addEventListener('click', () => {
  const form = $('#auth-form');
  form.dataset.mode = form.dataset.mode === 'login' ? 'register' : 'login';
  updateAuthUI();
});

$('#auth-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  hideAuthError();
  const mode = e.currentTarget.dataset.mode;
  const username = $('#auth-username').value.trim();
  const password = $('#auth-password').value;
  const display  = $('#auth-display').value.trim();
  const submit   = $('#auth-submit');
  submit.disabled = true;

  try {
    const path = mode === 'register' ? '/auth/register' : '/auth/login';
    const body = mode === 'register'
      ? { username, password, display_name: display || undefined }
      : { username, password };
    const res = await api(path, { method: 'POST', body });
    state.user = res.user;
    state.csrf = res.csrf;
    showShell();
  } catch (err) {
    showAuthError(err.message);
  } finally {
    submit.disabled = false;
  }
});

async function doLogout() {
  try { await api('/auth/logout', { method: 'POST' }); } catch {}
  location.reload();
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
  $('#auth-screen').classList.add('hidden');
  $('#app-shell').classList.remove('hidden');

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
    case 'persona':      return renderStub(main, 'Persona editor',
      'Edit traits, personality, examples, and voice without touching lumi.yaml. Phase 10.10.');
    case 'models':       return renderStub(main, 'Model manager',
      'Load/unload Ollama models, pull new ones, configure routing. Phase 10.12.');
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

  // Star
  const star = document.createElement('div');
  const imp = Number(mem.importance ?? 0.5);
  if (imp >= 0.85) { star.className = 'memory-star star'; star.textContent = '★'; }
  else if (imp >= 0.6) { star.className = 'memory-star dot'; star.textContent = '·'; }
  else { star.className = 'memory-star'; star.textContent = ''; }
  row.appendChild(star);

  // Fact
  const fact = document.createElement('div');
  fact.className = 'memory-fact';
  fact.textContent = mem.fact || '(empty)';
  row.appendChild(fact);

  // Time
  const time = document.createElement('div');
  time.className = 'memory-time';
  time.textContent = formatMemoryAge(mem.timestamp);
  row.appendChild(time);

  // Valence
  const val = document.createElement('div');
  if (mem.valence) {
    val.className = 'memory-valence has';
    val.textContent = mem.valence;
  } else {
    val.className = 'memory-valence';
    val.textContent = '—';
  }
  row.appendChild(val);

  // Actions
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
    // Semantic search hits don't carry ids — show a marker.
    const hint = document.createElement('span');
    hint.className = 'muted';
    hint.style.fontSize = '11px';
    hint.textContent = 'search hit';
    actions.appendChild(hint);
  }
  row.appendChild(actions);

  // Click to expand (only when we have an id)
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

  // Expanded detail block
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
  try {
    const status = await api('/setup-status');
    if (status.needs_setup) {
      location.href = '/setup';
      return;
    }
  } catch {}
  await checkAuth();
})();
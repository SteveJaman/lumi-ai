/* ============================================================
   Lumi web client — SPA router, chat, dashboard, memory,
   persona editor, models + providers, integrations, stream, settings,
   voice in browser, chat file uploads.
   Polling-based; socket.io is best-effort for audio only.
   Login / register live on their own page (/login).
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
  // Set from /auth/me. Remote (tunnel) sessions get a reduced UI; the
  // server enforces the same tier independently.
  isLocal:  true,
  features: { stream: false },
  _agentToggling: false,
};

/* --------------------------------------------------------------
   Theme — light (default) / dark toggle, persisted in localStorage
   -------------------------------------------------------------- */
function savedTheme() {
  let saved = null;
  try { saved = localStorage.getItem('lumi-theme'); } catch {}
  return saved === 'light' || saved === 'dark' ? saved : null;
}

function initTheme() {
  // theme-init.js already set data-theme before first paint; this just
  // syncs the toggle button and follows OS changes until the user picks.
  applyTheme(document.documentElement.getAttribute('data-theme') || 'light');

  const mq = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)');
  mq?.addEventListener?.('change', (e) => {
    if (!savedTheme()) applyTheme(e.matches ? 'dark' : 'light');
  });
}

function applyTheme(theme, { persist = false } = {}) {
  document.documentElement.setAttribute('data-theme', theme);
  if (persist) {
    try { localStorage.setItem('lumi-theme', theme); } catch {}
  }
  // Mood accents are tuned per theme; re-derive for the new one.
  if (_currentMood) setAccentForMood(_currentMood);

  const btn = document.getElementById('theme-toggle');
  if (btn) {
    // Icon shows the mode you'll switch TO (GitHub/Vercel convention)
    btn.textContent = theme === 'dark' ? '☀' : '☾';
    btn.title = theme === 'dark'
      ? 'Switch to light mode'
      : 'Switch to dark mode';
  }
}

function toggleTheme() {
  const current =
    document.documentElement.getAttribute('data-theme') || 'light';
  applyTheme(current === 'light' ? 'dark' : 'light', { persist: true });
}

// Wire the toggle once the DOM has the button
document.addEventListener('click', (e) => {
  if (e.target && e.target.id === 'theme-toggle') {
    toggleTheme();
  }
});

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

/* --------------------------------------------------------------
   Fingerprinting — cheap stable hash of any JSON-able value.
   Used to skip DOM work when polled state is identical.
   -------------------------------------------------------------- */
function fingerprint(value) {
  const str = (function stable(v) {
    if (v === null) return 'null';
    if (v === undefined) return 'undefined';
    if (typeof v !== 'object') return JSON.stringify(v);
    if (Array.isArray(v)) return '[' + v.map(stable).join(',') + ']';
    return '{' + Object.keys(v).sort()
      .map(k => JSON.stringify(k) + ':' + stable(v[k])).join(',') + '}';
  })(value);

  let h = 0x811c9dc5;
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h = (h * 0x01000193) >>> 0;
  }
  return h.toString(36) + ':' + str.length;
}

const ICONS = {
  chat:      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>',
  dashboard: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="9"/><rect x="14" y="3" width="7" height="5"/><rect x="14" y="12" width="7" height="9"/><rect x="3" y="16" width="7" height="5"/></svg>',
  persona:   '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 4-6 8-6s8 2 8 6"/></svg>',
  memory:    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a4 4 0 0 0-4 4v1H7a3 3 0 0 0 0 6v1a3 3 0 0 0 3 3h1v1a3 3 0 0 0 6 0v-1h1a3 3 0 0 0 3-3v-1a3 3 0 0 0 0-6V6a4 4 0 0 0-4-4h-1V1a3 3 0 0 0-6 0z" /></svg>',
  models:    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 9h6v6H9z"/><path d="M9 1v4M15 1v4M9 19v4M15 19v4M1 9h4M1 15h4M19 9h4M19 15h4"/></svg>',
  plugs:     '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 2v6M15 2v6M6 8h12v3a6 6 0 0 1-12 0z"/><path d="M12 17v5"/></svg>',
  stream:    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="2"/><path d="M16.24 7.76a6 6 0 0 1 0 8.49M7.76 16.24a6 6 0 0 1 0-8.49M19.07 4.93a10 10 0 0 1 0 14.14M4.93 19.07a10 10 0 0 1 0-14.14"/></svg>',
  settings:  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
};

/* --------------------------------------------------------------
   Auth bootstrap
   -------------------------------------------------------------- */
async function checkAuth() {
  try {
    const me = await api('/auth/me');
    state.user = me.user;
    state.csrf = me.csrf;
    state.host = me.host;
    state.isLocal = me.is_local !== false;
    state.features = me.features || { stream: false };

    showShell();

    // Refresh theme button icon now that the sidebar exists
    const t = document.documentElement.getAttribute('data-theme') || 'light';
    applyTheme(t);

    initBootLoader();
    await pollBootStatus();
    if (!_bootState.ready) {
      _bootState.pollTimer = setInterval(pollBootStatus, 700);
    }
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
// hostOnly: hidden for remote (tunnel) sessions, whose API calls for
// these pages are refused server-side. feature: hidden unless the
// server reports that feature enabled.
const NAV = [
  { id: 'chat',         label: 'Chat',         icon: 'chat' },
  { id: 'dashboard',    label: 'Dashboard',    icon: 'dashboard' },
  { id: 'persona',      label: 'Persona',      icon: 'persona',  hostOnly: true },
  { id: 'memory',       label: 'Memory',       icon: 'memory',   hostOnly: true },
  { id: 'models',       label: 'Models',       icon: 'models',   hostOnly: true },
  { id: 'integrations', label: 'Integrations', icon: 'plugs',    hostOnly: true },
  { id: 'stream',       label: 'Stream',       icon: 'stream',   hostOnly: true, feature: 'stream' },
  { id: 'settings',     label: 'Settings',     icon: 'settings', hostOnly: true },
];

function visibleNav() {
  return NAV.filter(n =>
    (state.isLocal || !n.hostOnly) &&
    (!n.feature || state.features?.[n.feature]));
}

function showShell() {
  $('#host-name').textContent = state.host?.name || 'Lumi';
  $('#user-name').textContent = state.user.display_name || state.user.username;
  $('#user-avatar').textContent =
    (state.user.display_name || state.user.username)
      .slice(0, 1).toUpperCase();

  // Open whichever page the URL names (e.g. a reload on #/memory).
  state.view = viewFromHash();

  const nav = $('#nav');
  nav.innerHTML = '';
  for (const item of visibleNav()) {
    // Real links: keyboard/focus/middle-click work for free, and the
    // hashchange listener does the routing.
    const el = document.createElement('a');
    el.className = 'nav-item' + (item.id === state.view ? ' active' : '');
    el.href = `#/${item.id}`;
    el.dataset.view = item.id;
    el.title = item.label;
    el.innerHTML = `${ICONS[item.icon]}<span>${item.label}</span>`;
    nav.appendChild(el);
  }

  navigate(state.view, true);
  startStatusPolling();
}

function viewFromHash() {
  const id = (location.hash || '').replace(/^#\/?/, '');
  return visibleNav().some(n => n.id === id) ? id : 'chat';
}

function navigate(view, replace = false) {
  if (!visibleNav().some(n => n.id === view)) view = 'chat';
  state.view = view;
  if (view !== 'chat') stopChatPolling();
  if (view !== 'stream') stopStreamPolling();
  if (view !== 'chat') resetMoodAccent();

  if (view !== 'models') {
    stopModelsTimers();
    _modelsState.routingDirty = false;
  }
  if (!replace) location.hash = `#/${view}`;
  $$('.nav-item').forEach(el => {
    const on = el.dataset.view === view;
    el.classList.toggle('active', on);
    if (on) el.setAttribute('aria-current', 'page');
    else el.removeAttribute('aria-current');
  });
  render();
}

window.addEventListener('hashchange', () => {
  const view = viewFromHash();
  if (view !== state.view) navigate(view, true);
});

/* --------------------------------------------------------------
   Boot sequence — bloom loader
   --------------------------------------------------------------
   The server exposes /api/status.boot as a dict of subsystems:
     { memory, mic, tts, vts, self_model, discord }
   Each entry has { state, detail } where state is one of:
     "loading" | "ready" | "skipped" | "error"
   boot.complete is true when every subsystem is in a terminal
   state. If the server doesn't send a boot block at all, we
   treat the app as already booted and skip the loader.
   -------------------------------------------------------------- */
const _bootState = {
  ready: false,
  pollTimer: null,
  skipShown: false,
  startedAt: 0,
};

const BOOT_LABELS = {
  memory:     'memory',
  mic:        'microphone',
  tts:        'voice',
  vts:        'avatar',
  //  discord:    'discord',
};

const BOOT_STATE_LABEL = {
  ready:   'ready',
  loading: '…',
  skipped: 'skipped',
  error:   'failed',
};

const MOOD_ACCENTS = {
  happy:     { color: '#f0883e', glow: '240, 136, 62'  },
  delighted: { color: '#f0883e', glow: '240, 136, 62'  },
  excited:   { color: '#f778ba', glow: '247, 120, 186' },
  laughing:  { color: '#f778ba', glow: '247, 120, 186' },
  amused:    { color: '#f0a030', glow: '240, 160, 48'  },
  sad:       { color: '#7db8ef', glow: '125, 184, 239' },
  unhappy:   { color: '#7db8ef', glow: '125, 184, 239' },
  angry:     { color: '#f87373', glow: '248, 115, 115' },
  frustrated:{ color: '#f87373', glow: '248, 115, 115' },
  confused:  { color: '#a78ce0', glow: '167, 140, 224' },
  uncertain: { color: '#a78ce0', glow: '167, 140, 224' },
  surprised: { color: '#7ad4dc', glow: '122, 212, 220' },
  calm:      { color: '#7ee0d4', glow: '126, 224, 212' },
  satisfied: { color: '#7ee0d4', glow: '126, 224, 212' },
  proud:     { color: '#7ee0d4', glow: '126, 224, 212' },
  grateful:  { color: '#7ee0d4', glow: '126, 224, 212' },
};

const MOOD_EMOJI = {
  happy:     '😊',
  delighted: '✨',
  excited:   '⭐',
  laughing:  '😄',
  amused:    '🙂',
  sad:       '🌧️',
  unhappy:   '☁️',
  angry:     '🔥',
  frustrated:'😤',
  confused:  '💭',
  uncertain: '🤔',
  surprised: '😲',
  calm:      '🌙',
  satisfied: '😌',
  proud:     '💪',
  grateful:  '💗',
};


let _moodResetTimer = null;
let _currentMood = null;

function initBootLoader() {
  const loader = document.getElementById('boot-loader');
  const stepsEl = document.getElementById('boot-steps');
  const skipBtn = document.getElementById('boot-skip');

  if (!loader || !stepsEl) return;

  loader.classList.remove('hidden');
  _bootState.startedAt = Date.now();

  stepsEl.innerHTML = Object.entries(BOOT_LABELS).map(([key, label]) =>
    `<li data-subsystem="${key}" data-state="loading">` +
    `<span>${label}</span>` +
    `<span class="state">…</span>` +
    `</li>`
  ).join('');

  if (skipBtn) {
    skipBtn.classList.add('hidden');
    skipBtn.addEventListener('click', () => finishBootLoader());
  }
}

function updateBloomFromBoot(boot) {
  if (!boot) {
    // No boot block from the server — skip immediately.
    finishBootLoader();
    return;
  }

  const stepsEl = document.getElementById('boot-steps');
  if (stepsEl) {
    for (const [key] of Object.entries(BOOT_LABELS)) {
      const li = stepsEl.querySelector(`[data-subsystem="${key}"]`);
      if (!li) continue;
      const s = boot[key] || { state: 'loading', detail: '' };
      li.dataset.state = s.state;
      const stateEl = li.querySelector('.state');
      if (stateEl) {
        stateEl.textContent = BOOT_STATE_LABEL[s.state] || s.state;
        if (s.detail) stateEl.title = s.detail;
      }
    }
  }

  const keys = Object.keys(BOOT_LABELS);
  const done = keys.filter(k => {
    const s = boot[k] || {};
    return s.state === 'ready' || s.state === 'skipped' || s.state === 'error';
  }).length;
  const pct = done / keys.length;

  const flower = document.querySelector('.boot-flower');
  if (flower) flower.style.setProperty('--bloom', pct.toFixed(3));

  // Skip button after 3s of waiting
  if (!_bootState.skipShown && Date.now() - _bootState.startedAt > 3000) {
    _bootState.skipShown = true;
    const skipBtn = document.getElementById('boot-skip');
    if (skipBtn) skipBtn.classList.remove('hidden');
  }

  // If all tracked subsystems are done, let the user click to enter
  if (done === keys.length && !_bootState.ready) {
    const titleEl = document.getElementById('boot-title');
    if (titleEl) {
      titleEl.textContent = 'ready — click to enter ✨';
      titleEl.style.cursor = 'pointer';
      titleEl.onclick = () => finishBootLoader();
    }
    if (flower) {
      flower.style.cursor = 'pointer';
      flower.onclick = () => finishBootLoader();
    }
  }
}

async function pollBootStatus() {
  if (_bootState.ready) return;

  try {
    const res = await api('/api/status');
    state.status = res;

    // Sidebar status pills update from the same payload
    updateStatusPills(res.boot || null);

    updateBloomFromBoot(res.boot || null);
  } catch {
    // Server might still be starting up — retry next tick.
  }
}

function finishBootLoader() {
  if (_bootState.ready) return;
  _bootState.ready = true;

  if (_bootState.pollTimer) {
    clearInterval(_bootState.pollTimer);
    _bootState.pollTimer = null;
  }

  const flower = document.querySelector('.boot-flower');
  if (flower) flower.classList.add('complete');

  setTimeout(() => {
    const loader = document.getElementById('boot-loader');
    if (!loader) return;
    loader.classList.add('exit');
    setTimeout(() => loader.remove(), 700);
  }, 1000);

  // Populate pills right away so they aren't blank once the
  // loader finishes.
  updateStatusPills(state.status?.boot || null);
}

function updateStatusPills(boot) {
  const map = { vts: 'dot-vts', mic: 'dot-mic' };

  for (const [key, id] of Object.entries(map)) {
    const el = document.getElementById(id);
    if (!el) continue;
    const st = (boot && boot[key] && boot[key].state) || null;
    el.className = 'dot' + (
      st === 'ready'   ? ' ok'  :
      st === 'error'   ? ' err' :
      st === 'loading' ? ' warn' :
      st === 'skipped' ? ''     :
      ''
    );
  }

  // Ollama: derived from whether the models endpoint responds.
  const ollamaEl = document.getElementById('dot-ollama');
  if (ollamaEl) {
    if (!ollamaEl.dataset.checked) {
      ollamaEl.dataset.checked = '1';
      api('/api/models').then(() => {
        ollamaEl.className = 'dot ok';
      }).catch(() => {
        ollamaEl.className = 'dot err';
      });
    }
  }
}

/* --------------------------------------------------------------
   Mood-reactive accent — applied on the chat page only
   -------------------------------------------------------------- */
function applyMoodAccent(tags) {
  if (state.view !== 'chat') return;
  if (!tags || !tags.length) return;

  const tag = tags[tags.length - 1].toLowerCase();
  const mood = MOOD_ACCENTS[tag];

  if (!mood) return;
  if (_currentMood === tag) return;

  _currentMood = tag;
  setAccentForMood(tag);

  const chip = document.getElementById('mood-chip');
  const emoji = document.getElementById('mood-emoji');
  const label = document.getElementById('mood-label');
  if (chip && emoji && label) {
    emoji.textContent = MOOD_EMOJI[tag] || '🌙';
    label.textContent = tag;
    chip.title = `Lumi seems ${tag}`;
    // Restart the pop so a mood *change* is as visible as the first one.
    chip.classList.remove('active', 'leaving');
    void chip.offsetWidth;
    chip.classList.add('active');
  }

  clearTimeout(_moodResetTimer);
  _moodResetTimer = setTimeout(resetMoodAccent, 8000);
}

// Mood colours are bright pastels picked for the dark theme. On the
// light theme they're mixed toward the text colour so the accent still
// reads as text/borders on white.
function setAccentForMood(tag) {
  const mood = MOOD_ACCENTS[tag];
  if (!mood) return;
  const root = document.documentElement;
  const light = root.getAttribute('data-theme') !== 'dark';
  root.style.setProperty('--accent', light
    ? `color-mix(in srgb, ${mood.color} 55%, #1E2A2E)`
    : mood.color);
  root.style.setProperty('--accent-bg',
    `rgba(${mood.glow}, ${light ? 0.20 : 0.12})`);
}

function resetMoodAccent() {
  _currentMood = null;
  // Drop the inline overrides so the active theme's own accent applies.
  document.documentElement.style.removeProperty('--accent');
  document.documentElement.style.removeProperty('--accent-bg');
  clearTimeout(_moodResetTimer);
  const chip = document.getElementById('mood-chip');
  if (!chip || !chip.classList.contains('active')) return;
  // Fade out, then hide — unless a new mood re-activated it meanwhile.
  chip.classList.add('leaving');
  const onEnd = (e) => {
    if (e.target !== chip) return;   // ignore the emoji's own animation
    chip.removeEventListener('animationend', onEnd);
    if (chip.classList.contains('leaving')) {
      chip.classList.remove('active', 'leaving');
    }
  };
  chip.addEventListener('animationend', onEnd);
}

/* --------------------------------------------------------------
   Status polling
   -------------------------------------------------------------- */
const _statusFp = { value: null };

async function startStatusPolling() {
  const tick = async () => {
    try {
      const s = await api('/api/status');
      state.status = s;

      // Sidebar pills react to boot state on every poll
      if (s.boot) updateStatusPills(s.boot);

      const uiSlice = {
        agent_mode:  !!s.agent_mode,
        ai_thinking: !!s.ai_thinking,
        ai_speaking: !!s.ai_speaking,
        host_name:   s.host?.name || null,
      };
      const fp = fingerprint(uiSlice);
      if (fp !== _statusFp.value) {
        _statusFp.value = fp;
        updateAgentChip();
      }
      // Cheap, and its answer depends on history as well as status,
      // so run it every tick rather than only when status changes.
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
  if (!state.isLocal) {
    chip.disabled = true;
    chip.title = 'Agent mode can only be changed on the host machine.';
    return;
  }
  chip.title = on
    ? 'Agent mode is ON — Lumi can use tools. Click to disable.'
    : 'Agent mode is OFF — conversation only. Click to enable.';
}

let _thinkingBubbleEl = null;
// Fingerprint of the newest history entry at the moment the current
// thinking spell began (undefined = not thinking). A reply has landed
// once the newest entry is an assistant message that differs from it.
// Anchoring on content rather than history length keeps working when
// the 100-message window is full and the length stops growing.
let _thinkingAnchor = undefined;

function updateTypingIndicator() {
  const isThinking = !!state.status.ai_thinking;
  const lastMsg = state.history[state.history.length - 1] || null;
  const lastFp = lastMsg ? fingerprint(lastMsg) : null;

  if (!isThinking) _thinkingAnchor = undefined;
  else if (_thinkingAnchor === undefined) _thinkingAnchor = lastFp;

  const replied = isThinking && lastMsg && lastMsg.role === 'assistant'
    && lastFp !== _thinkingAnchor;

  const scroll = $('#chat-scroll');
  const want = !!scroll && isThinking && !replied;

  if (_thinkingBubbleEl && (!want || _thinkingBubbleEl.parentNode !== scroll)) {
    _thinkingBubbleEl.remove();
    _thinkingBubbleEl = null;
  }
  if (!want) return;

  const wasAtBottom =
    (scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight) < 80;

  if (_thinkingBubbleEl) {
    // Keep it below any message that arrived while thinking.
    if (scroll.lastElementChild !== _thinkingBubbleEl) {
      scroll.appendChild(_thinkingBubbleEl);
      if (wasAtBottom) scroll.scrollTop = scroll.scrollHeight;
    }
    return;
  }

  const div = document.createElement('div');
  div.className = 'msg assistant thinking-msg';

  const avatar = document.createElement('div');
  avatar.className = 'msg-avatar';
  const img = document.createElement('img');
  img.src = '/web/Lumi.png';
  img.alt = '';
  img.onerror = () => { img.remove(); avatar.textContent = '◆'; };
  avatar.appendChild(img);
  div.appendChild(avatar);

  const bubble = document.createElement('div');
  bubble.className = 'msg-bubble thinking-bubble';
  bubble.innerHTML = `
    <span class="dot-pulse">
      <span></span><span></span><span></span>
    </span>
  `;
  div.appendChild(bubble);
  scroll.appendChild(div);

  _thinkingBubbleEl = div;
  if (wasAtBottom) scroll.scrollTop = scroll.scrollHeight;
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
    _statusFp.value = null;
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
   View dispatcher
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
    case 'integrations': return renderIntegrations(main);
    case 'stream':       return renderStream(main);
    case 'settings':     return renderSettings(main);
  }
}

/* --------------------------------------------------------------
   Modals — shared shell for every dialog.
   Dialogs stack instead of wiping each other, Escape closes the
   topmost one, and onClose always runs exactly once, however the
   dialog was dismissed.
   -------------------------------------------------------------- */
const _modalStack = [];

function openModal({ title = '', wide = false, onClose = null } = {}) {
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';

  const card = document.createElement('div');
  card.className = 'modal-card' + (wide ? ' modal-wide' : '');
  card.setAttribute('role', 'dialog');
  card.setAttribute('aria-modal', 'true');
  card.tabIndex = -1;

  const header = document.createElement('div');
  header.className = 'modal-header';
  const titleEl = document.createElement('div');
  titleEl.className = 'modal-title';
  titleEl.textContent = title;
  const closeBtn = document.createElement('button');
  closeBtn.className = 'modal-close';
  closeBtn.textContent = '×';
  closeBtn.setAttribute('aria-label', 'Close');
  header.appendChild(titleEl);
  header.appendChild(closeBtn);
  card.appendChild(header);
  overlay.appendChild(card);

  const prevFocus = document.activeElement;
  let closed = false;
  const modal = {
    overlay,
    card,
    close() {
      if (closed) return;
      closed = true;
      overlay.remove();
      const i = _modalStack.indexOf(modal);
      if (i !== -1) _modalStack.splice(i, 1);
      if (prevFocus && prevFocus.isConnected) prevFocus.focus?.();
      if (onClose) onClose();
    },
  };

  closeBtn.addEventListener('click', () => modal.close());

  // Only a click that both starts and ends on the backdrop dismisses —
  // dragging a text selection out of a field shouldn't close the dialog.
  let downOnBackdrop = false;
  overlay.addEventListener('mousedown', (e) => {
    downOnBackdrop = e.target === overlay;
  });
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay && downOnBackdrop) modal.close();
  });

  _modalStack.push(modal);
  document.body.appendChild(overlay);
  card.focus();
  return modal;
}

document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape' || !_modalStack.length) return;
  e.preventDefault();
  _modalStack[_modalStack.length - 1].close();
});

/* ==============================================================
   CHAT
   ============================================================== */
let _chatPollTimer = null;
let _chatFp = null;
let _chatRenderedCount = 0;

const _chatAttachments = [];

const _MAX_UPLOAD_BYTES = 25 * 1024 * 1024;

function startChatPolling() {
  stopChatPolling();
  _chatFp = null;
  _chatRenderedCount = 0;
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

    const nextFp = fingerprint(next);
    if (nextFp === _chatFp) return;

    const prev = state.history;
    const prevLen = prev.length;

    const inSync = _chatRenderedCount === prevLen;
    const canAppend =
      inSync &&
      prevLen <= next.length &&
      fingerprint(next.slice(0, prevLen)) === fingerprint(prev);
    // A streaming reply edits the newest entry in place — same length,
    // everything before it unchanged. Patch that one bubble instead of
    // redrawing (and re-animating) the whole conversation.
    const canPatchLast =
      !canAppend && inSync &&
      prevLen > 0 && next.length === prevLen &&
      fingerprint(next.slice(0, -1)) === fingerprint(prev.slice(0, -1));

    state.history = next;
    _chatFp = nextFp;

    if (canAppend) {
      appendNewMessages(next, prevLen);
    } else if (canPatchLast) {
      patchLastMessage(next[next.length - 1]);
    } else {
      renderChat();
    }
  } catch {
    // Next tick retries.
  }
}

function isChatAtBottom(scroll) {
  return (scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight) < 80;
}

function scrollChatToBottom(scroll, smooth = false) {
  scroll.scrollTo({ top: scroll.scrollHeight, behavior: smooth ? 'smooth' : 'auto' });
}

// "↓ Latest" button: visible whenever the reader has scrolled away from
// the bottom; says "New messages" if something arrived meanwhile.
let _chatUnseen = 0;

function updateJumpButton() {
  const scroll = $('#chat-scroll');
  const btn = $('#chat-jump');
  if (!scroll || !btn) return;
  const atBottom = isChatAtBottom(scroll);
  if (atBottom) _chatUnseen = 0;
  btn.classList.toggle('is-away', atBottom);
  btn.tabIndex = atBottom ? -1 : 0;
  btn.classList.toggle('has-new', _chatUnseen > 0);
  btn.textContent = _chatUnseen > 0 ? '↓ New messages' : '↓ Latest';
}

function appendNewMessages(fullHistory, fromIndex) {
  const scroll = $('#chat-scroll');
  if (!scroll) return;

  const wasAtBottom = isChatAtBottom(scroll);

  for (let i = fromIndex; i < fullHistory.length; i++) {
    renderChatMessage(fullHistory[i], scroll);
  }
  _chatRenderedCount = fullHistory.length;

  // Instant, not smooth: a smooth follow would briefly read as
  // "scrolled away" and flash the jump button on every message.
  if (wasAtBottom) scrollChatToBottom(scroll);
  else _chatUnseen += fullHistory.length - fromIndex;
  syncMoodFromLatest();
  updateTypingIndicator();
  updateJumpButton();
}

function patchLastMessage(msg) {
  const scroll = $('#chat-scroll');
  if (!scroll) return;
  const rendered = scroll.querySelectorAll('.msg:not(.thinking-msg)');
  const old = rendered[rendered.length - 1];
  if (!old) { renderChat(); return; }

  const wasAtBottom = isChatAtBottom(scroll);
  old.replaceWith(buildChatMessage(msg, { animate: false }));
  if (wasAtBottom) scrollChatToBottom(scroll);
  syncMoodFromLatest();
  updateTypingIndicator();
  updateJumpButton();
}

async function renderChatView(main) {
  main.innerHTML = `
    <div class="page">
      <div class="chat-wrap">

        <!-- Blurred video loop + stars, behind everything -->
        <div class="chat-bg" aria-hidden="true">
          <div class="chat-bg-stars"></div>
          <div class="chat-bg-stars-2"></div>
          <div class="chat-bg-rising">
            <span></span><span></span><span></span><span></span><span></span>
            <span></span><span></span><span></span><span></span><span></span>
            <span></span><span></span><span></span><span></span><span></span>
            <span></span><span></span><span></span><span></span><span></span>
          </div>
          <div class="chat-bg-mood"></div>
        </div>

        <div class="chat-scroll-wrap">
          <div class="chat-scroll" id="chat-scroll" aria-live="polite"></div>
          <button class="chat-jump is-away" id="chat-jump" type="button"
                  title="Scroll to the newest message">↓ Latest</button>
        </div>

        <div class="chat-status">
          <button class="mode-chip" id="agent-chip"
                  title="Toggle agent mode">⚡ Agent mode: OFF</button>
          <button class="mode-chip" id="tts-toggle"
                  title="Toggle voice playback in this tab">🔇 Voice: OFF</button>
          <button class="mode-chip" id="mic-toggle"
                  title="Hold to record a voice message">🎤 Hold to talk</button>
          <span class="grow"></span>
          <span class="mood-chip" id="mood-chip" title="Lumi's current mood">
            <span class="mood-emoji" id="mood-emoji">🌙</span>
            <span id="mood-label">calm</span>
          </span>
          <span class="muted" id="chat-host"></span>
        </div>
        <div class="chat-attachments" id="chat-attachments"></div>
        <div class="chat-input">
          <button class="chat-attach-btn" id="chat-attach"
                  title="Attach a file (PDF, DOCX, text, image)">📎</button>
          <input type="file" id="chat-file-input" hidden multiple
                 accept=".pdf,.docx,.doc,.txt,.md,.json,.yaml,.yml,.csv,.log,.png,.jpg,.jpeg,.gif,.webp,.bmp">
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
  // The keyboard hint wraps and gets clipped on narrow screens, where
  // there's usually no Shift key anyway.
  if (window.matchMedia?.('(max-width: 760px)').matches) {
    input.placeholder = 'Message Lumi…';
  }
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

  $('#chat-attach').addEventListener('click', () => {
    $('#chat-file-input').click();
  });
  $('#chat-file-input').addEventListener('change', (e) => {
    handleFiles(Array.from(e.target.files || []));
    e.target.value = '';
  });

  const chatWrap = document.querySelector('.chat-wrap');
  if (chatWrap) {
    chatWrap.addEventListener('dragover', (e) => {
      e.preventDefault();
      chatWrap.classList.add('drag-over');
    });
    chatWrap.addEventListener('dragleave', () => {
      chatWrap.classList.remove('drag-over');
    });
    chatWrap.addEventListener('drop', (e) => {
      e.preventDefault();
      chatWrap.classList.remove('drag-over');
      handleFiles(Array.from(e.dataTransfer.files || []));
    });
  }

  input.addEventListener('paste', (e) => {
    const items = e.clipboardData?.items || [];
    const files = [];
    for (const it of items) {
      if (it.kind === 'file') {
        const f = it.getAsFile();
        if (f) files.push(f);
      }
    }
    if (files.length) {
      e.preventDefault();
      handleFiles(files);
    }
  });

  $('#chat-send').addEventListener('click', sendChat);
  $('#agent-chip').addEventListener('click', toggleAgentMode);

  const chatScroll = $('#chat-scroll');
  chatScroll.addEventListener('scroll', updateJumpButton, { passive: true });
  $('#chat-jump').addEventListener('click', () => {
    scrollChatToBottom(chatScroll, true);
  });

  renderAttachmentChips();
  initVoiceControls();
}

function renderChat() {
  const scroll = $('#chat-scroll');
  if (!scroll) return;

  const wasAtBottom = isChatAtBottom(scroll);
  const prevTop = scroll.scrollTop;

  // Build off-DOM and swap in one go. Rows drawn by a full redraw
  // don't play the pop-in animation — only genuinely new ones do.
  const frag = document.createDocumentFragment();
  state.history.forEach(m => frag.appendChild(buildChatMessage(m, { animate: false })));
  scroll.replaceChildren(frag);
  _thinkingBubbleEl = null;

  _chatRenderedCount = state.history.length;
  _chatFp = fingerprint(state.history);

  // Stay pinned to the bottom if we were there; otherwise keep the
  // reader where they were instead of snapping to the top.
  if (wasAtBottom) scrollChatToBottom(scroll);
  else scroll.scrollTop = prevTop;
  syncMoodFromLatest();
  updateTypingIndicator();
  updateJumpButton();
}

function renderChatMessage(msg, container = null) {
  const scroll = container || $('#chat-scroll');
  if (!scroll) return;
  scroll.appendChild(buildChatMessage(msg));
}

// Emotion tags like "[happy]" that the TTS layer understands.
function extractTags(raw) {
  return [...String(raw || '').matchAll(/\[([a-zA-Z][a-zA-Z\s\-]{0,30})\]/g)]
    .map(m => m[1].toLowerCase())
    .filter(t => isCanonicalTag(t));
}

// Drive the mood accent from the newest message, once per message —
// a later redraw of the same message won't revive a mood that has
// already faded.
let _moodSourceFp = null;

function syncMoodFromLatest() {
  const last = state.history[state.history.length - 1];
  if (!last || last.role !== 'assistant') return;
  const fp = fingerprint(last);
  if (fp === _moodSourceFp) return;
  _moodSourceFp = fp;
  const tags = extractTags(last.content);
  if (tags.length) applyMoodAccent(tags);
}

function buildChatMessage(msg, { animate = true } = {}) {
  const role = msg.role || 'assistant';
  const div = document.createElement('div');
  div.className = `msg ${role}` + (animate ? '' : ' msg-static');

  const avatar = document.createElement('div');
  avatar.className = 'msg-avatar';
  if (role === 'assistant') {
    const img = document.createElement('img');
    img.src = '/web/Lumi.png';
    img.alt = '';
    img.onerror = () => {
      img.remove();
      avatar.textContent = '◆';
    };
    avatar.appendChild(img);
  } else {
    avatar.textContent = (state.user?.display_name || '?')
      .slice(0, 1).toUpperCase();
  }
  div.appendChild(avatar);

  const bubble = document.createElement('div');
  bubble.className = 'msg-bubble';

  const raw = String(msg.content || '');
  const tags = extractTags(raw);

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

  const atts = msg.attachments;
  if (Array.isArray(atts) && atts.length) {
    const attRow = document.createElement('div');
    attRow.className = 'msg-attachments';
    for (const a of atts) {
      const badge = document.createElement('span');
      badge.className = 'msg-attachment-badge';
      const icon = a.kind === 'image' ? '🖼️'
                 : a.kind === 'pdf'   ? '📄'
                 : a.kind === 'doc'   ? '📝'
                 : '📎';
      badge.textContent = `${icon} ${a.name || 'file'}`;
      attRow.appendChild(badge);
    }
    bubble.appendChild(attRow);
  }

  const cleaned = raw.replace(
    /\[([a-zA-Z][a-zA-Z\s\-]{0,30})\]/g,
    (m, inner) => isCanonicalTag(inner.toLowerCase()) ? '' : m,
  ).replace(/[ \t]{2,}/g, ' ').trim();

  const text = document.createElement('span');
  text.textContent = cleaned;
  bubble.appendChild(text);

  div.appendChild(bubble);
  return div;
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

/* --------------------------------------------------------------
   Attachments (file upload)
   -------------------------------------------------------------- */

async function handleFiles(files) {
  for (const f of files) {
    if (f.size > _MAX_UPLOAD_BYTES) {
      alert(`"${f.name}" is larger than ` +
            `${Math.round(_MAX_UPLOAD_BYTES / (1024*1024))} MB.`);
      continue;
    }

    const chip = {
      name: f.name,
      kind: 'uploading',
      ready: false,
      error: null,
      replaced: false,
    };
    _chatAttachments.push(chip);
    renderAttachmentChips();

    let replaced = false;

    try {
      let res = await uploadOne(f, /* replace */ false);
      let data = await res.json().catch(() => ({}));

      // Duplicate → prompt the user, then either replace or cancel.
      if (res.status === 409 && data.error === 'duplicate') {
        const choice = await confirmDuplicate(data);
        if (choice !== 'replace') {
          const idx = _chatAttachments.indexOf(chip);
          if (idx !== -1) _chatAttachments.splice(idx, 1);
          renderAttachmentChips();
          continue;
        }
        res = await uploadOne(f, /* replace */ true);
        data = await res.json().catch(() => ({}));
        replaced = true;
      }

      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);

      chip.kind         = data.kind || 'text';
      chip.stored_name  = data.stored_name;
      chip.stored_path  = data.stored_path;
      chip.source       = data.source;
      chip.subfolder    = data.subfolder;
      chip.text         = data.text || '';
      chip.size         = data.size || 0;
      chip.ready        = true;
      chip.error        = null;
      chip.replaced     = replaced;
      if (data.extract_error) chip.extract_error = data.extract_error;
      if (data.text_truncated) chip.truncated = true;
    } catch (e) {
      chip.error = e.message;
    }
    renderAttachmentChips();
  }
}

async function uploadOne(file, replace) {
  const fd = new FormData();
  fd.append('file', file, file.name);
  const url = replace
    ? '/api/chat/upload?replace=1'
    : '/api/chat/upload';
  return fetch(url, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'X-CSRF-Token': state.csrf || '' },
    body: fd,
  });
}

// Duplicate prompts run one at a time, so dropping a second batch of
// files while a prompt is open queues behind it instead of replacing it.
let _dupPromptQueue = Promise.resolve();

function confirmDuplicate(info) {
  const result = _dupPromptQueue.then(() => showDuplicatePrompt(info));
  _dupPromptQueue = result.catch(() => {});
  return result;
}

function showDuplicatePrompt(info) {
  return new Promise((resolve) => {
    let choice = 'cancel';
    const modal = openModal({
      title: 'File already exists',
      onClose: () => resolve(choice),
    });

    const existing = info.existing || {};
    const incomingName = info.incoming?.name || 'This file';
    const existingSize = formatBytesLocal(existing.size || 0);
    const existingWhen = existing.modified_at
      ? new Date(existing.modified_at * 1000).toLocaleString()
      : 'unknown';

    const body = document.createElement('div');
    body.innerHTML = `
        <div style="font-size:13px;line-height:1.6;color:var(--text)">
          <strong>${escapeHtml(incomingName)}</strong> has
          <strong>identical contents</strong> to a file already in
          your library:
          <div style="margin:12px 0;padding:12px 14px;
                      background:var(--bg-inset);
                      border:1px solid var(--border);
                      border-radius:var(--r-md);
                      font-family:var(--font-mono);font-size:12px">
            <div style="color:var(--text)">${escapeHtml(existing.original_name || 'unknown')}</div>
            <div style="color:var(--text-mute);margin-top:4px">
              ${escapeHtml(existing.subfolder || '')}/ &middot; ${existingSize}
            </div>
            <div style="color:var(--text-mute);margin-top:2px">
              uploaded ${escapeHtml(existingWhen)}
            </div>
          </div>
          Replace the existing copy with this one, or cancel and keep
          the original?
        </div>
        <div class="modal-actions">
          <button class="btn" data-action="cancel">Cancel</button>
          <button class="btn btn-primary" data-action="replace">Replace</button>
        </div>
    `;
    modal.card.appendChild(body);

    const cancelBtn = body.querySelector('[data-action="cancel"]');
    cancelBtn.addEventListener('click', () => modal.close());
    body.querySelector('[data-action="replace"]').addEventListener('click', () => {
      choice = 'replace';
      modal.close();
    });
    cancelBtn.focus();
  });
}

function formatBytesLocal(n) {
  if (!n || n < 0) return '—';
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0, v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v < 10 ? 2 : 1)} ${units[i]}`;
}

function renderAttachmentChips() {
  const el = $('#chat-attachments');
  if (!el) return;
  el.innerHTML = '';

  for (let i = 0; i < _chatAttachments.length; i++) {
    const a = _chatAttachments[i];
    const chip = document.createElement('div');
    chip.className = 'chat-attachment-chip';

    if (a.error) chip.classList.add('error');
    else if (!a.ready) chip.classList.add('uploading');

    const icon = a.kind === 'image' ? '🖼️'
               : a.kind === 'pdf'   ? '📄'
               : a.kind === 'doc'   ? '📝'
               : a.kind === 'uploading' ? '⏳'
               : '📎';

    const label = document.createElement('span');
    label.className = 'chat-attachment-label';
    if (a.error) {
      label.textContent = `${a.name} — ${a.error}`;
    } else if (!a.ready) {
      label.textContent = `${a.name} — uploading…`;
    } else {
      const kb = Math.round((a.size || 0) / 1024);
      const parts = [a.name];
      if (a.subfolder) parts.push(`→ ${a.subfolder}/`);
      parts.push(`(${kb} KB)`);
      if (a.replaced) parts.push('· replaced');
      if (a.truncated) parts.push('· truncated');
      label.textContent = parts.join(' ');
    }

    const remove = document.createElement('button');
    remove.className = 'chat-attachment-remove';
    remove.textContent = '×';
    remove.title = 'Remove';
    remove.addEventListener('click', () => {
      _chatAttachments.splice(i, 1);
      renderAttachmentChips();
    });

    chip.appendChild(document.createTextNode(icon + ' '));
    chip.appendChild(label);
    chip.appendChild(remove);
    el.appendChild(chip);
  }
}

async function sendChat() {
  const input = $('#chat-input');
  if (!input) return;
  const text = input.value.trim();

  const ready = _chatAttachments.filter(a => a.ready && !a.error);
  if (!text && !ready.length) return;

  if (_chatAttachments.some(a => !a.ready)) {
    if (!confirm('Some files are still uploading. Send anyway?')) return;
  }

  const payloadAttachments = ready.map(a => ({
    name:        a.name,
    stored_path: a.stored_path,
    kind:        a.kind,
    text:        a.text,
  }));

  input.value = '';
  input.style.height = 'auto';
  _chatAttachments.length = 0;
  renderAttachmentChips();

  // Same shape /api/history returns, so the next poll sees a matching
  // prefix and appends rather than redrawing everything.
  const localMsg = { role: 'user', content: text };
  if (payloadAttachments.length) {
    localMsg.attachments =
      payloadAttachments.map(a => ({ name: a.name, kind: a.kind }));
  }
  appendLocalMessage(localMsg);

  try {
    await api('/api/chat/send', {
      method: 'POST',
      body: { text, attachments: payloadAttachments },
      csrf: true,
    });
    refreshHistory();
  } catch (err) {
    appendLocalMessage({
      role: 'system',
      content: `Failed to send: ${err.message}`,
    });
  }
}

// Show a message immediately, before the server echoes it back. Your
// own message always scrolls into view.
function appendLocalMessage(msg) {
  state.history.push(msg);
  const scroll = $('#chat-scroll');
  if (!scroll || _chatRenderedCount !== state.history.length - 1) {
    renderChat();
  } else {
    appendNewMessages(state.history, state.history.length - 1);
  }
  if (scroll) scrollChatToBottom(scroll);
  _chatUnseen = 0;
  updateJumpButton();
}

/* ==============================================================
   VOICE IN BROWSER (Phase 10.19)
   ============================================================== */

let _voiceSocket = null;
let _voiceEnabled = false;
let _mediaRecorder = null;
let _mediaChunks = [];
let _micHeld = false;       // true while the talk button is pressed
let _micStarting = false;   // true while getUserMedia is pending

// Preferred recording formats, best first. Chrome/Firefox take webm or
// ogg; Safari only records mp4. Whisper (via ffmpeg) decodes all three.
const RECORDER_MIME_CANDIDATES = [
  'audio/webm;codecs=opus',
  'audio/webm',
  'audio/ogg;codecs=opus',
  'audio/mp4',
];

function pickRecorderMime() {
  if (typeof MediaRecorder.isTypeSupported !== 'function') return '';
  return RECORDER_MIME_CANDIDATES.find(t => MediaRecorder.isTypeSupported(t)) || '';
}

function extForMime(mime) {
  const base = (mime || '').split(';')[0].trim();
  return {
    'audio/webm': 'webm',
    'audio/ogg':  'ogg',
    'audio/mp4':  'mp4',
    'audio/x-m4a': 'm4a',
    'audio/wav':  'wav',
  }[base] || 'webm';
}

function initVoiceControls() {
  const ttsBtn = $('#tts-toggle');
  const micBtn = $('#mic-toggle');

  if (ttsBtn) {
    try { _voiceEnabled = localStorage.getItem('lumi_voice') === 'on'; } catch {}
    updateVoiceButton();
    ttsBtn.addEventListener('click', () => {
      _voiceEnabled = !_voiceEnabled;
      try { localStorage.setItem('lumi_voice', _voiceEnabled ? 'on' : 'off'); } catch {}
      updateVoiceButton();
      ensureVoiceSocket();
    });
  }

  if (micBtn) {
    const supported = typeof MediaRecorder !== 'undefined'
      && navigator.mediaDevices
      && typeof navigator.mediaDevices.getUserMedia === 'function';
    if (!supported) {
      micBtn.disabled = true;
      micBtn.title = 'Voice recording is not supported in this browser.';
    } else {
      // Pointer events cover mouse, touch and pen. Capturing the
      // pointer means a finger sliding off the button still delivers
      // the matching pointerup.
      micBtn.addEventListener('pointerdown', (e) => {
        if (e.button !== 0) return;
        micBtn.setPointerCapture?.(e.pointerId);
        startMicRecording();
      });
      micBtn.addEventListener('pointerup', stopMicRecording);
      micBtn.addEventListener('pointercancel', stopMicRecording);
      micBtn.addEventListener('contextmenu', (e) => e.preventDefault());
    }
  }

  ensureVoiceSocket();
}

function updateVoiceButton() {
  const btn = $('#tts-toggle');
  if (!btn) return;
  btn.textContent = _voiceEnabled ? '🔊 Voice: ON' : '🔇 Voice: OFF';
  btn.classList.toggle('agent', _voiceEnabled);
}

function ensureVoiceSocket() {
  if (typeof io === 'undefined') return;

  // One socket per page load. socket.io reconnects on its own and the
  // 'connect' handler re-sends the opt-in, so never open a second one —
  // each extra socket would play every clip again.
  if (_voiceSocket) {
    if (_voiceSocket.connected) {
      _voiceSocket.emit('web_voice_optin', { enabled: _voiceEnabled });
    }
    return;
  }

  try {
    _voiceSocket = io({
      transports: ['websocket', 'polling'],
      withCredentials: true,
    });

    _voiceSocket.on('connect', () => {
      _voiceSocket.emit('web_voice_optin', { enabled: _voiceEnabled });
    });

    _voiceSocket.on('tts_audio', (payload) => {
      if (!_voiceEnabled) return;
      if (!payload || !payload.data) return;
      playTtsAudio(payload.data);
    });
  } catch (e) {
    console.warn('voice socket failed', e);
  }
}

function playTtsAudio(b64) {
  try {
    const bytes = atob(b64);
    const arr = new Uint8Array(bytes.length);
    for (let i = 0; i < bytes.length; i++) arr[i] = bytes.charCodeAt(i);
    const blob = new Blob([arr], { type: 'audio/wav' });
    const url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    audio.addEventListener('ended', () => URL.revokeObjectURL(url));
    audio.play().catch(() => URL.revokeObjectURL(url));
  } catch (e) {
    console.warn('audio playback failed', e);
  }
}

async function startMicRecording() {
  const btn = $('#mic-toggle');
  if (!btn || btn.disabled) return;
  _micHeld = true;
  if (_micStarting) return;
  if (_mediaRecorder && _mediaRecorder.state === 'recording') return;

  _micStarting = true;
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) {
    _micHeld = false;
    alert('Microphone permission denied.');
    return;
  } finally {
    _micStarting = false;
  }

  // Released while the permission prompt / device open was pending —
  // don't start a recording nobody is holding the button for.
  if (!_micHeld) {
    stream.getTracks().forEach(t => t.stop());
    return;
  }

  const requestedMime = pickRecorderMime();
  let recorder;
  try {
    recorder = requestedMime
      ? new MediaRecorder(stream, { mimeType: requestedMime })
      : new MediaRecorder(stream);
  } catch (e) {
    stream.getTracks().forEach(t => t.stop());
    _micHeld = false;
    alert('Could not start recording: ' + e.message);
    return;
  }

  _mediaChunks = [];
  _mediaRecorder = recorder;
  recorder.addEventListener('dataavailable', (e) => {
    if (e.data.size > 0) _mediaChunks.push(e.data);
  });
  recorder.addEventListener('stop', async () => {
    stream.getTracks().forEach(t => t.stop());
    // recorder.mimeType is what the browser actually produced, which
    // can differ from what we asked for.
    const mime = recorder.mimeType || requestedMime || 'audio/webm';
    const blob = new Blob(_mediaChunks, { type: mime });
    _mediaChunks = [];
    if (blob.size < 1000) return;

    btn.textContent = '🎤 Transcribing…';
    btn.disabled = true;
    try {
      const fd = new FormData();
      fd.append('audio', blob, `clip.${extForMime(mime)}`);
      const res = await fetch('/api/voice_message', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'X-CSRF-Token': state.csrf || '' },
        body: fd,
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      if (data.text) {
        appendLocalMessage({ role: 'user', content: data.text });
      }
    } catch (e) {
      alert('Voice message failed: ' + e.message);
    } finally {
      btn.textContent = '🎤 Hold to talk';
      btn.disabled = false;
    }
  });

  recorder.start();
  btn.textContent = '🎤 Recording… (release)';
  btn.classList.add('agent');
}

function stopMicRecording() {
  _micHeld = false;
  const btn = $('#mic-toggle');
  if (_mediaRecorder && _mediaRecorder.state === 'recording') {
    _mediaRecorder.stop();
  }
  if (btn) btn.classList.remove('agent');
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

  // Events come through the web server so this works from any device.
  // Remote sessions can't read them (they can include window titles
  // and file names from the host), so say so instead of showing an
  // empty list.
  let events = [];
  let eventsError = null;
  if (state.isLocal) {
    try {
      const res = await api('/api/log_events');
      events = res.events || [];
    } catch (e) {
      eventsError = `Couldn't load events: ${e.message}`;
    }
  } else {
    eventsError = 'Log events are local-only. Open the dashboard on the ' +
                  'host machine to see them.';
  }
  state.events = events;

  renderDashboardStats();
  renderEventFilters();
  if (eventsError) {
    const el = $('#dash-events');
    el.innerHTML = '';
    const note = document.createElement('div');
    note.className = 'empty';
    note.style.padding = '24px';
    note.textContent = eventsError;
    el.appendChild(note);
  } else {
    renderEvents(events);
  }

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
      <div class="stat-label">${escapeHtml(st.label)}</div>
      <div class="stat-value">${escapeHtml(st.value)}</div>
    </div>
  `).join('');
}

function renderEventFilters() {
  const cats = ['all','ambient','heartbeat','memory','emotional',
                'mic','vts','discord','system'];
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
      <span class="ts">${escapeHtml(ev.time || '')}</span>
      <span class="cat ${escapeHtml(ev.category || '')}">${escapeHtml(ev.category || '')}</span>
      <span class="ev-msg">${escapeHtml(ev.message ?? '')}</span>
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
  const sort = ($('#memory-sort')?.value || 'recent');
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
    const toggle = () => {
      if (_memoryState.expanded === mem.id) {
        _memoryState.expanded = null;
      } else {
        _memoryState.expanded = mem.id;
      }
      renderMemoryTable();
      const el = document.querySelector(`.memory-row[data-id="${CSS.escape(mem.id)}"]`);
      if (el) {
        el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        el.focus({ preventScroll: true });
      }
    };
    // Focusable so the details can be opened from the keyboard; the
    // Edit/Delete buttons inside keep their own keys.
    row.tabIndex = 0;
    row.setAttribute('aria-expanded', String(_memoryState.expanded === mem.id));
    row.addEventListener('click', toggle);
    row.addEventListener('keydown', (e) => {
      if (e.target !== row) return;
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        toggle();
      }
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
      ['User',       mem.user_id || '(unknown)'],
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
  const modal = openModal({
    title: mode === 'create' ? 'Add memory' : 'Edit memory',
  });
  const { card } = modal;

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
  cancelBtn.addEventListener('click', () => modal.close());
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
      modal.close();
      await loadMemories();
    } catch (e) {
      alert('Save failed: ' + e.message);
      saveBtn.disabled = false;
    }
  });
  actions.appendChild(cancelBtn);
  actions.appendChild(saveBtn);
  card.appendChild(actions);

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
  const modal = openModal({ title: 'System Prompt Preview', wide: true });
  const { card } = modal;

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
  okBtn.addEventListener('click', () => modal.close());
  actions.appendChild(copyBtn);
  actions.appendChild(okBtn);
  card.appendChild(actions);
}

/* ==============================================================
   MODELS + PROVIDERS (Phase 10.12 / 10.12b)
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
  providers: [],
  providerCatalog: [],
  providerErrors: {},
  routingDirty: false,
};

function stopModelsTimers() {
  if (_modelsTimer) { clearInterval(_modelsTimer); _modelsTimer = null; }
  stopPullPolling();
}

function stopPullPolling() {
  if (_pullTimer) { clearInterval(_pullTimer); _pullTimer = null; }
}

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
  _modelsTimer = setInterval(loadModelsPage, 30000);
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

  try {
    const prov = await api('/api/providers');
    _modelsState.providers       = prov.providers || [];
    _modelsState.providerCatalog = prov.catalog   || [];
    _modelsState.providerErrors  = prov.errors    || {};
    console.debug('[models] catalog size', _modelsState.providerCatalog.length,
                  'errors', _modelsState.providerErrors);
  } catch (e) {
    console.error('[models] /api/providers failed', e);
    _modelsState.providers       = [];
    _modelsState.providerCatalog = [];
    _modelsState.providerErrors  = {};
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

function renderInstalledSection() {
  const host = $('#models-installed-section');
  if (!host) return;

  if (host.contains(document.activeElement)) return;

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
    'except the provider\u2019s own API.';
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
  const modal = openModal({ title: p.label });
  const { card } = modal;

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
      modal.close();
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
  cancelBtn.addEventListener('click', () => modal.close());
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
      modal.close();
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
      modal.close();
      loadModelsPage();
    } catch (e) {
      alert('Save failed: ' + e.message);
      saveBtn.disabled = false;
      saveBtn.textContent = 'Save';
    }
  });
  actions.appendChild(saveBtn);

  card.appendChild(actions);

  setTimeout(() => keyInput.focus(), 50);
}

function renderRoutingSection() {
  const host = $('#models-routing-section');
  if (!host) return;

  if (_modelsState.routingDirty) return;

  if (host.contains(document.activeElement)) return;

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

  const markDirty = () => {
    if (_modelsState.routingDirty) return;
    _modelsState.routingDirty = true;
    const hint = document.getElementById('routing-dirty-hint');
    if (hint) hint.style.display = '';
  };
  grid.querySelectorAll('input, select').forEach(el => {
    el.addEventListener('input', markDirty);
    el.addEventListener('change', markDirty);
  });

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

  const dirtyHint = document.createElement('span');
  dirtyHint.id = 'routing-dirty-hint';
  dirtyHint.className = 'muted';
  dirtyHint.style.marginLeft = '12px';
  dirtyHint.style.fontSize = '12px';
  dirtyHint.style.display = 'none';
  dirtyHint.textContent = 'Unsaved changes — polling paused';
  actions.appendChild(dirtyHint);

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

  const cloudIds = new Set((_modelsState.providers || [])
    .filter(p => p.id !== 'ollama').map(p => p.id));
  const isCloudSpec = (v) => !!v && v.includes(':')
    && cloudIds.has(v.split(':')[0].toLowerCase());

  const localGroup = document.createElement('optgroup');
  localGroup.label = 'Ollama (local)';

  const localNames = names.slice();
  if (current && !localNames.includes(current)
      && !isCloudSpec(current)) {
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

  for (const p of _modelsState.providers) {
    if (p.id === 'ollama' || !p.configured) continue;
    if (byProvider[p.label]) continue;
    const grp = document.createElement('optgroup');
    grp.label = p.label;
    const opt = document.createElement('option');
    opt.disabled = true;
    opt.textContent = '(failed to load: ' +
      ((_modelsState.providerErrors || {})[p.id] || 'no models') + ')';
    grp.appendChild(opt);
    sel.appendChild(grp);
  }

  if (isCloudSpec(current)
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
    _modelsState.routingDirty = false;
    const hint = document.getElementById('routing-dirty-hint');
    if (hint) hint.style.display = 'none';

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
   INTEGRATIONS (Phase 10.13)
   ============================================================== */

const _integrationsState = {
  items: [],
  loading: false,
};

async function renderIntegrations(main) {
  main.innerHTML = `
    <div class="page">
      <div class="page-header">
        <div>
          <div class="page-title">Integrations</div>
          <div class="page-sub">Connect Lumi to external services.</div>
        </div>
        <button class="btn btn-sm" id="integrations-refresh">Refresh</button>
      </div>
      <div class="page-scroll">
        <div class="integration-grid" id="integrations-grid">
          <div class="integration-empty">Loading…</div>
        </div>
      </div>
    </div>
  `;

  $('#integrations-refresh').addEventListener('click', loadIntegrations);
  await loadIntegrations();
}

async function loadIntegrations() {
  const grid = $('#integrations-grid');
  if (!grid) return;
  try {
    const res = await api('/api/integrations');
    _integrationsState.items = res.integrations || [];
    renderIntegrationCards();
  } catch (e) {
    grid.innerHTML = `<div class="integration-empty">
      Failed to load: ${escapeHtml(e.message)}</div>`;
  }
}

function renderIntegrationCards() {
  const grid = $('#integrations-grid');
  if (!grid) return;
  grid.innerHTML = '';
  if (!_integrationsState.items.length) {
    grid.innerHTML = '<div class="integration-empty">No integrations available.</div>';
    return;
  }
  for (const it of _integrationsState.items) {
    grid.appendChild(buildIntegrationCard(it));
  }
}

const _INTEGRATION_STATE_LABEL = {
  ok:   'connected',
  warn: 'warning',
  err:  'error',
  off:  'not configured',
};

function buildIntegrationCard(it) {
  const card = document.createElement('div');
  card.className = 'integration-card';

  const header = document.createElement('div');
  header.className = 'integration-header';

  const emoji = document.createElement('div');
  emoji.className = 'integration-emoji';
  emoji.textContent = it.emoji || '◆';
  header.appendChild(emoji);

  const titleWrap = document.createElement('div');
  titleWrap.className = 'integration-title-wrap';

  const name = document.createElement('div');
  name.className = 'integration-name';
  name.textContent = it.label;
  titleWrap.appendChild(name);

  const status = document.createElement('span');
  status.className = `integration-status state-${it.state}`;
  status.textContent = _INTEGRATION_STATE_LABEL[it.state] || it.state;
  titleWrap.appendChild(status);

  header.appendChild(titleWrap);
  card.appendChild(header);

  const desc = document.createElement('div');
  desc.className = 'integration-desc';
  desc.textContent = it.description;
  card.appendChild(desc);

  if (it.message) {
    const msg = document.createElement('div');
    msg.className = 'integration-message';
    msg.textContent = it.message;
    card.appendChild(msg);
  }

  if (it.has_toggle) {
    const row = document.createElement('div');
    row.className = 'integration-toggle-row';

    const label = document.createElement('span');
    label.textContent = 'Module enabled';
    row.appendChild(label);

    const sw = document.createElement('label');
    sw.className = 'switch';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = !!it.toggle_on;
    cb.addEventListener('change', () =>
      onToggleIntegration(it.id, cb.checked));
    const slider = document.createElement('span');
    slider.className = 'switch-slider';
    sw.appendChild(cb);
    sw.appendChild(slider);
    row.appendChild(sw);

    card.appendChild(row);
  }

  const actions = document.createElement('div');
  actions.className = 'integration-actions';

  if (it.fields && it.fields.length) {
    const cfgBtn = document.createElement('button');
    cfgBtn.className = 'btn btn-sm';
    cfgBtn.textContent = it.fields.some(f => f.is_set) ? 'Edit' : 'Configure';
    cfgBtn.addEventListener('click', () => openIntegrationModal(it));
    actions.appendChild(cfgBtn);
  }

  const testBtn = document.createElement('button');
  testBtn.className = 'btn btn-sm';
  testBtn.textContent = 'Test';
  testBtn.addEventListener('click', () => onTestIntegration(it.id, testBtn));
  actions.appendChild(testBtn);

  if (it.docs) {
    const link = document.createElement('a');
    link.className = 'integration-link';
    link.href = it.docs;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.textContent = 'Docs ↗';
    actions.appendChild(link);
  }

  card.appendChild(actions);
  return card;
}

async function onTestIntegration(id, btn) {
  const prev = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Testing…';
  try {
    const res = await api(`/api/integrations/${encodeURIComponent(id)}/test`, {
      method: 'POST', body: {}, csrf: true,
    });
    alert((res.ok ? '✓ ' : '✗ ') + res.message);
    if (res.ok) loadIntegrations();
  } catch (e) {
    alert('Test failed: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = prev;
  }
}

async function onToggleIntegration(id, enabled) {
  try {
    await api(`/api/integrations/${encodeURIComponent(id)}/toggle`, {
      method: 'POST', body: { enabled }, csrf: true,
    });
    loadIntegrations();
  } catch (e) {
    alert('Toggle failed: ' + e.message);
    loadIntegrations();
  }
}

function openIntegrationModal(it) {
  const modal = openModal({ title: `${it.emoji || ''} ${it.label}`.trim() });
  const { card } = modal;

  const hint = document.createElement('div');
  hint.className = 'provider-hint';
  hint.style.marginBottom = '16px';
  hint.textContent =
    'Values are written to .env with restricted permissions. ' +
    'Leave a field blank to keep its current value. ' +
    'Some changes require a restart to take effect.';
  card.appendChild(hint);

  const inputs = {};
  for (const f of it.fields) {
    const field = document.createElement('div');
    field.className = 'field';

    const lbl = document.createElement('label');
    lbl.className = 'label';
    lbl.textContent = f.label;
    field.appendChild(lbl);

    const inp = document.createElement('input');
    inp.type = f.secret ? 'password' : 'text';
    inp.className = 'input';
    if (f.is_set) {
      inp.placeholder = f.secret
        ? `Current: ${f.masked} — leave blank to keep`
        : `Current: ${f.masked || 'set'} — leave blank to keep`;
    } else {
      inp.placeholder = f.placeholder || '';
    }
    field.appendChild(inp);
    inputs[f.key] = inp;
    card.appendChild(field);
  }

  const actions = document.createElement('div');
  actions.className = 'modal-actions';

  const anySet = it.fields.some(f => f.is_set);
  if (anySet) {
    const clearBtn = document.createElement('button');
    clearBtn.className = 'btn btn-sm btn-danger';
    clearBtn.textContent = 'Clear all';
    clearBtn.style.marginRight = 'auto';
    clearBtn.addEventListener('click', async () => {
      if (!confirm(`Remove all stored values for ${it.label}?`)) return;
      const values = {};
      for (const f of it.fields) values[f.key] = '';
      clearBtn.disabled = true;
      try {
        await api(`/api/integrations/${encodeURIComponent(it.id)}`, {
          method: 'PUT', body: { values }, csrf: true,
        });
        modal.close();
        loadIntegrations();
      } catch (e) {
        alert('Clear failed: ' + e.message);
        clearBtn.disabled = false;
      }
    });
    actions.appendChild(clearBtn);
  }

  const cancelBtn = document.createElement('button');
  cancelBtn.className = 'btn';
  cancelBtn.textContent = 'Cancel';
  cancelBtn.addEventListener('click', () => modal.close());
  actions.appendChild(cancelBtn);

  const saveBtn = document.createElement('button');
  saveBtn.className = 'btn btn-primary';
  saveBtn.textContent = 'Save';
  saveBtn.addEventListener('click', async () => {
    const values = {};
    for (const [key, inp] of Object.entries(inputs)) {
      const v = inp.value.trim();
      if (v) values[key] = v;
    }
    if (!Object.keys(values).length) {
      modal.close();
      return;
    }
    saveBtn.disabled = true;
    saveBtn.textContent = 'Saving…';
    try {
      await api(`/api/integrations/${encodeURIComponent(it.id)}`, {
        method: 'PUT', body: { values }, csrf: true,
      });
      modal.close();
      loadIntegrations();
    } catch (e) {
      alert('Save failed: ' + e.message);
      saveBtn.disabled = false;
      saveBtn.textContent = 'Save';
    }
  });
  actions.appendChild(saveBtn);

  card.appendChild(actions);

  const firstEmpty = it.fields.find(f => !f.is_set);
  if (firstEmpty && inputs[firstEmpty.key]) {
    setTimeout(() => inputs[firstEmpty.key].focus(), 50);
  }
}

/* ==============================================================
   STREAM — OBS control, avatar embed, remote access info
   ============================================================== */

const _streamState = {
  data:      null,
  fp:        null,
  pollTimer: null,
  busy:      false,
};

function stopStreamPolling() {
  if (_streamState.pollTimer) clearInterval(_streamState.pollTimer);
  _streamState.pollTimer = null;
  _streamState.fp = null;
}

async function renderStream(main) {
  main.innerHTML = `
    <div class="page">
      <div class="page-header">
        <div>
          <div class="page-title">Stream</div>
          <div class="page-sub">Control OBS and watch the avatar from anywhere.</div>
        </div>
        <button class="btn btn-sm" id="stream-refresh">Refresh</button>
      </div>
      <div class="page-scroll">
        <div class="stream-layout" id="stream-body">
          <div class="integration-empty">Loading…</div>
        </div>
      </div>
    </div>
  `;
  $('#stream-refresh').addEventListener('click', () => loadStream(true));
  await loadStream(true);
  stopStreamPolling();
  _streamState.pollTimer = setInterval(() => {
    if (state.view === 'stream' && !document.hidden) loadStream(false);
  }, 3000);
}

async function loadStream(force) {
  if (_streamState.busy) return;
  try {
    const data = await api('/api/stream/status');
    applyStreamData(data, force);
  } catch (e) {
    const body = $('#stream-body');
    if (body) body.innerHTML = `<div class="integration-empty">
      Failed to load: ${escapeHtml(e.message)}</div>`;
  }
}

function applyStreamData(data, force = false) {
  const fp = fingerprint(data);
  if (!force && fp === _streamState.fp) return;
  _streamState.data = data;
  _streamState.fp = fp;
  renderStreamBody();
}

function renderStreamBody() {
  const body = $('#stream-body');
  if (!body) return;
  const d = _streamState.data;

  // Keep the player iframe across redraws so a 3 s poll doesn't
  // restart the video.
  const oldPlayer = $('#stream-player', body);
  const embed = d.embed_url ? withTwitchParent(d.embed_url) : '';
  body.innerHTML = '';

  if (embed) {
    if (oldPlayer && oldPlayer.dataset.src === embed) {
      body.appendChild(oldPlayer);
    } else {
      const wrap = document.createElement('div');
      wrap.className = 'stream-player';
      wrap.id = 'stream-player';
      wrap.dataset.src = embed;
      const frame = document.createElement('iframe');
      frame.src = embed;
      frame.title = 'Stream preview';
      frame.allow = 'autoplay; fullscreen; picture-in-picture';
      frame.allowFullscreen = true;
      frame.referrerPolicy = 'no-referrer';
      wrap.appendChild(frame);
      body.appendChild(wrap);
    }
  }

  const grid = document.createElement('div');
  grid.className = 'integration-grid';
  grid.appendChild(buildObsCard(d.obs || {}));
  grid.appendChild(buildSubtitleCard(d.subtitles || {}, d.obs || {}));
  grid.appendChild(buildRemoteCard(d));
  if (!embed) grid.appendChild(buildEmbedHintCard());
  body.appendChild(grid);
}

// Twitch's player refuses to load without parent=<embedding host>.
function withTwitchParent(url) {
  try {
    const u = new URL(url);
    if (u.hostname === 'player.twitch.tv' && !u.searchParams.has('parent')) {
      u.searchParams.set('parent', location.hostname);
    }
    return u.toString();
  } catch {
    return '';
  }
}

function streamCard(title, stateClass, stateLabel) {
  const card = document.createElement('div');
  card.className = 'integration-card';
  const header = document.createElement('div');
  header.className = 'integration-header';
  const wrap = document.createElement('div');
  wrap.className = 'integration-title-wrap';
  const name = document.createElement('div');
  name.className = 'integration-name';
  name.textContent = title;
  wrap.appendChild(name);
  if (stateLabel) {
    const pill = document.createElement('span');
    pill.className = `integration-status state-${stateClass}`;
    pill.textContent = stateLabel;
    wrap.appendChild(pill);
  }
  header.appendChild(wrap);
  card.appendChild(header);
  return card;
}

function streamText(card, text, cls = 'integration-desc') {
  const el = document.createElement('div');
  el.className = cls;
  el.textContent = text;
  card.appendChild(el);
  return el;
}

function buildObsCard(obs) {
  if (!obs.connected) {
    const card = streamCard('OBS Studio', 'warn', 'disconnected');
    streamText(card, obs.error || 'Not connected.', 'integration-message');
    streamText(card,
      'Enable Tools → WebSocket Server Settings in OBS, then set the ' +
      'password on the Integrations page.');
    const actions = document.createElement('div');
    actions.className = 'integration-actions';
    const link = document.createElement('a');
    link.className = 'btn btn-sm';
    link.href = '#/integrations';
    link.textContent = 'Open Integrations';
    actions.appendChild(link);
    card.appendChild(actions);
    return card;
  }

  const live = !!obs.streaming;
  const card = streamCard(
    'OBS Studio', live ? 'err' : 'off', live ? `live ${obs.timecode.split('.')[0]}` : 'offline',
  );
  streamText(card, `OBS ${obs.obs_version} · scene: ${obs.current_scene || '—'}`);

  if (obs.scenes && obs.scenes.length) {
    const label = streamText(card, 'Scenes', 'stream-label');
    label.id = 'stream-scenes-label';
    const scenes = document.createElement('div');
    scenes.className = 'stream-scenes';
    scenes.setAttribute('role', 'group');
    scenes.setAttribute('aria-labelledby', 'stream-scenes-label');
    for (const name of obs.scenes) {
      const btn = document.createElement('button');
      const on = name === obs.current_scene;
      btn.className = 'btn btn-sm stream-scene' + (on ? ' active' : '');
      btn.textContent = name;
      btn.setAttribute('aria-pressed', on ? 'true' : 'false');
      btn.addEventListener('click', () => {
        if (!on) streamAction('/api/stream/scene', { scene: name }, btn);
      });
      scenes.appendChild(btn);
    }
    card.appendChild(scenes);
  }

  const actions = document.createElement('div');
  actions.className = 'integration-actions';
  const go = document.createElement('button');
  if (live) {
    go.className = 'btn btn-danger';
    go.textContent = 'Stop stream';
    go.addEventListener('click', () => {
      if (confirm('Stop the stream? Viewers will be disconnected.')) {
        streamAction('/api/stream/stop', {}, go);
      }
    });
  } else {
    go.className = 'btn btn-primary';
    go.textContent = 'Go live';
    go.addEventListener('click', () => {
      if (confirm('Start streaming? OBS will go live with the current scene.')) {
        streamAction('/api/stream/start', {}, go);
      }
    });
  }
  actions.appendChild(go);
  card.appendChild(actions);
  return card;
}

function buildSubtitleCard(subs, obs) {
  const card = streamCard(
    'Subtitles', subs.enabled ? 'ok' : 'off', subs.enabled ? 'on' : 'off',
  );
  if (!subs.enabled) {
    streamText(card, 'Turned off. Set OBS_SUBTITLES_ENABLED = True in constants.py.');
    return card;
  }
  streamText(card,
    'Each spoken line is written here in timed chunks. In OBS, add a ' +
    'Text source and turn on "Read from file":');
  streamText(card, subs.file, 'integration-message');
  streamText(card, obs.subtitle_source
    ? `Also pushed live to the "${obs.subtitle_source}" source.`
    : 'Tip: set "Subtitle text source" on the Integrations page for ' +
      'instant updates instead of OBS\'s file polling.');
  return card;
}

function buildRemoteCard(d) {
  const origins = d.public_origins || [];
  const card = streamCard(
    'Remote access', origins.length ? 'ok' : 'off',
    origins.length ? 'configured' : 'local only',
  );
  if (origins.length) {
    streamText(card, 'Lumi accepts sign-ins from:');
    for (const o of origins) streamText(card, o, 'integration-message');
    if (d.remote) streamText(card, 'You are connected through the tunnel.');
  } else {
    streamText(card,
      'Only reachable from this computer. To reach Lumi from campus or ' +
      'your phone, run this on the host:');
    streamText(card, 'python scripts/setup_tunnel.py', 'integration-message');
  }
  return card;
}

function buildEmbedHintCard() {
  const card = streamCard('Stream preview', 'off', 'not set');
  streamText(card,
    'Set "Stream embed URL" on the OBS Studio integration to watch the ' +
    'avatar here, e.g. https://player.twitch.tv/?channel=yourname or ' +
    'https://www.youtube.com/embed/live_stream?channel=CHANNEL_ID.');
  return card;
}

async function streamAction(path, body, btn) {
  if (_streamState.busy) return;
  _streamState.busy = true;
  const prev = btn.textContent;
  btn.disabled = true;
  btn.textContent = '…';
  try {
    const res = await api(path, { method: 'POST', body, csrf: true });
    applyStreamData({ ..._streamState.data, obs: res.obs }, true);
  } catch (e) {
    alert(e.message);
    btn.disabled = false;
    btn.textContent = prev;
  } finally {
    _streamState.busy = false;
  }
}

/* ==============================================================
   SETTINGS (Phase 10.14)
   ============================================================== */

const _settingsState = {
  activeTab: 'general',
  general:   null,
  about:     null,
  advanced:  null,
  sessions:  null,
};

const SETTINGS_TABS = [
  { id: 'general',  label: 'General' },
  { id: 'security', label: 'Security' },
  { id: 'advanced', label: 'Advanced' },
  { id: 'about',    label: 'About' },
];

async function renderSettings(main) {
  main.innerHTML = `
    <div class="page">
      <div class="page-header">
        <div>
          <div class="page-title">Settings</div>
          <div class="page-sub" id="settings-sub">Configure Lumi.</div>
        </div>
      </div>
      <div class="settings-wrap">
        <aside class="settings-sidebar" id="settings-sidebar"></aside>
        <div class="settings-main">
          <div class="settings-fields" id="settings-fields">
            <div class="integration-empty">Loading…</div>
          </div>
        </div>
      </div>
    </div>
  `;

  renderSettingsSidebar();
  await loadSettingsTab();
}

function renderSettingsSidebar() {
  const side = $('#settings-sidebar');
  if (!side) return;
  side.innerHTML = '';
  for (const t of SETTINGS_TABS) {
    const el = document.createElement('div');
    el.className = 'settings-nav-item' +
      (t.id === _settingsState.activeTab ? ' active' : '');
    el.textContent = t.label;
    el.addEventListener('click', () => {
      _settingsState.activeTab = t.id;
      renderSettingsSidebar();
      loadSettingsTab();
    });
    side.appendChild(el);
  }
}

async function loadSettingsTab() {
  const panel = $('#settings-fields');
  const sub   = $('#settings-sub');
  if (!panel) return;

  panel.innerHTML = '<div class="integration-empty">Loading…</div>';

  const tab = _settingsState.activeTab;
  if (sub) sub.textContent = SETTINGS_TABS
    .find(t => t.id === tab)?.label || 'Settings';

  try {
    if (tab === 'general') {
      const data = await api('/api/settings/general');
      _settingsState.general = data;
      renderGeneralTab(panel, data);
    } else if (tab === 'security') {
      const sessions = await api('/api/settings/security/sessions');
      _settingsState.sessions = sessions.sessions || [];
      renderSecurityTab(panel);
    } else if (tab === 'advanced') {
      const data = await api('/api/settings/advanced');
      _settingsState.advanced = data.targets || [];
      renderAdvancedTab(panel);
    } else if (tab === 'about') {
      const data = await api('/api/settings/about');
      _settingsState.about = data;
      renderAboutTab(panel, data);
    }
  } catch (e) {
    panel.innerHTML = `<div class="integration-empty">
      Failed to load: ${escapeHtml(e.message)}</div>`;
  }
}

function renderGeneralTab(panel, data) {
  panel.innerHTML = '';

  const h = document.createElement('h2');
  h.className = 'settings-section-title';
  h.textContent = 'General';
  panel.appendChild(h);

  const help = document.createElement('div');
  help.className = 'settings-help';
  help.textContent =
    'These values write to constants.py. A .bak is saved before every ' +
    'write and changes take effect on the next turn — no restart required.';
  panel.appendChild(help);

  panel.appendChild(buildSettingsField({
    label: 'Timezone',
    help: 'IANA timezone name, e.g. America/New_York or Europe/London.',
    value: data.timezone,
    key: 'timezone',
  }));

  panel.appendChild(buildSettingsField({
    label: 'Ollama endpoint',
    help: 'Base URL for the local Ollama server. Applies to all ' +
          'Ollama-routed models.',
    value: data.llm_endpoint,
    key: 'llm_endpoint',
  }));

  const info = document.createElement('div');
  info.className = 'settings-info-grid';
  const readonly = [
    ['Workspace root', data.workspace_root],
    ['Main model',     data.model_name],
    ['Fast model',     data.fast_model_name +
      (data.fast_model_enabled ? '' : ' (disabled)')],
    ['Python',         data.python_version],
    ['Platform',       data.platform],
  ];
  for (const [k, v] of readonly) {
    const dt = document.createElement('div');
    dt.className = 'settings-info-key';
    dt.textContent = k;
    const dd = document.createElement('div');
    dd.className = 'settings-info-val';
    dd.textContent = v || '—';
    info.appendChild(dt);
    info.appendChild(dd);
  }
  panel.appendChild(info);

  const actions = document.createElement('div');
  actions.className = 'settings-actions';
  const saveBtn = document.createElement('button');
  saveBtn.className = 'btn btn-primary';
  saveBtn.textContent = 'Save changes';
  saveBtn.addEventListener('click', onSaveGeneral);
  actions.appendChild(saveBtn);
  panel.appendChild(actions);
}

function buildSettingsField({ label, help, value, key, type = 'text' }) {
  const wrap = document.createElement('div');
  wrap.className = 'settings-field';

  const lbl = document.createElement('label');
  lbl.className = 'settings-label';
  lbl.textContent = label;
  wrap.appendChild(lbl);

  const inp = document.createElement('input');
  inp.type = type;
  inp.className = 'settings-input';
  inp.dataset.key = key;
  inp.value = value == null ? '' : String(value);
  wrap.appendChild(inp);

  if (help) {
    const h = document.createElement('div');
    h.className = 'settings-field-help';
    h.textContent = help;
    wrap.appendChild(h);
  }
  return wrap;
}

async function onSaveGeneral() {
  const btn = document.querySelector('.settings-actions .btn-primary');
  const values = {};
  const tzEl = document.querySelector('.settings-input[data-key="timezone"]');
  const epEl = document.querySelector('.settings-input[data-key="llm_endpoint"]');
  if (tzEl) values.timezone = tzEl.value.trim();
  if (epEl) values.llm_endpoint = epEl.value.trim();

  const orig = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = 'Saving…'; }
  try {
    const res = await api('/api/settings/general', {
      method: 'PUT', body: { values }, csrf: true,
    });
    _settingsState.general = res.general;
    flashSettingsSaved('Saved. Live values refreshed.');
    renderSettingsSidebar();
    loadSettingsTab();
  } catch (e) {
    alert('Save failed: ' + e.message);
    if (btn) { btn.disabled = false; btn.textContent = orig; }
  }
}

function renderSecurityTab(panel) {
  panel.innerHTML = '';

  const h = document.createElement('h2');
  h.className = 'settings-section-title';
  h.textContent = 'Security';
  panel.appendChild(h);

  const pwCard = document.createElement('div');
  pwCard.className = 'settings-card';

  const pwTitle = document.createElement('div');
  pwTitle.className = 'settings-card-title';
  pwTitle.textContent = 'Change password';
  pwCard.appendChild(pwTitle);

  const pwHelp = document.createElement('div');
  pwHelp.className = 'settings-help';
  pwHelp.textContent =
    'Your current password is required. Other sessions stay signed in.';
  pwCard.appendChild(pwHelp);

  const oldField = document.createElement('div');
  oldField.className = 'settings-field';
  oldField.innerHTML = '<label class="settings-label">Current password</label>';
  const oldInput = document.createElement('input');
  oldInput.type = 'password';
  oldInput.className = 'settings-input';
  oldInput.id = 'pw-old';
  oldInput.autocomplete = 'current-password';
  oldField.appendChild(oldInput);
  pwCard.appendChild(oldField);

  const newField = document.createElement('div');
  newField.className = 'settings-field';
  newField.innerHTML = '<label class="settings-label">New password (min 8 chars)</label>';
  const newInput = document.createElement('input');
  newInput.type = 'password';
  newInput.className = 'settings-input';
  newInput.id = 'pw-new';
  newInput.autocomplete = 'new-password';
  newField.appendChild(newInput);
  pwCard.appendChild(newField);

  const confirmField = document.createElement('div');
  confirmField.className = 'settings-field';
  confirmField.innerHTML = '<label class="settings-label">Confirm new password</label>';
  const confirmInput = document.createElement('input');
  confirmInput.type = 'password';
  confirmInput.className = 'settings-input';
  confirmInput.id = 'pw-confirm';
  confirmInput.autocomplete = 'new-password';
  confirmField.appendChild(confirmInput);
  pwCard.appendChild(confirmField);

  const pwActions = document.createElement('div');
  pwActions.className = 'settings-actions';
  const pwBtn = document.createElement('button');
  pwBtn.className = 'btn btn-primary';
  pwBtn.textContent = 'Update password';
  pwBtn.addEventListener('click', async () => {
    const oldPw = $('#pw-old').value;
    const newPw = $('#pw-new').value;
    const cnf   = $('#pw-confirm').value;
    if (!oldPw || !newPw) {
      alert('Please fill in all three fields.');
      return;
    }
    if (newPw !== cnf) {
      alert('New passwords do not match.');
      return;
    }
    if (newPw.length < 8) {
      alert('New password must be at least 8 characters.');
      return;
    }
    pwBtn.disabled = true;
    pwBtn.textContent = 'Updating…';
    try {
      const res = await api('/api/settings/security/password', {
        method: 'POST',
        body: { old_password: oldPw, new_password: newPw },
        csrf: true,
      });
      flashSettingsSaved(res.message || 'Password updated.');
      $('#pw-old').value = '';
      $('#pw-new').value = '';
      $('#pw-confirm').value = '';
    } catch (e) {
      alert('Password update failed: ' + e.message);
    } finally {
      pwBtn.disabled = false;
      pwBtn.textContent = 'Update password';
    }
  });
  pwActions.appendChild(pwBtn);
  pwCard.appendChild(pwActions);
  panel.appendChild(pwCard);

  const sessCard = document.createElement('div');
  sessCard.className = 'settings-card';

  const sessTitle = document.createElement('div');
  sessTitle.className = 'settings-card-title';
  sessTitle.textContent = 'Active sessions';
  sessCard.appendChild(sessTitle);

  const sessions = _settingsState.sessions || [];
  if (!sessions.length) {
    const empty = document.createElement('div');
    empty.className = 'settings-help';
    empty.textContent = 'No sessions found.';
    sessCard.appendChild(empty);
  } else {
    const table = document.createElement('div');
    table.className = 'settings-session-table';
    for (const s of sessions) {
      table.appendChild(buildSessionRow(s));
    }
    sessCard.appendChild(table);
  }

  const sessActions = document.createElement('div');
  sessActions.className = 'settings-actions';
  const revokeBtn = document.createElement('button');
  revokeBtn.className = 'btn btn-sm btn-danger';
  revokeBtn.textContent = 'Sign out other sessions';
  revokeBtn.addEventListener('click', async () => {
    if (!confirm('Sign out every session except this one?')) return;
    revokeBtn.disabled = true;
    revokeBtn.textContent = 'Revoking…';
    try {
      const res = await api('/api/settings/security/sessions/revoke-others', {
        method: 'POST', body: {}, csrf: true,
      });
      flashSettingsSaved(res.message || 'Other sessions signed out.');
      loadSettingsTab();
    } catch (e) {
      alert('Failed: ' + e.message);
    } finally {
      revokeBtn.disabled = false;
      revokeBtn.textContent = 'Sign out other sessions';
    }
  });
  sessActions.appendChild(revokeBtn);
  sessCard.appendChild(sessActions);

  panel.appendChild(sessCard);
}

function buildSessionRow(s) {
  const row = document.createElement('div');
  row.className = 'settings-session-row';

  const left = document.createElement('div');
  left.className = 'settings-session-left';

  const idLine = document.createElement('div');
  idLine.className = 'settings-session-id';
  idLine.textContent = s.prefix;
  if (s.is_current) {
    const badge = document.createElement('span');
    badge.className = 'settings-session-badge';
    badge.textContent = 'current';
    idLine.appendChild(badge);
  }
  left.appendChild(idLine);

  const meta = document.createElement('div');
  meta.className = 'settings-session-meta';
  const age = formatDuration(s.age_seconds);
  const exp = formatDuration(s.expires_in_seconds);
  meta.textContent = `Active for ${age} · expires in ${exp}`;
  left.appendChild(meta);

  row.appendChild(left);

  if (!s.is_current) {
    const btn = document.createElement('button');
    btn.className = 'btn btn-sm';
    btn.textContent = 'Revoke';
    btn.addEventListener('click', async () => {
      if (!confirm(`Sign out session ${s.prefix}?`)) return;
      btn.disabled = true;
      try {
        await api('/api/settings/security/sessions/revoke', {
          method: 'POST', body: { prefix: s.prefix }, csrf: true,
        });
        loadSettingsTab();
      } catch (e) {
        alert('Failed: ' + e.message);
        btn.disabled = false;
      }
    });
    row.appendChild(btn);
  }

  return row;
}

function formatDuration(sec) {
  if (sec == null || sec < 0) return '—';
  if (sec < 60) return `${Math.round(sec)}s`;
  if (sec < 3600) return `${Math.round(sec / 60)}m`;
  if (sec < 86400) return `${Math.round(sec / 3600)}h`;
  const days = Math.round(sec / 86400);
  return `${days}d`;
}

function renderAdvancedTab(panel) {
  panel.innerHTML = '';

  const h = document.createElement('h2');
  h.className = 'settings-section-title';
  h.textContent = 'Advanced';
  panel.appendChild(h);

  const warn = document.createElement('div');
  warn.className = 'settings-danger-note';
  warn.innerHTML =
    '<strong>Danger zone.</strong> Actions below are irreversible. ' +
    'Each one wipes data on disk. Nothing here touches constants.py or ' +
    'your .env — only the runtime data files.';
  panel.appendChild(warn);

  for (const t of _settingsState.advanced || []) {
    panel.appendChild(buildResetRow(t));
  }
}

function buildResetRow(target) {
  const row = document.createElement('div');
  row.className = 'settings-reset-row' + (target.dangerous ? ' dangerous' : '');

  const left = document.createElement('div');
  left.className = 'settings-reset-left';

  const name = document.createElement('div');
  name.className = 'settings-reset-name';
  name.textContent = target.id;
  left.appendChild(name);

  const desc = document.createElement('div');
  desc.className = 'settings-reset-desc';
  desc.textContent = target.label;
  left.appendChild(desc);

  row.appendChild(left);

  const btn = document.createElement('button');
  btn.className = 'btn btn-sm ' + (target.dangerous ? 'btn-danger' : '');
  btn.textContent = 'Reset';
  btn.addEventListener('click', async () => {
    const confirmText = target.dangerous
      ? `Permanently wipe ${target.label}? This cannot be undone.`
      : `Reset ${target.label}?`;
    if (!confirm(confirmText)) return;
    btn.disabled = true;
    btn.textContent = 'Working…';
    try {
      const res = await api('/api/settings/advanced/reset', {
        method: 'POST',
        body: { target: target.id },
        csrf: true,
      });
      flashSettingsSaved(res.message || 'Done.');
    } catch (e) {
      alert('Reset failed: ' + e.message);
    } finally {
      btn.disabled = false;
      btn.textContent = 'Reset';
    }
  });
  row.appendChild(btn);

  return row;
}

function renderAboutTab(panel, data) {
  panel.innerHTML = '';

  const h = document.createElement('h2');
  h.className = 'settings-section-title';
  h.textContent = 'About';
  panel.appendChild(h);

  const brand = document.createElement('div');
  brand.className = 'settings-about-brand';
  brand.innerHTML = `
    <div class="settings-about-logo">◆</div>
    <div>
      <div class="settings-about-name">Lumi</div>
      <div class="settings-about-version">v${escapeHtml(String(data.version))}</div>
    </div>
  `;
  panel.appendChild(brand);

  const info = document.createElement('div');
  info.className = 'settings-info-grid';
  const rows = [
    ['Version',         data.version],
    ['Python',          data.python_version],
    ['Platform',        data.platform],
    ['Workspace root',  data.workspace_root],
    ['Ollama endpoint', data.llm_endpoint],
    ['Main model',      data.model_name],
    ['Fast model',      data.fast_model_name +
      (data.fast_model_enabled ? '' : ' (disabled)')],
    ['Uptime',          data.uptime_human],
    ['Log hub port',    String(data.log_hub_port)],
  ];
  for (const [k, v] of rows) {
    const dt = document.createElement('div');
    dt.className = 'settings-info-key';
    dt.textContent = k;
    const dd = document.createElement('div');
    dd.className = 'settings-info-val';
    dd.textContent = v || '—';
    info.appendChild(dt);
    info.appendChild(dd);
  }
  panel.appendChild(info);

  const modTitle = document.createElement('div');
  modTitle.className = 'settings-card-title';
  modTitle.style.marginTop = '24px';
  modTitle.textContent = 'Modules loaded';
  panel.appendChild(modTitle);

  const modGrid = document.createElement('div');
  modGrid.className = 'settings-module-grid';
  for (const m of data.modules || []) {
    const chip = document.createElement('span');
    chip.className = 'settings-module-chip' + (m.enabled ? '' : ' off');
    chip.textContent = m.name + (m.enabled ? '' : ' · off');
    modGrid.appendChild(chip);
  }
  panel.appendChild(modGrid);

  const credits = document.createElement('div');
  credits.className = 'settings-credits';
  credits.innerHTML = `
    <div class="settings-card-title" style="margin-top:24px">Credits</div>
    <p>Architecture inspired by <strong>Neuro-sama</strong> (Vedal987).</p>
    <p>Built on Ollama, ChromaDB, faster-whisper, Fish Audio,
       Playwright, pyvts, aiohttp, python-socketio, and pyautogui.</p>
    <p class="settings-credits-foot">
      Local, experimental AI companion project. All responses are
      generated dynamically by the language model.
    </p>
  `;
  panel.appendChild(credits);
}

function flashSettingsSaved(message) {
  const sub = $('#settings-sub');
  if (!sub) return;
  const prev = sub.textContent;
  sub.textContent = message || 'Saved.';
  sub.classList.add('persona-saved');
  setTimeout(() => {
    sub.textContent = prev;
    sub.classList.remove('persona-saved');
  }, 2000);
}

/* ==============================================================
   UTIL + BOOT
   ============================================================== */
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

initTheme();   // <-- new line — runs before auth so there's no flash

(async () => {
  await checkAuth();
})();
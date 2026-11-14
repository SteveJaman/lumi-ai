// Lumi Island: idle pill, dashboard (Thinking / Calendar / Tasks), and
// approval requests from the HITL server (modules/hitl_server.py), which
// override whatever is showing. No timers run while idle; one reconnect
// timer runs only after the socket closes.
(() => {
  'use strict';

  const WS_URL = 'ws://127.0.0.1:5090/hitl';
  const MAX_BACKOFF = 10000;
  const MORPH_MS = 400;                 // a bit longer than --dur in CSS
  const STAGE_PAD_X = 24, STAGE_PAD_Y = 28;   // .stage padding, for fit()
  const DANGER = /^(run_command|run_in_terminal|write_file|delete_file)$/;

  const $ = (id) => document.getElementById(id);
  const island = $('island');
  const views = { idle: $('pill'), dashboard: $('dashboard'), approval: $('approval') };

  let state = 'idle';
  let returnTo = 'idle';       // where an approval goes back to
  let tab = 'thinking';
  let queue = [];
  let ws = null, retry = 500, reconnectTimer = null;

  // ── Window sizing (only inside launch_island.py) ─────────────
  function api() { return window.pywebview && window.pywebview.api; }
  function fitWindow(grow) {
    const a = api();
    if (!a || !a.fit) return;
    if (grow) { a.fit(460, 420); return; }     // room to animate into
    const r = island.getBoundingClientRect();
    a.fit(Math.ceil(r.width) + STAGE_PAD_X, Math.ceil(r.height) + STAGE_PAD_Y);
  }

  // ── State ────────────────────────────────────────────────────
  function setState(next) {
    if (next === state) return;
    const growing = next !== 'idle';
    if (growing) fitWindow(true);
    state = next;
    island.classList.remove('idle', 'dashboard', 'approval');
    island.classList.add(next);
    for (const [name, el] of Object.entries(views)) el.inert = name !== next;
    // One-shot: shrink the window to the island once the morph ends.
    setTimeout(() => fitWindow(false), MORPH_MS);
    if (next === 'dashboard') loadTab(tab);
  }

  // ── Approval ─────────────────────────────────────────────────
  function renderApproval() {
    const cur = queue[0];
    if (!cur) { setState(returnTo); return; }
    const danger = DANGER.test(cur.tool);
    island.classList.toggle('danger', danger);
    $('eyebrow').textContent = danger ? 'Lumi wants to run · check carefully' : 'Lumi wants to run';
    $('tool').textContent = cur.tool || 'unknown tool';
    $('preview').textContent = cur.preview || '(no details)';
    $('preview').scrollTop = 0;
    $('more').hidden = queue.length < 2;
    $('more').textContent = `+${queue.length - 1}`;
    if (state !== 'approval') { returnTo = state; setState('approval'); }
  }

  function respond(decision) {
    const cur = queue.shift();
    if (cur && ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'approval_response', id: cur.id, decision }));
    }
    renderApproval();
  }

  // ── Dashboard tabs ───────────────────────────────────────────
  function selectTab(name) {
    tab = name;
    document.querySelectorAll('.tab').forEach((b) =>
      b.setAttribute('aria-selected', String(b.dataset.tab === name)));
    for (const t of ['thinking', 'calendar', 'tasks']) $(`panel-${t}`).hidden = t !== name;
    loadTab(name);
  }

  function loadTab(name) {
    if (name === 'calendar') loadList('/api/widgets/calendar', $('calendar'), 'events', 'Calendar');
    if (name === 'tasks') loadList('/api/widgets/assignments', $('tasks'), 'assignments', 'Tasks');
  }

  function message(ul, text) {
    ul.replaceChildren(Object.assign(document.createElement('li'), { className: 'empty', textContent: text }));
  }

  function formatWhen(v) {
    if (!v) return '';
    const d = new Date(v);
    if (isNaN(d)) return String(v);
    return d.toLocaleString([], { weekday: 'short', month: 'short', day: 'numeric',
                                  hour: 'numeric', minute: '2-digit' });
  }

  async function loadList(url, ul, key, label) {
    message(ul, 'Loading…');
    let res;
    try {
      res = await fetch(url, { credentials: 'same-origin', headers: { Accept: 'application/json' } });
    } catch (_) {
      message(ul, 'Lumi isn’t reachable.');
      return;
    }
    if (res.status === 404) { message(ul, `${label} isn’t set up on the server yet.`); return; }
    if (res.status === 401) { message(ul, 'Sign in to Lumi to see this.'); return; }
    if (!res.ok) { message(ul, `Couldn’t load ${label.toLowerCase()} (${res.status}).`); return; }
    let data;
    try { data = await res.json(); } catch (_) { message(ul, 'The server sent something unreadable.'); return; }
    const items = Array.isArray(data) ? data : (data && (data[key] || data.items)) || [];
    if (!items.length) { message(ul, `Nothing in ${label.toLowerCase()}.`); return; }
    ul.replaceChildren(...items.map((it) => {
      const li = document.createElement('li');
      const title = document.createElement('span');
      title.className = 'title';
      title.textContent = it.title || it.summary || it.name || 'Untitled';
      const when = document.createElement('span');
      when.className = 'when';
      when.textContent = formatWhen(it.start || it.due || it.date || it.when);
      li.append(title, when);
      return li;
    }));
  }

  // ── WebSocket ────────────────────────────────────────────────
  function setStatus(s) { island.dataset.status = s; }

  function connect() {
    reconnectTimer = null;
    setStatus('connecting');
    try { ws = new WebSocket(WS_URL); } catch (_) { scheduleReconnect(); return; }

    ws.onopen = () => { retry = 500; setStatus('open'); };

    ws.onmessage = (e) => {
      let msg;
      try { msg = JSON.parse(e.data); } catch (_) { return; }
      if (!msg || typeof msg !== 'object') return;
      if (msg.type === 'approval_request' && msg.id) {
        if (!queue.some((r) => r.id === msg.id)) {   // re-sent on reconnect
          queue.push({ id: String(msg.id), tool: String(msg.tool || ''),
                       preview: String(msg.preview || '') });
          renderApproval();
        }
      } else if (msg.type === 'approval_expired') {
        const n = queue.length;
        queue = queue.filter((r) => r.id !== msg.id);
        if (queue.length !== n) renderApproval();
      } else if (msg.type === 'lumi_thinking') {
        const el = $('thinking');
        el.textContent = String(msg.text || '');
        el.classList.toggle('empty', !msg.text);
        if (!msg.text) el.textContent = 'No thoughts yet.';
      }
    };

    ws.onclose = () => {
      ws = null;
      if (queue.length) { queue = []; renderApproval(); }   // server fell back to the terminal
      setStatus('closed');
      scheduleReconnect();
    };
    ws.onerror = () => { try { ws && ws.close(); } catch (_) {} };
  }

  function scheduleReconnect() {
    if (reconnectTimer) return;
    reconnectTimer = setTimeout(connect, retry);
    retry = Math.min(retry * 2, MAX_BACKOFF);
  }

  // ── Wiring ───────────────────────────────────────────────────
  $('pill').addEventListener('click', () => setState('dashboard'));
  $('close').addEventListener('click', () => setState('idle'));
  $('approve').addEventListener('click', () => respond('approve'));
  $('decline').addEventListener('click', () => respond('decline'));
  document.querySelectorAll('.tab').forEach((b) =>
    b.addEventListener('click', () => selectTab(b.dataset.tab)));
  window.addEventListener('pywebviewready', () => fitWindow(false));

  connect();
})();

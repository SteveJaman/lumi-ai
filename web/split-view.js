/* ============================================================
   Split-view workspace — the right-hand pane.
     Browser   live JPEG frames of Lumi's read-only Playwright browser
               (socket: browser_frame, browser_status). Never an iframe.
     Terminal  output of commands Lumi runs (socket: terminal_output)
     Files     read-only tree of ./data/ (GET /api/workspace/files,
               /api/workspace/files/content?path=)
     Models    the Model Pipeline: which Claude tier is working and why
               (claude_model_switch, claude_token_stream), an !auto task's
               Plan -> Execute -> Verify progress (autonomous_step), and the
               catch-up snapshot sent on join (model_pipeline_state)
   app.js calls init({ api, state, socket }) once the user is signed in.
   Host-only: the server ignores workspace_join from remote tabs and the
   file routes answer 403, so the toggle is only shown on the host.
   Everything from the server is written with textContent.
   ============================================================ */

const OPEN_KEY = 'lumi-split-open';
const TAB_KEY = 'lumi-split-tab';
const MAX_TERMINAL_CHARS = 200_000;

let C = null;           // { api, state, socket }
let sock = null;
let joined = false;
let open = false;
let tab = 'browser';
let filesLoaded = false;
let fileSeq = 0;
let selectedRow = null;

const TABS = ['browser', 'terminal', 'files', 'models'];
const PHASES = ['plan', 'execute', 'verify'];
const MAX_LOG = 60;

const $ = (id) => document.getElementById(id);

export function init(ctx) {
  if (C) return;
  C = ctx;
  if (!C.state.isLocal) return;              // host-only feature

  const toggle = $('split-toggle');
  const pane = $('split-pane');
  if (!toggle || !pane) return;
  toggle.hidden = false;

  toggle.addEventListener('click', () => setOpen(!open));
  $('split-close').addEventListener('click', () => { setOpen(false); toggle.focus(); });
  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === '\\') { e.preventDefault(); setOpen(!open); }
    else if (e.key === 'Escape' && open && pane.contains(document.activeElement)) { setOpen(false); toggle.focus(); }
  });

  const tabs = [...pane.querySelectorAll('.split-tab')];
  tabs.forEach((b) => b.addEventListener('click', () => selectTab(b.dataset.tab)));
  pane.querySelector('.split-tabs').addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
    const i = tabs.findIndex((b) => b.dataset.tab === tab);
    const next = tabs[(i + (e.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length];
    selectTab(next.dataset.tab);
    next.focus();
  });

  $('split-terminal-clear').addEventListener('click', () => { $('split-terminal').textContent = ''; });
  $('split-files-refresh').addEventListener('click', () => loadFiles());

  wireSocket();

  let savedTab = 'browser';
  try { savedTab = localStorage.getItem(TAB_KEY) || 'browser'; } catch { /* private mode */ }
  selectTab(savedTab);
  let savedOpen = false;
  try { savedOpen = localStorage.getItem(OPEN_KEY) === '1'; } catch { /* private mode */ }
  if (savedOpen) setOpen(true);
}

/* ---------- open / close / tabs ---------- */

function setOpen(on) {
  open = !!on;
  const pane = $('split-pane');
  pane.hidden = !open;
  document.getElementById('app-shell').classList.toggle('split-open', open);
  $('split-toggle').setAttribute('aria-expanded', String(open));
  $('split-toggle').title = open ? 'Hide workspace (Ctrl+\\)' : 'Show workspace (Ctrl+\\)';
  try { localStorage.setItem(OPEN_KEY, open ? '1' : '0'); } catch { /* private mode */ }
  if (open) {
    $('split-toggle-dot').hidden = true;
    join();
    if (tab === 'files' && !filesLoaded) loadFiles();
  } else {
    leave();
  }
}

function selectTab(name) {
  if (!TABS.includes(name)) name = 'browser';
  tab = name;
  document.querySelectorAll('#split-pane .split-tab').forEach((b) => {
    const on = b.dataset.tab === name;
    b.classList.toggle('on', on);
    b.setAttribute('aria-selected', String(on));
    b.tabIndex = on ? 0 : -1;
  });
  for (const t of TABS) $(`split-panel-${t}`).hidden = t !== name;
  if (name === 'models') $('split-toggle-dot').hidden = true;
  try { localStorage.setItem(TAB_KEY, name); } catch { /* private mode */ }
  if (name === 'files' && open && !filesLoaded) loadFiles();
  if (name === 'terminal') scrollTerminal(true);
}

/* ---------- socket ---------- */

function join() {
  if (sock && sock.connected && !joined) { sock.emit('workspace_join'); joined = true; }
}
function leave() {
  if (sock && joined) { try { sock.emit('workspace_leave'); } catch { /* closing */ } }
  joined = false;
}

function wireSocket() {
  sock = C.socket && C.socket();
  if (!sock) return;
  sock.on('connect', () => { joined = false; if (open) join(); });
  sock.on('disconnect', () => { joined = false; });
  sock.on('browser_frame', onFrame);
  sock.on('browser_status', onStatus);
  sock.on('terminal_output', onTerminal);
  sock.on('claude_model_switch', onModelSwitch);
  sock.on('claude_token_stream', onTokens);
  sock.on('autonomous_step', (m) => onStep(m, false));
  sock.on('model_pipeline_state', onPipelineState);
  if (open) join();
}

/* ---------- browser tab ---------- */

let lastFrameUrl = null;
function onFrame(msg) {
  if (!msg || typeof msg.data !== 'string' || !/^[A-Za-z0-9+/=]+$/.test(msg.data.slice(0, 64))) return;
  const img = $('split-browser-img');
  img.src = `data:image/jpeg;base64,${msg.data}`;
  img.hidden = false;
  $('split-browser-empty').hidden = true;
  const url = String(msg.url || '');
  if (url !== lastFrameUrl) { $('split-browser-url').textContent = url || '—'; lastFrameUrl = url; }
}

function onStatus(msg) {
  if (!msg) return;
  $('split-browser-status').textContent = String(msg.message || '');
  if (typeof msg.active === 'boolean') {
    $('split-browser-live').classList.toggle('on', msg.active);
    if (msg.active && !open) $('split-toggle-dot').hidden = false;
  }
}

/* ---------- terminal tab ---------- */

function scrollTerminal(force) {
  const pre = $('split-terminal');
  pre.scrollTop = pre.scrollHeight;
  if (force) requestAnimationFrame(() => { pre.scrollTop = pre.scrollHeight; });
}

function onTerminal(msg) {
  if (!msg || !msg.text) return;
  const pre = $('split-terminal');
  // Follow the output only if the user hasn't scrolled up to read.
  const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
  const span = document.createElement('span');
  const stream = msg.stream === 'stderr' ? 'stderr' : msg.stream === 'info' ? 'info' : 'stdout';
  span.className = `t-${stream}`;
  span.textContent = String(msg.text);
  pre.appendChild(span);
  while (pre.textContent.length > MAX_TERMINAL_CHARS && pre.firstChild) pre.removeChild(pre.firstChild);
  if (atBottom) scrollTerminal(false);
  if (!open && stream === 'info') $('split-toggle-dot').hidden = false;
}

/* ---------- files tab ---------- */

function fmtSize(n) {
  if (!Number.isFinite(n)) return '';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

async function loadFiles() {
  const tree = $('split-tree');
  tree.textContent = '';
  const li = document.createElement('li');
  li.className = 'split-empty';
  li.textContent = 'Loading…';
  tree.appendChild(li);
  try {
    const data = await C.api('/api/workspace/files');
    tree.textContent = '';
    const kids = (data && data.children) || [];
    if (!kids.length) {
      li.textContent = 'data/ is empty.';
      tree.appendChild(li);
    } else {
      renderNodes(tree, kids, 1);
    }
    if (data && data.truncated) {
      const more = document.createElement('li');
      more.className = 'split-empty';
      more.textContent = 'Too many files; the list was cut short.';
      tree.appendChild(more);
    }
    filesLoaded = true;
  } catch (e) {
    li.textContent = `Couldn't list files: ${e.message || e}`;
  }
}

function renderNodes(ul, nodes, level) {
  for (const node of nodes) {
    const li = document.createElement('li');
    li.setAttribute('role', 'treeitem');
    li.setAttribute('aria-level', String(level));
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'split-tree__row';
    const icon = document.createElement('span');
    icon.className = 'split-tree__icon';
    icon.setAttribute('aria-hidden', 'true');
    const name = document.createElement('span');
    name.textContent = String(node.name);
    row.append(icon, name);
    li.appendChild(row);

    if (node.type === 'dir') {
      const sub = document.createElement('ul');
      sub.setAttribute('role', 'group');
      sub.hidden = true;
      icon.textContent = '▸';
      li.setAttribute('aria-expanded', 'false');
      let built = false;
      row.addEventListener('click', () => {
        if (!built) { renderNodes(sub, node.children || [], level + 1); built = true; }
        sub.hidden = !sub.hidden;
        icon.textContent = sub.hidden ? '▸' : '▾';
        li.setAttribute('aria-expanded', String(!sub.hidden));
      });
      li.appendChild(sub);
    } else {
      icon.textContent = '·';
      const size = document.createElement('span');
      size.className = 'split-tree__size';
      size.textContent = fmtSize(node.size);
      row.appendChild(size);
      row.addEventListener('click', () => openFile(node.path, row));
    }
    ul.appendChild(li);
  }
}

async function openFile(path, row) {
  if (selectedRow) selectedRow.classList.remove('on');
  selectedRow = row;
  row.classList.add('on');
  const seq = ++fileSeq;
  const code = $('split-file-content').firstElementChild;
  $('split-file-name').textContent = `data/${path}`;
  code.textContent = 'Loading…';
  try {
    const data = await C.api(`/api/workspace/files/content?path=${encodeURIComponent(path)}`);
    if (seq !== fileSeq) return;                // a newer click won
    code.textContent = String(data.content || '');
    if (data.truncated) code.textContent += `\n\n[showing the first 512 KB of ${fmtSize(data.size)}]`;
    $('split-file-content').scrollTop = 0;
  } catch (e) {
    if (seq !== fileSeq) return;
    code.textContent = `Couldn't open this file: ${e.message || e}`;
  }
}

/* ---------- models tab (Model Pipeline) ---------- */

let liveTimer = 0;
const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : '');

function fmtUsd(n) {
  const v = Number(n) || 0;
  if (v === 0) return '$0.00';
  return v < 1 ? `$${v.toFixed(4)}` : `$${v.toFixed(2)}`;
}

function setLive(state, text) {
  const el = $('mp-live');
  el.dataset.state = state;
  el.textContent = text;
}

function pulseLive(text) {
  setLive('busy', text);
  clearTimeout(liveTimer);
  liveTimer = setTimeout(() => setLive('idle', 'idle'), 6000);
}

function logLine(kind, text, ts) {
  const ol = $('mp-log');
  const li = document.createElement('li');
  li.className = `mp-log__row mp-log__row--${kind}`;
  const t = document.createElement('time');
  const d = ts ? new Date(ts * 1000) : new Date();
  t.textContent = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  const span = document.createElement('span');
  span.textContent = text;
  li.append(t, span);
  ol.prepend(li);
  while (ol.children.length > MAX_LOG) ol.removeChild(ol.lastElementChild);
}

function hint() {
  // A quiet dot on the toggle when something happens out of view.
  if (!open || tab !== 'models') $('split-toggle-dot').hidden = false;
}

function showTier(msg) {
  document.querySelectorAll('#split-panel-models .mp-tier').forEach((li) => {
    const on = li.dataset.tier === msg.model;
    li.classList.toggle('on', on);
    if (on) li.setAttribute('aria-current', 'true'); else li.removeAttribute('aria-current');
  });
  const id = msg.model_id && $(`mp-id-${msg.model}`);
  if (id) id.textContent = msg.model_id;
  $('mp-reason').textContent = `${cap(msg.model)} · ${msg.reason || ''}`;
}

function onModelSwitch(msg) {
  if (!msg || !msg.model) return;
  showTier(msg);
  pulseLive(`${msg.model} working`);
  const esc = /escalation/i.test(msg.reason || '');
  logLine(esc ? 'warn' : 'switch',
    `${esc ? 'Escalated to' : 'Routed to'} ${cap(msg.model)} (${msg.tier_cost} cost): ${msg.reason || ''}`, msg.ts);
  hint();
}

function showTotals(totals) {
  if (!totals) return;
  $('mp-tokens').textContent = (Number(totals.tokens) || 0).toLocaleString();
  $('mp-cost').textContent = fmtUsd(totals.cost_usd);
  $('mp-saved').textContent = fmtUsd(totals.saved_usd);
  const calls = totals.calls_by_tier || {};
  for (const t of ['haiku', 'sonnet', 'opus']) {
    if (calls[t] != null) $(`mp-calls-${t}`).textContent = Number(calls[t]).toLocaleString();
  }
}

function onTokens(msg) {
  if (!msg || !msg.model) return;
  showTotals(msg.totals);
  logLine('tokens', `${cap(msg.model)} ${msg.classifier ? 'classifier' : 'reply'}: `
    + `${(Number(msg.tokens) || 0).toLocaleString()} tokens (${msg.input_tokens} in / ${msg.output_tokens} out)`, msg.ts);
}

function resetFlow() {
  for (const p of PHASES) $(`mp-state-${p}`).textContent = '—';
  document.querySelectorAll('#split-panel-models .mp-node').forEach((li) => { li.dataset.state = 'todo'; });
  setBar(0, 'run');
}

function setBar(pct, state) {
  $('mp-bar-fill').style.width = `${pct}%`;
  $('mp-bar').setAttribute('aria-valuenow', String(pct));
  $('mp-bar').dataset.state = state;
}

function setPhase(msg) {
  const bad = ['failed', 'declined'].includes(msg.status);
  const nodes = [...document.querySelectorAll('#split-panel-models .mp-node')];
  if (msg.phase === 'done') {
    const ok = msg.status === 'success';
    nodes.forEach((li) => { li.dataset.state = 'done'; });
    if (!ok) nodes[nodes.length - 1].dataset.state = 'failed';
    $('mp-state-execute').textContent = 'done';
    $('mp-state-verify').textContent = ok ? 'verified' : 'unverified';
    setBar(100, ok ? 'done' : 'failed');
    return;
  }
  const at = PHASES.indexOf(msg.phase);
  if (at < 0) return;
  nodes.forEach((li, i) => {
    if (i < at) li.dataset.state = 'done';
    else if (i > at) li.dataset.state = 'todo';
    else if (bad) li.dataset.state = 'failed';
    else li.dataset.state = msg.status === 'success' && msg.phase !== 'execute' ? 'done' : 'active';
  });
  $(`mp-state-${msg.phase}`).textContent = msg.tool ? `${msg.tool} · ${msg.status}` : (msg.step || msg.status);
  const pct = Math.round(((at + (msg.status === 'success' && msg.phase !== 'execute' ? 1 : 0.5)) / PHASES.length) * 100);
  setBar(pct, bad ? 'failed' : 'run');
}

function onStep(msg, replay) {
  if (!msg || !msg.phase) return;
  if (msg.phase === 'plan' && msg.step === 'Planning') resetFlow();
  if (msg.session_id) $('mp-session').textContent = msg.session_id;
  setPhase(msg);
  if (!replay) {
    if (msg.phase === 'done') setLive(msg.status === 'success' ? 'ok' : 'bad', msg.status === 'success' ? 'verified' : 'unverified');
    else pulseLive(msg.step || msg.phase);
    hint();
  }
  const kind = ['failed', 'declined', 'denied'].includes(msg.status) ? 'bad' : msg.status === 'success' ? 'ok' : 'step';
  logLine(kind, `${msg.step}${msg.tool ? ` · ${msg.tool}` : ''} — ${msg.status}`, msg.ts);
}

function onPipelineState(msg) {
  if (!msg) return;
  if (msg.switch) showTier(msg.switch);
  if (msg.tokens) showTotals(msg.tokens.totals);
  const steps = Array.isArray(msg.steps) ? msg.steps : [];
  if (steps.length) {
    $('mp-log').textContent = '';
    resetFlow();
    for (const st of steps) onStep(st, true);
  }
}

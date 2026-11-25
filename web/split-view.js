/* ============================================================
   Split-view workspace — the right-hand pane.
     Browser   live JPEG frames of Lumi's read-only Playwright browser
               (socket: browser_frame, browser_status). Never an iframe.
     Terminal  output of commands Lumi runs (socket: terminal_output)
     Files     read-only tree of ./data/ (GET /api/workspace/files,
               /api/workspace/files/content?path=)
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
  if (!['browser', 'terminal', 'files'].includes(name)) name = 'browser';
  tab = name;
  document.querySelectorAll('#split-pane .split-tab').forEach((b) => {
    const on = b.dataset.tab === name;
    b.classList.toggle('on', on);
    b.setAttribute('aria-selected', String(on));
    b.tabIndex = on ? 0 : -1;
  });
  for (const t of ['browser', 'terminal', 'files']) $(`split-panel-${t}`).hidden = t !== name;
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

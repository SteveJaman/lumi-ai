/* ============================================================
   Lumi's notices panel (Phase 4-5): her inner thoughts, the curiosity
   notes she has kept, and scene changes from the foreground watcher.
   A small button opens a panel; nothing here is spoken or sent back.
   Events: inner_thought {text}, curiosity_note {count}, scene_preset {preset}.
   ============================================================ */

const MAX_ITEMS = 20;
const items = [];          // newest first: {kind, text, at}
let panel = null, button = null, open = false;

function label(kind) {
  return { thought: 'Inner thought', curiosity: 'Curiosity', scene: 'Scene' }[kind] || kind;
}

function render() {
  if (!panel) return;
  const list = panel.querySelector('.notices-list');
  list.replaceChildren();
  if (!items.length) {
    const empty = document.createElement('p');
    empty.className = 'notices-empty';
    empty.textContent = 'Nothing yet. Her private thoughts and notes show up here.';
    list.append(empty);
    return;
  }
  for (const it of items) {
    const row = document.createElement('div');
    row.className = `notice notice-${it.kind}`;
    const k = document.createElement('div');
    k.className = 'notice-kind';
    k.textContent = label(it.kind);
    const t = document.createElement('div');
    t.className = 'notice-text';
    t.textContent = it.text;
    row.append(k, t);
    list.append(row);
  }
}

function push(kind, text) {
  items.unshift({ kind, text, at: Date.now() });
  items.length = Math.min(items.length, MAX_ITEMS);
  if (button) button.dataset.count = String(items.length);
  render();
}

function setOpen(on) {
  open = on;
  if (panel) panel.hidden = !on;
  if (button) button.setAttribute('aria-expanded', String(on));
}

export function init({ socket } = {}) {
  if (panel) return;
  panel = document.createElement('section');
  panel.className = 'notices';
  panel.hidden = true;
  panel.setAttribute('aria-label', 'Lumi’s notices');
  const head = document.createElement('div');
  head.className = 'notices-head';
  const title = document.createElement('span');
  title.textContent = 'Lumi’s notices';
  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'btn btn-ghost btn-sm';
  close.textContent = 'Close';
  close.addEventListener('click', () => setOpen(false));
  head.append(title, close);
  const list = document.createElement('div');
  list.className = 'notices-list';
  panel.append(head, list);
  document.body.append(panel);

  button = document.createElement('button');
  button.type = 'button';
  button.className = 'notices-toggle';
  button.textContent = '💭';
  button.title = 'Lumi’s notices';
  button.setAttribute('aria-label', 'Show Lumi’s notices');
  button.setAttribute('aria-expanded', 'false');
  button.addEventListener('click', () => setOpen(!open));
  document.body.append(button);

  render();
  if (socket) {
    socket.on('inner_thought', (p) => { if (p && p.text) push('thought', p.text); });
    socket.on('curiosity_note', (p) => {
      const n = p && p.count ? p.count : 0;
      if (n) push('curiosity', `Read up on something while you were away: ${n} note${n === 1 ? '' : 's'} kept.`);
    });
    socket.on('scene_preset', (p) => { if (p && p.preset) push('scene', `Scene: ${p.preset}`); });
  }
}

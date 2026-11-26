/* ============================================================
   Mini Spotify desktop in the web app (host only).
   Backend: modules/media_bridge.py (/api/media/*) -> modules/spotify.py.

     init(ctx)    builds the sidebar player and the library view
     refresh()    re-reads the player now (the Settings card calls it)

   Player (sidebar, above you): art, title, heart; shuffle, previous, play,
   next, repeat; a seek bar; volume and a button that opens the library.
   Library view: Liked Songs, your playlists and albums on the left,
   the chosen list's songs on the right; a row plays from that song.

   Free accounts: Spotify only allows remote control on Premium. The
   bridge then uses the Spotify app's own Windows media controls, which
   cover play/pause/skip/seek/shuffle/repeat; play-a-row and the queue
   can't work, so they're hidden (can.play_track is false).

   Polls every 5 s while playing (15 s idle), not while the tab is hidden.
   Album art comes from Spotify's https CDN; all text is textContent.
   ============================================================ */

let C = null;
let now = null;                 // last /api/media/state answer
let show = false;
let timer = 0, tick = 0, volTimer = 0;
let busy = false, seeking = false, volBusyUntil = 0;
const D = {};                   // the bar's elements
const L = {                     // the library view
  open: false, shelf: 'playlists', items: [], next: 0, loading: false, seq: 0,
  sel: null, rows: [], tnext: null, ttotal: 0, tloading: false, tseq: 0,
};

const ART_OK = /^(data:image\/(png|jpeg);base64,|https:\/\/)/;
const LIKED = { kind: 'liked', id: '', uri: '', title: 'Liked Songs', subtitle: 'Your saved songs', browsable: true };  // no context address

const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};
const svg = (paths) => `<svg viewBox="0 0 24 24" aria-hidden="true">${paths}</svg>`;
const S = 'fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"';
const ICON = {
  prev: svg('<path d="M6 5h2v14H6zM20 5v14L9 12z" fill="currentColor"/>'),
  next: svg('<path d="M16 5h2v14h-2zM4 5v14l11-7z" fill="currentColor"/>'),
  play: svg('<path d="M7 4v16l13-8z" fill="currentColor"/>'),
  pause: svg('<path d="M6 4h4v16H6zM14 4h4v16h-4z" fill="currentColor"/>'),
  shuffle: svg(`<path ${S} d="M3 7h3c4 0 6 10 10 10h3M3 17h3c1.6 0 2.8-1.6 3.9-3.6M13.1 9.6C14.2 8.1 15 7 16 7h3M17 4l3 3-3 3M17 14l3 3-3 3"/>`),
  repeat: svg(`<path ${S} d="M4 11V9a3 3 0 0 1 3-3h12M16 3l3 3-3 3M20 13v2a3 3 0 0 1-3 3H5M8 21l-3-3 3-3"/>`),
  heart: svg('<path class="h" d="M12 20.3l-1.3-1.2C5.6 14.5 2.5 11.7 2.5 8.3 2.5 5.6 4.6 3.5 7.3 3.5c1.5 0 3 .7 4 1.8.9-1.1 2.4-1.8 4-1.8 2.7 0 4.8 2.1 4.8 4.8 0 3.4-3.1 6.2-8.2 10.8L12 20.3z"/>'),
  vol: svg(`<path d="M4 9v6h4l5 4V5L8 9z" fill="currentColor"/><path ${S} d="M16 8.5a5 5 0 0 1 0 7M18.5 6a8.5 8.5 0 0 1 0 12"/>`),
  mute: svg(`<path d="M4 9v6h4l5 4V5L8 9z" fill="currentColor"/><path ${S} d="M16.5 9.5l5 5M21.5 9.5l-5 5"/>`),
  library: svg(`<path ${S} d="M5 4v16M10 4v16M14.5 4.5l5 15"/>`),
  down: svg(`<path ${S} d="M6 9l6 6 6-6"/>`),
};

function btn(cls, label, icon, onClick) {
  const b = el('button', cls);
  b.type = 'button';
  b.innerHTML = icon;
  b.setAttribute('aria-label', label);
  b.title = label;
  b.addEventListener('click', onClick);
  return b;
}

export const fmt = (ms) => {
  const s = Math.max(0, Math.floor((ms || 0) / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
};

function artBox(cls, url) {
  const box = el('div', cls);
  if (ART_OK.test(url || '')) {
    const img = el('img');
    img.src = url; img.alt = ''; img.referrerPolicy = 'no-referrer'; img.loading = 'lazy';
    box.append(img);
  }
  return box;
}

/* ---------- life cycle ---------- */

export function init(ctx) {
  C = ctx;
  if (!C.state.isLocal) return;                       // the routes are host-only
  build();
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); else stop(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && L.open) setOpen(false); });
  refresh();
}

function stop() { clearTimeout(timer); timer = 0; clearInterval(tick); tick = 0; }

export async function refresh() {
  stop();
  if (document.hidden || !D.dock) return;
  try {
    now = await C.api('/api/media/state');
    show = !!(now.spotify && now.spotify.connected);
    paint();
  } catch (e) {
    if (e.status === 401 || e.status === 403) { show = false; paint(); return; }
  }
  timer = setTimeout(refresh, now && now.playing ? 5000 : 15000);
  if (now && now.playing) tick = setInterval(paintProgress, 1000);
}

async function act(action, value) {
  if (busy) return null;
  busy = true;
  say('');
  try {
    const r = await C.api('/api/media/command', { method: 'POST', csrf: true, body: value === undefined ? { action } : { action, value } });
    if (r && r.state) { now = r.state; paint(); }
    setTimeout(refresh, 800);
    return r;
  } catch (e) {
    say(e.message);
    return null;
  } finally { busy = false; }
}

function say(msg) { if (D.err) D.err.textContent = msg; }

/* ---------- the bottom bar ---------- */

function build() {
  const dock = document.getElementById('sp-dock');
  const full = document.getElementById('sp-full');
  if (!dock || !full) return;
  D.dock = dock; D.full = full;

  D.art = artBox('spd-art', '');
  D.title = el('div', 'spd-title');
  D.artist = el('div', 'spd-artist');
  const text = el('div', 'spd-text');
  text.append(D.title, D.artist);
  D.heart = btn('spd-icon spd-heart', 'Save to Liked Songs', ICON.heart, () => act('like'));
  const left = el('div', 'spd-now');
  left.append(D.art, text, D.heart);

  D.shuffle = btn('spd-icon spd-toggle', 'Shuffle', ICON.shuffle, () => act('shuffle', !(now && now.shuffle)));
  D.prev = btn('spd-icon', 'Previous', ICON.prev, () => act('previous'));
  D.play = btn('spd-play', 'Play', ICON.play, () => act('toggle'));
  D.next = btn('spd-icon', 'Next', ICON.next, () => act('next'));
  D.repeat = btn('spd-icon spd-toggle', 'Repeat', ICON.repeat, () => {
    const order = ['off', 'context', 'track'];
    act('repeat', order[(order.indexOf((now && now.repeat) || 'off') + 1) % 3]);
  });
  const ctl = el('div', 'spd-ctl');
  ctl.append(D.shuffle, D.prev, D.play, D.next, D.repeat);

  D.cur = el('span', 'spd-time', '0:00');
  D.dur = el('span', 'spd-time', '0:00');
  D.seek = el('input', 'spd-range spd-seek');
  Object.assign(D.seek, { type: 'range', min: 0, max: 1000, step: 1000, value: 0 });
  D.seek.setAttribute('aria-label', 'Song position');
  D.seek.addEventListener('input', () => { seeking = true; D.cur.textContent = fmt(+D.seek.value); fill(D.seek); });
  D.seek.addEventListener('change', async () => {
    const ms = +D.seek.value;
    if (now) { now.progress_ms = ms; now.at = Date.now() / 1000; }
    await act('seek', ms);
    seeking = false;
  });
  const prog = el('div', 'spd-prog');
  prog.append(D.cur, D.seek, D.dur);
  const mid = el('div', 'spd-mid');
  mid.append(ctl, prog);

  D.volIcon = btn('spd-icon', 'Mute', ICON.vol, () => {
    const v = now && now.volume;
    if (v == null) return;
    if (v > 0) { D.lastVol = v; setVolume(0); } else setVolume(D.lastVol || 50);
  });
  D.vol = el('input', 'spd-range spd-vol');
  Object.assign(D.vol, { type: 'range', min: 0, max: 100, step: 1, value: 50 });
  D.vol.setAttribute('aria-label', 'Spotify volume');
  D.vol.addEventListener('input', () => { fill(D.vol); clearTimeout(volTimer); volTimer = setTimeout(() => setVolume(+D.vol.value), 200); });
  D.lib = btn('spd-icon spd-libbtn', 'Open your library', ICON.library, () => setOpen(!L.open));
  const right = el('div', 'spd-right');
  right.append(D.volIcon, D.vol, D.lib);

  D.err = el('div', 'spd-err');
  D.err.setAttribute('role', 'status');
  dock.replaceChildren(left, mid, right, D.err);
  buildLibrary();
}

async function setVolume(v) {
  volBusyUntil = Date.now() + 1500;                   // don't let a poll snap the knob back mid-drag
  D.vol.value = v; fill(D.vol);
  await act('volume', Math.round(v));
}

function fill(range) {
  const max = +range.max || 1;
  range.style.setProperty('--p', `${Math.min(100, (+range.value / max) * 100)}%`);
}

function paint() {
  if (!D.dock) return;
  D.dock.hidden = !show;
  if (!show) { setOpen(false); return; }
  const has = !!(now && now.source);
  const can = (now && now.can) || {};
  const art = artBox('spd-art', has ? now.art : '');
  D.art.replaceWith(art); D.art = art;
  D.title.textContent = has ? (now.title || 'Unknown') : 'Nothing playing';
  D.title.title = D.title.textContent;
  D.title.classList.toggle('idle', !has);
  D.artist.textContent = has ? (now.artists || now.album || '') : 'Pick something from your library';

  D.heart.hidden = !can.like;
  D.heart.setAttribute('aria-pressed', String(!!now.liked));
  D.heart.title = now.liked ? 'Remove from Liked Songs' : 'Save to Liked Songs';
  D.heart.setAttribute('aria-label', D.heart.title);

  const playing = !!(now && now.playing);
  D.play.innerHTML = playing ? ICON.pause : ICON.play;
  D.play.setAttribute('aria-label', playing ? 'Pause' : 'Play'); D.play.title = playing ? 'Pause' : 'Play';
  D.play.disabled = !can.control;
  D.prev.disabled = !(can.prev && can.control);
  D.next.disabled = !(can.next && can.control);
  D.shuffle.hidden = !can.shuffle;
  D.shuffle.setAttribute('aria-pressed', String(!!now.shuffle));
  D.repeat.hidden = !can.repeat;
  const rep = now.repeat || 'off';
  D.repeat.dataset.mode = rep;
  D.repeat.setAttribute('aria-pressed', String(rep !== 'off'));
  D.repeat.title = { off: 'Repeat: off', context: 'Repeat: all', track: 'Repeat: this song' }[rep] || 'Repeat';
  D.repeat.setAttribute('aria-label', D.repeat.title);

  D.seek.disabled = !(can.seek && has && now.duration_ms);
  D.seek.max = Math.max(1000, now.duration_ms || 0);
  D.dur.textContent = fmt(now.duration_ms);

  const volOk = !!can.volume && now.volume != null;
  D.vol.hidden = D.volIcon.hidden = !volOk;
  if (volOk && Date.now() > volBusyUntil) { D.vol.value = now.volume; fill(D.vol); }
  D.volIcon.innerHTML = volOk && now.volume === 0 ? ICON.mute : ICON.vol;
  paintProgress();
  if (L.open) markPlaying();
}

function paintProgress() {
  if (!D.seek || seeking) return;
  if (!now || !now.duration_ms) { D.seek.value = 0; D.cur.textContent = '0:00'; fill(D.seek); return; }
  const elapsed = Math.min(now.duration_ms,
    now.progress_ms + (now.playing ? (Date.now() / 1000 - now.at) * 1000 : 0));
  D.seek.value = elapsed;
  D.cur.textContent = fmt(elapsed);
  fill(D.seek);
}

/* ---------- the library view ---------- */

function buildLibrary() {
  const side = el('nav', 'spx-side');
  side.setAttribute('aria-label', 'Your library');
  const tabs = el('div', 'spx-shelves');
  tabs.setAttribute('role', 'group');
  tabs.setAttribute('aria-label', 'Shelf');
  L.tabs = [['playlists', 'Playlists'], ['albums', 'Albums']].map(([shelf, label]) => {
    const b = el('button', 'spx-shelf', label);
    b.type = 'button'; b.dataset.shelf = shelf;
    b.addEventListener('click', () => loadShelf(shelf));
    tabs.append(b);
    return b;
  });
  L.list = el('ul', 'spx-list');
  L.more = el('button', 'spx-more', 'Load more');
  L.more.type = 'button';
  L.more.addEventListener('click', () => loadShelfPage());
  side.append(tabs, L.list, L.more);

  const main = el('div', 'spx-main');
  L.head = el('header', 'spx-head');
  L.note = el('div', 'spx-note');
  L.note.setAttribute('role', 'status');
  L.table = el('div', 'spx-tracks');
  L.table.setAttribute('role', 'list');
  L.tmore = el('button', 'spx-more', 'Load more songs');
  L.tmore.type = 'button';
  L.tmore.addEventListener('click', () => loadTracks());
  main.append(L.head, L.note, L.table, L.tmore);
  main.addEventListener('scroll', () => {          // keep going as you near the bottom
    if (!L.tmore.hidden && main.scrollTop + main.clientHeight > main.scrollHeight - 400) loadTracks();
  });
  L.list.addEventListener('scroll', () => {
    if (!L.more.hidden && L.list.scrollTop + L.list.clientHeight > L.list.scrollHeight - 200) loadShelfPage();
  });

  const close = btn('spd-icon spx-close', 'Close library', ICON.down, () => setOpen(false));
  D.full.replaceChildren(close, side, main);
}

function setOpen(open) {
  if (!D.full || L.open === open) return;
  L.open = open;
  D.full.hidden = !open;
  D.lib.classList.toggle('on', open);
  D.lib.setAttribute('aria-expanded', String(open));
  D.lib.title = open ? 'Close library' : 'Open your library';
  if (!open) { D.lib.focus(); return; }
  if (!L.items.length) loadShelf(L.shelf);
  if (!L.sel) select(LIKED);
  D.full.querySelector('.spx-close').focus();
}

function loadShelf(shelf) {
  L.shelf = shelf; L.items = []; L.next = 0; L.seq++;
  L.tabs.forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.shelf === shelf)));
  L.list.replaceChildren(listItem(LIKED));
  loadShelfPage();
}

async function loadShelfPage() {
  if (L.loading || L.next == null) return;
  L.loading = true;
  const seq = L.seq;
  L.more.textContent = 'Loading…';
  try {
    const r = await C.api(`/api/media/library?shelf=${encodeURIComponent(L.shelf)}&offset=${L.next}&limit=30`);
    if (seq !== L.seq) return;
    for (const it of r.items || []) { L.items.push(it); L.list.append(listItem(it)); }
    L.next = r.next_offset;
  } catch (e) {
    if (seq === L.seq) { L.note.textContent = e.message; L.next = null; }
  } finally {
    L.loading = false;
    L.more.textContent = 'Load more';
    L.more.hidden = L.next == null;
  }
}

function listItem(it) {
  const li = el('li');
  const b = el('button', 'spx-item');
  b.type = 'button';
  b.dataset.uri = it.uri || 'liked';
  const art = it.kind === 'liked' ? el('div', 'spx-art spx-liked-art') : artBox('spx-art', it.art);
  if (it.kind === 'liked') art.innerHTML = ICON.heart;
  const text = el('div', 'spx-item-text');
  text.append(el('div', 'spx-item-title', it.title || 'Untitled'), el('div', 'spx-item-sub', it.subtitle || ''));
  b.append(art, text);
  b.addEventListener('click', () => select(it));
  li.append(b);
  return li;
}

function kindOf(it) { return it.kind === 'liked' ? 'liked' : it.kind; }

function select(it) {
  L.sel = it; L.rows = []; L.tnext = 0; L.ttotal = 0; L.tseq++;
  L.list.querySelectorAll('.spx-item').forEach((b) =>
    b.setAttribute('aria-current', String(b.dataset.uri === (it.uri || 'liked'))));
  const can = (now && now.can) || {};
  L.head.replaceChildren();
  const art = it.kind === 'liked' ? el('div', 'spx-head-art spx-liked-art') : artBox('spx-head-art', it.art);
  if (it.kind === 'liked') art.innerHTML = ICON.heart;
  const info = el('div', 'spx-head-info');
  info.append(el('div', 'spx-kicker', { liked: 'Collection', playlist: 'Playlist', album: 'Album' }[it.kind] || ''),
    el('h2', 'spx-name', it.title || ''), L.count = el('div', 'spx-sub', it.subtitle || ''));
  L.head.append(art, info);
  if (it.uri && can.play_track) {
    const play = btn('spd-play spx-playall', `Play ${it.title}`, ICON.play, () => act('play_uri', it.uri));
    L.head.append(play);
  }
  L.note.textContent = '';
  if (!can.play_track) {
    L.note.textContent = 'Picking a song to play needs Spotify Premium. You can still browse, like songs, and control what’s playing in the Spotify app.';
  }
  L.table.replaceChildren();
  if (it.kind === 'playlist' && !it.browsable) {
    L.note.textContent = 'Spotify only lets apps list the songs of playlists you own or collaborate on.'
      + (can.play_track ? ' You can still play this one.' : '');
    L.tmore.hidden = true;
    return;
  }
  loadTracks();
}

async function loadTracks() {
  const it = L.sel;
  if (!it || L.tloading || L.tnext == null) return;
  L.tloading = true;
  const seq = L.tseq;
  L.tmore.textContent = 'Loading…';
  try {
    const q = `kind=${encodeURIComponent(kindOf(it))}&id=${encodeURIComponent(it.id || '')}&offset=${L.tnext}&limit=50`;
    const r = await C.api(`/api/media/tracks?${q}`);
    if (seq !== L.tseq) return;
    for (const row of r.items || []) { L.rows.push(row); L.table.append(trackRow(row, L.rows.length - 1)); }
    L.tnext = r.next_offset;
    L.ttotal = r.total || L.rows.length;
    const n = `${L.ttotal.toLocaleString()} ${L.ttotal === 1 ? 'song' : 'songs'}`;
    if (it.kind === 'liked') L.count.textContent = n;
    else if (it.kind === 'album') L.count.textContent = `${it.subtitle ? `${it.subtitle} · ` : ''}${n}`;
    markPlaying();
  } catch (e) {
    if (seq === L.tseq) { L.note.textContent = e.message; L.tnext = null; }
  } finally {
    if (seq === L.tseq) {
      L.tloading = false;
      L.tmore.textContent = 'Load more songs';
      L.tmore.hidden = L.tnext == null;
    }
  }
}

function trackRow(row, index) {
  const can = (now && now.can) || {};
  const r = el('div', 'spx-row');
  r.setAttribute('role', 'listitem');
  r.dataset.uri = row.uri || '';
  const playable = row.playable && can.play_track;
  const num = el('div', 'spx-num');
  num.append(el('span', 'spx-n', String(row.number || index + 1)));
  if (playable) {
    const p = btn('spx-rowplay', `Play ${row.title}`, ICON.play, () => playRow(index));
    num.append(p);
    r.addEventListener('dblclick', (e) => { if (!e.target.closest('button')) playRow(index); });
  }
  if (!row.playable) r.classList.add('off');
  const t = el('div', 'spx-t');
  const art = artBox('spx-row-art', row.art_url);
  const tt = el('div', 'spx-tt');
  tt.append(el('div', 'spx-row-title', row.title || 'Unknown'), el('div', 'spx-row-artist', row.artist || ''));
  t.append(art, tt);
  const album = el('div', 'spx-alb', row.album || '');
  const dur = el('div', 'spx-dur', fmt(row.duration_ms));
  const heart = el('div', 'spx-like');
  if (row.is_liked != null && /^spotify:(track|episode):/.test(row.uri || '')) {
    const h = btn('spd-icon spd-heart', '', ICON.heart, () => likeRow(row, h));
    paintHeart(h, row.is_liked);
    heart.append(h);
  }
  r.append(num, t, album, heart, dur);
  return r;
}

function paintHeart(h, on) {
  h.setAttribute('aria-pressed', String(!!on));
  h.title = on ? 'Remove from Liked Songs' : 'Save to Liked Songs';
  h.setAttribute('aria-label', h.title);
}

async function likeRow(row, h) {
  const want = !row.is_liked;
  paintHeart(h, want);                                  // optimistic; undone below if Spotify says no
  const r = await act('like_uri', { uri: row.uri, on: want });
  if (r) row.is_liked = want; else paintHeart(h, row.is_liked);
}

function playRow(index) {
  const it = L.sel, row = L.rows[index];
  if (!it || !row) return;
  if (it.kind === 'liked') {
    // Liked Songs has no playlist address Lumi can hand Spotify, so it queues up the loaded songs from here on.
    const uris = L.rows.slice(index, index + 100).filter((x) => x.playable).map((x) => x.uri);
    act('play_in_context', { track_uri: row.uri, uris });
  } else {
    act('play_in_context', { context_uri: it.uri, track_uri: row.uri });
  }
}

function markPlaying() {
  const uri = now && now.uri;
  L.table?.querySelectorAll('.spx-row').forEach((r) => r.classList.toggle('now', !!uri && r.dataset.uri === uri));
}

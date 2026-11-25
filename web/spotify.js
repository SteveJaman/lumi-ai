/* ============================================================
   Spotify in the web app (backend: modules/spotify.py, /api/spotify/*).

     init({ api, state })   starts the sidebar mini player (host only)
     buildCard(onChange)    the Settings -> Connections card

   The mini player polls /api/spotify/player every 5 s while the tab is
   visible and something is playing (20 s when idle), and stops when the
   tab is hidden. Album art comes from Spotify's https CDN; everything
   else is written with textContent.
   ============================================================ */

let C = null;
let timer = 0;
let now = null;            // last /player answer
let busy = false;
let connected = false;
let tick = 0;

const REDIRECT_PATH = '/api/spotify/callback';
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};
const btn = (label, cls, onClick) => {
  const b = el('button', cls, label);
  b.type = 'button';
  if (onClick) b.addEventListener('click', onClick);
  return b;
};
const ICON = {
  prev: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 5h2v14H6zM20 5v14L9 12z" fill="currentColor"/></svg>',
  next: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M16 5h2v14h-2zM4 5v14l11-7z" fill="currentColor"/></svg>',
  play: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 4v16l13-8z" fill="currentColor"/></svg>',
  pause: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 4h4v16H6zM14 4h4v16h-4z" fill="currentColor"/></svg>',
};

export function init(ctx) {
  C = ctx;
  if (!C.state.isLocal) return;                       // the routes are host-only
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); else stop(); });
  refreshStatus();
}

async function refreshStatus() {
  try {
    const st = await C.api('/api/spotify/status');
    connected = !!st.connected;
  } catch { connected = false; }
  if (connected) refresh(); else { stop(); paint(); }
}

function stop() { clearTimeout(timer); timer = 0; clearInterval(tick); tick = 0; }

async function refresh() {
  stop();
  if (!connected || document.hidden) return;
  try {
    now = (await C.api('/api/spotify/player')).now;
    paint();
  } catch (e) {
    if (e.status === 409) { connected = false; now = null; paint(); return; }   // signed out
  }
  timer = setTimeout(refresh, now && now.playing ? 5000 : 20000);
  if (now && now.playing) tick = setInterval(paintProgress, 1000);
}

/* ---------- sidebar mini player ---------- */

function paint() {
  const box = document.getElementById('sp-mini');
  if (!box) return;
  box.hidden = !connected;
  if (!connected) { box.replaceChildren(); return; }
  box.replaceChildren();
  const art = el('div', 'sp-art');
  if (now && now.art && now.art.startsWith('https://')) {
    const img = el('img');
    img.src = now.art; img.alt = ''; img.referrerPolicy = 'no-referrer';
    art.append(img);
  }
  const text = el('div', 'sp-text');
  if (now) {
    const title = el(now.url && now.url.startsWith('https://open.spotify.com/') ? 'a' : 'span', 'sp-title', now.title);
    if (title.tagName === 'A') { title.href = now.url; title.target = '_blank'; title.rel = 'noopener noreferrer'; title.title = 'Open in Spotify'; }
    text.append(title, el('div', 'sp-artist', now.artists || now.album || ''));
  } else {
    text.append(el('span', 'sp-title sp-idle', 'Nothing playing'), el('div', 'sp-artist', 'Spotify'));
  }
  const ctl = el('div', 'sp-ctl');
  const mk = (name, label, action) => {
    const b = btn('', 'sp-btn', () => act(action));
    b.innerHTML = ICON[name];
    b.setAttribute('aria-label', label); b.title = label;
    return b;
  };
  const playing = !!(now && now.playing);
  ctl.append(mk('prev', 'Previous track', 'previous'),
    mk(playing ? 'pause' : 'play', playing ? 'Pause' : 'Play', playing ? 'pause' : 'play'),
    mk('next', 'Next track', 'next'));
  const bar = el('div', 'sp-bar');
  bar.append(el('i'));
  const err = el('div', 'sp-err');
  err.setAttribute('role', 'status');
  box.append(art, text, ctl, bar, err);
  paintProgress();
}

function paintProgress() {
  const i = document.querySelector('#sp-mini .sp-bar i');
  if (!i) return;
  if (!now || !now.duration_ms) { i.style.width = '0'; return; }
  const elapsed = now.progress_ms + (now.playing ? (Date.now() / 1000 - now.at) * 1000 : 0);
  i.style.width = `${Math.min(100, (elapsed / now.duration_ms) * 100)}%`;
}

async function act(action) {
  if (busy) return;
  busy = true;
  const err = document.querySelector('#sp-mini .sp-err');
  if (err) err.textContent = '';
  try {
    await C.api(`/api/spotify/player/${action}`, { method: 'POST', csrf: true, body: {} });
    setTimeout(refresh, 400);                         // Spotify takes a moment to report the change
  } catch (e) {
    if (err) err.textContent = e.message;
  } finally { busy = false; }
}

/* ---------- Settings -> Connections card ---------- */

export function buildCard(onChange) {
  const card = el('div', 'integration-card ws-card sp-card');
  const head = el('div', 'integration-header');
  const tw = el('div', 'integration-title-wrap');
  const badge = el('span', 'integration-status state-off', 'checking…');
  tw.append(el('div', 'integration-name', 'Spotify'), badge);
  head.append(el('div', 'integration-emoji', '🎵'), tw);
  card.append(head, el('div', 'integration-desc',
    'See what’s playing in the sidebar and let Lumi play, pause and skip for you. Playback control needs Spotify Premium.'));
  const body = el('div', 'sp-card-body');
  card.append(body);
  draw(card, body, badge, onChange);
  return card;
}

async function draw(card, body, badge, onChange) {
  let st;
  try { st = await C.api('/api/spotify/status'); }
  catch (e) { body.replaceChildren(el('div', 'integration-message ws-error', `Couldn’t load: ${e.message}`)); return; }
  const again = () => { draw(card, body, badge, onChange); refreshStatus(); if (onChange) onChange(); };
  const status = el('div', 'ws-form-msg');
  status.setAttribute('role', 'status');
  const fail = (e) => { status.textContent = e.message; status.classList.add('bad'); };
  const actions = el('div', 'integration-actions');
  body.replaceChildren();

  if (st.connected) {
    badge.className = 'integration-status state-ok'; badge.textContent = 'connected';
    body.append(el('div', 'integration-message',
      `Signed in as ${st.user || 'your account'} · ${st.premium ? 'Premium' : 'Free (Lumi can show what’s playing, but not control it)'}.`));
    actions.append(btn('Disconnect', 'btn btn-sm btn-danger', async () => {
      if (!confirm('Disconnect Spotify? Lumi will stop seeing and controlling your music.')) return;
      try { await C.api('/api/spotify/disconnect', { method: 'POST', csrf: true, body: {} }); } catch (e) { return fail(e); }
      again();
    }));
  } else if (!st.configured) {
    badge.className = 'integration-status state-off'; badge.textContent = 'not set up';
    const redirect = `http://127.0.0.1:${location.port || 80}${REDIRECT_PATH}`;
    const steps = el('ol', 'ws-steps');
    steps.append(el('li', '', 'Open the Spotify Developer Dashboard and create an app (any name).'));
    const li2 = el('li', '', 'Add this Redirect URI, then tick “Web API” and save: ');
    const code = el('code', 'sp-uri', redirect);
    const copy = btn('Copy', 'btn btn-sm btn-ghost', async () => {
      try { await navigator.clipboard.writeText(redirect); copy.textContent = 'Copied'; } catch { copy.textContent = 'Select and copy it'; }
    });
    li2.append(code, copy);
    steps.append(li2, el('li', '', 'Open the app’s Settings, copy its Client ID, and paste it here.'));
    const input = el('input', 'settings-input');
    input.placeholder = 'Client ID (32 letters and digits)';
    input.setAttribute('aria-label', 'Spotify Client ID');
    input.spellcheck = false;
    const save = btn('Save Client ID', 'btn btn-sm btn-primary', async () => {
      save.disabled = true; status.classList.remove('bad'); status.textContent = 'Saving…';
      try { await C.api('/api/spotify/client', { method: 'POST', csrf: true, body: { client_id: input.value } }); again(); }
      catch (e) { fail(e); save.disabled = false; }
    });
    const link = el('a', 'integration-link', 'Open Spotify Developer Dashboard ↗');
    link.href = 'https://developer.spotify.com/dashboard'; link.target = '_blank'; link.rel = 'noopener noreferrer';
    body.append(steps, input);
    actions.append(save, link);
  } else {
    badge.className = 'integration-status state-warn'; badge.textContent = 'sign in';
    body.append(el('div', 'integration-message', 'Your Client ID is saved. Sign in to Spotify to finish.'));
    const connect = btn('Connect Spotify', 'btn btn-sm btn-primary', async () => {
      connect.disabled = true; status.classList.remove('bad'); status.textContent = 'Opening Spotify…';
      let res;
      try { res = await C.api('/api/spotify/connect', { method: 'POST', csrf: true, body: {} }); }
      catch (e) { fail(e); connect.disabled = false; return; }
      if (!res.auth_url.startsWith('https://accounts.spotify.com/')) { status.textContent = 'Unexpected sign-in address; not opening it.'; return; }
      window.open(res.auth_url, '_blank', 'noopener');
      status.textContent = 'Finish signing in on the Spotify tab. This card updates by itself.';
      const started = Date.now();
      const poll = async () => {
        if (!card.isConnected || Date.now() - started > 180000) { connect.disabled = false; return; }
        try { if ((await C.api('/api/spotify/status')).connected) { again(); return; } } catch { /* keep waiting */ }
        setTimeout(poll, 2000);
      };
      setTimeout(poll, 2000);
    });
    const reset = btn('Use a different Client ID', 'btn btn-sm btn-ghost', async () => {
      try { await C.api('/api/spotify/disconnect', { method: 'POST', csrf: true, body: {} }); } catch { /* fine */ }
      try { await C.api('/api/spotify/client', { method: 'POST', csrf: true, body: { client_id: '' } }); } catch { /* reset below */ }
      again();
    });
    actions.append(connect, reset);
  }
  body.append(actions, status);
}

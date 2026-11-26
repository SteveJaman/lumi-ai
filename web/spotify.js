/* ============================================================
   Spotify in the web app (backend: modules/spotify.py, /api/spotify/*).

     init({ api, state })   starts the player bar and library (web/sp-player.js)
     buildCard(onChange)    the Settings -> Connections card
   ============================================================ */

import * as player from './sp-player.js';

let C = null;

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

export function init(ctx) {
  C = ctx;
  player.init(ctx);
}

/* The player shows while Spotify is connected; the card tells it when that changes. */
function refreshStatus() { player.refresh(); }

/* ---------- Settings -> Connections card ---------- */

export function buildCard(onChange) {
  const card = el('div', 'integration-card ws-card sp-card');
  const head = el('div', 'integration-header');
  const tw = el('div', 'integration-title-wrap');
  const badge = el('span', 'integration-status state-off', 'checking…');
  tw.append(el('div', 'integration-name', 'Spotify'), badge);
  head.append(el('div', 'integration-emoji', '🎵'), tw);
  card.append(head, el('div', 'integration-desc',
    'A player bar with your playlists, albums and Liked Songs, and Lumi can play, pause and skip for you. Picking songs to play needs Spotify Premium.'));
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
      `Signed in as ${st.user || 'your account'} · ${st.premium ? 'Premium' : 'Free (playback goes through the Spotify app on this PC)'}.`));
    if (st.needs_reconnect) {
      body.append(el('div', 'integration-message ws-error',
        'Lumi needs one more permission for Liked Songs, podcasts and the like button. Reconnect to grant it.'));
      actions.append(btn('Reconnect', 'btn btn-sm btn-primary', async () => {
        try { await C.api('/api/spotify/disconnect', { method: 'POST', csrf: true, body: {} }); } catch (e) { return fail(e); }
        again();                                     // shows "Connect Spotify" with your saved Client ID
      }));
    }
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

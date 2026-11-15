/* ============================================================
   Settings → General → Remote island: let Lumi Island on another
   device (a Surface over Tailscale) connect to this PC.
   Off by default. Turning it on listens on this PC's Tailscale
   address only and shows a pairing code ONCE (only a hash is kept).
   ============================================================ */

let C = null;            // { api }
export function init(ctx) { C = ctx; }

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

export function buildRemoteCard() {
  const wrap = el('div', 'settings-field remote');
  wrap.append(el('div', 'settings-label', 'Remote island (another device over Tailscale)'));
  const body = el('div', 'remote-body');
  const status = el('div', 'disp-status');
  status.setAttribute('role', 'status');
  wrap.append(body, status,
    el('div', 'settings-field-help',
      'Lets Lumi Island on your Surface (or another PC) connect here. It listens on your Tailscale address only, ' +
      'never on Wi-Fi or a LAN, and a device needs the pairing code to get in. A paired island can approve tools, ' +
      'chat as you and see your calendar and mail summary, so keep the code to your own devices.'));

  const say = (t, bad) => { status.textContent = t; status.classList.toggle('bad', !!bad); };

  async function act(action) {
    say(action === 'disable' ? 'Turning off…' : 'Working…');
    try {
      const res = await C.api('/api/island/remote', { method: 'POST', body: { action }, csrf: true });
      draw(res.status, res.code);
      say(res.warning ? res.warning : action === 'disable' ? 'Off. Remote islands were disconnected.' : '', !!res.warning);
    } catch (e) { say(e.message, true); }
  }

  function draw(st, code) {
    body.replaceChildren();
    const row = el('div', 'disp-row');
    row.append(el('div', 'disp-name', 'Status'),
      el('span', 'remote-state ' + (st.enabled ? 'on' : 'off'), st.enabled ? `On · ${st.host}:${st.port}` : 'Off'));
    body.append(row);
    if (!st.enabled) {
      body.append(el('div', 'remote-note', st.tailscale_ip ? `Tailscale address found: ${st.tailscale_ip}`
        : 'Tailscale isn’t running on this PC. Start it and sign in first.'));
      const on = el('button', 'btn btn-sm btn-primary', 'Turn on and show a pairing code');
      on.type = 'button';
      on.disabled = !st.tailscale_ip;
      on.addEventListener('click', () => act('enable'));
      body.append(on);
      return;
    }
    if (code) {
      const box = el('div', 'remote-code');
      box.append(el('div', 'remote-code-label', 'Pairing code (shown only now)'), el('div', 'remote-code-value', code));
      const steps = el('ol', 'remote-steps');
      for (const t of [
        'On the Surface, install Lumi Island (the setup file from island/src-tauri/target/release/bundle/).',
        'Open it, hover the top of the screen, open the island and click the gear.',
        `Host: ${st.host}   Port: ${st.port}   Code: the one above. Save.`,
      ]) steps.append(el('li', '', t));
      box.append(steps);
      body.append(box);
    } else {
      body.append(el('div', 'remote-note', 'Paired islands can connect. Lost the code? Make a new one (this disconnects every paired island).'));
    }
    const btns = el('div', 'remote-btns');
    const regen = el('button', 'btn btn-sm', 'New code');
    regen.type = 'button';
    regen.addEventListener('click', () => { if (confirm('Make a new pairing code? Every paired island is disconnected and needs the new code.')) act('regenerate'); });
    const off = el('button', 'btn btn-sm btn-danger', 'Turn off');
    off.type = 'button';
    off.addEventListener('click', () => act('disable'));
    btns.append(regen, off);
    body.append(btns);
  }

  body.append(el('div', 'remote-note', 'Loading…'));
  C.api('/api/island/remote').then((st) => draw(st)).catch((e) => say(e.message, true));
  return wrap;
}

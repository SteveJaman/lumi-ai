// Lumi Island: hidden sliver → peek on hover → open on click, plus
// approval cards from Lumi's HITL server (modules/hitl_server.py), which
// stay until answered. Status (idle / thinking / speaking) is pushed by
// Lumi; nothing here polls. One reconnect timer runs only while Lumi is
// unreachable.
(() => {
  'use strict';

  const WS_URL = 'ws://127.0.0.1:5090/hitl';
  const MAX_BACKOFF = 10000;
  const MORPH_MS = 340;       // a little longer than --dur
  const LEAVE_MS = 450;       // grace period before peek/open tucks away
  const SIZES = {             // must match island.css
    hidden: [200, 8], peek: [260, 44], open: [340, 116], approval: [420, 276],
  };
  const STATUS_TEXT = {
    idle: 'Here with you', thinking: 'Thinking…', speaking: 'Speaking…', offline: 'Offline',
  };
  const DANGER = /^(run_command|run_in_terminal|write_file|delete_file)$/;

  const $ = (id) => document.getElementById(id);
  const island = $('island');

  let state = 'hidden';
  let hovering = false;
  let leaveTimer = null;
  let queue = [];
  let ws = null, retry = 500, reconnectTimer = null;

  // ── Window size (Rust side: island/src-tauri/src/main.rs) ─────
  function fit(w, h) {
    const invoke = window.__TAURI__ && window.__TAURI__.core && window.__TAURI__.core.invoke;
    if (invoke) invoke('fit', { width: w, height: h }).catch(() => {});
  }

  function setState(next) {
    if (next === state) return;
    const [w, h] = SIZES[next];
    const [cw, ch] = SIZES[state];
    const grows = w >= cw && h >= ch;
    if (grows) fit(w, h);             // room first, then animate into it
    else fit(Math.max(w, cw), Math.max(h, ch));
    state = next;
    island.className = `island ${next}${island.classList.contains('danger') ? ' danger' : ''}`;
    $('approval').inert = next !== 'approval';
    if (!grows) setTimeout(() => { if (state === next) fit(w, h); }, MORPH_MS);
  }

  // Where to go when nothing forces a state.
  function rest() {
    if (queue.length) return setState('approval');
    setState(hovering ? (state === 'open' ? 'open' : 'peek') : 'hidden');
  }

  // ── Hover and click (coucou-style) ─────────────────────────────
  island.addEventListener('mouseenter', () => {
    hovering = true;
    clearTimeout(leaveTimer);
    if (state === 'hidden') setState('peek');
  });
  island.addEventListener('mouseleave', () => {
    hovering = false;
    clearTimeout(leaveTimer);
    leaveTimer = setTimeout(rest, LEAVE_MS);
  });
  $('face').addEventListener('click', () => {
    if (state === 'peek') setState('open');
    else if (state === 'open') setState('peek');
  });

  // ── Status ────────────────────────────────────────────────────
  function setStatus(s) {
    island.dataset.status = s;
    $('status').textContent = STATUS_TEXT[s] || s;
  }

  // ── Approvals ─────────────────────────────────────────────────
  function renderApproval() {
    const cur = queue[0];
    if (!cur) { island.classList.remove('danger'); rest(); return; }
    const danger = DANGER.test(cur.tool);
    island.classList.toggle('danger', danger);
    $('eyebrow').textContent = danger ? 'Lumi wants to run · check carefully' : 'Lumi wants to run';
    $('tool').textContent = cur.tool || 'unknown tool';
    $('preview').textContent = cur.preview || '(no details)';
    $('preview').scrollTop = 0;
    $('more').hidden = queue.length < 2;
    $('more').textContent = `+${queue.length - 1}`;
    setState('approval');
  }

  function respond(decision) {
    const cur = queue.shift();
    if (cur && ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'approval_response', id: cur.id, decision }));
    }
    renderApproval();
  }
  $('approve').addEventListener('click', () => respond('approve'));
  $('decline').addEventListener('click', () => respond('decline'));

  // ── Connection to Lumi ─────────────────────────────────────────
  function connect() {
    reconnectTimer = null;
    try { ws = new WebSocket(WS_URL); } catch (_) { scheduleReconnect(); return; }

    ws.onopen = () => { retry = 500; setStatus('idle'); };

    ws.onmessage = (e) => {
      let msg;
      try { msg = JSON.parse(e.data); } catch (_) { return; }
      if (!msg || typeof msg !== 'object') return;
      if (msg.type === 'lumi_status' && STATUS_TEXT[msg.state]) {
        setStatus(msg.state);
      } else if (msg.type === 'approval_request' && msg.id) {
        if (!queue.some((r) => r.id === msg.id)) {   // re-sent on reconnect
          queue.push({ id: String(msg.id), tool: String(msg.tool || ''),
                       preview: String(msg.preview || '') });
          renderApproval();
        }
      } else if (msg.type === 'approval_expired') {
        const n = queue.length;
        queue = queue.filter((r) => r.id !== msg.id);
        if (queue.length !== n) renderApproval();
      }
    };

    ws.onclose = () => {
      ws = null;
      setStatus('offline');
      if (queue.length) { queue = []; renderApproval(); }  // Lumi fell back to the terminal
      scheduleReconnect();
    };
    ws.onerror = () => { try { ws && ws.close(); } catch (_) {} };
  }

  function scheduleReconnect() {
    if (reconnectTimer) return;
    reconnectTimer = setTimeout(connect, retry);
    retry = Math.min(retry * 2, MAX_BACKOFF);
  }

  setStatus('offline');
  fit(...SIZES.hidden);
  connect();
})();

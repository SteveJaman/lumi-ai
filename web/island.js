// Lumi Island: shows approval requests from the HITL server
// (modules/hitl_server.py) and sends back approve/decline.
// No timers run while connected and idle. A single reconnect timer
// runs only after the socket closes.
(() => {
  'use strict';

  const WS_URL = 'ws://127.0.0.1:5090/hitl';
  const MAX_BACKOFF = 10000;
  const DANGER = /^(run_command|run_in_terminal|write_file|delete_file)$/;

  if (new URLSearchParams(location.search).has('overlay')) {
    document.documentElement.classList.add('overlay');
  }

  const $ = (id) => document.getElementById(id);
  const island = $('island');
  const label = $('label');
  const more = $('more');
  const card = $('card');
  const eyebrow = $('eyebrow');
  const tool = $('tool');
  const preview = $('preview');
  const approve = $('approve');
  const decline = $('decline');
  const hint = $('hint');

  let queue = [];          // pending requests, oldest first
  let ws = null;
  let retry = 500;
  let reconnectTimer = null;

  function setInteractive(on) {
    // Only exists inside launch_island.py's window.
    try { window.pywebview?.api?.set_interactive?.(on); } catch (_) {}
  }

  function render() {
    const cur = queue[0];
    const open = Boolean(cur);
    island.classList.toggle('open', open);
    card.inert = !open;
    approve.disabled = decline.disabled = !open;

    if (open) {
      const danger = DANGER.test(cur.tool || '');
      island.classList.toggle('danger', danger);
      island.setAttribute('role', 'alertdialog');
      island.setAttribute('aria-label', `Approve ${cur.tool}?`);
      label.textContent = 'Approval needed';
      eyebrow.textContent = danger ? 'Lumi wants to run (check carefully)'
                                   : 'Lumi wants to run';
      tool.textContent = cur.tool || 'unknown tool';
      preview.textContent = cur.preview || '';
      preview.scrollTop = 0;
      more.hidden = queue.length < 2;
      more.textContent = `+${queue.length - 1}`;
    } else {
      // Keep the old text in the card while it animates shut.
      island.removeAttribute('role');
      island.removeAttribute('aria-label');
      label.textContent = 'Lumi';
      more.hidden = true;
    }
    setInteractive(open);
  }

  function setStatus(status) {
    island.dataset.status = status;
    hint.textContent = {
      open: 'Connected to Lumi. Approval requests will appear here.',
      connecting: 'Connecting to Lumi…',
      closed: 'Lumi isn’t running. Reconnecting quietly in the background.',
    }[status];
  }

  function connect() {
    reconnectTimer = null;
    setStatus('connecting');
    try {
      ws = new WebSocket(WS_URL);
    } catch (_) {
      scheduleReconnect();
      return;
    }

    ws.onopen = () => {
      retry = 500;
      setStatus('open');
    };

    ws.onmessage = (e) => {
      let msg;
      try { msg = JSON.parse(e.data); } catch (_) { return; }
      if (!msg || typeof msg !== 'object') return;
      if (msg.type === 'approval_request' && msg.id) {
        // The server re-sends open requests on reconnect.
        if (!queue.some((r) => r.id === msg.id)) {
          queue.push({ id: String(msg.id), tool: String(msg.tool || ''),
                       preview: String(msg.preview || '') });
          render();
        }
      } else if (msg.type === 'approval_expired') {
        const before = queue.length;
        queue = queue.filter((r) => r.id !== msg.id);
        if (queue.length !== before) render();
      }
    };

    ws.onclose = () => {
      ws = null;
      // The server has already handed these to the terminal.
      if (queue.length) { queue = []; render(); }
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

  function respond(decision) {
    const cur = queue[0];
    if (!cur) return;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'approval_response', id: cur.id, decision }));
    }
    queue.shift();
    render();
  }

  approve.addEventListener('click', () => respond('approve'));
  decline.addEventListener('click', () => respond('decline'));
  // pywebview's bridge appears after load; sync click-through once it does.
  window.addEventListener('pywebviewready', render);

  render();
  connect();
})();

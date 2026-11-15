/*
 * Lumi Pipeline: a live pixel-art office for Lumi's agents.
 *
 * Shared by the web app (web/pipeline.js) and Lumi Island
 * (island/src/pipeline.js). The two files are identical; edit one and copy
 * it over the other.
 *
 * Usage:
 *   const pl = LumiPipeline.mount(wrapperEl, { onDecision: (id, 'approve'|'decline') => {} });
 *   pl.update(agents);         // the agent_update payload: {main: {status, visual_state, ...}, ...}
 *   pl.setApprovals(list);     // [{id, tool, agent, preview}] waiting for the user
 *   pl.destroy();
 *
 * Rendering: everything is drawn with fillRect into a 192x108 off-screen
 * buffer (one unit = one art pixel), then scaled up in a single drawImage
 * with smoothing off. Names and the approval overlay are drawn at native
 * resolution so text stays sharp. The loop runs at up to 30 fps only while
 * something moves or animates, drops to 2 fps when everyone is idle, and
 * stops when the page is hidden or the canvas is off-screen.
 */
(function () {
  'use strict';

  const W = 192, H = 108;
  const ORDER = ['main', 'minecraft', 'office', 'system'];
  const LOOK = {
    main:      { name: 'Main',      shirt: '#4aa3ff', dark: '#2a6cb5', hair: '#3b2a22' },
    minecraft: { name: 'Minecraft', shirt: '#5bd16a', dark: '#2f8a3c', hair: '#6b4a2a' },
    office:    { name: 'Office',    shirt: '#b48cff', dark: '#7553c4', hair: '#1f1d2b' },
    system:    { name: 'System',    shirt: '#ff5a6a', dark: '#b8303d', hair: '#e9e1cf' },
  };
  // Feet positions. station: where it works; idle: where it rests;
  // front: where it walks to wave at you when it needs an approval.
  const SPOTS = {
    main:      { station: [86, 63, 1], idle: [86, 63, 1], front: [54, 100] },
    minecraft: { station: [32, 50],    idle: [24, 78],    front: [84, 100] },
    office:    { station: [128, 52],   idle: [119, 84, 1], front: [114, 100] },
    system:    { station: [165, 58],   idle: [150, 86],   front: [144, 100] },
  };
  const ANIMATED = new Set(['typing', 'swinging', 'reading', 'blinking', 'waving', 'alarm']);
  const WALK_SPEED = 40;           // art pixels per second
  const FAST_MS = 33, SLOW_MS = 500;
  const SKIN = '#f2c9a0', PANTS = '#2a2f45', SHOE = '#151821', EYE = '#1a1a22';

  function derive(id, status) {
    if (status === 'waiting_approval') return 'waving';
    if (status === 'error') return 'alarm';
    const busy = status === 'thinking' || status === 'executing';
    return {
      main: busy ? 'typing' : 'sitting', minecraft: busy ? 'swinging' : 'standing',
      office: busy ? 'reading' : 'sitting', system: busy ? 'blinking' : 'standing',
    }[id] || 'standing';
  }

  function spotFor(id, vs) {
    const s = SPOTS[id];
    if (vs === 'waving') return s.front;
    if (vs === 'sitting' || vs === 'standing') return s.idle;
    return s.station;
  }

  // ── static room (drawn once into its own buffer) ──────────────────
  function drawRoom(c) {
    const r = (x, y, w, h, col) => { c.fillStyle = col; c.fillRect(x, y, w, h); };
    r(0, 0, W, 38, '#1a1f2e');                                  // back wall
    for (let x = 0; x < W; x += 24) r(x, 0, 1, 38, '#1e2435');  // wall panels
    r(0, 36, W, 2, '#2c3449');                                  // skirting
    for (let y = 38; y < H; y += 8)                             // floor tiles
      for (let x = 0; x < W; x += 8)
        r(x, y, 8, 8, ((x + y) >> 3) % 2 ? '#222a3b' : '#252e41');
    r(56, 76, 80, 22, '#2b2440'); r(58, 78, 76, 18, '#332a4d');  // rug

    // Window (Minecraft's station): sky, sun, blocky hills.
    r(10, 5, 44, 28, '#39445f');
    r(12, 7, 40, 24, '#6fb6ff'); r(12, 7, 40, 8, '#8ec7ff');
    r(42, 9, 6, 6, '#ffe27a');
    const hills = [[12, 22, 8], [20, 19, 6], [26, 21, 10], [36, 18, 8], [44, 21, 8]];
    for (const [x, y, w] of hills) { r(x, y, w, 31 - y, '#5bbf5b'); r(x, y + 2, w, 31 - y - 2, '#8b5a2b'); }
    r(31, 7, 2, 24, '#39445f'); r(12, 18, 40, 2, '#39445f');     // panes
    r(9, 33, 46, 2, '#4a5675');                                 // sill

    // Desk (Main's station) with monitor and keyboard.
    r(70, 56, 40, 4, '#7a5233'); r(70, 60, 40, 1, '#5a3b24');
    r(72, 61, 3, 11, '#5a3b24'); r(105, 61, 3, 11, '#5a3b24');
    r(73, 43, 16, 11, '#151821'); r(80, 54, 2, 2, '#151821');    // monitor
    r(92, 54, 12, 2, '#3a4157');                                 // keyboard

    // Bookshelf (Office's station).
    r(114, 8, 28, 36, '#5a3d27');
    const books = ['#e0675a', '#5aa0e0', '#e0c35a', '#7ad48a', '#c27ae0', '#e08f5a'];
    for (let s = 0; s < 4; s++) {
      const y = 10 + s * 8;
      r(116, y + 6, 24, 2, '#3e2a1b');
      for (let b = 0; b < 7; b++) r(117 + b * 3, y + (b % 3 === 0 ? 1 : 0), 2, 6 - (b % 3 === 0 ? 1 : 0), books[(b + s) % books.length]);
    }
    r(106, 80, 26, 6, '#5b3f7a'); r(106, 76, 26, 4, '#6b4b8a');  // couch
    r(104, 76, 3, 10, '#4c3466'); r(131, 76, 3, 10, '#4c3466');

    // Server rack (System's station).
    r(155, 6, 24, 46, '#2b2f3a'); r(156, 7, 22, 44, '#20232c');
    for (let i = 0; i < 8; i++) r(157, 9 + i * 5, 20, 4, '#2f3441');
    // Plant and coffee corner.
    r(176, 84, 8, 6, '#8b5a2b'); r(175, 76, 10, 8, '#3f9a52'); r(177, 72, 6, 5, '#4fb563');
    r(4, 60, 10, 14, '#3a4157'); r(5, 61, 8, 4, '#c2c9da'); r(7, 66, 4, 3, '#6b4a2a');  // coffee machine
  }

  // ── characters ─────────────────────────────────────────────────────
  function drawAgent(c, a, t) {
    const L = LOOK[a.id];
    const flash = a.vs === 'alarm' && Math.floor(t * 4) % 2 === 0;
    const shirt = flash ? '#ff2d3d' : L.shirt, dark = flash ? '#a3121e' : L.dark;
    const skin = flash ? '#ff9a9a' : SKIN;
    const r = (x, y, w, h, col) => { c.fillStyle = col; c.fillRect(Math.round(x), Math.round(y), w, h); };
    let x = a.x, y = a.y;
    const seated = !a.walking && a.seated;
    let bob = 0;
    if (a.walking) bob = Math.floor(t * 8) % 2;
    else if (a.vs === 'typing') bob = Math.floor(t * 6) % 2;
    else if (a.vs === 'waving' || a.vs === 'alarm') bob = Math.floor(t * 3) % 2;
    else bob = Math.floor(t / 1.6) % 2 === 0 ? 0 : 1;          // breathing
    y -= bob;

    r(x - 5, a.y - 1, 10, 2, 'rgba(0,0,0,0.25)');               // shadow
    // legs
    if (seated) {
      r(x - 5, y - 4, 4, 3, PANTS); r(x + 1, y - 4, 4, 3, PANTS);
      r(x - 6, y - 2, 3, 2, SHOE); r(x + 3, y - 2, 3, 2, SHOE);
      y -= 1;
    } else {
      const step = a.walking ? (Math.floor(t * 8) % 2 ? 1 : -1) : 0;
      r(x - 4, y - 5 + Math.max(0, step), 3, 5 - Math.max(0, step), PANTS);
      r(x + 1, y - 5 + Math.max(0, -step), 3, 5 - Math.max(0, -step), PANTS);
      r(x - 4, y - 1, 3, 1, SHOE); r(x + 1, y - 1, 3, 1, SHOE);
    }
    // body
    r(x - 5, y - 11, 10, 6, shirt); r(x - 5, y - 6, 10, 1, dark);
    // arms by state
    const vs = a.walking ? 'walking' : a.vs;
    if (vs === 'typing') { r(x - 7, y - 9, 3, 2, dark); r(x + 4, y - 9, 3, 2, dark); r(x - 7, y - 8, 2, 1, skin); r(x + 5, y - 8, 2, 1, skin); }
    else if (vs === 'waving' || vs === 'alarm') {
      r(x - 7, y - 11, 2, 5, dark);
      const up = Math.floor(t * 4) % 2;
      r(x + 5, y - 17 + up, 2, 6, dark); r(x + 5 + (up ? 1 : 0), y - 18 + up, 2, 2, skin);
    } else if (vs === 'blinking') { r(x - 7, y - 11, 2, 5, dark); r(x + 5, y - 14, 2, 5, dark); r(x + 6, y - 15, 2, 2, skin); }
    else if (vs === 'reading') {
      r(x - 6, y - 10, 2, 3, dark); r(x + 4, y - 10, 2, 3, dark);
      const flip = Math.floor(t) % 2;
      r(x - 4, y - 11, 4, 5, flip ? '#f3efe4' : '#dcd6c6'); r(x, y - 11, 4, 5, flip ? '#dcd6c6' : '#f3efe4');
      r(x - 3, y - 9, 2, 1, '#9a9488'); r(x + 1, y - 9, 2, 1, '#9a9488');
    } else if (vs === 'swinging') {
      r(x - 7, y - 11, 2, 5, dark);
      const f = Math.floor(t * 6) % 3;                         // pickaxe: up, mid, down
      const tip = [[x + 9, y - 18], [x + 12, y - 11], [x + 9, y - 4]][f];
      const hx = x + 6, hy = y - 9;
      for (let i = 0; i <= 4; i++) r(hx + (tip[0] - hx) * i / 4, hy + (tip[1] - hy) * i / 4, 1, 1, '#8b5a2b');
      r(tip[0] - 2, tip[1] - 1, 4, 2, '#b8c2d1');
      r(x + 5, y - 10, 2, 3, skin);
    } else {
      const swing = a.walking ? Math.floor(t * 8) % 2 : 0;
      r(x - 7, y - 11 + swing, 2, 5, dark); r(x + 5, y - 11 + (1 - swing), 2, 5, dark);
    }
    // head
    r(x - 4, y - 17, 8, 6, skin);
    r(x - 4, y - 18, 8, 2, L.hair); r(x - 5, y - 17, 1, 3, L.hair); r(x + 4, y - 17, 1, 3, L.hair);
    const blink = Math.floor((t + a.phase) % 4.2 * 10) === 0;
    if (!blink) { r(x - 2, y - 14, 1, 1, EYE); r(x + 1, y - 14, 1, 1, EYE); }
    if (vs === 'alarm') r(x - 1, y - 12, 2, 1, EYE);            // open mouth

    // "!" above the head while it needs you (yellow) or is in trouble (red)
    if ((vs === 'waving' && Math.floor(t * 2) % 2 === 0) || vs === 'alarm') {
      const col = vs === 'alarm' ? (flash ? '#ff2d3d' : '#ffd2d2') : '#ffd23f';
      r(x - 1, y - 27, 2, 5, col); r(x - 1, y - 21, 2, 2, col);
    }
    a.headY = y - 18;
  }

  function drawDynamic(c, agents, t) {
    const r = (x, y, w, h, col) => { c.fillStyle = col; c.fillRect(x, y, w, h); };
    const by = Object.fromEntries(agents.map((a) => [a.id, a]));
    // Monitor: code scrolling while Main types.
    const typing = by.main && by.main.vs === 'typing' && !by.main.walking;
    r(74, 44, 14, 9, typing ? '#12405a' : '#0d1a24');
    if (typing) {
      const off = Math.floor(t * 6);
      for (let i = 0; i < 4; i++) r(75, 45 + i * 2, 3 + ((i + off) * 5) % 9, 1, i % 2 ? '#5cf2d2' : '#9ad1ff');
    } else if (Math.floor(t * 2) % 2) r(75, 51, 2, 1, '#5cf2d2');
    // Rack LEDs: calm green, busy flicker, red alarm.
    const sys = by.system ? by.system.vs : 'standing';
    for (let row = 0; row < 8; row++) {
      for (let col = 0; col < 3; col++) {
        let on, color = '#3ddc84';
        if (sys === 'alarm') { on = Math.floor(t * 4) % 2 === 0; color = '#ff3b4a'; }
        else if (sys === 'blinking') { on = ((row * 7 + col * 13 + Math.floor(t * 12)) * 2654435761 >>> 0) % 3 !== 0; color = (row + col) % 3 ? '#3ddc84' : '#ffc23d'; }
        else on = (row + col + Math.floor(t / 1.5)) % 5 === 0;
        r(159 + col * 4, 10 + row * 5, 2, 2, on ? color : '#1b3326');
      }
    }
    // Characters back to front; Main sits behind the desk.
    const sorted = agents.slice().sort((a, b) => a.y - b.y);
    for (const a of sorted) drawAgent(c, a, t);
    if (by.main && by.main.seated && !by.main.walking && Math.abs(by.main.x - SPOTS.main.station[0]) < 1) {
      r(70, 56, 40, 4, '#7a5233'); r(70, 60, 40, 1, '#5a3b24');   // desk edge in front of her legs
      r(92, 54, 12, 2, '#3a4157');
    }
  }

  // ── component ─────────────────────────────────────────────────────
  function mount(wrapper, opts) {
    opts = opts || {};
    let canvas = wrapper.querySelector('canvas');
    if (!canvas) { canvas = document.createElement('canvas'); canvas.id = 'pipeline-canvas'; wrapper.appendChild(canvas); }
    canvas.classList.add('lumi-pl-canvas');
    let overlay = wrapper.querySelector('.lumi-pl-overlay');
    if (!overlay) { overlay = document.createElement('div'); overlay.className = 'lumi-pl-overlay'; wrapper.appendChild(overlay); }
    const ctx = canvas.getContext('2d');
    const buf = document.createElement('canvas'); buf.width = W; buf.height = H;
    const bctx = buf.getContext('2d');
    const room = document.createElement('canvas'); room.width = W; room.height = H;
    drawRoom(room.getContext('2d'));

    const agents = ORDER.map((id, i) => {
      const [x, y, seated] = SPOTS[id].idle;
      return { id, x, y, tx: x, ty: y, seated: !!seated, vs: derive(id, 'idle'), status: 'idle', task: '', progress: 0,
               walking: false, phase: i * 1.3, headY: y - 18, placed: false };
    });
    let approvals = [];
    let scale = 1, cssScale = 1, raf = 0, timer = 0, last = 0, visible = true, destroyed = false;
    const t0 = performance.now();

    function resize() {
      const cw = wrapper.clientWidth || W * 2;
      const dpr = window.devicePixelRatio || 1;
      scale = Math.max(1, Math.ceil((cw * dpr) / W));
      canvas.width = W * scale; canvas.height = H * scale;
      canvas.style.width = cw + 'px'; canvas.style.height = Math.round(cw * H / W) + 'px';
      cssScale = cw / W;
      kick();
    }

    function step(dt) {
      for (const a of agents) {
        const dx = a.tx - a.x, dy = a.ty - a.y, d = Math.hypot(dx, dy);
        if (d < 0.5) { a.x = a.tx; a.y = a.ty; a.walking = false; continue; }
        const mv = Math.min(d, WALK_SPEED * dt);
        a.x += dx / d * mv; a.y += dy / d * mv; a.walking = true;
      }
    }

    function render(t) {
      bctx.drawImage(room, 0, 0);
      drawDynamic(bctx, agents, t);
      ctx.imageSmoothingEnabled = false;
      ctx.drawImage(buf, 0, 0, W * scale, H * scale);
      // Names and progress bars at native resolution.
      const fs = Math.max(9, Math.round(3.4 * scale));
      ctx.font = `600 ${fs}px ui-monospace, Consolas, monospace`;
      ctx.textAlign = 'center'; ctx.textBaseline = 'top';
      for (const a of agents) {
        const x = a.x * scale, y = (a.y + 1.5) * scale;
        ctx.fillStyle = 'rgba(0,0,0,0.55)'; ctx.fillText(LOOK[a.id].name, x + 1, y + 1);
        ctx.fillStyle = LOOK[a.id].shirt; ctx.fillText(LOOK[a.id].name, x, y);
        if ((a.status === 'executing' || a.status === 'thinking') && a.progress > 0) {
          const bw = 14 * scale, bh = Math.max(2, scale * 0.8), by = y + fs + 2;
          ctx.fillStyle = 'rgba(255,255,255,0.12)'; ctx.fillRect(x - bw / 2, by, bw, bh);
          ctx.fillStyle = LOOK[a.id].shirt; ctx.fillRect(x - bw / 2, by, bw * Math.min(1, a.progress / 100), bh);
        }
      }
      placeOverlay();
    }

    function animating() {
      return agents.some((a) => a.walking || ANIMATED.has(a.vs));
    }

    function loop(now) {
      raf = 0; timer = 0;
      if (destroyed || !visible || document.hidden) return;
      const dt = last ? Math.min(0.1, (now - last) / 1000) : 0;
      last = now;
      step(dt);
      render((now - t0) / 1000);
      const wait = animating() ? FAST_MS : SLOW_MS;
      timer = setTimeout(() => { raf = requestAnimationFrame(loop); }, Math.max(0, wait - 16));
    }

    function kick() {
      if (destroyed || raf || !visible || document.hidden) return;
      clearTimeout(timer); timer = 0;
      last = 0;
      raf = requestAnimationFrame(loop);
    }

    // ── approval overlay ───────────────────────────────────────────
    const cards = new Map();                         // agent id -> element
    function placeOverlay() {
      const byAgent = new Map();
      for (const ap of approvals) {
        const id = ORDER.includes(ap.agent) ? ap.agent : 'main';
        if (!byAgent.has(id)) byAgent.set(id, []);
        byAgent.get(id).push(ap);
      }
      for (const [id, el] of cards) if (!byAgent.has(id)) { el.remove(); cards.delete(id); }
      for (const [id, list] of byAgent) {
        let el = cards.get(id);
        const first = list[0];
        if (!el) {
          el = document.createElement('div');
          el.className = 'lumi-pl-ask';
          el.innerHTML = '<div class="lumi-pl-tool"></div><div class="lumi-pl-btns">'
            + '<button type="button" class="lumi-pl-no">Decline</button>'
            + '<button type="button" class="lumi-pl-yes">Approve</button></div>';
          el.querySelector('.lumi-pl-yes').addEventListener('click', () => decide(el.dataset.req, 'approve'));
          el.querySelector('.lumi-pl-no').addEventListener('click', () => decide(el.dataset.req, 'decline'));
          overlay.appendChild(el);
          cards.set(id, el);
        }
        if (el.dataset.req !== first.id) {
          el.dataset.req = first.id;
          el.querySelector('.lumi-pl-tool').textContent =
            `${LOOK[id].name} wants: ${first.tool}${list.length > 1 ? ` (+${list.length - 1})` : ''}`;
          el.title = (first.preview || '').slice(0, 600);
        }
        const a = agents.find((g) => g.id === id);
        const left = Math.round(a.x * cssScale) + 'px', top = Math.round((a.headY - 11) * cssScale) + 'px';
        if (el.style.left !== left) el.style.left = left;
        if (el.style.top !== top) el.style.top = top;
      }
    }
    function decide(id, decision) {
      if (!id) return;
      approvals = approvals.filter((ap) => ap.id !== id);
      if (opts.onDecision) opts.onDecision(id, decision);
      placeOverlay();
    }

    // ── public API ─────────────────────────────────────────────────
    function update(snapshot) {
      for (const a of agents) {
        const s = (snapshot || {})[a.id];
        if (!s) continue;
        a.status = s.status || 'idle';
        a.vs = s.visual_state || derive(a.id, a.status);
        a.task = s.task || '';
        a.progress = Number(s.progress) || 0;
        const [tx, ty, seated] = spotFor(a.id, a.vs);
        a.tx = tx; a.ty = ty; a.seated = !!seated;
        if (!a.placed) { a.x = tx; a.y = ty; a.placed = true; }     // first data: no walk-in
      }
      kick();
    }
    function setApprovals(list) {
      approvals = (list || []).filter((ap) => ap && ap.id);
      placeOverlay();
      kick();
    }

    const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(resize) : null;
    if (ro) ro.observe(wrapper); else window.addEventListener('resize', resize);
    const io = typeof IntersectionObserver !== 'undefined'
      ? new IntersectionObserver((es) => { visible = es.some((e) => e.isIntersecting); if (visible) kick(); })
      : null;
    if (io) io.observe(canvas);
    const onVis = () => { if (!document.hidden) kick(); };
    document.addEventListener('visibilitychange', onVis);
    resize();

    return {
      update, setApprovals,
      destroy() {
        destroyed = true;
        cancelAnimationFrame(raf); clearTimeout(timer);
        if (ro) ro.disconnect(); else window.removeEventListener('resize', resize);
        if (io) io.disconnect();
        document.removeEventListener('visibilitychange', onVis);
        overlay.textContent = '';
      },
      _agents: agents,                   // for tests
    };
  }

  window.LumiPipeline = { mount, derive, spotFor, SPOTS, ORDER };
})();

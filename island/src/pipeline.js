/*
 * Lumi Pipeline: her agents at work in her cyberpunk room.
 *
 * Shared by the web app (web/pipeline.js) and Lumi Island (island/src/pipeline.js);
 * the two files are identical, edit one and copy it over the other. The art lives in
 * pixel-office/ next to this file and is baked by scripts/build_pixel_office.py:
 * Pixel Agents' character poses, tiles and some furniture (MIT,
 * github.com/pixel-agents-hq/pixel-agents), redrawn as four Lumis after her character
 * sheet, in a room of her own.
 *
 *   const pl = LumiPipeline.mount(wrapperEl, {
 *     base: '/web/pixel-office/',                 // where the baked art is
 *     onDecision: (id, 'approve' | 'decline') => {},
 *   });
 *   pl.update(agents);      // agent_update payload: {main: {status, visual_state, task, progress}, ...}
 *   pl.setApprovals(list);  // [{id, tool, agent, preview}]
 *   pl.destroy();
 *
 * How it plays (after Pixel Agents' office engine): a busy agent walks (BFS over the
 * room's walk grid, 48 px/s) to its work station and types, or reads while it searches
 * or reads pages; an idle one walks to its resting seat and sits. Its monitors switch on
 * while it works. An approval puts the permission bubble over its head and Approve /
 * Decline buttons above it; a finished task shows the green check for 2 s; an error
 * flashes it red. The room is drawn at 1x into a 320x208 buffer and scaled up crisp;
 * names and buttons are drawn at native resolution. 30 fps while anyone walks or works,
 * about 8 fps otherwise (the server LEDs), nothing while hidden.
 */
(function () {
  'use strict';

  const ORDER = ['main', 'minecraft', 'office', 'system'];
  const NAMES = { main: 'Main', minecraft: 'Minecraft', office: 'Office', system: 'System' };
  const READING_RE = /web_search|browse|research|search_files|read_file|read_window|reading|searching|triag|recall|library/i;
  const FAST_MS = 33, SLOW_MS = 120;
  const BLINK_S = 0.15;
  const DONE_BUBBLE_S = 2;

  let assetsPromise = null;
  function loadAssets(base) {
    if (assetsPromise) return assetsPromise;
    const img = (src) => new Promise((resolve, reject) => {
      const i = new Image();
      i.onload = () => resolve(i);
      i.onerror = () => reject(new Error(`couldn't load ${src}`));
      i.src = base + src;
    });
    const meta = window.LUMI_OFFICE ? Promise.resolve(window.LUMI_OFFICE) : new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = base + 'office.js';
      s.onload = () => (window.LUMI_OFFICE ? resolve(window.LUMI_OFFICE) : reject(new Error('office.js is empty')));
      s.onerror = () => reject(new Error(`couldn't load ${base}office.js`));
      document.head.appendChild(s);
    });
    assetsPromise = Promise.all([
      meta, img('floor.png'), img('atlas.png'), img('bubble-permission.png'), img('bubble-waiting.png'),
      ...ORDER.flatMap((a) => [img(`chars/${a}.png`), img(`chars/${a}_blink.png`)]),
    ]).then(([meta, floor, atlas, bubblePerm, bubbleDone, ...sheets]) => {
      const chars = {};
      ORDER.forEach((a, i) => { chars[a] = { open: sheets[i * 2], blink: sheets[i * 2 + 1] }; });
      return { meta, floor, atlas, bubblePerm, bubbleDone, chars };
    });
    assetsPromise.catch(() => { assetsPromise = null; });
    return assetsPromise;
  }

  // ── pathfinding over the walk grid (4-way BFS, like Pixel Agents' tileMap.ts) ──
  function bfs(walk, from, to) {
    const rows = walk.length, cols = walk[0].length;
    const ok = (c, r) => r >= 0 && r < rows && c >= 0 && c < cols && (walk[r][c] === '1' || (c === to.col && r === to.row));
    const key = (c, r) => r * cols + c;
    const prev = new Map([[key(from.col, from.row), null]]);
    const q = [[from.col, from.row]];
    while (q.length) {
      const [c, r] = q.shift();
      if (c === to.col && r === to.row) break;
      for (const [dc, dr] of [[0, -1], [1, 0], [0, 1], [-1, 0]]) {
        const nc = c + dc, nr = r + dr, k = key(nc, nr);
        if (!prev.has(k) && ok(nc, nr)) { prev.set(k, key(c, r)); q.push([nc, nr]); }
      }
    }
    const end = key(to.col, to.row);
    if (!prev.has(end)) return [{ col: to.col, row: to.row }];     // unreachable: just go there
    const path = [];
    for (let k = end; k !== null && k !== key(from.col, from.row); k = prev.get(k)) {
      path.push({ col: k % cols, row: Math.floor(k / cols) });
    }
    return path.reverse();
  }

  function mount(wrapper, opts) {
    opts = opts || {};
    const base = opts.base || 'pixel-office/';
    let canvas = wrapper.querySelector('canvas');
    if (!canvas) { canvas = document.createElement('canvas'); canvas.id = 'pipeline-canvas'; wrapper.appendChild(canvas); }
    canvas.classList.add('lumi-pl-canvas');
    let overlay = wrapper.querySelector('.lumi-pl-overlay');
    if (!overlay) { overlay = document.createElement('div'); overlay.className = 'lumi-pl-overlay'; wrapper.appendChild(overlay); }
    const ctx = canvas.getContext('2d');

    let A = null;                        // assets once loaded
    let W = 320, H = 208, buf = null, bctx = null, tint = null, tctx = null;
    let scale = 1, cssScale = 1, raf = 0, timer = 0, last = 0, visible = true, destroyed = false;
    let snapshot = {}, approvals = [];
    const t0 = performance.now();
    const agents = {};
    let flickerUntil = 0, nextFlicker = 4;
    let light = null;                    // theme lighting (theme.json "room"), see setLighting

    function setLighting(room) {
      light = room && room.tint ? room : null;
      kick();
    }
    const onTheme = (e) => setLighting(e.detail && e.detail.manifest && e.detail.manifest.room);
    window.addEventListener('lumi-theme', onTheme);
    if (window.LumiThemes && window.LumiThemes.manifest()) setLighting(window.LumiThemes.manifest().room);

    function makeAgent(id) {
      const st = A.meta.agents[id];
      const spot = st.rest;
      return {
        id, st, spot, target: spot, path: [],
        col: spot.col, row: spot.row, x: spot.col * 16 + 8, y: spot.row * 16 + 8,
        dir: spot.face, walking: false, frame: 0, frameT: 0,
        status: 'idle', vs: 'sitting', task: '', progress: 0,
        nextBlink: 1 + Math.random() * 4, blinkUntil: 0, doneUntil: 0, headY: 0,
      };
    }

    function resize() {
      const cw = wrapper.clientWidth || W * 2;
      const dpr = window.devicePixelRatio || 1;
      scale = Math.max(1, Math.round((cw * dpr) / W));
      canvas.width = W * scale; canvas.height = H * scale;
      canvas.style.width = cw + 'px'; canvas.style.height = Math.round(cw * H / W) + 'px';
      cssScale = cw / W;
      kick();
    }

    // ── state ────────────────────────────────────────────────────
    function busyOf(a) { return a.status !== 'idle'; }

    function retarget(a) {
      const want = busyOf(a) ? a.st.work : a.st.rest;
      if (want === a.target) return;
      a.target = want;
      a.path = bfs(A.meta.walk, { col: a.col, row: a.row }, want);
      a.walking = a.path.length > 0;
      a.frame = 0; a.frameT = 0;
      if (!a.walking) { a.spot = want; a.dir = want.face; }     // already there (Main rests at her desk)
    }

    function step(dt, now) {
      const speed = A.meta.timing.walk_px_s;
      for (const id of ORDER) {
        const a = agents[id];
        if (a.walking) {
          const next = a.path[0];
          const tx = next.col * 16 + 8, ty = next.row * 16 + 8;
          const dx = tx - a.x, dy = ty - a.y, d = Math.hypot(dx, dy);
          if (Math.abs(dx) > Math.abs(dy)) a.dir = dx > 0 ? 'right' : 'left';
          else if (d > 0) a.dir = dy > 0 ? 'down' : 'up';
          const mv = speed * dt;
          if (d <= mv) {
            a.x = tx; a.y = ty; a.col = next.col; a.row = next.row; a.path.shift();
            if (!a.path.length) { a.walking = false; a.dir = a.target.face; a.spot = a.target; }
          } else { a.x += dx / d * mv; a.y += dy / d * mv; }
          a.frameT += dt;
          if (a.frameT >= A.meta.timing.walk_frame_s) { a.frameT = 0; a.frame = (a.frame + 1) % 4; }
        } else {
          a.frameT += dt;
          if (a.frameT >= A.meta.timing.type_frame_s) { a.frameT = 0; a.frame = (a.frame + 1) % 2; }
        }
        if (now >= a.nextBlink) { a.blinkUntil = now + BLINK_S; a.nextBlink = now + 2.5 + Math.random() * 4; }
      }
      if (now >= nextFlicker) { flickerUntil = now + 0.12; nextFlicker = now + 5 + Math.random() * 8; }
    }

    function working(a) {
      return !a.walking && busyOf(a) && a.spot === a.st.work && a.status !== 'waiting_approval' && a.status !== 'error';
    }

    function frameOf(a) {
      const F = A.meta.frame;
      if (a.walking) return F.walk[a.frame % 4];
      if (working(a)) return (a.vs === 'reading' || READING_RE.test(a.task)) ? F.read[a.frame % 2] : F.type[a.frame % 2];
      return a.spot.sit ? F.seated : F.walk[1];
    }

    function animating(now) {
      return ORDER.some((id) => { const a = agents[id]; return a.walking || working(a) || a.status === 'error'; })
        || approvals.length > 0 || ORDER.some((id) => agents[id].doneUntil > now);
    }

    // ── drawing ──────────────────────────────────────────────────
    function drawAtlas(c, idx, x, y, flip) {
      const [sx, sy, w, h] = A.meta.rects[idx];
      if (!flip) { c.drawImage(A.atlas, sx, sy, w, h, x, y, w, h); return; }
      c.save(); c.translate(x + w, y); c.scale(-1, 1); c.drawImage(A.atlas, sx, sy, w, h, 0, 0, w, h); c.restore();
    }

    function drawAgent(c, a, now) {
      const F = A.meta.frame;
      const f = frameOf(a);
      const rowIdx = F.rows[a.dir] ?? 0;
      const sheet = now < a.blinkUntil ? A.chars[a.id].blink : A.chars[a.id].open;
      const sit = !a.walking && a.spot.sit ? A.meta.timing.sit_offset : 0;
      const dx = Math.round(a.x - 8), dy = Math.round(a.y + sit - 32);
      const flip = a.dir === 'left';
      const alarm = a.status === 'error' && Math.floor(now * 4) % 2 === 0;
      if (alarm) {                                    // flash red: tint the frame on a scratch canvas
        tctx.clearRect(0, 0, 16, 32);
        tctx.globalCompositeOperation = 'source-over';
        tctx.drawImage(sheet, f * 16, rowIdx * 32, 16, 32, 0, 0, 16, 32);
        tctx.globalCompositeOperation = 'source-atop';
        tctx.fillStyle = 'rgba(255,45,70,0.55)';
        tctx.fillRect(0, 0, 16, 32);
      }
      const src = alarm ? tint : sheet, sx = alarm ? 0 : f * 16, sy = alarm ? 0 : rowIdx * 32;
      if (flip) { c.save(); c.translate(dx + 16, dy); c.scale(-1, 1); c.drawImage(src, sx, sy, 16, 32, 0, 0, 16, 32); c.restore(); }
      else c.drawImage(src, sx, sy, 16, 32, dx, dy, 16, 32);
      a.headY = dy;
    }

    function render(now) {
      const M = A.meta;
      bctx.clearRect(0, 0, W, H);
      bctx.drawImage(A.floor, 0, 0);
      const screensOn = new Set();
      for (const id of ORDER) if (working(agents[id])) for (const i of agents[id].st.screens) screensOn.add(i);
      const items = [];
      M.drawables.forEach((d, i) => items.push({ z: d.z, d, i }));
      for (const id of ORDER) items.push({ z: agents[id].y + 8.5, a: agents[id] });
      items.sort((p, q) => p.z - q.z);
      for (const it of items) {
        if (it.a) { drawAgent(bctx, it.a, now); continue; }
        const d = it.d;
        let idx = d.a;
        if (d.on && screensOn.has(it.i)) idx = d.on[Math.floor(now / 0.3) % d.on.length];
        else if (d.flicker) idx = now < flickerUntil ? d.flicker[2] : d.flicker[0];
        else if (d.frames && d.fps) idx = d.frames[Math.floor(now * d.fps) % d.frames.length];
        drawAtlas(bctx, idx, d.x, d.y, d.flip);
      }
      // the theme's light over the room (Sakura washes it pink, Cyberpunk cyan)
      if (light) {
        const alpha = light.alpha ?? 0.15;
        bctx.save();
        bctx.globalCompositeOperation = light.blend || 'soft-light';
        bctx.globalAlpha = alpha;
        bctx.fillStyle = light.tint;
        bctx.fillRect(0, 0, W, H);
        if (light.glow) {
          const g = bctx.createRadialGradient(W / 2, 0, 8, W / 2, 0, W * 0.75);
          g.addColorStop(0, light.glow); g.addColorStop(1, 'rgba(0,0,0,0)');
          bctx.globalCompositeOperation = 'screen';
          bctx.globalAlpha = alpha * 0.9;
          bctx.fillStyle = g;
          bctx.fillRect(0, 0, W, H);
        }
        bctx.restore();
      }
      // bubbles over everything (Pixel Agents' permission "..." and done check)
      for (const id of ORDER) {
        const a = agents[id];
        const b = a.status === 'waiting_approval' ? A.bubblePerm : a.doneUntil > now ? A.bubbleDone : null;
        if (!b) continue;
        const bob = a.status === 'waiting_approval' ? Math.floor(now * 2) % 2 : 0;
        bctx.globalAlpha = a.doneUntil > now && a.status !== 'waiting_approval' ? Math.min(1, (a.doneUntil - now) / 0.5) : 1;
        bctx.drawImage(b, Math.round(a.x - b.width / 2), Math.round(a.headY - b.height + 3 - bob));
        bctx.globalAlpha = 1;
      }
      ctx.imageSmoothingEnabled = false;
      ctx.drawImage(buf, 0, 0, W * scale, H * scale);
      // names + progress at native resolution
      const fs = Math.max(10, Math.round(3.6 * scale));
      ctx.font = `700 ${fs}px ui-monospace, Consolas, monospace`;
      ctx.textAlign = 'center'; ctx.textBaseline = 'top';
      for (const id of ORDER) {
        const a = agents[id];
        const sit = !a.walking && a.spot.sit ? A.meta.timing.sit_offset : 0;
        const x = a.x * scale, y = (a.y + sit + 1) * scale;
        ctx.fillStyle = 'rgba(8,8,16,0.75)'; ctx.fillText(NAMES[id], x + 1, y + 1);
        ctx.fillStyle = a.st.accent; ctx.fillText(NAMES[id], x, y);
        if (busyOf(a) && a.progress > 0) {
          const bw = 14 * scale, bh = Math.max(2, Math.round(scale * 0.7)), by = y + fs + 2;
          ctx.fillStyle = 'rgba(255,255,255,0.15)'; ctx.fillRect(x - bw / 2, by, bw, bh);
          ctx.fillStyle = a.st.accent; ctx.fillRect(x - bw / 2, by, bw * Math.min(1, a.progress / 100), bh);
        }
      }
      placeOverlay();
    }

    function loop(nowMs) {
      raf = 0; timer = 0;
      if (destroyed || !visible || document.hidden || !A) return;
      const now = (nowMs - t0) / 1000;
      const dt = last ? Math.min(0.1, now - last) : 0;
      last = now;
      step(dt, now);
      render(now);
      const wait = animating(now) ? FAST_MS : SLOW_MS;
      timer = setTimeout(() => { raf = requestAnimationFrame(loop); }, Math.max(0, wait - 16));
    }

    function kick() {
      if (destroyed || raf || !visible || document.hidden || !A) return;
      clearTimeout(timer); timer = 0;
      last = 0;
      raf = requestAnimationFrame(loop);
    }

    // ── approval overlay (HTML, over the waiting Lumi's head) ─────
    const cards = new Map();
    function placeOverlay() {
      if (!A) return;
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
            `${NAMES[id]} wants: ${first.tool}${list.length > 1 ? ` (+${list.length - 1})` : ''}`;
          el.title = (first.preview || '').slice(0, 600);
        }
        const a = agents[id];
        const left = Math.round(a.x * cssScale) + 'px', top = Math.round((a.headY - 14) * cssScale) + 'px';
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

    // ── public API ──────────────────────────────────────────────
    function apply() {
      if (!A) return;
      const now = (performance.now() - t0) / 1000;
      for (const id of ORDER) {
        const a = agents[id];
        const s = snapshot[id];
        if (!s) continue;
        const was = a.status;
        a.status = s.status || 'idle';
        a.vs = s.visual_state || '';
        a.task = s.task || '';
        a.progress = Number(s.progress) || 0;
        if (was !== 'idle' && was !== 'error' && a.status === 'idle') a.doneUntil = now + DONE_BUBBLE_S;
        retarget(a);
      }
      kick();
    }
    function update(snap) { snapshot = snap || {}; apply(); }
    function setApprovals(list) { approvals = (list || []).filter((ap) => ap && ap.id); placeOverlay(); kick(); }

    loadAssets(base).then((assets) => {
      if (destroyed) return;
      A = assets;
      W = A.meta.W; H = A.meta.H;
      buf = document.createElement('canvas'); buf.width = W; buf.height = H;
      bctx = buf.getContext('2d');
      tint = document.createElement('canvas'); tint.width = 16; tint.height = 32;
      tctx = tint.getContext('2d');
      for (const id of ORDER) agents[id] = makeAgent(id);
      for (const id of ORDER) {                     // first data: appear in place, no walk-in
        const s = snapshot[id];
        if (s && s.status && s.status !== 'idle') {
          const a = agents[id]; const w = a.st.work;
          Object.assign(a, { status: s.status, target: w, spot: w, col: w.col, row: w.row,
                             x: w.col * 16 + 8, y: w.row * 16 + 8, dir: w.face });
        }
      }
      resize();
      apply();
    }).catch((e) => {
      const note = document.createElement('div');
      note.className = 'lumi-pl-error';
      note.textContent = `Pipeline art didn't load (${e.message}). Run scripts/build_pixel_office.py.`;
      overlay.appendChild(note);
      console.error('[pipeline]', e);
    });

    const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(resize) : null;
    if (ro) ro.observe(wrapper); else window.addEventListener('resize', resize);
    const io = typeof IntersectionObserver !== 'undefined'
      ? new IntersectionObserver((es) => { visible = es.some((e) => e.isIntersecting); if (visible) kick(); })
      : null;
    if (io) io.observe(canvas);
    const onVis = () => { if (!document.hidden) kick(); };
    document.addEventListener('visibilitychange', onVis);

    return {
      update, setApprovals, setLighting,
      destroy() {
        destroyed = true;
        cancelAnimationFrame(raf); clearTimeout(timer);
        if (ro) ro.disconnect(); else window.removeEventListener('resize', resize);
        if (io) io.disconnect();
        document.removeEventListener('visibilitychange', onVis);
        window.removeEventListener('lumi-theme', onTheme);
        overlay.textContent = '';
      },
      get _agents() { return agents; },              // for tests
      get _ready() { return !!A; },
    };
  }

  window.LumiPipeline = { mount, bfs, ORDER };
})();

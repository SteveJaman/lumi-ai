/*
 * Lumi Pipeline: her agents at work in her cyberpunk room, Sims-style.
 *
 * Shared by the web app (web/pipeline.js) and Lumi Island (island/src/pipeline.js);
 * scripts/sync_themes.py copies this file (and pixel-office/lumi-sprites.js) over.
 * The art lives in pixel-office/ next to this file and is baked by
 * scripts/build_pixel_office.py (+ scripts/lumi_hd.py): the room from Pixel Agents'
 * tiles and furniture (MIT, github.com/pixel-agents-hq/pixel-agents), and four 48x96
 * Lumis after her character sheet, with her activity poses. pixel-office/lumi-sprites.js
 * maps every pose to an optional hand-drawn PNG (see there for the format and fallbacks).
 *
 *   const pl = LumiPipeline.mount(wrapperEl, {
 *     base: '/web/pixel-office/',                 // where the baked art is
 *     onDecision: (id, 'approve' | 'decline') => {},
 *   });
 *   pl.update(agents);      // agent_update payload: {main: {status, visual_state, task, progress, model, model_name}, ...}
 *   pl.setApprovals(list);  // [{id, tool, agent, preview}]
 *   pl.setMood(mood);       // {valence, arousal, frustration, curiosity, boredom}; normally from
 *                           // the backend's mood_update (window.lumiMood + the 'lumi-mood' event)
 *   pl.destroy();
 *
 * How it plays: a busy agent walks (BFS over the room's walk grid) to its station
 * and types, or reads while it searches; an idle one goes back to its seat. Its
 * monitors switch on while it works. An approval puts the permission bubble over its
 * head and Approve / Decline buttons above it; a finished task shows the green check
 * for 2 s; an error flashes it red.
 *
 * Ambient life (the Sims part): idle for IDLE_BEFORE_AMBIENT_S, a Lumi does things on
 * her own (BEHAVIOR_WEIGHTS): strolls across the room, looks around, stretches, gets
 * coffee and drinks it at her seat, tidies her desk, looks out of the window, checks the
 * server racks; naps slumped at her desk at night (22:00-06:00, after 2 min idle), has
 * a rice ball at lunch (12:00-14:00). A thought bubble shows what she's having, and her
 * halo glows like a plumbob: green when Lumi's mood is good, amber, red when she's
 * frustrated. Mood also sets the pace and the choices. The backend always wins: a new
 * task interrupts whatever she's doing. Ambient time only runs while the canvas is
 * drawn, so it stops while the page is hidden.
 *
 * Click a Lumi for a card: status, task, model and tier, time in this state, her last
 * 5 changes (kept here, not on the server). Click elsewhere or Esc to close.
 *
 * Scale: the 320x192 room is laid out at 2x (a 640x384 "buffer" space); Lumis are 48x96
 * there (a tile and a half wide, three tall). Rendering is crisp and cached, after Pixel
 * Agents' sprite cache: every sprite frame, atlas piece, the floor and the bubbles are
 * scaled ONCE to the current zoom (nearest neighbour, imageSmoothingEnabled = false)
 * into offscreen canvases, and each frame only copies them to whole device pixels. The
 * cache is rebuilt when the zoom changes (a resize). The zoom snaps to a whole number of
 * device pixels per art pixel when one is close. It never drops below MIN_CSS_SCALE:
 * on a narrow canvas (the island) the view is cropped instead and a camera follows
 * whoever is busy or selected (drag to pan). Names, buttons and the card are at native
 * resolution. 30 fps while anyone walks or works, about 8 fps otherwise, nothing while
 * hidden.
 */
(function () {
  'use strict';

  const ORDER = ['main', 'minecraft', 'office', 'system'];
  const NAMES = { main: 'Main', minecraft: 'Minecraft', office: 'Office', system: 'System' };
  const STATUS_LABEL = { idle: 'Idle', thinking: 'Thinking', executing: 'Executing', waiting_approval: 'Needs you', error: 'Error' };
  const READING_RE = /web_search|browse|research|search_files|read_file|read_window|reading|searching|triag|recall|library/i;
  const FAST_MS = 33, SLOW_MS = 120;
  const BLINK_S = 0.15;
  const DONE_BUBBLE_S = 2;
  const HISTORY = 5;
  const RS = 2;                                 // room laid out at 2x
  const MIN_CSS_SCALE = 0.95;                   // css px per art px: never smaller (the camera crops instead)
  const SNAP = 0.18;                            // snap the zoom to a whole number of device px within this

  // ── ambient behaviour ─────────────────────────────────────────
  const IDLE_BEFORE_AMBIENT_S = 8;
  const SLEEP_AFTER_IDLE_S = 120;              // at night
  const NIGHT = [22, 6], LUNCH = [12, 14];      // local hours [from, to)
  const MAX_WALK_PX_S = 64;                     // room px/s; mood can speed her up, never to a run
  // Relative odds of each idle behaviour, per agent (before mood). Sleep and lunch
  // aren't in here: they depend on the clock (see pickBehavior).
  const BEHAVIOR_WEIGHTS = {
    main:      { deskwork: 5, look: 3, stretch: 2, drink: 2, wander: 1.5, window: 0.5, rack: 0 },
    minecraft: { wander: 5, window: 4, look: 2, stretch: 1, drink: 1, deskwork: 0.5, rack: 0 },
    office:    { deskwork: 5, drink: 2, stretch: 2, look: 1, wander: 1, window: 0.5, rack: 0 },
    system:    { rack: 5, look: 2, stretch: 1.5, drink: 1, wander: 0.5, deskwork: 0, window: 0 },
  };
  const LUNCH_WEIGHT = 6;                       // added once per lunch window per agent
  const AMBIENT_LABEL = {
    wander: 'strolling around', look: 'looking around', stretch: 'stretching', drink: 'having coffee',
    deskwork: 'tidying up her desk', window: 'looking out the window', rack: 'watching the servers',
    eat: 'having a rice ball', sleep: 'asleep at her desk',
  };

  const rand = (a, b) => a + Math.random() * (b - a);
  const inHours = ([from, to], h) => (from <= to ? h >= from && h < to : h >= from || h < to);
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

  let assetsPromise = null;
  function loadAssets(base) {
    if (assetsPromise) return assetsPromise;
    const img = (src) => new Promise((resolve, reject) => {
      const i = new Image();
      i.onload = () => resolve(i);
      i.onerror = () => reject(new Error(`couldn't load ${src}`));
      i.src = base + src;
    });
    const optional = (src) => img(src).catch(() => null);
    const script = (file, ready) => (ready() ? Promise.resolve(ready()) : new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = base + file;
      s.onload = () => (ready() ? resolve(ready()) : reject(new Error(`${file} is empty`)));
      s.onerror = () => reject(new Error(`couldn't load ${base}${file}`));
      document.head.appendChild(s);
    }));
    assetsPromise = Promise.all([
      script('office.js', () => window.LUMI_OFFICE), script('lumi-sprites.js', () => window.LumiSprites),
      img('floor.png'), img('atlas.png'), img('bubble-permission.png'), img('bubble-waiting.png'),
      ...ORDER.flatMap((a) => [optional(`chars/${a}.png`), optional(`chars/${a}_blink.png`)]),
    ]).then(async ([meta, sprites, floor, atlas, bubblePerm, bubbleDone, ...sheets]) => {
      const chars = {};
      const loaded = await Promise.all(ORDER.map((a, i) =>
        sprites.loadAgent(base, a, { open: sheets[i * 2], blink: sheets[i * 2 + 1] })));
      ORDER.forEach((a, i) => { chars[a] = loaded[i]; });
      const missing = loaded.flatMap((c) => c.missing);
      const fake = ORDER.filter((a) => chars[a].placeholder);
      if (fake.length) console.warn(`[pipeline] no baked sheet for ${fake.join(', ')}: drawing placeholders. Run scripts/build_pixel_office.py.`);
      if (missing.length) {
        console.info(`[pipeline] ${missing.length} poses use the baked sheet (drop PNGs in ${base}${sprites.CUSTOM_DIR} to replace them): `
          + missing.join(', '));
      }
      return { meta, sprites, floor, atlas, bubblePerm, bubbleDone, chars };
    });
    assetsPromise.catch(() => { assetsPromise = null; });
    return assetsPromise;
  }

  // ── pathfinding over the walk grid (4-way BFS, like Pixel Agents' tileMap.ts) ──
  // `blocked` (optional): "col,row" keys to keep off (other Lumis' seats).
  function bfs(walk, from, to, blocked) {
    const rows = walk.length, cols = walk[0].length;
    const ok = (c, r) => r >= 0 && r < rows && c >= 0 && c < cols
      && ((walk[r][c] === '1' && !(blocked && blocked.has(`${c},${r}`))) || (c === to.col && r === to.row));
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

  // Mood -> how she moves, what she picks, and her halo's colour. Neutral without a mood.
  function moodFactors(m) {
    const num = (k, d) => (m && Number.isFinite(+m[k]) ? +m[k] : d);
    const valence = clamp(num('valence', 0.2), -1, 1);
    const arousal = clamp(num('arousal', 0.5), 0, 1);
    const frustration = clamp(num('frustration', 0), 0, 1);
    const boredom = clamp(num('boredom', 0), 0, 1);
    const plumbob = !m ? null
      : frustration > 0.55 || valence < -0.35 ? '#ff5a6e'
      : frustration > 0.3 || valence < 0.05 ? '#ffc46b'
      : '#5cf28a';
    return {
      speed: clamp(0.75 + 0.5 * arousal + 0.35 * frustration, 0.6, 1.4),   // pace
      restless: 1 + 2 * frustration,                                       // more pacing when frustrated
      curious: 1 + boredom,                                                // looks about when bored
      sleepy: arousal < 0.3 ? (0.3 - arousal) * 20 : 0,                    // naps by day when drained
      plumbob,
    };
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
    let W = 640, H = 384, CW = 48, CH = 96, tint = null, tctx = null;
    let k = 1, cssScale = 1, viewW = 640, raf = 0, timer = 0, last = 0, visible = true, destroyed = false;
    let camX = 0, camTarget = 0, dragFrom = null, manualUntil = 0;
    const cache = new Map();             // "<key>" -> offscreen canvas pre-scaled at k
    let snapshot = {}, approvals = [];
    let sim = 0;                         // ambient clock: only runs while drawn
    let mood = null, mf = moodFactors(null);
    const t0 = performance.now();
    const agents = {};
    let flickerUntil = 0, nextFlicker = 4;
    let light = null;                    // theme lighting (theme.json "room"), see setLighting
    let inspected = null, inspectEl = null, inspectAt = 0;

    function setLighting(room) {
      light = room && room.tint ? room : null;
      kick();
    }
    const onTheme = (e) => setLighting(e.detail && e.detail.manifest && e.detail.manifest.room);
    window.addEventListener('lumi-theme', onTheme);
    if (window.LumiThemes && window.LumiThemes.manifest()) setLighting(window.LumiThemes.manifest().room);

    function setMood(m) { mood = m && typeof m === 'object' ? m : null; mf = moodFactors(mood); kick(); }
    const onMood = (e) => setMood(e.detail);
    window.addEventListener('lumi-mood', onMood);
    if (window.lumiMood) setMood(window.lumiMood);     // the last mood_update this page heard

    function makeAgent(id) {
      const st = A.meta.agents[id];
      const spot = st.rest;
      return {
        id, st, spot, target: spot, path: [],
        col: spot.col, row: spot.row, x: spot.col * 16 + 8, y: spot.row * 16 + 8,
        dir: spot.face, walking: false, frame: 0, frameT: 0,
        status: 'idle', vs: 'sitting', task: '', progress: 0, model: null, modelName: null,
        nextBlink: 1 + Math.random() * 4, blinkUntil: 0, doneUntil: 0, headY: 0,
        idleSince: 0, beh: null, nextBeh: IDLE_BEFORE_AMBIENT_S + Math.random() * 6, lunchDay: '',
        since: Date.now(), history: [],
      };
    }

    // Zoom: device px per art (buffer) px. Fills the width when that's big enough; otherwise
    // MIN_CSS_SCALE with a camera over part of the room. Snapped to a whole number when close.
    function resize() {
      const cw = wrapper.clientWidth || W;
      const dpr = window.devicePixelRatio || 1;
      let css = Math.max(cw / W, MIN_CSS_SCALE);
      let kk = css * dpr;
      const whole = Math.round(kk);
      if (whole >= 1 && Math.abs(kk - whole) <= SNAP) kk = whole;
      css = kk / dpr;
      viewW = Math.min(W, cw / css);
      canvas.width = Math.round(cw * dpr); canvas.height = Math.round(H * kk);
      canvas.style.width = cw + 'px'; canvas.style.height = Math.round(H * css) + 'px';
      if (kk !== k) cache.clear();
      k = kk; cssScale = css;
      camX = Math.min(camX, W - viewW);
      kick();
    }

    // The sprite cache: an image region scaled once to the zoom, nearest neighbour.
    function scaled(key, img, sx, sy, sw, sh, factor) {
      const id = key;
      let c = cache.get(id);
      if (c) return c;
      c = document.createElement('canvas');
      c.width = Math.max(1, Math.round(sw * factor * k));
      c.height = Math.max(1, Math.round(sh * factor * k));
      const g = c.getContext('2d');
      g.imageSmoothingEnabled = false;
      g.drawImage(img, sx, sy, sw, sh, 0, 0, c.width, c.height);
      cache.set(id, c);
      return c;
    }
    const X = (bx) => Math.round((bx - camX) * k);       // buffer -> device px
    const Y = (by) => Math.round(by * k);
    function blit(c, bx, by, flip) {
      if (!flip) { ctx.drawImage(c, X(bx), Y(by)); return; }
      ctx.save(); ctx.translate(X(bx) + c.width, Y(by)); ctx.scale(-1, 1); ctx.drawImage(c, 0, 0); ctx.restore();
    }
    // Vector extras (glow, bubbles, z z z) draw in buffer coordinates through this.
    const inBuffer = () => ctx.setTransform(k, 0, 0, k, -camX * k, 0);
    const inDevice = () => ctx.setTransform(1, 0, 0, 1, 0, 0);

    // Camera: centred on the selected Lumi, else the busy ones, else everyone; drag to pan.
    function aimCamera(dt) {
      if (viewW >= W - 0.5) { camX = 0; return false; }
      if (performance.now() >= manualUntil) {
        const pick = inspected ? [agents[inspected]]
          : ORDER.map((id) => agents[id]).filter((a) => busyOf(a));
        const list = pick.length ? pick : ORDER.map((id) => agents[id]);
        const fx = list.reduce((s, a) => s + a.x * RS, 0) / list.length;
        camTarget = clamp(fx - viewW / 2, 0, W - viewW);
      }
      const before = camX;
      camX += (camTarget - camX) * Math.min(1, dt * 3);
      if (Math.abs(camTarget - camX) < 0.3) camX = camTarget;
      return Math.abs(camX - before) > 0.05;
    }

    // ── moving ───────────────────────────────────────────────────
    function busyOf(a) { return a.status !== 'idle'; }
    const tileKey = (t) => `${t.col},${t.row}`;
    // Other Lumis' seats and stations, and where the others stand or are headed.
    function othersBlocked(a) {
      const s = new Set();
      for (const id of ORDER) {
        if (id === a.id || !agents[id]) continue;
        const o = agents[id];
        s.add(tileKey(o.st.work)); s.add(tileKey(o.st.rest)); s.add(`${o.col},${o.row}`); s.add(tileKey(o.target));
      }
      return s;
    }
    function goTo(a, spot) {
      a.target = spot;
      a.path = bfs(A.meta.walk, { col: a.col, row: a.row }, spot, othersBlocked(a));
      a.walking = a.path.length > 0;
      a.frame = 0; a.frameT = 0;
      if (!a.walking) { a.spot = spot; a.dir = spot.face || a.dir; }
    }

    function retarget(a) {
      const want = busyOf(a) ? a.st.work : a.st.rest;
      if (want === a.target) return;
      goTo(a, want);
    }

    function step(dt, now) {
      const speed = Math.min(MAX_WALK_PX_S, A.meta.timing.walk_px_s * mf.speed);
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
            if (!a.path.length) { a.walking = false; a.dir = a.target.face || a.dir; a.spot = a.target; }
          } else { a.x += dx / d * mv; a.y += dy / d * mv; }
          a.frameT += dt;
          if (a.frameT >= A.meta.timing.walk_frame_s / mf.speed) { a.frameT = 0; a.frame = (a.frame + 1) % 64; }
        } else {
          a.frameT += dt;
          const ps = poseInfo(a);
          if (a.frameT >= (ps.frameS || A.meta.timing.type_frame_s)) { a.frameT = 0; a.frame = (a.frame + 1) % 64; }
        }
        if (now >= a.nextBlink) { a.blinkUntil = now + BLINK_S; a.nextBlink = now + 2.5 + Math.random() * 4; }
        ambient(a, dt);
      }
      if (now >= nextFlicker) { flickerUntil = now + 0.12; nextFlicker = now + 5 + Math.random() * 8; }
    }

    // ── ambient behaviour ────────────────────────────────────────
    // A behaviour is a short script of steps: {go: spot} | {pose, dur, look?}.
    function spotsOf(kind) { return (A.meta.spots && A.meta.spots[kind]) || []; }
    function freeSpot(a, kind) {
      const taken = othersBlocked(a);
      const list = spotsOf(kind).filter((s) => !taken.has(tileKey(s)));
      return list.length ? list[Math.floor(Math.random() * list.length)] : null;
    }
    // Anywhere on the floor at least 4 tiles away, so a stroll crosses the room.
    function wanderTile(a) {
      const walk = A.meta.walk, taken = othersBlocked(a);
      const options = [];
      for (let r = 0; r < walk.length; r++) for (let c = 0; c < walk[0].length; c++) {
        if (walk[r][c] !== '1' || taken.has(`${c},${r}`)) continue;
        if (Math.abs(c - a.col) + Math.abs(r - a.row) >= 4) options.push({ col: c, row: r });
      }
      if (!options.length) return null;
      const t = options[Math.floor(Math.random() * options.length)];
      return { col: t.col, row: t.row, face: ['down', 'left', 'right', 'up'][Math.floor(Math.random() * 4)], sit: false };
    }
    const deskOf = (a) => (a.st.work.sit ? a.st.work : a.st.rest);
    const pause = (lo, hi) => rand(lo, hi) / mf.speed;

    function plan(a, kind) {
      const rest = a.st.rest;
      switch (kind) {
        case 'wander': {
          const steps = [];
          const hops = 1 + Math.floor(Math.random() * (mf.restless > 2 ? 3 : 2));
          for (let i = 0; i < hops; i++) {
            const t = wanderTile(a);
            if (t) steps.push({ go: t }, { pose: 'idle', dur: pause(2, 5) / mf.restless * 1.5, look: Math.random() < 0.5 });
          }
          return steps.length ? [...steps, { go: rest }] : null;
        }
        case 'look':     return [{ pose: a.spot.sit ? 'sit' : 'idle', dur: pause(2.5, 4), look: true }];
        case 'stretch':  return [{ pose: 'stretch', dur: 3.2 }];
        case 'drink': {
          const cup = freeSpot(a, 'coffee');
          return cup ? [{ go: { ...cup, sit: false } }, { pose: 'idle', dur: 1.5 }, { go: rest }, { pose: 'drink', dur: pause(7, 11) }] : null;
        }
        case 'deskwork': {
          const desk = deskOf(a);
          return [{ go: desk }, { pose: 'type', dur: pause(6, 12) }, ...(desk === rest ? [] : [{ go: rest }])];
        }
        case 'window': {
          const w = freeSpot(a, 'window');
          return w ? [{ go: { ...w, sit: false } }, { pose: 'idle', dur: pause(5, 9) }, { go: rest }] : null;
        }
        case 'rack': {
          const k = freeSpot(a, 'rack') || (a.st.work.sit ? null : a.st.work);
          return k ? [{ go: { ...k, sit: false } }, { pose: 'idle', dur: pause(8, 14) }, { go: rest }] : null;
        }
        case 'eat': {
          const desk = deskOf(a);
          return [{ go: desk }, { pose: 'eat', dur: pause(10, 14) }, ...(desk === rest ? [] : [{ go: rest }])];
        }
        case 'sleep':    return [{ go: deskOf(a) }, { pose: 'sleep', dur: Infinity }];
      }
      return null;
    }

    function pickBehavior(a, idleFor) {
      const hour = new Date().getHours();
      if ((inHours(NIGHT, hour) && idleFor > SLEEP_AFTER_IDLE_S) || (mf.sleepy > 2 && idleFor > 60 && Math.random() < 0.3)) {
        return 'sleep';
      }
      const w = { ...BEHAVIOR_WEIGHTS[a.id] };
      w.wander *= mf.restless;
      w.look *= mf.curious; w.window *= mf.curious;
      const today = new Date().toDateString();
      if (inHours(LUNCH, hour) && a.lunchDay !== today) w.eat = LUNCH_WEIGHT;
      const kinds = Object.keys(w).filter((k) => w[k] > 0);
      let r = Math.random() * kinds.reduce((s, k) => s + w[k], 0);
      for (const k of kinds) { r -= w[k]; if (r <= 0) return k; }
      return kinds[kinds.length - 1];
    }

    function startBehavior(a, kind) {
      const steps = plan(a, kind);
      if (!steps) return false;
      if (kind === 'eat') a.lunchDay = new Date().toDateString();
      a.beh = { kind, steps, i: -1, until: 0, nextLook: 0, looks: 0 };
      nextStep(a);
      return true;
    }
    function nextStep(a) {
      const b = a.beh;
      b.i += 1;
      const s = b.steps[b.i];
      if (!s) { endBehavior(a); return; }
      if (s.go) goTo(a, s.go);
      else {
        b.until = sim + s.dur; b.nextLook = sim + 0.8; b.looks = 0; a.frame = 0;
        // Coffee and lunch face the room: she turns her chair round, so the mug shows.
        if ((s.pose === 'drink' || s.pose === 'eat') && a.dir === 'up') a.dir = 'down';
      }
    }
    function endBehavior(a) {
      a.beh = null;
      a.nextBeh = sim + rand(4, 10) / mf.speed;
      if (a.target !== a.st.rest && !busyOf(a)) goTo(a, a.st.rest);
      else if (!a.walking) a.dir = a.spot.face || a.dir;
    }
    function cancelBehavior(a) { a.beh = null; }

    function ambient(a, dt) {
      if (busyOf(a) || a.doneUntil > (performance.now() - t0) / 1000) return;
      const b = a.beh;
      if (!b) {
        if (a.walking) return;
        const idleFor = sim - a.idleSince;
        if (idleFor >= IDLE_BEFORE_AMBIENT_S && sim >= a.nextBeh) {
          if (!startBehavior(a, pickBehavior(a, idleFor))) a.nextBeh = sim + 3;
        }
        return;
      }
      const s = b.steps[b.i];
      if (!s) return endBehavior(a);
      if (s.go) { if (!a.walking) nextStep(a); return; }
      if (b.kind === 'sleep') {                          // wake up with the sun (or a lift in mood)
        if (!inHours(NIGHT, new Date().getHours()) && mf.sleepy <= 2) endBehavior(a);
        return;
      }
      if (s.look && sim >= b.nextLook && b.looks < 3) {
        a.dir = ['left', 'right', 'down', 'left'][(b.looks + Math.floor(Math.random() * 2)) % 4];
        b.looks += 1; b.nextLook = sim + rand(0.7, 1.2);
      }
      if (sim >= b.until) { if (s.look) a.dir = a.spot.face || a.dir; nextStep(a); }
    }

    // ── poses ────────────────────────────────────────────────────
    function working(a) {
      return !a.walking && busyOf(a) && a.spot === a.st.work && a.status !== 'waiting_approval' && a.status !== 'error';
    }
    function poseName(a) {
      if (a.walking) return 'walk';
      if (working(a)) return (a.vs === 'reading' || READING_RE.test(a.task)) ? 'read' : 'type';
      const s = a.beh && a.beh.steps[a.beh.i];
      if (s && s.pose && !busyOf(a)) return s.pose;
      return a.spot.sit ? 'sit' : 'idle';
    }
    function poseInfo(a) { return A.chars[a.id].poses[poseName(a)]; }
    const SEATED = new Set(['sit', 'type', 'read', 'drink', 'eat', 'sleep']);
    const sitsNow = (a) => !a.walking && a.spot.sit && SEATED.has(poseName(a));

    function animating(now) {
      return ORDER.some((id) => { const a = agents[id]; return a.walking || working(a) || a.status === 'error'; })
        || approvals.length > 0 || ORDER.some((id) => agents[id].doneUntil > now);
    }

    // ── drawing ──────────────────────────────────────────────────
    function drawAtlas(c, idx, x, y, flip) {
      const [sx, sy, w, h] = A.meta.rects[idx];
      blit(scaled(`atlas:${idx}`, A.atlas, sx, sy, w, h, RS), x * RS, y * RS, flip);
    }

    // Where her feet are, in buffer pixels.
    const feet = (a) => [Math.round(a.x * RS), Math.round((a.y + (sitsNow(a) ? A.meta.timing.sit_offset : 0)) * RS)];

    function drawShadow(c, a) {
      const [fx, fy] = feet(a);
      c.save();
      inBuffer();
      c.globalAlpha = 0.35;
      c.fillStyle = '#05060c';
      c.beginPath(); c.ellipse(fx, fy - 2, 13, 4, 0, 0, Math.PI * 2); c.fill();
      c.restore();
    }

    function drawAgent(c, a, now) {
      const F = A.meta.frame;
      const name = poseName(a);
      const pose = A.chars[a.id].poses[name];
      const col = pose.cols[a.frame % pose.cols.length];
      const rowIdx = F.rows[a.dir] ?? 0;
      const sheet = pose.custom ? pose.img : (now < a.blinkUntil && name !== 'sleep' ? A.chars[a.id].blink : A.chars[a.id].open);
      const [fx, fy] = feet(a);
      const dx = fx - CW / 2, top = fy - CH;
      const flip = a.dir === 'left';
      if (!sitsNow(a)) drawShadow(c, a);
      const alarm = a.status === 'error' && Math.floor(now * 4) % 2 === 0;
      if (alarm) {                                    // flash red: tint the frame on a scratch canvas
        tctx.clearRect(0, 0, CW, CH);
        tctx.globalCompositeOperation = 'source-over';
        tctx.drawImage(sheet, col * CW, rowIdx * CH, CW, CH, 0, 0, CW, CH);
        tctx.globalCompositeOperation = 'source-atop';
        tctx.fillStyle = 'rgba(255,45,70,0.55)';
        tctx.fillRect(0, 0, CW, CH);
      }
      const which = pose.custom ? `custom:${name}` : (sheet === A.chars[a.id].blink ? 'blink' : 'open');
      const frame = alarm
        ? scaled(`alarm:${a.id}:${name}:${col}:${rowIdx}:${which}`, tint, 0, 0, CW, CH, 1)
        : scaled(`${a.id}:${which}:${col}:${rowIdx}`, sheet, col * CW, rowIdx * CH, CW, CH, 1);
      if (alarm) cache.delete(`alarm:${a.id}:${name}:${col}:${rowIdx}:${which}`);   // tint changes: don't keep
      blit(frame, dx, top, flip);
      a.headY = top;
      drawHaloGlow(c, a, dx, top, now, name);
    }

    // Her halo lights up like a plumbob: her accent, or Lumi's mood colour when known.
    function drawHaloGlow(c, a, dx, top, now, name) {
      const hy = top + (name === 'sleep' ? 11 : 4);
      const glow = 0.28 + 0.14 * Math.sin(now * 2.2 + ORDER.indexOf(a.id));
      c.save();
      inBuffer();
      c.globalCompositeOperation = 'lighter';
      c.globalAlpha = glow;
      const g = c.createRadialGradient(dx + CW / 2, hy, 1, dx + CW / 2, hy, 14);
      g.addColorStop(0, mf.plumbob || a.st.accent); g.addColorStop(1, 'rgba(0,0,0,0)');
      c.fillStyle = g;
      c.fillRect(dx + CW / 2 - 14, hy - 8, 28, 16);
      c.restore();
    }

    // Over everything: the thought bubble, coffee steam, z z z.
    function drawActivity(c, a, now) {
      const name = poseName(a);
      const [fx] = feet(a);
      const top = a.headY;
      if (name === 'sleep') {
        c.save();
        inBuffer();
        c.textAlign = 'center'; c.textBaseline = 'middle';
        for (let i = 0; i < 3; i++) {
          const t = ((now * 0.3) + i / 3) % 1;
          const zx = fx + 10 + t * 16 + Math.sin(t * 6 + i) * 3, zy = top + 14 - t * 34;
          c.globalAlpha = Math.max(0, Math.sin(t * Math.PI)) * 0.95;
          c.font = `700 ${10 + Math.round(t * 8)}px ui-monospace, Consolas, monospace`;
          c.lineWidth = 3; c.strokeStyle = '#141a2c'; c.strokeText('z', zx, zy);
          c.fillStyle = '#e7eeff'; c.fillText('z', zx, zy);
        }
        c.restore();
        return;
      }
      if (name !== 'drink' && name !== 'eat') return;
      if (name === 'drink' && a.dir !== 'up') {              // steam off the mug
        c.save();
        inBuffer();
        for (let i = 0; i < 3; i++) {
          const t = ((now * 0.6) + i / 3) % 1;
          c.globalAlpha = Math.sin(t * Math.PI) * 0.7;
          c.fillStyle = '#e7f6ff';
          const sx = fx + (a.dir === 'left' ? 6 : -6) + Math.sin(t * 8 + i) * 2;
          c.fillRect(Math.round(sx), Math.round(top + 46 - t * 16), 2, 2);
        }
        c.restore();
      }
      // A Sims-style thought bubble with what she's having.
      const bx = fx + 18, by = top + 4, bob = Math.sin(now * 2) * 1.5;
      c.save();
      inBuffer();
      c.fillStyle = 'rgba(250,252,255,0.96)'; c.strokeStyle = '#141a2c'; c.lineWidth = 2;
      c.beginPath(); c.arc(bx - 8, by + 22 + bob, 2, 0, Math.PI * 2); c.fill(); c.stroke();
      c.beginPath(); c.arc(bx - 3, by + 16 + bob, 3, 0, Math.PI * 2); c.fill(); c.stroke();
      c.beginPath(); c.ellipse(bx + 8, by + bob, 13, 10, 0, 0, Math.PI * 2); c.fill(); c.stroke();
      const ix = bx + 2, iy = by - 5 + bob;
      if (name === 'drink') {                                // a teal mug
        c.fillStyle = '#141a2c'; c.fillRect(ix - 1, iy - 1, 11, 12);
        c.fillStyle = '#4fb3a8'; c.fillRect(ix, iy, 9, 10);
        c.fillStyle = '#f7f8fc'; c.fillRect(ix, iy + 4, 9, 2);
        c.fillStyle = '#6a4630'; c.fillRect(ix + 1, iy, 7, 2);
        c.fillStyle = '#141a2c'; c.fillRect(ix + 9, iy + 2, 3, 6);
        c.fillStyle = '#4fb3a8'; c.fillRect(ix + 10, iy + 3, 1, 4);
      } else {                                               // a rice ball
        c.fillStyle = '#141a2c';
        c.beginPath(); c.moveTo(ix + 5, iy - 2); c.lineTo(ix + 12, iy + 11); c.lineTo(ix - 2, iy + 11); c.closePath(); c.fill();
        c.fillStyle = '#fbfbf6';
        c.beginPath(); c.moveTo(ix + 5, iy); c.lineTo(ix + 10, iy + 10); c.lineTo(ix, iy + 10); c.closePath(); c.fill();
        c.fillStyle = '#1b2a22'; c.fillRect(ix + 2, iy + 6, 6, 4);
      }
      c.restore();
    }

    function render(now) {
      const M = A.meta;
      const bctx = ctx;
      inDevice();
      ctx.imageSmoothingEnabled = false;
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      blit(scaled('floor', A.floor, 0, 0, A.floor.width, A.floor.height, RS), 0, 0);
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
        inBuffer();
        bctx.globalCompositeOperation = light.blend || 'soft-light';
        bctx.globalAlpha = alpha;
        bctx.fillStyle = light.tint;
        bctx.fillRect(0, 0, W, H);
        if (light.glow) {
          const g = bctx.createRadialGradient(W / 2, 0, 16, W / 2, 0, W * 0.75);
          g.addColorStop(0, light.glow); g.addColorStop(1, 'rgba(0,0,0,0)');
          bctx.globalCompositeOperation = 'screen';
          bctx.globalAlpha = alpha * 0.9;
          bctx.fillStyle = g;
          bctx.fillRect(0, 0, W, H);
        }
        bctx.restore();
      }
      for (const id of ORDER) drawActivity(bctx, agents[id], now);
      // bubbles over everything (Pixel Agents' permission "..." and done check)
      for (const id of ORDER) {
        const a = agents[id];
        const b = a.status === 'waiting_approval' ? A.bubblePerm : a.doneUntil > now ? A.bubbleDone : null;
        if (!b) continue;
        const bob = a.status === 'waiting_approval' ? Math.floor(now * 2) % 2 : 0;
        bctx.globalAlpha = a.doneUntil > now && a.status !== 'waiting_approval' ? Math.min(1, (a.doneUntil - now) / 0.5) : 1;
        const [fx] = feet(a);
        const bs = scaled(b === A.bubblePerm ? 'bubble:perm' : 'bubble:done', b, 0, 0, b.width, b.height, RS);
        blit(bs, fx - b.width * RS / 2, a.headY - b.height * RS + 6 - bob * RS);
        bctx.globalAlpha = 1;
      }
      inDevice();
      // names + progress at native resolution
      const fs = Math.max(10, Math.round(7 * k));
      ctx.font = `700 ${fs}px ui-monospace, Consolas, monospace`;
      ctx.textAlign = 'center'; ctx.textBaseline = 'top';
      for (const id of ORDER) {
        const a = agents[id];
        const [fx, fy] = feet(a);
        const x = X(fx), y = Y(fy + 2);
        ctx.fillStyle = 'rgba(8,8,16,0.75)'; ctx.fillText(NAMES[id], x + 1, y + 1);
        ctx.fillStyle = a.st.accent; ctx.fillText(NAMES[id], x, y);
        if (busyOf(a) && a.progress > 0) {
          const bw = 28 * k, bh = Math.max(2, Math.round(k * 1.5)), by = y + fs + 2;
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
      sim += dt;
      step(dt, now);
      const panning = aimCamera(dt);
      render(now);
      const wait = animating(now) || panning || dragFrom ? FAST_MS : SLOW_MS;
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
        const [fx] = feet(a);
        const left = Math.round((fx - camX) * cssScale) + 'px', top = Math.round((a.headY - 20) * cssScale) + 'px';
        if (el.style.left !== left) el.style.left = left;
        if (el.style.top !== top) el.style.top = top;
      }
      placeInspect();
    }
    function decide(id, decision) {
      if (!id) return;
      approvals = approvals.filter((ap) => ap.id !== id);
      if (opts.onDecision) opts.onDecision(id, decision);
      placeOverlay();
    }

    // ── click to inspect ────────────────────────────────────────
    function hitAgent(ev) {
      if (!A) return null;
      const r = canvas.getBoundingClientRect();
      const bx = (ev.clientX - r.left) / cssScale + camX, by = (ev.clientY - r.top) / cssScale;
      let best = null;
      for (const id of ORDER) {
        const a = agents[id];
        const [fx, fy] = feet(a);
        if (bx >= fx - 16 && bx <= fx + 16 && by >= a.headY + 6 && by <= fy && (!best || a.y > best.y)) best = a;
      }
      return best;
    }
    const ago = (ms) => {
      const s = Math.max(0, Math.round(ms / 1000));
      return s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${Math.floor(s / 3600)}h ${Math.floor(s % 3600 / 60)}m`;
    };
    const clock = (t) => new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });

    function openInspect(a) {
      inspected = a.id;
      if (!inspectEl) {
        inspectEl = document.createElement('div');
        inspectEl.className = 'lumi-pl-inspect';
        inspectEl.setAttribute('role', 'dialog');
        inspectEl.innerHTML = '<div class="lumi-pl-in-head"><span class="lumi-pl-in-name"></span>'
          + '<span class="lumi-pl-pill"></span></div>'
          + '<div class="lumi-pl-in-task"></div>'
          + '<dl class="lumi-pl-in-meta"><dt>Model</dt><dd class="lumi-pl-in-model"></dd>'
          + '<dt>In this state</dt><dd class="lumi-pl-in-since"></dd></dl>'
          + '<div class="lumi-pl-in-label">Recent</div><ol class="lumi-pl-in-hist"></ol>';
        overlay.appendChild(inspectEl);
      }
      inspectAt = 0;
      inspectEl.hidden = false;
      inspectEl.setAttribute('aria-label', `${NAMES[a.id]} agent`);
      fillInspect(true);
      placeInspect();
    }
    function closeInspect() { inspected = null; if (inspectEl) inspectEl.hidden = true; }
    function fillInspect(force) {
      if (!inspected || !inspectEl) return;
      const t = Date.now();
      if (!force && t - inspectAt < 1000) return;                 // the card text once a second
      inspectAt = t;
      const a = agents[inspected];
      const q = (s) => inspectEl.querySelector(s);
      q('.lumi-pl-in-name').textContent = NAMES[a.id];
      q('.lumi-pl-in-name').style.color = a.st.accent;
      const pill = q('.lumi-pl-pill');
      pill.textContent = STATUS_LABEL[a.status] || a.status;
      pill.className = `lumi-pl-pill s-${STATUS_LABEL[a.status] ? a.status : 'idle'}`;
      const doing = !busyOf(a) && a.beh ? AMBIENT_LABEL[a.beh.kind] : '';
      q('.lumi-pl-in-task').textContent = a.task || (doing ? `(${doing})` : 'Nothing right now');
      const tier = a.model ? `${a.model} tier` : '';
      q('.lumi-pl-in-model').textContent = [a.modelName, tier].filter(Boolean).join(' · ') || '—';
      q('.lumi-pl-in-since').textContent = ago(t - a.since);
      const ol = q('.lumi-pl-in-hist');
      ol.textContent = '';
      for (const h of a.history) {
        const li = document.createElement('li');
        const time = document.createElement('time'); time.textContent = clock(h.t);
        const st = document.createElement('b'); st.textContent = STATUS_LABEL[h.status] || h.status;
        li.append(time, st, document.createTextNode(h.task ? ` ${h.task}` : ''));
        ol.append(li);
      }
      if (!a.history.length) ol.append(Object.assign(document.createElement('li'), { textContent: 'No changes yet.' }));
    }
    function placeInspect() {
      if (!inspected || !inspectEl) return;
      fillInspect(false);
      const a = agents[inspected];
      const [fx] = feet(a);
      const w = inspectEl.offsetWidth || 220, ww = wrapper.clientWidth;
      let left = Math.round((fx - camX + 20) * cssScale);
      if (left + w > ww - 4) left = Math.max(4, Math.round((fx - camX - 20) * cssScale - w));
      const top = Math.max(4, Math.round(a.headY * cssScale));
      inspectEl.style.left = left + 'px';
      inspectEl.style.top = top + 'px';
    }
    const onCanvasClick = (ev) => {
      const a = hitAgent(ev);
      if (a) { openInspect(a); kick(); } else closeInspect();
    };
    const onDocDown = (ev) => {
      if (!inspected) return;
      if (inspectEl && inspectEl.contains(ev.target)) return;
      if (ev.target === canvas) return;                           // the canvas click decides
      closeInspect();
    };
    const onKey = (ev) => { if (ev.key === 'Escape' && inspected) { closeInspect(); ev.stopPropagation(); } };
    const onHover = (ev) => {
      if (dragFrom) {
        const moved = (ev.clientX - dragFrom.x) / cssScale;
        if (Math.abs(moved) > 3) dragFrom.dragged = true;
        camTarget = camX = clamp(dragFrom.cam - moved, 0, Math.max(0, W - viewW));
        manualUntil = performance.now() + 10000;
        kick();
        return;
      }
      canvas.style.cursor = hitAgent(ev) ? 'pointer' : (viewW < W - 0.5 ? 'grab' : '');
    };
    const onDown = (ev) => { if (viewW < W - 0.5) dragFrom = { x: ev.clientX, cam: camX, dragged: false }; };
    const onUp = () => { setTimeout(() => { dragFrom = null; }, 0); };
    canvas.addEventListener('click', (ev) => { if (dragFrom && dragFrom.dragged) return; onCanvasClick(ev); });
    canvas.addEventListener('mousemove', onHover);
    canvas.addEventListener('pointerdown', onDown);
    window.addEventListener('pointerup', onUp);
    document.addEventListener('pointerdown', onDocDown, true);
    document.addEventListener('keydown', onKey, true);

    // ── public API ──────────────────────────────────────────────
    function apply() {
      if (!A) return;
      const now = (performance.now() - t0) / 1000;
      for (const id of ORDER) {
        const a = agents[id];
        const s = snapshot[id];
        if (!s) continue;
        const was = a.status, wasTask = a.task;
        a.status = s.status || 'idle';
        a.vs = s.visual_state || '';
        a.task = s.task || '';
        a.progress = Number(s.progress) || 0;
        a.model = s.model || null;
        a.modelName = s.model_name || null;
        if (was !== a.status || (a.task && a.task !== wasTask)) {
          if (was !== a.status) a.since = Date.now();
          a.history.unshift({ t: Date.now(), status: a.status, task: a.task });
          a.history.length = Math.min(a.history.length, HISTORY);
        }
        if (was !== 'idle' && was !== 'error' && a.status === 'idle') a.doneUntil = now + DONE_BUBBLE_S;
        if (busyOf(a)) {
          if (a.beh) { cancelBehavior(a); if (!a.walking) a.dir = a.spot.face || a.dir; }   // the backend always wins
          retarget(a);
        } else if (was !== a.status) {
          a.idleSince = sim; a.nextBeh = sim + IDLE_BEFORE_AMBIENT_S;
          if (a.beh) cancelBehavior(a);
          retarget(a);
        } else if (!a.beh) {
          retarget(a);
        }
      }
      fillInspect(true);
      kick();
    }
    function update(snap) { snapshot = snap || {}; apply(); }
    function setApprovals(list) { approvals = (list || []).filter((ap) => ap && ap.id); placeOverlay(); kick(); }

    loadAssets(base).then((assets) => {
      if (destroyed) return;
      A = assets;
      W = A.meta.W * RS; H = A.meta.H * RS;
      CW = (A.meta.char && A.meta.char.w) || 48; CH = (A.meta.char && A.meta.char.h) || 96;
      tint = document.createElement('canvas'); tint.width = CW; tint.height = CH;
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
      update, setApprovals, setLighting, setMood,
      destroy() {
        destroyed = true;
        cancelAnimationFrame(raf); clearTimeout(timer);
        if (ro) ro.disconnect(); else window.removeEventListener('resize', resize);
        if (io) io.disconnect();
        document.removeEventListener('visibilitychange', onVis);
        document.removeEventListener('pointerdown', onDocDown, true);
        document.removeEventListener('keydown', onKey, true);
        canvas.removeEventListener('mousemove', onHover);
        canvas.removeEventListener('pointerdown', onDown);
        window.removeEventListener('pointerup', onUp);
        cache.clear();
        window.removeEventListener('lumi-theme', onTheme);
        window.removeEventListener('lumi-mood', onMood);
        overlay.textContent = '';
      },
      get _agents() { return agents; },              // for tests
      get _ready() { return !!A; },
      get _sim() { return sim; },
      _behave(id, kind) { const a = agents[id]; if (a && !busyOf(a)) { a.idleSince = sim - 999; return startBehavior(a, kind); } return false; },
      _pick(id, idleFor) { return pickBehavior(agents[id], idleFor); },
      _pose(id) { return agents[id] ? poseName(agents[id]) : null; },
      get _mood() { return mf; },
      get _size() { return { W, H, CW, CH }; },
      get _view() { return { k, cssScale, viewW, camX, cached: cache.size }; },
    };
  }

  window.LumiPipeline = { mount, bfs, ORDER, BEHAVIOR_WEIGHTS, moodFactors };
})();

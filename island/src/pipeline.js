/*
 * Lumi Pipeline: her agents at work in her cyberpunk room.
 *
 * Shared by the web app (web/pipeline.js) and Lumi Island (island/src/pipeline.js);
 * scripts/sync_themes.py copies this file (and pixel-office/lumi-sprites.js) over.
 * The art lives in pixel-office/ next to this file and is baked by
 * scripts/build_pixel_office.py: Pixel Agents' character poses, tiles and some
 * furniture (MIT, github.com/pixel-agents-hq/pixel-agents), redrawn as four Lumis
 * after her character sheet, in a room of her own. pixel-office/lumi-sprites.js maps
 * every pose to an optional hand-drawn PNG (see there for the format and fallbacks).
 *
 *   const pl = LumiPipeline.mount(wrapperEl, {
 *     base: '/web/pixel-office/',                 // where the baked art is
 *     onDecision: (id, 'approve' | 'decline') => {},
 *   });
 *   pl.update(agents);      // agent_update payload: {main: {status, visual_state, task, progress, model, model_name}, ...}
 *   pl.setApprovals(list);  // [{id, tool, agent, preview}]
 *   pl.setMood(mood);       // {valence, arousal, frustration, boredom} or null; also the window 'lumi-mood' event
 *   pl.destroy();
 *
 * How it plays (after Pixel Agents' office engine): a busy agent walks (BFS over the
 * room's walk grid) to its work station and types, or reads while it searches or
 * reads pages; an idle one walks to its resting seat and sits. Its monitors switch on
 * while it works. An approval puts the permission bubble over its head and Approve /
 * Decline buttons above it; a finished task shows the green check for 2 s; an error
 * flashes it red.
 *
 * Ambient life: once an agent has been idle for IDLE_BEFORE_AMBIENT_S it does things on
 * its own (BEHAVIOR_WEIGHTS: wander, look around, stretch, coffee, desk work, the
 * window, the server rack), naps at night (22:00-06:00, after 2 min idle), has lunch
 * (12:00-14:00). Lumi's mood, when the page has it, changes the pace and the choices.
 * The backend's state always wins: a new task interrupts whatever she's doing. Ambient
 * time runs only while the canvas is drawn, so it stops while the page is hidden.
 *
 * Click a Lumi for a card: status, task, model and tier, time in this state, her
 * last 5 changes (kept here, not on the server). Click elsewhere or Esc to close.
 *
 * The room is drawn at 1x into a 320x192 buffer and scaled up crisp; names, buttons
 * and the card are at native resolution. 30 fps while anyone walks or works, about
 * 8 fps otherwise, nothing while hidden.
 */
(function () {
  'use strict';

  const ORDER = ['main', 'minecraft', 'planner', 'system'];
  const NAMES = { main: 'Main', minecraft: 'Minecraft', planner: 'Planner', system: 'System' };
  const STATUS_LABEL = { idle: 'Idle', thinking: 'Thinking', executing: 'Executing', waiting_approval: 'Needs you', error: 'Error' };
  const READING_RE = /web_search|browse|research|search_files|read_file|read_window|reading|searching|triag|recall|library/i;
  const FAST_MS = 33, SLOW_MS = 120;
  const BLINK_S = 0.15;
  const DONE_BUBBLE_S = 2;
  const HISTORY = 5;

  // ── ambient behaviour ─────────────────────────────────────────
  const IDLE_BEFORE_AMBIENT_S = 8;
  const SLEEP_AFTER_IDLE_S = 120;              // at night
  const NIGHT = [22, 6], LUNCH = [12, 14];      // local hours [from, to)
  const MAX_WALK_PX_S = 64;                     // mood can speed her up, never to a run
  // Relative odds of each idle behaviour, per agent (before mood). Sleep and lunch
  // aren't in here: they depend on the clock (see pickBehavior).
  const BEHAVIOR_WEIGHTS = {
    main:      { deskwork: 5, look: 3, stretch: 2, drink: 2, wander: 1, window: 0.5, rack: 0 },
    minecraft: { wander: 5, window: 4, look: 2, stretch: 1, drink: 1, deskwork: 0.5, rack: 0 },
    planner:   { deskwork: 5, drink: 2, stretch: 2, look: 1, wander: 1, window: 0.5, rack: 0 },
    system:    { rack: 5, look: 2, stretch: 1, drink: 1, wander: 0.5, deskwork: 0, window: 0 },
  };
  const LUNCH_WEIGHT = 6;                       // added once per lunch window per agent
  const AMBIENT_LABEL = {
    wander: 'wandering', look: 'looking around', stretch: 'stretching', drink: 'having coffee',
    deskwork: 'tidying up her desk', window: 'looking out the window', rack: 'watching the servers',
    eat: 'having lunch', sleep: 'asleep',
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
        console.warn(`[pipeline] ${missing.length} poses have no custom PNG in ${base}${sprites.CUSTOM_DIR} and use the baked sheet: `
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

  // Mood -> how she moves and what she picks. Neutral without a mood.
  function moodFactors(m) {
    const arousal = m && Number.isFinite(+m.arousal) ? clamp(+m.arousal, 0, 1) : 0.5;
    const frustration = m && Number.isFinite(+m.frustration) ? clamp(+m.frustration, 0, 1) : 0;
    const boredom = m && Number.isFinite(+m.boredom) ? clamp(+m.boredom, 0, 1) : 0;
    return {
      speed: clamp(0.75 + 0.5 * arousal + 0.35 * frustration, 0.6, 1.4),   // pace
      restless: 1 + 2 * frustration,                                       // more pacing when frustrated
      curious: 1 + boredom,                                                // looks about when bored
      sleepy: arousal < 0.3 ? (0.3 - arousal) * 20 : 0,                    // naps by day when drained
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
    let W = 320, H = 192, buf = null, bctx = null, tint = null, tctx = null;
    let scale = 1, cssScale = 1, raf = 0, timer = 0, last = 0, visible = true, destroyed = false;
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

    function makeAgent(id) {
      const st = A.meta.agents[id];
      const spot = st.rest;
      return {
        id, st, spot, target: spot, path: [],
        col: spot.col, row: spot.row, x: spot.col * 16 + 8, y: spot.row * 16 + 8,
        dir: spot.face, walking: false, frame: 0, frameT: 0,
        status: 'idle', vs: 'sitting', task: '', progress: 0, model: null, modelName: null,
        nextBlink: 1 + Math.random() * 4, blinkUntil: 0, doneUntil: 0, headY: 0,
        idleSince: 0, beh: null, nextBeh: IDLE_BEFORE_AMBIENT_S, lunchDay: '',
        since: Date.now(), history: [],
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
    function wanderTile(a) {
      const walk = A.meta.walk, taken = othersBlocked(a), home = a.st.rest;
      const options = [];
      for (let r = 0; r < walk.length; r++) for (let c = 0; c < walk[0].length; c++) {
        if (walk[r][c] !== '1' || taken.has(`${c},${r}`)) continue;
        const d = Math.abs(c - home.col) + Math.abs(r - home.row);
        if (d >= 2 && d <= 7) options.push({ col: c, row: r });
      }
      if (!options.length) return null;
      const t = options[Math.floor(Math.random() * options.length)];
      return { col: t.col, row: t.row, face: ['down', 'left', 'right'][Math.floor(Math.random() * 3)], sit: false };
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
            if (t) steps.push({ go: t }, { pose: 'idle', dur: pause(1.5, 4) / mf.restless * 1.5 });
          }
          return steps.length ? [...steps, { go: rest }] : null;
        }
        case 'look':     return [{ pose: a.spot.sit ? 'sit' : 'idle', dur: pause(2.5, 4), look: true }];
        case 'stretch':  return [{ pose: 'stretch', dur: 2.4 }];
        case 'drink': {
          const cup = freeSpot(a, 'coffee');
          return cup ? [{ go: { ...cup, sit: false } }, { pose: 'idle', dur: 1.2 }, { go: rest }, { pose: 'drink', dur: pause(6, 10) }] : null;
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
      else { b.until = sim + s.dur; b.nextLook = sim + 0.8; b.looks = 0; a.frame = 0; }
    }
    function endBehavior(a) {
      a.beh = null;
      a.nextBeh = sim + rand(4, 10) / mf.speed;
      if (a.target !== a.st.rest && !busyOf(a)) goTo(a, a.st.rest);
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
    const sitsNow = (a) => !a.walking && a.spot.sit && poseName(a) !== 'stretch';

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
      const name = poseName(a);
      const pose = A.chars[a.id].poses[name];
      const col = pose.cols[a.frame % pose.cols.length];
      const rowIdx = F.rows[a.dir] ?? 0;
      const sheet = pose.custom ? pose.img : (now < a.blinkUntil ? A.chars[a.id].blink : A.chars[a.id].open);
      const sit = sitsNow(a) ? A.meta.timing.sit_offset : 0;
      const slump = name === 'sleep' && !pose.custom ? 1 : 0;
      let dx = Math.round(a.x - 8), dy = Math.round(a.y + sit + slump - 32);
      // stretch without custom art: a slow rise on her toes (scale-y pulse from the feet)
      const sy = name === 'stretch' && !pose.custom ? 1 + 0.09 * Math.max(0, Math.sin(sim * 2.6)) : 1;
      const h = Math.round(32 * sy), top = dy + 32 - h;
      const flip = a.dir === 'left';
      const alarm = a.status === 'error' && Math.floor(now * 4) % 2 === 0;
      if (alarm) {                                    // flash red: tint the frame on a scratch canvas
        tctx.clearRect(0, 0, 16, 32);
        tctx.globalCompositeOperation = 'source-over';
        tctx.drawImage(sheet, col * 16, rowIdx * 32, 16, 32, 0, 0, 16, 32);
        tctx.globalCompositeOperation = 'source-atop';
        tctx.fillStyle = 'rgba(255,45,70,0.55)';
        tctx.fillRect(0, 0, 16, 32);
      }
      const src = alarm ? tint : sheet, sx = alarm ? 0 : col * 16, syy = alarm ? 0 : rowIdx * 32;
      if (flip) { c.save(); c.translate(dx + 16, top); c.scale(-1, 1); c.drawImage(src, sx, syy, 16, 32, 0, 0, 16, h); c.restore(); }
      else c.drawImage(src, sx, syy, 16, 32, dx, top, 16, h);
      a.headY = top;
      // her halo glows a little (the sheet draws the ring; this is the light)
      const glow = 0.18 + 0.12 * Math.sin(now * 2 + ORDER.indexOf(a.id));
      c.save();
      c.globalCompositeOperation = 'lighter';
      c.globalAlpha = glow;
      c.fillStyle = a.st.accent;
      c.fillRect(dx + 4, top + 1, 8, 1); c.fillRect(dx + 5, top, 6, 1); c.fillRect(dx + 5, top + 2, 6, 1);
      c.restore();
      if (!pose.custom && pose.effect) drawEffect(c, a, pose.effect, dx, dy, now);
    }

    // Small props over a baked frame for poses the sheet has no art for.
    function drawEffect(c, a, kind, dx, dy, now) {
      const px = (x, y, col) => { c.fillStyle = col; c.fillRect(x, y, 1, 1); };
      const facingUp = a.dir === 'up';
      const hx = a.dir === 'left' ? dx + 2 : dx + 12, hy = dy + 19;       // her hand, roughly
      if (kind === 'mug' && !facingUp) {
        const lift = Math.floor(now * 0.8) % 3 === 0 ? -2 : 0;             // a sip now and then
        c.fillStyle = '#f4f6fb'; c.fillRect(hx - 1, hy + lift, 3, 3);
        px(hx - 1, hy + lift, a.st.accent); px(hx, hy + lift, a.st.accent); px(hx + 1, hy + lift, a.st.accent);
        px(a.dir === 'left' ? hx - 2 : hx + 2, hy + 1 + lift, '#d5d6e0');
        const t = (now * 1.5) % 1;
        c.globalAlpha = 1 - t; px(hx, hy - 2 + lift - Math.round(t * 3), '#c6f6f8'); c.globalAlpha = 1;
      } else if (kind === 'food' && !facingUp) {
        const bite = Math.floor(now * 2) % 2 ? -1 : 0;
        c.fillStyle = '#f4f6fb'; c.fillRect(hx - 1, hy + 1 + bite, 3, 2); px(hx, hy + bite, '#f4f6fb');
        c.fillStyle = '#16161c'; c.fillRect(hx - 1, hy + 2 + bite, 3, 1);
      } else if (kind === 'zzz') {
        for (let i = 0; i < 3; i++) {
          const t = ((now * 0.35) + i / 3) % 1;
          const zx = Math.round(a.x + 3 + t * 6 + i), zy = Math.round(dy + 2 - t * 12);
          c.globalAlpha = Math.max(0, 1 - t) * 0.9;
          c.fillStyle = '#e7eeff';
          c.fillRect(zx, zy, 3, 1); px(zx + 1, zy + 1, '#e7eeff'); c.fillRect(zx, zy + 2, 3, 1);
        }
        c.globalAlpha = 1;
      }
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
        const sit = sitsNow(a) ? A.meta.timing.sit_offset : 0;
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
      sim += dt;
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
      const bx = (ev.clientX - r.left) / (r.width / W), by = (ev.clientY - r.top) / (r.height / H);
      let best = null;
      for (const id of ORDER) {
        const a = agents[id];
        if (bx >= a.x - 7 && bx <= a.x + 7 && by >= a.headY + 1 && by <= a.headY + 32 && (!best || a.y > best.y)) best = a;
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
      const w = inspectEl.offsetWidth || 220, ww = wrapper.clientWidth;
      let left = Math.round(a.x * cssScale + 12 * cssScale);
      if (left + w > ww - 4) left = Math.max(4, Math.round(a.x * cssScale - 12 * cssScale - w));
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
    const onHover = (ev) => { canvas.style.cursor = hitAgent(ev) ? 'pointer' : ''; };
    canvas.addEventListener('click', onCanvasClick);
    canvas.addEventListener('mousemove', onHover);
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
          if (a.beh) cancelBehavior(a);                   // the backend always wins
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
      update, setApprovals, setLighting, setMood,
      destroy() {
        destroyed = true;
        cancelAnimationFrame(raf); clearTimeout(timer);
        if (ro) ro.disconnect(); else window.removeEventListener('resize', resize);
        if (io) io.disconnect();
        document.removeEventListener('visibilitychange', onVis);
        document.removeEventListener('pointerdown', onDocDown, true);
        document.removeEventListener('keydown', onKey, true);
        canvas.removeEventListener('click', onCanvasClick);
        canvas.removeEventListener('mousemove', onHover);
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
    };
  }

  window.LumiPipeline = { mount, bfs, ORDER, BEHAVIOR_WEIGHTS, moodFactors };
})();

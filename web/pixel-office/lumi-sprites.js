/*
 * Lumi's sprite manifest for the Pipeline (web/pipeline.js, island/src/pipeline.js).
 * Copied to island/src/pixel-office/ by scripts/sync_themes.py.
 *
 * Every pose of every agent has a name, e.g. "lumi-main-idle". Each name is an
 * OPTIONAL hand-drawn PNG at pixel-office/chars/custom/<name>.png:
 *
 *     16 px wide frames, 32 px tall, any number of frames across (played in a loop),
 *     3 rows: facing down, up, right (left is the right row mirrored).
 *     Transparent background; the feet at the bottom row of pixels, like the baked sheets.
 *
 * Drop one in and it's used on the next load, no code change. A pose without one
 * falls back, in order, to:
 *   1. the baked sheet chars/<agent>.png (scripts/build_pixel_office.py: Lumi after her
 *      character sheet, one look per agent), using BAKED_FRAMES below, plus a small
 *      effect drawn on top for the poses the sheet has no art for (EFFECTS: a mug, a
 *      rice ball, z z z, a stretch);
 *   2. if that sheet is missing too, a placeholder drawn here on a canvas in her
 *      palette (silver/teal hair, cat ears, halo, white top, black shorts, socks).
 * Whatever falls back is listed once in the console.
 */
(function () {
  'use strict';

  const AGENTS = ['main', 'minecraft', 'planner', 'system'];
  const POSES = ['idle', 'walk', 'sit', 'type', 'read', 'stretch', 'drink', 'eat', 'sleep'];

  // { main: { idle: 'lumi-main-idle', walk: 'lumi-main-walk', ... }, minecraft: {...}, ... }
  const LUMI_FRAMES = {};
  for (const a of AGENTS) {
    LUMI_FRAMES[a] = {};
    for (const p of POSES) LUMI_FRAMES[a][p] = `lumi-${a}-${p}`;
  }

  // Columns of the baked 128x96 sheet (8 frames: walk1-3, type1-2, read1-2, seated).
  const BAKED_FRAMES = {
    idle: [1], walk: [0, 1, 2, 1], sit: [7], type: [3, 4], read: [5, 6],
    stretch: [1], drink: [7], eat: [7], sleep: [7],
  };
  // What pipeline.js draws over a baked (not custom) frame for these poses.
  const EFFECTS = { stretch: 'stretch', drink: 'mug', eat: 'food', sleep: 'zzz' };
  // Seconds per frame for custom strips.
  const FRAME_S = { walk: 0.15, type: 0.3, read: 0.3, idle: 0.5, sit: 0.5, stretch: 0.25, drink: 0.4, eat: 0.35, sleep: 0.8 };
  const CUSTOM_DIR = 'chars/custom/';

  // Her character sheet's palette, and each agent's accent (tie, halo).
  const PALETTE = {
    silver: '#dfe1ea', silverD: '#7d8196', teal: '#8fd8cf', tealD: '#2b6f6c', skin: '#f3d6c8',
    white: '#f4f6fb', lav: '#d5d6e0', black: '#16161c', pink: '#f4a7bd', mint: '#8fd8cf', eye: '#3fa79a',
  };
  const ACCENT = { main: '#5cf2d2', minecraft: '#5bd16a', planner: '#b48cff', system: '#ff5a7a' };

  // A 128x96 sheet in the baked layout, drawn here: used only when the baked
  // sheet itself is missing, so the Pipeline still runs.
  function placeholderSheet(agent) {
    const c = document.createElement('canvas');
    c.width = 128; c.height = 96;
    const g = c.getContext('2d');
    const P = PALETTE, acc = ACCENT[agent] || P.teal;
    const rect = (x, y, w, h, col) => { g.fillStyle = col; g.fillRect(x, y, w, h); };
    for (let row = 0; row < 3; row++) {
      for (let f = 0; f < 8; f++) {
        const x = f * 16, y = row * 32;
        const seated = f === 7, step = f === 0 ? -1 : f === 2 ? 1 : 0;
        const by = y + (seated ? 3 : 0);
        rect(x + 5, y + 1, 6, 1, acc);                                  // halo
        rect(x + 4, by + 3, 2, 2, row === 1 ? P.teal : P.silver);       // cat ears
        rect(x + 10, by + 3, 2, 2, row === 1 ? P.silver : P.teal);
        if (row !== 1) { rect(x + 5, by + 4, 1, 1, P.pink); rect(x + 10, by + 4, 1, 1, P.pink); }
        rect(x + 4, by + 5, 4, 8, row === 1 ? P.teal : P.silver);       // split hair
        rect(x + 8, by + 5, 4, 8, row === 1 ? P.silver : P.teal);
        if (row === 0) {                                                // face
          rect(x + 5, by + 8, 6, 5, P.skin);
          rect(x + 6, by + 10, 1, 1, P.eye); rect(x + 9, by + 10, 1, 1, P.eye);
        } else if (row === 2) {
          rect(x + 8, by + 8, 3, 5, P.skin); rect(x + 10, by + 10, 1, 1, P.eye);
        }
        rect(x + 4, by + 13, 8, 7, P.white);                            // top
        if (row === 0) rect(x + 7, by + 14, 2, 4, acc);                 // tie
        rect(x + 4, by + 20, 8, 3, P.black);                            // shorts
        if (!seated) {
          rect(x + 5 + step, by + 23, 2, 6, P.white);                   // socks
          rect(x + 9 - step, by + 23, 2, 6, P.white);
          rect(x + 5 + step, by + 29, 2, 2, P.black); rect(x + 9 - step, by + 29, 2, 2, P.black);
          rect(x + 5 + step, by + 31, 2, 1, P.mint); rect(x + 9 - step, by + 31, 2, 1, P.mint);
        } else {
          rect(x + 4, by + 23, 8, 3, P.white);
        }
        if (f >= 3 && f <= 6) rect(x + 3, by + 15, 10, 1, P.lav);       // arms forward: typing / reading
      }
    }
    return c;
  }

  const loadImg = (src) => new Promise((resolve) => {
    const i = new Image();
    i.onload = () => resolve(i);
    i.onerror = () => resolve(null);
    i.src = src;
  });

  // { open, blink, poses: { idle: {img, cols, custom, effect, frameS}, ... }, missing: [names] }
  async function loadAgent(base, agent, baked) {
    let open = baked && baked.open, blink = baked && baked.blink, placeholder = false;
    if (!open) { open = blink = placeholderSheet(agent); placeholder = true; }
    if (!blink) blink = open;
    const customs = await Promise.all(POSES.map((p) => loadImg(base + CUSTOM_DIR + LUMI_FRAMES[agent][p] + '.png')));
    const poses = {}, missing = [];
    POSES.forEach((p, i) => {
      const img = customs[i];
      if (img && img.width >= 16 && img.height >= 96) {
        const n = Math.floor(img.width / 16);
        poses[p] = { img, cols: [...Array(n).keys()], custom: true, effect: null, frameS: FRAME_S[p] };
      } else {
        poses[p] = { img: null, cols: BAKED_FRAMES[p], custom: false, effect: EFFECTS[p] || null, frameS: FRAME_S[p] };
        missing.push(LUMI_FRAMES[agent][p]);
      }
    });
    return { open, blink, poses, missing, placeholder };
  }

  window.LumiSprites = { AGENTS, POSES, LUMI_FRAMES, BAKED_FRAMES, EFFECTS, CUSTOM_DIR, PALETTE, ACCENT,
                         placeholderSheet, loadAgent };
})();

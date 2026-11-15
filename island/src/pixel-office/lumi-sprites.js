/*
 * Lumi's sprite manifest for the Pipeline (web/pipeline.js, island/src/pipeline.js).
 * Copied to island/src/pixel-office/ by scripts/sync_themes.py.
 *
 * Every pose of every agent has a name, e.g. "lumi-main-idle". Each name is an
 * OPTIONAL hand-drawn PNG at pixel-office/chars/custom/<name>.png:
 *
 *     48 px wide frames, 96 px tall, any number of frames across (played in a loop),
 *     3 rows: facing down, up, right (left is the right row mirrored).
 *     Transparent background, her feet on the bottom row of pixels, like the baked sheets.
 *
 * Drop one in and it's used on the next load, no code change. A pose without one
 * falls back, in order, to:
 *   1. the baked sheet chars/<agent>.png (scripts/build_pixel_office.py + lumi_hd.py:
 *      four 48x96 Lumis after her character sheet, with stretch, coffee, rice ball and
 *      asleep frames), using BAKED_FRAMES below;
 *   2. if that sheet is missing too, a placeholder drawn here on a canvas in her
 *      palette (silver/teal hair, cat ears, halo, white top, black shorts, socks).
 * pipeline.js adds the live extras on top (steam, z z z, the thought bubble).
 */
(function () {
  'use strict';

  const AGENTS = ['main', 'minecraft', 'office', 'system'];
  const POSES = ['idle', 'walk', 'sit', 'type', 'read', 'stretch', 'drink', 'eat', 'sleep'];
  const FW = 48, FH = 96;

  // { main: { idle: 'lumi-main-idle', walk: 'lumi-main-walk', ... }, minecraft: {...}, ... }
  const LUMI_FRAMES = {};
  for (const a of AGENTS) {
    LUMI_FRAMES[a] = {};
    for (const p of POSES) LUMI_FRAMES[a][p] = `lumi-${a}-${p}`;
  }

  // Columns of the baked 768x288 sheet (16 frames of 48x96; scripts/lumi_hd.py).
  const BAKED_FRAMES = {
    idle: [1], walk: [0, 1, 2, 1], sit: [7], type: [3, 4], read: [5, 6],
    stretch: [8, 8, 9, 9, 9, 8], drink: [10, 10, 10, 11, 11, 10], eat: [12, 12, 13, 12, 13, 12], sleep: [14, 15],
  };
  // Seconds per frame.
  const FRAME_S = { walk: 0.15, type: 0.3, read: 0.3, idle: 0.5, sit: 0.5, stretch: 0.45, drink: 0.55, eat: 0.5, sleep: 1.2 };
  const CUSTOM_DIR = 'chars/custom/';

  // Her character sheet's palette, and each agent's accent (tie, halo).
  const PALETTE = {
    silver: '#dfe1ea', silverD: '#7d8196', teal: '#8fd8cf', tealD: '#2b6f6c', skin: '#fae6dd',
    white: '#f4f6fb', lav: '#d5d6e0', black: '#16161c', pink: '#f4a7bd', mint: '#8fd8cf', eye: '#3fa79a',
  };
  const ACCENT = { main: '#5cf2d2', minecraft: '#5bd16a', office: '#b48cff', system: '#ff5a7a' };

  // A 768x288 sheet in the baked layout, drawn here: used only when the baked
  // sheet itself is missing, so the Pipeline still runs. Drawn at 16x32 and scaled 3x.
  function placeholderSheet(agent) {
    const small = document.createElement('canvas');
    small.width = 128; small.height = 96;
    const g = small.getContext('2d');
    const P = PALETTE, acc = ACCENT[agent] || P.teal;
    const rect = (x, y, w, h, col) => { g.fillStyle = col; g.fillRect(x, y, w, h); };
    for (let row = 0; row < 3; row++) {
      for (let f = 0; f < 8; f++) {
        const x = f * 16, y = row * 32;
        const seated = f === 7, step = f === 0 ? -1 : f === 2 ? 1 : 0;
        const by = y + (seated ? 3 : 0);
        rect(x + 5, y + 1, 6, 1, acc);                                  // halo
        rect(x + 4, by + 3, 2, 2, P.white);                             // cat ears
        rect(x + 10, by + 3, 2, 2, P.white);
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
    const c = document.createElement('canvas');
    c.width = FW * 16; c.height = FH * 3;
    const h = c.getContext('2d');
    h.imageSmoothingEnabled = false;
    // frames 8-15 (stretch, coffee, rice ball, asleep) reuse standing / seated
    const src = [0, 1, 2, 3, 4, 5, 6, 7, 1, 1, 7, 7, 7, 7, 7, 7];
    for (let row = 0; row < 3; row++) {
      src.forEach((s, col) => h.drawImage(small, s * 16, row * 32, 16, 32, col * FW, row * FH, FW, FH));
    }
    return c;
  }

  const loadImg = (src) => new Promise((resolve) => {
    const i = new Image();
    i.onload = () => resolve(i);
    i.onerror = () => resolve(null);
    i.src = src;
  });

  // { open, blink, poses: { idle: {img, cols, custom, frameS}, ... }, missing: [names] }
  async function loadAgent(base, agent, baked) {
    let open = baked && baked.open, blink = baked && baked.blink, placeholder = false;
    if (!open || open.width < FW * 16) { open = blink = placeholderSheet(agent); placeholder = true; }
    if (!blink || blink.width < FW * 16) blink = open;
    const customs = await Promise.all(POSES.map((p) => loadImg(base + CUSTOM_DIR + LUMI_FRAMES[agent][p] + '.png')));
    const poses = {}, missing = [];
    POSES.forEach((p, i) => {
      const img = customs[i];
      if (img && img.width >= FW && img.height >= FH * 3) {
        const n = Math.floor(img.width / FW);
        poses[p] = { img, cols: [...Array(n).keys()], custom: true, frameS: FRAME_S[p] };
      } else {
        poses[p] = { img: null, cols: BAKED_FRAMES[p], custom: false, frameS: FRAME_S[p] };
        missing.push(LUMI_FRAMES[agent][p]);
      }
    });
    return { open, blink, poses, missing, placeholder };
  }

  window.LumiSprites = { AGENTS, POSES, FW, FH, LUMI_FRAMES, BAKED_FRAMES, CUSTOM_DIR, PALETTE, ACCENT,
                         placeholderSheet, loadAgent };
})();

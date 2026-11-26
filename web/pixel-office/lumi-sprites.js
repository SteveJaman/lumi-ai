/*
 * Lumi's sprite sheets for the Pipeline (web/pipeline.js, island/src/pipeline.js).
 * Copied to island/src/pixel-office/ by scripts/sync_themes.py.
 *
 * One sheet per agent, chars/<agent>-sheet.png, in the RPG Maker walk layout:
 *
 *     4 columns: the walk cycle (columns 1 and 3 are legs together; 1 is her stand)
 *     3 rows:    facing down, up, right (left is the right row mirrored)
 *     cells:     the sheet's width / 4 by height / 3, transparent, her feet on the
 *                cell's bottom row (pipeline.js puts that point on her tile's centre)
 *
 * Written by scripts/lumi_pixel_art.py (hand-placed chibi pixel art, 40x56 cells) or
 * scripts/generate_sprites.py (an image model, cut to the same layout). Replace a
 * sheet with any PNG in that layout, any size, transparent or on a plain background:
 * normalizeSheet() clears the background and fits it to 40x56 cells. Without one, a
 * plain placeholder in her colours is drawn here so the Pipeline still runs.
 *
 * Her activities (typing, coffee, a nap) use the standing frame; pipeline.js adds the
 * live extras on top (a typing bob, steam, z z z, the thought bubble).
 */
(function () {
  'use strict';

  const AGENTS = ['main', 'minecraft', 'office', 'system'];
  const POSES = ['idle', 'walk', 'sit', 'type', 'read', 'stretch', 'drink', 'eat', 'sleep'];
  const COLS = 4, ROWS = 3;
  const ROW = { down: 0, up: 1, right: 2, left: 2 };
  const STAND = 1;
  // Sheet columns per pose.
  const FRAMES = {
    idle: [STAND], walk: [0, 1, 2, 3], sit: [STAND], type: [STAND, STAND], read: [STAND],
    stretch: [STAND], drink: [STAND], eat: [STAND], sleep: [STAND],
  };
  // Seconds per frame.
  const FRAME_S = { walk: 0.15, type: 0.3, read: 0.3, idle: 0.5, sit: 0.5, stretch: 0.45, drink: 0.55, eat: 0.5, sleep: 1.2 };

  const PALETTE = {
    silver: '#eef0f7', teal: '#5ec6b9', skin: '#fff0e8', white: '#f7f9fd', black: '#25252e',
    eye: '#3fb7a9', outline: '#2a2336', pink: '#f6b8c9',
  };
  const ACCENT = { main: '#5cf2d2', minecraft: '#5bd16a', office: '#b48cff', system: '#ff5a7a' };

  const sheetFile = (agent) => `chars/${agent}-sheet.png`;

  // A 4x3 sheet of 40x56 cells, only for when the real sheet is missing.
  function placeholderSheet(agent) {
    const CW = 40, CH = 56;
    const c = document.createElement('canvas');
    c.width = CW * COLS; c.height = CH * ROWS;
    const g = c.getContext('2d');
    const P = PALETTE, acc = ACCENT[agent] || P.teal;
    const rect = (x, y, w, h, col) => { g.fillStyle = col; g.fillRect(x, y, w, h); };
    for (let row = 0; row < ROWS; row++) {
      for (let f = 0; f < COLS; f++) {
        const x = f * CW, y = row * CH, lift = f === 0 ? 2 : 0, liftR = f === 2 ? 2 : 0;
        rect(x + 14, y + 2, 12, 2, acc);                                   // halo
        rect(x + 8, y + 7, 24, 22, P.outline);                             // head
        rect(x + 9, y + 8, 11, 20, row === 1 ? P.teal : P.silver);
        rect(x + 20, y + 8, 11, 20, row === 1 ? P.silver : P.teal);
        if (row !== 1) {
          rect(x + 12, y + 14, 16, 13, P.skin);
          rect(x + 14, y + 18, 3, 4, P.eye); rect(x + 23, y + 18, 3, 4, P.eye);
        }
        rect(x + 12, y + 29, 16, 9, P.white);                              // jacket
        rect(x + 19, y + 29, 2, 6, acc);                                   // tie
        rect(x + 14, y + 38, 12, 3, P.black);                              // shorts
        rect(x + 14, y + 41 - lift, 4, 13, P.white);                       // legs
        rect(x + 22, y + 41 - liftR, 4, 13, P.white);
        rect(x + 13, y + 53 - lift, 6, 3, P.outline); rect(x + 21, y + 53 - liftR, 6, 3, P.outline);
      }
    }
    return c;
  }

  // ── Any art in the 4x3 layout, made to fit ────────────────────────────────
  // The house format is 40x56 cells on a transparent sheet (160x168). Art from
  // elsewhere is often far bigger (e.g. 333x393 cells) on a solid background; drawn
  // as is, every frame would be a huge box covering the whole room. So: clear the
  // background that touches the sheet's edge, crop every cell to the box the whole
  // character fits in (the same box for all 12, so she doesn't jump between frames),
  // and scale that into 40x56 with her feet on the bottom row.
  const CELL_W = 40, CELL_H = 56;
  const BG_TOLERANCE = 28;          // per channel, from the corners' colour
  const SOLID = 24;                 // alpha above this counts as her

  function clearEdgeBackground(px, W, H) {
    const at = (x, y) => (y * W + x) * 4;
    const corners = [at(0, 0), at(W - 1, 0), at(0, H - 1), at(W - 1, H - 1)];
    if (corners.some((i) => px[i + 3] < 200)) return;                  // already transparent
    const bg = [0, 1, 2].map((k) => corners.map((i) => px[i + k]).sort((a, b) => a - b)[1]);
    const near = (i) => px[i + 3] > 0 && Math.abs(px[i] - bg[0]) <= BG_TOLERANCE
      && Math.abs(px[i + 1] - bg[1]) <= BG_TOLERANCE && Math.abs(px[i + 2] - bg[2]) <= BG_TOLERANCE;
    // Flood fill from the edges, so dark parts of her outfit inside the outline stay.
    const seen = new Uint8Array(W * H);
    const stack = [];
    const push = (x, y) => {
      const p = y * W + x;
      if (!seen[p] && near(p * 4)) { seen[p] = 1; stack.push(p); }
    };
    for (let x = 0; x < W; x++) { push(x, 0); push(x, H - 1); }
    for (let y = 0; y < H; y++) { push(0, y); push(W - 1, y); }
    while (stack.length) {
      const p = stack.pop();
      px[p * 4 + 3] = 0;
      const x = p % W, y = (p - x) / W;
      if (x > 0) push(x - 1, y);
      if (x < W - 1) push(x + 1, y);
      if (y > 0) push(x, y - 1);
      if (y < H - 1) push(x, y + 1);
    }
  }

  // Halve until within 2x of the target, then one last step: smoother than one big jump.
  function shrink(src, sx, sy, sw, sh, dw, dh) {
    let cur = src, cx = sx, cy = sy, w = sw, h = sh;
    while (w / 2 >= dw * 2 && h / 2 >= dh * 2) {
      const c = document.createElement('canvas');
      c.width = Math.ceil(w / 2); c.height = Math.ceil(h / 2);
      const g = c.getContext('2d');
      g.imageSmoothingEnabled = true; g.imageSmoothingQuality = 'high';
      g.drawImage(cur, cx, cy, w, h, 0, 0, c.width, c.height);
      cur = c; cx = 0; cy = 0; w = c.width; h = c.height;
    }
    return { img: cur, x: cx, y: cy, w, h };
  }

  function normalizeSheet(img) {
    const W = img.width, H = img.height;
    if (W === CELL_W * COLS && H === CELL_H * ROWS) return img;       // the house format
    const src = document.createElement('canvas');
    src.width = W; src.height = H;
    const sg = src.getContext('2d', { willReadFrequently: true });
    sg.drawImage(img, 0, 0);
    const data = sg.getImageData(0, 0, W, H);
    const px = data.data;
    clearEdgeBackground(px, W, H);
    sg.putImageData(data, 0, 0);
    const cw = Math.floor(W / COLS), ch = Math.floor(H / ROWS);
    let x0 = cw, y0 = ch, x1 = -1, y1 = -1;                            // one box for every cell
    for (let y = 0; y < ch * ROWS; y++) {
      const cy = y % ch;
      for (let x = 0; x < cw * COLS; x++) {
        if (px[(y * W + x) * 4 + 3] <= SOLID) continue;
        const cx = x % cw;
        if (cx < x0) x0 = cx;
        if (cx > x1) x1 = cx;
        if (cy < y0) y0 = cy;
        if (cy > y1) y1 = cy;
      }
    }
    if (x1 < 0) return null;                                            // nothing there
    const bw = x1 - x0 + 1, bh = y1 - y0 + 1;
    const s = Math.min(CELL_W / bw, CELL_H / bh, 1);
    const dw = Math.max(1, Math.round(bw * s)), dh = Math.max(1, Math.round(bh * s));
    const out = document.createElement('canvas');
    out.width = CELL_W * COLS; out.height = CELL_H * ROWS;
    const og = out.getContext('2d');
    og.imageSmoothingEnabled = true; og.imageSmoothingQuality = 'high';
    for (let r = 0; r < ROWS; r++) {
      for (let c = 0; c < COLS; c++) {
        const step = shrink(src, c * cw + x0, r * ch + y0, bw, bh, dw, dh);
        og.drawImage(step.img, step.x, step.y, step.w, step.h,
                     c * CELL_W + Math.round((CELL_W - dw) / 2), r * CELL_H + CELL_H - dh, dw, dh);
      }
    }
    return out;
  }

  // { sheet, cw, ch, poses: { idle: {cols, frameS}, ... }, placeholder }
  function loadAgent(agent, img) {
    let art = null;
    if (img && img.width >= COLS && img.height >= ROWS) {
      try { art = normalizeSheet(img); } catch (e) { console.warn(`[pipeline] ${agent} sheet:`, e); }
    }
    const ok = !!art;
    const sheet = ok ? art : placeholderSheet(agent);
    const poses = {};
    for (const p of POSES) poses[p] = { cols: FRAMES[p], frameS: FRAME_S[p] };
    return {
      sheet, poses, placeholder: !ok,
      cw: Math.floor(sheet.width / COLS), ch: Math.floor(sheet.height / ROWS),
    };
  }

  window.LumiSprites = { AGENTS, POSES, COLS, ROWS, ROW, STAND, FRAMES, FRAME_S, PALETTE, ACCENT,
                         CELL_W, CELL_H, sheetFile, placeholderSheet, normalizeSheet, loadAgent };
})();

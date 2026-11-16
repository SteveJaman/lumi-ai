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
 * sheet with any PNG in that layout and it's used on the next load. Without one, a
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

  // { sheet, cw, ch, poses: { idle: {cols, frameS}, ... }, placeholder }
  function loadAgent(agent, img) {
    const ok = img && img.width >= COLS && img.height >= ROWS;
    const sheet = ok ? img : placeholderSheet(agent);
    const poses = {};
    for (const p of POSES) poses[p] = { cols: FRAMES[p], frameS: FRAME_S[p] };
    return {
      sheet, poses, placeholder: !ok,
      cw: Math.floor(sheet.width / COLS), ch: Math.floor(sheet.height / ROWS),
    };
  }

  window.LumiSprites = { AGENTS, POSES, COLS, ROWS, ROW, STAND, FRAMES, FRAME_S, PALETTE, ACCENT,
                         sheetFile, placeholderSheet, loadAgent };
})();

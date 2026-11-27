/* ============================================================
   The Live2D avatar on the web client (Phase 3, Stage).
   Renders a Cubism model (.model3.json) with Pixi 7 and
   pixi-live2d-display-advanced, both from web/vendor. The model comes
   from the folder named in data/avatars/live2d.json (served by
   /api/avatar/live2d/file/...). Live2D's Cubism Core is not ours to
   ship: you download it from Live2D and put it at
   web/vendor/live2dcubismcore.min.js.

   The face moves with the same motion engine as the VTube Studio avatar:
   the server sends parameter frames ("avatar_frame", 20 a second) while
   this Stage is open. Without frames (server down, or not yet) a local
   idle keeps her moving. A click on the model is a "poke", a drag is a
   "drag" (sent as avatar_touch); the reply's flinch plays here as a
   decaying nudge on top of the frames.

   mount(host, { socket }) -> a cleanup function, or null when there is
   no Live2D model configured (the caller then tries the VRM stage).
   With a model but no Cubism Core, the host says what to download.
   ============================================================ */

const VENDOR = '/web/vendor';
const CORE_SRC = `${VENDOR}/live2dcubismcore.min.js`;   // Live2D's own runtime: a classic script, loaded first
const FPS = 30;
const FRAME_STALE_MS = 1000;                             // no frame for this long: back to the local idle
const FLINCH_TAU_S = 0.35;
// What the reply's flinch channels move on the model (avatar_touch.py), and how far (degrees).
const FLINCH_PARAM = { head_tilt: 'ParamAngleZ', body_bounce: 'ParamBodyAngleY', lean: 'ParamBodyAngleX' };
const FLINCH_SCALE = 10;

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const existing = document.querySelector(`script[data-src="${src}"]`);
    if (existing) {
      if (existing.dataset.ok) return resolve();
      existing.addEventListener('load', () => resolve());
      existing.addEventListener('error', () => reject(new Error(src)));
      return;
    }
    const s = document.createElement('script');
    s.src = src;
    s.dataset.src = src;
    s.onload = () => { s.dataset.ok = '1'; resolve(); };
    s.onerror = () => reject(new Error(src));
    document.head.append(s);
  });
}

function note(host, text) {
  const p = document.createElement('p');
  p.className = 'stage-note';
  p.textContent = text;
  host.replaceChildren(p);
  return () => {};
}

// Cubism Core first (a classic script), then the Pixi 7 and Live2D modules, which the
// import map in index.html points at web/vendor. Pixi and the plugin share one copy.
let libsReady = null;
function loadLibs() {
  if (!libsReady) {
    libsReady = loadScript(CORE_SRC).then(() =>
      Promise.all([import('pixi.js'), import('pixi-live2d-display-advanced/cubism4')])
        .then(([PIXI, L2D]) => ({ PIXI, Live2DModel: L2D.Live2DModel })));
  }
  return libsReady;
}

export async function mount(host, opts = {}) {
  if (!host) return null;
  const socket = opts.socket || null;
  let st;
  try {
    const r = await fetch('/api/avatar/live2d/status', { credentials: 'same-origin', cache: 'no-store' });
    if (!r.ok) return null;
    st = await r.json();
  } catch (_) {
    return null;
  }
  if (!st.model) return null;                          // no Live2D model configured
  if (!st.core) {
    return note(host, 'The Live2D model is set up, but its Cubism Core file is missing. Download "Cubism SDK for Web" from live2d.com and put live2dcubismcore.min.js in web\\vendor on this PC, then reload.');
  }

  host.replaceChildren(Object.assign(document.createElement('p'), { className: 'stage-note', textContent: 'Loading the avatar…' }));
  let PIXI, Live2DModel;
  try {
    ({ PIXI, Live2DModel } = await loadLibs());
  } catch (_) {
    return note(host, 'The avatar libraries are missing. Run npm install in web\\vendor on this PC.');
  }

  const canvas = document.createElement('canvas');
  host.replaceChildren(canvas);
  const app = new PIXI.Application({
    view: canvas,
    backgroundAlpha: 0,
    antialias: true,
    autoDensity: true,
    resolution: Math.min(window.devicePixelRatio || 1, 1.5),
    resizeTo: host,
  });
  app.ticker.maxFPS = FPS;

  let model;
  try {
    model = await Live2DModel.from(`/api/avatar/live2d/file/${encodeURIComponent(st.entry)}`);
    // Cubism Core 6 keeps the draw order on the model; the plugin (built for Cubism 4) reads it
    // from drawables. Copy it across, or every frame crashes on the first drawable.
    const cm = model.internalModel.coreModel._model;
    if (cm && cm.drawables && !cm.drawables.renderOrders) cm.drawables.renderOrders = cm.renderOrders;
  } catch (_) {
    app.destroy(true);
    return note(host, 'The Live2D model couldn’t be loaded. Check the model folder in data/avatars/live2d.json.');
  }
  app.stage.addChild(model);

  // Fit against the model's natural size, not model.width: that already includes the last scale,
  // so measuring it would grow the model on every resize.
  const fit = () => {
    const w = app.screen.width, h = app.screen.height;
    const bw = model.internalModel.originalWidth, bh = model.internalModel.originalHeight;
    const scale = Math.min(w / bw, h / bh) * 0.95;
    model.scale.set(scale);
    model.x = (w - bw * scale) / 2;
    model.y = (h - bh * scale) / 2;
  };
  fit();
  const ro = new ResizeObserver(() => fit());
  ro.observe(host);

  // ---- the face: server frames, else the local idle ----
  const core = model.internalModel.coreModel;
  let frame = null, frameAt = 0;
  const impulses = [];                                  // flinches: {param, amount, start}
  const t0 = performance.now();

  const onFrame = (msg) => {
    if (msg && msg.p) { frame = msg.p; frameAt = performance.now(); }
  };
  const onReaction = (r) => {
    if (!r || !r.flinch) return;
    const now = performance.now();
    for (const [k, v] of Object.entries(r.flinch)) {
      const param = FLINCH_PARAM[k];
      if (param) impulses.push({ param, amount: v * FLINCH_SCALE, start: now });
    }
  };

  model.internalModel.on('beforeModelUpdate', () => {
    const now = performance.now();
    if (frame && now - frameAt < FRAME_STALE_MS) {
      for (const [id, v] of Object.entries(frame)) core.setParameterValueById(id, v);
    } else {
      const s = (now - t0) / 1000;                      // local idle: slow sway and breathing
      core.setParameterValueById('ParamAngleX', 4 * Math.sin(s * 0.5) + 1.5 * Math.sin(s * 1.7 + 1));
      core.setParameterValueById('ParamAngleY', 2 * Math.sin(s * 0.7 + 2));
      core.setParameterValueById('ParamAngleZ', 1.5 * Math.sin(s * 0.37));
      core.setParameterValueById('ParamBodyAngleX', 1.2 * Math.sin(s * 1.3));
    }
    for (let i = impulses.length - 1; i >= 0; i--) {
      const im = impulses[i];
      const v = im.amount * Math.exp(-((now - im.start) / 1000) / FLINCH_TAU_S);
      if (v < 0.05) { impulses.splice(i, 1); continue; }
      core.setParameterValueById(im.param, core.getParameterValueById(im.param) + v);
    }
  });

  // ---- touch: a click is a poke, a drag is a drag; the zone is from where the press began ----
  let press = null;
  const onDown = (e) => {
    const b = model.getBounds();
    const rel = b.height ? (e.offsetY - b.y) / b.height : 0.5;
    press = { x: e.offsetX, y: e.offsetY, moved: false, zone: rel < 0.28 ? 'head' : rel < 0.7 ? 'body' : null };
  };
  const onUp = () => {
    if (!press) return;
    const p = press;
    press = null;
    if (p.zone && socket) socket.emit('avatar_touch', { zone: p.zone, action: p.moved ? 'drag' : 'poke' });
  };
  // Eyes and head follow the pointer while it's over the stage.
  const onMove = (e) => {
    if (press && !press.moved && Math.hypot(e.offsetX - press.x, e.offsetY - press.y) > 8) press.moved = true;
    model.focus(e.clientX, e.clientY);
  };
  canvas.addEventListener('pointerdown', onDown);
  canvas.addEventListener('pointermove', onMove);
  canvas.addEventListener('pointerup', onUp);
  canvas.addEventListener('pointerleave', () => { press = null; });

  if (socket) {
    socket.on('avatar_frame', onFrame);
    socket.on('avatar_reaction', onReaction);
    socket.emit('avatar_stage_on');
  }

  const onVis = () => { if (document.hidden) app.ticker.stop(); else app.ticker.start(); };
  document.addEventListener('visibilitychange', onVis);

  return () => {
    document.removeEventListener('visibilitychange', onVis);
    canvas.removeEventListener('pointerdown', onDown);
    canvas.removeEventListener('pointermove', onMove);
    canvas.removeEventListener('pointerup', onUp);
    ro.disconnect();
    if (socket) {
      socket.emit('avatar_stage_off');
      socket.off('avatar_frame', onFrame);
      socket.off('avatar_reaction', onReaction);
    }
    app.destroy(true, { children: true });
  };
}

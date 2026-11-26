/* ============================================================
   The Live2D avatar on the web client (Phase 3, Stage).
   Renders a Cubism 4 model (.model3.json) with Pixi 6 and
   pixi-live2d-display, both from web/vendor. The model comes from the
   folder named in data/avatars/live2d.json (served by
   /api/avatar/live2d/file/...). Live2D's Cubism Core is not ours to
   ship: you download it from Live2D and put it at
   web/vendor/live2dcubismcore.min.js.

   mount(host) -> a cleanup function, or null when there is no Live2D
   model configured (the caller then tries the VRM stage). With a model
   but no Cubism Core, the host says what to download.
   ============================================================ */

const VENDOR = '/web/vendor';
const CORE_SRC = `${VENDOR}/live2dcubismcore.min.js`;   // Live2D's own runtime: a classic script, loaded first
const FPS = 30;

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

// Cubism 6 Core first (a classic script), then the Pixi 7 and Live2D modules, which the
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

export async function mount(host) {
  if (!host) return null;
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

  // Idle: a slow head sway and breathing, written before each frame (layered sines, never quite repeats).
  const core = model.internalModel.coreModel;
  let t0 = performance.now();
  model.internalModel.on('beforeModelUpdate', () => {
    const s = (performance.now() - t0) / 1000;
    core.setParameterValueById('ParamAngleX', 4 * Math.sin(s * 0.5) + 1.5 * Math.sin(s * 1.7 + 1));
    core.setParameterValueById('ParamAngleY', 2 * Math.sin(s * 0.7 + 2));
    core.setParameterValueById('ParamAngleZ', 1.5 * Math.sin(s * 0.37));
    core.setParameterValueById('ParamBodyAngleX', 1.2 * Math.sin(s * 1.3));
  });

  // Eyes and head follow the pointer while it's over the stage.
  const onMove = (e) => model.focus(e.clientX, e.clientY);
  canvas.addEventListener('pointermove', onMove);

  const onVis = () => { if (document.hidden) app.ticker.stop(); else app.ticker.start(); };
  document.addEventListener('visibilitychange', onVis);

  return () => {
    document.removeEventListener('visibilitychange', onVis);
    canvas.removeEventListener('pointermove', onMove);
    ro.disconnect();
    app.destroy(true, { children: true });
  };
}

/* ============================================================
   The Stage on the Island: Lumi's Live2D model in the Home view.
   The model and the Live2D runtime come from Lumi over the Island's own
   socket (modules/island_stage.py: stage_manifest, stage_chunk), so the
   Island needs no web session and no copy of the model on disk. Each file
   is loaded as a blob URL. Motion is local (idle sway, breathing, blinks,
   and a flinch when you click the model), so it doesn't need VTube Studio.

   IslandStage.init({ request, container, card })   request(type, data) -> reply
   IslandStage.show()    load on first show, then animate
   IslandStage.hide()    stop animating (the model stays loaded)
   ============================================================ */
(() => {
  'use strict';

  const FPS = 30;
  const FRAME_MS = 1000 / FPS;
  const FLINCH_TAU_S = 0.35;
  const SWAY_DEG = 4;                         // the Stage's own sway, as before the server frames
  const KEY_PREFIX = 'model/';

  let request = null, container = null, card = null;
  let loading = null, app = null, model = null, loaded = false, unavailable = false;
  let impulses = [], t0 = 0;

  function note(text) {
    if (!container) return;
    const p = document.createElement('p');
    p.className = 'stage-note';
    p.textContent = text;
    container.replaceChildren(p);
  }

  function b64ToBytes(b64) {
    const bin = atob(b64);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  // Every chunk of one file, joined into a Blob with the right type.
  async function fetchFile(key, size, progress) {
    const parts = [];
    let offset = 0;
    while (offset < size || (size === 0 && parts.length === 0)) {
      const r = await request('stage_chunk', { key, offset });
      if (!r || !r.ok) throw new Error((r && r.error) || `couldn't fetch ${key}`);
      const bytes = b64ToBytes(r.data);
      parts.push(bytes);
      offset += bytes.length;
      progress(bytes.length);
      if (r.done || bytes.length === 0) break;
    }
    return parts;
  }

  const TYPES = { json: 'application/json', png: 'image/png', jpg: 'image/jpeg',
                  js: 'text/javascript', moc3: 'application/octet-stream' };
  const typeOf = (key) => TYPES[key.split('.').pop().toLowerCase()] || 'application/octet-stream';

  function loadScript(url) {
    return new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = url;
      s.onload = () => resolve();
      s.onerror = () => reject(new Error('a Live2D script failed to load'));
      document.head.append(s);
    });
  }

  async function load() {
    const m = await request('stage_manifest', {});
    if (!m || !m.ok || !m.model) { unavailable = true; if (card) card.hidden = true; return; }
    if (card) card.hidden = false;
    if (!m.core) return note('Lumi’s Live2D Cubism Core file is missing on this PC, so the Island can’t draw the model.');

    const modelFiles = m.files.filter((f) => f.key.startsWith(KEY_PREFIX));   // the runtime is fetched below
    const total = modelFiles.reduce((n, f) => n + f.size, 0) || 1;
    let got = 0;
    const blobs = {};
    for (const f of modelFiles) {
      const parts = await fetchFile(f.key, f.size, (n) => {
        got += n;
        note(`Loading the avatar… ${Math.round((got / total) * 100)}%`);
      });
      blobs[f.key] = new Blob(parts, { type: typeOf(f.key) });
    }
    // The loader takes the model as File objects whose webkitRelativePath is the model's own
    // path ("model/ariu.model3.json", "model/ariu.2048/texture_00.png"), so the names the model
    // uses resolve without any rewriting.
    const loadable = Object.keys(blobs).map((key) => {
      const f = new File([blobs[key]], key.split('/').pop(), { type: typeOf(key) });
      Object.defineProperty(f, 'webkitRelativePath', { value: key });
      return f;
    });

    // The runtime: Cubism Core, Pixi 7, pixi-sound, then pixi-live2d-display (classic UMD scripts, in order).
    for (const name of ['core', 'pixi', 'sound', 'live2d']) {
      const key = `vendor/${name}`;
      if (!m.files.some((f) => f.key === key)) return note('The Live2D runtime isn’t on Lumi’s PC yet (web/vendor).');
      const parts = await fetchFile(key, m.files.find((f) => f.key === key).size, () => {});
      await loadScript(URL.createObjectURL(new Blob(parts, { type: 'text/javascript' })));
    }
    const PIXI = window.PIXI;
    const Live2DModel = PIXI && PIXI.live2d && PIXI.live2d.Live2DModel;
    if (!Live2DModel) return note('The Live2D runtime loaded without its model class.');

    const canvas = document.createElement('canvas');
    container.replaceChildren(canvas);
    app = new PIXI.Application({ view: canvas, backgroundAlpha: 0, antialias: true,
                                 autoDensity: true, resizeTo: container });
    app.ticker.maxFPS = FPS;
    model = await Live2DModel.from(loadable);
    // Cubism Core 6 keeps the draw order on the model; the plugin (built for Cubism 4) reads it
    // from drawables. Copy it across, or every frame crashes on the first drawable (as in web/live2d-stage.js).
    const cm = model.internalModel.coreModel._model;
    if (cm && cm.drawables && !cm.drawables.renderOrders) cm.drawables.renderOrders = cm.renderOrders;
    app.stage.addChild(model);
    fit();
    new ResizeObserver(fit).observe(container);
    model.internalModel.on('beforeModelUpdate', motion);
    canvas.addEventListener('pointerdown', (e) => {
      const b = model.getBounds();
      const rel = b.height ? (e.offsetY - b.y) / b.height : 0.5;
      if (rel >= 0 && rel < 0.7) impulses.push({ param: rel < 0.28 ? 'ParamAngleZ' : 'ParamBodyAngleY', amount: 8, start: performance.now() });
    });
    loaded = true;
    t0 = performance.now();
  }

  function fit() {
    if (!model || !app) return;
    const w = app.screen.width, h = app.screen.height;
    const bw = model.internalModel.originalWidth, bh = model.internalModel.originalHeight;
    const scale = Math.min(w / bw, h / bh) * 0.95;
    model.scale.set(scale);
    model.x = (w - bw * scale) / 2;
    model.y = (h - bh * scale) / 2;
  }

  // Local motion, set before each model update: sway, breathing, blinks, touch flinches.
  let blinkStart = -1, nextBlink = 2.5;
  function motion() {
    const core = model.internalModel.coreModel;
    const s = (performance.now() - t0) / 1000;
    core.setParameterValueById('ParamAngleX', SWAY_DEG * Math.sin(s * 0.5) + 1.5 * Math.sin(s * 1.7 + 1));
    core.setParameterValueById('ParamAngleY', 2 * Math.sin(s * 0.7 + 2));
    core.setParameterValueById('ParamAngleZ', 1.5 * Math.sin(s * 0.37));
    core.setParameterValueById('ParamBodyAngleX', 1.2 * Math.sin(s * 1.3));
    core.setParameterValueById('ParamBreath', 0.5 + 0.5 * Math.sin(s * 1.3));
    if (blinkStart < 0 && s >= nextBlink) blinkStart = s;
    let open = 1;
    if (blinkStart >= 0) {
      const k = (s - blinkStart) / 0.16;
      if (k >= 1) { blinkStart = -1; nextBlink = s + 2.5 + Math.random() * 4; } else open = 1 - Math.sin(Math.PI * k);
    }
    core.setParameterValueById('ParamEyeLOpen', open);
    core.setParameterValueById('ParamEyeROpen', open);
    const now = performance.now();
    impulses = impulses.filter((im) => {
      const v = im.amount * Math.exp(-((now - im.start) / 1000) / FLINCH_TAU_S);
      if (v < 0.05) return false;
      core.setParameterValueById(im.param, core.getParameterValueById(im.param) + v);
      return true;
    });
  }

  window.IslandStage = {
    init(opts) { request = opts.request; container = opts.container; card = opts.card || null; },
    show() {
      if (unavailable || !request || !container) return;
      if (loaded) { if (app) app.ticker.start(); return; }
      if (!loading) loading = load().catch((e) => { note(`The avatar couldn’t load: ${e.message}`); loading = null; });
    },
    hide() { if (app) app.ticker.stop(); },
  };
})();

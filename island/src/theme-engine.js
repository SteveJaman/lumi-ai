/*
 * Lumi theme engine. Shared by the web app (web/theme-engine.js) and Lumi Island
 * (island/src/theme-engine.js, copied by scripts/sync_themes.py).
 *
 * A theme is a folder under themes/:
 *   theme.json      manifest (schema: themes/README.md): palette, fonts, background,
 *                   cursor, Pipeline room lighting
 *   style.css       the look; scoped by html[data-lumi-theme="<id>"] and
 *                   [data-surface="web" | "island"]
 *   background.js   optional animated canvas: LumiThemes.registerBackground(id, factory)
 *   assets/         images it uses
 *
 *   LumiThemes.init({ base: '/web/themes/', surface: 'web', host: document.body })
 *   LumiThemes.apply('sakura')            -> Promise
 *   window.addEventListener('lumi-theme', (e) => e.detail.manifest)   // Pipeline relights
 *
 * Performance: background and cursor canvases draw at most 30 fps, stop while the
 * page is hidden (minimised window, background tab), render one still frame for
 * prefers-reduced-motion, and the sparkle cursor only runs while sparkles are alive.
 */
(function () {
  'use strict';

  const MAX_FPS = 30;
  const LAST_KEY = 'lumi-theme-last';
  const S = {
    base: 'themes/', surface: 'web', host: null, current: null, manifest: null,
    manifests: new Map(), scripts: new Map(), factories: new Map(),
    bg: null, cursor: null, link: null, paletteKeys: [],
  };
  const reducedMotion = () => window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // ── a 30 fps loop that sleeps while hidden ─────────────────────────
  function makeLoop(draw, fps) {
    const minMs = 1000 / Math.min(MAX_FPS, fps || MAX_FPS);
    let raf = 0, timer = 0, last = 0, running = false, t0 = performance.now();
    const tick = (now) => {
      raf = 0;
      if (!running || document.hidden) return;
      const dt = last ? Math.min(0.1, (now - last) / 1000) : 0;
      last = now;
      const keep = draw((now - t0) / 1000, dt);
      if (keep === false) { running = false; return; }
      timer = setTimeout(() => { raf = requestAnimationFrame(tick); }, Math.max(0, minMs - 4));
    };
    const loop = {
      start() { if (running) return; running = true; last = 0; raf = requestAnimationFrame(tick); },
      stop() { running = false; cancelAnimationFrame(raf); clearTimeout(timer); raf = 0; timer = 0; },
      get running() { return running; },
    };
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && running && !raf && !timer) { last = 0; raf = requestAnimationFrame(tick); }
    });
    return loop;
  }

  function sizeCanvas(c, maxDpr) {
    const dpr = Math.min(window.devicePixelRatio || 1, maxDpr || 1.5);
    const r = c.getBoundingClientRect();
    const w = Math.max(1, Math.round(r.width * dpr)), h = Math.max(1, Math.round(r.height * dpr));
    if (c.width !== w || c.height !== h) { c.width = w; c.height = h; return true; }
    return false;
  }

  // ── background canvas ──────────────────────────────────────────────
  function stopBackground() {
    if (!S.bg) return;
    S.bg.loop.stop();
    if (S.bg.instance && S.bg.instance.destroy) { try { S.bg.instance.destroy(); } catch (_) {} }
    S.bg.canvas.remove();
    window.removeEventListener('resize', S.bg.onResize);
    S.bg = null;
  }

  function startBackground(id, manifest) {
    stopBackground();
    const spec = manifest.background;
    const factory = S.factories.get(id);
    if (!spec || !factory) return;
    const canvas = document.createElement('canvas');
    canvas.id = 'lumi-bg';
    canvas.setAttribute('aria-hidden', 'true');
    S.host.prepend(canvas);
    const ctx = canvas.getContext('2d');
    const env = { palette: manifest.palette || {}, options: spec.options || {}, surface: S.surface };
    let instance;
    try { instance = factory(canvas, ctx, env) || {}; } catch (e) { console.error('[theme] background failed', e); canvas.remove(); return; }
    const draw = (t, dt) => {
      if (sizeCanvas(canvas, spec.maxDpr) && instance.resize) instance.resize(canvas.width, canvas.height);
      instance.frame && instance.frame(t, dt, canvas.width, canvas.height);
      return !reducedMotion();                      // reduced motion: one still frame
    };
    const loop = makeLoop(draw, spec.fps);
    const onResize = () => { if (!loop.running) draw(0, 0); };
    window.addEventListener('resize', onResize);
    S.bg = { canvas, loop, instance, onResize };
    loop.start();
  }

  // ── sparkle cursor ─────────────────────────────────────────────────
  function setCursor(kind, palette) {
    if (S.cursor) { S.cursor.off(); S.cursor = null; }
    if (kind !== 'sparkle' || reducedMotion()) return;
    const canvas = document.createElement('canvas');
    canvas.id = 'lumi-cursor';
    canvas.setAttribute('aria-hidden', 'true');
    document.body.appendChild(canvas);
    const ctx = canvas.getContext('2d');
    const colors = [palette.primary, palette.secondary, palette.glow, palette.accent].filter(Boolean);
    const parts = [];
    let dpr = 1;
    const draw = (t, dt) => {
      if (sizeCanvas(canvas, 1.5)) dpr = canvas.width / Math.max(1, canvas.getBoundingClientRect().width);
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      for (let i = parts.length - 1; i >= 0; i--) {
        const p = parts[i];
        p.life -= dt;
        if (p.life <= 0) { parts.splice(i, 1); continue; }
        p.x += p.vx * dt; p.y += p.vy * dt; p.vy += 30 * dt; p.rot += p.spin * dt;
        const a = Math.min(1, p.life / p.max * 1.4), s = p.size * (0.4 + p.life / p.max) * dpr;
        ctx.save();
        ctx.translate(p.x * dpr, p.y * dpr); ctx.rotate(p.rot);
        ctx.globalAlpha = a; ctx.fillStyle = p.color;
        ctx.beginPath();                            // four-point star
        for (let k = 0; k < 8; k++) {
          const r = k % 2 ? s * 0.32 : s;
          ctx.lineTo(Math.cos(k * Math.PI / 4) * r, Math.sin(k * Math.PI / 4) * r);
        }
        ctx.closePath(); ctx.fill();
        ctx.restore();
      }
      return parts.length > 0;                      // sleep when no sparkles are alive
    };
    const loop = makeLoop(draw, 30);
    let lastSpawn = 0;
    const onMove = (e) => {
      const now = performance.now();
      if (now - lastSpawn < 24 || parts.length > 70) return;
      lastSpawn = now;
      for (let k = 0; k < 2; k++) {
        parts.push({ x: e.clientX, y: e.clientY, vx: (Math.random() - 0.5) * 60, vy: -20 - Math.random() * 40,
                     rot: Math.random() * 6, spin: (Math.random() - 0.5) * 6, size: 2.5 + Math.random() * 3.5,
                     life: 0.7, max: 0.7, color: colors[(Math.random() * colors.length) | 0] || '#fff' });
      }
      if (!loop.running) loop.start();
    };
    window.addEventListener('pointermove', onMove, { passive: true });
    S.cursor = { off() { loop.stop(); window.removeEventListener('pointermove', onMove); canvas.remove(); } };
  }

  // ── loading ────────────────────────────────────────────────────────
  async function manifestOf(id) {
    if (S.manifests.has(id)) return S.manifests.get(id);
    const r = await fetch(`${S.base}${id}/theme.json`, { cache: 'no-cache' });
    if (!r.ok) throw new Error(`theme ${id}: HTTP ${r.status}`);
    const m = await r.json();
    S.manifests.set(id, m);
    return m;
  }

  function loadScript(id, file) {
    if (S.scripts.has(id)) return S.scripts.get(id);
    const p = new Promise((resolve) => {
      const s = document.createElement('script');
      s.src = `${S.base}${id}/${file}`;
      s.onload = () => resolve(true);
      s.onerror = () => { console.warn(`[theme] ${id}/${file} failed to load`); resolve(false); };
      document.head.appendChild(s);
    });
    S.scripts.set(id, p);
    return p;
  }

  // Resolves true when the stylesheet loaded, false when it failed or took
  // over STYLE_TIMEOUT_MS. No style -> drop the href (an empty href would
  // point the <link> at the page itself).
  const STYLE_TIMEOUT_MS = 4000;
  function loadStyle(href) {
    return new Promise((resolve) => {
      const link = S.link;
      link.onload = link.onerror = null;
      if (!href) { link.removeAttribute('href'); resolve(true); return; }
      const done = (ok) => { clearTimeout(timer); link.onload = link.onerror = null; resolve(ok); };
      const timer = setTimeout(() => done(false), STYLE_TIMEOUT_MS);
      link.onload = () => done(true);
      link.onerror = () => { console.warn(`[theme] ${href} failed to load`); done(false); };
      link.href = href;
    });
  }

  // <html data-theme-ready="<id>|fallback|none"> + a 'lumi-theme-ready' event,
  // once the first theme's stylesheet has settled. The web app's wake-up screen
  // waits for it (briefly) before showing the app. Never blocks first paint.
  function markReady(value) {
    const root = document.documentElement;
    const first = !root.dataset.themeReady;
    root.dataset.themeReady = value;
    if (first) window.dispatchEvent(new CustomEvent('lumi-theme-ready', { detail: { value } }));
  }

  function setPalette(palette) {
    const root = document.documentElement;
    for (const k of S.paletteKeys) root.style.removeProperty(`--lt-${k}`);
    S.paletteKeys = Object.keys(palette || {});
    for (const [k, v] of Object.entries(palette || {})) root.style.setProperty(`--lt-${k}`, v);
  }

  async function apply(id) {
    id = id || 'default';
    let m;
    try { m = await manifestOf(id); } catch (e) {
      console.warn('[theme]', e.message);
      if (id !== 'default') return apply('default');     // unknown or broken theme id: the default look
      markReady('none');                                  // no theme at all: the app's own tokens
      return null;
    }
    const root = document.documentElement;
    root.setAttribute('data-lumi-theme', id);
    root.setAttribute('data-surface', S.surface);
    setPalette(m.palette);
    if (!S.link) {
      S.link = document.createElement('link');
      S.link.rel = 'stylesheet';
      S.link.id = 'lumi-theme-css';
      document.head.appendChild(S.link);
    }
    const styled = loadStyle(m.style ? `${S.base}${id}/${m.style}` : '');
    styled.then((ok) => markReady(ok ? id : 'fallback'));
    if (m.background && m.background.script) await loadScript(id, m.background.script);
    S.current = id; S.manifest = m;
    startBackground(id, m);
    setCursor(m.cursor, m.palette || {});
    try { localStorage.setItem(LAST_KEY, id); } catch (_) {}
    window.dispatchEvent(new CustomEvent('lumi-theme', { detail: { id, manifest: m } }));
    return m;
  }

  function init(opts) {
    S.base = opts.base || S.base;
    S.surface = opts.surface || S.surface;
    S.host = opts.host || document.body;
    let last = null;
    try { last = localStorage.getItem(LAST_KEY); } catch (_) {}
    return apply(opts.initial || last || 'default');   // no flash of the default look on reload
  }

  window.LumiThemes = {
    init, apply,
    registerBackground(id, factory) { S.factories.set(id, factory); },
    current: () => S.current,
    manifest: () => S.manifest,
    manifestOf,
  };
})();

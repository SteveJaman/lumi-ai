/* Sakura: a plum night sky, a soft moon, drifting bokeh and falling petals. */
LumiThemes.registerBackground('sakura', (canvas, ctx, { palette, options, surface }) => {
  const P = palette;
  const small = surface === 'island';
  const petals = [], bokeh = [];
  const nPetals = small ? 14 : (options.petals || 40);
  const nBokeh = small ? 4 : (options.bokeh || 12);
  let W = 1, H = 1, sky = null;
  const rnd = (a, b) => a + Math.random() * (b - a);
  function petal(top) {
    return { x: rnd(0, W), y: top ? rnd(-H * 0.2, 0) : rnd(0, H), s: rnd(5, 11) * (W / 1400 + 0.5),
             vy: rnd(18, 42), vx: rnd(-14, 10), rot: rnd(0, 6.28), spin: rnd(-1.6, 1.6),
             sway: rnd(0, 6.28), col: Math.random() < 0.7 ? P.petal : P.petal2, a: rnd(0.55, 0.95) };
  }
  function resize(w, h) {
    W = w; H = h;
    sky = ctx.createLinearGradient(0, 0, 0, H);
    sky.addColorStop(0, '#0b0614'); sky.addColorStop(0.55, P.bg || '#120a1c'); sky.addColorStop(1, '#2a1033');
    petals.length = 0; bokeh.length = 0;
    for (let i = 0; i < nPetals; i++) petals.push(petal(false));
    for (let i = 0; i < nBokeh; i++) bokeh.push({ x: rnd(0, W), y: rnd(0, H), r: rnd(20, 90) * (W / 1600 + 0.4),
                                                 vx: rnd(-6, 6), vy: rnd(-4, 4), c: [P.primary, P.secondary, P.accent][i % 3],
                                                 ph: rnd(0, 6.28) });
  }
  return {
    resize,
    frame(t, dt, w, h) {
      if (w !== W || h !== H || !sky) resize(w, h);
      ctx.fillStyle = sky; ctx.fillRect(0, 0, W, H);
      // moon with a halo
      const mx = W * 0.82, my = H * 0.16, mr = Math.min(W, H) * (small ? 0.09 : 0.06);
      const halo = ctx.createRadialGradient(mx, my, mr * 0.6, mx, my, mr * 4);
      halo.addColorStop(0, 'rgba(255, 220, 240, 0.30)'); halo.addColorStop(1, 'rgba(255, 220, 240, 0)');
      ctx.fillStyle = halo; ctx.fillRect(mx - mr * 4, my - mr * 4, mr * 8, mr * 8);
      ctx.fillStyle = P.moon || '#fff4e0'; ctx.beginPath(); ctx.arc(mx, my, mr, 0, 6.283); ctx.fill();
      // bokeh
      ctx.globalCompositeOperation = 'lighter';
      for (const b of bokeh) {
        b.x += b.vx * dt; b.y += b.vy * dt;
        if (b.x < -b.r) b.x = W + b.r; if (b.x > W + b.r) b.x = -b.r;
        if (b.y < -b.r) b.y = H + b.r; if (b.y > H + b.r) b.y = -b.r;
        const g = ctx.createRadialGradient(b.x, b.y, 0, b.x, b.y, b.r);
        const a = 0.10 + 0.05 * Math.sin(t * 0.8 + b.ph);
        g.addColorStop(0, b.c + Math.round(a * 255).toString(16).padStart(2, '0')); g.addColorStop(1, 'rgba(0,0,0,0)');
        ctx.fillStyle = g; ctx.beginPath(); ctx.arc(b.x, b.y, b.r, 0, 6.283); ctx.fill();
      }
      ctx.globalCompositeOperation = 'source-over';
      // petals: two-lobed, tumbling as they fall
      for (const p of petals) {
        p.sway += dt * 1.3;
        p.x += (p.vx + Math.sin(p.sway) * 18) * dt; p.y += p.vy * dt; p.rot += p.spin * dt;
        if (p.y > H + 20 || p.x < -30 || p.x > W + 30) Object.assign(p, petal(true));
        ctx.save();
        ctx.translate(p.x, p.y); ctx.rotate(p.rot); ctx.scale(1, 0.55 + 0.45 * Math.abs(Math.sin(p.sway)));
        ctx.globalAlpha = p.a; ctx.fillStyle = p.col;
        ctx.beginPath();
        ctx.moveTo(0, -p.s);
        ctx.bezierCurveTo(p.s * 0.9, -p.s * 0.8, p.s * 0.8, p.s * 0.6, 0, p.s);
        ctx.bezierCurveTo(-p.s * 0.8, p.s * 0.6, -p.s * 0.9, -p.s * 0.8, 0, -p.s);
        ctx.fill();
        ctx.globalAlpha = p.a * 0.5; ctx.fillStyle = '#fff';
        ctx.fillRect(-0.5, -p.s * 0.6, 1, p.s * 0.5);
        ctx.restore();
      }
      ctx.globalAlpha = 1;
    },
  };
});

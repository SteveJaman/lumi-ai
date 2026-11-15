/* Cyberpunk: layered skyline with lit windows, flying cars with light trails, rain. */
LumiThemes.registerBackground('cyberpunk', (canvas, ctx, { palette, options, surface }) => {
  const P = palette;
  const small = surface === 'island';
  let W = 1, H = 1, layers = [], cars = [], drops = [], sky = null;
  const rnd = (a, b) => a + Math.random() * (b - a);

  function skyline(depth) {
    const bs = [];
    const scale = H / 900;
    let x = 0;
    while (x < W) {
      const bw = rnd(40, 120) * scale * (1 + depth);
      const bh = rnd(0.18, 0.55 + depth * 0.25) * H;
      const wins = [];
      for (let wy = H - bh + 10 * scale; wy < H - 8; wy += 9 * scale) {
        for (let wx = x + 5 * scale; wx < x + bw - 6 * scale; wx += 8 * scale) {
          if (Math.random() < 0.22) {
            const c = Math.random() < 0.5 ? P.primary : (Math.random() < 0.6 ? P.secondary : P.accent);
            wins.push([wx, wy, c, rnd(0, 6.28)]);
          }
        }
      }
      bs.push({ x, w: bw, h: bh, wins, sign: Math.random() < 0.25 ? (Math.random() < 0.5 ? P.secondary : P.primary) : null });
      x += bw + rnd(2, 14) * scale;
    }
    return bs;
  }

  function car() {
    const dir = Math.random() < 0.5 ? 1 : -1;
    return { x: dir > 0 ? -60 : W + 60, y: rnd(0.12, 0.6) * H, v: rnd(80, 220) * dir * (W / 1400 + 0.4),
             c: Math.random() < 0.5 ? P.car : P.primary, s: rnd(0.6, 1.3) };
  }

  function resize(w, h) {
    W = w; H = h;
    sky = ctx.createLinearGradient(0, 0, 0, H);
    sky.addColorStop(0, '#02030a'); sky.addColorStop(0.6, '#0a0b22'); sky.addColorStop(1, '#2a0b33');
    layers = [skyline(0), skyline(0.6)];
    cars = Array.from({ length: small ? 3 : (options.cars || 8) }, car);
    cars.forEach((c) => { c.x = rnd(0, W); });
    drops = Array.from({ length: small ? 30 : (options.rain || 110) },
                       () => ({ x: rnd(0, W), y: rnd(0, H), v: rnd(500, 900), l: rnd(8, 18) }));
  }

  return {
    resize,
    frame(t, dt, w, h) {
      if (w !== W || h !== H || !sky) resize(w, h);
      ctx.fillStyle = sky; ctx.fillRect(0, 0, W, H);
      layers.forEach((bs, li) => {
        const fill = li === 0 ? '#0b1026' : '#05070f';
        for (const b of bs) {
          ctx.fillStyle = fill;
          ctx.fillRect(b.x, H - b.h, b.w, b.h);
          if (li !== 1) continue;
          for (const [wx, wy, c, ph] of b.wins) {
            if (Math.sin(t * 0.7 + ph) > -0.6) { ctx.fillStyle = c; ctx.globalAlpha = 0.75; ctx.fillRect(wx, wy, 3, 2); }
          }
          ctx.globalAlpha = 1;
          if (b.sign) {
            ctx.fillStyle = b.sign;
            ctx.globalAlpha = Math.sin(t * 3 + b.x) > -0.85 ? 0.9 : 0.25;
            ctx.fillRect(b.x + b.w * 0.2, H - b.h + 12, b.w * 0.6, 4);
            ctx.globalAlpha = 1;
          }
        }
      });
      ctx.globalCompositeOperation = 'lighter';
      for (const c of cars) {
        c.x += c.v * dt;
        if ((c.v > 0 && c.x > W + 80) || (c.v < 0 && c.x < -80)) Object.assign(c, car());
        const len = Math.abs(c.v) * 0.35, dir = Math.sign(c.v);
        const g = ctx.createLinearGradient(c.x, 0, c.x - dir * len, 0);
        g.addColorStop(0, c.c); g.addColorStop(1, 'rgba(0,0,0,0)');
        ctx.fillStyle = g; ctx.fillRect(Math.min(c.x, c.x - dir * len), c.y - 1, len, 2 * c.s);
        ctx.fillStyle = '#ffffff'; ctx.fillRect(c.x - 6 * c.s, c.y - 2 * c.s, 12 * c.s, 4 * c.s);
        ctx.fillStyle = c.c; ctx.fillRect(c.x - 7 * c.s, c.y + 2 * c.s, 14 * c.s, 1.5 * c.s);
      }
      ctx.strokeStyle = P.rain; ctx.globalAlpha = 0.18; ctx.lineWidth = 1;
      ctx.beginPath();
      for (const d of drops) {
        d.y += d.v * dt; d.x -= d.v * 0.12 * dt;
        if (d.y > H) { d.y = -d.l; d.x = rnd(0, W * 1.1); }
        ctx.moveTo(d.x, d.y); ctx.lineTo(d.x + d.l * 0.12, d.y - d.l);
      }
      ctx.stroke();
      ctx.globalAlpha = 1; ctx.globalCompositeOperation = 'source-over';
    },
  };
});

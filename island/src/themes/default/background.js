/* Lumi: a deep ink-blue night with slow teal/ice aurora ribbons, motes of light
   rising, a few faint floating halos (her motif) and, now and then, one of her
   white lilies drifting by. In the web app's light mode it draws a pale-ice
   version instead. */
LumiThemes.registerBackground('default', (canvas, ctx, { palette, options, surface }) => {
  const P = palette;
  const small = surface === 'island';
  const rnd = (a, b) => a + Math.random() * (b - a);
  let W = 1, H = 1, light = false, sky = null;
  let motes = [], halos = [], lilies = [];

  const isLight = () => document.documentElement.getAttribute('data-theme') === 'light';
  const hexA = (hex, a) => {
    const n = parseInt(hex.slice(1), 16);
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
  };

  function mote(anywhere) {
    return { x: rnd(0, W), y: anywhere ? rnd(0, H) : H + rnd(0, 40), r: rnd(1.2, 3.2) * (W / 1600 + 0.6),
             vy: rnd(-14, -32), sway: rnd(0, 6.28), c: Math.random() < 0.6 ? P.ice : (Math.random() < 0.7 ? P.mint : P.peach),
             tw: rnd(0, 6.28) };
  }
  function halo() {
    const r = rnd(26, 60) * (W / 1600 + 0.5);
    return { x: rnd(0, W), y: rnd(H * 0.1, H * 0.8), r, vx: rnd(-6, 6), vy: rnd(-4, 2), tilt: rnd(0.18, 0.32),
             ph: rnd(0, 6.28) };
  }
  function lily(start) {
    const s = rnd(7, 12) * (W / 1600 + 0.6);
    return { x: start ? rnd(0, W) : -40, y: rnd(H * 0.15, H * 0.85), s, vx: rnd(10, 22), rot: rnd(0, 6.28),
             spin: rnd(-0.3, 0.3), bob: rnd(0, 6.28), wait: start ? 0 : rnd(4, 18) };
  }

  function resize(w, h) {
    W = w; H = h;
    light = isLight();
    sky = ctx.createLinearGradient(0, 0, 0, H);
    if (light) {
      sky.addColorStop(0, '#F4FBFC'); sky.addColorStop(0.6, '#E8F6F7'); sky.addColorStop(1, '#F7EFEC');
    } else {
      sky.addColorStop(0, '#04060E'); sky.addColorStop(0.55, P.bg || '#060913'); sky.addColorStop(1, '#0B1528');
    }
    motes = Array.from({ length: small ? 18 : (options.motes || 50) }, () => mote(true));
    halos = Array.from({ length: small ? 1 : (options.halos || 3) }, halo);
    lilies = Array.from({ length: small ? 1 : (options.lilies || 2) }, (_, i) => lily(i === 0));
  }

  function aurora(t) {
    const bands = [
      { c: P.mint, y: 0.30, amp: 0.07, f: 0.9, sp: 0.05, a: light ? 0.16 : 0.26 },
      { c: P.ice, y: 0.42, amp: 0.05, f: 1.4, sp: -0.035, a: light ? 0.13 : 0.18 },
      { c: P.peach, y: 0.58, amp: 0.04, f: 0.7, sp: 0.025, a: light ? 0.12 : 0.09 },
    ];
    for (const b of bands) {
      const g = ctx.createLinearGradient(0, H * (b.y - 0.12), 0, H * (b.y + 0.14));
      g.addColorStop(0, hexA(b.c, 0)); g.addColorStop(0.5, hexA(b.c, b.a)); g.addColorStop(1, hexA(b.c, 0));
      ctx.fillStyle = g;
      ctx.beginPath();
      const step = Math.max(8, W / 60);
      ctx.moveTo(0, H);
      for (let x = 0; x <= W + step; x += step) {
        const u = x / W;
        const y = H * (b.y + b.amp * Math.sin(u * 6.28 * b.f + t * b.sp * 6.28) + 0.02 * Math.sin(u * 17 + t * 0.4));
        ctx.lineTo(x, y);
      }
      ctx.lineTo(W, H);
      ctx.closePath();
      ctx.fill();
    }
  }

  function drawLily(l) {
    ctx.save();
    ctx.translate(l.x, l.y + Math.sin(l.bob) * 6);
    ctx.rotate(l.rot);
    ctx.globalAlpha = light ? 0.85 : 0.7;
    ctx.fillStyle = light ? '#FFFFFF' : P.lily;
    for (let k = 0; k < 6; k++) {                    // six petals: Allium triquetrum
      ctx.rotate(Math.PI / 3);
      ctx.beginPath();
      ctx.ellipse(0, -l.s * 0.75, l.s * 0.32, l.s * 0.75, 0, 0, 6.283);
      ctx.fill();
    }
    ctx.fillStyle = '#E8F27A';
    ctx.beginPath(); ctx.arc(0, 0, l.s * 0.22, 0, 6.283); ctx.fill();
    ctx.restore();
    ctx.globalAlpha = 1;
  }

  return {
    resize,
    frame(t, dt, w, h) {
      if (w !== W || h !== H || !sky || isLight() !== light) resize(w, h);
      ctx.fillStyle = sky; ctx.fillRect(0, 0, W, H);
      aurora(t);

      // halos: thin tilted rings with a soft glow
      for (const o of halos) {
        o.x += o.vx * dt; o.y += o.vy * dt; o.ph += dt * 0.8;
        if (o.x < -o.r * 2) o.x = W + o.r; if (o.x > W + o.r * 2) o.x = -o.r;
        if (o.y < -o.r * 2) o.y = H + o.r; if (o.y > H + o.r * 2) o.y = -o.r;
        const a = (light ? 0.45 : 0.42) + 0.14 * Math.sin(o.ph);
        ctx.strokeStyle = hexA(P.mint, a);
        ctx.lineWidth = Math.max(1.5, o.r * 0.09);
        ctx.shadowColor = hexA(P.ice, a); ctx.shadowBlur = o.r * 0.5;
        ctx.beginPath(); ctx.ellipse(o.x, o.y, o.r, o.r * o.tilt, 0, 0, 6.283); ctx.stroke();
        ctx.shadowBlur = 0;
      }

      // motes of light rising and twinkling
      ctx.globalCompositeOperation = light ? 'source-over' : 'lighter';
      for (const m of motes) {
        m.y += m.vy * dt; m.sway += dt; m.tw += dt * 2.2;
        m.x += Math.sin(m.sway) * 6 * dt;
        if (m.y < -10) Object.assign(m, mote(false));
        const a = (light ? 0.45 : 0.55) * (0.55 + 0.45 * Math.sin(m.tw));
        ctx.fillStyle = hexA(m.c, a);
        ctx.beginPath(); ctx.arc(m.x, m.y, m.r, 0, 6.283); ctx.fill();
        ctx.fillStyle = hexA(m.c, a * 0.18);
        ctx.beginPath(); ctx.arc(m.x, m.y, m.r * 3.2, 0, 6.283); ctx.fill();
      }
      ctx.globalCompositeOperation = 'source-over';

      // a lily now and then, drifting across
      for (const l of lilies) {
        if (l.wait > 0) { l.wait -= dt; continue; }
        l.x += l.vx * dt; l.rot += l.spin * dt; l.bob += dt * 0.9;
        if (l.x > W + 40) Object.assign(l, lily(false));
        drawLily(l);
      }
    },
  };
});

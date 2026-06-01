/* ============================================================
   Satellite Debug Tool — lightweight SVG chart helpers
   Produces multi-series line charts resembling pyqtgraph output.
   ============================================================ */
(function () {
  // deterministic pseudo-random
  function rng(seed) { let s = seed; return () => (s = (s * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff; }

  // ---- waveform generators (resemble real device telemetry) ----
  function gen(kind, n, seed) {
    const r = rng(seed || 7); const out = [];
    for (let i = 0; i < n; i++) {
      const t = i / n;
      let v = 0;
      if (kind === 'roll')  v = Math.sin(t * 22) * 1.5 + (r() - .5) * 0.8;
      else if (kind === 'pitch') v = Math.cos(t * 14 + 1) * 1.2 + (r() - .5) * 0.6 + 0.5;
      else if (kind === 'yaw')   v = 90 + Math.sin(t * 4) * 60 + (t > .6 ? -110 : 0) + (r() - .5) * 6;
      else if (kind === 'ins_yaw') v = (t < .15 ? 120 : t < .25 ? 40 : 175) + Math.sin(t*30)*4 + (t>.55&&t<.62? -180:0) + (r()-.5)*3;
      else if (kind === 'az')    { const saw = ((t * 3) % 1); v = 80 + saw * 240 - (t > .7 ? 0 : 0) + (r() - .5) * 8; if (t>.33&&t<.36) v=320; }
      else if (kind === 'el')    v = 42 + Math.sin(t * 8) * 4 + (r() - .5) * 3;
      else if (kind === 'snr')   { const burst = (t > .18 && t < .32) || t > .72; v = burst ? 9 + Math.sin(t*60)*3 + r()*4 : 1 + r()*0.8; }
      else if (kind === 'snr_avg') { const burst = (t > .18 && t < .32) || t > .72; v = burst ? 8 + Math.sin(t*20)*1.5 : 1 + r()*0.4; }
      else if (kind === 'scan')  v = 0.5 + (r() - .5) * 0.6 + (t > .72 ? Math.sin(t*40)*0.4 : 0);
      else v = Math.sin(t * 18 + seed) * 1 + (r() - .5);
      out.push(v);
    }
    return out;
  }

  function pathFor(pts, w, h, yMin, yMax, padL, padR, padT, padB) {
    const n = pts.length;
    const iw = w - padL - padR, ih = h - padT - padB;
    const sx = i => padL + (i / (n - 1)) * iw;
    const sy = v => padT + ih - ((v - yMin) / (yMax - yMin)) * ih;
    let d = '';
    for (let i = 0; i < n; i++) d += (i ? 'L' : 'M') + sx(i).toFixed(1) + ' ' + sy(pts[i]).toFixed(1) + ' ';
    return d.trim();
  }

  // opts: { width,height, series:[{kind|points,color,seed,width}], yMin,yMax,
  //         title, yTicks, xTicks:[labels], grid, n, pad }
  function lineChart(opts) {
    const w = opts.width, h = opts.height;
    const pad = Object.assign({ l: 34, r: 8, t: 6, b: 16 }, opts.pad || {});
    const n = opts.n || 200;
    const series = opts.series.map(s => ({
      color: s.color, lw: s.width || 1.2,
      points: s.points || gen(s.kind, n, s.seed),
    }));
    let yMin = opts.yMin, yMax = opts.yMax;
    if (yMin == null || yMax == null) {
      let lo = Infinity, hi = -Infinity;
      series.forEach(s => s.points.forEach(v => { lo = Math.min(lo, v); hi = Math.max(hi, v); }));
      const m = (hi - lo) * 0.12 || 1; yMin = lo - m; yMax = hi + m;
    }
    const iw = w - pad.l - pad.r, ih = h - pad.t - pad.b;
    let svg = `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" class="chart-svg" preserveAspectRatio="none" style="display:block">`;
    // grid
    const yt = opts.yTicks || 4;
    for (let i = 0; i <= yt; i++) {
      const y = (pad.t + (i / yt) * ih).toFixed(1);
      const val = (yMax - (i / yt) * (yMax - yMin));
      svg += `<line x1="${pad.l}" y1="${y}" x2="${w - pad.r}" y2="${y}" stroke="var(--grid-line)" stroke-width="1"/>`;
      svg += `<text x="${pad.l - 5}" y="${+y + 3}" text-anchor="end" font-size="8" font-family="var(--font-mono)" fill="var(--text-3)">${Math.abs(val) >= 100 ? val.toFixed(0) : val.toFixed(0)}</text>`;
    }
    // x ticks
    if (opts.xTicks) {
      opts.xTicks.forEach((lb, i) => {
        const x = (pad.l + (i / (opts.xTicks.length - 1)) * iw).toFixed(1);
        svg += `<line x1="${x}" y1="${pad.t}" x2="${x}" y2="${h - pad.b}" stroke="var(--grid-line)" stroke-width="1"/>`;
        svg += `<text x="${x}" y="${h - 4}" text-anchor="middle" font-size="8" font-family="var(--font-mono)" fill="var(--text-3)">${lb}</text>`;
      });
    }
    // series
    series.forEach(s => {
      const d = pathFor(s.points, w, h, yMin, yMax, pad.l, pad.r, pad.t, pad.b);
      svg += `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="${s.lw}" stroke-linejoin="round" vector-effect="non-scaling-stroke" opacity="0.95"/>`;
    });
    svg += `</svg>`;
    return svg;
  }

  window.chartGen = gen;
  window.lineChart = lineChart;
})();

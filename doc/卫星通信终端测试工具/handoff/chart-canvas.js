/* ============================================================
   CanvasChart — themed multi-series line plotter
   Fast enough for live 10Hz streaming. Reads CSS vars for theme.
   ============================================================ */
(function () {
  function cssVar(name, el) {
    return getComputedStyle(el || document.documentElement).getPropertyValue(name).trim() || '#888';
  }

  class CanvasChart {
    constructor(canvas, opts) {
      this.canvas = canvas;
      this.ctx = canvas.getContext('2d');
      this.opts = Object.assign({
        yMin: null, yMax: null, autoY: true, normalize: false,
        padL: 38, padR: 10, padT: 8, padB: 18,
        xLabel: 's', yTicks: 4, win: 600,
      }, opts || {});
      this.series = [];     // {key,color,data:[],visible,realMin,realMax}
      this.xDomain = null;  // [t0,t1] seconds for x labels
      this._resize();
    }
    _resize() {
      const r = this.canvas.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      this.w = Math.max(40, r.width); this.h = Math.max(20, r.height);
      this.canvas.width = this.w * dpr; this.canvas.height = this.h * dpr;
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    setSeries(s) { this.series = s; }
    setXDomain(d) { this.xDomain = d; }
    draw() {
      this._resize();
      const { ctx, w, h, opts } = this;
      const grid = cssVar('--grid-line', this.canvas);
      const txt3 = cssVar('--text-3', this.canvas);
      ctx.clearRect(0, 0, w, h);
      const padL = opts.padL, padR = opts.padR, padT = opts.padT, padB = opts.padB;
      const iw = w - padL - padR, ih = h - padT - padB;
      const vis = this.series.filter(s => s.visible !== false && s.data && s.data.length);

      // y-domain
      let yMin = opts.yMin, yMax = opts.yMax;
      if (opts.normalize) { yMin = 0; yMax = 1; }
      else if (opts.autoY || yMin == null) {
        let lo = Infinity, hi = -Infinity;
        vis.forEach(s => s.data.forEach(v => { if (v < lo) lo = v; if (v > hi) hi = v; }));
        if (!isFinite(lo)) { lo = -1; hi = 1; }
        const m = (hi - lo) * 0.12 || 1; yMin = lo - m; yMax = hi + m;
      }
      this._yMin = yMin; this._yMax = yMax;

      // grid + y labels
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.textBaseline = 'middle'; ctx.fillStyle = txt3; ctx.strokeStyle = grid; ctx.lineWidth = 1;
      const yt = opts.yTicks;
      for (let i = 0; i <= yt; i++) {
        const y = Math.round(padT + (i / yt) * ih) + 0.5;
        ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
        if (!opts.normalize) {
          const val = yMax - (i / yt) * (yMax - yMin);
          ctx.textAlign = 'right';
          ctx.fillText(Math.abs(val) >= 1000 ? (val/1000).toFixed(1)+'k' : val.toFixed(Math.abs(val) < 10 ? 1 : 0), padL - 5, y);
        }
      }
      // x labels
      if (this.xDomain) {
        ctx.textAlign = 'center'; ctx.textBaseline = 'alphabetic';
        const nT = 6;
        for (let i = 0; i <= nT; i++) {
          const x = padL + (i / nT) * iw;
          const t = this.xDomain[0] + (i / nT) * (this.xDomain[1] - this.xDomain[0]);
          ctx.fillText(t.toFixed(t < 100 ? 0 : 0), x, h - 5);
        }
      }

      // series
      const n0 = vis.length ? vis[0].data.length : 0;
      vis.forEach(s => {
        const data = s.data; const n = data.length; if (n < 2) return;
        let lo = yMin, hi = yMax;
        if (opts.normalize) {
          lo = Infinity; hi = -Infinity;
          for (let v of data) { if (v < lo) lo = v; if (v > hi) hi = v; }
          if (hi - lo < 1e-9) hi = lo + 1;
        }
        ctx.beginPath();
        ctx.strokeStyle = s.color; ctx.lineWidth = s.lw || 1.25;
        ctx.lineJoin = 'round'; ctx.lineCap = 'round';
        for (let i = 0; i < n; i++) {
          const x = padL + (i / (n - 1)) * iw;
          const y = padT + ih - ((data[i] - lo) / (hi - lo)) * ih;
          if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        }
        ctx.globalAlpha = 0.95; ctx.stroke(); ctx.globalAlpha = 1;
      });
    }
  }

  window.CanvasChart = CanvasChart;
  window.cssVar = cssVar;
})();

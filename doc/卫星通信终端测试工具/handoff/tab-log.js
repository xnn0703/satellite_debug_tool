/* ============================================================
   Log tab — open WindTerm .log, parse to curves
   ============================================================ */
(function () {
  const App = window.App;
  const ICN = window.svgIcon;
  window.Tabs = window.Tabs || {};
  let view, data = null, charts = [], el = {}, mode = 'grouped', normalize = false;
  const KEYS = [
    { k: 'pll_err', sig: 1 }, { k: 'vco_v', sig: 2 }, { k: 'temp_c', sig: 3 },
    { k: 'agc', sig: 6 }, { k: 'lock_det', sig: 4 }, { k: 'rssi', sig: 9 },
  ];

  function gen() {
    const N = 1820; const out = { n: N, rows: 18234, cols: KEYS.length, data: {}, name: 'trace_20260415.log', size: '12 MB' };
    let ns = {}; const nz = (k, a) => (ns[k] = (ns[k] || 0) * 0.82 + (Math.random() - .5) * a);
    KEYS.forEach(K => out.data[K.k] = []);
    for (let i = 0; i < N; i++) {
      const t = i / N;
      out.data.pll_err.push(Math.sin(t * 40) * 2 + nz('pe', 0.4) + (t > .5 && t < .56 ? 6 : 0));
      out.data.vco_v.push(2.5 + Math.sin(t * 8) * 0.3 + nz('vc', 0.05));
      out.data.temp_c.push(42 + t * 12 + Math.sin(t * 20) * 0.6 + nz('tc', 0.1));
      out.data.agc.push(120 + Math.sin(t * 14) * 30 + nz('ag', 4));
      out.data.lock_det.push(t > .12 && t < .9 ? 1 + nz('ld', 0.04) : nz('ld', 0.1));
      out.data.rssi.push(-78 + Math.sin(t * 10) * 6 + nz('rs', 1.2));
    }
    return out;
  }

  function build() {
    view.innerHTML = `
    <div class="filebar">
      <button class="btn btn--primary" id="lgOpen"><span data-icon="folder" data-size="14"></span>Open .log</button>
      <button class="btn btn--ghost btn--icon" id="lgClear" title="清除"><span data-icon="trash" data-size="14"></span></button>
      <div class="finfo" id="lgInfo"><span class="fnone">未加载文件 — 解析 WindTerm 串口 log 为曲线</span></div>
      <div class="filebar-spacer"></div>
      <div class="seg seg--accent" id="lgMode">
        <button data-mode="single"><span data-icon="grid" data-size="13"></span>单图</button>
        <button data-mode="grouped" class="is-active"><span data-icon="columns" data-size="13"></span>分列</button>
      </div>
      <button class="btn btn--sm" id="lgNorm"><span data-icon="sigma" data-size="12"></span>归一化</button>
    </div>
    <div style="padding:6px 16px 0;font-size:11px;color:var(--text-3)" id="lgHint"></div>
    <div class="charts scroll" id="lgCharts" style="padding-top:12px;flex:1"></div>`;
    window.hydrateIcons(view);
    ['lgInfo','lgCharts','lgHint'].forEach(id => el[id] = view.querySelector('#' + id));
    view.querySelector('#lgOpen').onclick = open;
    view.querySelector('#lgClear').onclick = () => { data = null; el.lgCharts.innerHTML = ''; el.lgHint.textContent = ''; el.lgInfo.innerHTML = '<span class="fnone">未加载文件 — 解析 WindTerm 串口 log 为曲线</span>'; };
    view.querySelector('#lgMode').onclick = (e) => { const b = e.target.closest('[data-mode]'); if (!b) return; mode = b.dataset.mode; [...b.parentElement.children].forEach(x => x.classList.toggle('is-active', x === b)); buildCharts(); };
    view.querySelector('#lgNorm').onclick = (e) => { normalize = !normalize; e.currentTarget.classList.toggle('is-active', normalize); buildCharts(); };
    if (data) { renderInfo(); buildCharts(); }
  }
  function open() {
    view.querySelector('#lgOpen').innerHTML = ICN('refresh', { size: 14 }) + '解析中…';
    setTimeout(() => { data = gen(); view.querySelector('#lgOpen').innerHTML = ICN('folder', { size: 14 }) + 'Open .log'; renderInfo(); buildCharts(); window.toast('已解析 ' + data.name + ' · ' + data.rows.toLocaleString() + ' 行', 'ok'); }, 600);
  }
  function renderInfo() {
    el.lgInfo.innerHTML = `<span class="ficon" data-icon="file" data-size="16"></span><div><div class="fname">${data.name}</div><div class="fmeta">${data.size} · ${data.rows.toLocaleString()} 行 · ${data.cols} 列</div></div>`;
    el.lgHint.textContent = 'X 轴时间 = 行号 × 100ms（占位，收到下位机时间戳后更新）';
    window.hydrateIcons(el.lgInfo);
  }
  function buildCharts() {
    if (!data) return; el.lgCharts.innerHTML = ''; charts = [];
    const xt = ['0', '30', '60', '90', '120', '150', '182'];
    if (mode === 'single') {
      const box = document.createElement('div'); box.className = 'chart-box'; box.style.flex = '1';
      box.innerHTML = `<div class="cb-head"><span class="cb-title">全部列 ${normalize ? '· 归一化' : ''}</span><div class="cb-legend">${KEYS.map(K => `<span><i style="background:var(--sig-${K.sig})"></i>${K.k}</span>`).join('')}</div></div><div style="height:460px"><canvas style="width:100%;height:100%;display:block"></canvas></div>`;
      el.lgCharts.appendChild(box);
      const ch = new window.CanvasChart(box.querySelector('canvas'), { autoY: true, normalize });
      ch.setSeries(KEYS.map(K => ({ key: K.k, color: `var(--sig-${K.sig})`, data: data.data[K.k] })));
      ch.setXDomain([0, 182]); ch.draw(); charts.push(ch);
    } else {
      KEYS.forEach(K => {
        const box = document.createElement('div'); box.className = 'chart-box';
        box.innerHTML = `<div class="cb-head"><span class="cb-title">${K.k}</span><div class="cb-legend"><span><i style="background:var(--sig-${K.sig})"></i>${K.k}</span></div></div><div style="height:120px"><canvas style="width:100%;height:100%;display:block"></canvas></div>`;
        el.lgCharts.appendChild(box);
        const ch = new window.CanvasChart(box.querySelector('canvas'), { autoY: true });
        ch.setSeries([{ key: K.k, color: `var(--sig-${K.sig})`, data: data.data[K.k] }]);
        ch.setXDomain([0, 182]); ch.draw(); charts.push(ch);
      });
    }
  }
  Tabs.log = { mount(v) { view = v; build(); }, unmount() {}, redrawTheme() { if (data) buildCharts(); } };
})();

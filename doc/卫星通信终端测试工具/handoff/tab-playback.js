/* ============================================================
   Playback tab — open .sdb, time scrubber, windowed charts, map
   ============================================================ */
(function () {
  const App = window.App;
  const ICN = window.svgIcon;
  window.Tabs = window.Tabs || {};

  const GROUPS = [
    { g: '姿态', t: 'Attitude', keys: ['yaw','ins_yaw','roll','pitch'], yMin: null, yMax: null },
    { g: '指向', t: 'Pointing', keys: ['ant_az','ant_el'], yMin: null, yMax: null },
    { g: '信号', t: 'Signal', keys: ['snr','snr_avg','snr_raw','scan_loss'], yMin: 0, yMax: 16 },
  ];
  let view, rec = null, win = [0, 1], cursor = 0, playing = false, playRaf = null;
  let charts = [], mapOpen = false, el = {};
  let lmap = null, ltile = null, lcursor = null, levt = [], mapInited = false;

  function genRecording() {
    const dur = 1190.7, N = 1500;
    const data = {}; ['roll','pitch','yaw','ins_yaw','ant_az','ant_el','snr','snr_avg','snr_raw','scan_loss','gps_lat','gps_lon'].forEach(k => data[k] = []);
    const events = []; const gps = [];
    let avg = 1, ns = {}; const nz = (k,a) => (ns[k] = (ns[k]||0)*0.8 + (Math.random()-.5)*a);
    let phase = 'scan', pT = 0, pDur = 80;
    for (let i = 0; i < N; i++) {
      const t = (i / (N - 1)) * dur;
      pT += dur / N;
      if (pT > pDur) { pT = 0; if (phase === 'scan') { phase = 'track'; pDur = 120 + Math.random()*180; events.push({ t, lvl:'ok', code:'LOCK', msg:'LOCK_ACQUIRED' }); } else { phase = 'scan'; pDur = 50 + Math.random()*70; events.push({ t, lvl:'err', code:'LOST', msg:'TRACK_LOST → SCAN' }); } }
      const track = phase === 'track';
      data.roll.push(-0.2 + Math.sin(t*0.05)*1.4 + nz('r',0.2));
      data.pitch.push(0.5 + Math.cos(t*0.04)*1.0 + nz('p',0.2));
      const yaw = 87 + Math.sin(t*0.02)*40 + (track?0:Math.sin(t*0.2)*10);
      data.yaw.push(yaw + nz('y',0.6));
      data.ins_yaw.push((track? yaw : (i%40<20?120:175)) + nz('iy',1.5));
      data.ant_az.push(track ? 271 + nz('aa',0.8) : ((t*30)%360));
      data.ant_el.push(42 + Math.sin(t*0.1)*3 + nz('ae',0.4));
      const base = track ? 9 + Math.sin(t*0.3)*2.5 : 1;
      const snr = Math.max(0, base + nz('s', track?1.2:0.4));
      data.snr.push(snr); avg += (snr-avg)*0.05; data.snr_avg.push(avg);
      data.snr_raw.push(Math.max(0, snr + nz('sr',1.6)));
      data.scan_loss.push(Math.max(0, 0.4 + (track?Math.abs(nz('sl',0.7)):0.1)));
      const lat = 31.2304 + Math.sin(t*0.004)*0.05 + t*4e-6;
      const lon = 121.4737 + Math.cos(t*0.003)*0.06 + t*6e-6;
      data.gps_lat.push(lat); data.gps_lon.push(lon);
      if (i % 6 === 0) gps.push([lat, lon]);
    }
    events.push({ t: 524, lvl:'wrn', code:'SNR', msg:'SNR below 6 dB threshold' });
    return { dur, n: N, data, events, gps, name: 'recording_20260529_220418.sdb', size: '148 MB' };
  }

  function build() {
    view.innerHTML = `
    <div class="filebar">
      <button class="btn btn--primary" id="pbOpen"><span data-icon="folder" data-size="14"></span>Open .sdb</button>
      <button class="btn btn--ghost btn--icon" id="pbClear" title="清除"><span data-icon="trash" data-size="14"></span></button>
      <div class="finfo" id="pbInfo"><span class="fnone">未加载文件 — 打开一个 .sdb 录像</span></div>
      <div class="filebar-spacer"></div>
      <button class="btn" id="pbMap" disabled><span data-icon="map" data-size="13"></span>地图</button>
    </div>
    <div class="scrubwrap" id="pbScrub" style="display:none">
      <div class="scrub-row">
        <button class="btn btn--icon" id="pbPlay"><span data-icon="play" data-size="14"></span></button>
        <div class="scrub-time"><span id="pbCur">00:00.0</span> <span class="mut">/ <span id="pbDur">19:50.7</span></span></div>
        <div class="track" id="pbTrack">
          <div class="rail"></div>
          <div class="winsel" id="pbWin"></div>
          <div class="cursor" id="pbCursor" style="left:0"></div>
        </div>
        <span class="chip"><span data-icon="clock" data-size="11"></span>视窗 <b id="pbWinLen">全部</b></span>
        <button class="btn btn--sm" id="pbAll">全览</button>
      </div>
    </div>
    <div style="flex:1;min-height:0;position:relative;display:flex;flex-direction:column">
      <div class="charts scroll" id="pbCharts" style="padding-top:12px"></div>
      <div class="mapwrap" id="pbMapWrap" style="display:none">
        <div class="mh"><span data-icon="location" data-size="14" style="color:var(--accent)"></span><span class="t">GPS 轨迹</span>
          <div class="mleg"><span><i style="background:var(--ok)"></i>起点</span><span><i style="background:var(--err)"></i>终点</span><span><i style="background:var(--warn)"></i>事件</span></div>
          <span class="ic" id="pbMapClose" data-icon="x" data-size="14" style="margin-left:auto;cursor:pointer;color:var(--text-3)"></span></div>
        <div class="mbody" id="pbMapBody"></div>
      </div>
    </div>`;
    window.hydrateIcons(view);
    ['pbInfo','pbScrub','pbCharts','pbWin','pbCursor','pbCur','pbDur','pbWinLen','pbMapWrap','pbMapBody','pbPlay','pbMap'].forEach(id => el[id] = view.querySelector('#' + id));

    view.querySelector('#pbOpen').onclick = open;
    view.querySelector('#pbClear').onclick = clear;
    view.querySelector('#pbAll').onclick = () => { win = [0, 1]; refresh(); };
    el.pbMap.onclick = () => { mapOpen = !mapOpen; el.pbMapWrap.style.display = mapOpen ? 'flex' : 'none'; if (mapOpen) mapShow(); };
    view.querySelector('#pbMapClose').onclick = () => { mapOpen = false; el.pbMapWrap.style.display = 'none'; };
    el.pbPlay.onclick = togglePlay;
    setupTrack();
    if (rec) { renderInfo(); buildCharts(); refresh(); }
  }

  function open() {
    view.querySelector('#pbOpen').innerHTML = ICN('refresh', { size: 14 }) + '解析中…';
    setTimeout(() => {
      rec = genRecording(); win = [0, 1]; cursor = 0;
      view.querySelector('#pbOpen').innerHTML = ICN('folder', { size: 14 }) + 'Open .sdb';
      el.pbMap.disabled = false;
      renderInfo(); el.pbScrub.style.display = 'flex'; buildCharts(); refresh();
      window.toast('已加载 ' + rec.name, 'ok');
    }, 650);
  }
  function clear() { rec = null; charts = []; el.pbCharts.innerHTML = ''; el.pbScrub.style.display = 'none'; el.pbMap.disabled = true; el.pbMapWrap.style.display = 'none'; mapOpen = false; destroyMap();
    el.pbInfo.innerHTML = '<span class="fnone">未加载文件 — 打开一个 .sdb 录像</span>'; }

  function renderInfo() {
    el.pbInfo.innerHTML = `<span class="ficon" data-icon="file" data-size="16"></span><div><div class="fname">${rec.name}</div><div class="fmeta">${rec.size} · ${fmtClock(rec.dur)} · ${rec.n.toLocaleString()} samples · 12 ch</div></div>`;
    el.pbDur.textContent = fmtClock(rec.dur);
    window.hydrateIcons(el.pbInfo);
  }

  function buildCharts() {
    el.pbCharts.innerHTML = ''; charts = [];
    GROUPS.forEach(G => {
      const box = document.createElement('div'); box.className = 'chart-box';
      box.innerHTML = `<div class="cb-head"><span class="cb-title">${G.g} ${G.t}</span><div class="cb-legend">${G.keys.map(k => `<span><i style="background:${App.color(k)}"></i>${k}</span>`).join('')}</div></div><div style="height:150px"><canvas style="width:100%;height:100%;display:block"></canvas></div>`;
      el.pbCharts.appendChild(box);
      const chart = new window.CanvasChart(box.querySelector('canvas'), { autoY: G.yMin == null, yMin: G.yMin, yMax: G.yMax });
      charts.push({ G, chart });
    });
  }

  function refresh() {
    if (!rec) return;
    const a = Math.floor(win[0] * rec.n), b = Math.ceil(win[1] * rec.n);
    charts.forEach(c => {
      c.chart.setSeries(c.G.keys.map(k => ({ key: k, color: App.color(k), data: rec.data[k].slice(a, b), lw: k === 'snr_avg' ? 1.6 : 1.2 })));
      c.chart.setXDomain([win[0] * rec.dur, win[1] * rec.dur]);
      c.chart.draw();
    });
    el.pbWin.style.left = (win[0] * 100) + '%';
    el.pbWin.style.width = ((win[1] - win[0]) * 100) + '%';
    // event marks
    [...view.querySelectorAll('.evtmark')].forEach(m => m.remove());
    const track = view.querySelector('#pbTrack');
    rec.events.forEach(ev => {
      const m = document.createElement('span'); m.className = 'evtmark';
      m.style.left = (ev.t / rec.dur * 100) + '%';
      m.style.background = ev.lvl === 'err' ? 'var(--err)' : ev.lvl === 'wrn' ? 'var(--warn)' : 'var(--ok)';
      m.title = ev.msg; track.appendChild(m);
    });
    const len = (win[1] - win[0]) * rec.dur;
    el.pbWinLen.textContent = (win[0] === 0 && win[1] === 1) ? '全部' : fmtClock(len);
    if (mapOpen) updateMapCursor();
  }

  function setupTrack() {
    const track = view.querySelector('#pbTrack');
    let drag = null;
    const pct = (e) => Math.min(1, Math.max(0, (e.clientX - track.getBoundingClientRect().left) / track.getBoundingClientRect().width));
    track.addEventListener('pointerdown', (e) => {
      if (!rec) return;
      const p = pct(e); const near = (x) => Math.abs(p - x) < 0.03;
      if (near(win[0])) drag = 'l'; else if (near(win[1])) drag = 'r';
      else if (p > win[0] && p < win[1]) { drag = 'm'; drag = { type: 'm', off: p - win[0], wlen: win[1] - win[0] }; }
      else { cursor = p; updateCursor(); drag = 'c'; }
      track.setPointerCapture(e.pointerId);
    });
    track.addEventListener('pointermove', (e) => {
      if (!drag || !rec) return; const p = pct(e);
      if (drag === 'l') win[0] = Math.min(p, win[1] - 0.02);
      else if (drag === 'r') win[1] = Math.max(p, win[0] + 0.02);
      else if (drag === 'c') { cursor = p; updateCursor(); return; }
      else if (drag.type === 'm') { let s = p - drag.off; s = Math.max(0, Math.min(1 - drag.wlen, s)); win = [s, s + drag.wlen]; }
      refresh();
    });
    track.addEventListener('pointerup', () => drag = null);
  }
  function updateCursor() { el.pbCursor.style.left = (cursor * 100) + '%'; el.pbCur.textContent = fmtClock(cursor * (rec ? rec.dur : 0)); if (mapOpen) updateMapCursor(); }

  function togglePlay() {
    if (!rec) return; playing = !playing;
    el.pbPlay.innerHTML = ICN(playing ? 'pause' : 'play', { size: 14 });
    if (playing) {
      let last = performance.now();
      const loop = (now) => { if (!playing) return; const d = (now - last) / 1000; last = now;
        cursor += d / rec.dur * 20; // 20x speed
        if (cursor > win[1]) cursor = win[0];
        if (cursor < win[0]) cursor = win[0];
        updateCursor(); playRaf = requestAnimationFrame(loop); };
      playRaf = requestAnimationFrame(loop);
    } else cancelAnimationFrame(playRaf);
  }

  function tileURL() {
    const th = document.documentElement.getAttribute('data-theme');
    return th === 'light'
      ? 'https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png'
      : 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png';
  }
  function posAt(frac) {
    if (!rec) return [31.23, 121.47];
    const i = Math.min(rec.n - 1, Math.max(0, Math.round(frac * (rec.n - 1))));
    return [rec.data.gps_lat[i], rec.data.gps_lon[i]];
  }
  function destroyMap() { if (lmap) { lmap.remove(); lmap = null; ltile = null; lcursor = null; levt = []; mapInited = false; } }

  function mapShow() {
    if (!rec || typeof L === 'undefined') return;
    if (!mapInited) initMap();
    setTimeout(() => { if (lmap) { lmap.invalidateSize(); fitMap(); } }, 60);
  }
  function fitMap() {
    const pts = rec.gps.map(p => [p[0], p[1]]);
    if (pts.length) lmap.fitBounds(L.latLngBounds(pts).pad(0.25));
  }
  function initMap() {
    const body = el.pbMapBody; body.innerHTML = '';
    lmap = L.map(body, { zoomControl: true, attributionControl: true, preferCanvas: false, fadeAnimation: false, zoomAnimation: false });
    ltile = L.tileLayer(tileURL(), { subdomains: 'abcd', maxZoom: 19, attribution: '© OpenStreetMap © CARTO' }).addTo(lmap);
    // full track
    const track = rec.gps.map(p => [p[0], p[1]]);
    L.polyline(track, { color: getCSS('--accent'), weight: 3, opacity: .9 }).addTo(lmap);
    L.polyline(track, { color: getCSS('--accent'), weight: 11, opacity: .12 }).addTo(lmap); // glow
    // start / end
    L.circleMarker(track[0], dotStyle(getCSS('--ok'), 7)).addTo(lmap).bindTooltip('起点 t=0', { direction: 'top' });
    L.circleMarker(track[track.length - 1], dotStyle(getCSS('--err'), 7)).addTo(lmap).bindTooltip('终点 t=' + fmtClock(rec.dur), { direction: 'top' });
    // event markers at GPS position of each event time
    const lvlColor = { err: getCSS('--err'), wrn: getCSS('--warn'), ok: getCSS('--ok'), inf: getCSS('--info') };
    rec.events.forEach(ev => {
      const pos = posAt(ev.t / rec.dur);
      const col = lvlColor[ev.lvl] || getCSS('--info');
      const m = L.circleMarker(pos, dotStyle(col, 6.5, '#0a0e13')).addTo(lmap);
      m.bindTooltip(ev.lvl.toUpperCase(), { direction: 'top', offset: [0, -4] });
      m.bindPopup(`<b style="color:${col}">${ev.code || ev.lvl.toUpperCase()}</b><br>${ev.msg}<br><span style="color:var(--text-3)">t = ${fmtClock(ev.t)} · ${pos[0].toFixed(4)}, ${pos[1].toFixed(4)}</span>`);
      m.on('click', () => { cursor = ev.t / rec.dur; updateCursor(); });
      levt.push(m);
    });
    // moving cursor
    lcursor = L.circleMarker(posAt(cursor), { radius: 7, color: getCSS('--accent-2'), weight: 3, fillColor: getCSS('--accent-2'), fillOpacity: .25 }).addTo(lmap);
    mapInited = true;
  }
  function updateMapCursor() { if (lcursor) lcursor.setLatLng(posAt(cursor)); }
  function dotStyle(color, r, ring) { return { radius: r, color: ring || '#0a0e13', weight: 2, fillColor: color, fillOpacity: 1 }; }
  function getCSS(v) { return getComputedStyle(document.documentElement).getPropertyValue(v).trim() || '#2DD4BF'; }
  function swapTiles() { if (lmap && ltile) { ltile.setUrl(tileURL()); } }

  function fmtClock(s) { const m = Math.floor(s / 60), ss = (s % 60); return String(m).padStart(2,'0') + ':' + ss.toFixed(1).padStart(4,'0'); }

  Tabs.playback = {
    mount(v) { view = v; build(); },
    unmount() { playing = false; if (playRaf) cancelAnimationFrame(playRaf); destroyMap(); mapOpen = false; },
    redrawTheme() { swapTiles(); if (rec) refresh(); },
  };
})();

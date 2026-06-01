/* ============================================================
   Live tab — connect flow, streaming render, channel toggle,
   chart tools, CSS-3D attitude, events
   ============================================================ */
(function () {
  const App = window.App;
  const sig = i => `var(--sig-${i})`;
  const ICN = window.svgIcon;
  window.Tabs = window.Tabs || {};

  let view, charts = [], unsub = [], mode = 'grouped', normalize = false, statusExpanded = false;
  let elCache = {};

  const GROUP_TITLES = { '姿态': 'Attitude', '指向': 'Pointing', '信号': 'Signal', '位置': 'Position' };

  function chRow(c) {
    const on = App.visible[c.key];
    return `<div class="ch-row ${on ? 'is-on' : 'is-off'}" data-ch="${c.key}">
      <span class="ch-box">${on ? ICN('check', { size: 10, stroke: 2.6 }) : ''}</span>
      <span class="ch-swatch" style="background:${sig(c.sig)}"></span>
      <span class="ch-name">${c.key}</span><span class="ch-val" data-val="${c.key}">—</span>
    </div>`;
  }

  function build() {
    const groups = App.GROUPS.map(g => {
      const chs = App.CHANNELS.filter(c => c.group === g);
      return `<div class="ch-group-label">${g}<span class="ln"></span></div>` + chs.map(chRow).join('');
    }).join('');

    view.innerHTML = `
    <div class="live" style="flex:1;min-height:0">
      <!-- connection bar -->
      <div class="cbar">
        <div class="conn-state is-off" id="connState">
          <span class="cs-icon" data-icon="satellite" data-size="16"></span>
          <span class="cs-meta"><span class="cs-dev" id="csDev">—</span><span class="cs-stat" id="csStat"><span class="dot"></span>Disconnected</span></span>
        </div>
        <span class="div"></span>
        <label class="field"><span class="field-label">Type</span><select id="fType"><option>UDP</option><option>Serial</option></select></label>
        <label class="field"><span class="field-label">IP</span><input id="fIp" value="192.168.1.12" size="11"></label>
        <label class="field"><span class="field-label">Port</span><input id="fPort" value="4004" size="4" class="num"></label>
        <button class="btn btn--primary" id="btnConnect"><span data-icon="plug" data-size="14"></span>Connect</button>
        <button class="btn btn--danger btn--icon hidden" id="btnDisconnect" title="断开"><span data-icon="unplug" data-size="14"></span></button>
        <span class="div"></span>
        <button class="btn" id="btnRec"><span data-icon="record" data-size="13"></span><span id="recLbl">REC</span></button>
        <button class="btn" id="btnImport"><span data-icon="import" data-size="13"></span>Import</button>
        <button class="btn btn--ghost btn--icon" id="btnClear" title="清除"><span data-icon="trash" data-size="14"></span></button>
        <div class="cbar-spacer"></div>
        <div class="statline">
          <span class="s">FPS <b id="stFps">0</b></span>
          <span class="s">CH <b id="stCh">0</b></span>
          <span class="s">FRM <b id="stFrm">0</b></span>
          <span class="s err">ERR <b id="stErr">0</b></span>
        </div>
      </div>

      <!-- status strip -->
      <div class="sstrip" id="sstrip"></div>

      <!-- main -->
      <div class="live-main">
        <aside class="chpanel">
          <div class="chpanel-head"><span class="t">通道</span><span class="ct" id="chCount">0 / ${App.CHANNELS.length}</span></div>
          <div class="chpanel-tools">
            <button class="btn btn--sm btn--ghost" id="chAll">全选</button>
            <button class="btn btn--sm btn--ghost" id="chNone">清空</button>
            <button class="btn btn--sm btn--ghost" id="chInvert" style="margin-left:auto" title="反选"><span data-icon="layers" data-size="12"></span></button>
          </div>
          <label class="field chpanel-search"><span data-icon="search" data-size="13" style="color:var(--text-3)"></span><input id="chSearch" placeholder="筛选通道…" style="font-family:var(--font-sans);font-size:12px"></label>
          <div class="chpanel-list scroll" id="chList">${groups}</div>
        </aside>

        <section class="center">
          <div class="kpi-row" id="kpiRow"></div>
          <div class="chart-tools">
            <div class="seg seg--accent" id="modeSeg">
              <button data-mode="single"><span data-icon="grid" data-size="13"></span>单图</button>
              <button data-mode="grouped" class="is-active"><span data-icon="columns" data-size="13"></span>分组</button>
            </div>
            <button class="btn btn--sm" id="btnHideAll"><span data-icon="eyeOff" data-size="12"></span>全部隐藏</button>
            <button class="btn btn--sm" id="btnNorm"><span data-icon="sigma" data-size="12"></span>归一化</button>
            <button class="btn btn--sm" id="btnYauto"><span data-icon="gauge" data-size="12"></span>Y 自动</button>
            <div class="ct-right"><span class="chip"><span data-icon="clock" data-size="11"></span>窗口 <b id="winLbl">60s</b></span></div>
          </div>
          <div class="charts scroll" id="charts"></div>
        </section>

        <aside class="rpanel">
          <div class="rpanel-sec">
            <div class="rs-head"><span data-icon="compass" data-size="14" style="color:var(--accent)"></span><span class="t">姿态 3D</span><span class="ic" data-icon="maximize" data-size="13" id="att3dMax"></span></div>
            <div class="att3d">
              <span class="compass">NWU</span>
              <button class="btn btn--sm att-reset" id="attReset"><span data-icon="refresh" data-size="11"></span>复位</button>
              <div class="att-scene"><div class="att-world" id="attWorld">
                <div class="att-grid"></div>
                <div class="ax ax-x"></div><div class="ax ax-y"></div><div class="ax ax-z"></div>
                <div class="att-craft" id="attCraft"><div class="slab"><div class="face top"></div><div class="face bottom"></div><div class="face north"></div><div class="face south"></div><div class="face east"></div><div class="face west"></div></div><div class="craft-nose"></div></div>
                <div class="att-beam" id="attBeam"><div class="ln"></div><div class="tip"></div><div class="ring"></div></div>
              </div></div>
              <div class="att-nums"><span class="a">Roll <b id="aRoll">0.0°</b></span><span class="a">Pitch <b id="aPitch">0.0°</b></span><span class="a">Yaw <b id="aYaw">0.0°</b></span></div>
            </div>
          </div>
          <div class="rpanel-sec">
            <div class="rs-head"><span data-icon="gauge" data-size="14" style="color:var(--accent)"></span><span class="t">状态</span></div>
            <div class="state-grid" id="stateGrid"></div>
          </div>
          <div class="rpanel-sec" style="flex:1;display:flex;flex-direction:column;min-height:0">
            <div class="rs-head"><span data-icon="flag" data-size="14" style="color:var(--accent)"></span><span class="t">事件</span><span class="badge" id="evtBadge">0</span></div>
            <div class="evt-list scroll" id="evtList"></div>
          </div>
        </aside>
      </div>
    </div>`;

    elCache = {};
    ['connState','csDev','csStat','sstrip','kpiRow','charts','chList','chCount','stateGrid','evtList','evtBadge',
     'stFps','stCh','stFrm','stErr','recLbl','btnConnect','btnDisconnect','btnRec','winLbl',
     'attCraft','attBeam','attWorld','aRoll','aPitch','aYaw','btnNorm','btnHideAll'].forEach(id => elCache[id] = view.querySelector('#' + id));

    window.hydrateIcons(view);
    buildCharts();
    buildKpis();
    renderStatus();
    renderEvents();
    renderChannelValues();
    wire();
    renderAttitude();
  }

  /* ---------- charts ---------- */
  function buildCharts() {
    const host = elCache.charts; host.innerHTML = '';
    charts = [];
    if (mode === 'grouped') {
      App.GROUPS.forEach(g => {
        const chs = App.CHANNELS.filter(c => c.group === g);
        if (!chs.some(c => App.visible[c.key])) return;   // hide empty group
        const box = document.createElement('div'); box.className = 'chart-box';
        box.innerHTML = `<div class="cb-head"><span class="cb-title">${g} ${GROUP_TITLES[g]}</span><div class="cb-legend"></div></div>
          <div style="height:138px"><canvas style="width:100%;height:100%;display:block"></canvas></div>`;
        host.appendChild(box);
        const canvas = box.querySelector('canvas');
        const chart = new window.CanvasChart(canvas, { autoY: g !== '信号', yMin: g === '信号' ? 0 : null, yMax: g === '信号' ? 16 : null });
        charts.push({ group: g, keys: chs.map(c => c.key), chart, legend: box.querySelector('.cb-legend') });
      });
    } else {
      const box = document.createElement('div'); box.className = 'chart-box'; box.style.flex = '1';
      box.innerHTML = `<div class="cb-head"><span class="cb-title">全部通道 All channels${normalize ? ' · 归一化' : ''}</span><div class="cb-legend"></div></div>
        <div style="height:440px"><canvas style="width:100%;height:100%;display:block"></canvas></div>`;
      host.appendChild(box);
      const chart = new window.CanvasChart(box.querySelector('canvas'), { autoY: true, normalize });
      charts.push({ group: 'all', keys: App.CHANNELS.map(c => c.key), chart, legend: box.querySelector('.cb-legend') });
    }
    renderCharts(true);
  }

  function renderCharts(updateLegend) {
    charts.forEach(cc => {
      const series = cc.keys.filter(k => App.visible[k]).map(k => ({
        key: k, color: App.color(k), data: App.buffers[k] || [], visible: true,
        lw: (k === 'snr_avg' || k === 'ins_yaw' || k === 'ant_az') ? 1.5 : 1.2,
      }));
      cc.chart.opts.normalize = (cc.group === 'all') ? normalize : false;
      cc.chart.setSeries(series);
      if (App.t > 0) cc.chart.setXDomain([Math.max(0, App.t - App.win * App.dt), App.t]);
      cc.chart.draw();
      if (updateLegend) {
        cc.legend.innerHTML = series.slice(0, 6).map(s => `<span><i style="background:${s.color}"></i>${s.key}</span>`).join('');
      }
    });
  }

  /* ---------- kpis ---------- */
  function buildKpis() {
    const defs = [
      { k: 'roll', lbl: 'ROLL', icon: 'compass', unit: '°' },
      { k: 'pitch', lbl: 'PITCH', icon: 'compass', unit: '°' },
      { k: 'yaw', lbl: 'YAW', icon: 'crosshair', unit: '°' },
      { k: 'snr', lbl: 'SNR', icon: 'signal', unit: 'dB', warn: true },
      { k: 'lock', lbl: 'LOCK', icon: 'lock', unit: '', special: 'lock' },
    ];
    elCache.kpiRow.innerHTML = defs.map(d => `
      <div class="kpi" data-kpi="${d.k}">
        <div class="k-label">${ICN(d.icon, { size: 11 })}${d.lbl}</div>
        <div class="k-val"><span data-kval="${d.k}">—</span><span class="k-unit">${d.unit}</span></div>
        <div class="k-sub" data-ksub="${d.k}"></div>
      </div>`).join('');
    window.hydrateIcons(elCache.kpiRow);
  }
  function renderKpis() {
    const v = App.values;
    const set = (k, val, sub, warn) => {
      const e = elCache.kpiRow.querySelector(`[data-kval="${k}"]`); if (!e) return;
      e.textContent = val;
      const s = elCache.kpiRow.querySelector(`[data-ksub="${k}"]`); if (s) s.textContent = sub || '';
      const card = elCache.kpiRow.querySelector(`[data-kpi="${k}"]`);
      if (card) card.classList.toggle('kpi--warn', !!warn);
    };
    if (!App.connected) { ['roll','pitch','yaw','snr','lock'].forEach(k => set(k, '—', '')); return; }
    set('roll', v.roll.toFixed(2), 'σ 0.4');
    set('pitch', v.pitch.toFixed(2), 'σ 0.3');
    set('yaw', v.yaw.toFixed(1), 'tgt ' + App.heading.toFixed(0));
    set('snr', v.snr.toFixed(1), 'avg ' + v.snr_avg.toFixed(1), v.snr < 6);
    set('lock', App.lock ? '1' : '0', App.mode === 'TRACK' ? 'TRACKING' : 'SCAN');
  }

  /* ---------- status strip ---------- */
  function renderStatus() {
    const conn = App.connected;
    const track = App.mode === 'TRACK';
    const chips = [];
    chips.push(['is-ok','LINK', conn ? 'OK' : '—', conn]);
    if (App.recording) chips.push(['is-ok', 'REC', fmtDur(App.recT), true]);
    chips.push(['is-info','BEAT', conn ? '1Hz' : '—', conn]);
    chips.push('|');
    chips.push([track ? 'is-ok' : 'is-info', 'TRACE', conn ? (track ? 'TRACKING' : 'SCAN_GLOBAL') : '—', conn]);
    chips.push([App.lock ? 'is-ok' : '', 'LOCK', conn ? String(App.lock) : '—', conn]);
    chips.push([conn ? 'is-ok' : '', 'GPS', conn ? '3D' : '—', conn]);
    chips.push([App.insAligned ? 'is-ok' : 'is-warn', 'INS', conn ? (App.insAligned ? 'DONE' : 'ALIGN') : '—', conn]);
    chips.push([conn ? 'is-ok' : '', 'PLL', conn ? 'LOCKED' : '—', conn]);
    chips.push([conn ? 'is-ok' : '', 'MODEM', conn ? 'CONN' : '—', conn]);

    const limit = statusExpanded ? chips.length : 8;
    let shown = 0, html = '', hiddenCount = 0;
    chips.forEach((c, i) => {
      if (c === '|') { if (shown < limit) html += '<span class="grp-div"></span>'; return; }
      if (shown >= limit) { hiddenCount++; return; }
      const [cls, k, val] = c;
      html += `<span class="chip ${cls}"><span class="dot"></span>${k} <b>${val}</b></span>`;
      shown++;
    });
    if (hiddenCount > 0) html += `<button class="btn btn--ghost btn--sm" id="strMore" style="margin-left:auto">+${hiddenCount} 更多</button>`;
    else if (statusExpanded) html += `<button class="btn btn--ghost btn--sm" id="strMore" style="margin-left:auto">收起</button>`;
    elCache.sstrip.innerHTML = html;
    const more = view.querySelector('#strMore');
    if (more) more.onclick = () => { statusExpanded = !statusExpanded; renderStatus(); };

    // state grid
    const cell = (cls, k, v) => `<div class="state-cell ${cls}"><span class="dot"></span><div><div class="sc-k">${k}</div><div class="sc-v">${v}</div></div></div>`;
    elCache.stateGrid.innerHTML =
      cell(conn ? 'ok' : 'off', 'LINK', conn ? 'OK' : 'DOWN') +
      cell(App.lock ? 'ok' : 'off', 'LOCK', conn ? String(App.lock) : '—') +
      cell(conn ? 'ok' : 'off', 'GPS', conn ? '3D FIX' : '—') +
      cell(App.insAligned ? 'ok' : (conn ? 'warn' : 'off'), 'INS', conn ? (App.insAligned ? 'DONE' : 'ALIGN') : '—');
  }
  function fmtDur(s) { const m = Math.floor(s / 60), ss = Math.floor(s % 60); return String(m).padStart(2,'0') + ':' + String(ss).padStart(2,'0'); }

  /* ---------- events ---------- */
  function renderEvents() {
    const list = elCache.evtList;
    if (!App.events.length) { list.innerHTML = `<div style="padding:18px;text-align:center;color:var(--text-3);font-size:11px">${App.connected ? '暂无事件' : '连接后显示事件流'}</div>`; }
    else list.innerHTML = App.events.slice(0, 60).map(e => `<div class="evt lvl-${e.lvl}"><span class="evt-time">${e.time.slice(0,8)}</span><span class="evt-lvl">${e.code ? e.lvl.toUpperCase() : e.lvl.toUpperCase()}</span><span class="evt-msg">${e.msg}</span></div>`).join('');
    elCache.evtBadge.textContent = App.events.length;
    elCache.evtBadge.style.display = App.events.length ? '' : 'none';
  }

  /* ---------- channel values ---------- */
  function renderChannelValues() {
    App.CHANNELS.forEach(c => {
      const e = elCache.chList.querySelector(`[data-val="${c.key}"]`);
      if (e) e.textContent = App.connected ? (App.values[c.key] != null ? App.values[c.key].toFixed(2) : '—') : '—';
    });
    const onCount = App.CHANNELS.filter(c => App.visible[c.key]).length;
    elCache.chCount.textContent = onCount + ' / ' + App.CHANNELS.length;
    elCache.stCh.textContent = onCount;
  }

  /* ---------- attitude ---------- */
  function renderAttitude() {
    const v = App.values; const conn = App.connected;
    const roll = conn ? v.roll : 0, pitch = conn ? v.pitch : 0, yaw = conn ? v.yaw : 0;
    const az = conn ? v.ant_az : 0, el = conn ? v.ant_el : 45;
    if (elCache.attCraft) elCache.attCraft.style.transform = `rotateZ(${-yaw}deg) rotateX(${pitch * 1.4}deg) rotateY(${roll * 1.4}deg)`;
    if (elCache.attBeam) {
      const len = 60 + el * 0.8;
      elCache.attBeam.style.height = len + 'px';
      elCache.attBeam.style.transform = `rotateZ(${-az}deg) rotateX(${-(90 - el)}deg)`;
    }
    if (elCache.aRoll) elCache.aRoll.textContent = roll.toFixed(1) + '°';
    if (elCache.aPitch) elCache.aPitch.textContent = pitch.toFixed(1) + '°';
    if (elCache.aYaw) elCache.aYaw.textContent = (yaw < 0 ? yaw + 360 : yaw).toFixed(0) + '°';
  }
  let worldYaw = 0;

  /* ---------- wiring ---------- */
  function wire() {
    elCache.btnConnect.onclick = connect;
    elCache.btnDisconnect.onclick = disconnect;
    elCache.btnRec.onclick = () => {
      if (!App.connected) { toast('请先连接设备'); return; }
      App.recording = !App.recording; if (App.recording) App.recT = 0;
      elCache.btnRec.classList.toggle('is-active', App.recording);
      elCache.recLbl.textContent = App.recording ? 'REC ●' : 'REC';
      toast(App.recording ? '开始录制 recording_…sdb' : '录制已停止 · 已保存');
      renderStatus();
    };
    elCache.btnClear = view.querySelector('#btnClear');
    elCache.btnClear.onclick = () => { App.CHANNELS.forEach(c => App.buffers[c.key] = []); App.events = []; renderCharts(true); renderEvents(); toast('已清除缓冲'); };
    view.querySelector('#btnImport').onclick = () => toast('Import：从 .sdb 导入到实时视图（演示）');

    // channel toggles
    elCache.chList.onclick = (e) => {
      const row = e.target.closest('[data-ch]'); if (!row) return;
      const k = row.dataset.ch; App.visible[k] = !App.visible[k];
      row.classList.toggle('is-on', App.visible[k]); row.classList.toggle('is-off', !App.visible[k]);
      row.querySelector('.ch-box').innerHTML = App.visible[k] ? ICN('check', { size: 10, stroke: 2.6 }) : '';
      buildCharts(); renderChannelValues();
    };
    view.querySelector('#chAll').onclick = () => bulkVis(() => true);
    view.querySelector('#chNone').onclick = () => bulkVis(() => false);
    view.querySelector('#chInvert').onclick = () => bulkVis(k => !App.visible[k]);
    view.querySelector('#chSearch').oninput = (e) => {
      const q = e.target.value.toLowerCase();
      elCache.chList.querySelectorAll('[data-ch]').forEach(r => r.style.display = r.dataset.ch.includes(q) ? '' : 'none');
    };

    // mode seg
    view.querySelector('#modeSeg').onclick = (e) => {
      const b = e.target.closest('[data-mode]'); if (!b) return;
      mode = b.dataset.mode;
      [...b.parentElement.children].forEach(x => x.classList.toggle('is-active', x === b));
      buildCharts();
    };
    elCache.btnHideAll.onclick = () => { bulkVis(() => false); };
    elCache.btnNorm.onclick = () => { normalize = !normalize; elCache.btnNorm.classList.toggle('is-active', normalize); buildCharts(); if (normalize) toast('归一化：各曲线按自身窗口缩放到 0–1'); };
    view.querySelector('#btnYauto').onclick = () => { renderCharts(true); toast('已复位 Y 轴'); };
    view.querySelector('#attReset').onclick = () => { elCache.attWorld.style.transform = 'rotateX(58deg) rotateZ(-15deg)'; toast('视角已复位'); };
  }
  function bulkVis(fn) {
    App.CHANNELS.forEach(c => App.visible[c.key] = fn(c.key));
    elCache.chList.querySelectorAll('[data-ch]').forEach(r => {
      const on = App.visible[r.dataset.ch];
      r.classList.toggle('is-on', on); r.classList.toggle('is-off', !on);
      r.querySelector('.ch-box').innerHTML = on ? ICN('check', { size: 10, stroke: 2.6 }) : '';
    });
    buildCharts(); renderChannelValues();
  }

  /* ---------- connect flow ---------- */
  function connect() {
    if (App.connected) return;
    elCache.btnConnect.innerHTML = ICN('refresh', { size: 14 }) + '连接中…';
    elCache.btnConnect.disabled = true;
    elCache.csStat.innerHTML = '<span class="dot" style="background:var(--warn)"></span>握手中…';
    setTimeout(() => {
      App.connected = true; App.reset(); App.seed(60); App.start();
      App.pushEvent('inf', 'HELLO', 'device handshake · afd01 / fw v1.2.0');
      elCache.btnConnect.classList.add('hidden');
      elCache.btnDisconnect.classList.remove('hidden');
      elCache.connState.classList.remove('is-off');
      elCache.csDev.textContent = 'afd01';
      elCache.csStat.innerHTML = '<span class="dot"></span>LINK OK · UDP 192.168.1.12:4004';
      window.Tabs._device && window.Tabs._device.refreshDevice && window.Tabs._device.refreshDevice();
      toast('已连接 afd01', 'ok');
      fullRender(true);
    }, 950);
  }
  function disconnect() {
    App.stop(); App.connected = false; App.recording = false;
    elCache.btnConnect.classList.remove('hidden'); elCache.btnConnect.disabled = false;
    elCache.btnConnect.innerHTML = ICN('plug', { size: 14 }) + 'Connect';
    elCache.btnDisconnect.classList.add('hidden');
    elCache.btnRec.classList.remove('is-active'); elCache.recLbl.textContent = 'REC';
    elCache.connState.classList.add('is-off');
    elCache.csDev.textContent = '—';
    elCache.csStat.innerHTML = '<span class="dot"></span>Disconnected';
    toast('已断开');
    fullRender(true);
  }

  /* ---------- render loop ---------- */
  let lastChartDraw = 0;
  function onTick() {
    renderCharts(false);
    renderKpis();
    renderChannelValues();
    renderAttitude();
    if (App.recording) { const c = view.querySelector('#strMore'); renderStatusLight(); }
    elCache.stFrm.textContent = App.frames.toLocaleString();
  }
  function renderStatusLight() {
    // only update REC timer chip text to avoid full rebuild
    const recChip = [...elCache.sstrip.querySelectorAll('.chip')].find(c => c.textContent.startsWith('REC'));
    if (recChip) recChip.querySelector('b').textContent = fmtDur(App.recT);
  }
  function onState() {
    elCache.stFps.textContent = App.fps;
    elCache.stErr.textContent = App.errors;
    renderStatus();
  }
  function fullRender() { renderCharts(true); renderKpis(); renderChannelValues(); renderStatus(); renderEvents(); renderAttitude(); }

  /* ---------- module ---------- */
  Tabs.live = {
    mount(v) {
      view = v; build();
      unsub.push(App.on('tick', onTick));
      unsub.push(App.on('state', onState));
      unsub.push(App.on('event', () => { renderEvents(); renderStatus(); }));
      // restore running state if already connected
      if (App.connected) {
        elCache.btnConnect.classList.add('hidden'); elCache.btnDisconnect.classList.remove('hidden');
        elCache.connState.classList.remove('is-off'); elCache.csDev.textContent = 'afd01';
        elCache.csStat.innerHTML = '<span class="dot"></span>LINK OK · UDP 192.168.1.12:4004';
        fullRender();
      }
    },
    unmount() { unsub.forEach(f => f()); unsub = []; charts = []; },
    redrawTheme() { if (view) { buildCharts(); } },
  };
  function toast(m, k) { window.toast && window.toast(m, k); }
})();

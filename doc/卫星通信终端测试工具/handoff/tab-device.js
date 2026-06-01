/* ============================================================
   Device tab — info, parameter read/write, OTA firmware upgrade
   ============================================================ */
(function () {
  const App = window.App;
  const ICN = window.svgIcon;
  window.Tabs = window.Tabs || {};
  let view, el = {}, otaFile = null, otaRunning = false;

  const PARAMS = [
    { k: 'device_ip', type: 'IP', val: '192.168.1.12', note: '需重启' },
    { k: 'remote_port', type: 'UINT16', val: '4004', note: '' },
    { k: 'trace_mode', type: 'ENUM', val: 'SCAN_GLOBAL', opts: ['SCAN_GLOBAL','TRACK','MANUAL','STANDBY'], note: '' },
    { k: 'sample_rate', type: 'ENUM', val: '100', opts: ['10','50','100','200'], note: '' },
    { k: 'snr_threshold', type: 'FLOAT', val: '6.0', note: '' },
    { k: 'beam_az_offset', type: 'FLOAT', val: '0.0', note: '' },
    { k: 'gps_enable', type: 'BOOL', val: 'true', opts: ['true','false'], note: '' },
  ];
  let edited = {};

  function build() {
    if (!App.connected) { renderEmpty(); return; }
    view.innerHTML = `<div class="dev scroll">
      <div>
        <div class="dev-sec-h"><span data-icon="cpu" data-size="15" style="color:var(--accent)"></span><span class="t">设备信息</span><span class="desc">afd01 · 已连接</span><button class="btn btn--sm" id="dvRefresh" style="margin-left:auto"><span data-icon="refresh" data-size="12"></span>刷新</button></div>
        <div class="dev-info">
          <div class="cell"><div class="k">硬件类型</div><div class="v">afd01</div></div>
          <div class="cell"><div class="k">固件版本</div><div class="v">v1.2.0</div></div>
          <div class="cell"><div class="k">序列号</div><div class="v">SN1234-A7</div></div>
          <div class="cell"><div class="k">协议版本</div><div class="v">v2</div></div>
        </div>
      </div>

      <div>
        <div class="dev-sec-h"><span data-icon="sliders" data-size="15" style="color:var(--accent)"></span><span class="t">参数管理</span>
          <div style="margin-left:auto;display:flex;gap:8px"><button class="btn btn--sm" id="dvReadAll"><span data-icon="download" data-size="12"></span>读取全部</button><button class="btn btn--sm btn--ghost" id="dvReset"><span data-icon="history" data-size="12"></span>恢复出厂</button></div></div>
        <table class="ptable"><thead><tr><th style="width:30%">名称</th><th style="width:14%">类型</th><th>当前值</th><th style="width:140px">操作</th><th style="width:110px">状态</th></tr></thead>
        <tbody id="dvRows"></tbody></table>
      </div>

      <div>
        <div class="dev-sec-h"><span data-icon="upload" data-size="15" style="color:var(--accent)"></span><span class="t">固件升级 OTA</span></div>
        <div class="ota">
          <div class="ota-file" id="otaFile"><span data-icon="file" data-size="16" style="color:var(--text-3)"></span><span style="color:var(--text-3);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1" id="otaName">未选择固件</span><button class="btn btn--sm" id="otaPick" style="margin-left:auto"><span data-icon="folder" data-size="12"></span>选择固件…</button></div>
          <label class="chk on" id="otaPause"><span class="box">${ICN('check', { size: 11, stroke: 2.6 })}</span>暂停实时数据（全力传输）</label>
          <div class="ota-steps" id="otaSteps">
            <div class="ota-step" data-step="0"><span data-icon="upload" data-size="13"></span>传输</div>
            <div class="ota-step" data-step="1"><span data-icon="check" data-size="13"></span>校验</div>
            <div class="ota-step" data-step="2"><span data-icon="refresh" data-size="13"></span>写入</div>
            <div class="ota-step" data-step="3"><span data-icon="bolt" data-size="13"></span>重启</div>
          </div>
          <div class="ota-prog"><div class="bar" id="otaBar"></div></div>
          <div style="display:flex;align-items:center;gap:12px">
            <span class="num" id="otaPct" style="font-size:12px;color:var(--text-2)">0%</span>
            <span id="otaStat" style="font-size:12px;color:var(--text-3);white-space:nowrap">空闲</span>
            <button class="btn btn--primary" id="otaGo" style="margin-left:auto" disabled><span data-icon="bolt" data-size="13"></span>上传并升级</button>
          </div>
        </div>
      </div>
    </div>`;
    window.hydrateIcons(view);
    el.rows = view.querySelector('#dvRows');
    renderRows();
    view.querySelector('#dvRefresh').onclick = () => window.toast('已刷新设备信息');
    view.querySelector('#dvReadAll').onclick = () => { edited = {}; renderRows(); window.toast('已读取全部参数', 'ok'); };
    view.querySelector('#dvReset').onclick = () => window.toast('已发送恢复出厂指令');
    view.querySelector('#otaPick').onclick = pickFw;
    view.querySelector('#otaPause').onclick = (e) => { const c = e.currentTarget; c.classList.toggle('on'); };
    view.querySelector('#otaGo').onclick = runOta;
  }

  function renderRows() {
    el.rows.innerHTML = PARAMS.map((p, i) => {
      const dirty = edited[p.k] != null;
      const cur = dirty ? edited[p.k] : p.val;
      let editor;
      if (p.opts) editor = `<div class="field" style="height:24px"><select data-edit="${i}">${p.opts.map(o => `<option ${o === cur ? 'selected' : ''}>${o}</option>`).join('')}</select></div>`;
      else editor = `<div class="field" style="height:24px;width:140px"><input data-edit="${i}" value="${cur}" class="num"></div>`;
      const status = dirty ? `<span class="pstat dirty">${ICN('pencil', { size: 12 })}已修改</span>` : (p.note ? `<span class="pstat warn">${ICN('alert', { size: 12 })}${p.note}</span>` : `<span class="pstat ok">${ICN('check', { size: 12 })}已同步</span>`);
      return `<tr><td class="pk">${p.k}</td><td><span class="pt">${p.type}</span></td><td>${editor}</td>
        <td><div class="pact"><button class="btn btn--sm" data-apply="${i}"><span data-icon="check" data-size="11"></span>应用</button></div></td>
        <td>${status}</td></tr>`;
    }).join('');
    window.hydrateIcons(el.rows);
    el.rows.querySelectorAll('[data-edit]').forEach(inp => {
      const i = +inp.dataset.edit;
      const h = () => { edited[PARAMS[i].k] = inp.value; markDirty(i); };
      inp.oninput = h; inp.onchange = h;
    });
    el.rows.querySelectorAll('[data-apply]').forEach(b => b.onclick = () => {
      const i = +b.dataset.apply; const p = PARAMS[i];
      if (edited[p.k] != null) { p.val = edited[p.k]; delete edited[p.k]; }
      renderRows(); window.toast(`已应用 ${p.k} = ${p.val}`, 'ok');
    });
  }
  function markDirty(i) {
    const row = el.rows.children[i];
    if (row) row.querySelector('td:last-child').innerHTML = `<span class="pstat dirty">${ICN('pencil', { size: 12 })}已修改</span>`;
  }

  function pickFw() {
    otaFile = { name: 'firmware_v1.3.0.bin', size: '1.2 MB', ver: 'v1.3.0' };
    view.querySelector('#otaName').innerHTML = `<b class="mono" style="color:var(--text)">${otaFile.name}</b> <span style="color:var(--text-3)">${otaFile.size} · ${otaFile.ver}</span>`;
    view.querySelector('#otaFile').querySelector('[data-icon]').style.color = 'var(--accent)';
    view.querySelector('#otaGo').disabled = false;
  }

  function runOta() {
    if (otaRunning || !otaFile) return; otaRunning = true;
    const go = view.querySelector('#otaGo'); go.disabled = true;
    const bar = view.querySelector('#otaBar'), pct = view.querySelector('#otaPct'), stat = view.querySelector('#otaStat');
    const steps = [...view.querySelectorAll('.ota-step')];
    const phases = [['传输中… 分卷 1/5', 0, 55], ['校验固件…', 55, 70], ['写入 Flash…', 70, 92], ['重启设备…', 92, 100]];
    let p = 0;
    function phase() {
      if (p >= phases.length) { stat.textContent = '升级完成 · 设备已重启'; stat.style.color = 'var(--ok)'; steps.forEach(s => { s.classList.remove('on'); s.classList.add('done'); }); go.disabled = false; otaRunning = false; window.toast('OTA 升级完成 ✓', 'ok'); return; }
      const [label, from, to] = phases[p];
      steps.forEach((s, i) => s.classList.toggle('on', i === p));
      steps.forEach((s, i) => s.classList.toggle('done', i < p));
      stat.textContent = label; let v = from;
      const iv = setInterval(() => {
        v += (to - from) / 16 + Math.random();
        if (v >= to) { v = to; clearInterval(iv); p++; setTimeout(phase, 220); }
        bar.style.width = v + '%'; pct.textContent = Math.round(v) + '%';
      }, 70);
    }
    phase();
  }

  function renderEmpty() {
    view.innerHTML = `<div class="dev-empty"><div class="inner">
      <span data-icon="unplug" data-size="40" style="color:var(--text-3)"></span>
      <div style="font-size:14px;color:var(--text-2)">请先在<b style="color:var(--accent-2)">实时</b>页面连接设备</div>
      <div style="font-size:12px;color:var(--text-3)">连接后这里显示设备信息、参数表与 OTA 升级</div>
      <button class="btn btn--primary" id="dvGoLive"><span data-icon="activity" data-size="13"></span>前往实时页面</button>
    </div></div>`;
    window.hydrateIcons(view);
    view.querySelector('#dvGoLive').onclick = () => window.Shell.go('live');
  }

  Tabs.device = {
    mount(v) { view = v; build(); },
    unmount() {},
    refreshDevice() { if (view && view.querySelector('.dev-empty')) build(); },
    redrawTheme() {},
  };
  Tabs._device = Tabs.device;
})();

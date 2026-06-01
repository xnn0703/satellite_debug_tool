/* ============================================================
   Design Direction page — dynamic content builder
   ============================================================ */
(function () {
  const $ = (s, r) => (r || document).querySelector(s);
  const el = (h) => { const t = document.createElement('template'); t.innerHTML = h.trim(); return t.content.firstElementChild; };
  const sig = n => `var(--sig-${n})`;

  /* ---------------- 01 · palette ---------------- */
  const THEMES = [
    { id: 'dark', nm: '深色 Dark', badge: '默认', hex: { bg:'#090D12', panel:'#0E141C', card:'#121A24', border:'#1F2C38', text:'#D6E0E9', 'text-2':'#8696A4', accent:'#2DD4BF', ok:'#34D399', warn:'#FBBF24', err:'#FB7185', info:'#38BDF8' } },
    { id: 'dark-hc', nm: '强光 Outdoor', badge: '500cd/m²', hex: { bg:'#000000', panel:'#060606', card:'#0E0E0E', border:'#3A3A3A', text:'#FFFFFF', 'text-2':'#C8C8C8', accent:'#00F5D4', ok:'#00FF95', warn:'#FFD000', err:'#FF5470', info:'#4DC4FF' } },
    { id: 'light', nm: '浅色 Light', badge: '室内', hex: { bg:'#EEF1F4', panel:'#F6F8FA', card:'#FFFFFF', border:'#D8DEE4', text:'#1B2630', 'text-2':'#5A6976', accent:'#0EA5A4', ok:'#059669', warn:'#B45309', err:'#DC2626', info:'#0284C7' } },
  ];
  const ROLES = [['bg','主背景'],['panel','面板'],['card','卡片'],['border','边框'],['text','主文字'],['text-2','次文字'],['accent','主强调'],['ok','成功'],['warn','警告'],['err','错误'],['info','信息']];
  $('#palGrid').innerHTML = THEMES.map(t => `
    <div class="pal-col" data-theme="${t.id}">
      <div class="pal-top"><span class="nm">${t.nm}</span><span class="badge">${t.badge}</span></div>
      <div class="pal-rows">
        ${ROLES.map(([k, lbl]) => `<div class="pal-row"><span class="sw" style="background:${t.hex[k]}"></span><span class="rl">${lbl}</span><span class="hx">${t.hex[k]}</span></div>`).join('')}
      </div>
    </div>`).join('');

  /* ---------------- 02 · signal palette ---------------- */
  const SIGNAMES = ['roll','pitch','yaw','ins_yaw','ant_az','ant_el','snr','snr_avg','gps_lat','gps_lon','tgt_az','tgt_el','pll','temp','volt','scan'];
  $('#sigGrid').innerHTML = SIGNAMES.map((nm, i) => `<div class="sig-item"><span class="ln" style="background:${sig(i+1)}"></span><span class="lb">${nm}</span></div>`).join('');

  /* ---------------- 03 · type ---------------- */
  const TYPE = [
    ['KPI 数字', 'IBM Plex Mono · 600 · 26px', '<span class="num" style="font-size:26px;font-weight:600">−0.15°</span>'],
    ['标题 H2', 'IBM Plex Sans · 600 · 16px', '<span style="font-size:16px;font-weight:600">姿态 Attitude</span>'],
    ['正文 Base', 'IBM Plex Sans · 400 · 13px', '<span style="font-size:13px">实时画曲线、看状态灯、录制 .sdb 数据</span>'],
    ['标签 Label', 'IBM Plex Sans · 500 · 11px', '<span style="font-size:11px;font-weight:500;letter-spacing:.04em;color:var(--text-2)">REMOTE PORT</span>'],
    ['值 · 等宽', 'IBM Plex Mono · 500 · 12px', '<span class="num" style="font-size:12px">roll −2.34 · yaw 87.20 · snr −83.1</span>'],
    ['微标签', 'IBM Plex Mono · 600 · 10px', '<span class="num" style="font-size:10px;letter-spacing:.08em;color:var(--text-3)">FPS 12 · CH 14 · ERR 0</span>'],
  ];
  $('#typeList').innerHTML = TYPE.map(([nm, meta, spec]) => `<div class="type-row"><div class="meta"><b>${nm}</b>${meta}</div><div class="spec">${spec}</div></div>`).join('');

  /* ---------------- 04 · icons ---------------- */
  $('#iconGrid').innerHTML = (window.ICON_NAMES || []).map(n => `<div class="icon-cell"><span data-icon="${n}" data-size="20"></span><span class="nm">${n}</span></div>`).join('');

  /* ---------------- 05 · components ---------------- */
  $('#kpiShelf').innerHTML = `
    <div class="kpi"><div class="k-label"><span data-icon="compass" data-size="11"></span>ROLL</div><div class="k-val">−0.15<span class="k-unit">°</span></div><div class="k-sub">σ 0.42</div></div>
    <div class="kpi"><div class="k-label"><span data-icon="compass" data-size="11"></span>PITCH</div><div class="k-val">−0.57<span class="k-unit">°</span></div><div class="k-sub">σ 0.31</div></div>
    <div class="kpi kpi--warn"><div class="k-label"><span data-icon="signal" data-size="11"></span>SNR</div><div class="k-val">8.4<span class="k-unit">dB</span></div><div class="k-sub">avg 7.9</div></div>`;
  const CH = [['roll','−2.34',1,1],['pitch','1.55',2,1],['yaw','87.20',3,1],['snr','−83.1',7,0]];
  $('#chShelf').innerHTML = CH.map(([nm,v,c,on]) => `
    <div class="ch-row ${on?'is-on':'is-off'}">
      <span class="ch-box">${on?window.svgIcon('check',{size:10,stroke:2.6}):''}</span>
      <span class="ch-swatch" style="background:${sig(c)}"></span>
      <span class="ch-name">${nm}</span><span class="ch-val">${v}</span>
    </div>`).join('');
  const EVT = [['22:11:36','INF','inf','TRACE_MODE_CHANGE → SCAN_GLOBAL'],['22:11:35','OK','ok','LOCK_ACQUIRED'],['22:10:02','WRN','wrn','SNR below 6 dB threshold'],['22:08:44','ERR','err','TRACK_LOST']];
  $('#evtShelf').innerHTML = EVT.map(([tm,lv,cl,msg]) => `<div class="evt lvl-${cl}"><span class="evt-time">${tm}</span><span class="evt-lvl">${lv}</span><span class="evt-msg">${msg}</span></div>`).join('');

  /* ---------------- chart helpers ---------------- */
  const XT = ['0','20','40','60','80','100'];
  function chartBox(title, legend, chart) {
    return `<div class="chart-box"><div class="cb-head"><span class="cb-title">${title}</span><div class="cb-legend">${legend.map(([c,l])=>`<span><i style="background:${c}"></i>${l}</span>`).join('')}</div></div>${chart}</div>`;
  }

  /* ---------------- 06 · HERO live tab ---------------- */
  function buildHero() {
    const chartH = 132;
    const W = 'auto';
    const att = window.lineChart({ width: 560, height: chartH, n: 200, yTicks: 4, xTicks: XT,
      series: [
        { kind:'yaw', color:sig(3), seed:3, width:1.3 },
        { kind:'ins_yaw', color:sig(2), seed:9, width:1.3 },
        { kind:'roll', color:sig(1), seed:1 },
        { kind:'pitch', color:sig(6), seed:5 },
      ] });
    const pnt = window.lineChart({ width: 560, height: chartH, n: 200, yTicks: 4, xTicks: XT,
      series: [ { kind:'az', color:sig(4), seed:2, width:1.3 }, { kind:'el', color:sig(6), seed:4 } ] });
    const sgl = window.lineChart({ width: 560, height: chartH, n: 200, yTicks: 4, xTicks: XT, yMin:0, yMax:14,
      series: [ { kind:'snr', color:sig(3), seed:6 }, { kind:'snr_avg', color:sig(6), seed:7, width:1.4 }, { kind:'scan', color:sig(2), seed:8 } ] });

    const chRows = (rows) => rows.map(([nm,v,c,on,off]) => `
      <div class="ch-row ${on?'is-on':''} ${off?'is-off':''}">
        <span class="ch-box">${on?window.svgIcon('check',{size:10,stroke:2.6}):''}</span>
        <span class="ch-swatch" style="background:${sig(c)}"></span>
        <span class="ch-name">${nm}</span><span class="ch-val">${v}</span>
      </div>`).join('');

    $('#hero').innerHTML = `
      <!-- global bar -->
      <div class="gbar">
        <div class="gbar-brand"><span class="gmark" data-icon="satellite" data-size="15"></span>Satellite Debug Tool</div>
        <div class="seg seg--accent gtabs">
          <button class="is-active"><span data-icon="activity" data-size="13"></span>实时</button>
          <button><span data-icon="history" data-size="13"></span>回放</button>
          <button><span data-icon="list" data-size="13"></span>Log</button>
          <button><span data-icon="cpu" data-size="13"></span>设备</button>
        </div>
        <div class="gbar-right">
          <button class="btn btn--icon btn--ghost"><span data-icon="moon" data-size="15"></span></button>
          <button class="btn btn--ghost btn--sm"><span data-icon="refresh" data-size="13"></span>检查更新</button>
          <button class="btn btn--icon btn--ghost"><span data-icon="settings" data-size="15"></span></button>
        </div>
      </div>

      <!-- connection bar -->
      <div class="cbar">
        <div class="conn-state">
          <span class="cs-icon" data-icon="satellite" data-size="16"></span>
          <span class="cs-meta"><span class="cs-dev">afd01</span><span class="cs-stat"><span class="dot"></span>LINK OK · UDP 192.168.1.12:4004</span></span>
        </div>
        <span class="div"></span>
        <label class="field"><span class="field-label">Type</span><select><option>UDP</option></select></label>
        <label class="field"><span class="field-label">IP</span><input value="192.168.1.12" size="11"></label>
        <label class="field"><span class="field-label">Port</span><input value="4004" size="4" class="num"></label>
        <button class="btn btn--primary"><span data-icon="plug" data-size="14"></span>Connect</button>
        <button class="btn btn--danger btn--icon"><span data-icon="unplug" data-size="14"></span></button>
        <span class="div"></span>
        <button class="btn is-active"><span data-icon="record" data-size="13"></span>REC</button>
        <button class="btn"><span data-icon="import" data-size="13"></span>Import</button>
        <button class="btn btn--ghost btn--icon"><span data-icon="trash" data-size="14"></span></button>
        <div class="cbar-spacer"></div>
        <div class="statline">
          <span class="s">FPS <b>12</b></span><span class="s">CH <b>14</b></span><span class="s">FRM <b>24,501</b></span><span class="s err">ERR <b>0</b></span>
        </div>
      </div>

      <!-- status strip -->
      <div class="sstrip">
        <span class="chip is-ok"><span class="dot"></span>LINK <b>OK</b></span>
        <span class="chip is-ok"><span class="dot"></span>REC <b>02:14</b></span>
        <span class="chip is-info"><span class="dot"></span>BEAT <b>1Hz</b></span>
        <span class="grp-div"></span>
        <span class="chip is-info"><span class="dot"></span>TRACE <b>SCAN_GLOBAL</b></span>
        <span class="chip is-ok"><span class="dot"></span>LOCK <b>1</b></span>
        <span class="chip is-ok"><span class="dot"></span>GPS <b>3D</b></span>
        <span class="chip is-warn"><span class="dot"></span>INS <b>ALIGN</b></span>
        <span class="chip is-ok"><span class="dot"></span>PLL <b>LOCKED</b></span>
        <span class="chip"><span class="dot"></span>MODEM <b>CONN</b></span>
        <button class="btn btn--ghost btn--sm" style="margin-left:auto">+2 更多</button>
      </div>

      <!-- main -->
      <div class="live-main">
        <!-- channel panel -->
        <aside class="chpanel">
          <div class="chpanel-head"><span class="t">通道</span><span class="ct">9 / 14</span></div>
          <div class="chpanel-tools"><button class="btn btn--sm btn--ghost">全选</button><button class="btn btn--sm btn--ghost">清空</button><button class="btn btn--sm btn--ghost" style="margin-left:auto"><span data-icon="eye" data-size="12"></span></button></div>
          <label class="field chpanel-search"><span data-icon="search" data-size="13" style="color:var(--text-3)"></span><input placeholder="筛选通道…" style="font-family:var(--font-sans);font-size:12px"></label>
          <div class="chpanel-list scroll">
            <div class="ch-group-label">姿态<span class="ln"></span></div>
            ${chRows([['roll','−2.34',1,1],['pitch','1.55',6,1],['yaw','87.20',3,1],['ins_yaw','86.9',2,1]])}
            <div class="ch-group-label">指向<span class="ln"></span></div>
            ${chRows([['ant_az','271.4',4,1],['ant_el','42.1',6,1],['tgt_az','0.00',7,0,1]])}
            <div class="ch-group-label">信号<span class="ln"></span></div>
            ${chRows([['snr','8.41',3,1],['snr_avg','7.92',6,1],['scan_loss','0.5',2,0,1]])}
            <div class="ch-group-label">位置<span class="ln"></span></div>
            ${chRows([['gps_lat','31.23',9,0,1],['gps_lon','121.55',10,0,1]])}
          </div>
        </aside>

        <!-- center -->
        <section class="center">
          <div class="kpi-row">
            <div class="kpi"><div class="k-label"><span data-icon="compass" data-size="11"></span>ROLL</div><div class="k-val">−0.15<span class="k-unit">°</span></div><div class="k-sub">σ 0.42</div></div>
            <div class="kpi"><div class="k-label"><span data-icon="compass" data-size="11"></span>PITCH</div><div class="k-val">−0.57<span class="k-unit">°</span></div><div class="k-sub">σ 0.31</div></div>
            <div class="kpi"><div class="k-label"><span data-icon="crosshair" data-size="11"></span>YAW</div><div class="k-val">87.2<span class="k-unit">°</span></div><div class="k-sub">tgt 87.0</div></div>
            <div class="kpi kpi--warn"><div class="k-label"><span data-icon="signal" data-size="11"></span>SNR</div><div class="k-val">8.4<span class="k-unit">dB</span></div><div class="k-sub">avg 7.9</div></div>
            <div class="kpi"><div class="k-label"><span data-icon="lock" data-size="11"></span>LOCK</div><div class="k-val">1<span class="k-unit">trk</span></div><div class="k-sub">stable 96%</div></div>
          </div>
          <div class="chart-tools">
            <div class="seg seg--accent"><button><span data-icon="grid" data-size="13"></span>单图</button><button class="is-active"><span data-icon="columns" data-size="13"></span>分组</button></div>
            <button class="btn btn--sm"><span data-icon="eyeOff" data-size="12"></span>全部隐藏</button>
            <button class="btn btn--sm"><span data-icon="sigma" data-size="12"></span>归一化</button>
            <button class="btn btn--sm"><span data-icon="gauge" data-size="12"></span>Y 自动</button>
            <div class="ct-right"><span class="chip"><span data-icon="clock" data-size="11"></span>窗口 120s</span></div>
          </div>
          <div class="charts scroll">
            ${chartBox('姿态 Attitude', [[sig(3),'yaw'],[sig(2),'ins_yaw'],[sig(1),'roll'],[sig(6),'pitch']], att)}
            ${chartBox('指向 Pointing', [[sig(4),'ant_az'],[sig(6),'ant_el']], pnt)}
            ${chartBox('信号 Signal', [[sig(3),'snr'],[sig(6),'snr_avg'],[sig(2),'scan_loss']], sgl)}
          </div>
        </section>

        <!-- right -->
        <aside class="rpanel">
          <div class="rpanel-sec">
            <div class="rs-head"><span data-icon="compass" data-size="14" style="color:var(--accent)"></span><span class="t">姿态 3D</span><span class="ic" data-icon="maximize" data-size="13"></span></div>
            <div class="att3d">
              <span class="compass">N ↑</span>
              <button class="btn btn--sm att-reset"><span data-icon="refresh" data-size="11"></span>复位</button>
              ${attitudeSVG()}
              <div class="att-nums"><span class="a">Roll <b>−0.2°</b></span><span class="a">Pitch <b>1.5°</b></span><span class="a">Yaw <b>87°</b></span></div>
            </div>
          </div>
          <div class="rpanel-sec">
            <div class="rs-head"><span data-icon="gauge" data-size="14" style="color:var(--accent)"></span><span class="t">状态</span></div>
            <div class="state-grid">
              <div class="state-cell ok"><span class="dot"></span><div><div class="sc-k">LINK</div><div class="sc-v">OK</div></div></div>
              <div class="state-cell ok"><span class="dot"></span><div><div class="sc-k">LOCK</div><div class="sc-v">1</div></div></div>
              <div class="state-cell ok"><span class="dot"></span><div><div class="sc-k">GPS</div><div class="sc-v">3D FIX</div></div></div>
              <div class="state-cell warn"><span class="dot"></span><div><div class="sc-k">INS</div><div class="sc-v">ALIGN</div></div></div>
            </div>
          </div>
          <div class="rpanel-sec" style="flex:1;display:flex;flex-direction:column;min-height:0">
            <div class="rs-head"><span data-icon="flag" data-size="14" style="color:var(--accent)"></span><span class="t">事件</span><span class="badge">3</span></div>
            <div class="evt-list scroll">
              <div class="evt lvl-inf"><span class="evt-time">22:11:36</span><span class="evt-lvl">INF</span><span class="evt-msg">TRACE_MODE_CHANGE</span></div>
              <div class="evt lvl-ok"><span class="evt-time">22:11:35</span><span class="evt-lvl">OK</span><span class="evt-msg">LOCK_ACQUIRED</span></div>
              <div class="evt lvl-wrn"><span class="evt-time">22:10:02</span><span class="evt-lvl">WRN</span><span class="evt-msg">SNR &lt; 6dB</span></div>
              <div class="evt lvl-err"><span class="evt-time">22:08:44</span><span class="evt-lvl">ERR</span><span class="evt-msg">TRACK_LOST</span></div>
              <div class="evt lvl-inf"><span class="evt-time">22:08:40</span><span class="evt-lvl">INF</span><span class="evt-msg">SCAN_GLOBAL start</span></div>
            </div>
          </div>
        </aside>
      </div>`;
  }

  /* simple isometric phased-array + beam, pure SVG */
  function attitudeSVG() {
    return `<svg width="100%" height="196" viewBox="0 0 300 196" preserveAspectRatio="xMidYMid meet" style="display:block">
      <g transform="translate(150 108)">
        <line x1="0" y1="0" x2="86" y2="44" stroke="var(--err)" stroke-width="1.5" opacity=".8"/>
        <line x1="0" y1="0" x2="-86" y2="44" stroke="var(--ok)" stroke-width="1.5" opacity=".55"/>
        <line x1="0" y1="0" x2="0" y2="-78" stroke="var(--info)" stroke-width="1.5" opacity=".7"/>
        <text x="92" y="50" font-size="8" font-family="var(--font-mono)" fill="var(--err)">N</text>
        <text x="-98" y="50" font-size="8" font-family="var(--font-mono)" fill="var(--ok)">W</text>
        <text x="4" y="-80" font-size="8" font-family="var(--font-mono)" fill="var(--info)">Z</text>
        <g opacity=".95">
          <polygon points="-70,6 8,-30 78,2 0,40" fill="color-mix(in srgb,var(--info) 60%, #1f3140)" stroke="var(--info)" stroke-width="1"/>
          <polygon points="-70,6 8,-30 8,-24 -70,12" fill="color-mix(in srgb,var(--info) 30%, #0c1620)" stroke="var(--info)" stroke-width=".6"/>
          <polygon points="78,2 8,-30 8,-24 78,8" fill="color-mix(in srgb,var(--info) 22%, #0c1620)" stroke="var(--info)" stroke-width=".6"/>
        </g>
        <path d="M4,4 L40,-20 L26,-2 Z" fill="var(--err)" opacity=".9"/>
        <line x1="0" y1="-6" x2="46" y2="-96" stroke="var(--accent)" stroke-width="2" stroke-dasharray="1 3" opacity=".85"/>
        <circle cx="46" cy="-96" r="4" fill="var(--accent)"/>
        <circle cx="46" cy="-96" r="9" fill="none" stroke="var(--accent)" stroke-width="1" opacity=".4"/>
      </g>
    </svg>`;
  }

  /* ---------------- 07 · explorations ---------------- */
  function buildExplore() {
    const grid = $('#exploreGrid');
    const cards = [];

    // status strip A: GitHub-minimal
    cards.push(exploreCard('A1', '状态条 · 极简点阵', '单行 + 溢出收起', `
      <div style="display:flex;flex-wrap:wrap;gap:7px;align-items:center">
        <span class="chip is-ok"><span class="dot"></span>LINK <b>OK</b></span>
        <span class="chip is-info"><span class="dot"></span>TRACE <b>SCAN</b></span>
        <span class="chip is-ok"><span class="dot"></span>GPS <b>3D</b></span>
        <span class="chip is-warn"><span class="dot"></span>INS <b>ALIGN</b></span>
        <button class="btn btn--sm btn--ghost">+4</button>
      </div>`));

    // status strip B: dense LED board
    cards.push(exploreCard('A2', '状态条 · LED 灯板', '固定一行 · 不跳高', `
      <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:6px">
        ${[['LINK','OK','ok'],['LOCK','1','ok'],['GPS','3D','ok'],['INS','ALIGN','warn'],['TRACE','SCAN','info'],['PLL','LOCK','ok'],['MODEM','CONN','ok'],['BEAT','1Hz','info']].map(([k,v,c])=>`
          <div style="display:flex;align-items:center;gap:7px;padding:6px 9px;background:var(--card);border:1px solid var(--border);border-radius:6px">
            <span style="width:6px;height:6px;border-radius:50%;background:var(--${c==='ok'?'ok':c==='warn'?'warn':'info'});box-shadow:0 0 5px var(--${c==='ok'?'ok':c==='warn'?'warn':'info'})"></span>
            <span style="font:600 9px/1 var(--font-sans);letter-spacing:.05em;color:var(--text-3)">${k}</span>
            <span style="font:600 11px/1 var(--font-mono);color:var(--text);margin-left:auto">${v}</span>
          </div>`).join('')}
      </div>`));

    // channel panel A: grouped checkbox
    cards.push(exploreCard('B1', '通道面板 · 分组勾选', '勾选 = 显隐', `
      <div style="background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:6px">
        <div class="ch-group-label" style="padding-top:4px">姿态<span class="ln"></span></div>
        <div class="ch-row is-on"><span class="ch-box">${window.svgIcon('check',{size:10,stroke:2.6})}</span><span class="ch-swatch" style="background:${sig(1)}"></span><span class="ch-name">roll</span><span class="ch-val">−2.34</span></div>
        <div class="ch-row is-on"><span class="ch-box">${window.svgIcon('check',{size:10,stroke:2.6})}</span><span class="ch-swatch" style="background:${sig(3)}"></span><span class="ch-name">yaw</span><span class="ch-val">87.20</span></div>
        <div class="ch-row is-off"><span class="ch-box"></span><span class="ch-swatch" style="background:${sig(7)}"></span><span class="ch-name">snr</span><span class="ch-val">−83.1</span></div>
      </div>`));

    // channel panel B: eye-toggle rows
    cards.push(exploreCard('B2', '通道面板 · 眼睛开关', '色块=色 · 眼睛=显隐 · 值大字', `
      <div style="background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:8px;display:grid;gap:5px">
        ${[['roll','−2.34',1,1],['yaw','87.20',3,1],['snr','−83.10',7,0]].map(([nm,v,c,on])=>`
          <div style="display:grid;grid-template-columns:14px 1fr auto 18px;align-items:center;gap:9px;padding:5px 8px;border-radius:5px;background:${on?'var(--card-2)':'transparent'};opacity:${on?1:.55}">
            <span style="width:10px;height:10px;border-radius:3px;background:${sig(c)}"></span>
            <span style="font-size:12px;color:var(--text)">${nm}</span>
            <span class="num" style="font-size:14px;font-weight:600;color:var(--text)">${v}</span>
            <span data-icon="${on?'eye':'eyeOff'}" data-size="14" style="color:var(--text-3)"></span>
          </div>`).join('')}
      </div>`));

    // event timeline A: list w/ badges
    cards.push(exploreCard('C1', '事件线 · 等级徽标', '未读高亮 · 双击跳时刻', `
      <div style="background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:5px">
        <div class="evt lvl-err"><span class="evt-time">22:08:44</span><span class="evt-lvl">ERR</span><span class="evt-msg">TRACK_LOST</span></div>
        <div class="evt lvl-wrn"><span class="evt-time">22:10:02</span><span class="evt-lvl">WRN</span><span class="evt-msg">SNR below threshold</span></div>
        <div class="evt lvl-ok"><span class="evt-time">22:11:35</span><span class="evt-lvl">OK</span><span class="evt-msg">LOCK_ACQUIRED</span></div>
      </div>`));

    // event timeline B: rail
    cards.push(exploreCard('C2', '事件线 · 时间轨', '可视化密度 · 跳变定位', `
      <div style="background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:14px 12px">
        <div style="position:relative;height:8px;background:var(--card-2);border-radius:4px;margin-bottom:14px">
          ${[[12,'err'],[28,'wrn'],[55,'ok'],[63,'inf'],[78,'wrn'],[90,'ok']].map(([p,c])=>`<span style="position:absolute;left:${p}%;top:-3px;width:4px;height:14px;border-radius:2px;background:var(--${c==='err'?'err':c==='wrn'?'warn':c==='ok'?'ok':'info'})"></span>`).join('')}
        </div>
        <div style="display:flex;justify-content:space-between;font:500 9px/1 var(--font-mono);color:var(--text-3)"><span>00:00</span><span>10:00</span><span>20:00</span></div>
        <div class="evt lvl-err" style="margin-top:10px"><span class="evt-time">08:44</span><span class="evt-lvl">ERR</span><span class="evt-msg">TRACK_LOST · t=524s</span></div>
      </div>`));

    grid.innerHTML = '';
    cards.forEach(c => grid.appendChild(c));
  }
  function exploreCard(tag, nm, desc, body) {
    return el(`<div class="explore-card"><div class="ec-head"><span class="ec-tag">${tag}</span><span class="ec-nm">${nm}</span><span class="ec-desc">${desc}</span></div><div class="ec-body">${body}</div></div>`);
  }

  /* ---------------- theme switch ---------------- */
  $('#themeSwitch').addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    document.documentElement.setAttribute('data-theme', b.dataset.t);
    $('#stage').setAttribute('data-theme', b.dataset.t);
    [...$('#themeSwitch').children].forEach(x => x.classList.toggle('on', x === b));
  });
  // keep palette columns fixed to their own themes (already set); stage follows global
  document.getElementById('stage').setAttribute('data-theme', 'dark');

  /* ---------------- build ---------------- */
  buildHero();
  buildExplore();
  window.hydrateIcons(document);
})();

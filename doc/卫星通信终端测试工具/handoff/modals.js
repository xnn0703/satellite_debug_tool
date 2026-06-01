/* ============================================================
   Modals — Settings dialog + Update (检查更新) flow
   ============================================================ */
(function () {
  const ICN = window.svgIcon;
  let overlay, modal;

  function ensure() {
    if (overlay) return;
    overlay = document.createElement('div'); overlay.className = 'modal-overlay';
    overlay.innerHTML = `<div class="modal" id="modal"></div>`;
    document.body.appendChild(overlay);
    modal = overlay.querySelector('#modal');
    overlay.addEventListener('click', e => { if (e.target === overlay) close(); });
    document.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });
  }
  function open(html, w) { ensure(); modal.style.width = (w || 460) + 'px'; modal.innerHTML = html; window.hydrateIcons(modal); overlay.classList.add('on'); }
  function close() { if (overlay) overlay.classList.remove('on'); }

  /* ---------- settings ---------- */
  function openSettings() {
    open(`
      <div class="modal-h"><span data-icon="settings" data-size="16" style="color:var(--accent)"></span><span class="t">设置</span><span class="x" id="mX" data-icon="x" data-size="16"></span></div>
      <div class="modal-b">
        <div class="form-sec">
          <div class="fs-t">${ICN('folder',{size:13})}目录配置</div>
          <div class="form-row"><label>录制 / 回放</label><div class="field"><input value="~/Documents/sat/rec" style="font-size:11px"><span data-icon="folder" data-size="13" style="color:var(--text-3);cursor:pointer"></span></div></div>
          <div class="form-row"><label>Log 导入</label><div class="field"><input value="~/Documents/sat/log" style="font-size:11px"><span data-icon="folder" data-size="13" style="color:var(--text-3);cursor:pointer"></span></div></div>
          <div class="form-row"><label>固件导入</label><div class="field"><input value="~/Documents/sat/fw" style="font-size:11px"><span data-icon="folder" data-size="13" style="color:var(--text-3);cursor:pointer"></span></div></div>
        </div>
        <div class="form-sec">
          <div class="fs-t">${ICN('columns',{size:13})}图表分组</div>
          <button class="btn"><span data-icon="layers" data-size="13"></span>管理图表分组…</button>
        </div>
        <div class="form-sec">
          <div class="fs-t">${ICN('refresh',{size:13})}自动更新</div>
          <label class="chk on" id="setAuto"><span class="box">${ICN('check',{size:11,stroke:2.6})}</span>启动时后台检查更新</label>
          <div class="form-row" style="margin-top:10px"><label>检查间隔（小时）</label><div class="field" style="width:90px"><input value="24" class="num"></div></div>
        </div>
      </div>
      <div class="modal-f"><button class="btn" id="mCancel">取消</button><button class="btn btn--primary" id="mOk">确定</button></div>`);
    modal.querySelector('#mX').onclick = close;
    modal.querySelector('#mCancel').onclick = close;
    modal.querySelector('#mOk').onclick = () => { close(); window.toast('设置已保存', 'ok'); };
    modal.querySelector('#setAuto').onclick = e => e.currentTarget.classList.toggle('on');
  }

  /* ---------- update flow ---------- */
  function openUpdate() {
    pageChecking();
    setTimeout(pageNewFound, 1400);
  }
  function shell(body) {
    open(`<div class="modal-h"><span data-icon="refresh" data-size="16" style="color:var(--accent)"></span><span class="t">检查更新</span><span class="x" id="mX" data-icon="x" data-size="16"></span></div>${body}`);
    const x = modal.querySelector('#mX'); if (x) x.onclick = close;
  }
  function pageChecking() {
    shell(`<div class="modal-b"><div class="update-hero"><div class="big" style="background:var(--accent-soft);color:var(--accent)">${ICN('refresh',{size:24})}</div><div style="font-size:14px;color:var(--text)">正在连接发版服务器…</div><div style="font-size:12px;color:var(--text-3)">当前版本 v1.0.0</div></div></div>`);
  }
  function pageNewFound() {
    shell(`<div class="modal-b">
        <div class="update-hero" style="padding-bottom:6px"><div class="big" style="background:var(--ok-soft);color:var(--ok)">${ICN('download',{size:24})}</div>
        <div style="font-size:15px;font-weight:600;color:var(--text)">发现新版本 v1.1.0</div><div style="font-size:12px;color:var(--text-3)">当前 v1.0.0 · 5 个分卷 · 150 MB</div></div>
        <div style="font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--text-3);font-weight:600;margin:6px 0 8px">更新内容</div>
        <div class="changelog">## v1.1.0
- 修复 yaw 跳变 bug
- 通道勾选改为控制曲线显隐
- 新增 SNR 阈值告警
- 状态条单行不跳高
- 事件时间线未读徽标</div>
      </div>
      <div class="modal-f"><button class="btn btn--ghost" id="uSkip">跳过此版本</button><button class="btn" id="uLater">稍后</button><button class="btn btn--primary" id="uGo"><span data-icon="download" data-size="13"></span>立即更新</button></div>`);
    modal.querySelector('#uSkip').onclick = () => { close(); window.toast('已跳过 v1.1.0'); };
    modal.querySelector('#uLater').onclick = close;
    modal.querySelector('#uGo').onclick = pageDownloading;
  }
  function pageDownloading() {
    shell(`<div class="modal-b"><div class="update-hero"><div class="big" style="background:var(--accent-soft);color:var(--accent)">${ICN('download',{size:24})}</div>
      <div style="font-size:14px;color:var(--text)">下载 v1.1.0</div><div id="uSub" style="font-size:12px;color:var(--text-3)">分卷 1 / 5</div></div>
      <div class="ota-prog" style="margin-top:6px"><div class="bar" id="uBar"></div></div>
      <div style="display:flex;justify-content:space-between;margin-top:8px"><span class="num" id="uPct" style="font-size:12px;color:var(--text-2)">0%</span><span class="num" id="uMb" style="font-size:12px;color:var(--text-3)">0 / 150 MB</span></div></div>
      <div class="modal-f"><button class="btn" id="uCancel">取消</button></div>`);
    modal.querySelector('#uCancel').onclick = close;
    let v = 0; const bar = modal.querySelector('#uBar'), pct = modal.querySelector('#uPct'), mb = modal.querySelector('#uMb'), sub = modal.querySelector('#uSub');
    const iv = setInterval(() => {
      v += 1.4 + Math.random() * 1.6; if (v >= 100) { v = 100; clearInterval(iv); setTimeout(pageLaunching, 400); }
      bar.style.width = v + '%'; pct.textContent = Math.round(v) + '%'; mb.textContent = (v * 1.5).toFixed(1) + ' / 150 MB';
      if (sub) sub.textContent = '分卷 ' + Math.min(5, Math.ceil(v / 20)) + ' / 5';
    }, 90);
  }
  function pageLaunching() {
    shell(`<div class="modal-b"><div class="update-hero"><div class="big" style="background:var(--ok-soft);color:var(--ok)">${ICN('bolt',{size:24})}</div>
      <div style="font-size:14px;color:var(--text)">正在启动升级器…</div><div style="font-size:12px;color:var(--text-3)">主程序即将退出，升级完成后自动启动新版</div></div></div>
      <div class="modal-f"><button class="btn btn--primary" id="uDone">好的</button></div>`);
    modal.querySelector('#uDone').onclick = () => { close(); window.toast('（演示）升级器已启动'); };
  }

  window.Modals = { openSettings, openUpdate, close };
})();

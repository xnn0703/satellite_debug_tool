/* ============================================================
   Shell — boot, tab switching, global controls, toast
   ============================================================ */
(function () {
  const view = document.getElementById('view');
  const tabsEl = document.getElementById('tabs');
  let current = null, currentName = null;

  function go(name) {
    if (name === currentName) return;
    if (current && current.unmount) current.unmount();
    view.innerHTML = '';
    currentName = name; current = window.Tabs[name];
    [...tabsEl.querySelectorAll('[data-tab]')].forEach(b => b.classList.toggle('is-active', b.dataset.tab === name));
    if (current && current.mount) current.mount(view);
  }
  tabsEl.addEventListener('click', e => { const b = e.target.closest('[data-tab]'); if (b) go(b.dataset.tab); });

  // theme cycle button
  const THEMES = ['dark', 'dark-hc', 'light'];
  document.getElementById('themeBtn').onclick = () => {
    const t = window.Tweaks.get(); const i = THEMES.indexOf(t.theme);
    t.theme = THEMES[(i + 1) % THEMES.length]; window.Tweaks.apply(true);
    toast('主题：' + ({ dark: '深色', 'dark-hc': '强光高对比', light: '浅色' })[t.theme]);
  };
  document.getElementById('settingsBtn').onclick = () => window.Modals.openSettings();
  document.getElementById('updateBtn').onclick = () => window.Modals.openUpdate();

  // toast
  const wrap = document.getElementById('toastWrap');
  window.toast = function (msg, kind) {
    const el = document.createElement('div'); el.className = 'toast ' + (kind || '');
    el.innerHTML = `<span class="ti" data-icon="${kind === 'ok' ? 'check' : 'info'}" data-size="14"></span>${msg}`;
    wrap.appendChild(el); window.hydrateIcons(el);
    requestAnimationFrame(() => el.classList.add('on'));
    setTimeout(() => { el.classList.remove('on'); setTimeout(() => el.remove(), 250); }, 2600);
  };

  window.Shell = {
    go,
    redrawActive() { if (current && current.redrawTheme) current.redrawTheme(); },
  };

  window.hydrateIcons(document);
  go('live');
})();

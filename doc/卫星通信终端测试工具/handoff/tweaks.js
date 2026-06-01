/* ============================================================
   Tweaks panel (vanilla) — host edit-mode protocol + controls
   ============================================================ */
(function () {
  const ICN = window.svgIcon;
  const LS = 'sdt_tweaks_v1';
  const ACCENTS = {
    teal:   { a: '#2DD4BF', a2: '#5EEAD4', dim: '#14B8A6', soft: '#2dd4bf1f', ink: '#04110F' },
    sky:    { a: '#38BDF8', a2: '#7DD3FC', dim: '#0EA5E9', soft: '#38bdf81f', ink: '#05121b' },
    violet: { a: '#A78BFA', a2: '#C4B5FD', dim: '#8B5CF6', soft: '#a78bfa1f', ink: '#150b29' },
    amber:  { a: '#FBBF24', a2: '#FCD34D', dim: '#F59E0B', soft: '#fbbf241f', ink: '#1a1403' },
  };
  const DEFAULTS = { theme: 'dark', density: 'compact', accent: 'teal', grid: true, glow: true };
  let t = Object.assign({}, DEFAULTS);

  function apply(redraw) {
    const r = document.documentElement;
    r.setAttribute('data-theme', t.theme);
    r.setAttribute('data-density', t.density);
    r.setAttribute('data-accent', t.accent);
    const ac = ACCENTS[t.accent];
    r.style.setProperty('--accent', ac.a); r.style.setProperty('--accent-2', ac.a2);
    r.style.setProperty('--accent-dim', ac.dim); r.style.setProperty('--accent-soft', ac.soft);
    r.style.setProperty('--accent-ink', ac.ink);
    r.style.setProperty('--grid-line', t.grid ? '' : 'transparent');
    if (!t.grid) r.style.setProperty('--grid-line', 'transparent'); else r.style.removeProperty('--grid-line');
    r.classList.toggle('no-glow', !t.glow);
    window.parent.postMessage({ type: '__edit_mode_set_keys', edits: t }, '*');
    if (redraw && window.Shell) window.Shell.redrawActive();
    // sync theme button glyph
    const tb = document.querySelector('#themeBtn span'); if (tb) tb.outerHTML = ICN(t.theme === 'light' ? 'sun' : t.theme === 'dark-hc' ? 'contrast' : 'moon', { size: 15 });
  }

  let panel;
  function buildPanel() {
    panel = document.createElement('div'); panel.className = 'tweaks hidden';
    panel.innerHTML = `
      <div class="tweaks-h" id="twDrag"><span data-icon="sliders" data-size="15" style="color:var(--accent)"></span><span class="t">Tweaks</span><span class="x" id="twClose" data-icon="x" data-size="15"></span></div>
      <div class="tweaks-b">
        <div class="tw-sec"><div class="tw-l">主题</div><div class="tw-seg" data-k="theme">
          <button data-v="dark">深色</button><button data-v="dark-hc">强光</button><button data-v="light">浅色</button></div></div>
        <div class="tw-sec"><div class="tw-l">密度</div><div class="tw-seg" data-k="density">
          <button data-v="compact">紧凑</button><button data-v="comfy">舒适</button></div></div>
        <div class="tw-sec"><div class="tw-l">强调色</div><div class="tw-swatches" id="twAccent">
          ${Object.keys(ACCENTS).map(k => `<span class="tw-sw" data-v="${k}" style="background:${ACCENTS[k].a}"></span>`).join('')}</div></div>
        <div class="tw-sec"><div class="tw-l">图表</div>
          <label class="chk" id="twGrid" style="margin-bottom:8px"><span class="box">${ICN('check',{size:11,stroke:2.6})}</span>网格线</label>
          <label class="chk" id="twGlow"><span class="box">${ICN('check',{size:11,stroke:2.6})}</span>信号辉光</label></div>
      </div>`;
    document.body.appendChild(panel);
    window.hydrateIcons(panel);
    panel.querySelector('#twClose').onclick = () => { hide(); window.parent.postMessage({ type: '__edit_mode_dismissed' }, '*'); };
    panel.querySelectorAll('.tw-seg').forEach(seg => seg.onclick = (e) => {
      const b = e.target.closest('[data-v]'); if (!b) return; t[seg.dataset.k] = b.dataset.v; sync(); apply(true);
    });
    panel.querySelector('#twAccent').onclick = (e) => { const s = e.target.closest('[data-v]'); if (!s) return; t.accent = s.dataset.v; sync(); apply(true); };
    panel.querySelector('#twGrid').onclick = () => { t.grid = !t.grid; sync(); apply(true); };
    panel.querySelector('#twGlow').onclick = () => { t.glow = !t.glow; sync(); apply(true); };
    dragify(panel.querySelector('#twDrag'), panel);
    sync();
  }
  function sync() {
    panel.querySelectorAll('.tw-seg').forEach(seg => seg.querySelectorAll('[data-v]').forEach(b => b.classList.toggle('on', b.dataset.v === t[seg.dataset.k])));
    panel.querySelectorAll('#twAccent .tw-sw').forEach(s => s.classList.toggle('on', s.dataset.v === t.accent));
    panel.querySelector('#twGrid').classList.toggle('on', t.grid);
    panel.querySelector('#twGlow').classList.toggle('on', t.glow);
  }
  function dragify(handle, el) {
    let sx, sy, ox, oy, on = false;
    handle.addEventListener('pointerdown', e => { if (e.target.id === 'twClose') return; on = true; sx = e.clientX; sy = e.clientY; const r = el.getBoundingClientRect(); ox = r.left; oy = r.top; el.style.right = 'auto'; el.style.bottom = 'auto'; el.style.left = ox + 'px'; el.style.top = oy + 'px'; handle.setPointerCapture(e.pointerId); });
    handle.addEventListener('pointermove', e => { if (!on) return; el.style.left = (ox + e.clientX - sx) + 'px'; el.style.top = (oy + e.clientY - sy) + 'px'; });
    handle.addEventListener('pointerup', () => on = false);
  }
  function show() { if (!panel) buildPanel(); panel.classList.remove('hidden'); }
  function hide() { if (panel) panel.classList.add('hidden'); }

  window.addEventListener('message', (e) => {
    const ty = e && e.data && e.data.type;
    if (ty === '__activate_edit_mode') show();
    else if (ty === '__deactivate_edit_mode') hide();
  });
  window.parent.postMessage({ type: '__edit_mode_available' }, '*');

  window.Tweaks = { apply, get: () => t };
  apply(false);
})();

/* ============================================================
   Telemetry engine — channels, mock streaming, state machine
   window.App holds all shared state for the prototype.
   ============================================================ */
(function () {
  const CHANNELS = [
    { key: 'roll',     group: '姿态', sig: 1,  unit: '°',  def: true  },
    { key: 'pitch',    group: '姿态', sig: 6,  unit: '°',  def: true  },
    { key: 'yaw',      group: '姿态', sig: 3,  unit: '°',  def: true  },
    { key: 'ins_yaw',  group: '姿态', sig: 2,  unit: '°',  def: true  },
    { key: 'ant_az',   group: '指向', sig: 4,  unit: '°',  def: true  },
    { key: 'ant_el',   group: '指向', sig: 10, unit: '°',  def: true  },
    { key: 'ant_skew', group: '指向', sig: 12, unit: '°',  def: false },
    { key: 'tgt_az',   group: '指向', sig: 7,  unit: '°',  def: false },
    { key: 'tgt_el',   group: '指向', sig: 9,  unit: '°',  def: false },
    { key: 'snr',      group: '信号', sig: 3,  unit: 'dB', def: true  },
    { key: 'snr_avg',  group: '信号', sig: 6,  unit: 'dB', def: true  },
    { key: 'snr_raw',  group: '信号', sig: 12, unit: 'dB', def: false },
    { key: 'scan_loss',group: '信号', sig: 2,  unit: '',   def: false },
    { key: 'gps_lat',  group: '位置', sig: 9,  unit: '°',  def: false },
    { key: 'gps_lon',  group: '位置', sig: 10, unit: '°',  def: false },
  ];
  const GROUPS = ['姿态', '指向', '信号', '位置'];

  // smoothed noise per channel
  const noiseState = {};
  function noise(k, amp, damp) {
    const s = noiseState[k] || (noiseState[k] = 0);
    noiseState[k] = s * (damp == null ? 0.85 : damp) + (Math.random() - 0.5) * amp;
    return noiseState[k];
  }

  const App = {
    CHANNELS, GROUPS,
    win: 600,                // points in live window
    dt: 0.1,                 // 10 Hz
    t: 0,
    connected: false,
    recording: false, recT: 0,
    mode: 'SCAN',            // SCAN | TRACK
    modeT: 0, modeDur: 14,
    lock: 0,
    insAligned: false, insT: 0,
    tgtAz: 271.3, tgtEl: 42.0, heading: 87.0,
    buffers: {},
    visible: {},
    values: {},
    events: [],
    frames: 0, errors: 0,
    listeners: { tick: [], event: [], state: [] },
    gpsPath: [],

    on(ev, fn) { this.listeners[ev].push(fn); return () => this.off(ev, fn); },
    off(ev, fn) { const a = this.listeners[ev]; const i = a.indexOf(fn); if (i >= 0) a.splice(i, 1); },
    emit(ev, d) { this.listeners[ev].slice().forEach(f => f(d)); },

    reset() {
      this.t = 0; this.mode = 'SCAN'; this.modeT = 0; this.lock = 0;
      this.insAligned = false; this.insT = 0; this.frames = 0; this.errors = 0;
      this.events = []; this.gpsPath = [];
      CHANNELS.forEach(c => { this.buffers[c.key] = []; this.values[c.key] = 0;
        if (this.visible[c.key] === undefined) this.visible[c.key] = c.def; });
    },

    seed(seconds) {
      const n = Math.round(seconds / this.dt);
      for (let i = 0; i < n; i++) this.step(true);
    },

    pushEvent(lvl, code, msg) {
      const tm = new Date();
      const e = { lvl, code, msg, t: this.t,
        time: tm.toTimeString().slice(0, 8) + '.' + String(tm.getMilliseconds()).padStart(3, '0') };
      this.events.unshift(e);
      if (this.events.length > 200) this.events.pop();
      this.emit('event', e);
    },

    step(silent) {
      const t = this.t;
      // mode machine
      this.modeT += this.dt;
      if (this.modeT >= this.modeDur) {
        this.modeT = 0;
        if (this.mode === 'SCAN') {
          this.mode = 'TRACK'; this.lock = 1; this.modeDur = 22 + Math.random() * 14;
          if (!silent) { this.pushEvent('ok', 'LOCK_ACQUIRED', 'LOCK_ACQUIRED · az=' + this.tgtAz.toFixed(1)); }
        } else {
          this.mode = 'SCAN'; this.lock = 0; this.modeDur = 8 + Math.random() * 8;
          if (!silent) { this.pushEvent('err', 'TRACK_LOST', 'TRACK_LOST → SCAN_GLOBAL'); this.pushEvent('inf', 'TRACE_MODE_CHANGE', 'TRACE_MODE → SCAN_GLOBAL'); }
        }
      }
      // INS alignment after connect
      if (!this.insAligned) { this.insT += this.dt; if (this.insT > 6) { this.insAligned = true; if (!silent) this.pushEvent('ok', 'INS_ALIGN_DONE', 'INS_STATUS → ALIGN_DONE'); } }

      const track = this.mode === 'TRACK';
      const v = {};
      v.roll = -0.2 + Math.sin(t * 0.18) * 1.1 + noise('roll', 0.25);
      v.pitch = 0.5 + Math.cos(t * 0.13) * 0.9 + noise('pitch', 0.2);
      v.heading = this.heading + Math.sin(t * 0.05) * 3;
      v.yaw = v.heading + (track ? noise('yaw', 0.4) : Math.sin(t * 0.3) * 8 + noise('yaw', 1.2));
      v.ins_yaw = v.yaw + 0.4 + noise('ins', 0.3);
      // antenna azimuth: sweep during scan, lock during track
      if (track) v.ant_az = this.tgtAz + noise('antaz', 0.6);
      else v.ant_az = ((t * 36) % 360);
      v.ant_el = this.tgtEl + Math.sin(t * 0.2) * 2 + noise('antel', 0.4);
      v.ant_skew = 6 + Math.sin(t * 0.1) * 4 + noise('skew', 0.3);
      v.tgt_az = this.tgtAz; v.tgt_el = this.tgtEl;
      const base = track ? 9.2 + Math.sin(t * 0.6) * 2.4 : 1.0;
      v.snr = Math.max(0, base + noise('snr', track ? 1.2 : 0.5));
      v.snr_raw = Math.max(0, v.snr + noise('snrr', 1.6));
      // snr_avg smoothing
      const prevAvg = this.values.snr_avg || v.snr;
      v.snr_avg = prevAvg + (v.snr - prevAvg) * 0.06;
      v.scan_loss = Math.max(0, 0.4 + (track ? Math.abs(noise('scan', 0.8)) : 0.1) + Math.random() * 0.2);
      // gps drift (moving vehicle)
      const baseLat = 31.2304, baseLon = 121.4737;
      v.gps_lat = baseLat + Math.sin(t * 0.012) * 0.018 + t * 1e-6;
      v.gps_lon = baseLon + Math.cos(t * 0.01) * 0.022 + t * 1.4e-6;
      if (Math.round(t / this.dt) % 8 === 0) this.gpsPath.push([v.gps_lat, v.gps_lon]);

      // snr warning
      if (!silent && track && v.snr < 6 && Math.random() < 0.02) this.pushEvent('wrn', 'SNR_LOW', 'SNR ' + v.snr.toFixed(1) + ' dB below 6 dB');

      CHANNELS.forEach(c => {
        const val = v[c.key] != null ? v[c.key] : 0;
        this.values[c.key] = val;
        const buf = this.buffers[c.key] || (this.buffers[c.key] = []);
        buf.push(val);
        if (buf.length > this.win) buf.shift();
      });
      this.frames++;
      this.t += this.dt;
    },

    // ---- live runner ----
    _raf: null, _last: 0, _fpsT: 0, _fpsN: 0, fps: 0,
    start() {
      if (this._raf) return;
      this._last = performance.now();
      const loop = (now) => {
        this._raf = requestAnimationFrame(loop);
        const elapsed = now - this._last;
        if (elapsed >= 100) {                 // 10 Hz sim
          this._last = now - (elapsed % 100);
          const steps = Math.min(3, Math.floor(elapsed / 100));
          for (let i = 0; i < steps; i++) this.step(false);
          if (this.recording) this.recT += steps * this.dt;
          this._fpsN += steps;
          this.emit('tick');
        }
        this._fpsT += 0;
      };
      // fps counter
      this._fpsTimer = setInterval(() => { this.fps = this._fpsN; this._fpsN = 0; this.emit('state'); }, 1000);
      this._raf = requestAnimationFrame(loop);
    },
    stop() { if (this._raf) cancelAnimationFrame(this._raf); this._raf = null; clearInterval(this._fpsTimer); },
  };

  // sig color resolver
  App.color = (key) => {
    const c = CHANNELS.find(x => x.key === key);
    return c ? getComputedStyle(document.documentElement).getPropertyValue('--sig-' + c.sig).trim() : '#888';
  };

  // initialize buffers / visibility / values at load
  CHANNELS.forEach(c => { App.buffers[c.key] = []; App.values[c.key] = 0; App.visible[c.key] = c.def; });

  window.App = App;
})();

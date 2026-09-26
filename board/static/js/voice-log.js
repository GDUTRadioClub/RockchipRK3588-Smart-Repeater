/* ELF2 中继语音日志前端 */
(function () {
  'use strict';

  const CSRF = window.VLOG_CSRF || '';
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

  const state = {
    day: '',
    category: '',
    kind: '',
    q: '',
    pos: '',
    limit: 100,
    offset: 0,
    items: [],
    total: 0,
    selected: null,
    segs: [],
    peaks: [],
    playTimer: null,
    refreshing: false,
  };

  function toast(msg, type) {
    const el = $('#toast');
    if (!el) { console.log(msg); return; }
    el.textContent = msg;
    el.className = 'toast show ' + (type || '');
    clearTimeout(el._t);
    el._t = setTimeout(() => { el.className = 'toast ' + (type || ''); }, 3200);
  }

  async function api(url, opts) {
    const o = Object.assign({}, opts || {});
    o.headers = Object.assign({}, o.headers || {});
    if (o.method && o.method !== 'GET') {
      o.headers['X-CSRF-Token'] = CSRF;
      if (o.body && typeof o.body === 'string') o.headers['Content-Type'] = 'application/json';
    }
    const resp = await fetch(url, o);
    if (resp.status === 401) { location.href = '/login'; throw new Error('未登录'); }
    const ct = resp.headers.get('content-type') || '';
    if (!ct.includes('application/json')) {
      const t = await resp.text();
      throw new Error(t.slice(0, 120) || ('HTTP ' + resp.status));
    }
    const d = await resp.json();
    if (d && d.ok === false) throw new Error(d.error || '请求失败');
    return d;
  }

  const pad = (n) => String(n).padStart(2, '0');
  function hms(sec) {
    sec = Math.max(0, Math.round(sec || 0));
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    return h ? (h + ':' + pad(m) + ':' + pad(s)) : (m + ':' + pad(s));
  }
  function today() {
    const d = new Date();
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  }

  // ---------------- 状态条 ----------------
  async function loadStatus() {
    try {
      const d = await api('/api/voice/status');
      const rec = d.recording;
      const badge = $('#vlog-live-badge');
      badge.className = 'badge ' + (rec ? 'busy' : 'idle');
      badge.textContent = rec
        ? ('录音中 ' + (d.kind === 'tx' ? '发射' : d.kind === 'both' ? '收发' : '接收'))
        : (d.active ? '收尾中' : '空闲');

      const rx = $('#vlog-rx-badge');
      rx.className = 'badge ' + (d.rx ? 'on' : 'idle');
      rx.textContent = 'BUSY ' + (d.rx ? '有信号' : '空闲');
      const tx = $('#vlog-tx-badge');
      tx.className = 'badge ' + (d.tx ? 'busy' : 'idle');
      tx.textContent = 'PTT ' + (d.tx ? '发射中' : '释放');

      const t = d.today || {};
      $('#vlog-m-total').textContent = t.total || 0;
      $('#vlog-m-voice').textContent = (t.categories && t.categories.voice) ? t.categories.voice.n : 0;
      $('#vlog-m-sec').textContent = hms(t.seconds || 0);
      $('#vlog-m-mb').textContent = (d.size_mb || 0).toFixed(1);

      $('#vlog-st-rec').textContent = rec
        ? ('正在写入 · ' + (d.stats && d.stats.error ? ('异常: ' + d.stats.error) : '正常'))
        : ('待触发 · 已记录 ' + ((d.stats && d.stats.sessions) || 0) + ' 次会话 / '
           + ((d.stats && d.stats.segments) || 0) + ' 段');
      $('#vlog-st-asr').textContent = d.asr_running
        ? ('处理中 ' + ((d.asr_current && d.asr_current.file) || ''))
        : (d.queue ? (d.queue + ' 条待处理') : '空闲')
          + (d.counters ? (' · 完成 ' + (d.counters.asr_done || 0)
            + ' 失败 ' + (d.counters.asr_error || 0)) : '');
      $('#vlog-st-vad').textContent = (d.vad && d.vad.available)
        ? ('silero 已加载 · ' + (d.vad.model || '').split('/').pop())
        : ('silero ' + ((d.vad && d.vad.state) || '不可用')
           + (d.vad && d.vad.error ? ('（' + d.vad.error + '）') : ''));
      $('#vlog-st-llm').textContent = (d.llm && d.llm.ready)
        ? '就绪（常驻中）'
        : ((d.llm && d.llm.running) ? '启动中…' : '未加载（按需启动）');
      const cats = (t.categories || {});
      $('#vlog-st-cats').textContent = Object.keys(cats).length
        ? Object.keys(cats).map(k => k + ' ' + cats[k].n).join(' / ')
        : '--';
      const lv = d.level || {};
      const lvEl = $('#vlog-st-level');
      if (lvEl) {
        lvEl.textContent = '最近峰值 ' + (lv.recent_peak || 0) + ' / rms '
          + (lv.recent_dbfs === undefined ? '--' : lv.recent_dbfs) + ' dBFS'
          + (lv.clipped ? ('　⚠ ' + lv.clipped + ' 条削顶，请降低 PGA') : '');
        lvEl.className = lv.clipped ? 'vlog-warn' : '';
        lvEl.title = lv.hint || '';
      }
      const status = d.status || {};
      $('#vlog-st-ret').textContent = '保留 ' + ((d.retention && d.retention.days) || 30) + ' 天 / '
        + ((d.retention && d.retention.mb) || 20480) + ' MB · 当前 '
        + (d.files || 0) + ' 文件';
      if (d.summary) {
        const s = d.summary;
        $('#vlog-sum-state').textContent = s.running
          ? ('生成中：' + (s.stage || '') + '（' + (s.chunks || 0) + ' 块）')
          : ('日报时间 ' + (d.summary_time || '23:30') + ' · 引擎 '
             + ({ auto: '自动', local: '本地 LLM', external: '外部 API' }[d.summary_provider] || d.summary_provider)
             + (s.error ? (' · 上次失败: ' + s.error) : ''));
      }
    } catch (e) { /* 静默，避免刷屏 */ }
  }

  // ---------------- 日期列表 ----------------
  async function loadDays() {
    try {
      const d = await api('/api/voice/days');
      const sel = $('#vlog-day');
      const cur = state.day || today();
      const days = (d.days || []).map(x => x.day);
      if (!days.includes(cur)) days.unshift(cur);
      sel.innerHTML = '';
      days.forEach(day => {
        const o = document.createElement('option');
        o.value = day; o.textContent = day;
        sel.appendChild(o);
      });
      sel.value = cur;
      state.day = cur;
    } catch (e) { toast(e.message, 'error'); }
  }

  // ---------------- 列表 ----------------
  async function loadList() {
    if (state.refreshing) return;
    state.refreshing = true;
    try {
      const qs = new URLSearchParams({
        day: state.day, limit: state.limit, offset: state.offset,
      });
      if (state.category) qs.set('category', state.category);
      if (state.kind) qs.set('kind', state.kind);
      if (state.q) qs.set('q', state.q);
      if (state.pos) qs.set('pos', state.pos);
      const d = await api('/api/voice/list?' + qs.toString());
      state.items = d.items || [];
      renderList();
      const total = (d.stats && d.stats.total) || 0;
      $('#vlog-page-info').textContent = '第 ' + (Math.floor(state.offset / state.limit) + 1)
        + ' 页 · 共 ' + total + ' 条';
      $('#btn-vlog-prev').disabled = state.offset <= 0;
      $('#btn-vlog-next').disabled = state.offset + state.limit >= total;
      const st = d.stats || {};
      $('#vlog-m-total-sub').textContent = '段 · 当前筛选 ' + state.items.length
        + (st.aprs_pos ? ' · 含位置 ' + st.aprs_pos : '');
      loadTimeline();
    } catch (e) {
      toast('加载失败：' + e.message, 'error');
    } finally {
      state.refreshing = false;
    }
  }

  function tag(cls, text) {
    const s = document.createElement('span');
    s.className = 'tag ' + cls;
    s.textContent = text;
    return s;
  }

  function renderList() {
    const box = $('#vlog-list');
    box.innerHTML = '';
    if (!state.items.length) {
      const d = document.createElement('div');
      d.className = 'muted small vlog-empty';
      d.textContent = '该日期（或筛选条件下）没有记录。中继一旦收到信号或本机发射就会自动生成。';
      box.appendChild(d);
      return;
    }
    state.items.forEach(it => {
      const row = document.createElement('div');
      row.className = 'vlog-item' + (state.selected === it.id ? ' sel' : '');
      row.dataset.id = it.id;

      const tm = document.createElement('div');
      tm.className = 'vlog-item-time';
      tm.textContent = (it.ts || '').slice(11, 19);

      const main = document.createElement('div');
      main.className = 'vlog-item-main';
      const tags = document.createElement('div');
      tags.className = 'vlog-item-tags';
      tags.appendChild(tag('k-' + it.kind, it.kind_label));
      tags.appendChild(tag('c-' + (it.category || 'pending'), it.category_label));
      (it.callsigns || []).forEach(cs => {
        const raw = (it.callsigns_raw || []).filter(x => x && x !== cs);
        const t = tag('cs', '呼号 ' + cs);
        if (raw.length) t.title = '识别原文为 ' + raw.join('/') + '，已按白名单纠错';
        tags.appendChild(t);
      });
      if (it.aprs_pos) {
        // 尾音里解出了对方的 APRS 位置信标：标出来，坐标放 tooltip
        const t = tag('aprs-pos', '位置 ' + (it.aprs_call || 'APRS'));
        const bits = ['本段含 APRS 位置信息'];
        if (it.aprs_call) bits.push('呼号 ' + it.aprs_call);
        if (it.aprs_lat != null && it.aprs_lon != null) {
          bits.push('坐标 ' + Number(it.aprs_lat).toFixed(4) + ', '
                    + Number(it.aprs_lon).toFixed(4));
        }
        t.title = bits.join('：');
        tags.appendChild(t);
      }
      if (it.asr_status === 'pending' || it.asr_status === 'running') {
        tags.appendChild(tag('asr-' + it.asr_status, it.asr_status === 'running' ? '识别中' : '待识别'));
      } else if (it.asr_status === 'error') {
        tags.appendChild(tag('asr-error', '识别失败'));
      }
      main.appendChild(tags);
      const txt = document.createElement('div');
      const hasText = (it.text || '').trim().length > 0;
      txt.className = 'vlog-item-text' + (hasText ? '' : ' no-text');
      txt.textContent = hasText ? it.text : ('（' + it.category_label + '，无识别文字）');
      main.appendChild(txt);

      const right = document.createElement('div');
      right.className = 'vlog-item-right';
      right.textContent = it.seconds.toFixed(1) + 's';

      row.appendChild(tm); row.appendChild(main); row.appendChild(right);
      row.addEventListener('click', () => selectItem(it.id));
      box.appendChild(row);
    });
  }

  // ---------------- 全天时间轴 + 区间缩放（range brush） ----------------
  // 缩放状态：**不持久化**，切日期/刷新都回到全天（作者确认）。
  const TL_FULL = 86400;
  const TL_MIN = 60;                 // 最小可缩放区间 60 秒
  const TL_STEPS = [60, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400];
  let tlZoom = { a: 0, b: TL_FULL };
  let tlItems = [];                  // 缓存当天段，缩放时不必重新请求
  let tlDayStart = 0;

  function tlFmt(sec) {
    sec = Math.max(0, Math.min(TL_FULL, Math.round(sec)));
    const p = n => String(n).padStart(2, '0');
    return p(Math.floor(sec / 3600)) + ':' + p(Math.floor((sec % 3600) / 60))
      + ':' + p(sec % 60);
  }

  // 配色优先级：APRS（含本机 APRS 发射）> 本机发射 > 语音 > 其它
  function tlCls(it) {
    const cat = it.category || '';
    if (cat === 'aprs') return 'cat-aprs';
    if (it.kind === 'tx' || it.kind === 'both') return 'cat-tx';
    if (cat === 'voice') return 'cat-voice';
    if (cat) return 'cat-' + cat;
    return 'cat-empty';
  }

  function tlIsFull() { return tlZoom.a <= 0 && tlZoom.b >= TL_FULL; }

  function drawAxis() {
    const axis = $('#vlog-tl-axis');
    if (!axis) return;
    const a = tlZoom.a, b = tlZoom.b, win = Math.max(1, b - a);
    const target = win / 6;
    let step = TL_STEPS[TL_STEPS.length - 1];
    for (const v of TL_STEPS) { if (v >= target) { step = v; break; } }
    const out = [];
    for (let t = Math.ceil(a / step) * step; t <= b + 1; t += step) {
      out.push('<span style="left:' + (((t - a) / win) * 100) + '%">'
        + tlFmt(t).slice(0, 5) + '</span>');
    }
    axis.innerHTML = out.join('');
  }

  function renderBlocks() {
    const track = $('#vlog-tl-track');
    if (!track) return;
    track.innerHTML = '';
    const a = tlZoom.a, b = tlZoom.b, win = Math.max(1, b - a);
    tlItems.forEach(it => {
      const t0 = it.epoch - tlDayStart;
      const t1 = t0 + (it.seconds || 1);
      if (t1 < a || t0 > b) return;                 // 完全在窗口外
      const left = Math.max(0, Math.min(100, ((t0 - a) / win) * 100));
      const w = Math.max(0, Math.min(100 - left, ((it.seconds || 1) / win) * 100));
      const el = document.createElement('div');
      el.className = 'vlog-tl-block ' + tlCls(it)
        + (it.kind === 'tx' ? ' kind-tx' : '') + (it.kind === 'both' ? ' kind-both' : '')
        + (state.selected === it.id ? ' sel' : '');
      el.style.left = left + '%';
      el.style.width = w + '%';
      el.title = (it.ts_label || it.ts || '') + ' · ' + (it.kind_label || it.kind || '')
        + ' · ' + (it.category_label || it.category || '待识别')
        + ' · ' + (it.seconds || 0).toFixed(1) + 's';
      el.dataset.id = it.id;
      el.addEventListener('click', () => selectItem(it.id));
      track.appendChild(el);
    });
    const nowSec = (Date.now() / 1000) - tlDayStart;
    if (state.day === today() && nowSec >= a && nowSec <= b) {
      const n = document.createElement('div');
      n.className = 'vlog-tl-now';
      n.style.left = (((nowSec - a) / win) * 100) + '%';
      track.appendChild(n);
    }
    markTimelineCursor();
  }

  function renderBrush() {
    const bg = $('#vlog-tl-brush-bg');
    const sel = $('#vlog-tl-brush-sel');
    const bar = $('#vlog-tl-brush');
    if (!bg || !sel || !bar) return;
    if (bg.dataset.n !== String(tlItems.length)) {
      bg.dataset.n = String(tlItems.length);
      bg.innerHTML = '';
      tlItems.forEach(it => {
        const left = Math.max(0, Math.min(100, ((it.epoch - tlDayStart) / TL_FULL) * 100));
        const w = Math.max(0.15, ((it.seconds || 1) / TL_FULL) * 100);
        const i = document.createElement('i');
        i.className = tlCls(it);
        i.style.left = left + '%';
        i.style.width = w + '%';
        bg.appendChild(i);
      });
    }
    sel.style.left = ((tlZoom.a / TL_FULL) * 100) + '%';
    sel.style.width = (((tlZoom.b - tlZoom.a) / TL_FULL) * 100) + '%';
    bar.classList.toggle('full', tlIsFull());
    $$('.vlog-tl-handle', bar).forEach(h => {
      h.setAttribute('aria-valuenow', String(Math.round(tlZoom[h.dataset.h])));
      h.setAttribute('aria-valuetext', tlFmt(tlZoom[h.dataset.h]));
    });
    const lbl = $('#vlog-tl-range');
    if (lbl) {
      lbl.textContent = tlIsFull() ? '全天 24 小时'
        : (tlFmt(tlZoom.a) + ' – ' + tlFmt(tlZoom.b) + '（跨度 ' + tlFmt(tlZoom.b - tlZoom.a) + '）');
    }
    const rst = $('#btn-tl-reset');
    if (rst) rst.hidden = tlIsFull();
    const hint = $('#vlog-tl-hint');
    if (hint) hint.hidden = !tlIsFull();
  }

  function tlSet(a, b, quiet) {
    a = Math.max(0, Math.min(TL_FULL - TL_MIN, a));
    b = Math.min(TL_FULL, Math.max(a + TL_MIN, b));
    if (b - a < TL_MIN) { b = Math.min(TL_FULL, a + TL_MIN); a = Math.max(0, b - TL_MIN); }
    tlZoom = { a, b };
    drawAxis(); renderBlocks(); renderBrush();
    if (!quiet && state.selected) {
      // 选中项被缩出窗口时保留选中态，但不再画游标（markTimelineCursor 已处理）
    }
  }

  function tlReset() { tlSet(0, TL_FULL); }

  function bindBrush() {
    const bar = $('#vlog-tl-brush');
    if (!bar) return;
    let drag = null;
    const secAt = ev => {
      const r = bar.getBoundingClientRect();
      return Math.max(0, Math.min(TL_FULL, ((ev.clientX - r.left) / Math.max(1, r.width)) * TL_FULL));
    };
    bar.addEventListener('pointerdown', ev => {
      const hEl = ev.target.closest('.vlog-tl-handle');
      if (hEl) {
        drag = { mode: hEl.dataset.h };
      } else if (ev.target.closest('.vlog-tl-brush-sel')) {
        drag = { mode: 'pan', startX: ev.clientX, a0: tlZoom.a, b0: tlZoom.b };
      } else {
        // 点空白：以该时刻为中心平移（保持跨度）
        const t = secAt(ev), half = (tlZoom.b - tlZoom.a) / 2;
        tlSet(t - half, t + half);
        return;
      }
      try { bar.setPointerCapture(ev.pointerId); } catch (e) { /* 忽略 */ }
      const selEl = $('#vlog-tl-brush-sel');
      if (selEl) selEl.classList.add('dragging');
      ev.preventDefault();
    });
    bar.addEventListener('pointermove', ev => {
      if (!drag) return;
      if (drag.mode === 'pan') {
        const r = bar.getBoundingClientRect();
        const dt = ((ev.clientX - drag.startX) / Math.max(1, r.width)) * TL_FULL;
        let a = drag.a0 + dt, b = drag.b0 + dt;
        if (a < 0) { b -= a; a = 0; }
        if (b > TL_FULL) { a -= (b - TL_FULL); b = TL_FULL; }
        tlSet(a, b);
      } else {
        const t = secAt(ev);
        if (drag.mode === 'a') tlSet(Math.min(t, tlZoom.b - TL_MIN), tlZoom.b);
        else tlSet(tlZoom.a, Math.max(t, tlZoom.a + TL_MIN));
      }
      ev.preventDefault();
    });
    const end = () => {
      drag = null;
      const selEl = $('#vlog-tl-brush-sel');
      if (selEl) selEl.classList.remove('dragging');
    };
    bar.addEventListener('pointerup', end);
    bar.addEventListener('pointercancel', end);
    bar.addEventListener('dblclick', ev => { tlReset(); ev.preventDefault(); });
    // 键盘可达：Tab 聚焦手柄后用方向键微调，Shift 加速
    $$('.vlog-tl-handle', bar).forEach(h => {
      h.addEventListener('keydown', ev => {
        const step = ev.shiftKey ? 3600 : 300;
        const d = ev.key === 'ArrowLeft' ? -step : (ev.key === 'ArrowRight' ? step : 0);
        if (!d) return;
        ev.preventDefault();
        if (h.dataset.h === 'a') tlSet(tlZoom.a + d, tlZoom.b);
        else tlSet(tlZoom.a, tlZoom.b + d);
      });
    });
  }

  async function loadTimeline() {
    try {
      const d = await api('/api/voice/timeline?day=' + encodeURIComponent(state.day));
      tlItems = d.items || [];
      tlDayStart = new Date(state.day + 'T00:00:00').getTime() / 1000;
      tlZoom = { a: 0, b: TL_FULL };            // 换日期即回到全天
      const bg = $('#vlog-tl-brush-bg');
      if (bg) bg.dataset.n = '';                // 强制重画总览条
      tlSet(0, TL_FULL);
    } catch (e) { /* 忽略 */ }
  }

  function markTimelineCursor() {
    const track = $('#vlog-tl-track');
    if (!track) return;
    const old = track.querySelector('.vlog-tl-cursor');
    if (old) old.remove();
    if (!state.selected) return;
    const it = state.items.find(x => x.id === state.selected);
    if (!it) return;
    // 游标位置按**当前缩放窗口**换算；缩出窗口外就不画（避免跑到轨道外面）
    const t = (it.epoch - tlDayStart);
    const win = Math.max(1, tlZoom.b - tlZoom.a);
    if (t < tlZoom.a || t > tlZoom.b) return;
    const c = document.createElement('div');
    c.className = 'vlog-tl-cursor';
    c.style.left = (((t - tlZoom.a) / win) * 100) + '%';
    track.appendChild(c);
  }

  // ---------------- 详情 ----------------
  async function selectItem(id) {
    state.selected = id;
    // 目标不在当前缩放区间内时，把窗口平移过去（保持跨度），否则点了色块却看不到游标
    const hit = tlItems.find(x => x.id === id);
    if (hit) {
      const t0 = hit.epoch - tlDayStart;
      const t1 = t0 + (hit.seconds || 0);
      if (t1 < tlZoom.a || t0 > tlZoom.b) {
        const win = tlZoom.b - tlZoom.a;
        let a = (t0 + (hit.seconds || 0) / 2) - win / 2, b = a + win;
        if (a < 0) { b -= a; a = 0; }
        if (b > TL_FULL) { a -= (b - TL_FULL); b = TL_FULL; }
        tlSet(Math.max(0, a), Math.min(TL_FULL, b), true);
      }
    }
    $$('.vlog-item').forEach(el => el.classList.toggle('sel', Number(el.dataset.id) === id));
    const it = state.items.find(x => x.id === id);
    if (!it) return;
    $('#vlog-detail').classList.remove('hidden');
    $('#vlog-detail-title').textContent = (it.ts || '').replace('T', ' ');
    $('#vlog-d-kind').textContent = it.kind_label;
    $('#vlog-d-cat').textContent = it.category_label;
    $('#vlog-d-info').textContent = it.seconds.toFixed(1) + 's · rms '
      + (it.rms || 0).toFixed(0) + ' · 峰值 ' + (it.peak || 0) + ' · '
      + ((it.bytes || 0) / 1024).toFixed(0) + ' KB'
      + (it.rtf ? (' · RTF ' + it.rtf) : '')
      + ((it.callsigns || []).length ? (' · 呼号 ' + it.callsigns.join('/')) : '');
    $('#vlog-d-download').href = '/api/voice/' + id + '/download';

    const audio = $('#vlog-audio');
    audio.src = '/api/voice/' + id + '/audio';
    if ($('#vlog-autoplay').checked) {
      audio.play().catch(() => {});
    }
    renderSegs(it);
    markTimelineCursor();

    try {
      const d = await api('/api/voice/' + id + '/peaks?n=700');
      state.peaks = d.peaks || [];
      drawWave();
    } catch (e) { state.peaks = []; drawWave(); }
  }

  function renderSegs(it) {
    const box = $('#vlog-segs');
    box.innerHTML = '';
    const segs = it.segments || [];
    state.segs = segs;
    if (!segs.length) {
      const d = document.createElement('div');
      d.className = 'muted small';
      d.textContent = (it.text || '').trim()
        ? it.text
        : ('本条为「' + it.category_label + '」，未生成文字（非语音不送 ASR，避免幻觉）。');
      if ((it.text || '').trim() && it.asr_status === 'done') {
        d.className = 'vlog-seg vlog-seg-full';
      }
      box.appendChild(d);
      return;
    }
    segs.forEach(sg => {
      const row = document.createElement('div');
      row.className = 'vlog-seg';
      row.dataset.start = sg.start;
      row.dataset.end = sg.end;
      const t = document.createElement('div');
      t.className = 'vlog-seg-time';
      t.textContent = sg.start.toFixed(1) + 's';
      const x = document.createElement('div');
      x.textContent = sg.text;
      row.appendChild(t); row.appendChild(x);
      row.addEventListener('click', () => {
        const a = $('#vlog-audio');
        a.currentTime = Math.max(0, Number(sg.start) - 0.1);
        a.play().catch(() => {});
      });
      box.appendChild(row);
    });
  }

  function drawWave() {
    const cv = $('#vlog-canvas');
    if (!cv) return;
    const dpr = window.devicePixelRatio || 1;
    const w = cv.clientWidth || 480, h = 120;
    cv.width = w * dpr; cv.height = h * dpr;
    const g = cv.getContext('2d');
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);
    g.fillStyle = '#0d1526';
    g.fillRect(0, 0, w, h);
    const p = state.peaks || [];
    if (!p.length) {
      g.fillStyle = '#64748b'; g.font = '12px sans-serif';
      g.fillText('无波形数据', 10, h / 2);
      return;
    }
    const max = Math.max.apply(null, p) || 1;
    const step = w / p.length;
    g.fillStyle = '#3b82f6';
    for (let i = 0; i < p.length; i++) {
      const bh = Math.max(1, (p[i] / max) * (h - 8));
      g.fillRect(i * step, (h - bh) / 2, Math.max(1, step - 0.4), bh);
    }
    if (state.segs && state.segs.length) {
      g.fillStyle = 'rgba(34,197,94,.18)';
      const dur = $('#vlog-audio').duration || 0;
      if (dur > 0) {
        state.segs.forEach(sg => {
          g.fillRect((sg.start / dur) * w, 0, Math.max(1, ((sg.end - sg.start) / dur) * w), h);
        });
      }
    }
  }

  function onTimeUpdate() {
    const a = $('#vlog-audio');
    const t = a.currentTime;
    $$('#vlog-segs .vlog-seg').forEach(el => {
      const s = Number(el.dataset.start || -1), e = Number(el.dataset.end || -1);
      el.classList.toggle('act', s >= 0 && t >= s && t <= e);
    });
    const cv = $('#vlog-canvas');
    if (cv && a.duration > 0) {
      drawWave();
      const g = cv.getContext('2d');
      const dpr = window.devicePixelRatio || 1;
      g.setTransform(dpr, 0, 0, dpr, 0, 0);
      const w = cv.clientWidth || 480;
      g.fillStyle = '#f87171';
      g.fillRect((t / a.duration) * w, 0, 1.5, 120);
    }
  }

  // ---------------- 日报 ----------------
  async function loadSummary() {
    try {
      const d = await api('/api/voice/summary?day=' + encodeURIComponent(state.day));
      const s = d.summary || {};
      $('#vlog-summary').textContent = s.summary
        || ((d.transcript || '').trim()
          ? '该日期还没有生成日报。点击「立即生成日报」即可基于当日 ' +
            ((d.transcript || '').split('\n').length) + ' 条语音转写生成。'
          : '该日期没有可总结的语音通联内容。');
      $('#vlog-transcript').textContent = d.transcript || '（无）';
      if (s.status && s.status !== 'done' && s.error) {
        $('#vlog-summary').textContent = '上次生成失败：' + s.error;
      }
      const st = d.state || {};
      if (st.running) {
        $('#vlog-sum-state').textContent = '生成中：' + (st.stage || '') + '（' + (st.chunks || 0) + ' 块）';
      }
    } catch (e) { /* 忽略 */ }
  }

  async function runSummary() {
    const btn = $('#btn-vlog-summary-run');
    btn.disabled = true;
    try {
      const prov = $('#vlog-sum-provider').value;
      await api('/api/voice/summary/run', {
        method: 'POST',
        body: JSON.stringify({ day: state.day, provider: prov }),
      });
      toast('已开始生成日报，请稍候…', 'success');
      pollSummary();
    } catch (e) {
      toast('生成失败：' + e.message, 'error');
      btn.disabled = false;
    }
  }

  function pollSummary() {
    const btn = $('#btn-vlog-summary-run');
    let n = 0;
    clearInterval(state.playTimer);
    state.playTimer = setInterval(async () => {
      n++;
      await loadStatus();
      await loadSummary();
      const running = /生成中/.test($('#vlog-sum-state').textContent || '');
      if (!running || n > 120) {
        clearInterval(state.playTimer);
        btn.disabled = false;
        if (!running) toast('日报已更新', 'success');
      }
    }, 4000);
  }

  async function loadCsWhitelist() {
    try {
      const d = await api('/api/settings');
      const s = d.settings || {};
      const el = $('#vlog-cs-whitelist');
      if (el) el.value = s.vlog_callsign_whitelist || '';
    } catch (e) { /* 非管理员读取失败可忽略 */ }
  }

  async function saveCsWhitelist() {
    const el = $('#vlog-cs-whitelist');
    if (!el) return;
    try {
      await api('/api/settings', {
        method: 'POST',
        body: JSON.stringify({ vlog_callsign_whitelist: el.value }),
      });
      toast('呼号白名单已保存（对之后的新录音生效）', 'success');
      el.value = (el.value || '').toUpperCase();
    } catch (e) { toast('保存失败：' + e.message, 'error'); }
  }

  // ---------------- 事件 ----------------
  function bind() {
    $('#btn-vlog-refresh').addEventListener('click', () => { loadDays(); loadList(); loadSummary(); loadStatus(); });
    $('#vlog-day').addEventListener('change', (e) => {
      state.day = e.target.value; state.offset = 0; state.selected = null;
      tlZoom = { a: 0, b: TL_FULL };   // 换日期回到全天
      $('#vlog-detail').classList.add('hidden');
      loadList(); loadSummary();
    });
    $('#vlog-cat').addEventListener('change', (e) => { state.category = e.target.value; state.offset = 0; loadList(); });
    $('#vlog-kind').addEventListener('change', (e) => { state.kind = e.target.value; state.offset = 0; loadList(); });
    $('#vlog-pos').addEventListener('change', (e) => { state.pos = e.target.value; state.offset = 0; loadList(); });
    $('#btn-vlog-search').addEventListener('click', () => { state.q = $('#vlog-q').value.trim(); state.offset = 0; loadList(); });
    $('#vlog-q').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { state.q = e.target.value.trim(); state.offset = 0; loadList(); }
    });
    $('#btn-vlog-prev').addEventListener('click', () => {
      state.offset = Math.max(0, state.offset - state.limit); loadList();
    });
    $('#btn-vlog-next').addEventListener('click', () => {
      state.offset += state.limit; loadList();
    });
    $$('[data-export]').forEach(b => b.addEventListener('click', () => {
      window.location.href = '/api/voice/export?day=' + encodeURIComponent(state.day)
        + '&fmt=' + b.dataset.export;
    }));
    $('#vlog-audio').addEventListener('timeupdate', onTimeUpdate);
    $('#vlog-audio').addEventListener('loadedmetadata', drawWave);
    window.addEventListener('resize', drawWave);

    $('#vlog-canvas').addEventListener('click', (e) => {
      const cv = $('#vlog-canvas');
      const a = $('#vlog-audio');
      if (!a.duration) return;
      const rect = cv.getBoundingClientRect();
      const pct = (e.clientX - rect.left) / rect.width;
      a.currentTime = Math.max(0, Math.min(a.duration, pct * a.duration));
    });

    $('#btn-vlog-retranscribe').addEventListener('click', async () => {
      if (!state.selected) return;
      try {
        await api('/api/voice/' + state.selected + '/retranscribe', { method: 'POST', body: '{}' });
        toast('已加入识别队列', 'success');
        setTimeout(loadList, 2500);
      } catch (e) { toast(e.message, 'error'); }
    });

    $('#btn-vlog-delete').addEventListener('click', async () => {
      if (!state.selected) return;
      if (!confirm('确定删除这条录音及记录？此操作不可恢复。')) return;
      try {
        await api('/api/voice/' + state.selected + '/delete', { method: 'POST', body: '{}' });
        toast('已删除', 'success');
        state.selected = null;
        $('#vlog-detail').classList.add('hidden');
        loadDays(); loadList(); loadStatus();
      } catch (e) { toast(e.message, 'error'); }
    });

    $('#btn-vlog-summary-run').addEventListener('click', runSummary);
    $('#btn-vlog-cs-save').addEventListener('click', saveCsWhitelist);
  }

  // ---------------- 启动 ----------------
  document.addEventListener('DOMContentLoaded', async () => {
    bindBrush();
    const _rst = $('#btn-tl-reset');
    if (_rst) _rst.addEventListener('click', () => tlReset());
    await loadDays();
    bind();
    await loadStatus();
    await loadList();
    await loadSummary();
    await loadCsWhitelist();
    // 上一次 settle 之后再排下一次。原来用 setInterval 不等返回，
    // /api/voice/status 一变慢就会重叠堆积，把板端 GIL 抢死（见 static/js/poll.js）。
    ELF2Poll.loop(loadStatus, 3000, { immediate: true });
    ELF2Poll.loop(() => {
      const a = $('#vlog-audio');
      if (a && !a.paused) return;
      return loadList();
    }, 15000);
  });
})();

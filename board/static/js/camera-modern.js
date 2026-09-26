/* ELF2 现代化摄像头录像管理 / Hikvision 风格回放控制台
 * 独立于 app.js 运行，只依赖页面中的 cam-* 元素和后端 /api/camera/* 接口。
 */
(function () {
  'use strict';

  const DAY_MS = 24 * 60 * 60 * 1000;
  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || '';
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  const state = {
    initialized: false,
    date: localDate(new Date()),
    type: 'all',
    q: '',
    sort: 'desc',
    page: 1,
    pageSize: 25,
    segments: [],          // 当前日期全部分片
    timelineSegments: [],  // 按类型过滤后的时间轴分片
    filtered: [],          // 表格过滤后的分片
    selected: new Set(),
    storage: null,
    current: null,
    playlist: [],
    playheadMs: 0,
    timelineZoom: 1,
    follow: true,
    dragging: false,
    dragMoved: false,
    dragWasPlaying: false,
    dragStartX: 0,
    dragStartY: 0,
    dragStartBar: null,
    pendingSeekMs: 0,
    batchBusy: false,
    loading: false,
  };

  const els = {};
  let consoleFlashTimer = null;

  function cacheEls() {
    els.datePicker = $('#cam-date-picker');
    els.type = $('#cam-filter-type');
    els.zoom = $('#cam-timeline-zoom');
    els.follow = $('#cam-follow-play');
    els.timelineInner = $('#cam-timeline-inner');
    els.timelineScroll = $('#cam-timeline-scroll');
    els.timelineRuler = $('#cam-timeline-ruler');
    els.laneLoop = $('#cam-lane-loop');
    els.laneManual = $('#cam-lane-manual');
    els.playhead = $('#cam-timeline-playhead');
    els.tooltip = $('#cam-timeline-tooltip');
    els.consoleRoot = $('#cam-console');
    els.modeLiveBtn = $('#cam-mode-live');
    els.modePlayBtn = $('#cam-mode-play');
    els.liveImg = $('#camera-preview');
    els.liveBadge = $('#cam-live-badge');
    els.liveText = $('#cam-live-text');
    els.liveBusy = $('#cam-live-busy');
    els.video = $('#cam-modern-video');
    els.playerWrap = $('#cam-player-wrap');
    els.playerEmpty = $('#cam-player-empty');
    els.playerLoading = $('#cam-player-loading');
    els.playerTitle = $('#cam-player-title');
    els.playerState = $('#cam-player-state');
    els.progress = $('#cam-progress');
    els.progressPlayed = $('#cam-progress-played');
    els.progressBuffer = $('#cam-progress-buffer');
    els.progressThumb = $('#cam-progress-thumb');
    els.timeCurrent = $('#cam-time-current');
    els.timeDuration = $('#cam-time-duration');
    els.btnPlay = $('#cam-btn-play');
    els.btnPrev = $('#cam-btn-prev');
    els.btnNext = $('#cam-btn-next');
    els.playSpeed = $('#cam-play-speed');
    els.autoNext = $('#cam-auto-next');
    els.segmentsBody = $('#cam-segments-body');
    els.selectAll = $('#cam-select-all');
    els.search = $('#cam-segment-search');
    els.sort = $('#cam-segment-sort');
    els.pageSize = $('#cam-segment-page-size');
    els.pageInfo = $('#cam-page-info');
    els.pagePrev = $('#cam-page-prev');
    els.pageNext = $('#cam-page-next');
    els.selectedCount = $('#cam-selected-count');
    els.console = $('#cam-console');
    els.consoleCount = $('#cam-console-count-badge');
    els.consoleDate = $('#cam-console-date-badge');
    els.selectedInfo = $('#cam-selected-info');
    els.entryCount = $('#cam-entry-count');
    els.entrySize = $('#cam-entry-size');
    els.entryLoop = $('#cam-entry-loop');
  }

  // ------------------------------------------------------------------
  // 工具
  // ------------------------------------------------------------------
  function localDate(d) {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return `${y}-${m}-${day}`;
  }

  function dateStartMs(dateStr = state.date) {
    const d = new Date(`${dateStr}T00:00:00`);
    return d.getTime();
  }

  function shiftDate(dateStr, days) {
    const d = new Date(`${dateStr}T00:00:00`);
    d.setDate(d.getDate() + days);
    return localDate(d);
  }

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  function pad2(n) {
    return String(n).padStart(2, '0');
  }

  function fmtClock(seconds) {
    seconds = Math.max(0, Math.floor(Number(seconds) || 0));
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    return `${pad2(h)}:${pad2(m)}:${pad2(s)}`;
  }

  function fmtClockMs(ms) {
    return fmtClock((Number(ms) || 0) / 1000);
  }

  function fmtBytes(n) {
    n = Number(n || 0);
    if (n >= 1024 * 1024 * 1024) return (n / 1024 / 1024 / 1024).toFixed(2) + ' GB';
    if (n >= 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + ' MB';
    if (n >= 1024) return (n / 1024).toFixed(1) + ' KB';
    return n + ' B';
  }

  function fmtDuration(ms) {
    const seconds = Math.max(0, Math.round((Number(ms) || 0) / 1000));
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    if (h > 0) return `${h}时${pad2(m)}分${pad2(s)}秒`;
    if (m > 0) return `${m}分${pad2(s)}秒`;
    return `${s}秒`;
  }

  function escapeHtml(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function toast(message, type = '') {
    const el = $('#toast');
    if (!el) return;
    el.textContent = message;
    el.className = 'toast show ' + type;
    clearTimeout(el._camTimer);
    el._camTimer = setTimeout(() => { el.className = 'toast ' + type; }, 3200);
  }

  async function api(url, options = {}) {
    const headers = Object.assign({}, options.headers || {});
    if (!(options.body instanceof FormData)) {
      headers['Content-Type'] = 'application/json';
    }
    if (options.method && options.method !== 'GET') {
      headers['X-CSRF-Token'] = csrfToken;
    }
    const resp = await fetch(url, Object.assign({}, options, { headers }));
    if (resp.status === 401) {
      location.href = '/login';
      throw new Error('未登录');
    }
    const contentType = resp.headers.get('content-type') || '';
    const data = contentType.includes('application/json')
      ? await resp.json()
      : await resp.text();
    if (!resp.ok || (data && data.ok === false)) {
      throw new Error((data && data.error) || `HTTP ${resp.status}`);
    }
    return data;
  }

  function isCameraTabActive() {
    return !!$('#tab-camera')?.classList.contains('active');
  }

  function openConsole() {
    if (!state.initialized) init();
    if (!isCameraTabActive()) return;
    loadAll({ silent: true, keepPage: true });
    if (els.console) {
      els.console.scrollIntoView({ behavior: 'smooth', block: 'start' });
      els.console.classList.add('flash');
      clearTimeout(consoleFlashTimer);
      consoleFlashTimer = setTimeout(() => els.console?.classList.remove('flash'), 1800);
    }
  }

  function segByFilename(filename) {
    return state.segments.find((seg) => seg.filename === filename) || null;
  }

  function segAt(ms) {
    const list = state.playlist;
    for (const seg of list) {
      if (seg.start_ms <= ms && ms < seg.end_ms) return seg;
    }
    return null;
  }

  function nearestSeg(ms) {
    const list = state.playlist;
    if (!list.length) return null;
    let best = list[0];
    let bestDist = Math.abs(ms - best.start_ms);
    for (const seg of list) {
      const dist = Math.abs(ms - seg.start_ms);
      if (dist < bestDist) {
        best = seg;
        bestDist = dist;
      }
    }
    return best;
  }

  // ------------------------------------------------------------------
  // 数据加载
  // ------------------------------------------------------------------
  async function loadAll(options = {}) {
    if (state.loading) return;
    state.loading = true;
    const silent = !!options.silent;
    try {
      const [timelineData, storageData] = await Promise.all([
        api(`/api/camera/timeline?date=${encodeURIComponent(state.date)}`),
        api('/api/camera/storage'),
      ]);
      state.segments = timelineData.segments || [];
      state.storage = storageData.storage || timelineData.storage || null;
      if (state.current && !state.segments.some((seg) => seg.filename === state.current.filename)) {
        stopPlayback();
      }
      refreshPlaylist();
      if (!options.keepPage) state.page = 1;
      renderAll();
      if (!silent) toast('录像数据已刷新', 'success');
    } catch (e) {
      if (!silent) toast(e.message, 'error');
    } finally {
      state.loading = false;
    }
  }

  async function loadStorageOnly() {
    try {
      const data = await api('/api/camera/storage');
      state.storage = data.storage || state.storage;
      renderStorage();
    } catch (e) {
      // 静默轮询失败
    }
  }

  function refreshPlaylist() {
    const list = state.segments
      .filter((seg) => state.type === 'all' || seg.type === state.type)
      .sort((a, b) => (a.start_ms || 0) - (b.start_ms || 0));
    state.timelineSegments = list;
    state.playlist = list;
  }

  function renderAll() {
    renderStorage();
    renderTimeline();
    renderSegments();
    updateConsoleBadges();
    updateSelectedInfo();
    updatePlayerState();
  }

  // ------------------------------------------------------------------
  // 存储图形
  // ------------------------------------------------------------------
  function setBarWidth(sel, percent) {
    const el = $(sel);
    if (el) el.style.width = `${clamp(Number(percent) || 0, 0, 100)}%`;
  }

  function setGauge(id, percent) {
    const el = $(id);
    if (!el) return;
    const pct = clamp(Number(percent) || 0, 0, 100);
    const radius = 48;
    const circumference = 2 * Math.PI * radius;
    el.style.strokeDasharray = circumference.toFixed(2);
    el.style.strokeDashoffset = (circumference * (1 - pct / 100)).toFixed(2);
  }

  function renderStorage() {
    const s = state.storage;
    if (!s) return;
    const disk = s.disk || {};
    const rec = s.recordings || {};
    const lim = s.limits || {};
    const usage = s.usage || {};

    setGauge('#cam-gauge-disk', usage.disk_percent);
    setGauge('#cam-gauge-loop', usage.loop_percent);
    setGauge('#cam-gauge-storage', usage.storage_percent);
    $('#cam-gauge-disk-pct').textContent = `${Number(usage.disk_percent || 0).toFixed(1)}%`;
    $('#cam-gauge-loop-pct').textContent = `${Number(usage.loop_percent || 0).toFixed(1)}%`;
    $('#cam-gauge-storage-pct').textContent = `${Number(usage.storage_percent || 0).toFixed(1)}%`;

    const mediaSize = Number(rec.media_size || 0);
    const loopSize = Number(rec.loop_size || 0);
    const diskTotal = Number(disk.total || 0);
    const diskUsed = Number(disk.used || 0);
    const diskFree = Number(disk.free || 0);
    const loopCap = Number(lim.loop_max_bytes || 0);
    const storageCap = Number(lim.storage_max_bytes || 0);

    $('#cam-loop-size').textContent = fmtBytes(loopSize);
    $('#cam-loop-cap').textContent = loopCap > 0 ? `上限 ${fmtBytes(loopCap)}` : '未设置上限';
    $('#cam-loop-count').textContent = `${rec.loop_count || 0} 个分片`;
    setBarWidth('#cam-loop-bar', usage.loop_percent);

    $('#cam-storage-size').textContent = fmtBytes(mediaSize);
    $('#cam-storage-cap').textContent = storageCap > 0 ? `存储上限 ${fmtBytes(storageCap)}` : '按磁盘容量';
    $('#cam-storage-free').textContent = `剩余 ${fmtBytes(Math.max(0, storageCap - mediaSize))}`;
    setBarWidth('#cam-storage-bar', usage.storage_percent);

    $('#cam-disk-size').textContent = `${Number(usage.disk_percent || 0).toFixed(1)}%`;
    $('#cam-disk-detail').textContent = `${fmtBytes(diskUsed)} / ${fmtBytes(diskTotal)}`;
    $('#cam-disk-free').textContent = `可用 ${fmtBytes(diskFree)}`;
    setBarWidth('#cam-disk-bar', usage.disk_percent);

    $('#cam-storage-count').textContent = `${rec.count || 0} 条`;
    $('#cam-storage-loop-count').textContent = `${rec.loop_count || 0} 条`;
    $('#cam-storage-manual-count').textContent = `${rec.manual_count || 0} 条`;
    $('#cam-storage-duration').textContent = fmtDuration(rec.total_duration_ms);

    if (els.entryCount) els.entryCount.textContent = `${state.segments.length || 0} 个`;
    if (els.entrySize) els.entrySize.textContent = fmtBytes(mediaSize);
    if (els.entryLoop) {
      els.entryLoop.textContent = loopCap > 0
        ? `${fmtBytes(loopSize)} / ${fmtBytes(loopCap)}`
        : fmtBytes(loopSize);
    }

    const mode = lim.storage_cap_source === 'disk' ? '磁盘容量模式' : '策略上限模式';
    $('#cam-storage-mode').textContent = mode;
    $('#cam-policy-loop').textContent = loopCap > 0 ? fmtBytes(loopCap) : '不限';
    $('#cam-policy-storage').textContent = storageCap > 0
      ? fmtBytes(storageCap) + (lim.storage_cap_source === 'disk' ? '（磁盘）' : '')
      : '不限';
    $('#cam-policy-files').textContent = lim.loop_max_files ? `${lim.loop_max_files} 个` : '不限';
    $('#cam-policy-mode').textContent = mode;
  }

  // ------------------------------------------------------------------
  // 时间轴
  // ------------------------------------------------------------------
  function updateConsoleBadges() {
    if (els.consoleDate) els.consoleDate.textContent = state.date;
    if (els.consoleCount) {
      els.consoleCount.textContent = `${state.timelineSegments.length} 个分片`;
    }
  }

  function renderTimeline() {
    if (!els.timelineInner) return;
    const zoom = Number(state.timelineZoom) || 1;
    els.timelineInner.style.width = `${zoom * 100}%`;
    const dayStart = dateStartMs();
    const dayEnd = dayStart + DAY_MS;

    // 小时刻度
    let rulerHtml = '';
    for (let h = 0; h <= 24; h += 1) {
      const left = (h / 24) * 100;
      const major = h % 2 === 0;
      rulerHtml += `<div class="cam-timeline-tick${major ? ' major' : ''}" style="left:${left}%">`
        + (h < 24 ? `<span>${pad2(h)}:00</span>` : '')
        + '</div>';
    }
    els.timelineRuler.innerHTML = rulerHtml;

    const lanes = { loop: [], manual: [] };
    state.timelineSegments.forEach((seg) => {
      const start = Math.max(dayStart, Number(seg.start_ms) || dayStart);
      const rawEnd = Number(seg.end_ms) || ((Number(seg.start_ms) || dayStart) + (Number(seg.duration_ms) || 1000));
      const end = Math.min(dayEnd, rawEnd);
      if (end <= dayStart || start >= dayEnd) return;
      const left = ((start - dayStart) / DAY_MS) * 100;
      // 极小分片交给 CSS min-width 保证可见；这里保留真实时长比例，避免互相覆盖导致点不准
      const width = Math.max(0.002, ((end - start) / DAY_MS) * 100);
      const type = seg.type === 'manual' ? 'manual' : 'loop';
      const selected = state.current && state.current.filename === seg.filename ? ' selected' : '';
      lanes[type].push(
        `<div class="cam-segment-bar ${type}${selected}" data-cam-bar="${escapeHtml(seg.filename)}"`
        + ` style="left:${left}%;width:${width}%" title="${escapeHtml(seg.filename)}"></div>`
      );
    });
    els.laneLoop.innerHTML = lanes.loop.join('');
    els.laneManual.innerHTML = lanes.manual.join('');

    updatePlayheadPosition();

    // 时间轴变宽后，把当前播放位置滚到可视区域
    if (state.follow && !state.dragging) {
      requestAnimationFrame(scrollPlayheadIntoView);
    }
  }

  function updatePlayheadPosition() {
    if (!els.playhead || !els.timelineInner) return;
    const dayStart = dateStartMs();
    const ms = Number(state.playheadMs);
    if (!ms || ms < dayStart || ms > dayStart + DAY_MS) {
      els.playhead.classList.remove('show');
      return;
    }
    const pct = ((ms - dayStart) / DAY_MS) * 100;
    els.playhead.style.left = `${clamp(pct, 0, 100)}%`;
    els.playhead.classList.add('show');
  }

  function updatePlayhead(ms, options = {}) {
    state.playheadMs = ms;
    updatePlayheadPosition();
    if (!options.skipInfo) updateSelectedInfo();
    if (state.follow && !state.dragging) {
      scrollPlayheadIntoView();
    }
  }

  function scrollPlayheadIntoView() {
    if (!els.timelineScroll || !els.timelineInner) return;
    const dayStart = dateStartMs();
    const pct = clamp(((state.playheadMs - dayStart) / DAY_MS) * 100, 0, 100);
    const x = (els.timelineInner.clientWidth * pct) / 100;
    const viewLeft = els.timelineScroll.scrollLeft;
    const viewRight = viewLeft + els.timelineScroll.clientWidth;
    if (x < viewLeft + 60 || x > viewRight - 60) {
      els.timelineScroll.scrollLeft = Math.max(0, x - els.timelineScroll.clientWidth * 0.38);
    }
  }

  function timelineTimeFromEvent(evt) {
    const rect = els.timelineInner.getBoundingClientRect();
    const x = clamp(evt.clientX - rect.left, 0, rect.width);
    return dateStartMs() + (x / Math.max(1, rect.width)) * DAY_MS;
  }

  function onTimelinePointerDown(evt) {
    if (evt.button !== 0) return;
    state.dragging = true;
    state.dragMoved = false;
    state.dragWasPlaying = !!(els.video && !els.video.paused && state.current);
    state.dragStartX = evt.clientX;
    state.dragStartY = evt.clientY;
    state.dragStartBar = evt.target?.closest?.('[data-cam-bar]')?.dataset?.camBar || null;
    els.timelineInner.setPointerCapture?.(evt.pointerId);
    const ms = timelineTimeFromEvent(evt);
    updatePlayhead(ms, { skipInfo: true });
    updateTooltip(ms, evt, segAt(ms));
  }

  function onTimelinePointerMove(evt) {
    if (!state.dragging) {
      const ms = timelineTimeFromEvent(evt);
      const seg = segAt(ms);
      updateTooltip(ms, evt, seg);
      return;
    }
    const distance = Math.hypot(evt.clientX - state.dragStartX, evt.clientY - state.dragStartY);
    if (!state.dragMoved && distance > 4) state.dragMoved = true;
    if (state.dragMoved) {
      handleTimelineDrag(evt, true);
    } else {
      const ms = timelineTimeFromEvent(evt);
      updatePlayhead(ms, { skipInfo: true });
      updateTooltip(ms, evt, segAt(ms));
    }
  }

  function onTimelinePointerUp(evt) {
    if (!state.dragging) return;
    const wasMoved = state.dragMoved;
    const barName = state.dragStartBar;
    state.dragging = false;
    state.dragMoved = false;
    state.dragStartBar = null;
    els.timelineInner.releasePointerCapture?.(evt.pointerId);

    // 点击分片条：用几何命中找出鼠标下的分片条，短分片重叠时按中心距离取最近
    if (!wasMoved) {
      const hitName = barAtPoint(evt.clientX, evt.clientY) || barName;
      const clicked = hitName ? segByFilename(hitName) : null;
      if (clicked) {
        loadSegment(clicked, { offsetMs: 0, autoplay: true });
        return;
      }
    }

    const ms = timelineTimeFromEvent(evt);
    updatePlayhead(ms);
    const seg = segAt(ms);
    if (seg) {
      if (state.current && state.current.filename === seg.filename && els.video.duration) {
        els.video.currentTime = clamp((ms - seg.start_ms) / 1000, 0, Math.max(0, els.video.duration - 0.05));
      } else {
        loadSegment(seg, {
          offsetMs: Math.max(0, ms - seg.start_ms),
          autoplay: state.dragWasPlaying,
        });
      }
    }
  }

  function handleTimelineDrag(evt, allowLoad) {
    const ms = timelineTimeFromEvent(evt);
    updatePlayhead(ms, { skipInfo: true });
    updateTooltip(ms, evt);

    const seg = segAt(ms);
    if (!seg) return;
    if (state.current && state.current.filename === seg.filename && els.video.duration) {
      const offset = clamp((ms - seg.start_ms) / 1000, 0, Math.max(0, els.video.duration - 0.05));
      if (Math.abs((els.video.currentTime || 0) - offset) > 0.25) {
        els.video.currentTime = offset;
      }
    } else if (allowLoad) {
      loadSegment(seg, { offsetMs: ms - seg.start_ms, autoplay: false });
    }
  }

  function updateTooltip(ms, evt, seg = null) {
    if (!els.tooltip || !els.timelineInner) return;
    const rect = els.timelineInner.getBoundingClientRect();
    const x = clamp(evt.clientX - rect.left, 0, rect.width);
    els.tooltip.style.left = `${clamp(x, 4, Math.max(4, rect.width - 180))}px`;
    els.tooltip.style.top = `${evt.clientY - rect.top + 8}px`;
    let html = `<b>${pad2(new Date(ms).getHours())}:${pad2(new Date(ms).getMinutes())}:${pad2(new Date(ms).getSeconds())}</b>`
      + `<br>${escapeHtml(localDate(new Date(ms)))}`;
    if (seg) {
      html += `<br><span style="color:#93c5fd">${escapeHtml(seg.filename)}</span>`
        + `<br><span style="color:#94a3b8">${seg.start_hm} - ${seg.end_hm}</span>`;
    }
    els.tooltip.innerHTML = html;
    els.tooltip.classList.add('show');
  }

  function hideTooltip() {
    els.tooltip?.classList.remove('show');
  }

  function barAtPoint(clientX, clientY) {
    let best = null;
    let bestDistance = Infinity;
    $$('.cam-segment-bar').forEach((bar) => {
      const rect = bar.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) return;
      const hit = clientX >= rect.left - 1 && clientX <= rect.right + 1
        && clientY >= rect.top - 1 && clientY <= rect.bottom + 1;
      if (!hit) return;
      const distance = Math.abs(clientX - (rect.left + rect.width / 2));
      if (distance < bestDistance) {
        bestDistance = distance;
        best = bar.dataset.camBar;
      }
    });
    return best;
  }

  // ------------------------------------------------------------------
  // 播放器
  // ------------------------------------------------------------------
  function setPlayerLoading(show) {
    els.playerLoading?.classList.toggle('show', !!show);
  }

  function updatePlayerState() {
    if (!els.playerState) return;
    if (!state.current) {
      els.playerState.textContent = '已停止';
      els.playerEmpty?.classList.remove('hidden');
      els.playerTitle.textContent = '--';
      return;
    }
    const video = els.video;
    els.playerEmpty?.classList.add('hidden');
    els.playerTitle.textContent = state.current.filename;
    if (video && !video.paused) {
      els.playerState.textContent = '播放中';
    } else if (video && video.ended) {
      els.playerState.textContent = '播放结束';
    } else {
      els.playerState.textContent = '已暂停';
    }
  }

  function updatePlayButton() {
    if (!els.btnPlay) return;
    els.btnPlay.textContent = els.video && !els.video.paused ? '⏸' : '▶';
  }

  function updateSelectedInfo() {
    if (!els.selectedInfo) return;
    const seg = state.current;
    if (seg) {
      els.selectedInfo.textContent = `${seg.start_hm || '--'} - ${seg.end_hm || '--'} · ${seg.filename} · ${fmtBytes(seg.size)}`;
    } else if (state.playheadMs) {
      const d = new Date(state.playheadMs);
      els.selectedInfo.textContent = `时间轴位置：${localDate(d)} ${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
    } else {
      els.selectedInfo.textContent = '未选择分片';
    }
  }

  function markSelectedBars() {
    $$('[data-cam-bar]').forEach((bar) => {
      bar.classList.toggle('selected', !!state.current && bar.dataset.camBar === state.current.filename);
    });
  }

  function loadSegment(seg, options = {}) {
    if (!seg || !els.video) return;
    // 点时间轴/分片列表播放时自动从「实时预览」切到「录像回放」
    if (isLiveMode()) setMode('play');
    const video = els.video;
    const same = state.current && state.current.filename === seg.filename;
    state.current = seg;
    state.playheadMs = Number(seg.start_ms) + (Number(options.offsetMs) || 0);
    updateSelectedInfo();
    markSelectedBars();
    updateConsoleBadges();

    if (same && video.getAttribute('src')) {
      if (typeof options.offsetMs === 'number') {
        const duration = video.duration || (seg.duration_ms / 1000);
        video.currentTime = clamp(options.offsetMs / 1000, 0, Math.max(0, duration - 0.05));
      }
      if (options.autoplay) video.play().catch(() => {});
      return;
    }

    state.pendingSeekMs = Number(options.offsetMs) || 0;
    setPlayerLoading(true);
    video.src = seg.url;
    video.load();

    const onLoaded = () => {
      video.removeEventListener('loadedmetadata', onLoaded);
      setPlayerLoading(false);
      if (state.pendingSeekMs > 0 && video.duration) {
        video.currentTime = clamp(state.pendingSeekMs / 1000, 0, Math.max(0, video.duration - 0.05));
      }
      state.pendingSeekMs = 0;
      video.playbackRate = Number(els.playSpeed?.value || 1);
      if (options.autoplay) video.play().catch(() => {});
      updatePlayerState();
      updateProgressUI();
    };
    video.addEventListener('loadedmetadata', onLoaded, { once: true });
  }

  function playAtGlobalTime(ms, autoplay = true) {
    const seg = segAt(ms) || nearestSeg(ms);
    if (!seg) {
      toast('该时间点没有可用录像分片', 'error');
      return;
    }
    loadSegment(seg, { offsetMs: Math.max(0, ms - seg.start_ms), autoplay });
  }

  function currentPlaylistIndex() {
    if (!state.current) return -1;
    return state.playlist.findIndex((seg) => seg.filename === state.current.filename);
  }

  function playNext() {
    const index = currentPlaylistIndex();
    const next = state.playlist[index + 1];
    if (!next) {
      toast('已经是最后一个分片', 'error');
      return;
    }
    loadSegment(next, { autoplay: true, offsetMs: 0 });
  }

  function playPrev() {
    const index = currentPlaylistIndex();
    const prev = state.playlist[index - 1];
    if (!prev) {
      toast('已经是第一个分片', 'error');
      return;
    }
    loadSegment(prev, { autoplay: true, offsetMs: 0 });
  }

  function togglePlay() {
    if (!state.current) {
      playAtGlobalTime(state.playheadMs || dateStartMs() + 8 * 3600 * 1000, true);
      return;
    }
    if (els.video.paused) els.video.play().catch(() => {});
    else els.video.pause();
  }

  function updateProgressUI() {
    const video = els.video;
    const duration = Number(video?.duration) || (state.current?.duration_ms || 0) / 1000;
    const current = Number(video?.currentTime) || 0;
    const pct = duration > 0 ? clamp((current / duration) * 100, 0, 100) : 0;
    if (els.progressPlayed) els.progressPlayed.style.width = `${pct}%`;
    if (els.progressThumb) els.progressThumb.style.left = `${pct}%`;
    if (els.timeCurrent) els.timeCurrent.textContent = fmtClock(current);
    if (els.timeDuration) els.timeDuration.textContent = fmtClock(duration);
    if (video && video.buffered && video.buffered.length && duration > 0) {
      const bufferedEnd = video.buffered.end(video.buffered.length - 1);
      if (els.progressBuffer) els.progressBuffer.style.width = `${clamp((bufferedEnd / duration) * 100, 0, 100)}%`;
    }
  }

  function setProgressFromEvent(evt) {
    if (!els.progress || !els.video || !els.video.duration) return;
    const rect = els.progress.getBoundingClientRect();
    const pct = clamp((evt.clientX - rect.left) / Math.max(1, rect.width), 0, 1);
    els.video.currentTime = pct * els.video.duration;
    updateProgressUI();
  }

  function snapshotCurrentFrame() {
    if (!els.video || !state.current) {
      toast('请先播放一个分片', 'error');
      return;
    }
    try {
      const canvas = document.createElement('canvas');
      canvas.width = els.video.videoWidth || 1280;
      canvas.height = els.video.videoHeight || 720;
      canvas.getContext('2d').drawImage(els.video, 0, 0, canvas.width, canvas.height);
      const link = document.createElement('a');
      link.download = `snapshot_${state.current.start_hm.replace(/:/g, '')}.jpg`;
      link.href = canvas.toDataURL('image/jpeg', 0.92);
      link.click();
      toast('截图已保存', 'success');
    } catch (e) {
      toast('截图失败：' + e.message, 'error');
    }
  }

  function downloadCurrent() {
    if (!state.current) {
      toast('请先选择分片', 'error');
      return;
    }
    const link = document.createElement('a');
    link.href = `${state.current.url}?download=1`;
    link.download = state.current.filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
  }

  function toggleFullscreen() {
    if (!els.playerWrap) return;
    if (document.fullscreenElement) {
      document.exitFullscreen?.();
    } else {
      els.playerWrap.requestFullscreen?.();
    }
  }

  // ------------------------------------------------------------------
  // 表格 / 分片管理
  // ------------------------------------------------------------------
  function filteredSegments() {
    const q = state.q.trim().toLowerCase();
    let rows = state.segments.filter((seg) => state.type === 'all' || seg.type === state.type);
    if (q) {
      rows = rows.filter((seg) => {
        const hay = `${seg.filename} ${seg.start || ''} ${seg.start_hm || ''} ${seg.type || ''}`.toLowerCase();
        return hay.includes(q);
      });
    }
    rows.sort((a, b) => {
      const diff = (a.start_ms || 0) - (b.start_ms || 0);
      return state.sort === 'asc' ? diff : -diff;
    });
    return rows;
  }

  function renderSegments() {
    const rows = filteredSegments();
    state.filtered = rows;
    const total = rows.length;
    const pages = Math.max(1, Math.ceil(total / state.pageSize));
    state.page = clamp(state.page, 1, pages);
    const start = (state.page - 1) * state.pageSize;
    const pageRows = rows.slice(start, start + state.pageSize);
    if (!pageRows.length) {
      els.segmentsBody.innerHTML = '<tr><td colspan="7" class="cam-segment-empty">当前条件下暂无录像分片</td></tr>';
    } else {
      els.segmentsBody.innerHTML = pageRows.map((seg) => {
        const typeLabel = seg.type === 'loop' ? '循环' : (seg.type === 'manual' ? '手动' : '其他');
        const typeClass = seg.type === 'loop' ? 'loop' : (seg.type === 'manual' ? 'manual' : 'other');
        const checked = state.selected.has(seg.filename) ? ' checked' : '';
        return `<tr data-cam-row="${escapeHtml(seg.filename)}">
          <td class="cam-check-col"><input type="checkbox" data-cam-select="${escapeHtml(seg.filename)}"${checked}></td>
          <td title="${escapeHtml(seg.filename)}">${escapeHtml(seg.filename)}</td>
          <td><span class="cam-type-badge ${typeClass}">${typeLabel}</span></td>
          <td>${escapeHtml(seg.start || '--')}</td>
          <td>${fmtDuration(seg.duration_ms)}</td>
          <td>${fmtBytes(seg.size)}</td>
          <td>
            <div class="cam-row-actions">
              <button class="btn ghost" data-cam-play-file="${escapeHtml(seg.filename)}">播放</button>
              <button class="btn ghost" data-cam-download-file="${escapeHtml(seg.filename)}">下载</button>
              <button class="btn ghost danger" data-cam-del-file="${escapeHtml(seg.filename)}">删除</button>
            </div>
          </td>
        </tr>`;
      }).join('');
    }
    if (els.pageInfo) els.pageInfo.textContent = `第 ${state.page} / ${pages} 页 · 共 ${total} 条`;
    if (els.pagePrev) els.pagePrev.disabled = state.page <= 1;
    if (els.pageNext) els.pageNext.disabled = state.page >= pages;
    const pageNames = pageRows.map((seg) => seg.filename);
    if (els.selectAll) {
      els.selectAll.checked = pageNames.length > 0 && pageNames.every((name) => state.selected.has(name));
      els.selectAll.indeterminate = pageNames.some((name) => state.selected.has(name)) && !els.selectAll.checked;
    }
    updateSelectedCount();
  }

  function updateSelectedCount() {
    if (els.selectedCount) {
      const total = state.selected.size;
      const bytes = state.segments
        .filter((seg) => state.selected.has(seg.filename))
        .reduce((sum, seg) => sum + Number(seg.size || 0), 0);
      els.selectedCount.textContent = `已选 ${total} 项 · ${fmtBytes(bytes)}`;
    }
  }

  function toggleSelected(filename, checked) {
    if (checked) state.selected.add(filename);
    else state.selected.delete(filename);
    updateSelectedCount();
    renderSegments();
  }

  async function deleteFilenames(names) {
    if (!names.length) {
      toast('请选择要删除的分片', 'error');
      return;
    }
    if (!confirm(`确定删除选中的 ${names.length} 个录像分片？`)) return;
    state.batchBusy = true;
    try {
      const data = await api('/api/camera/recordings/batch_delete', {
        method: 'POST',
        body: JSON.stringify({ files: names }),
      });
      names.forEach((name) => state.selected.delete(name));
      if (state.current && names.includes(state.current.filename)) {
        stopPlayback();
      }
      toast(`已删除 ${data.deleted?.length || 0} 个分片`, 'success');
      await loadAll({ silent: true, keepPage: true });
    } catch (e) {
      toast(e.message, 'error');
    } finally {
      state.batchBusy = false;
    }
  }

  async function batchDownload() {
    const names = Array.from(state.selected);
    if (!names.length) {
      toast('请选择要下载的分片', 'error');
      return;
    }
    names.forEach((name, index) => {
      const seg = segByFilename(name);
      if (!seg) return;
      setTimeout(() => {
        const link = document.createElement('a');
        link.href = `${seg.url}?download=1`;
        link.download = seg.filename;
        document.body.appendChild(link);
        link.click();
        link.remove();
      }, index * 220);
    });
    toast(`已触发 ${names.length} 个分片下载`, 'success');
  }

  function exportCsv() {
    const rows = state.filtered.length ? state.filtered : state.segments;
    if (!rows.length) {
      toast('暂无可导出的分片', 'error');
      return;
    }
    const header = ['文件名', '类型', '开始时间', '结束时间', '时长(秒)', '大小(字节)'];
    const lines = [header.join(',')];
    rows.forEach((seg) => {
      lines.push([
        seg.filename,
        seg.type,
        seg.start,
        seg.end,
        Math.round((seg.duration_ms || 0) / 1000),
        seg.size || 0,
      ].map((v) => `"${String(v == null ? '' : v).replace(/"/g, '""')}"`).join(','));
    });
    const blob = new Blob(['\ufeff' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = `camera_segments_${state.date}.csv`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
    toast('分片清单已导出', 'success');
  }

  function stopPlayback(resetPlayhead = true) {
    if (els.video) {
      els.video.pause();
      try { els.video.removeAttribute('src'); els.video.load(); } catch (e) { /* ignore */ }
    }
    state.current = null;
    setPlayerLoading(false);
    if (resetPlayhead) state.playheadMs = dateStartMs();
    updatePlayerState();
    updatePlayButton();
    updateProgressUI();
    updatePlayheadPosition();
    markSelectedBars();
    updateSelectedInfo();
  }

  // ------------------------------------------------------------------
  // 事件绑定
  // ------------------------------------------------------------------
  // ------------------------------------------------------------------
  // 实时预览 / 录像回放 模式切换
  // 实时预览不再单独占一张卡片，而是复用回放控制台的播放区：同一块区域，
  // 由头部开关切换「实时画面（MJPEG）」与「录像分片回放」，减少页面冗余。
  // 循环录像在后台始终运行，预览只是旁路观看，因此「停止预览」只断开画面。
  // ------------------------------------------------------------------
  const liveState = { mode: 'live', timer: null, retryTimer: null };

  function isLiveMode() {
    return liveState.mode === 'live';
  }

  function syncModeButtons() {
    const root = els.consoleRoot || $('#cam-console');
    const live = isLiveMode();
    root?.classList.toggle('cam-mode-live', live);
    els.modeLiveBtn?.classList.toggle('active', live);
    els.modePlayBtn?.classList.toggle('active', !live);
  }

  function setMode(mode, options = {}) {
    liveState.mode = mode === 'play' ? 'play' : 'live';
    syncModeButtons();
    if (isLiveMode()) {
      if (!options.noAttach) startLivePreview();
      pollBusy();
    } else {
      detachLivePreview();
      updatePlayerState();
    }
  }

  async function startLivePreview() {
    const img = els.liveImg;
    if (!img) return;
    if (!img.dataset.liveSrc) {
      try {
        await api('/api/camera/start', { method: 'POST', body: '{}' });
      } catch (e) {
        // 采集进程多半已在运行（后台循环录像），继续尝试取流
      }
    }
    if (!isLiveMode()) return;
    img.dataset.liveSrc = '1';
    img.src = '/api/camera/stream?t=' + Date.now();
    els.liveBadge?.classList.add('on');
    if (els.liveText) els.liveText.textContent = '实时预览中（MJPEG）';
    pollBusy();
    if (!liveState.timer) {
      // 不重叠轮询（见 static/js/poll.js）：原先 setInterval 不等返回，
      // 板端变慢时会与其它轮询一起堆积
      liveState.timer = ELF2Poll.loop(() => {
        if (isCameraTabActive() && isLiveMode()) return pollBusy();
      }, 2000);
    }
  }

  function detachLivePreview() {
    const img = els.liveImg;
    if (img) {
      delete img.dataset.liveSrc;
      img.src = '';
    }
    els.liveBadge?.classList.remove('on');
    if (els.liveText) els.liveText.textContent = '实时预览未启动';
  }

  // 摄像头 Tab 可见 + 实时预览模式 + 还没取流 -> 自动挂上预览
  function maybeAutoPreview() {
    if (!isCameraTabActive() || !isLiveMode()) return;
    if (els.liveImg && !els.liveImg.dataset.liveSrc) startLivePreview();
    pollBusy();
  }

  async function pollBusy() {
    if (!els.liveBusy) return;
    try {
      const data = await api('/api/busy/status');
      renderLiveBusy(data.busy || {});
    } catch (e) { /* 静默 */ }
  }

  function renderLiveBusy(busy) {
    const el = els.liveBusy;
    if (!el) return;
    const raw = busy.sysfs_value;
    if (raw === '' || raw == null || String(raw).startsWith('ERR')) {
      el.textContent = 'BUSY 未接入';
      el.classList.remove('on');
      el.title = `GPIO3_A5（全局 GPIO ${busy.gpio ?? 101}）不可读：${busy.error || '未导出'}`;
      return;
    }
    el.classList.toggle('on', !!busy.active);
    el.textContent = busy.active
      ? `BUSY 接收中${busy.on_for ? ' ' + busy.on_for + 's' : ''}`
      : 'BUSY 空闲';
    el.title = `GPIO3_A5（全局 GPIO ${busy.gpio ?? 101}）· 原始电平 ${raw} · 累计触发 ${busy.count || 0} 次` +
      (busy.tx_conflict ? ' · 发射期间仍 BUSY，注意自激' : '');
  }

  function bindEvents() {
    $('#btn-cam-open-console-top')?.addEventListener('click', openConsole);
    $('#btn-cam-open-console-2')?.addEventListener('click', openConsole);
    $('#cam-playback-entry')?.addEventListener('click', (evt) => {
      if (evt.target?.closest?.('button')) return;
      openConsole();
    });
    $('#btn-cam-refresh')?.addEventListener('click', () => loadAll());

    // 实时预览 / 录像回放 模式切换
    els.modeLiveBtn?.addEventListener('click', () => setMode('live'));
    els.modePlayBtn?.addEventListener('click', () => setMode('play'));
    $('#cam-btn-live-fullscreen')?.addEventListener('click', () => {
      const wrap = els.playerWrap || $('#cam-player-wrap');
      if (!wrap) return;
      if (document.fullscreenElement) document.exitFullscreen?.();
      else wrap.requestFullscreen?.();
    });
    els.liveImg?.addEventListener('error', () => {
      if (!isLiveMode()) return;
      if (els.liveText) els.liveText.textContent = '预览中断，3 秒后重连…';
      clearTimeout(liveState.retryTimer);
      liveState.retryTimer = setTimeout(() => {
        if (isLiveMode() && els.liveImg?.dataset.liveSrc) {
          els.liveImg.src = '/api/camera/stream?t=' + Date.now();
        }
      }, 3000);
    });
    // app.js 的「启动预览 / 停止预览」按钮通过事件通知本模块同步 UI
    window.addEventListener('elf2:camera-preview-start', () => {
      liveState.mode = 'live';
      syncModeButtons();
      if (els.liveImg) els.liveImg.dataset.liveSrc = '1';
      els.liveBadge?.classList.add('on');
      if (els.liveText) els.liveText.textContent = '实时预览中（MJPEG）';
      pollBusy();
    });
    window.addEventListener('elf2:camera-preview-stop', () => {
      if (els.liveImg) delete els.liveImg.dataset.liveSrc;
      els.liveBadge?.classList.remove('on');
      if (els.liveText) els.liveText.textContent = '实时预览未启动';
    });
    $('#btn-camera-recordings-refresh')?.addEventListener('click', () => loadAll());
    $('#btn-camera-cleanup')?.addEventListener('click', async () => {
      if (!confirm('将按循环容量和存储上限清理最旧录像，是否继续？')) return;
      try {
        const data = await api('/api/camera/recordings/cleanup', { method: 'POST', body: '{}' });
        const n = data.cleanup?.deleted_count || 0;
        toast(n ? `已按策略清理 ${n} 个文件` : '当前无需清理', 'success');
        if (state.current && data.cleanup?.deleted?.some((x) => x.filename === state.current.filename)) {
          stopPlayback();
        }
        await loadAll({ silent: true, keepPage: true });
      } catch (e) {
        toast(e.message, 'error');
      }
    });

    // 日期 / 过滤器
    els.datePicker?.addEventListener('change', () => {
      if (!els.datePicker.value) return;
      state.date = els.datePicker.value;
      state.page = 1;
      stopPlayback();
      loadAll({ silent: true });
    });
    $('#cam-date-prev')?.addEventListener('click', () => {
      state.date = shiftDate(state.date, -1);
      els.datePicker.value = state.date;
      state.page = 1;
      stopPlayback();
      loadAll({ silent: true });
    });
    $('#cam-date-next')?.addEventListener('click', () => {
      state.date = shiftDate(state.date, 1);
      els.datePicker.value = state.date;
      state.page = 1;
      stopPlayback();
      loadAll({ silent: true });
    });
    $('#cam-date-today')?.addEventListener('click', () => {
      state.date = localDate(new Date());
      els.datePicker.value = state.date;
      state.page = 1;
      stopPlayback();
      loadAll({ silent: true });
    });

    els.type?.addEventListener('change', () => {
      state.type = els.type.value;
      state.page = 1;
      refreshPlaylist();
      renderTimeline();
      renderSegments();
      updateConsoleBadges();
      if (state.current && state.type !== 'all' && state.current.type !== state.type) {
        stopPlayback();
      }
    });
    els.zoom?.addEventListener('change', () => {
      state.timelineZoom = Number(els.zoom.value) || 1;
      renderTimeline();
    });
    els.follow?.addEventListener('change', () => {
      state.follow = !!els.follow.checked;
      if (state.follow) scrollPlayheadIntoView();
    });

    // 时间轴拖拽
    els.timelineInner?.addEventListener('pointerdown', onTimelinePointerDown);
    els.timelineInner?.addEventListener('pointermove', onTimelinePointerMove);
    els.timelineInner?.addEventListener('pointerup', onTimelinePointerUp);
    els.timelineInner?.addEventListener('pointercancel', () => { state.dragging = false; });
    els.timelineInner?.addEventListener('pointerleave', (evt) => {
      if (!state.dragging) hideTooltip();
      else onTimelinePointerMove(evt);
    });

    // 播放器控制
    els.btnPlay?.addEventListener('click', togglePlay);
    els.btnPrev?.addEventListener('click', playPrev);
    els.btnNext?.addEventListener('click', playNext);
    $('#cam-btn-stop')?.addEventListener('click', () => stopPlayback());
    $('#cam-btn-snapshot')?.addEventListener('click', snapshotCurrentFrame);
    $('#cam-btn-download')?.addEventListener('click', downloadCurrent);
    $('#cam-btn-fullscreen')?.addEventListener('click', toggleFullscreen);
    els.playSpeed?.addEventListener('change', () => {
      if (els.video) els.video.playbackRate = Number(els.playSpeed.value) || 1;
    });

    // 当前分片进度条
    let progressDragging = false;
    const onProgressDown = (evt) => {
      if (!els.video?.duration) return;
      progressDragging = true;
      els.progress.setPointerCapture?.(evt.pointerId);
      setProgressFromEvent(evt);
    };
    const onProgressMove = (evt) => {
      if (progressDragging) setProgressFromEvent(evt);
    };
    const onProgressUp = (evt) => {
      if (!progressDragging) return;
      progressDragging = false;
      els.progress.releasePointerCapture?.(evt.pointerId);
      setProgressFromEvent(evt);
    };
    els.progress?.addEventListener('pointerdown', onProgressDown);
    els.progress?.addEventListener('pointermove', onProgressMove);
    els.progress?.addEventListener('pointerup', onProgressUp);

    // 表格事件
    els.segmentsBody?.addEventListener('click', async (evt) => {
      const target = evt.target;
      const playFile = target?.dataset?.camPlayFile;
      const delFile = target?.dataset?.camDelFile;
      const downloadFile = target?.dataset?.camDownloadFile;
      if (playFile) {
        const seg = segByFilename(playFile);
        if (seg) loadSegment(seg, { autoplay: true, offsetMs: 0 });
      }
      if (downloadFile) {
        const seg = segByFilename(downloadFile);
        if (seg) {
          const link = document.createElement('a');
          link.href = `${seg.url}?download=1`;
          link.download = seg.filename;
          link.click();
        }
      }
      if (delFile) await deleteFilenames([delFile]);
    });
    els.segmentsBody?.addEventListener('change', (evt) => {
      const name = evt.target?.dataset?.camSelect;
      if (name) toggleSelected(name, !!evt.target.checked);
    });
    els.selectAll?.addEventListener('change', () => {
      const rows = state.filtered.slice((state.page - 1) * state.pageSize, state.page * state.pageSize);
      rows.forEach((seg) => {
        if (els.selectAll.checked) state.selected.add(seg.filename);
        else state.selected.delete(seg.filename);
      });
      renderSegments();
    });

    $('#btn-cam-batch-delete')?.addEventListener('click', () => deleteFilenames(Array.from(state.selected)));
    $('#btn-cam-batch-download')?.addEventListener('click', batchDownload);
    $('#btn-cam-export-csv')?.addEventListener('click', exportCsv);

    let searchTimer = null;
    els.search?.addEventListener('input', () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => {
        state.q = els.search.value || '';
        state.page = 1;
        renderSegments();
      }, 180);
    });
    els.sort?.addEventListener('change', () => {
      state.sort = els.sort.value;
      state.page = 1;
      renderSegments();
    });
    els.pageSize?.addEventListener('change', () => {
      state.pageSize = Number(els.pageSize.value) || 25;
      state.page = 1;
      renderSegments();
    });
    els.pagePrev?.addEventListener('click', () => {
      state.page = Math.max(1, state.page - 1);
      renderSegments();
    });
    els.pageNext?.addEventListener('click', () => {
      state.page += 1;
      renderSegments();
    });

    // 视频事件
    const video = els.video;
    video?.addEventListener('play', () => { updatePlayButton(); updatePlayerState(); });
    video?.addEventListener('pause', () => { updatePlayButton(); updatePlayerState(); });
    video?.addEventListener('waiting', () => setPlayerLoading(true));
    video?.addEventListener('playing', () => setPlayerLoading(false));
    video?.addEventListener('canplay', () => setPlayerLoading(false));
    video?.addEventListener('timeupdate', () => {
      if (!state.current) return;
      const videoTimeMs = (Number(video.currentTime) || 0) * 1000;
      if (!state.dragging) {
        updatePlayhead(state.current.start_ms + videoTimeMs, { skipInfo: true });
      }
      updateProgressUI();
      updateSelectedInfo();
    });
    video?.addEventListener('progress', updateProgressUI);
    video?.addEventListener('durationchange', updateProgressUI);
    video?.addEventListener('ended', () => {
      updatePlayerState();
      updateProgressUI();
      if (els.autoNext?.checked && state.current) {
        const index = currentPlaylistIndex();
        const next = state.playlist[index + 1];
        if (next) {
          loadSegment(next, { autoplay: true, offsetMs: 0 });
        } else {
          updatePlayhead(state.current.end_ms || state.playheadMs);
        }
      }
    });
    video?.addEventListener('error', () => {
      setPlayerLoading(false);
      updatePlayerState();
      toast('该分片当前不可播放，可能是文件仍在写入或浏览器不支持该编码', 'error');
    });

    window.addEventListener('elf2:camera-settings-saved', () => loadAll({ silent: true, keepPage: true }));

    // 轮询：仅在摄像头 Tab 可见时刷新存储；每 30 秒安静刷新分片
    // 一律走 ELF2Poll.loop：上一次返回之后才排下一次，不会重叠堆积（见 static/js/poll.js）
    ELF2Poll.loop(() => {
      if (isCameraTabActive()) return loadStorageOnly();
    }, 8000);
    ELF2Poll.loop(maybeAutoPreview, 3000);
    ELF2Poll.loop(() => {
      if (isCameraTabActive() && !state.dragging && !state.batchBusy) {
        return loadAll({ silent: true, keepPage: true });
      }
    }, 30000);
  }

  // ------------------------------------------------------------------
  // 初始化
  // ------------------------------------------------------------------
  function init() {
    if (state.initialized) return;
    state.initialized = true;
    cacheEls();
    state.date = els.datePicker?.value || localDate(new Date());
    if (els.datePicker) els.datePicker.value = state.date;
    state.playheadMs = dateStartMs() + 8 * 3600 * 1000;
    bindEvents();
    setMode('live', { noAttach: true });
    renderTimeline();
    maybeAutoPreview();
    if (isCameraTabActive()) loadAll({ silent: true });
  }

  function activate() {
    if (!state.initialized) init();
    loadAll({ silent: true });
    maybeAutoPreview();
  }

  document.addEventListener('DOMContentLoaded', () => {
    init();
    const cameraTabBtn = document.querySelector('.tab-btn[data-tab="camera"]');
    cameraTabBtn?.addEventListener('click', activate);
    // 用户可能通过 URL hash/浏览器恢复直接进入摄像头页
    if (isCameraTabActive()) loadAll({ silent: true });
  });
})();

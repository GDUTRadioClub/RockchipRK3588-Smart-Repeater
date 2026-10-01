/* ELF2 智能中继控制中心前端逻辑 */
(function () {
  'use strict';

  const role = window.APP_ROLE || 'user';
  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || '';
  let ttsState = { current: 'local', voices: [] };   // 外部 TTS 已下线，仅本地 Piper

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  // 每个浏览器标签页一个 client id：板端用它区分「哪个页面在按住 PTT」，
  // 别的标签页的操作不会互相掐断。（原来这个常量定义在对话页的流式朗读块里，
  // 对话搬去助手页时被一起删掉，于是 PTT 自检抛 `TTS_CLIENT_ID is not defined`
  // —— 2026-09-27 修回，并改成不绑对话的通用名字。）
  const CLIENT_ID = (() => {
    try {
      let v = sessionStorage.getItem('elf2-client');
      if (!v) {
        v = (window.crypto && crypto.randomUUID)
          ? crypto.randomUUID()
          : 'c' + Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
        sessionStorage.setItem('elf2-client', v);
      }
      return v;
    } catch (e) {
      return '';
    }
  })();

  function showToast(msg, type = '') {
    const el = $('#toast');
    if (!el) return;
    el.textContent = msg;
    el.className = 'toast show ' + type;
    clearTimeout(el._timer);
    el._timer = setTimeout(() => { el.className = 'toast ' + type; }, 3200);
  }

  async function apiFetch(url, options = {}) {
    const opts = Object.assign({}, options);
    opts.headers = Object.assign({}, opts.headers || {});
    if (!(opts.body instanceof FormData)) {
      opts.headers['Content-Type'] = 'application/json';
    }
    if (opts.method && opts.method !== 'GET') {
      opts.headers['X-CSRF-Token'] = csrfToken;
    }
    const resp = await fetch(url, opts);
    if (resp.status === 401) {
      location.href = '/login';
      throw new Error('未登录');
    }
    const ct = resp.headers.get('content-type') || '';
    let data;
    if (ct.includes('application/json')) {
      data = await resp.json();
    } else {
      data = { ok: resp.ok, text: await resp.text() };
    }
    if (!resp.ok || (data && data.ok === false)) {
      throw new Error((data && data.error) || `HTTP ${resp.status}`);
    }
    return data;
  }

  function fmtUptime(sec) {
    sec = Math.floor(sec || 0);
    const d = Math.floor(sec / 86400);
    const h = Math.floor((sec % 86400) / 3600);
    const m = Math.floor((sec % 3600) / 60);
    if (d) return `${d}天 ${h}小时`;
    if (h) return `${h}小时 ${m}分`;
    return `${m}分 ${sec % 60}秒`;
  }

  function startBeijingClock() {
    const el = $('#clock-beijing');
    if (!el) return;
    const fmt = new Intl.DateTimeFormat('zh-CN', {
      timeZone: 'Asia/Shanghai',
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
      hour12: false,
    });
    // 日期与时间拆成两个 span：手机端 CSS 只隐藏 .clock-date。
    // 桌面渲染结果与原来逐字符一致（date + 空格 + time）。
    const dateEl = document.createElement('span');
    dateEl.className = 'clock-date';
    const timeEl = document.createElement('span');
    timeEl.className = 'clock-time';
    el.textContent = '';
    el.append(dateEl, document.createTextNode(' '), timeEl);
    const tick = () => {
      const parts = fmt.formatToParts(new Date());
      const get = (t) => parts.find(p => p.type === t)?.value || '';
      dateEl.textContent = `${get('year')}-${get('month')}-${get('day')}`;
      timeEl.textContent = `${get('hour')}:${get('minute')}:${get('second')}`;
    };
    tick();
    setInterval(tick, 1000);
  }

  function setBar(id, percent) {
    const el = $(id);
    if (el) el.style.width = Math.max(0, Math.min(100, percent)) + '%';
  }

  // ---------------- tabs ----------------
  function initTabs() {
    $$('.tab-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        $$('.tab-btn').forEach(b => b.classList.remove('active'));
        $$('.tab-panel').forEach(p => p.classList.remove('active'));
        btn.classList.add('active');
        const p = $('#tab-' + btn.dataset.tab);
        if (p) p.classList.add('active');
        updateMoreState();
      });
    });
    if (role !== 'admin') {
      $$('.admin-only').forEach(el => { el.style.display = 'none'; });
    }
    updateMoreState();
  }

  // ---------------- 手机底部导航（≤720px 生效，桌面无副作用） ----------------
  // 选中的 tab 不在底栏里（气象/用户管理/设置）时点亮「更多」，
  // 让用户知道当前功能收在上拉面板里。
  function updateMoreState() {
    const more = $('.nav-more');
    if (!more) return;
    const barHasActive = $$('.nav-bar .tab-btn').some(b => b.classList.contains('active'));
    more.classList.toggle('active', !barHasActive);
  }

  function initNavSheet() {
    const more = $('.nav-more');
    const sheet = $('#nav-sheet');
    if (!more || !sheet) return;
    const setOpen = (open) => {
      sheet.classList.toggle('open', open);
      more.classList.toggle('open', open);
      more.setAttribute('aria-expanded', open ? 'true' : 'false');
    };
    more.addEventListener('click', (e) => {
      e.stopPropagation();
      setOpen(!sheet.classList.contains('open'));
    });
    // 面板里点了任意一项（切 tab 或跳页）就收起
    $$('.tab-btn', sheet).forEach(b => b.addEventListener('click', () => setOpen(false)));
    // 点面板外、或按 Esc 收起
    document.addEventListener('click', (e) => {
      if (sheet.classList.contains('open') && !sheet.contains(e.target)) setOpen(false);
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') setOpen(false);
    });
  }

  // ---------------- 总览页子选项卡（运行概览 / 能量统计） ----------------
  function initOverviewSubtabs() {
    const box = $('#ov-subtabs');
    if (!box) return;
    $$('.sub-tab-btn', box).forEach(btn => {
      btn.addEventListener('click', () => {
        $$('.sub-tab-btn', box).forEach(b => b.classList.toggle('active', b === btn));
        $$('#tab-overview .sub-panel').forEach(
          p => p.classList.toggle('active', p.id === btn.dataset.subtab));
        if (btn.dataset.subtab === 'ov-energy') {
          // 首次切进来才拉数据：别让它在总览轮询里白拉一整天
          if (!energyState.loaded) loadEnergy();
          else drawEnergyChart();
        }
      });
    });
  }

  // ---------------- overview ----------------
  async function loadStatus() {
    try {
      const data = await apiFetch('/api/status');
      const v = data.voltages || {};
      if (v.battery) {
        $('#battery-voltage').textContent = (v.battery.voltage ?? '--') + ' V';
        $('#battery-raw').textContent = `raw ${v.battery.raw ?? '--'} · 引脚 ${v.battery.pin_voltage ?? '--'} V · 倍率 ${v.battery.multiplier ?? '--'} V/V`;
        setBar('#battery-bar', Math.min(100, (v.battery.voltage || 0) / 15 * 100));
      }
      if (v.pv) {
        $('#pv-voltage').textContent = (v.pv.voltage ?? '--') + ' V';
        $('#pv-raw').textContent = `raw ${v.pv.raw ?? '--'} · 引脚 ${v.pv.pin_voltage ?? '--'} V · 倍率 ${v.pv.multiplier ?? '--'} V/V`;
        setBar('#pv-bar', Math.min(100, (v.pv.voltage || 0) / 30 * 100));
      }
      $('#cpu-percent').textContent = (data.cpu_percent ?? '--') + ' %';
      $('#loadavg').textContent = `Load ${(+data.loadavg['1m']).toFixed(2)} / ${(+data.loadavg['5m']).toFixed(2)} / ${(+data.loadavg['15m']).toFixed(2)}`;
      setBar('#cpu-bar', data.cpu_percent || 0);
      const mem = data.memory || {};
      $('#mem-percent').textContent = (mem.percent ?? '--') + ' %';
      $('#mem-detail').textContent = mem.total ? `${(mem.used / 1073741824).toFixed(1)} / ${(mem.total / 1073741824).toFixed(1)} GiB` : '--';
      setBar('#mem-bar', mem.percent || 0);
      const tEl = $('#temperatures');
      if (tEl) {
        if (data.temperatures && data.temperatures.length) {
          tEl.innerHTML = data.temperatures.map(t => `<div><span>${escapeHtml(t.name)}</span><b>${t.celsius} °C</b></div>`).join('');
        } else {
          tEl.innerHTML = '<span class="muted">无温度传感器</span>';
        }
      }
      $('#uptime').textContent = fmtUptime(data.uptime_seconds);
      renderDisks(data.disks);
      $('#last-update').textContent = '更新于 ' + (data.time || '');
    } catch (e) {
      console.warn('status error', e);
    }
  }

  // 开发板存储卡片（eMMC / NVMe）：与其它卡片同一套 card/metric/sub/bar 结构
  function renderDisks(disks) {
    const box = $('#disk-cards');
    if (!box) return;
    const list = disks || [];
    if (!list.length) {
      box.innerHTML = '<div class="card"><div class="card-title">开发板存储</div>'
        + '<div class="metric">-- %</div>'
        + '<div class="sub">未读到磁盘信息（挂载点 / /userdata /opt/ai 都不存在？）</div>'
        + '<div class="bar"><span style="width:0%"></span></div></div>';
      return;
    }
    const gib = n => (Number(n || 0) / 1073741824).toFixed(1);
    box.innerHTML = list.map(d => {
      const pct = Math.max(0, Math.min(100, Number(d.percent) || 0));
      const warn = pct >= 90 ? ' class="warn"' : '';
      return '<div class="card">'
        + '<div class="card-title">' + escapeHtml(d.kind || '存储')
        + ' · ' + escapeHtml(d.mount || '') + '</div>'
        + '<div class="metric">' + (d.percent ?? '--') + ' %</div>'
        + '<div class="sub" title="' + escapeHtml(d.device || '') + '">已用 '
        + gib(d.used) + ' / ' + gib(d.total) + ' GiB · 可用 ' + gib(d.free) + ' GiB</div>'
        + '<div class="bar"><span' + warn + ' style="width:' + pct + '%"></span></div>'
        + '</div>';
    }).join('');
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }

  // ---------------- voltage calibration ----------------
  let calCache = {};

  function renderCalLive(v) {
    calCache = v || {};
    const box = $('#cal-live');
    if (!box) return;
    const item = (key) => {
      const c = calCache[key];
      if (!c) return '';
      return `${escapeHtml(c.label || key)} <b>${c.voltage ?? '--'} V</b>`;
    };
    const parts = [item('battery'), item('pv')].filter(Boolean);
    box.innerHTML = parts.length ? ('实时值：' + parts.join('　·　')) : '实时值：--';
  }

  async function loadCalibration() {
    try {
      const data = await apiFetch('/api/status');
      const v = data.voltages || {};
      if (v.battery) {
        if ($('#battery-ch')) $('#battery-ch').value = v.battery.adc_channel;
        $('#battery-zero').value = v.battery.zero_raw;
        $('#battery-mult').value = v.battery.multiplier;
      }
      if (v.pv) {
        if ($('#pv-ch')) $('#pv-ch').value = v.pv.adc_channel;
        $('#pv-zero').value = v.pv.zero_raw;
        $('#pv-mult').value = v.pv.multiplier;
      }
      renderCalLive(v);
    } catch (e) { showToast(e.message, 'error'); }
  }

  // 填入设计值：倍率 = 1/分压比（电池 10.11、光伏 17.01）
  function fillDesignCal() {
    let n = 0;
    ['battery', 'pv'].forEach((key) => {
      const c = calCache[key];
      if (!c || c.design_multiplier == null) return;
      const el = $(key === 'battery' ? '#battery-mult' : '#pv-mult');
      if (el) { el.value = c.design_multiplier; n += 1; }
    });
    showToast(n ? `已填入设计倍率（${n} 个通道），保存后生效` : '设计值不可用', n ? 'success' : 'error');
  }

  async function saveCalibration() {
    try {
      await apiFetch('/api/voltage/calibrate', {
        method: 'POST',
        body: JSON.stringify({
          battery_adc_channel: parseInt($('#battery-ch')?.value ?? '4', 10),
          battery_zero_raw: parseFloat($('#battery-zero').value),
          battery_multiplier: parseFloat($('#battery-mult').value),
          pv_adc_channel: parseInt($('#pv-ch')?.value ?? '6', 10),
          pv_zero_raw: parseFloat($('#pv-zero').value),
          pv_multiplier: parseFloat($('#pv-mult').value),
        }),
      });
      showToast('电压校准已保存', 'success');
      loadStatus();
      loadCalibration();
    } catch (e) { showToast(e.message, 'error'); }
  }

  // 能量统计采样设置（采样间隔 / 保留天数 / 是否落库）
  async function loadEnergySettings() {
    try {
      const d = await apiFetch('/api/settings');
      const s = d.settings || d || {};
      if ($('#energy-sample-sec')) {
        $('#energy-sample-sec').value = s.energy_sample_sec ?? 60;
      }
      if ($('#energy-retention-days')) {
        $('#energy-retention-days').value = s.energy_retention_days ?? 365;
      }
      if ($('#energy-log-enabled')) {
        $('#energy-log-enabled').value =
          String(s.energy_log_enabled ?? '1') === '0' ? '0' : '1';
      }
    } catch (e) { /* 设置页读不到不该挡住整个总览 */ }
  }

  async function saveEnergySettings() {
    try {
      await apiFetch('/api/settings', {
        method: 'POST',
        body: JSON.stringify({
          energy_sample_sec: parseInt($('#energy-sample-sec')?.value ?? '60', 10),
          energy_retention_days: parseInt($('#energy-retention-days')?.value ?? '365', 10),
          energy_log_enabled: $('#energy-log-enabled')?.value ?? '1',
        }),
      });
      showToast('能量统计设置已保存（采样线程下一轮生效）', 'success');
      loadEnergySettings();
      if (energyState.loaded) loadEnergy(false);
    } catch (e) { showToast(e.message, 'error'); }
  }

  // ---------------- users ----------------
  async function loadUsers() {
    if (role !== 'admin') return;
    try {
      const data = await apiFetch('/api/users');
      const tbody = $('#users-table tbody');
      if (!data.users.length) {
        tbody.innerHTML = '<tr><td colspan="5" class="muted">暂无用户</td></tr>';
        return;
      }
      tbody.innerHTML = data.users.map(u => `
        <tr>
          <td>${u.id}</td>
          <td>${escapeHtml(u.username)}</td>
          <td>${u.role === 'admin' ? '管理员' : '用户'}</td>
          <td>${escapeHtml(u.last_login || '--')}</td>
          <td>
            <button class="btn ghost" data-reset="${u.id}" data-name="${escapeHtml(u.username)}">重置密码</button>
            ${u.username.toLowerCase() === 'admin' ? '' : `<button class="btn ghost" data-del="${u.id}" data-name="${escapeHtml(u.username)}">删除</button>`}
          </td>
        </tr>`).join('');
    } catch (e) { showToast(e.message, 'error'); }
  }

  async function addUser() {
    try {
      await apiFetch('/api/users', {
        method: 'POST',
        body: JSON.stringify({
          username: $('#new-user-name').value,
          password: $('#new-user-pass').value,
          role: $('#new-user-role').value,
        }),
      });
      $('#new-user-name').value = '';
      $('#new-user-pass').value = '';
      showToast('用户已添加', 'success');
      loadUsers();
    } catch (e) { showToast(e.message, 'error'); }
  }

  // ---------------- LLM 提供方（auto / local / external） ----------------
  // 解析结果由后端算（网络探测在后端，前端只显示），reason 的人话也由后端给，
  // 避免前后端各翻一套。
  function renderLlmRoute(r) {
    const box = $('#llm-route-status');
    if (!box || !r) return;
    const eff = r.effective === 'external' ? '外部 API' : '本地模型';
    const net = r.net || {};
    const items = [
      ['当前生效', eff + (r.model ? '（' + r.model + '）' : '')],
      ['模式', r.mode_label || r.mode || '--'],
      ['依据', (r.reason_label || r.reason || '--')],
    ];
    if (r.mode === 'auto') {
      items.push(['网络探测',
        (net.ok ? '可达' : '不可达') + '（' + (net.ms != null ? net.ms + 'ms' : '--') +
        (net.cached ? ' · 缓存 ' + net.age + 's' : ' · 刚探测') + '）']);
    } else {
      items.push(['网络探测', '仅自动模式需要探测']);
    }
    if (r.base_url) items.push(['地址', r.base_url]);
    box.innerHTML = items.map(function (x) {
      return '<span class="muted small">' + x[0] + '：</span> <b>' + x[1] + '</b>';
    }).join('<br>');
  }

  async function loadLlmRoute() {
    // 读状态接口（走后端 60 秒缓存，不在每次加载时都去连外网）
    try {
      const s = await apiFetch('/api/status');
      if (s && s.llm) { renderLlmRoute(s.llm); return s.llm; }
    } catch (e) { /* 忽略 */ }
    return null;
  }

  async function probeLlmRoute() {
    // 按钮用：强制重新探测（网络刚恢复时不必等缓存过期）
    const state = $('#llm-probe-state');
    if (state) state.textContent = '探测中…';
    try {
      const d = await apiFetch('/api/llm/probe', { method: 'POST', body: '{}' });
      renderLlmRoute(d);
      if (state) {
        state.textContent = '已探测：' + (d.reason_label || d.reason || '') +
          '（' + (d.net && d.net.ms != null ? d.net.ms + 'ms' : '--') + '）';
      }
      showToast('当前生效：' + (d.effective_label || d.effective), 'success');
      return d;
    } catch (e) {
      if (state) state.textContent = '探测失败：' + e.message;
      showToast('探测失败：' + e.message, 'error');
      return null;
    }
  }

  // ---------------- settings ----------------
  async function loadSettings() {
    if (role !== 'admin') return;
    try {
      const data = await apiFetch('/api/settings');
      const s = data.settings;
      $('#set-llm-provider').value = s.llm_provider || 'auto';
      $('#set-local-base').value = s.local_base_url || '';
      $('#set-local-model').value = s.local_model || '';
      $('#set-ext-base').value = s.external_base_url || '';
      $('#set-ext-model').value = s.external_model || '';
      $('#set-auto-play').checked = s.record_auto_play === '1';
      if (s.local_api_key_set) $('#set-local-key').placeholder = '已设置，留空保持不变';
      if (s.external_api_key_set) $('#set-ext-key').placeholder = '已设置，留空保持不变';
      if ($('#set-tts-voice')) $('#set-tts-voice').value = s.tts_local_voice || '';
      if ($('#set-tts-en-voice')) $('#set-tts-en-voice').value = s.tts_en_voice || '';
      if ($('#set-tts-icao')) $('#set-tts-icao').checked = s.tts_icao === '1';
      if ($('#set-tts-icao-voice')) $('#set-tts-icao-voice').dataset.saved = s.tts_icao_voice || '';
      if ($('#set-tts-auto-speak')) $('#set-tts-auto-speak').checked = s.tts_auto_speak === '1';
      applySpeakPolicy(s.tts_auto_speak === '1');
      // 定时重启
      if ($('#set-reboot-enabled')) $('#set-reboot-enabled').checked = s.reboot_enabled === '1';
      if ($('#set-reboot-times')) $('#set-reboot-times').value = s.reboot_times || '';
      if ($('#set-reboot-notice')) $('#set-reboot-notice').value = s.reboot_notice_sec || '30';
      if ($('#set-reboot-text')) $('#set-reboot-text').value = s.reboot_text || '';
      clearDirty('#llm-save-state'); clearDirty('#tts-save-state'); clearDirty('#reboot-save-state');
      loadLlmRoute();
    } catch (e) { showToast(e.message, 'error'); }
  }

  // 每张卡片各自保存：LLM/页面 与 语音 分开，避免「改了语音卡片却要按 LLM 卡片的保存」
  async function saveLlmSettings() {
    const body = {
      llm_provider: $('#set-llm-provider').value,
      local_base_url: $('#set-local-base').value,
      local_model: $('#set-local-model').value,
      external_base_url: $('#set-ext-base').value,
      external_model: $('#set-ext-model').value,
      record_auto_play: $('#set-auto-play').checked ? '1' : '0',
    };
    if ($('#set-local-key').value) body.local_api_key = $('#set-local-key').value;
    if ($('#set-ext-key').value) body.external_api_key = $('#set-ext-key').value;
    try {
      await apiFetch('/api/settings', { method: 'POST', body: JSON.stringify(body) });
      showToast('LLM / 页面设置已保存', 'success');
      clearDirty('#llm-save-state');
      loadSettings();
      loadLlmRoute();
    } catch (e) { showToast(e.message, 'error'); }
  }

  async function saveTtsSettings() {
    const body = {
      tts_local_voice: $('#set-tts-voice')?.value || '',
      tts_en_voice: $('#set-tts-en-voice')?.value || '',
      tts_icao: $('#set-tts-icao')?.checked ? '1' : '0',
      tts_icao_voice: $('#set-tts-icao-voice')?.value || '',
      tts_auto_speak: $('#set-tts-auto-speak')?.checked ? '1' : '0',
    };
    try {
      await apiFetch('/api/settings', { method: 'POST', body: JSON.stringify(body) });
      showToast('语音设置已保存（全局生效）', 'success');
      clearDirty('#tts-save-state');
      loadTtsProviders();
    } catch (e) { showToast(e.message, 'error'); }
  }

  // ---- 设置页分类收纳：记住展开/收起状态，并提供整页展开/收起 ----
  function initAccordions() {
    const boxes = $$('details.acc');
    if (!boxes.length) return;
    const KEY = 'elf2-set-acc';
    let saved = {};
    try { saved = JSON.parse(localStorage.getItem(KEY) || '{}') || {}; } catch (e) { saved = {}; }
    const persist = () => {
      try { localStorage.setItem(KEY, JSON.stringify(saved)); } catch (e) { /* 忽略隐私模式 */ }
    };
    boxes.forEach((d) => {
      const k = d.dataset.acc || '';
      if (k && typeof saved[k] === 'boolean') d.open = saved[k];
      d.addEventListener('toggle', () => {
        if (!k) return;
        saved[k] = d.open;
        persist();
      });
    });
    $('#btn-acc-expand')?.addEventListener('click', () => {
      boxes.forEach((d) => { d.open = true; if (d.dataset.acc) saved[d.dataset.acc] = true; });
      persist();
    });
    $('#btn-acc-collapse')?.addEventListener('click', () => {
      boxes.forEach((d) => { d.open = false; if (d.dataset.acc) saved[d.dataset.acc] = false; });
      persist();
    });
  }

  // ---- 每卡片「有未保存的修改」提示 ----
  function bindDirty(cardSel, stateSel) {
    const card = $(cardSel);
    if (!card) return;
    const mark = () => {
      const el = $(stateSel);
      if (el) { el.textContent = '● 有未保存的修改'; el.classList.add('dirty'); }
    };
    card.addEventListener('input', mark);
    card.addEventListener('change', mark);
  }

  function clearDirty(stateSel, text) {
    const el = $(stateSel);
    if (el) { el.textContent = text || '已保存'; el.classList.remove('dirty'); }
  }

  // ---- 定时重启计划 ----
  async function saveRebootSettings() {
    const body = {
      reboot_enabled: $('#set-reboot-enabled')?.checked ? '1' : '0',
      reboot_times: $('#set-reboot-times')?.value || '',
      reboot_notice_sec: $('#set-reboot-notice')?.value || '30',
      reboot_text: $('#set-reboot-text')?.value || '',
    };
    try {
      await apiFetch('/api/settings', { method: 'POST', body: JSON.stringify(body) });
      showToast('重启计划已保存', 'success');
      clearDirty('#reboot-save-state');
      loadSettings();
    } catch (e) { showToast(e.message, 'error'); }
  }

  async function checkRebootPermission() {
    const el = $('#reboot-status');
    if (el) el.textContent = '检测中…';
    try {
      const d = await apiFetch('/api/reboot/check');
      const times = (d.times || []).join('、') || '（未设置时刻）';
      if (el) {
        el.textContent = d.ok
          ? `重启权限正常（${d.output || ''}）· 生效时刻：${times}`
          : `重启权限不可用：${d.output || '未知'} —— 检查 /etc/sudoers.d/99-elf2-reboot`;
      }
    } catch (e) { if (el) el.textContent = '检测失败：' + e.message; }
  }

  async function rebootNow() {
    if (!confirm('确定立即重启中继主控？所有连接会中断约 1~3 分钟。')) return;
    if (!confirm('再次确认：现在就重启？')) return;
    try {
      await apiFetch('/api/reboot/now', { method: 'POST', body: '{}' });
      showToast('已下发重启命令，连接即将中断', 'success');
    } catch (e) { showToast('重启失败：' + e.message, 'error'); }
  }

  async function changeOwnPassword() {
    try {
      await apiFetch('/api/me/password', {
        method: 'POST',
        body: JSON.stringify({ old_password: $('#my-old-pass').value, new_password: $('#my-new-pass').value }),
      });
      $('#my-old-pass').value = '';
      $('#my-new-pass').value = '';
      showToast('密码已修改', 'success');
    } catch (e) { showToast(e.message, 'error'); }
  }

  // ---------------- global audio volume ----------------
  function renderMixerStatus(data) {
    const el = $('#audio-mixer-status');
    if (!el) return;
    const row = (name, v) => v ? `<div><span>${name}</span><b>${v.percent ?? '--'}% ${v.on === false ? '（静音）' : '（开启）'}</b></div>` : `<div><span>${name}</span><b class="muted">不可用</b></div>`;
    el.innerHTML = [
      row('Headphone', data.headphone),
      row('Speaker', data.speaker),
      row('PCM', data.pcm),
      `<div><span>播放设备</span><b>${escapeHtml(data.device || '--')}</b></div>`,
    ].join('');
  }

  async function loadAudioVolume() {
    if (role !== 'admin') return;
    try {
      const data = await apiFetch('/api/audio/volume');
      const slider = $('#volume-slider');
      if (slider) slider.value = data.percent ?? 80;
      if ($('#volume-value')) $('#volume-value').textContent = (data.percent ?? '--') + '%';
      if ($('#volume-mute')) $('#volume-mute').checked = !!data.muted;
      renderMixerStatus(data);
    } catch (e) { showToast(e.message, 'error'); }
  }

  async function applyAudioVolume() {
    try {
      const data = await apiFetch('/api/audio/volume', {
        method: 'POST',
        body: JSON.stringify({
          percent: parseInt($('#volume-slider')?.value || '80', 10),
          muted: !!$('#volume-mute')?.checked,
        }),
      });
      if ($('#volume-value')) $('#volume-value').textContent = (data.percent ?? '--') + '%';
      showToast(`全局音量已设为 ${data.percent}%${data.muted ? '（静音）' : ''}`, 'success');
      loadAudioVolume();
    } catch (e) { showToast(e.message, 'error'); }
  }

  // ---------------- LLM ----------------
  // 「LLM 对话」页与它的提供方下拉已删除：对话并入「中继语音助手 → 文本对话」，
  // 提供方/模型/Base URL/Key 统一在「设置 / 校准 → LLM / 页面设置」里改（loadSettings）。

  // ---------------- TTS ----------------
  // 语音音色 / ICAO / 流式朗读策略的唯一来源是「设置 / 校准」页。
  // LLM 对话页与助手页不再放这些控件，避免同一开关两处各说各话。
  let speakPolicyOn = false;
  function ttsVoice() { return $('#set-tts-voice')?.value || ''; }
  function ttsEnVoice() { return $('#set-tts-en-voice')?.value || ''; }
  function ttsIcao() {
    const el = $('#set-tts-icao');
    return el ? !!el.checked : true;
  }

  function renderVoiceTable() {
    const tb = $('#voice-table tbody');
    if (!tb) return;
    const rows = ttsState.voices.map(v => {
      const sz = v.size ? (v.size / 1048576).toFixed(1) + ' MB' : '-';
      const protectedVoice = v.id === 'zh_CN-huayan-medium';
      const del = protectedVoice
        ? '<span class="muted small">内置保护</span>'
        : `<button class="btn ghost small" data-del-voice="${escapeHtml(v.id)}">删除</button>`;
      return `<tr><td>${escapeHtml(v.id)}</td><td>${escapeHtml(v.name)}</td><td>${escapeHtml(v.language || '')}</td>` +
             `<td>${sz}</td><td>${v.ready ? '可用' : '不完整'}</td><td>${del}</td></tr>`;
    }).join('');
    tb.innerHTML = rows || '<tr><td colspan="6" class="muted">暂无音色包</td></tr>';
    const info = $('#voice-manage-info');
    if (info) info.textContent = `共 ${ttsState.voices.length} 个音色包（Rosmontis_en 等英文音色可用于中英混读）`;
    tb.querySelectorAll('[data-del-voice]').forEach((btn) => {
      btn.addEventListener('click', () => deleteVoicePack(btn.dataset.delVoice));
    });
  }

  async function deleteVoicePack(voiceId) {
    if (!confirm(`确定删除音色包 ${voiceId} ？该目录 /opt/ai/voices/${voiceId} 会被移除。`)) return;
    try {
      await apiFetch('/api/tts/voice/' + encodeURIComponent(voiceId), { method: 'DELETE' });
      showToast('音色包已删除：' + voiceId, 'success');
      await loadTtsProviders();
    } catch (e) { showToast(e.message, 'error'); }
  }

  // 朗读策略落地：policy 是「行为开关」，必须最先应用。
  // 原来这三行写在整个 try 的最末尾，前面「列音色包 / 渲染音色表」任何一步抛异常，
  // 策略就被静默跳过、复选框保持 HTML 默认的未勾选 → 流式朗读一声不响地关掉。
  function applySpeakPolicy(on) {
    speakPolicyOn = !!on;
    if ($('#set-tts-auto-speak')) $('#set-tts-auto-speak').checked = !!on;
  }

  async function loadTtsProviders() {
    let data;
    try {
      data = await apiFetch('/api/tts/providers');
    } catch (e) {
      showToast(e.message, 'error');
      return;
    }
    // 先落地策略，再做下面这些纯装饰性的下拉/表格渲染
    applySpeakPolicy(data.auto_speak);
    try {
      ttsState.current = 'local';
      ttsState.voices = data.voices || [];
      const voiceOptions = ttsState.voices.map(v =>
        `<option value="${escapeHtml(v.id)}">${escapeHtml(v.name)}${v.ready ? '' : '（不完整）'}</option>`).join('');
      const cur = $('#set-tts-voice')?.value || '';
      if ($('#set-tts-voice')) $('#set-tts-voice').innerHTML = voiceOptions || '<option value="">无可选音色</option>';
      // 英文音色下拉：默认「自动」（按 language=en* 挑一个）
      const enVoices = ttsState.voices.filter(v => String(v.language || '').toLowerCase().startsWith('en'));
      const enOptions = '<option value="">自动（有英文音色就用）</option>' + enVoices.map(v =>
        `<option value="${escapeHtml(v.id)}">${escapeHtml(v.name)}</option>`).join('');
      const savedEn = $('#set-tts-en-voice')?.dataset.saved || '';
      ['#set-tts-en-voice'].forEach((sel) => {
        const el = $(sel);
        if (!el) return;
        const prev = el.value || savedEn;
        el.innerHTML = enOptions;
        if (prev && Array.from(el.options).some(o => o.value === prev)) el.value = prev;
      });
      // ICAO 专用音色下拉：全部音色 + 自动
      const icaoOptions = '<option value="">自动（优先 lessac 等官方英文音色）</option>' + ttsState.voices.map(v =>
        `<option value="${escapeHtml(v.id)}">${escapeHtml(v.name)}（${escapeHtml(v.language || '')}）</option>`).join('');
      const savedIcao = $('#set-tts-icao-voice')?.dataset.saved || '';
      const icaoSel = $('#set-tts-icao-voice');
      if (icaoSel) {
        const prev = icaoSel.value || savedIcao;
        icaoSel.innerHTML = icaoOptions;
        if (prev && Array.from(icaoSel.options).some(o => o.value === prev)) icaoSel.value = prev;
      }
      const want = data.local?.voice || '';
      ['#set-tts-voice'].forEach((sel) => {
        const el = $(sel);
        if (!el) return;
        const val = el.value || cur || want;
        if (val && Array.from(el.options).some(o => o.value === val)) el.value = val;
      });
      renderVoiceTable();
    } catch (e) {
      // 只影响下拉/表格这类展示，策略已在上面落地，不会被这里连累
      showToast(e.message, 'error');
    }
  }





  // 带进度的上传（fetch 无法上报上传进度，这里用 XHR）
  function uploadWithProgress(url, fd, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', url, true);
      xhr.withCredentials = true;
      xhr.timeout = 15 * 60 * 1000;
      if (csrfToken) xhr.setRequestHeader('X-CSRF-Token', csrfToken);
      if (xhr.upload && onProgress) {
        xhr.upload.onprogress = (e) => {
          if (e.lengthComputable) onProgress(e.loaded / e.total, e.loaded, e.total);
        };
      }
      xhr.onload = () => {
        let data = null;
        try { data = JSON.parse(xhr.responseText); } catch (e) { data = null; }
        if (xhr.status === 401) return reject(new Error('未登录或会话已过期，请重新登录后再上传'));
        if (xhr.status === 413) return reject(new Error((data && data.error) || '上传内容超过板端上限'));
        if (xhr.status >= 200 && xhr.status < 300 && (!data || data.ok !== false)) {
          return resolve(data || { ok: true });
        }
        reject(new Error((data && data.error) || ('上传失败 HTTP ' + xhr.status)));
      };
      xhr.onerror = () => reject(new Error('网络错误：上传中断（请确认板端在线，且文件未超过上限）'));
      xhr.ontimeout = () => reject(new Error('上传超时（15 分钟），请重试或改用有线网络'));
      xhr.onabort = () => reject(new Error('上传已取消'));
      xhr.send(fd);
    });
  }

  function setUploadBar(barSel, infoSel, ratio, text) {
    const bar = $(barSel);
    if (bar) bar.style.width = Math.max(0, Math.min(100, Math.round(ratio * 100))) + '%';
    const info = $(infoSel);
    if (info && text) info.textContent = text;
  }

  async function uploadVoicePack() {
    const file = $('#tts-voice-zip')?.files?.[0];
    if (!file) return showToast('请选择音色包 zip', 'error');
    const mb = file.size / 1024 / 1024;
    const fd = new FormData();
    fd.append('voice_pack', file, file.name);
    fd.append('voice_id', ($('#tts-voice-id')?.value || '').trim());
    const t0 = Date.now();
    setUploadBar('#voice-upload-bar', '#voice-upload-info', 0, `准备上传 ${file.name}（${mb.toFixed(1)} MB）…`);
    try {
      await uploadWithProgress('/api/tts/upload_voice', fd, (r, loaded, total) => {
        const sp = (loaded / 1024 / 1024) / Math.max(0.001, (Date.now() - t0) / 1000);
        setUploadBar('#voice-upload-bar', '#voice-upload-info', r,
          `上传中 ${(r * 100).toFixed(0)}%（${(loaded / 1048576).toFixed(1)}/${(total / 1048576).toFixed(1)} MB，${sp.toFixed(1)} MB/s）`);
      });
      setUploadBar('#voice-upload-bar', '#voice-upload-info', 1, `上传完成：${file.name}（${mb.toFixed(1)} MB）`);
      showToast('音色包上传成功，已部署到 /opt/ai/voices', 'success');
      await loadTtsProviders();
    } catch (e) {
      setUploadBar('#voice-upload-bar', '#voice-upload-info', 0, '上传失败：' + e.message);
      showToast(e.message, 'error');
    }
  }

  // ---------------- board microphone capture & stream ----------------
  let micPollTimer = null;
  let micListenAbort = null;
  let micListenCtx = null;
  let micListenNextTime = 0;
  let micListenLeftover = new Uint8Array(0);

  async function loadMicLevel() {
    try {
      const data = await apiFetch('/api/mic/level');
      const l = data.level || {};
      const running = !!l.running;
      const level = running ? (l.level ?? 0) : 0;
      if ($('#mic-level')) $('#mic-level').textContent = level + '%';
      if ($('#mic-rms')) $('#mic-rms').textContent = running ? (l.rms ?? '--') : '--';
      if ($('#mic-peak')) $('#mic-peak').textContent = running ? (l.peak ?? '--') : '--';
      if ($('#mic-dbfs')) $('#mic-dbfs').textContent = running ? (l.dbfs ?? '--') : '--';
      if ($('#mic-left-rms')) $('#mic-left-rms').textContent = running ? (l.left_rms ?? '--') : '--';
      if ($('#mic-right-rms')) $('#mic-right-rms').textContent = running ? (l.right_rms ?? '--') : '--';
      if ($('#mic-device')) $('#mic-device').textContent = l.device || '--';
      setBar('#mic-level-bar', level);
      if (!l.running && micPollTimer) {
        micPollTimer.stop();
        micPollTimer = null;
      }
    } catch (e) { /* ignore polling errors */ }
  }

  async function loadMicSettings() {
    try {
      const data = await apiFetch('/api/mic/settings');
      const s = data.settings || {};
      if ($('#mic-source')) $('#mic-source').value = s.source || 'main';
      if ($('#mic-channel')) $('#mic-channel').value = s.channel || 'right';
      if ($('#mic-pga')) { $('#mic-pga').value = s.pga_percent ?? 25; $('#mic-pga-val').textContent = (s.pga_percent ?? 25) + '%'; }
      if ($('#mic-adc')) { $('#mic-adc').value = s.adc_percent ?? 100; $('#mic-adc-val').textContent = (s.adc_percent ?? 100) + '%'; }
      if ($('#mic-l2r2')) { $('#mic-l2r2').value = s.l2r2_percent ?? 0; $('#mic-l2r2-val').textContent = (s.l2r2_percent ?? 0) + '%'; }
      if ($('#mic-aux')) { $('#mic-aux').value = s.aux_boost_percent ?? 0; $('#mic-aux-val').textContent = (s.aux_boost_percent ?? 0) + '%'; }
      if ($('#mic-pga-boost')) $('#mic-pga-boost').checked = !!s.pga_boost;
    } catch (e) { showToast(e.message, 'error'); }
  }

  async function applyMicSettings() {
    try {
      const body = {
        source: $('#mic-source')?.value || 'main',
        channel: $('#mic-channel')?.value || 'right',
        pga_percent: parseInt($('#mic-pga')?.value || '25', 10),
        adc_percent: parseInt($('#mic-adc')?.value || '100', 10),
        l2r2_percent: parseInt($('#mic-l2r2')?.value || '0', 10),
        aux_boost_percent: parseInt($('#mic-aux')?.value || '0', 10),
        pga_boost: !!$('#mic-pga-boost')?.checked,
      };
      const data = await apiFetch('/api/mic/settings', { method: 'POST', body: JSON.stringify(body) });
      showToast('麦克风设置已应用', 'success');
      loadMicSettings();
      loadMicLevel();
    } catch (e) { showToast(e.message, 'error'); }
  }

  async function startMicCapture() {
    try {
      await apiFetch('/api/mic/capture/start', { method: 'POST', body: '{}' });
      showToast('开发板麦克风采集已启动', 'success');
      // 电平轮询 250ms：必须不重叠，否则板端一变慢就是 4 请求/秒的堆积源
      if (!micPollTimer) {
        micPollTimer = ELF2Poll.loop(loadMicLevel, 250, { immediate: true });
      }
    } catch (e) { showToast(e.message, 'error'); }
  }

  async function stopMicCapture() {
    try {
      stopMicListen();
      await apiFetch('/api/mic/capture/stop', { method: 'POST', body: '{}' });
      showToast('开发板麦克风采集已停止', 'success');
      if (micPollTimer) { micPollTimer.stop(); micPollTimer = null; }
      loadMicLevel();
    } catch (e) { showToast(e.message, 'error'); }
  }

  async function startMicListen() {
    if (micListenAbort) return;
    try {
      micListenAbort = new AbortController();
      const resp = await fetch('/api/mic/stream', { signal: micListenAbort.signal });
      if (!resp.ok) {
        const text = await resp.text();
        throw new Error(`HTTP ${resp.status}: ${text.slice(0, 120)}`);
      }
      micListenCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
      await micListenCtx.resume();
      const gain = micListenCtx.createGain();
      gain.gain.value = 0.9;
      gain.connect(micListenCtx.destination);
      micListenNextTime = micListenCtx.currentTime + 0.08;
      const reader = resp.body.getReader();
      showToast('网页实时监听已开启', 'success');
      micListenLeftover = new Uint8Array(0);
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        if (!value || !value.length) continue;
        let all;
        if (micListenLeftover.length) {
          all = new Uint8Array(micListenLeftover.length + value.length);
          all.set(micListenLeftover, 0);
          all.set(value, micListenLeftover.length);
        } else {
          all = value;
        }
        const sampleBytes = all.byteLength - (all.byteLength % 2);
        if (sampleBytes <= 0) {
          micListenLeftover = all;
          continue;
        }
        const samples = new Int16Array(all.buffer, all.byteOffset, sampleBytes / 2);
        const floats = new Float32Array(samples.length);
        for (let i = 0; i < samples.length; i++) floats[i] = samples[i] / 32768;
        const buf = micListenCtx.createBuffer(1, floats.length, 16000);
        buf.copyToChannel(floats, 0);
        const src = micListenCtx.createBufferSource();
        src.buffer = buf;
        src.connect(gain);
        const now = micListenCtx.currentTime;
        const startAt = Math.max(now + 0.03, micListenNextTime);
        src.start(startAt);
        micListenNextTime = startAt + buf.duration;
        micListenLeftover = sampleBytes === all.byteLength ? new Uint8Array(0) : all.slice(sampleBytes);
      }
    } catch (e) {
      if (e.name !== 'AbortError') showToast('网页监听失败：' + e.message, 'error');
    } finally {
      stopMicListen();
    }
  }

  function stopMicListen() {
    if (micListenAbort) {
      try { micListenAbort.abort(); } catch (e) {}
      micListenAbort = null;
    }
    if (micListenCtx) {
      try { micListenCtx.close(); } catch (e) {}
      micListenCtx = null;
    }
    micListenNextTime = 0;
    micListenLeftover = new Uint8Array(0);
  }

  // ---------------- intercom ----------------
  let audioCtx = null;
  let mediaStream = null;
  let scriptNode = null;
  let silentGain = null;
  let recordChunks = [];
  let recording = false;
  let recordSampleRate = 16000;

  function mergeFloat32(chunks) {
    let total = chunks.reduce((n, c) => n + c.length, 0);
    const out = new Float32Array(total);
    let off = 0;
    for (const c of chunks) { out.set(c, off); off += c.length; }
    return out;
  }

  function encodeWav(samples, sampleRate) {
    const buffer = new ArrayBuffer(44 + samples.length * 2);
    const view = new DataView(buffer);
    const writeStr = (off, s) => { for (let i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i)); };
    writeStr(0, 'RIFF');
    view.setUint32(4, 36 + samples.length * 2, true);
    writeStr(8, 'WAVE');
    writeStr(12, 'fmt ');
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, 1, true);
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    writeStr(36, 'data');
    view.setUint32(40, samples.length * 2, true);
    let off = 44;
    for (let i = 0; i < samples.length; i++, off += 2) {
      const s = Math.max(-1, Math.min(1, samples[i]));
      view.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    }
    return new Blob([view], { type: 'audio/wav' });
  }

  async function startRecording() {
    if (recording) return;
    try {
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        throw new Error('当前页面不是安全上下文，浏览器未提供麦克风接口；请使用 HTTPS、localhost，或改用 WAV 上传测试');
      }
      mediaStream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: false, noiseSuppression: false, autoGainControl: false },
      });
      audioCtx = new AudioContext({ sampleRate: 16000 });
      await audioCtx.resume();
      recordSampleRate = audioCtx.sampleRate;
      const source = audioCtx.createMediaStreamSource(mediaStream);
      scriptNode = audioCtx.createScriptProcessor(4096, 1, 1);
      silentGain = audioCtx.createGain();
      silentGain.gain.value = 0;
      scriptNode.onaudioprocess = (e) => {
        if (!recording) return;
        const data = e.inputBuffer.getChannelData(0);
        const copy = new Float32Array(data.length);
        copy.set(data);
        recordChunks.push(copy);
        let peak = 0;
        for (let i = 0; i < data.length; i++) peak = Math.max(peak, Math.abs(data[i]));
        setBar('#record-meter', Math.min(100, peak * 140));
      };
      source.connect(scriptNode);
      scriptNode.connect(silentGain);
      silentGain.connect(audioCtx.destination);
      recordChunks = [];
      recording = true;
      $('#record-dot').classList.add('recording');
      $('#record-status').textContent = '录音中…';
      $('#btn-record').textContent = '停止并上传';
    } catch (e) {
      showToast('无法访问麦克风：' + e.message, 'error');
    }
  }

  async function stopRecording() {
    if (!recording) return;
    recording = false;
    $('#record-dot').classList.remove('recording');
    $('#record-status').textContent = '正在生成 WAV…';
    $('#btn-record').disabled = true;
    try {
      if (scriptNode) scriptNode.disconnect();
      if (silentGain) silentGain.disconnect();
      if (audioCtx) await audioCtx.close();
      if (mediaStream) mediaStream.getTracks().forEach(t => t.stop());
      const samples = mergeFloat32(recordChunks);
      const blob = encodeWav(samples, recordSampleRate);
      const fd = new FormData();
      fd.append('audio', blob, 'web_intercom.wav');
      if (!recordTxEnabled()) fd.append('dry', '1');
      const data = await apiFetch('/api/intercom/upload', { method: 'POST', body: fd });
      showToast(`${recordTxToast(data, '录音')}（${data.duration_ms} ms，${(data.size / 1024).toFixed(1)} KB）`,
                recordTxOk(data) ? 'success' : 'error');
      $('#record-status').textContent = '录音已上传';
    } catch (e) {
      showToast('录音上传失败：' + e.message, 'error');
      $('#record-status').textContent = '上传失败';
    } finally {
      $('#btn-record').disabled = false;
      $('#btn-record').textContent = '开始录音';
      setBar('#record-meter', 0);
      audioCtx = null; mediaStream = null; scriptNode = null; silentGain = null; recordChunks = [];
    }
  }

  // ---------------- 实时对讲：网页麦克风 → 板端 AUX（按住说话） ----------------
  let pushStream = null;          // MediaStream
  let pushCtx = null;             // AudioContext(16000)
  let pushNode = null;            // ScriptProcessor
  let pushMute = null;            // 静音 Gain（保持音频图运行）
  let pushLoopTimer = null;       // UI 刷新
  let pushActive = false;
  let pushToken = '';
  let pushBytes = 0;
  let pushStartedAt = 0;
  let pushQueue = [];
  let pushBusy = false;

  function pushSetStatus(text, kind) {
    const el = $('#push-status');
    if (el) el.textContent = text;
    const dot = $('#push-dot');
    if (dot) {
      dot.classList.toggle('recording', kind === 'on');
    }
  }

  async function pushSendQueue(end) {
    if (pushBusy) return;                      // 上一块还没发完，等下一轮
    if (!pushQueue.length && !end) return;
    pushBusy = true;
    let payload = new Uint8Array(0);
    if (pushQueue.length) {
      let total = 0;
      pushQueue.forEach((b) => { total += b.length; });
      payload = new Uint8Array(total);
      let off = 0;
      pushQueue.forEach((b) => { payload.set(b, off); off += b.length; });
      pushQueue = [];
    }
    const url = '/api/intercom/push?token=' + encodeURIComponent(pushToken) + (end ? '&end=1' : '');
    try {
      const resp = await fetch(url, {
        method: 'POST', body: payload,
        headers: { 'Content-Type': 'application/octet-stream', 'X-CSRF-Token': csrfToken },
        credentials: 'same-origin',
      });
      const data = await resp.json().catch(() => null);
      if (!resp.ok || (data && data.ok === false)) {
        throw new Error((data && data.error) || ('HTTP ' + resp.status));
      }
      if (data && typeof data.total === 'number') pushBytes = data.total;
    } catch (e) {
      pushBusy = false;
      await stopPushTalk('推送中断：' + e.message);
      return;
    }
    pushBusy = false;
  }

  async function startPushTalk() {
    if (pushActive) return;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      showToast('当前不是安全上下文（HTTPS/localhost），浏览器未提供麦克风接口', 'error');
      pushSetStatus('需要 HTTPS');
      return;
    }
    try {
      pushStream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      pushCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
      await pushCtx.resume();
      const src = pushCtx.createMediaStreamSource(pushStream);
      pushNode = pushCtx.createScriptProcessor(4096, 1, 1);
      pushMute = pushCtx.createGain();
      pushMute.gain.value = $('#push-monitor')?.checked ? 0.35 : 0;   // 可选本机监听
      pushNode.onaudioprocess = (ev) => {
        if (!pushActive) return;
        const f = ev.inputBuffer.getChannelData(0);
        const pcm = new Int16Array(f.length);
        for (let i = 0; i < f.length; i++) {
          let s = f[i] * 2.2;                    // 适度增益，贴近电台 MIC 电平
          if (s > 1) s = 1; else if (s < -1) s = -1;
          pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
          const a = Math.abs(f[i]);
          if (a > (startPushTalk._peak || 0)) startPushTalk._peak = a;
        }
        pushQueue.push(new Uint8Array(pcm.buffer));
        setBar('#push-meter', Math.min(100, (startPushTalk._peak || 0) * 140));
        startPushTalk._peak = 0;
      };
      src.connect(pushNode);
      pushNode.connect(pushMute);
      pushMute.connect(pushCtx.destination);
      pushActive = true;
      pushBytes = 0;
      pushQueue = [];
      pushToken = 't' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
      pushStartedAt = Date.now();
      pushSetStatus('对讲中…（松开停止）', 'on');
      await pushSendQueue(false);              // 立刻建立 aplay + 拉 PTT
      pushLoopTimer = setInterval(() => {
        pushSendQueue(false);
        const sec = (Date.now() - pushStartedAt) / 1000;
        if (sec > 120) stopPushTalk('超过 120 秒自动停止');   // 安全上限
        const info = $('#push-info');
        if (info) info.textContent = `已推送 ${(pushBytes / 1024).toFixed(0)} KB / ${sec.toFixed(1)}s（16kHz 单声道，板端 AUX + PTT）`;
      }, 250);
    } catch (e) {
      showToast('无法访问麦克风：' + e.message, 'error');
      pushSetStatus('麦克风不可用');
      await stopPushTalk('麦克风不可用');
    }
  }

  async function stopPushTalk(reason) {
    if (!pushActive && !pushStream && !pushCtx) return;
    const wasActive = pushActive;
    pushActive = false;
    if (pushLoopTimer) { clearInterval(pushLoopTimer); pushLoopTimer = null; }
    try { if (pushNode) pushNode.disconnect(); } catch (e) {}
    try { if (pushMute) pushMute.disconnect(); } catch (e) {}
    if (pushStream) { pushStream.getTracks().forEach((t) => t.stop()); pushStream = null; }
    if (pushCtx) { try { await pushCtx.close(); } catch (e) {} pushCtx = null; }
    pushNode = null; pushMute = null;
    if (wasActive) {
      pushQueue = [];
      await pushSendQueue(true);               // 通知板端收尾并释放 PTT
    }
    pushSetStatus(reason ? ('已停止：' + reason) : '已停止');
    setBar('#push-meter', 0);
    const info = $('#push-info');
    if (info) info.textContent = `本次推送 ${(pushBytes / 1024).toFixed(0)} KB；松开按钮或 5 秒无数据，板端会自动释放 PTT。`;
  }

  // 对讲控制的三个播放动作（测试音 / 录音上传 / 上传 WAV）默认**发射**：
  // 勾选「发射（拉 PTT）」时板端先拉 PTT 再出声；取消勾选则以 dry=1 只送 AUX 本地放音。
  function recordTxEnabled() {
    const el = $('#record-tx-ptt');
    return el ? !!el.checked : true;
  }

  function recordTxToast(data, what) {
    const ptt = (data && data.ptt) || {};
    if (data && data.tx === false) return what + '已送到 AUX（未发射）';
    if (ptt.high) return what + '已发射：PTT 已拉高（GPIO ' + ptt.gpio + '）';
    return what + '已送 AUX，但 PTT 未拉高' + (ptt.error ? ('：' + ptt.error) : '');
  }

  function recordTxOk(data) {
    return !!(data && ((data.ptt && data.ptt.high) || data.tx === false));
  }

  async function playTestTone() {
    try {
      const data = await apiFetch('/api/intercom/test-tone', {
        method: 'POST', body: JSON.stringify({ dry: !recordTxEnabled() }),
      });
      showToast(recordTxToast(data, '测试音'), recordTxOk(data) ? 'success' : 'error');
    } catch (e) { showToast(e.message, 'error'); }
  }

  // ---------------- camera / weather ----------------
  // ---------------- camera / recording / RTMP ----------------
  let cameraSettings = {};
  let cameraOsd = {};
  let cameraServiceState = {};
  let cameraOsdTimer = null;

  function fmtBytes(n) {
    n = Number(n || 0);
    if (n > 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + ' MB';
    if (n > 1024) return (n / 1024).toFixed(1) + ' KB';
    return n + ' B';
  }

  function fillCameraForm(s, osd, bitrate) {
    cameraSettings = s || {};
    cameraOsd = osd || {};
    if ($('#camera-device')) $('#camera-device').value = s.device || '';
    if ($('#camera-resolution')) $('#camera-resolution').value = s.resolution || '';
    if ($('#camera-fps')) $('#camera-fps').value = s.fps ?? 15;
    if ($('#camera-record-dir')) $('#camera-record-dir').value = s.record_dir || '';
    if ($('#camera-loop-seconds')) $('#camera-loop-seconds').value = s.loop_seconds ?? 60;
    if ($('#camera-loop-max-mb')) $('#camera-loop-max-mb').value = s.loop_max_mb ?? 2048;
    if ($('#camera-loop-max-files')) $('#camera-loop-max-files').value = s.loop_max_files ?? 100;
    if ($('#camera-storage-max-mb')) $('#camera-storage-max-mb').value = s.storage_max_mb ?? 8192;
    if ($('#camera-rtmp-url')) $('#camera-rtmp-url').value = s.rtmp_url || '';
    // 码率：输入框里是「页面设置」的值（可能为空 = 跟随环境变量），
    // 但当前真正生效的值单独显示 —— 否则用户看到空框会以为码率没设。
    if ($('#camera-bitrate')) $('#camera-bitrate').value = s.bitrate || '';
    if ($('#camera-stream-bitrate')) $('#camera-stream-bitrate').value = s.stream_bitrate || '';
    updateCameraBitrateInfo(bitrate);
    if ($('#camera-loop-autostart')) $('#camera-loop-autostart').checked = !!s.loop_autostart;
    if ($('#camera-osd-enabled')) $('#camera-osd-enabled').checked = !!osd.enabled;
    if ($('#camera-osd-text')) $('#camera-osd-text').value = osd.text || '';
    if ($('#camera-osd-show-time')) $('#camera-osd-show-time').checked = !!osd.show_time;
    if ($('#camera-osd-position')) $('#camera-osd-position').value = osd.position || 'top-left';
    if ($('#camera-osd-fontsize')) $('#camera-osd-fontsize').value = osd.fontsize ?? 18;
    updateCameraOsd();
  }

  function updateCameraBitrateInfo(bitrate) {
    const el = $('#cam-bitrate-info');
    if (!el) return;
    if (!bitrate) { el.textContent = '码率：暂无数据'; return; }
    const srcText = (b) => (b.source === 'setting' ? '页面设置'
      : (b.source === 'env' ? ('环境变量 ' + (b.env_key || 'RELAY_CAM_BITRATE'))
        : '内置默认'));
    const rec = bitrate.record || {};
    const st = bitrate.stream || {};
    el.textContent = `当前生效：录像 ${rec.value || '--'}（${srcText(rec)}） · `
      + `推流 ${st.value || '--'}（${srcText(st)}）`;
  }

  function updateCameraOsd() {
    const el = $('#camera-osd');
    if (!el) return;
    if (!cameraOsd.enabled) { el.textContent = ''; return; }
    const parts = [];
    if (cameraOsd.show_time) parts.push(new Date().toLocaleString());
    if (cameraOsd.text) parts.push(cameraOsd.text);
    el.textContent = parts.join('\n');
    el.className = 'camera-osd ' + (cameraOsd.position || 'top-left');
  }

  async function loadCameraStatus() {
    try {
      const data = await apiFetch('/api/camera/status');
      cameraServiceState = data.service || {};
      fillCameraForm(data.settings || {}, data.osd || {}, data.bitrate || null);
      const devs = data.devices || [];
      if ($('#camera-devices')) {
        $('#camera-devices').textContent = devs.length ? (devs.slice(0, 8).join(', ') + (devs.length > 8 ? ` 等 ${devs.length} 个` : '')) : '未发现';
      }
      if ($('#camera-device-info')) $('#camera-device-info').textContent = (data.settings && data.settings.device) || '--';
      const st = data.service || {};
      const badge = $('#camera-status-badge');
      if (badge) {
        badge.textContent = st.running
          ? (st.recording ? `采集中 · ${st.recording_label === 'loop' ? '循环录像' : '录像中'}` : '采集中')
          : '未启动';
      }
      const autoBadge = $('#cam-loop-auto-badge');
      if (autoBadge) {
        autoBadge.textContent = data.loop_autostart
          ? (data.loop_manual_stop ? '自动（已手动暂停）' : (data.loop_running ? '自动 · 运行中' : '自动 · 待启动'))
          : '手动';
      }
      const box = $('#camera-service-status');
      if (box) {
        box.innerHTML = `
          <div><span>采集</span><b>${st.running ? '运行' : '停止'}</b></div>
          <div><span>预览客户端</span><b>${st.clients ?? 0}</b></div>
          <div><span>循环录像</span><b>${data.loop_running ? '运行中' : (data.loop_manual_stop ? '手动暂停' : '未运行')}</b></div>
          <div><span>录像任务</span><b>${st.recording ? (st.recording_label || '运行') : '停止'}</b></div>
          <div><span>RTMP</span><b>${st.rtmp ? '推流中' : '停止'}</b></div>
          ${st.last_error ? `<div><span>最近错误</span><b class="muted">${escapeHtml(String(st.last_error).slice(0,120))}</b></div>` : ''}`;
      }
      renderCameraRecordings(data.recordings || []);
      // 录像目录写不进去时把原因顶到眼前：以前只能靠"抓拍 500 / 录像没文件"猜
      const dw = $('#camera-dir-warning');
      if (dw) {
        const prob = data.record_dir_problem || '';
        dw.textContent = prob;
        dw.style.display = prob ? '' : 'none';
      }
      const wx = await apiFetch('/api/weather');
      if (wx.note && $('#weather-note')) $('#weather-note').textContent = wx.note;
    } catch (e) { /* 预留页静默 */ }
  }

  function renderCameraRecordings(list) {
    const tbody = $('#camera-recordings-table tbody');
    if (!tbody) return;
    if (!list.length) {
      tbody.innerHTML = '<tr><td colspan="5" class="muted">暂无录像</td></tr>';
      return;
    }
    tbody.innerHTML = list.map(r => `
      <tr>
        <td>${escapeHtml(r.filename)}</td>
        <td>${r.type === 'loop' ? '循环' : (r.type === 'manual' ? '手动' : '其他')}</td>
        <td>${fmtBytes(r.size)}</td>
        <td>${new Date((r.mtime || 0) * 1000).toLocaleString()}</td>
        <td>
          <button class="btn ghost" data-cam-play="${escapeHtml(r.url)}">播放</button>
          <button class="btn ghost" data-cam-del="${escapeHtml(r.filename)}">删除</button>
        </td>
      </tr>`).join('');
  }

  async function cameraStart() {
    // 启动预览：确保采集进程在跑，然后挂上 MJPEG 画面（后台循环录像不受影响）
    try {
      await apiFetch('/api/camera/start', { method: 'POST', body: '{}' });
      const img = $('#camera-preview');
      if (img) { img.dataset.liveSrc = '1'; img.src = '/api/camera/stream?t=' + Date.now(); }
      window.dispatchEvent(new CustomEvent('elf2:camera-preview-start'));
      showToast('实时预览已启动', 'success');
      loadCameraStatus();
    } catch (e) { showToast(e.message, 'error'); }
  }

  async function cameraStop() {
    // 只断开网页画面：循环录像仍在后台运行（要停采集请用「高级设置 → 停止采集服务」）
    const img = $('#camera-preview');
    if (img) { delete img.dataset.liveSrc; img.src = ''; }
    window.dispatchEvent(new CustomEvent('elf2:camera-preview-stop'));
    showToast('已断开实时预览（后台循环录像不受影响）', 'success');
  }

  async function cameraServiceStart() {
    try {
      await apiFetch('/api/camera/start', { method: 'POST', body: '{}' });
      showToast('摄像头采集服务已启动', 'success');
      loadCameraStatus();
    } catch (e) { showToast(e.message, 'error'); }
  }

  async function cameraServiceStop() {
    if (!confirm('停止采集服务会同时停止循环录像与 RTMP 推流，是否继续？')) return;
    try {
      await apiFetch('/api/camera/stop', { method: 'POST', body: '{}' });
      const img = $('#camera-preview');
      if (img) { delete img.dataset.liveSrc; img.src = ''; }
      window.dispatchEvent(new CustomEvent('elf2:camera-preview-stop'));
      showToast('摄像头采集服务已停止', 'success');
      loadCameraStatus();
    } catch (e) { showToast(e.message, 'error'); }
  }

  async function cameraSnapshot() {
    try {
      const data = await apiFetch('/api/camera/snapshot', { method: 'POST', body: '{}' });
      showToast('抓拍已保存：' + data.filename, 'success');
      loadCameraStatus();
    } catch (e) { showToast(e.message, 'error'); }
  }

  async function cameraLoopStart() {
    try {
      const data = await apiFetch('/api/camera/loop/start', {
        method: 'POST',
        body: JSON.stringify({ seconds: parseInt($('#camera-loop-seconds')?.value || '60', 10) }),
      });
      showToast(`循环录像已启动，分段 ${data.segment_seconds}s`, 'success');
      loadCameraStatus();
    } catch (e) { showToast(e.message, 'error'); }
  }

  async function cameraLoopStop() {
    try {
      await apiFetch('/api/camera/loop/stop', { method: 'POST', body: '{}' });
      showToast('循环录像已停止', 'success');
      loadCameraStatus();
    } catch (e) { showToast(e.message, 'error'); }
  }

  async function cameraManualStart() {
    try {
      const data = await apiFetch('/api/camera/record/manual/start', { method: 'POST', body: '{}' });
      showToast('单独录像已开始：' + data.filename, 'success');
      loadCameraStatus();
    } catch (e) { showToast(e.message, 'error'); }
  }

  async function cameraManualStop() {
    try {
      await apiFetch('/api/camera/record/manual/stop', { method: 'POST', body: '{}' });
      showToast('单独录像已停止并保存', 'success');
      loadCameraStatus();
    } catch (e) { showToast(e.message, 'error'); }
  }

  async function cameraRtmpStart() {
    try {
      const url = $('#camera-rtmp-url')?.value || '';
      await apiFetch('/api/camera/rtmp/start', { method: 'POST', body: JSON.stringify({ url }) });
      showToast('RTMP 推流已启动', 'success');
      loadCameraStatus();
    } catch (e) { showToast(e.message, 'error'); }
  }

  async function cameraRtmpStop() {
    try {
      await apiFetch('/api/camera/rtmp/stop', { method: 'POST', body: '{}' });
      showToast('RTMP 推流已停止', 'success');
      loadCameraStatus();
    } catch (e) { showToast(e.message, 'error'); }
  }

  async function saveCameraSettings() {
    const body = {
      device: $('#camera-device')?.value || '',
      resolution: $('#camera-resolution')?.value || '',
      fps: parseInt($('#camera-fps')?.value || '15', 10),
      quality: 5,
      record_dir: $('#camera-record-dir')?.value || '',
      loop_seconds: parseInt($('#camera-loop-seconds')?.value || '60', 10),
      loop_max_mb: parseInt($('#camera-loop-max-mb')?.value || '2048', 10),
      loop_max_files: parseInt($('#camera-loop-max-files')?.value || '100', 10),
      storage_max_mb: parseInt($('#camera-storage-max-mb')?.value || '8192', 10),
      loop_autostart: !!$('#camera-loop-autostart')?.checked,
      rtmp_url: $('#camera-rtmp-url')?.value || '',
      // 空串 = 跟随环境变量；后端会把 1.5M 这类写法归一化成 1500k
      bitrate: $('#camera-bitrate')?.value?.trim() || '',
      stream_bitrate: $('#camera-stream-bitrate')?.value?.trim() || '',
      osd: {
        enabled: !!$('#camera-osd-enabled')?.checked,
        text: $('#camera-osd-text')?.value || '',
        show_time: !!$('#camera-osd-show-time')?.checked,
        position: $('#camera-osd-position')?.value || 'top-left',
        fontsize: parseInt($('#camera-osd-fontsize')?.value || '18', 10),
      },
    };
    try {
      const res = await apiFetch('/api/camera/settings', { method: 'POST', body: JSON.stringify(body) });
      const changed = (res && res.changed) || [];
      const bit = changed.filter((k) => k === 'bitrate' || k === 'stream_bitrate');
      if (bit.length) {
        const which = bit.map((k) => (k === 'bitrate' ? '录像' : '推流')).join(' / ');
        showToast(`${which}码率已保存，录像/推流正在按新码率重开`, 'success');
      } else if (changed.length) {
        showToast('摄像头设置已保存', 'success');
      } else {
        showToast('设置没有变化', 'success');
      }
      if (res && res.bitrate) updateCameraBitrateInfo(res.bitrate);
      loadCameraStatus();
      window.dispatchEvent(new CustomEvent('elf2:camera-settings-saved'));
    } catch (e) { showToast(e.message, 'error'); }
  }

  // ---------------- weather / wind speed ----------------
  let weatherSettings = {};

  function weatherDate() {
    const d = $('#weather-date')?.value;
    if (d) return d;
    const now = new Date();
    const s = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
    if ($('#weather-date')) $('#weather-date').value = s;
    return s;
  }

  async function loadWeatherRealtime() {
    try {
      const rt = await apiFetch('/api/weather/realtime');
      weatherSettings = rt.settings || {};
      const r = rt.realtime || {};
      if ($('#weather-speed')) {
        $('#weather-speed').innerHTML = `${r.last_speed ?? '--'} <span style="font-size:16px">m/s</span>`;
      }
      if ($('#weather-raw')) $('#weather-raw').textContent = `raw ${r.last_raw ?? '--'} · 更新时间 ${r.last_ts || '--'}`;
      if ($('#weather-status')) {
        $('#weather-status').textContent = r.last_error ? `采集异常：${r.last_error}` : (r.last_ok ? '采集正常' : '等待数据');
        $('#weather-status').className = 'sub ' + (r.last_error ? 'text-error' : 'muted');
      }
      if ($('#weather-modbus-info')) $('#weather-modbus-info').textContent = `从站 ${weatherSettings.slave ?? '--'} / 寄存器 ${weatherSettings.register ?? '--'}`;
      if ($('#weather-interval-info')) $('#weather-interval-info').textContent = (weatherSettings.poll_interval ?? '--') + ' s';
    } catch (e) {
      if ($('#weather-status')) $('#weather-status').textContent = '采集服务异常：' + e.message;
    }
  }

  async function loadWeatherDaily() {
    const date = weatherDate();
    if ($('#weather-chart-date')) $('#weather-chart-date').textContent = date;
    try {
      const st = await apiFetch('/api/weather/stats?date=' + encodeURIComponent(date));
      const s = st.stats || {};
      if ($('#weather-max')) $('#weather-max').textContent = (s.max_speed ?? '--') + ' m/s';
      if ($('#weather-max-time')) $('#weather-max-time').textContent = s.max_time || '--';
      if ($('#weather-avg')) $('#weather-avg').textContent = (s.avg_speed ?? '--') + ' m/s';
      if ($('#weather-min')) $('#weather-min').textContent = (s.min_speed ?? '--') + ' m/s';
      if ($('#weather-count')) $('#weather-count').textContent = s.count ?? '--';
    } catch (e) { /* ignore */ }
    try {
      const interval = $('#weather-sample-interval')?.value || '5';
      const points = await apiFetch(`/api/weather/history?date=${encodeURIComponent(date)}&mode=minute&interval=${encodeURIComponent(interval)}`);
      drawWeatherChart(points.points || [], '#weather-chart', '当日暂无风力数据');
    } catch (e) { /* ignore */ }
  }

  async function loadThRealtime() {
    try {
      const d = await apiFetch('/api/weather/th');
      const r = d.realtime || {};
      const s = d.today || {};
      if ($('#th-temperature')) {
        $('#th-temperature').innerHTML = `${r.th_temperature ?? '--'} <span style="font-size:16px">°C</span>`;
      }
      if ($('#th-humidity')) {
        $('#th-humidity').textContent = `湿度 ${r.th_humidity ?? '--'} %RH · 更新 ${r.th_last_ts || '--'}`;
      }
      if ($('#th-status')) {
        let text;
        if (!r.th_enabled) text = '未启用（在「设置与电压校准 → 温湿度变送器」中启用）';
        else if (r.th_last_error) text = `采集异常：${r.th_last_error}`;
        else if (r.th_last_ok) text = `采集正常 · 错误 ${r.th_error_count || 0} 次`;
        else text = '等待数据（传感器未接线时正常）';
        $('#th-status').textContent = text;
        $('#th-status').className = 'sub ' + (r.th_last_error ? 'text-error' : 'muted');
      }
      const fmt = (v) => (v == null || v === '') ? '--' : `${v}`;
      if ($('#th-temp-range')) $('#th-temp-range').textContent = `${fmt(s.temp_min)} ~ ${fmt(s.temp_max)} °C`;
      if ($('#th-temp-avg')) $('#th-temp-avg').textContent = `${fmt(s.temp_avg)} °C`;
      if ($('#th-humi-range')) $('#th-humi-range').textContent = `${fmt(s.humi_min)} ~ ${fmt(s.humi_max)} %RH`;
      if ($('#th-humi-avg')) $('#th-humi-avg').textContent = `${fmt(s.humi_avg)} %RH`;
      if ($('#th-count')) $('#th-count').textContent = s.count ?? '--';
      if ($('#th-date-label')) $('#th-date-label').textContent = weatherDate();
    } catch (e) {
      if ($('#th-status')) $('#th-status').textContent = '温湿度接口异常：' + e.message;
    }
  }

  async function loadWeather() {
    await Promise.all([loadWeatherRealtime(), loadWeatherDaily(), loadRainRealtime(),
                       loadRainHourly(), loadThRealtime()]);
  }

  async function queryWeatherHistory() {
    const days = $('#weather-export-days')?.value || '7';
    const interval = $('#weather-export-interval')?.value || '10';
    const hint = $('#weather-history-hint');
    if (hint) hint.textContent = '查询中…';
    try {
      const data = await apiFetch(`/api/weather/history_range?days=${encodeURIComponent(days)}&interval=${encodeURIComponent(interval)}`);
      drawWeatherChart(data.points || [], '#weather-history-chart', '所选时间段暂无数据');
      if (hint) hint.textContent = `已查询最近 ${days} 天，采样 ${interval} 分钟，共 ${(data.points || []).length} 个点`;
    } catch (e) {
      if (hint) hint.textContent = '查询失败：' + e.message;
    }
  }

  function exportWeatherCsv() {
    const days = $('#weather-export-days')?.value || '7';
    const interval = $('#weather-export-interval')?.value || '10';
    window.location.href = `/api/weather/export.csv?days=${encodeURIComponent(days)}&interval=${encodeURIComponent(interval)}`;
  }

  // ---------------- precipitation / rain gauge ----------------
  function rainDate() {
    const d = $('#rain-date')?.value;
    if (d) return d;
    const now = new Date();
    const s = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
    if ($('#rain-date')) $('#rain-date').value = s;
    return s;
  }

  async function loadRainRealtime() {
    try {
      const rt = await apiFetch('/api/rain/realtime');
      const r = rt.realtime || {};
      const today = rt.today || {};
      if ($('#rain-total')) {
        $('#rain-total').innerHTML = `${r.last_total ?? '--'} <span style="font-size:16px">mm</span>`;
      }
      if ($('#rain-raw')) $('#rain-raw').textContent = `raw ${r.last_raw ?? '--'} · 更新时间 ${r.last_ts || '--'}`;
      if ($('#rain-status')) {
        $('#rain-status').textContent = r.last_error ? `采集异常：${r.last_error}` : (r.last_ok ? '采集正常' : '等待数据');
        $('#rain-status').className = 'sub ' + (r.last_error ? 'text-error' : 'muted');
      }
      if ($('#rain-today-total')) $('#rain-today-total').textContent = `${today.total_mm ?? '--'} mm`;
      if ($('#rain-recent-hour')) $('#rain-recent-hour').textContent = `${rt.recent_hour_mm ?? '--'} mm`;
      if ($('#rain-max-hour')) {
        $('#rain-max-hour').textContent = (Number(today.total_mm) > 0 && today.max_hour)
          ? `${today.max_hour} / ${today.max_hour_mm ?? 0} mm` : '--';
      }
      if ($('#rain-count')) {
        $('#rain-count').textContent = (today.points || []).reduce((a, p) => a + (p.samples || 0), 0);
      }
      const s = rt.settings || {};
      if ($('#rain-modbus-info')) $('#rain-modbus-info').textContent = `从站 ${s.rain_slave ?? '--'} / 寄存器 ${s.rain_register ?? '--'}`;
      if ($('#rain-scale-info')) $('#rain-scale-info').textContent = `${s.rain_scale ?? '--'} mm/raw`;
    } catch (e) {
      if ($('#rain-status')) $('#rain-status').textContent = '降水采集异常：' + e.message;
    }
  }

  async function loadRainHourly() {
    const date = rainDate();
    if ($('#rain-hourly-date-label')) $('#rain-hourly-date-label').textContent = date;
    const hint = $('#rain-hourly-hint');
    if (hint) hint.textContent = '查询中…';
    try {
      const data = await apiFetch(`/api/rain/hourly?date=${encodeURIComponent(date)}`);
      const stats = data.stats || {};
      const points = stats.points || [];
      drawRainChart(points);
      const tbody = $('#rain-hourly-table tbody');
      if (tbody) {
        tbody.innerHTML = points.map(p => {
          const v = Number(p.rain_mm || 0);
          return `<tr><td>${escapeHtml(p.hour || '--')}</td>` +
            `<td>${v.toFixed(2)}</td>` +
            `<td>${p.samples ?? 0}</td></tr>`;
        }).join('');
      }
      if (hint) {
        const n = points.reduce((a, p) => a + (p.samples || 0), 0);
        hint.textContent = `全天累计 ${stats.total_mm ?? 0} mm，共 ${n} 个采集点`;
      }
    } catch (e) {
      drawRainChart([]);
      if (hint) hint.textContent = '查询失败：' + e.message;
    }
  }

  function exportRainCsv() {
    const date = rainDate();
    window.location.href = `/api/rain/export.csv?date=${encodeURIComponent(date)}`;
  }

  // ---------------- 图表悬停：竖向准线 + 数值点 + 浮层 ----------------
  // 与「总览 - 能量统计 - 全日电压时间轴」共用同一套视觉语言：同底色/网格、
  // 悬停画竖向准线并在每条曲线上标点、浮层样式与 .energy-tip 同源
  // （见 dashboard.html 里的 .chart-tip 别名）。原先这套交互只有能量图有，
  // 风力时间轴是「裸画布」，所以把它抽成公用件，两边口径一致。
  const WIND_AVG_COLOR = '#3b82f6';   // 与卡片标题行图例的色块保持一致
  const WIND_MAX_COLOR = '#ef4444';
  const windCharts = new Map();       // canvas 选择器 -> 悬停状态

  function windChartState(selector, tipSel) {
    if (!windCharts.has(selector)) {
      windCharts.set(selector, { hover: -1, box: null, points: [], ticks: [],
                                 maxV: 1, emptyText: '', tipSel: '' });
    }
    const st = windCharts.get(selector);
    if (tipSel) st.tipSel = tipSel;
    return st;
  }

  function chartHideTip(tipSel) {
    const t = tipSel && $(tipSel);
    if (t) t.classList.add('hidden');
  }

  function chartShowTip(tipSel, box, clientX, html) {
    const tip = tipSel && $(tipSel);
    if (!tip || !box) return;
    tip.innerHTML = html;
    tip.classList.remove('hidden');
    const wrap = tip.parentElement;
    const wrapRect = wrap.getBoundingClientRect();
    let left = clientX - wrapRect.left + 14;
    if (left + tip.offsetWidth > wrap.clientWidth - 2) {
      left = Math.max(2, left - tip.offsetWidth - 28);
    }
    tip.style.left = left + 'px';
    tip.style.top = (box.pad.t + 6) + 'px';
  }

  // marks: [[y 像素, 颜色], …]；y 为 null/undefined 表示该点无数据，跳过不标
  function drawChartCrosshair(ctx, box, x, marks) {
    ctx.strokeStyle = '#8fa2c4';
    ctx.globalAlpha = 0.6;
    ctx.beginPath();
    ctx.moveTo(x, box.pad.t);
    ctx.lineTo(x, box.pad.t + box.ch);
    ctx.stroke();
    ctx.globalAlpha = 1;
    (marks || []).forEach(m => {
      if (!m || m[0] === null || m[0] === undefined) return;
      ctx.fillStyle = m[1];
      ctx.beginPath();
      ctx.arc(x, m[0], 3.2, 0, Math.PI * 2);
      ctx.fill();
    });
  }

  // X 轴刻度：把数据跨度均分若干段取下标。电压时间轴是固定 24 小时整点刻度，
  // 风力时间轴的跨度随数据而定，所以按比例取点，观感与它一致。
  function windTickIndexes(n, segments = 6) {
    if (!n) return [];
    const segs = Math.max(1, Math.min(segments, Math.max(1, n - 1)));
    const out = [];
    for (let k = 0; k <= segs; k++) {
      const i = n === 1 ? 0 : Math.round((n - 1) * k / segs);
      if (out.indexOf(i) < 0) out.push(i);
    }
    return out;
  }

  function windFmt(v, digits = 1) {
    if (v === null || v === undefined || v === '') return '--';
    const num = Number(v);
    return isFinite(num) ? num.toFixed(digits) + ' m/s' : '--';
  }

  // 悬停浮层正文：**两个数据都要点名**（平均 / 最大），再补最小与样本数
  function windTipHtml(p) {
    if (!p) return '';
    const bits = ['<b>' + (p.minute || p.ts || '') + '</b>'];
    bits.push('<span style="color:' + WIND_AVG_COLOR + '">平均风速</span> <b>'
              + windFmt(p.avg_speed) + '</b>');
    const mx = (p.max_speed === null || p.max_speed === undefined) ? p.avg_speed : p.max_speed;
    bits.push('<span style="color:' + WIND_MAX_COLOR + '">最大风速</span> <b>'
              + windFmt(mx) + '</b>');
    if (p.min_speed !== null && p.min_speed !== undefined) {
      bits.push('<span style="opacity:.7">本桶最小 ' + windFmt(p.min_speed) + '</span>');
    }
    const n = (p.n === null || p.n === undefined) ? p.count : p.n;
    if (n !== null && n !== undefined) {
      bits.push('<span style="opacity:.7">' + n + ' 个采样</span>');
    }
    return bits.join('<br>');
  }

  function windNearestIndex(clientX, st, canvas) {
    const b = st && st.box;
    if (!b || !st.points.length) return -1;
    const rect = canvas.getBoundingClientRect();
    const px = clientX - rect.left;
    let bi = -1;
    let bd = Infinity;
    st.points.forEach((p, i) => {
      const d = Math.abs(b.xOf(i) - px);
      if (d < bd) { bd = d; bi = i; }
    });
    return bi;
  }

  function bindWindChartHover(canvasSel, tipSel) {
    const cv = $(canvasSel);
    if (!cv) return;
    const st = windChartState(canvasSel, tipSel);
    const redraw = () => drawWeatherChart(st.points, canvasSel, st.emptyText || '暂无数据');
    cv.addEventListener('mousemove', ev => {
      const i = windNearestIndex(ev.clientX, st, cv);
      if (i < 0) { chartHideTip(st.tipSel); return; }
      if (i !== st.hover) { st.hover = i; redraw(); }
      chartShowTip(st.tipSel, st.box, ev.clientX, windTipHtml(st.points[i]));
    });
    cv.addEventListener('mouseleave', () => {
      if (st.hover !== -1) { st.hover = -1; redraw(); }
      chartHideTip(st.tipSel);
    });
  }

  function drawRainChart(points) {
    const canvas = $('#rain-hourly-chart');
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth || 800;
    const h = 220;
    canvas.width = Math.max(300, w) * dpr;
    canvas.height = h * dpr;
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = '#0d1526';
    ctx.fillRect(0, 0, w, h);
    const pad = { l: 42, r: 12, t: 14, b: 28 };
    const cw = Math.max(10, w - pad.l - pad.r);
    const ch = h - pad.t - pad.b;
    ctx.strokeStyle = '#26334d';
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let i = 0; i <= 4; i++) {
      const y = pad.t + ch * i / 4;
      ctx.moveTo(pad.l, y); ctx.lineTo(pad.l + cw, y);
    }
    ctx.stroke();
    if (!points.length) {
      ctx.fillStyle = '#8fa2c4';
      ctx.font = '13px Microsoft YaHei';
      ctx.fillText('当日暂无降水量数据', pad.l + 10, pad.t + 22);
      return;
    }
    const maxV = Math.max(1, ...points.map(p => Number(p.rain_mm) || 0));
    const n = points.length;
    const slot = cw / n;
    const barW = Math.max(4, slot * 0.55);
    ctx.fillStyle = '#38bdf8';
    points.forEach((p, i) => {
      const v = Number(p.rain_mm) || 0;
      const bh = ch * v / maxV;
      const x = pad.l + slot * i + (slot - barW) / 2;
      const y = pad.t + ch - bh;
      if (bh > 0) ctx.fillRect(x, y, barW, bh);
    });
    ctx.fillStyle = '#8fa2c4';
    ctx.font = '11px Microsoft YaHei';
    for (let i = 0; i <= 4; i++) {
      const v = maxV * (1 - i / 4);
      ctx.fillText(v.toFixed(1), 6, pad.t + ch * i / 4 + 4);
    }
    ctx.textAlign = 'center';
    points.forEach((p, i) => {
      if ((p.hour_index ?? i) % 3 === 0) {
        ctx.fillText((p.hour || '').slice(0, 2), pad.l + slot * i + slot / 2, h - 8);
      }
    });
    ctx.textAlign = 'left';
  }

  function drawWeatherChart(points, selector = '#weather-chart', emptyText = '暂无数据') {
    const canvas = $(selector);
    if (!canvas) return;
    const st = windChartState(selector);
    st.points = points || [];
    st.emptyText = emptyText;
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth || 800;
    const h = 220;
    canvas.width = Math.max(300, w) * dpr;
    canvas.height = h * dpr;
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = '#0d1526';
    ctx.fillRect(0, 0, w, h);
    const pad = { l: 42, r: 12, t: 14, b: 26 };
    const cw = Math.max(10, w - pad.l - pad.r);
    const ch = h - pad.t - pad.b;
    const n = st.points.length;
    const xOf = i => n <= 1 ? pad.l + cw / 2 : pad.l + cw * i / (n - 1);
    st.box = { pad, cw, ch, w, h, xOf };
    // 水平网格
    ctx.strokeStyle = '#26334d';
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const y = pad.t + ch * i / 4;
      ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(pad.l + cw, y); ctx.stroke();
    }
    const label = (p) => ((p && (p.minute || p.ts)) || '').slice(-5);
    // 竖向网格 + 时间刻度：与电压时间轴的整点刻度同一观感（跨度随数据而定，按比例取点）
    const ticks = windTickIndexes(n, 6);
    st.ticks = ticks;
    ctx.fillStyle = '#8fa2c4';
    ctx.font = '11px Microsoft YaHei';
    ticks.forEach((idx, k) => {
      const x = xOf(idx);
      ctx.globalAlpha = (k === 0 || k === ticks.length - 1) ? 0.9 : 0.4;
      ctx.beginPath(); ctx.moveTo(x, pad.t); ctx.lineTo(x, pad.t + ch); ctx.stroke();
      ctx.globalAlpha = 1;
      if (k > 0 && k < ticks.length - 1) {
        ctx.fillText(label(st.points[idx]),
                     Math.min(Math.max(x - 14, pad.l), pad.l + cw - 30), h - 8);
      }
    });
    if (!n) {
      ctx.fillStyle = '#8fa2c4';
      ctx.font = '13px Microsoft YaHei';
      ctx.fillText(emptyText, pad.l + 10, pad.t + 22);
      chartHideTip(st.tipSel);
      return;
    }
    // X 轴首尾时间
    ctx.fillStyle = '#8fa2c4';
    ctx.font = '11px Microsoft YaHei';
    ctx.fillText(label(st.points[0]), pad.l, h - 8);
    ctx.fillText(label(st.points[n - 1]), pad.l + cw - 30, h - 8);
    const valid = st.points.filter(p => p.avg_speed !== null && p.avg_speed !== undefined);
    const maxV = Math.max(1, ...valid.map(p => (p.max_speed ?? p.avg_speed ?? 0)));
    st.maxV = maxV;
    const yOf = v => pad.t + ch - ch * Math.min(1, (v || 0) / maxV);
    st.yOf = yOf;
    // 平均线（与标题行图例色块同色）
    ctx.strokeStyle = WIND_AVG_COLOR;
    ctx.lineWidth = 2;
    ctx.beginPath();
    valid.forEach((p, i) => {
      const idx = st.points.indexOf(p);
      const x = xOf(idx), y = yOf(p.avg_speed);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
    // 最大值线
    ctx.strokeStyle = WIND_MAX_COLOR;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    valid.forEach((p, i) => {
      const idx = st.points.indexOf(p);
      const x = xOf(idx), y = yOf(p.max_speed ?? p.avg_speed);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
    // Y 轴标注
    ctx.fillStyle = '#8fa2c4';
    for (let i = 0; i <= 4; i++) {
      const v = maxV * (1 - i / 4);
      ctx.fillText(v.toFixed(1), 6, pad.t + ch * i / 4 + 4);
    }
    // 悬停：竖向准线 + 两条曲线上的数据点（与电压时间轴一致）
    if (st.hover >= 0 && st.hover < n) {
      const p = st.points[st.hover];
      const mv = (p.max_speed === null || p.max_speed === undefined) ? p.avg_speed : p.max_speed;
      drawChartCrosshair(ctx, st.box, xOf(st.hover), [
        [p.avg_speed === null || p.avg_speed === undefined ? null : yOf(p.avg_speed), WIND_AVG_COLOR],
        [mv === null || mv === undefined ? null : yOf(mv), WIND_MAX_COLOR],
      ]);
    }
  }

  // ---------------- 能量统计（电池 / 光伏电压全日时间轴） ----------------
  // 数据来自后台采样器写入的 voltage_readings。电压原先**不落库**，
  // 所以历史补不回来，时间轴从启用采样之后开始积累。
  const energyState = {
    day: '', interval: 5, points: [], stats: null, loaded: false,
    hover: -1, box: null, scale: null,
    // 默认画**滤波后**的曲线（原始纹波会把趋势糊掉），原始点按需叠加
    filter: 'hampel', showRaw: false, rested: null, logging: null,
  };

  function energyToday() {
    const d = new Date();
    const p = n => String(n).padStart(2, '0');
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
  }

  // X 轴**固定 00:00→24:00**：这样不同日期的曲线能直接叠着比，也才叫「全日时间轴」。
  function energySecOfDay(epoch) {
    const d = new Date(epoch * 1000);
    return d.getHours() * 3600 + d.getMinutes() * 60 + d.getSeconds();
  }

  function energyFmtV(v, digits = 2) {
    return (v === null || v === undefined) ? '--' : Number(v).toFixed(digits) + ' V';
  }

  function energyHm(ts) {
    const m = /[T ](\d{2}:\d{2}:\d{2})/.exec(ts || '');
    return m ? m[1] : (ts || '--');
  }

  function energyVisible() {
    const p = $('#ov-energy');
    const t = $('#tab-overview');
    return !!(p && p.classList.contains('active')
              && t && t.classList.contains('active'));
  }

  async function loadEnergy(showToast) {
    const dayEl = $('#energy-date');
    const ivEl = $('#energy-interval');
    const day = (dayEl && dayEl.value) || energyToday();
    const interval = parseInt((ivEl && ivEl.value) || '5', 10) || 5;
    energyState.day = day;
    energyState.interval = interval;
    try {
      const d = await apiFetch('/api/energy/day?day=' + encodeURIComponent(day)
                               + '&interval=' + interval
                               + '&filter=' + encodeURIComponent(energyState.filter));
      energyState.points = d.points || [];
      energyState.stats = d.stats || {};
      energyState.rested = d.rested || null;
      energyState.logging = d.logging || null;
      const fsEl = $('#energy-filter');
      if (fsEl && d.filter) { fsEl.value = d.filter; energyState.filter = d.filter; }
      energyState.loaded = true;
      energyState.hover = -1;
      renderEnergyCards(d);
      drawEnergyChart();
    } catch (e) {
      if (showToast !== false) toast('能量数据加载失败：' + e.message, 'error');
    }
  }

  function renderEnergyCards(d) {
    const st = d.stats || {};
    const b = st.battery || {};
    const p = st.pv || {};
    const bf = st.battery_f || {};
    const pf = st.pv_f || {};
    const rest = st.rested || d.rested || {};
    const set = (id, txt) => { const el = $('#' + id); if (el) el.textContent = txt; };
    // 两套数并列：滤波后（看趋势/电量）与原始（留尖峰作证据）。
    // 原始 min/max 是当初刻意保留的取舍，这里不删。
    set('energy-bat-rested', rest.battery === null || rest.battery === undefined
        ? '--' : energyFmtV(rest.battery));
    set('energy-bat-min-f', energyFmtV(bf.min));
    set('energy-bat-avg-f', energyFmtV(bf.avg));
    set('energy-bat-drop-f', energyFmtV(st.battery_drop_f));
    set('energy-bat-max', energyFmtV(b.max));
    set('energy-bat-max-ts', energyHm(b.max_ts));
    set('energy-bat-min', energyFmtV(b.min));
    set('energy-bat-min-ts', energyHm(b.min_ts));
    set('energy-bat-drop', energyFmtV(st.battery_drop));
    set('energy-pv-rested', rest.pv === null || rest.pv === undefined
        ? '--' : energyFmtV(rest.pv));
    set('energy-pv-min-f', energyFmtV(pf.min));
    set('energy-pv-avg-f', energyFmtV(pf.avg));
    set('energy-pv-max', energyFmtV(p.max));
    set('energy-pv-max-ts', energyHm(p.max_ts));
    set('energy-pv-min', energyFmtV(p.min));
    set('energy-pv-min-ts', energyHm(p.min_ts));
    set('energy-pv-avg', energyFmtV(p.avg));
    set('energy-count', String(st.points || 0) + ' 点');
    const lg = d.logging || {};
    set('energy-sample-info', (lg.sample_sec === undefined ? '--' : lg.sample_sec) + ' 秒');
    set('energy-burst-info', lg.burst_sec === undefined
        ? '--' : ('摊开 ' + lg.burst_sec + ' 秒 × 每 ' + (lg.burst_gap_ms || 60)
                  + 'ms 一读，取中位数'));
    const fname = { hampel: 'Hampel（去离群）', median: '滑动中位数', none: '不滤波' };
    set('energy-filter-info', (fname[st.filter] || st.filter || '--') +
        (st.filter && st.filter !== 'none' ? '，窗口 ' + st.filter_window + ' 点' : ''));
    set('energy-outliers', (st.outliers === undefined ? '--' : st.outliers) + ' 点');
    set('energy-retention-info',
        (lg.retention_days === undefined ? '--' : lg.retention_days) + ' 天');
    set('energy-span', st.first_ts
        ? (energyHm(st.first_ts) + ' ~ ' + energyHm(st.last_ts)) : '--');
    const note = $('#energy-note');
    if (note) {
      if (lg.enabled === false) {
        note.textContent = '电压采样当前已关闭（设置 → 硬件校准与射频），时间轴不会有新数据。';
      } else {
        note.textContent = '电压原先不落库，历史无法回溯；时间轴从启用采样后开始积累。'
          + '「静息估计」＝近 ' + (rest.window_min || 30) + ' 分钟剔除发射中采样后的中位数'
          + '（' + (rest.samples || 0) + ' 个样本），播报与助手回答用的就是它；'
          + '原始列保留尖峰，用来发现异常。';
      }
    }
  }

  // 缺桶**不连线**：某点为 null 就断开，让图上的空档老实表达「这段时间没采到」，
  // 而不是拉一条直线假装连续。
  function drawEnergySeries(ctx, pts, key, color, xOf, yOf, lw) {
    ctx.strokeStyle = color;
    ctx.lineWidth = lw || 1.8;
    ctx.beginPath();
    let pen = false;
    pts.forEach(p => {
      const v = p[key];
      if (v === null || v === undefined) { pen = false; return; }
      const x = xOf(p);
      const y = yOf(v);
      if (pen) ctx.lineTo(x, y); else ctx.moveTo(x, y);
      pen = true;
    });
    ctx.stroke();
  }

  function drawEnergyChart() {
    const canvas = $('#energy-chart');
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth || 800;
    const h = canvas.clientHeight || 260;
    canvas.width = Math.max(300, w) * dpr;
    canvas.height = h * dpr;
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = '#0d1526';
    ctx.fillRect(0, 0, w, h);
    const pad = { l: 48, r: 14, t: 14, b: 26 };
    const cw = Math.max(10, w - pad.l - pad.r);
    const ch = h - pad.t - pad.b;
    const pts = energyState.points || [];
    energyState.box = { pad, cw, ch, w, h };
    ctx.strokeStyle = '#26334d';
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const y = pad.t + ch * i / 4;
      ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(pad.l + cw, y); ctx.stroke();
    }
    for (let hr = 3; hr < 24; hr += 3) {
      const x = pad.l + cw * hr / 24;
      ctx.globalAlpha = (hr % 6 === 0) ? 0.9 : 0.4;
      ctx.beginPath(); ctx.moveTo(x, pad.t); ctx.lineTo(x, pad.t + ch); ctx.stroke();
    }
    ctx.globalAlpha = 1;
    ctx.fillStyle = '#8fa2c4';
    ctx.font = '11px Microsoft YaHei';
    for (let hr = 0; hr <= 24; hr += 3) {
      const x = pad.l + cw * hr / 24;
      const lab = String(hr).padStart(2, '0') + ':00';
      ctx.fillText(lab, Math.min(x, pad.l + cw - 28), h - 8);
    }
    if (!pts.length) {
      ctx.fillStyle = '#8fa2c4';
      ctx.font = '13px Microsoft YaHei';
      ctx.fillText('当日暂无电压采样（采样从启用后开始，历史无法回溯）',
                   pad.l + 10, pad.t + 24);
      hideEnergyTip();
      return;
    }
    const vals = [];
    const useF = energyState.filter !== 'none';
    pts.forEach(p => {
      const bk = useF ? 'battery_f' : 'battery';
      const pk = useF ? 'pv_f' : 'pv';
      if (p[bk] !== null && p[bk] !== undefined) vals.push(+p[bk]);
      if (p[pk] !== null && p[pk] !== undefined) vals.push(+p[pk]);
      // 勾了「显示原始采样」就把原始点也纳入量程，否则原始线会被裁到框外
      if (energyState.showRaw) {
        if (p.battery !== null && p.battery !== undefined) vals.push(+p.battery);
        if (p.pv !== null && p.pv !== undefined) vals.push(+p.pv);
      }
    });
    let lo = vals.length ? Math.min.apply(null, vals) : 0;
    let hi = vals.length ? Math.max.apply(null, vals) : 1;
    if (!isFinite(lo) || !isFinite(hi)) { lo = 0; hi = 1; }
    if (hi - lo < 0.5) { const mid = (hi + lo) / 2; lo = mid - 0.5; hi = mid + 0.5; }
    const padV = Math.max(0.15, (hi - lo) * 0.12);
    lo = Math.max(0, lo - padV);
    hi = hi + padV;
    const xOf = p => pad.l + cw * (energySecOfDay(p.epoch) / 86400);
    const yOf = v => pad.t + ch - ch * ((v - lo) / (hi - lo));
    energyState.scale = { lo, hi };
    ctx.fillStyle = '#8fa2c4';
    for (let i = 0; i <= 4; i++) {
      ctx.fillText((hi - (hi - lo) * i / 4).toFixed(2), 6, pad.t + ch * i / 4 + 4);
    }
    // 原始采样（细、半透明）垫在下面，滤波曲线（粗）画在上面：
    // 一眼能看出滤波到底抹掉了什么，而不是「悄悄改了数」。
    if (energyState.showRaw) {
      ctx.globalAlpha = 0.45;
      drawEnergySeries(ctx, pts, 'battery', '#f5a623', xOf, yOf, 1.0);
      drawEnergySeries(ctx, pts, 'pv', '#3b82f6', xOf, yOf, 1.0);
      ctx.globalAlpha = 1;
    }
    const bkey = useF ? 'battery_f' : 'battery';
    const pkey = useF ? 'pv_f' : 'pv';
    drawEnergySeries(ctx, pts, bkey, '#f5a623', xOf, yOf, 1.8);
    drawEnergySeries(ctx, pts, pkey, '#3b82f6', xOf, yOf, 1.8);
    const hi2 = energyState.hover;
    if (hi2 >= 0 && hi2 < pts.length) {
      const p = pts[hi2];
      const x = xOf(p);
      ctx.strokeStyle = '#8fa2c4';
      ctx.globalAlpha = 0.6;
      ctx.beginPath(); ctx.moveTo(x, pad.t); ctx.lineTo(x, pad.t + ch); ctx.stroke();
      ctx.globalAlpha = 1;
      [[bkey, '#f5a623'], [pkey, '#3b82f6']].forEach(pair => {
        const v = p[pair[0]];
        if (v === null || v === undefined) return;
        ctx.fillStyle = pair[1];
        ctx.beginPath(); ctx.arc(x, yOf(v), 3.2, 0, Math.PI * 2); ctx.fill();
      });
    }
  }

  function energyNearestIndex(clientX) {
    const cv = $('#energy-chart');
    const b = energyState.box;
    if (!cv || !b || !energyState.points.length) return -1;
    const rect = cv.getBoundingClientRect();
    const sec = (clientX - rect.left - b.pad.l) / b.cw * 86400;
    let bi = -1;
    let bd = Infinity;
    energyState.points.forEach((p, i) => {
      const d = Math.abs(energySecOfDay(p.epoch) - sec);
      if (d < bd) { bd = d; bi = i; }
    });
    return bi;
  }

  function hideEnergyTip() {
    const t = $('#energy-tip');
    if (t) t.classList.add('hidden');
  }

  function showEnergyTip(i, clientX) {
    const tip = $('#energy-tip');
    const b = energyState.box;
    const p = energyState.points[i];
    if (!tip || !b || !p) return;
    const bits = ['<b>' + (p.time || '') + '</b>'];
    const useF = energyState.filter !== 'none';
    if (useF && p.battery_f !== null && p.battery_f !== undefined) {
      bits.push('<span style="color:#f5a623">电池（滤波）</span> <b>'
                + energyFmtV(p.battery_f) + '</b>');
      bits.push('<span style="opacity:.7">原始桶均值 ' + energyFmtV(p.battery) + '</span>');
    } else {
      bits.push('<span style="color:#f5a623">电池</span> <b>' + energyFmtV(p.battery) + '</b>');
    }
    bits.push('<span style="color:#3b82f6">光伏</span> <b>'
              + energyFmtV(useF ? p.pv_f : p.pv) + '</b>');
    if (p.battery_min !== null && p.battery_max !== null
        && p.battery_max !== p.battery_min) {
      bits.push('<span style="opacity:.7">本桶 ' + Number(p.battery_min).toFixed(2)
                + '~' + Number(p.battery_max).toFixed(2) + ' V</span>');
    }
    if (p.outliers) {
      bits.push('<span style="opacity:.7">本桶剔除 ' + p.outliers + ' 个疑似跌落点</span>');
    }
    bits.push('<span style="opacity:.7">' + (p.n || 0) + ' 个采样</span>');
    tip.innerHTML = bits.join('<br>');
    tip.classList.remove('hidden');
    const wrap = tip.parentElement;
    const wrapRect = wrap.getBoundingClientRect();
    let left = clientX - wrapRect.left + 14;
    if (left + tip.offsetWidth > wrap.clientWidth - 2) {
      left = Math.max(2, left - tip.offsetWidth - 28);
    }
    tip.style.left = left + 'px';
    tip.style.top = (b.pad.t + 6) + 'px';
  }

  function initEnergy() {
    const cv = $('#energy-chart');
    if (!cv) return;
    const di = $('#energy-date');
    if (di) {
      di.value = energyToday();
      di.max = energyToday();
      di.addEventListener('change', () => loadEnergy());
    }
    const iv = $('#energy-interval');
    if (iv) iv.addEventListener('change', () => loadEnergy());
    const fs = $('#energy-filter');
    if (fs) fs.addEventListener('change', () => {
      energyState.filter = fs.value || 'hampel';
      loadEnergy();
    });
    const sr = $('#energy-show-raw');
    if (sr) sr.addEventListener('change', () => {
      energyState.showRaw = !!sr.checked;
      drawEnergyChart();
    });
    const ex = $('#btn-energy-export');
    if (ex) {
      ex.addEventListener('click', () => {
        const day = ($('#energy-date') && $('#energy-date').value) || energyToday();
        window.location.href = '/api/energy/export?day=' + encodeURIComponent(day);
      });
    }
    cv.addEventListener('mousemove', ev => {
      const i = energyNearestIndex(ev.clientX);
      if (i < 0) { hideEnergyTip(); return; }
      if (i !== energyState.hover) {
        energyState.hover = i;
        drawEnergyChart();
      }
      showEnergyTip(i, ev.clientX);
    });
    cv.addEventListener('mouseleave', () => {
      if (energyState.hover !== -1) {
        energyState.hover = -1;
        drawEnergyChart();
      }
      hideEnergyTip();
    });
    window.addEventListener('resize', () => {
      if (energyVisible()) drawEnergyChart();
    });
    // 能量曲线按分钟刷新即可（采样间隔默认 60 秒），且只在子选项卡可见时拉
    ELF2Poll.loop(() => { if (energyVisible()) loadEnergy(false); }, 60000,
                  { skipHidden: true });
  }

  // 传感器卡片实时状态（设置页友好显示：是否采集 / 当前值 / 最后成功 / 错误）
  // ---------------- BUSY 接收诊断（设置/校准页） ----------------
  async function pollBusyDiag() {
    if (role !== 'admin') return;
    const card = $('#busy-diag-card');
    if (!card) return;
    if (document.getElementById('tab-settings')?.classList.contains('active') === false) return;
    try {
      const d = await apiFetch('/api/busy/diag');
      const b = d.busy || {};
      const raw = b.sysfs_value;
      if ($('#busy-polarity')) $('#busy-polarity').value = b.active_low ? '1' : '0';
      if ($('#busy-diag-state')) {
        const readable = !(raw === '' || raw == null || String(raw).startsWith('ERR'));
        let txt;
        if (!readable) txt = '引脚不可读：' + (b.error || 'GPIO 未导出');
        else if (b.active) txt = `触发中（${b.on_for || 0}s）`;
        else txt = '空闲（未收到信号）';
        if (readable && b.active && (b.on_for || 0) > 120) {
          txt += ' · 持续 >2 分钟，请核对极性或检查静噪是否常开';
        }
        $('#busy-diag-state').textContent = txt;
      }
      if ($('#busy-diag-live')) {
        const dir = d.busy?.direction || '--';
        $('#busy-diag-live').innerHTML = `
          <div><span>GPIO</span><b>${b.gpio ?? 101}（${d.chip} line ${d.line}）</b></div>
          <div><span>原始电平</span><b>${raw === '' || raw == null ? '--' : raw}</b></div>
          <div><span>方向</span><b>${dir || '--'}</b></div>
          <div><span>有效极性</span><b>${b.active_low ? '低有效' : '高有效'}</b></div>
          <div><span>当前判定</span><b>${b.active ? '接收中' : '空闲'}</b></div>
          <div><span>电平变化次数</span><b>${b.edges ?? 0}</b></div>
          <div><span>累计触发</span><b>${b.count ?? 0} 次 / ${b.total ?? 0}s</b></div>
          <div><span>最近变化</span><b>${b.idle_for ? Math.round(b.idle_for) + 's 前' : '--'}</b></div>
          ${b.tx_conflict ? '<div><span>告警</span><b class="on">发射期间仍 BUSY，注意自激</b></div>' : ''}
          ${b.error ? `<div><span>错误</span><b class="muted">${escapeHtml(String(b.error))}</b></div>` : ''}`;
      }
      if ($('#busy-diag-events')) {
        const evs = (d.events || []).slice(-12).reverse();
        $('#busy-diag-events').innerHTML = evs.length
          ? evs.map((e) => `<span class="muted small">${escapeHtml(e.t)}.${String(e.ms ?? 0).padStart(3, '0')} ${escapeHtml(e.action)}${e.level == null ? '' : ' @' + e.level}</span>`).join('<br>')
          : '<span class="muted small">暂无 BUSY 事件</span>';
      }
    } catch (e) { /* 静默 */ }
  }

  async function saveBusyPolarity() {
    try {
      const activeLow = ($('#busy-polarity')?.value || '1') === '1';
      await apiFetch('/api/busy/polarity', {
        method: 'POST', body: JSON.stringify({ active_low: activeLow }),
      });
      showToast('BUSY 极性已保存并立即生效', 'success');
      pollBusyDiag();
      updateRelayState();
    } catch (e) { showToast(e.message, 'error'); }
  }

  async function readThNow() {
    try {
      const d = await apiFetch('/api/weather/th/read', { method: 'POST', body: '{}' });
      const r = d.result || {};
      showToast(`读取成功：${r.temperature ?? '--'} °C / ${r.humidity ?? '--'} %RH`, 'success');
      loadThRealtime();
      loadSensorStatus();
    } catch (e) {
      showToast('读取失败：' + e.message, 'error');
      loadSensorStatus();
    }
  }

  async function loadSensorStatus() {
    if (role !== 'admin') return;
    try {
      const w = await apiFetch('/api/weather/realtime');
      const r = w.realtime || {};
      const ok = r.last_ok ? new Date(r.last_ok * 1000).toLocaleTimeString('zh-CN') : '--';
      const el = $('#wind-sensor-status');
      if (el) {
        const spd = (r.last_speed == null || r.last_speed === '') ? '--' : (r.last_speed + ' m/s');
        el.textContent = r.running
          ? `状态：采集中　风速 ${spd}　最后成功 ${ok}　错误 ${r.error_count || 0}` +
            (r.last_error ? `（${r.last_error}）` : '')
          : '状态：未运行（保存设置后自动启动）';
      }
      const bus = $('#bus-status');
      if (bus) {
        bus.textContent = r.running
          ? `状态：采集中　轮询 ${r.poll_count || 0} 次　错误 ${r.error_count || 0}`
          : '状态：未运行';
      }
    } catch (e) { /* 忽略 */ }
    try {
      const d = await apiFetch('/api/rain/realtime');
      const rt = d.realtime || {};
      const el = $('#rain-sensor-status');
      if (el) {
        const today = (d.today && d.today.total_mm != null) ? d.today.total_mm : 0;
        const hour = (d.recent_hour_mm != null) ? d.recent_hour_mm : 0;
        const ok = rt.last_ok ? new Date(rt.last_ok * 1000).toLocaleTimeString('zh-CN') : '--';
        el.textContent = rt.enabled
          ? `状态：已启用　今日 ${today} mm　近 1 小时 ${hour} mm　最后成功 ${ok}　错误 ${rt.error_count || 0}` +
            (rt.last_error ? `（${rt.last_error}）` : '')
          : '状态：已停用';
      }
    } catch (e) { /* 忽略 */ }
    try {
      const t = await apiFetch('/api/weather/th');
      const rt = t.realtime || {};
      const el = $('#th-sensor-status');
      const badge = $('#th-conn-badge');
      const ok = rt.th_last_ok ? new Date(rt.th_last_ok * 1000).toLocaleTimeString('zh-CN') : '--';
      if (el) {
        if (!rt.th_enabled) {
          el.textContent = '状态：未启用（保存设置并启用后开始轮询）';
        } else {
          el.textContent = `状态：已启用　温度 ${rt.th_temperature ?? '--'} °C　湿度 ${rt.th_humidity ?? '--'} %RH　` +
            `最后成功 ${ok}　错误 ${rt.th_error_count || 0}` + (rt.th_last_error ? `（${rt.th_last_error}）` : '');
        }
      }
      if (badge) {
        if (!rt.th_enabled) {
          badge.textContent = '未启用';
        } else if (rt.th_last_ok) {
          badge.textContent = '在线';
        } else {
          badge.textContent = rt.th_last_error ? '未接线/无响应' : '等待首次采集';
        }
      }
    } catch (e) { /* 忽略 */ }
  }

  async function loadWeatherSettings() {
    if (role !== 'admin') return;
    try {
      const data = await apiFetch('/api/weather/settings');
      const s = data.settings || {};
      if ($('#weather-port')) $('#weather-port').value = s.port || '';
      if ($('#weather-baud')) $('#weather-baud').value = s.baud ?? 9600;
      if ($('#weather-slave')) $('#weather-slave').value = s.slave ?? 1;
      if ($('#weather-function')) $('#weather-function').value = String(s.function ?? 3);
      if ($('#weather-register')) $('#weather-register').value = s.register ?? 0;
      if ($('#weather-quantity')) $('#weather-quantity').value = s.quantity ?? 1;
      if ($('#weather-scale')) $('#weather-scale').value = s.scale ?? 0.1;
      if ($('#weather-interval')) $('#weather-interval').value = s.poll_interval ?? 2;
      if ($('#rain-enabled')) $('#rain-enabled').value = s.rain_enabled ? '1' : '0';
      if ($('#rain-slave')) $('#rain-slave').value = s.rain_slave ?? 23;
      if ($('#rain-function')) $('#rain-function').value = String(s.rain_function ?? 3);
      if ($('#rain-register')) $('#rain-register').value = s.rain_register ?? 0;
      if ($('#rain-quantity')) $('#rain-quantity').value = s.rain_quantity ?? 1;
      if ($('#rain-scale')) $('#rain-scale').value = s.rain_scale ?? 0.1;
      if ($('#th-enabled')) $('#th-enabled').value = s.th_enabled ? '1' : '0';
      if ($('#th-slave')) $('#th-slave').value = s.th_slave ?? 3;
      if ($('#th-function')) $('#th-function').value = String(s.th_function ?? 4);
      if ($('#th-register')) $('#th-register').value = s.th_register ?? 1;
      if ($('#th-quantity')) $('#th-quantity').value = s.th_quantity ?? 2;
      if ($('#th-scale')) $('#th-scale').value = s.th_scale ?? 0.1;
      if ($('#th-humi-scale')) $('#th-humi-scale').value = s.th_humi_scale ?? 0.1;
      if ($('#th-temp-offset')) $('#th-temp-offset').value = s.th_temp_offset ?? 0;
    } catch (e) { /* ignore */ }
  }

  async function saveWeatherSettings() {
    const body = {
      port: $('#weather-port')?.value || '/dev/ttyS9',
      baud: parseInt($('#weather-baud')?.value || '9600', 10),
      parity: 'N',
      stopbits: 1,
      timeout: 1.0,
      slave: parseInt($('#weather-slave')?.value || '1', 10),
      function: parseInt($('#weather-function')?.value || '3', 10),
      register: parseInt($('#weather-register')?.value || '0', 10),
      quantity: parseInt($('#weather-quantity')?.value || '1', 10),
      scale: parseFloat($('#weather-scale')?.value || '0.1'),
      poll_interval: parseFloat($('#weather-interval')?.value || '2'),
      rain_enabled: ($('#rain-enabled')?.value || '1') === '1',
      rain_slave: parseInt($('#rain-slave')?.value || '23', 10),
      rain_function: parseInt($('#rain-function')?.value || '3', 10),
      rain_register: parseInt($('#rain-register')?.value || '0', 10),
      rain_quantity: parseInt($('#rain-quantity')?.value || '1', 10),
      rain_scale: parseFloat($('#rain-scale')?.value || '0.1'),
      rain_cumulative: true,
      th_enabled: ($('#th-enabled')?.value || '1') === '1',
      th_slave: parseInt($('#th-slave')?.value || '3', 10),
      th_function: parseInt($('#th-function')?.value || '4', 10),
      th_register: parseInt($('#th-register')?.value || '1', 10),
      th_quantity: parseInt($('#th-quantity')?.value || '2', 10),
      th_scale: parseFloat($('#th-scale')?.value || '0.1'),
      th_humi_scale: parseFloat($('#th-humi-scale')?.value || '0.1'),
      th_temp_offset: parseFloat($('#th-temp-offset')?.value || '0'),
    };
    try {
      await apiFetch('/api/weather/settings', { method: 'POST', body: JSON.stringify(body) });
      showToast('气象设置已保存', 'success');
      loadWeather();
    } catch (e) { showToast(e.message, 'error'); }
  }

  async function loadReservedPages() {
    await loadWeather();
  }

  // ---------------- PTT 发射自检（设置/校准「按住发射」，排查硬件） ----------------
  let pttHoldTimer = null;
  let pttPointerDown = false;      // 鼠标/手指是否仍按着
  let pttResumeTimer = null;

  async function pttManual(hold, reason) {
    try {
      const d = await apiFetch('/api/ptt/manual', {
        method: 'POST',
        body: JSON.stringify({ hold: !!hold, reason: reason || '', client_id: CLIENT_ID }),
      });
      renderPttDiag(d);
    } catch (e) {
      if (hold) showToast('PTT 自检失败：' + e.message, 'error');
    }
  }

  function startPttHold() {
    if (pttHoldTimer) return;
    pttManual(true, 'hold-start');
    pttHoldTimer = setInterval(() => pttManual(true, 'heartbeat'), 700);    // 心跳续期（0.7s，抗网络抖动）
    if ($('#ptt-diag-state')) $('#ptt-diag-state').textContent = '发射中（PTT 高）…';
  }

  // opts.resume：浏览器非用户主动地取消了指针（pointercancel / lostpointercapture）时，
  // 只要指针其实还按着，就自动恢复发射，避免出现「按住却间歇性发射」
  function stopPttHold(reason, opts) {
    const was = !!pttHoldTimer;
    if (pttHoldTimer) { clearInterval(pttHoldTimer); pttHoldTimer = null; }
    if (was) pttManual(false, reason || 'release');
    if ($('#ptt-diag-state')) $('#ptt-diag-state').textContent = reason || '已松开';
    if (opts && opts.resume && pttPointerDown) {
      clearTimeout(pttResumeTimer);
      pttResumeTimer = setTimeout(() => { if (pttPointerDown) startPttHold(); }, 200);
    }
  }

  function renderPttDiag(d) {
    const box = $('#ptt-diag-live');
    if (!box || !d) return;
    const p = d.ptt || {};
    const m = d.manual || {};
    const rows = [
      ['软件状态', (p.high ? '高（发射）' : '低（接收）') + '　引用计数 ' + (p.hold_count ?? 0) + (p.release_pending ? '（待释放）' : '')],
      ['引脚实测', 'value = ' + (p.sysfs_value || '--') + '　direction = ' + (p.direction || '--')],
      ['GPIO', (d.chip || 'gpiochip3') + ' line' + (d.line ?? 1) + ' → 全局 ' + (d.gpio_num ?? '?') + '　' + (d.active_high ? '高有效' : '低有效')],
      ['手动发射', m.held ? ('按住中（心跳 ' + m.heartbeat + 's / 上限 ' + m.max_hold + 's）') : (p.manual_hold ? '引用未释放' : '未按住')],
      ['sysfs 节点', p.sysfs || '--'],
      ['写入错误', p.error || '无'],
    ];
    box.innerHTML = rows.map(([k, v]) =>
      `<div><span>${escapeHtml(k)}</span><b>${escapeHtml(String(v))}</b></div>`).join('');
    const evBox = $('#ptt-diag-events');
    if (evBox) {
      const ev = (d.events || []).slice(-8).reverse();
      evBox.innerHTML = ev.length
        ? ev.map(e => `<div class="muted small">${escapeHtml(e.t)}.${String(e.ms || 0).padStart(3, '0')} ` +
            `${e.level ? '↑' : '↓'} <b>${escapeHtml(e.action)}</b> ${escapeHtml(e.reason || '')} ` +
            `<span class="muted">${escapeHtml(e.by || '')} hold=${e.hold}</span></div>`).join('')
        : '<span class="muted small">暂无 PTT 事件</span>';
    }
  }

  async function pollPttDiag() {
    const card = $('#ptt-diag-card');
    if (!card || card.offsetParent === null) return;      // 只在设置页可见时轮询
    try { renderPttDiag(await apiFetch('/api/ptt/diag')); } catch (e) { /* 忽略轮询错误 */ }
  }

  function bindPttSelfTest() {
    const btn = $('#btn-ptt-hold');
    if (btn) {
      btn.style.touchAction = 'none';
      btn.style.userSelect = 'none';
      btn.addEventListener('pointerdown', (e) => {
        e.preventDefault();
        pttPointerDown = true;
        try { btn.setPointerCapture(e.pointerId); } catch (_) { /* 忽略 */ }
        startPttHold();
      });
      btn.addEventListener('pointerup', () => { pttPointerDown = false; stopPttHold('已松开(pointerup)'); });
      btn.addEventListener('pointercancel', () => stopPttHold('指针被取消(pointercancel)', { resume: true }));
      btn.addEventListener('lostpointercapture', () => stopPttHold('指针捕获丢失', { resume: true }));
      window.addEventListener('pointerup', () => {
        pttPointerDown = false;
        if (pttHoldTimer) stopPttHold('已松开(window-pointerup)');
      });
      window.addEventListener('mouseup', () => {
        pttPointerDown = false;
        if (pttHoldTimer) stopPttHold('已松开(window-mouseup)');
      });
      window.addEventListener('blur', () => {
        pttPointerDown = false;
        if (pttHoldTimer) stopPttHold('窗口失焦');
      });
      window.addEventListener('beforeunload', () => { if (pttHoldTimer) pttManual(false, 'unload'); });
    }
    $('#btn-ptt-release')?.addEventListener('click', () => {
      pttPointerDown = false;
      stopPttHold('已强制松开');
    });
  }

  // ---------------- LLM 设置 ----------------
  async function loadLlmAgentSettings() {
    try {
      const d = await apiFetch('/api/settings');
      const s = d.settings || {};
      // 提示词（共用基础设定 + 语音播报约束）已统一收进「中继语音助手」页，本页不再有那两个输入框
      if ($('#set-agent-enabled')) $('#set-agent-enabled').checked = s.agent_enabled === '1';
      if ($('#set-agent-max-iters')) $('#set-agent-max-iters').value = s.agent_max_iters || 3;
      await loadAgentTools(s.agent_tools || '');
    } catch (e) { /* 非管理员或未登录 */ }
  }

  async function loadAgentTools(selectedCsv) {
    try {
      const d = await apiFetch('/api/agent/tools');
      const sel = new Set(String(selectedCsv || '').split(',').map(x => x.trim()).filter(Boolean));
      const box = $('#agent-tool-list');
      if (box) {
        box.innerHTML = (d.tools || []).map(t => {
          const on = sel.size ? sel.has(t.name) : true;
          return `<label class="checkline"><input type="checkbox" class="agent-tool" value="${escapeHtml(t.name)}"${on ? ' checked' : ''}>` +
            ` ${escapeHtml(t.title)} <span class="muted small">${escapeHtml(t.name)}${t.action ? '（会发射）' : ''}</span></label>`;
        }).join('') || '<span class="muted small">无</span>';
      }
    } catch (e) { showToast('技能列表加载失败：' + e.message, 'error'); }
  }

  async function saveLlmAgentSettings() {
    const boxes = [...document.querySelectorAll('.agent-tool')];
    const on = boxes.filter(c => c.checked).map(c => c.value);
    const body = {
      agent_enabled: $('#set-agent-enabled')?.checked ? '1' : '0',
      agent_max_iters: $('#set-agent-max-iters')?.value || 3,
      agent_tools: (boxes.length && on.length !== boxes.length) ? on.join(',') : '',
    };
    try {
      await apiFetch('/api/settings', { method: 'POST', body: JSON.stringify(body) });
      showToast('已保存 Agent 设置', 'success');
      loadAgentTools(body.agent_tools);
    } catch (e) { showToast('保存失败：' + e.message, 'error'); }
  }

  async function loadLlmStats() {
    try {
      const d = await apiFetch('/api/llm/stats?limit=20');
      const tb = $('#llm-stats-table tbody');
      if (tb) {
        const rows = d.recent || [];
        tb.innerHTML = rows.length ? rows.map(r => `<tr><td>${escapeHtml(String(r.ts || '').slice(11, 19))}</td>` +
          `<td>${escapeHtml(r.mode || '')}</td><td>${r.ttft_ms || 0} ms</td>` +
          `<td>${(r.tok_per_s || 0).toFixed(1)}</td><td>${r.tokens || 0}</td>` +
          `<td class="muted small">${escapeHtml(r.tools || '-')}</td></tr>`).join('')
          : '<tr><td colspan="6" class="muted">暂无记录</td></tr>';
      }
      const t = d.today || {};
      const el = $('#llm-stats-today');
      if (el) {
        el.textContent = `今日 ${t.runs || 0} 次 · 平均 ${(t.avg_tps || 0).toFixed(1)} tok/s · ` +
          `最快 ${(t.max_tps || 0).toFixed(1)} tok/s · 平均首字 ${Math.round(t.avg_ttft || 0)} ms · 共 ${t.tokens || 0} tokens`;
      }
    } catch (e) { /* 忽略 */ }
  }

  // ---------------- 语音输入：BUSY 虚拟按键 → 录音 → 端侧 ASR → 文本 ----------------
  let voiceSink = null;
  let voiceT0 = 0;
  let voicePeak = 0;
  let voiceTick = null;

  // ---------------- 总览：PTT / BUSY 实时状态（GPIO3_A1 拉高即 PTT 使能） ----------------
  async function updateRelayState() {
    if (role && document.getElementById('tab-overview')?.classList.contains('active') === false) return;
    try {
      const d = await apiFetch('/api/ptt/status');
      const p = d.ptt || {};
      const el = $('#relay-ptt');
      if (el) {
        el.classList.remove('on', 'rec');
        if (p.high) {
          el.textContent = 'PTT 使能（发射中）';
          el.classList.add('on');
          el.classList.remove('muted');
        } else {
          el.textContent = 'PTT 释放（接收）';
          el.classList.add('muted');
        }
      }
      // BUSY：控制板 BUSY 经光耦输入 GPIO3_A5（全局 GPIO 101），低有效
      const bz = d.busy || {};
      const b = $('#relay-busy');
      if (b) {
        b.classList.remove('on', 'rec');
        const raw = bz.sysfs_value;
        if (raw === '' || raw == null || String(raw).startsWith('ERR')) {
          b.textContent = bz.exported === false ? '引脚不可读' : '未接入';
          b.classList.add('muted');
        } else if (bz.active) {
          b.textContent = bz.tx_conflict
            ? `BUSY 接收中（${bz.on_for || 0}s，发射期间仍 BUSY，注意自激）`
            : `BUSY 接收中（${bz.on_for || 0}s）`;
          b.classList.add('on');
          b.classList.remove('muted');
        } else {
          b.textContent = `BUSY 空闲（GPIO${bz.gpio ?? 101} 电平 ${raw}${bz.count ? '，累计触发 ' + bz.count + ' 次' : ''}）`;
          b.classList.add('muted');
        }
      }
      // 本地录音 / 发射占用（与 GPIO BUSY 无关的本机状态）
      const lb = $('#relay-busy-local');
      if (lb) {
        lb.classList.remove('on', 'rec');
        if (voiceActive) {
          lb.textContent = '本地录音中（语音输入）';
          lb.classList.add('rec');
          lb.classList.remove('muted');
        } else if (p.high) {
          lb.textContent = '发射占用';
          lb.classList.add('on');
          lb.classList.remove('muted');
        } else {
          lb.textContent = '空闲';
          lb.classList.add('muted');
        }
      }
    } catch (e) { /* 忽略 */ }
  }

  // ---------------- event bindings ----------------
  function initEvents() {
    $('#volume-slider')?.addEventListener('input', (e) => {
      if ($('#volume-value')) $('#volume-value').textContent = e.target.value + '%';
    });
    $('#btn-volume-apply')?.addEventListener('click', applyAudioVolume);
    $('#btn-volume-refresh')?.addEventListener('click', loadAudioVolume);
    $('#btn-volume-test')?.addEventListener('click', playTestTone);
    $('#btn-save-cal')?.addEventListener('click', saveCalibration);
    bindPttSelfTest();
    $('#btn-cal-design')?.addEventListener('click', fillDesignCal);
    $('#btn-save-energy')?.addEventListener('click', saveEnergySettings);
    $('#btn-save-agent')?.addEventListener('click', saveLlmAgentSettings);
    $('#btn-refresh-tools')?.addEventListener('click', () => loadAgentTools(''));
    $('#btn-refresh-stats')?.addEventListener('click', loadLlmStats);
    $('#btn-clear-stats')?.addEventListener('click', async () => {
      try { await apiFetch('/api/llm/stats', { method: 'DELETE' }); loadLlmStats(); }
      catch (e) { showToast(e.message, 'error'); }
    });
    $('#btn-add-user')?.addEventListener('click', addUser);
    $('#btn-save-settings')?.addEventListener('click', saveLlmSettings);
    $('#btn-llm-probe')?.addEventListener('click', probeLlmRoute);
    $('#btn-change-pass')?.addEventListener('click', changeOwnPassword);
    $('#set-tts-icao-voice')?.addEventListener('change', (e) => {
      if (e.target) e.target.dataset.saved = e.target.value || '';
    });
    $('#set-tts-en-voice')?.addEventListener('change', (e) => {
      if (e.target) e.target.dataset.saved = e.target.value || '';
    });
    $('#btn-tts-voice-upload')?.addEventListener('click', uploadVoicePack);
    $('#btn-save-tts')?.addEventListener('click', saveTtsSettings);
    $('#btn-save-reboot')?.addEventListener('click', saveRebootSettings);
    $('#btn-reboot-check')?.addEventListener('click', checkRebootPermission);
    $('#btn-reboot-now')?.addEventListener('click', rebootNow);
    bindDirty('#llm-set-card', '#llm-save-state');
    bindDirty('#tts-set-card', '#tts-save-state');
    bindDirty('#reboot-set-card', '#reboot-save-state');
    const micSlider = (id, labelId) => {
      $(id)?.addEventListener('input', (e) => { if ($(labelId)) $(labelId).textContent = e.target.value + '%'; });
    };
    micSlider('#mic-pga', '#mic-pga-val');
    micSlider('#mic-adc', '#mic-adc-val');
    micSlider('#mic-l2r2', '#mic-l2r2-val');
    micSlider('#mic-aux', '#mic-aux-val');
    $('#btn-mic-settings-apply')?.addEventListener('click', applyMicSettings);
    $('#btn-weather-query')?.addEventListener('click', queryWeatherHistory);
    $('#btn-weather-export')?.addEventListener('click', exportWeatherCsv);
    $('#weather-sample-interval')?.addEventListener('change', loadWeatherDaily);
    $('#btn-weather-settings-save')?.addEventListener('click', saveWeatherSettings);
    $('#btn-th-settings-save')?.addEventListener('click', saveWeatherSettings);
    $('#btn-th-read-now')?.addEventListener('click', readThNow);
    $('#btn-busy-polarity-save')?.addEventListener('click', saveBusyPolarity);
    $('#btn-busy-refresh')?.addEventListener('click', pollBusyDiag);
    $('#btn-camera-service-start')?.addEventListener('click', cameraServiceStart);
    $('#btn-camera-service-stop')?.addEventListener('click', cameraServiceStop);
    $('#btn-wind-settings-save')?.addEventListener('click', saveWeatherSettings);
    $('#btn-rain-settings-save')?.addEventListener('click', saveWeatherSettings);
    $('#weather-date')?.addEventListener('change', loadWeatherDaily);
    // 风力时间轴 / 历史风力：悬停显示竖向准线 + 两个数据（平均 / 最大）的数值，
    // 风格与「总览 - 能量统计 - 全日电压时间轴」一致
    bindWindChartHover('#weather-chart', '#weather-tip');
    bindWindChartHover('#weather-history-chart', '#weather-history-tip');
    $('#btn-rain-query')?.addEventListener('click', loadRainHourly);
    $('#btn-rain-export')?.addEventListener('click', exportRainCsv);
    $('#rain-date')?.addEventListener('change', loadRainHourly);
    $('#btn-camera-start')?.addEventListener('click', cameraStart);
    $('#btn-camera-stop')?.addEventListener('click', cameraStop);
    $('#btn-camera-snapshot')?.addEventListener('click', cameraSnapshot);
    $('#btn-camera-loop-start')?.addEventListener('click', cameraLoopStart);
    $('#btn-camera-loop-stop')?.addEventListener('click', cameraLoopStop);
    $('#btn-camera-manual-start')?.addEventListener('click', cameraManualStart);
    $('#btn-camera-manual-stop')?.addEventListener('click', cameraManualStop);
    $('#btn-camera-rtmp-start')?.addEventListener('click', cameraRtmpStart);
    $('#btn-camera-rtmp-stop')?.addEventListener('click', cameraRtmpStop);
    $('#btn-camera-settings-save')?.addEventListener('click', saveCameraSettings);
    $('#camera-recordings-table')?.addEventListener('click', async (e) => {
      const playUrl = e.target?.dataset?.camPlay;
      const delName = e.target?.dataset?.camDel;
      if (playUrl) {
        const v = $('#camera-playback');
        if (v) { v.src = playUrl; v.style.display = 'block'; v.play().catch(() => {}); }
      }
      if (delName) {
        if (!confirm('确定删除录像 ' + delName + ' ？')) return;
        try {
          await apiFetch('/api/camera/recordings/' + encodeURIComponent(delName), { method: 'DELETE' });
          showToast('录像已删除', 'success');
          loadCameraStatus();
        } catch (err) { showToast(err.message, 'error'); }
      }
    });
    $('#btn-mic-start')?.addEventListener('click', startMicCapture);
    $('#btn-mic-stop')?.addEventListener('click', stopMicCapture);
    $('#btn-mic-listen')?.addEventListener('click', startMicListen);
    $('#btn-mic-listen-stop')?.addEventListener('click', stopMicListen);
    $('#btn-record')?.addEventListener('click', () => recording ? stopRecording() : startRecording());
    // 实时对讲：按住说话（指针事件兼容鼠标/触摸）
    const pushBtn = $('#btn-push-talk');
    if (pushBtn) {
      // 按住说话：用 pointer capture 把指针锁在按钮上。
      // 原来还挂了 pointerleave —— 按住时鼠标只要抖一下/移出按钮一点点，
      // 就立刻停发并松开 PTT，用户再按又拉高，观感就是「PTT 反复触发、说不了话」。
      pushBtn.style.touchAction = 'none';
      pushBtn.style.userSelect = 'none';
      pushBtn.addEventListener('pointerdown', (e) => {
        e.preventDefault();
        try { pushBtn.setPointerCapture(e.pointerId); } catch (_) { /* 忽略 */ }
        startPushTalk();
      });
      pushBtn.addEventListener('pointerup', () => { if (pushActive) stopPushTalk('松开按钮'); });
      pushBtn.addEventListener('lostpointercapture', () => { if (pushActive) stopPushTalk('松开按钮'); });
      pushBtn.addEventListener('pointercancel', () => { if (pushActive) stopPushTalk('取消'); });
      if (window.PointerEvent) {
        window.addEventListener('pointerup', () => { if (pushActive) stopPushTalk('松开按钮'); });
      } else {
        window.addEventListener('mouseup', () => { if (pushActive) stopPushTalk('松开按钮'); });
        window.addEventListener('touchend', () => { if (pushActive) stopPushTalk('松开按钮'); });
      }
    }
    $('#btn-push-stop')?.addEventListener('click', () => stopPushTalk('手动停止'));
    $('#push-monitor')?.addEventListener('change', () => {
      if (pushMute) pushMute.gain.value = $('#push-monitor')?.checked ? 0.35 : 0;
    });
    $('#btn-test-tone')?.addEventListener('click', playTestTone);
    $('#btn-upload-wav')?.addEventListener('click', async () => {
      const file = $('#wav-file')?.files?.[0];
      if (!file) return showToast('请先选择一个 WAV 文件', 'error');
      const fd = new FormData();
      fd.append('audio', file, file.name || 'upload.wav');
      if (!recordTxEnabled()) fd.append('dry', '1');
      try {
        const data = await apiFetch('/api/intercom/upload', { method: 'POST', body: fd });
        showToast(`${recordTxToast(data, 'WAV')}（${data.duration_ms} ms）`,
                  recordTxOk(data) ? 'success' : 'error');
      } catch (e) { showToast(e.message, 'error'); }
    });
    $('#users-table')?.addEventListener('click', async (e) => {
      const delId = e.target?.dataset?.del;
      const resetId = e.target?.dataset?.reset;
      if (delId) {
        if (!confirm(`确定删除用户 ${e.target.dataset.name}？`)) return;
        try { await apiFetch(`/api/users/${delId}`, { method: 'DELETE' }); showToast('用户已删除', 'success'); loadUsers(); }
        catch (err) { showToast(err.message, 'error'); }
      }
      if (resetId) {
        const pwd = prompt(`为 ${e.target.dataset.name} 设置新密码（至少 8 位）`);
        if (!pwd) return;
        try { await apiFetch(`/api/users/${resetId}/password`, { method: 'POST', body: JSON.stringify({ password: pwd }) }); showToast('密码已重置', 'success'); }
        catch (err) { showToast(err.message, 'error'); }
      }
    });
  }

  // ---------------- boot ----------------
  document.addEventListener('DOMContentLoaded', () => {
    startBeijingClock();
    initTabs();
    initNavSheet();
    initOverviewSubtabs();
    initEnergy();
    initAccordions();
    initEvents();
    loadStatus();
    loadTtsProviders();
    loadReservedPages();
    loadCameraStatus();
    loadMicLevel();
    loadMicSettings();
    cameraOsdTimer = ELF2Poll.loop(updateCameraOsd, 1000);
    if (role === 'admin') {
      loadUsers();
      loadSettings();
      loadCalibration();       // 电压校准的通道/零点/倍率：删对话代码时被连带删掉过，见上面 CLIENT_ID 的注释
      loadAudioVolume();
      loadWeatherSettings();
      loadSensorStatus();
      loadLlmAgentSettings();
      loadLlmStats();
      loadEnergySettings();
    }
    // 全部改成 ELF2Poll.loop：上一次 settle 之后再排下一次，绝不并发叠加。
    // 原来用 setInterval 时不接口变慢（板端单次可到 10~27s）就会重叠堆积，
    // 把 Flask 的 GIL 抢死 —— 见 static/js/poll.js 顶部说明。
    ELF2Poll.loop(loadStatus, 3000);
    ELF2Poll.loop(updateRelayState, 1500, { immediate: true });
    ELF2Poll.loop(pollPttDiag, 1200);
    ELF2Poll.loop(loadWeatherRealtime, 2000);
    ELF2Poll.loop(loadWeatherDaily, 10000);
    ELF2Poll.loop(loadRainRealtime, 2000);
    ELF2Poll.loop(loadThRealtime, 5000);
    ELF2Poll.loop(pollBusyDiag, 2000);
    ELF2Poll.loop(loadRainHourly, 10000);
  });
})();

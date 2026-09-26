/* APRS 收发页：地图 + 实时收包 + 发射面板 + 原始报文/十六进制
 * 地图底图走板端代理 /api/aprs/tile/<layer>/<z>/<x>/<y>（天地图·服务端 key）。
 * 坐标系说明：天地图是 CGCS2000，与 APRS 的 WGS-84 实用精度一致，
 * 直接绘制即可；不需要 GCJ-02/BD-09 偏移转换。
 */
(function () {
  'use strict';

  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };

  function toast(msg, kind) {
    var el = $('#toast');
    if (!el) { console.log(msg); return; }
    el.textContent = msg;
    el.className = 'toast show' + (kind ? ' ' + kind : '');
    clearTimeout(el._t);
    el._t = setTimeout(function () { el.className = 'toast'; }, 3600);
  }

  function csrf() {
    var m = document.querySelector('meta[name="csrf-token"]');
    return m ? m.getAttribute('content') : '';
  }

  function api(url, opts) {
    opts = opts || {};
    var h = Object.assign({ 'X-CSRF-Token': csrf() }, opts.headers || {});
    if (opts.body && !h['Content-Type']) h['Content-Type'] = 'application/json';
    return fetch(url, Object.assign({}, opts, { headers: h, credentials: 'same-origin' }))
      .then(function (r) { return r.json().catch(function () { return { ok: false, error: 'HTTP ' + r.status }; }); });
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function pad(n, w) { n = String(n); while (n.length < w) n = '0' + n; return n; }
  function hhmmss(ts) {
    var d = new Date(parseFloat(ts) * 1000);
    if (isNaN(d.getTime())) return '--:--:--';
    return pad(d.getHours(), 2) + ':' + pad(d.getMinutes(), 2) + ':' + pad(d.getSeconds(), 2);
  }

  /* ---------------- 时间/数字工具 ---------------- */
  function fmtDur(sec) {
    sec = Number(sec) || 0;
    if (sec < 60) return sec.toFixed(0) + 's';
    if (sec < 3600) return (sec / 60).toFixed(1) + 'min';
    return (sec / 3600).toFixed(1) + 'h';
  }

  /* ================= 地图 ================= */
  var map = null, homeMarker = null, rangeCircle = null;
  var stationLayer = null, trackLayer = null;
  var tileLayers = [], tileErrors = 0, tileOK = 0;
  var lastGeo = null, homeLL = null;

  var SYMDESC = {
    '-': '房子 / 固定台', '_': '气象站', '>': '汽车', 'k': '卡车', 'j': '吉普',
    'v': '面包车', '<': '自行车', 'R': '电台', 'b': '骑行', 'h': '加油站',
    'K': '学校', '#': '医院', 'a': 'ARES', 'S': '卫星', 'O': '气球',
    'Y': '帆船', 'P': '警车', 'U': '巴士', 'X': '直升机', '/': '中继/天线',
    'W': '气象站(海)', 'F': '消防车', 'I': '接入点', 'A': '警笛', 'r': '中继台',
    '@': '飓风', 'C': '海岸站', 'G': 'GPS', 'n': '导航站', 'p': '停车场',
    '=': '列车', '?': '未知', ' ': '未知'
  };

  function symLabel(t, c) {
    var d = SYMDESC[c];
    return (c || '?') + ' ' + (d || '');
  }

  function symColor(dtype) {
    return ({
      weather: '#2a9d8f', position: '#4361ee', status: '#8d99ae',
      telemetry: '#e07a5f', message: '#b5179e', mice: '#f4a261'
    })[dtype] || '#6c757d';
  }

  function initMap() {
    if (map) return;
    map = L.map('aprs-map', { zoomControl: true, preferCanvas: true })
      .setView([22.5333, 114.05], 10);
    L.control.scale({ imperial: false }).addTo(map);

    // 底图与注记分两层（天地图 img 影像 + cva 影像注记）
    var bases = [
      { id: 'img', opts: { maxZoom: 18, zIndex: 1 } },
      { id: 'cva', opts: { maxZoom: 18, zIndex: 2, pane: 'overlayPane' } }
    ];
    bases.forEach(function (b) {
      var url = '/api/aprs/tile/' + b.id + '/{z}/{x}/{y}';
      var lg = L.tileLayer(url, Object.assign({
        attribution: '&copy; 天地图 (CGCS2000)', noWrap: false
      }, b.opts));
      lg.on('tileload', function () { tileOK++; updateTileStatus(); });
      lg.on('tileerror', function () {
        tileErrors++; updateTileStatus();
        if (tileErrors === 6 && tileOK === 0) {
          setMapStatus('底图不可用（天地图未连通或未配置 key），仅显示台站与轨迹', true);
          map.removeLayer(lg);
        }
      });
      lg.addTo(map);
      tileLayers.push(lg);
    });

    stationLayer = L.layerGroup().addTo(map);
    trackLayer = L.layerGroup().addTo(map);
    updateTileStatus();
  }

  function updateTileStatus() {
    if (tileOK > 0) setMapStatus('天地图瓦片 ' + tileOK + ' 张已载入', false);
    else if (tileErrors > 0) setMapStatus('瓦片请求失败 ' + tileErrors + ' 次（检查网络/key）', true);
    else setMapStatus('正在载入底图…', false);
  }

  function setMapStatus(msg, warn) {
    var el = $('#aprs-map-status');
    if (el) { el.textContent = msg; el.style.color = warn ? '#c1121f' : ''; }
  }

  function drawGeo(data) {
    if (!map) initMap();
    lastGeo = data;
    homeLL = data.home && data.home.lat ? [data.home.lat, data.home.lon] : null;

    stationLayer.clearLayers();
    trackLayer.clearLayers();

    if (homeLL) {
      homeMarker = L.circleMarker(homeLL, {
        radius: 8, color: '#c1121f', weight: 3, fillColor: '#fff', fillOpacity: 1
      }).bindPopup('<b>本站 ' + esc(data.home.source || 'manual') + '</b><br>' +
        homeLL[0].toFixed(5) + ', ' + homeLL[1].toFixed(5) +
        '<br><button class="btn btn-sm" onclick="window.__aprsFit&&window.__aprsFit()">回到本站</button>');
      stationLayer.addLayer(homeMarker);
      if ($('#chk-range') && $('#chk-range').checked) { /* 预留 */ }
    }

    var tracks = data.tracks || {};
    (data.stations || []).forEach(function (s) {
      var ll = [parseFloat(s.lat), parseFloat(s.lon)];
      if (isNaN(ll[0]) || isNaN(ll[1])) return;
      var col = symColor(s.dtype);
      var m = L.circleMarker(ll, {
        radius: 6, color: col, weight: 2, fillColor: col, fillOpacity: 0.55
      });
      var rows = ['<b>' + esc(s.src) + '</b>  <span class="muted">' + esc(s.dtype || '') + '</span>',
        '时间：' + esc(s.ts || ''),
        '经纬：' + ll[0].toFixed(5) + ', ' + ll[1].toFixed(5),
        '符号：' + esc(symLabel(s.symbol_table, s.symbol_code)),
        '路径：' + esc(s.path || '')];
      if (s.alt_m != null) rows.push('海拔：' + s.alt_m + ' m');
      if (s.comment) rows.push('注释：' + esc(s.comment));
      m.bindPopup(rows.join('<br>'));
      m.bindTooltip(esc(s.src), { direction: 'top' });
      stationLayer.addLayer(m);

      var tk = tracks[s.src] || tracks[s.src.replace(/-\d+$/, '')];
      if (tk && tk.length > 1 && $('#aprs-track').checked) {
        var pts = tk.map(function (p) { return [parseFloat(p[0]), parseFloat(p[1])]; })
          .filter(function (p) { return !isNaN(p[0]) && !isNaN(p[1]); });
        if (pts.length > 1) {
          trackLayer.addLayer(L.polyline(pts, { color: col, weight: 2, opacity: 0.5 }));
        }
      }
    });

    $('#aprs-coord-note').textContent = (data.stations || []).length + ' 个台站 / ' + (data.count || 0) + ' 个位置点';
  }

  window.__aprsFit = function () {
    if (map && homeLL) map.setView(homeLL, 13);
  };

  function loadGeo() {
    var mins = $('#aprs-window').value;
    return api('/api/aprs/geo?minutes=' + encodeURIComponent(mins)).then(function (r) {
      if (r && r.ok !== false) drawGeo(r);
    }).catch(function () { });
  }

  /* ================= 收包列表 ================= */
  var lastID = 0;
  var listItems = [];

  function pktTitle(p) {
    if (p.dtype === 'message') return '→ ' + (p.msg_to || '') + ' ' + (p.msg_text || '');
    if (p.dtype === 'telemetry') return 'T#' + (p.seq || '') + ' ' + (p.info || '').slice(0, 40);
    if (p.comment) return p.comment;
    return p.info || '';
  }

  function renderList() {
    var box = $('#aprs-list');
    if (!listItems.length) { box.innerHTML = '<div class="aprs-empty">暂无数据</div>'; return; }
    box.innerHTML = listItems.map(function (p) {
      return '<div class="aprs-item" data-id="' + p.id + '">' +
        '<div class="aprs-item-l">' +
        '<span class="aprs-time">' + esc(hhmmss(p.ts_epoch)) + '</span>' +
        '<span class="aprs-dtype dt-' + esc(p.dtype || 'unknown') + '">' + esc(p.dtype_label || p.dtype) + '</span>' +
        '</div>' +
        '<div class="aprs-item-r">' +
        '<div class="aprs-src">' + esc(p.src) + (p.direct ? ' <span class="tag-direct">直收</span>' : ' <span class="tag-via">中继×' + (p.digi_count || 0) + '</span>') + '</div>' +
        '<div class="aprs-txt">' + esc(pktTitle(p)) + '</div>' +
        '</div></div>';
    }).join('');
  }

  function loadList(reset) {
    var q = ['limit=120'];
    var src = $('#aprs-filter-src').value.trim();
    var ty = $('#aprs-filter-type').value;
    if (src) q.push('src=' + encodeURIComponent(src));
    if (ty) q.push('dtype=' + encodeURIComponent(ty));
    if (!reset && lastID) q.push('since_id=' + lastID);
    return api('/api/aprs/list?' + q.join('&')).then(function (r) {
      if (!r || r.ok === false) return;
      var rows = r.packets || [];
      if (reset || !lastID) { listItems = rows; }
      else if (rows.length) { listItems = rows.concat(listItems).slice(0, 300); }
      if (listItems.length) lastID = Math.max(lastID, listItems[0].id || 0);
      // 反向：新包在上
      if (reset || !lastID) listItems.sort(function (a, b) { return (b.ts_epoch || 0) - (a.ts_epoch || 0); });
      renderList();
    });
  }

  /* ================= 详情 ================= */
  function hexdump(hex) {
    hex = (hex || '').replace(/\s+/g, '');
    var out = [];
    for (var i = 0; i < hex.length; i += 32) {
      var chunk = hex.substr(i, 32);
      var bytes = [];
      for (var j = 0; j < chunk.length; j += 2) bytes.push(parseInt(chunk.substr(j, 2), 16));
      var asc = bytes.map(function (b) { return (b >= 32 && b < 127) ? String.fromCharCode(b) : '.'; }).join('');
      out.push(pad((i / 2).toString(16), 4) + '  ' +
        chunk.replace(/(..)/g, '$1 ').trim().padEnd(47, ' ') + ' |' + asc + '|');
    }
    return out.join('\n');
  }

  function showDetail(id) {
    return api('/api/aprs/' + id).then(function (r) {
      if (!r || r.ok === false) { toast(r && r.error || '读取失败', 'err'); return; }
      var p = r.packet || {};
      var wx = null, tel = null;
      try { wx = p.wx_json ? JSON.parse(p.wx_json) : null; } catch (e) { }
      try { tel = p.telemetry_json ? JSON.parse(p.telemetry_json) : null; } catch (e) { }
      var mice = null;
      try { mice = p.mice_json ? JSON.parse(p.mice_json) : null; } catch (e) { }
      var kv = [
        ['时间', p.ts], ['来源', p.src], ['目的', p.dst], ['路径', p.path],
        ['类型', (p.dtype_label || '') + ' (' + (p.dtype || '') + ')'],
        ['帧长', p.frame_len + ' 字节'], ['控制/PID', (p.ctrl != null ? '0x' + p.ctrl.toString(16) : '-') +
          ' / ' + (p.pid != null ? '0x' + p.pid.toString(16) : '-')],
        ['中继跳数', p.digi_count + (p.direct ? '（直收）' : '')]
      ];
      if (p.lat != null) kv.push(['经纬度', p.lat.toFixed(6) + ', ' + p.lon.toFixed(6) +
        '（WGS-84，天地图 CGCS2000 可直接绘制）']);
      if (p.symbol_code) kv.push(['符号', p.symbol_table + p.symbol_code + '  ' + symLabel(p.symbol_table, p.symbol_code)]);
      if (p.alt_m != null) kv.push(['海拔', p.alt_m + ' m']);
      if (p.msg_to) kv.push(['收件', p.msg_to], ['正文', p.msg_text], ['消息号', p.msg_id || '']);
      kv.push(['信息字段', p.info || '']);
      if (wx) kv.push(['气象(已换算SI)', JSON.stringify(wx)]);
      if (tel) kv.push(['遥测', JSON.stringify(tel)]);
      if (mice) {
        kv.push(['Mic-E 目的地址', (mice.mice_dest || '') +
          '（Mic-E 把纬度编码在目的地址里，这是本包的纬度来源）']);
        kv.push(['Mic-E 半球', (mice.mice_lat_ns || '') + (mice.mice_lon_ew || '') +
          '，经度偏移 +' + (mice.mice_lon_offset || 0) + '00°']);
        kv.push(['Mic-E 状态位', (mice.mice_status || '') +
          '（标准集 ' + (mice.mice_std_msg || 0) + ' / 自定义集 ' + (mice.mice_cust_msg || 0) + '）']);
        kv.push(['Mic-E 类型', mice.mice_msg_capable ? '` 支持消息' : "' 单向追踪器"]);
      }
      kv.push(['收录时间', p.created || '']);

      var html = '<div class="aprs-detail-head"><strong>' + esc(p.src) + '</strong>  ' +
        '<span class="aprs-dtype dt-' + esc(p.dtype) + '">' + esc(p.dtype_label || p.dtype) + '</span>  ' +
        '<a class="btn ghost btn-sm" href="/api/aprs/' + id + '/raw">下载原始帧 .bin</a></div>' +
        '<table class="aprs-table">' + kv.map(function (r) {
          return '<tr><th>' + esc(r[0]) + '</th><td>' + esc(r[1]) + '</td></tr>';
        }).join('') + '</table>' +
        '<div class="aprs-hexhead">信息字段(hex)</div><pre class="aprs-hex">' + esc(p.info_hex || '') + '</pre>' +
        '<div class="aprs-hexhead">完整 AX.25 帧 ' + esc((p.raw_hex || '').length / 2) + ' 字节（含 FCS）</div>' +
        '<pre class="aprs-hex">' + esc(hexdump(p.raw_hex)) + '</pre>';
      $('#detail-body').innerHTML = html;
      $$('.aprs-tabs .tab-btn').forEach(function (b) {
        b.classList.toggle('active', b.dataset.atab === 'detail');
      });
      $$('.aprs-pane').forEach(function (pane) {
        pane.hidden = pane.dataset.apane !== 'detail';
      });
    });
  }

  /* ================= 状态 ================= */
  function setBadge(el, text, cls) {
    if (!el) return;
    el.textContent = text;
    el.className = 'badge ' + (cls || 'idle');
  }

  function loadStatus() {
    return api('/api/aprs/status').then(function (r) {
      if (!r || r.ok === false) return;
      var s = r.stats || {}, st = r.settings || {};
      setBadge($('#aprs-enable-badge'), r.enabled ? 'APRS 已启用' : 'APRS 已停用', r.enabled ? 'ok' : 'idle');
      setBadge($('#aprs-busy-badge'), r.busy ? 'BUSY 有信号' : 'BUSY 空闲', r.busy ? 'warn' : 'idle');
      setBadge($('#aprs-ptt-badge'), r.ptt ? 'PTT 发射中' : 'PTT 松开', r.ptt ? 'warn' : 'idle');
      var tnc = r.tnc || {};
      setBadge($('#aprs-tnc-badge'),
        'TNC 解出 ' + (tnc.unique || 0) + ' 帧 / 突发 ' + (tnc.bursts || 0),
        (tnc.unique || 0) > 0 ? 'ok' : 'idle');

      $('#m-rx').textContent = (r.by_type || []).reduce(function (a, x) { return a + (x.n || 0); }, 0);
      $('#m-rx-sub').textContent = '累计 ' + (s.rx_total || 0) + ' 帧，去重丢弃 ' + (s.rx_dropped || 0);
      $('#m-stations').textContent = (r.top_stations || []).length;
      $('#m-stations-sub').textContent = (r.top_stations || []).slice(0, 2).map(function (x) {
        return x.src + '×' + x.n;
      }).join(' ') || '—';
      $('#m-pos').textContent = s.positions || 0;
      $('#m-pos-sub').textContent = '气象 ' + (s.weather || 0) + ' / 遥测 ' + (s.telemetry || 0) +
        ' / 消息 ' + (s.messages || 0);
      $('#m-tx').textContent = (r.tx_today && r.tx_today.ok) || 0;
      $('#m-tx-sub').textContent = '尝试 ' + ((r.tx_today && r.tx_today.total) || 0) +
        '，顺延 ' + (s.tx_deferred || 0) + '，放弃 ' + (s.tx_skipped || 0);

      // 发射计划
      var nx = r.next_tx || {}, now = Date.now() / 1000;
      var names = { weather: '气象', telemetry: '遥测', position: '信标', status: '状态' };
      var parts = Object.keys(nx).map(function (k) {
        var left = nx[k] - now;
        return names[k] + ' ' + (left > 0 ? fmtDur(left) + '后' : '即将');
      });
      $('#tx-next').textContent = parts.length ? parts.join(' · ') : '（未启用定时发射）';
      schedFill(st);
      schedNext(r.next_tx);

      // 统计页
      $('#stat-type').querySelector('tbody').innerHTML = (r.by_type || []).map(function (x) {
        return '<tr><td>' + esc(x.dtype) + '</td><td>' + x.n + '</td></tr>';
      }).join('') || '<tr><td colspan="2">暂无</td></tr>';
      $('#stat-station').querySelector('tbody').innerHTML = (r.top_stations || []).map(function (x) {
        return '<tr><td>' + esc(x.src) + '</td><td>' + x.n + '</td></tr>';
      }).join('') || '<tr><td colspan="2">暂无</td></tr>';
      var tw = r.weather_source || {};
      $('#stat-tnc').querySelector('tbody').innerHTML = [
        ['采样样本', tnc.samples || 0], ['检出突发', tnc.bursts || 0],
        ['解出帧(去重前)', tnc.frames || 0], ['解出帧(唯一)', tnc.unique || 0],
        ['最大双音纯度', (tnc.max_ratio || 0).toFixed(3)],
        ['累计解码耗时', ((tnc.cpu_ms || 0) / 1000).toFixed(1) + ' s'],
        ['气象源', tw.online ? ('在线 风' + (tw.wind_ms != null ? tw.wind_ms + 'm/s' : '—') +
          ' 温' + (tw.temp_c != null ? tw.temp_c + '℃' : '—') +
          ' 湿' + (tw.humidity != null ? tw.humidity + '%' : '—')) : '离线/未接']
      ].map(function (r2) {
        return '<tr><th>' + esc(r2[0]) + '</th><td>' + esc(r2[1]) + '</td></tr>';
      }).join('');

      // 位置配置
      var pos = r.position || {};
      if (!$('#cfg-lat').value && pos.lat != null) {
        $('#cfg-lat').value = pos.lat; $('#cfg-lon').value = pos.lon;
      }
      if (!$('#cfg-call').value) $('#cfg-call').value = st.aprs_mycall || '';
      if (!$('#cfg-ssid').value) $('#cfg-ssid').value = st.aprs_ssid || 0;
      if (!$('#cfg-comment').value) $('#cfg-comment').value = st.aprs_comment || '';
      $('#cfg-pos-source').textContent = (r.position_stat && r.position_stat.source) || 'manual';
      $('#cfg-pos-stat').textContent = JSON.stringify(r.position_stat || {});

      if (homeLL && lastGeo) { /* 已由 drawGeo 画过 */ }
    }).catch(function () { });
  }

  /* ================= 发射 ================= */
  function doTx(type, extra) {
    var body = Object.assign({ type: type }, extra || {});
    var btnText = { weather: '气象包', position: '位置信标', telemetry: '遥测', status: '状态包', message: '消息' }[type] || type;
    $('#tx-result').innerHTML = '<span class="muted">正在发射 ' + esc(btnText) + '…（含载波侦听）</span>';
    return api('/api/aprs/tx', { method: 'POST', body: JSON.stringify(body) }).then(function (r) {
      if (r && r.ok !== false) {
        $('#tx-result').innerHTML = '<div class="ok">发射成功：<code>' + esc(r.info) + '</code>' +
          '<br><span class="muted">音频 ' + r.audio_s + 's，顺延 ' + ((r.defer_ms || 0) / 1000).toFixed(1) + 's</span></div>';
        toast('APRS ' + btnText + ' 已发射', 'ok');
        loadTxLog(); loadStatus();
      } else {
        $('#tx-result').innerHTML = '<div class="err">发射失败：' + esc((r && r.error) || '未知错误') + '</div>';
        toast('发射失败', 'err');
      }
    }).catch(function (e) {
      $('#tx-result').innerHTML = '<div class="err">请求异常：' + esc(e) + '</div>';
    });
  }

  function loadTxLog() {
    return api('/api/aprs/tx/list?limit=60').then(function (r) {
      if (!r || r.ok === false) return;
      var rows = (r.items || []).map(function (x) {
        return '<tr><td>' + esc(x.ts) + '</td><td>' + esc(x.trigger) + '</td>' +
          '<td>' + esc(x.ptype) + '</td><td>' + esc(x.to_call || '') + '</td>' +
          '<td class="mono-sm">' + esc((x.info || '').slice(0, 70)) + '</td>' +
          '<td>' + ((x.defer_ms || 0) / 1000).toFixed(1) + 's</td>' +
          '<td>' + ((x.audio_ms || 0) / 1000).toFixed(2) + 's</td>' +
          '<td>' + (x.ok ? '<span class="ok">成功</span>' : '<span class="err">' + esc(x.error || '失败') + '</span>') + '</td></tr>';
      }).join('');
      $('#txlog-table tbody').innerHTML = rows || '<tr><td colspan="8">暂无记录</td></tr>';
    });
  }

  /* ================= 事件绑定 ================= */
  function bind() {
    $('#btn-aprs-refresh').addEventListener('click', function () {
      lastID = 0; loadStatus(); loadList(true); loadGeo(); loadTxLog();
      toast('已刷新');
    });

    $('#aprs-list').addEventListener('click', function (e) {
      var it = e.target.closest('.aprs-item');
      if (it) showDetail(it.dataset.id);
    });

    $('#aprs-filter-type').addEventListener('change', function () { lastID = 0; loadList(true); });
    var ft = null;
    $('#aprs-filter-src').addEventListener('input', function () {
      clearTimeout(ft); ft = setTimeout(function () { lastID = 0; loadList(true); }, 350);
    });

    $('#aprs-window').addEventListener('change', loadGeo);
    $('#aprs-track').addEventListener('change', function () { if (lastGeo) drawGeo(lastGeo); });
    $('#btn-aprs-fit').addEventListener('click', window.__aprsFit);

    $$('[data-tx]').forEach(function (b) {
      b.addEventListener('click', function () { doTx(b.dataset.tx); });
    });
    $('#btn-tx-msg').addEventListener('click', function () {
      var to = $('#tx-msg-to').value.trim().toUpperCase();
      var text = $('#tx-msg-text').value.trim();
      if (!to) { toast('请填写收件呼号', 'err'); return; }
      if (!text) { toast('请填写消息正文', 'err'); return; }
      doTx('message', { to: to, text: text });
    });

    $$('.aprs-tabs .tab-btn').forEach(function (b) {
      b.addEventListener('click', function () {
        $$('.aprs-tabs .tab-btn').forEach(function (x) { x.classList.remove('active'); });
        b.classList.add('active');
        $$('.aprs-pane').forEach(function (p) { p.hidden = p.dataset.apane !== b.dataset.atab; });
        if (b.dataset.atab === 'txlog') loadTxLog();
      });
    });

    $('#btn-cfg-save').addEventListener('click', function () {
      var body = {
        aprs_mycall: $('#cfg-call').value.trim().toUpperCase(),
        aprs_ssid: $('#cfg-ssid').value,
        aprs_lat: $('#cfg-lat').value,
        aprs_lon: $('#cfg-lon').value,
        aprs_alt_m: $('#cfg-alt').value,
        aprs_comment: $('#cfg-comment').value
      };
      api('/api/aprs/pos', { method: 'POST', body: JSON.stringify(body) }).then(function (r) {
        if (r && r.ok !== false) {
          $('#cfg-result').textContent = '已保存';
          toast('本站位置已保存', 'ok');
          loadStatus(); loadGeo();
        } else {
          $('#cfg-result').textContent = '保存失败：' + ((r && r.error) || '');
          toast('保存失败', 'err');
        }
      });
    });

    var schedBtn = $('#btn-sched-save');
    if (schedBtn) schedBtn.addEventListener('click', schedSave);
    var allOn = $('#btn-sched-all-on');
    if (allOn) allOn.addEventListener('click', function () { schedAll(true); });
    var allOff = $('#btn-sched-all-off');
    if (allOff) allOff.addEventListener('click', function () { schedAll(false); });

    ['txt', 'csv', 'json'].forEach(function (f) {
      var el = $('#btn-aprs-export-' + f);
      if (el) el.addEventListener('click', function () {
        window.location.href = '/api/aprs/export?format=' + f;
      });
    });
  }

  /* ================= 定时发射计划（四项可调） ================= */
  var schedLoaded = false;
  var schedLastNext = {};
  // pt = 服务端 next_tx 里的类型名；st = settings 里的键前缀（信标用的是 beacon）
  var SCHED_ITEMS = [
    { pt: 'weather',   st: 'weather',   label: '气象' },
    { pt: 'position',  st: 'beacon',    label: '信标' },
    { pt: 'telemetry', st: 'telemetry', label: '遥测' },
    { pt: 'status',    st: 'status',    label: '状态' }
  ];

  function schedRows() { return $$('.sched-row'); }

  function schedRowFor(pt) {
    var found = null;
    schedRows().forEach(function (row) { if (row.dataset.pt === pt) found = row; });
    return found;
  }

  // 把秒数折成最易读的单位
  function schedUnitFor(sec) {
    if (sec >= 3600 && sec % 3600 === 0) return { unit: '3600', value: sec / 3600 };
    if (sec >= 60 && sec % 60 === 0) return { unit: '60', value: sec / 60 };
    return { unit: '1', value: sec };
  }

  // 只在首次与保存后回填，避免 4 秒轮询把用户正在改的内容冲掉
  function schedFill(st) {
    if (schedLoaded || !st) return;
    SCHED_ITEMS.forEach(function (it) {
      var row = schedRowFor(it.pt);
      if (!row) return;
      var sec = parseInt(st['aprs_' + it.st + '_interval'], 10);
      if (!isFinite(sec) || sec <= 0) sec = 1800;
      var u = schedUnitFor(sec);
      var en = $('.sched-en', row), num = $('.sched-num', row), un = $('.sched-unit', row);
      if (en) en.checked = String(st['aprs_' + it.st + '_enabled']) === '1';
      if (num) num.value = u.value;
      if (un) un.value = u.unit;
    });
    schedLoaded = true;
    schedNext(schedLastNext);
  }

  // 每轮状态刷新时更新各行的「下次发射」
  function schedNext(nx) {
    schedLastNext = nx || {};
    var now = Date.now() / 1000;
    SCHED_ITEMS.forEach(function (it) {
      var row = schedRowFor(it.pt);
      if (!row) return;
      var cell = $('.sched-now', row), en = $('.sched-en', row);
      if (!cell) return;
      if (en && !en.checked) { cell.textContent = '未启用'; return; }
      var t = schedLastNext[it.pt];
      if (t == null) { cell.textContent = '待排期'; return; }
      var left = t - now;
      cell.textContent = left > 0 ? ('下次 ' + fmtDur(left) + '后') : '即将发射';
    });
  }

  function schedCollect() {
    var body = {}, bad = [];
    SCHED_ITEMS.forEach(function (it) {
      var row = schedRowFor(it.pt);
      if (!row) return;
      var en = $('.sched-en', row);
      var on = !!(en && en.checked);
      body['aprs_' + it.st + '_enabled'] = on ? '1' : '0';
      var num = $('.sched-num', row), un = $('.sched-unit', row);
      var v = parseFloat(num && num.value);
      var mult = parseInt(un && un.value, 10) || 60;
      if (!isFinite(v) || v <= 0) {
        if (on) bad.push(it.label + ' 间隔需为正数');
        return;
      }
      var sec = Math.round(v * mult);
      if (sec < 60 || sec > 86400) {
        bad.push(it.label + ' 间隔需在 60 秒 ~ 24 小时');
        return;
      }
      body['aprs_' + it.st + '_interval'] = String(sec);
    });
    return { body: body, bad: bad };
  }

  function schedSave() {
    var res = $('#tx-sched-result');
    if (!res) return;
    var c = schedCollect();
    if (c.bad.length) {
      res.textContent = c.bad.join('；');
      toast(c.bad[0], 'err');
      return;
    }
    res.textContent = '正在保存…';
    api('/api/settings', { method: 'POST', body: JSON.stringify(c.body) }).then(function (r) {
      if (r && r.ok !== false) {
        res.textContent = '已保存';
        toast('定时发射计划已保存', 'ok');
        schedLoaded = false;
        loadStatus();
      } else {
        res.textContent = '保存失败：' + ((r && r.error) || '');
        toast('保存失败', 'err');
      }
    }).catch(function (e) {
      res.textContent = '请求异常：' + e;
      toast('保存失败', 'err');
    });
  }

  // 全开/全关只改勾选状态，需再点「保存发射计划」才落盘（避免误触发发射）
  function schedAll(on) {
    schedRows().forEach(function (row) {
      var en = $('.sched-en', row);
      if (en) en.checked = !!on;
    });
    var res = $('#tx-sched-result');
    if (res) res.textContent = (on ? '已勾选全部项目' : '已取消全部项目') + '，点「保存发射计划」生效';
    schedNext(schedLastNext);
  }

  /* ================= 启动 ================= */
  function boot() {
    if (window.APP_ROLE === 'admin') {
      var sbox = $('#tx-sched-edit');
      if (sbox) sbox.hidden = false;
    }
    initMap();
    bind();
    loadStatus(); loadList(true); loadGeo(); loadTxLog();
    // 不重叠轮询：上一次返回之后才排下一次（见 static/js/poll.js）
    ELF2Poll.loop(function () {
      var p = loadStatus();
      if ($('#aprs-live').checked) return Promise.all([p, loadList(false)]);
      return p;
    }, 4000);
    ELF2Poll.loop(function () {
      if ($('#aprs-live').checked) return loadGeo();
    }, 15000);
    console.log('[APRS] ready');
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();

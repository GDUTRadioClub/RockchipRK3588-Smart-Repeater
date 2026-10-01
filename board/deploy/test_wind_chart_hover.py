# -*- coding: utf-8 -*-
"""风力时间轴悬停预览自测（样式对齐「总览-能量统计-全日电压时间轴」）。

要证明的三件事（用户的三条要求）：
  1. **鼠标悬停显示竖向数据**：`drawWeatherChart` 必须记录绘图框与点集，
     并在悬停时画竖向准线 + 两条曲线上的数据点（`drawChartCrosshair`）；
     两个画布都要绑定 `bindWindChartHover`。
  2. **标明两个数据的显示**：标题行图例必须点出「平均风速 / 最大风速」，
     且图例色块与 JS 里的曲线常量、浮层文字颜色三者一致。
  3. **显示风格参照电压时间轴**：底色 #0d1526 / 网格 #26334d / 浮层类
     `.chart-tip`（与 `.energy-tip` 同源样式）/ 提示文案都与之对齐。

除了源码级断言，还把 `windFmt` / `windTipHtml` / `windTickIndexes` 抠出来
交给 **node 真实执行**（本机与板上都跑；没装 node 就 SKIP 这一节）——
浮层里到底写了什么，只有真的跑一遍才算数。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
BOARD = HERE.parent
APP_JS = BOARD / 'static' / 'js' / 'app.js'
DASH = BOARD / 'templates' / 'dashboard.html'

FAIL, OK, SKIP = [], [0], []


def check(name, cond, extra=''):
    if cond:
        OK[0] += 1
        print('  OK   %s' % name)
    else:
        FAIL.append(name)
        print('  FAIL %s %s' % (name, extra))


def skip(name, why=''):
    SKIP.append(name)
    print('  SKIP %s %s' % (name, why))


def read(p):
    try:
        return p.read_text(encoding='utf-8')
    except Exception as e:
        print('  FAIL 读不到 %s：%s' % (p, e))
        FAIL.append('read %s' % p.name)
        return ''


def grab_js_func(src, name):
    """按花括号配对抠出一个 JS 函数（含 function 行）。"""
    m = re.search(r'^[ \t]*function %s\(' % re.escape(name), src, re.M)
    if not m:
        return ''
    i = src.find('{', m.end() - 1)
    if i < 0:
        return ''
    depth = 0
    for j in range(i, len(src)):
        c = src[j]
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return src[m.start():j + 1]
    return ''


JS = read(APP_JS)
HTML = read(DASH)
draw_weather = grab_js_func(JS, 'drawWeatherChart')

print('=== 1. 悬停必须真的画出来（竖向准线 + 数据点）===')
check('drawWeatherChart 抠得出来', bool(draw_weather))
check('绘图时登记悬停状态（windChartState）', 'windChartState(selector)' in draw_weather)
check('把绘图框存进状态（st.box =）', 'st.box =' in draw_weather)
check('把点集存进状态（st.points =）', 'st.points =' in draw_weather)
check('悬停时画竖向准线（drawChartCrosshair）',
      'drawChartCrosshair(ctx, st.box' in draw_weather)
check('准线按 st.hover 触发',
      bool(re.search(r'st\.hover >= 0 && st\.hover < n', draw_weather)))
check('两条曲线各自标点（平均 + 最大）',
      'WIND_AVG_COLOR],' in draw_weather.replace('\n', ' ').replace('  ', ' ')
      or draw_weather.count('WIND_AVG_COLOR') >= 2 and draw_weather.count('WIND_MAX_COLOR') >= 2,
      (draw_weather.count('WIND_AVG_COLOR'), draw_weather.count('WIND_MAX_COLOR')))

print('\n=== 2. 两个画布都要绑定悬停 ===')
check('当日风力时间轴绑定（#weather-chart）',
      "bindWindChartHover('#weather-chart', '#weather-tip')" in JS)
check('历史风力绑定（#weather-history-chart）',
      "bindWindChartHover('#weather-history-chart', '#weather-history-tip')" in JS)
check('bindWindChartHover 内部挂了 mousemove',
      "addEventListener('mousemove'" in grab_js_func(JS, 'bindWindChartHover'))
check('bindWindChartHover 内部挂了 mouseleave（移开即隐藏）',
      "addEventListener('mouseleave'" in grab_js_func(JS, 'bindWindChartHover'))
check('悬停重绘用同一个 selector/emptyText（redraw 闭包）',
      bool(re.search(r'drawWeatherChart\(st\.points, canvasSel, st\.emptyText', JS)))

print('\n=== 3. 图例点名两个数据，且三处颜色一致 ===')


def legend_colors(anchor):
    """取某张卡片标题行图例里的色块（画布标签往前 900 字符内）。"""
    seg = HTML[max(0, anchor - 900):anchor]
    return re.findall(r'class="chart-dot"[^>]*background:(#[0-9a-fA-F]{3,6})', seg)


w_card = HTML.find('id="weather-chart"')
h_card = HTML.find('id="weather-history-chart"')
leg_w = legend_colors(w_card)
leg_h = legend_colors(h_card)
check('当日风力卡片图例点名「平均风速 / 最大风速」且配色正确',
      leg_w == ['#3b82f6', '#ef4444'], leg_w)
check('历史风力卡片图例点名「平均风速 / 最大风速」且配色正确',
      leg_h == ['#3b82f6', '#ef4444'], leg_h)
check('图例点名「平均风速」', '平均风速' in HTML)
check('图例点名「最大风速」', '最大风速' in HTML)
check('两个卡片都写了「鼠标悬停查看该处数值」', HTML.count('鼠标悬停查看该处数值') >= 2,
      HTML.count('鼠标悬停查看该处数值'))
check('图例色块与 JS 曲线常量一致（#3b82f6 平均 / #ef4444 最大）',
      "WIND_AVG_COLOR = '#3b82f6'" in JS and "WIND_MAX_COLOR = '#ef4444'" in JS)
check('曲线用的是这两个常量（不再硬编码颜色）',
      'ctx.strokeStyle = WIND_AVG_COLOR' in draw_weather
      and 'ctx.strokeStyle = WIND_MAX_COLOR' in draw_weather)

print('\n=== 4. 风格与电压时间轴对齐 ===')
check('背景色一致 #0d1526', "ctx.fillStyle = '#0d1526'" in draw_weather)
check('网格色一致（竖向网格也画了）',
      "ctx.strokeStyle = '#26334d'" in draw_weather
      and bool(re.search(r'moveTo\(x, pad\.t\); ctx\.lineTo\(x, pad\.t \+ ch\)', draw_weather)))
check('画布包在 chart-wrap 里（浮层定位需要 relative）',
      'class="chart-wrap"' in HTML and '.energy-wrap, .chart-wrap { position: relative; }' in HTML)
check('浮层用 chart-tip，且与 energy-tip 同源样式',
      'class="chart-tip hidden"' in HTML
      and '.energy-tip, .chart-tip { position: absolute' in HTML)
check('浮层隐藏类也同源', '.energy-tip.hidden, .chart-tip.hidden { display: none; }' in HTML)
check('浮层数字用等宽数字（tabular-nums，与 energy-tip 一致）',
      '.energy-tip b, .chart-tip b { font-variant-numeric: tabular-nums; }' in HTML)
check('两个画布都带 chart-canvas 类', HTML.count('class="chart-canvas"') == 2,
      HTML.count('class="chart-canvas"'))
check('时间刻度用了与电压轴同一观感（11px + 每段竖线）',
      "'11px Microsoft YaHei'" in draw_weather and 'windTickIndexes(n, 6)' in draw_weather)

print('\n=== 5. 浮层文案：node 真实执行 ===')
FMT = grab_js_func(JS, 'windFmt')
TIP = grab_js_func(JS, 'windTipHtml')
TICK = grab_js_func(JS, 'windTickIndexes')
have = [bool(FMT), bool(TIP), bool(TICK)]
check('三个纯函数都抠得出来（windFmt/windTipHtml/windTickIndexes）', all(have), have)

node = shutil.which('node')
if not node:
    skip('node 执行浮层文案', '本机/板上没有 node（源码断言仍然有效）')
elif not all(have):
    skip('node 执行浮层文案', '函数抠取失败')
else:
    script = '\n'.join([
        "const WIND_AVG_COLOR = '#3b82f6';",
        "const WIND_MAX_COLOR = '#ef4444';",
        FMT, TIP, TICK,
        "const out = {};",
        "out.full = windTipHtml({minute:'2026-10-01 08:35', avg_speed:3.2, max_speed:6.1,"
        " min_speed:0.4, n:12});",
        "out.nullv = windTipHtml({minute:'09:00', avg_speed:null, max_speed:null});",
        "out.avgonly = windTipHtml({minute:'10:00', avg_speed:2.5});",
        "out.count_only = windTipHtml({minute:'11:00', avg_speed:1, count:7});",
        "out.empty = windTipHtml(null);",
        "out.fmt = [windFmt(3.2), windFmt(null), windFmt(''), windFmt('abc'), windFmt(0)];",
        "out.ticks_empty = windTickIndexes(0);",
        "out.ticks_one = windTickIndexes(1);",
        "out.ticks13 = windTickIndexes(13, 6);",
        "out.ticks5 = windTickIndexes(5, 6);",
        "require('fs').writeFileSync(process.argv[2], JSON.stringify(out));",
    ])
    with tempfile.TemporaryDirectory() as td:
        jsp = os.path.join(td, 'tip.js')
        outp = os.path.join(td, 'out.json')
        Path(jsp).write_text(script, encoding='utf-8')
        try:
            with open(outp, 'wb') as fh:
                rc = subprocess.run([node, jsp, outp], stdout=fh,
                                    stderr=subprocess.STDOUT, timeout=60).returncode
            data = json.loads(Path(outp).read_text(encoding='utf-8')) if rc == 0 else {}
        except Exception as e:
            data = {}
            print('  node 执行异常：%s: %s' % (type(e).__name__, e))
        if not data:
            check('node 跑通并产出结果', False, 'rc=%s' % rc)
        else:
            check('node 跑通并产出结果', True)
            full = data.get('full', '')
            check('浮层含时间', '08:35' in full, full)
            check('浮层点名「平均风速」', '平均风速' in full)
            check('浮层点名「最大风速」', '最大风速' in full)
            check('浮层给出平均数值 3.2 m/s', '3.2 m/s' in full)
            check('浮层给出最大数值 6.1 m/s', '6.1 m/s' in full)
            check('浮层带最小与样本数', '0.4 m/s' in full and '12 个采样' in full)
            check('平均行用蓝、最大行用红（与图例一致）',
                  '#3b82f6' in full and '#ef4444' in full)
            check('缺值时显示 --（不显示 NaN/undefined）',
                  '--' in data.get('nullv', '') and 'NaN' not in data.get('nullv', ''))
            check('只有平均时最大行回落为平均值',
                  '最大风速' in data.get('avgonly', '') and '2.5 m/s' in data.get('avgonly', ''))
            check('样本数兼容 count 字段', '7 个采样' in data.get('count_only', ''))
            check('空点返回空串', data.get('empty') == '')
            check('windFmt：正常/空/非数字 → 正确文案',
                  data.get('fmt') == ['3.2 m/s', '--', '--', '--', '0.0 m/s'], data.get('fmt'))
            te, to, t13, t5 = (data.get('ticks_empty'), data.get('ticks_one'),
                               data.get('ticks13'), data.get('ticks5'))
            check('刻度：0 点 → 空', te == [])
            check('刻度：1 点 → [0]', to == [0])
            check('刻度：13 点分 6 段 → 7 个且首尾正确',
                  t13 and len(t13) == 7 and t13[0] == 0 and t13[-1] == 12, t13)
            check('刻度：点太少时不越界且递增',
                  bool(t5) and len(set(t5)) == len(t5) and t5 == sorted(t5) and t5[-1] == 4, t5)

print('\n=== 6. 缓存版本号 bump（否则浏览器还跑旧 app.js）===')
BASE_HTML = read(BOARD / 'templates' / 'base.html')
check('base.html 引用 app.js 带 v=', bool(re.search(r"js/app\.js',\s*v='[^']+'", BASE_HTML)))
check('版本号已不是 20261001a（本轮已改 app.js）', "v='20261001a'" not in BASE_HTML)

print('\n=== 7. 悬停交互：桩 canvas 真跑一遍（准线 + 两个点 + 浮层出现/消失）===')
NEED = {
    'windChartState': grab_js_func(JS, 'windChartState'),
    'chartHideTip': grab_js_func(JS, 'chartHideTip'),
    'chartShowTip': grab_js_func(JS, 'chartShowTip'),
    'drawChartCrosshair': grab_js_func(JS, 'drawChartCrosshair'),
    'windTickIndexes': TICK, 'windFmt': FMT, 'windTipHtml': TIP,
    'windNearestIndex': grab_js_func(JS, 'windNearestIndex'),
    'bindWindChartHover': grab_js_func(JS, 'bindWindChartHover'),
    'drawWeatherChart': draw_weather,
}
missing = [k for k, v in NEED.items() if not v]
check('桩测试所需函数都抠得出来', not missing, missing)
AVG_C = re.search(r"const WIND_AVG_COLOR = '#[0-9a-fA-F]+';", JS)
MAX_C = re.search(r"const WIND_MAX_COLOR = '#[0-9a-fA-F]+';", JS)
CH_MAP = re.search(r"const windCharts = new Map\(\);", JS)
CONSTS = bool(AVG_C and MAX_C and CH_MAP)

if not node:
    skip('桩 canvas 交互测试', '没有 node')
elif missing or not CONSTS:
    check('常量与函数齐备', False, (missing, CONSTS))
else:
    harness = '\n'.join([
        AVG_C.group(0),
        MAX_C.group(0),
        CH_MAP.group(0),
        "const window = { devicePixelRatio: 1 };",
        NEED['windChartState'], NEED['chartHideTip'], NEED['chartShowTip'],
        NEED['drawChartCrosshair'], NEED['windTickIndexes'], NEED['windFmt'],
        NEED['windTipHtml'], NEED['windNearestIndex'], NEED['bindWindChartHover'],
        NEED['drawWeatherChart'],
        """
// ---- 桩：记录每次 canvas 绘制调用 + 假 DOM ----
const drawCalls = [];
const ctx = new Proxy({}, {
  get: function (_t, k) { return function () { drawCalls.push([String(k)].concat([].slice.call(arguments))); }; },
  set: function (_t, k, v) { drawCalls.push(['set:' + String(k), v]); return true; },
});
const listeners = {};
function mkCanvas(sel) {
  return {
    clientWidth: 800, clientHeight: 220, width: 0, height: 0,
    getContext: function () { return ctx; },
    getBoundingClientRect: function () { return { left: 0, top: 0, width: 800, height: 220 }; },
    addEventListener: function (ev, fn) { listeners[sel + '|' + ev] = fn; },
  };
}
function mkTip() {
  const el = { innerHTML: '', offsetWidth: 160, style: {}, hidden: true };
  el.classList = {
    add: function (c) { if (c === 'hidden') el.hidden = true; },
    remove: function (c) { if (c === 'hidden') el.hidden = false; },
    contains: function (c) { return c === 'hidden' ? el.hidden : false; },
  };
  el.parentElement = { clientWidth: 800,
                       getBoundingClientRect: function () { return { left: 0, top: 0, width: 800 }; } };
  return el;
}
const ELS = {
  '#weather-chart': mkCanvas('#weather-chart'),
  '#weather-history-chart': mkCanvas('#weather-history-chart'),
  '#weather-tip': mkTip(),
  '#weather-history-tip': mkTip(),
};
function $(sel) { return ELS[sel] || null; }

const pts = [
  { minute: '2026-10-01 08:00', avg_speed: 2.0, max_speed: 4.0, min_speed: 0.5, n: 5 },
  { minute: '2026-10-01 08:10', avg_speed: 3.2, max_speed: 6.1, min_speed: 0.4, n: 12 },
  { minute: '2026-10-01 08:20', avg_speed: 1.5, max_speed: 2.2, min_speed: 0.2, n: 8 },
];
const arcs = function () { return drawCalls.filter(function (c) { return c[0] === 'arc'; }); };
const out = {};
bindWindChartHover('#weather-chart', '#weather-tip');   // 挂上 mousemove / mouseleave
drawWeatherChart(pts, '#weather-chart', '暂无');
out.arcs_no_hover = arcs().length;                 // 未悬停不该有数据点
drawCalls.length = 0;
const mv = listeners['#weather-chart|mousemove'];
out.has_mousemove = !!mv;
mv({ clientX: 400 });
const a = arcs();
out.arcs_hover = a.length;                          // 悬停时应有两个数据点
out.arc_xs = a.map(function (c) { return c[1]; });
out.vline_at_same_x = drawCalls.some(function (c, i) {
  return c[0] === 'moveTo' && a.length && Math.abs(c[1] - a[0][1]) < 0.01
      && drawCalls[i + 1] && drawCalls[i + 1][0] === 'lineTo'
      && Math.abs(drawCalls[i + 1][1] - a[0][1]) < 0.01;
});
const tip = ELS['#weather-tip'];
out.tip_html = tip.innerHTML;
out.tip_hidden = tip.hidden;
out.tip_left = tip.style.left;
out.tip_top = tip.style.top;
drawCalls.length = 0;
listeners['#weather-chart|mouseleave']();
out.arcs_after_leave = arcs().length;
out.tip_hidden_after_leave = tip.hidden;
require('fs').writeFileSync(process.argv[2], JSON.stringify(out));
""",
    ])
    with tempfile.TemporaryDirectory() as td:
        jsp = os.path.join(td, 'stub.js')
        outp = os.path.join(td, 'out.json')
        Path(jsp).write_text(harness, encoding='utf-8')
        rc = 1
        outtxt = ''
        try:
            with open(outp, 'wb') as fh:
                rc = subprocess.run([node, jsp, outp], stdout=fh,
                                    stderr=subprocess.STDOUT, timeout=60).returncode
            outtxt = Path(outp).read_text(encoding='utf-8', errors='replace')
            d = json.loads(outtxt) if rc == 0 else {}
        except Exception as e:
            d = {}
            print('  桩执行异常：%s: %s' % (type(e).__name__, e))
        if not d:
            check('桩 canvas 跑通', False, 'rc=%s' % rc)
            if outtxt.strip():
                print('  --- node 输出（前 900 字）---')
                for ln in outtxt.strip()[:900].splitlines():
                    print('  ' + ln)
            else:
                dbg = Path(tempfile.gettempdir()) / 'wind_stub_debug.js'
                try:
                    dbg.write_text(harness, encoding='utf-8')
                    print('  已留档 stub：%s' % dbg)
                except Exception:
                    pass
        else:
            check('桩 canvas 跑通', True)
            check('未悬停时不画数据点（准线只在悬停时出现）', d.get('arcs_no_hover') == 0,
                  d.get('arcs_no_hover'))
            check('mousemove 已挂上', d.get('has_mousemove') is True)
            check('悬停时画出两个数据点', d.get('arcs_hover') == 2, d.get('arcs_hover'))
            xs = d.get('arc_xs') or []
            check('两个点在同一竖向位置（竖向取数）',
                  len(xs) == 2 and abs(xs[0] - xs[1]) < 0.01, xs)
            check('同一条竖线上有准线（moveTo→lineTo 同 x）', d.get('vline_at_same_x') is True)
            html = d.get('tip_html') or ''
            check('浮层已显示（去掉 hidden）', d.get('tip_hidden') is False)
            check('浮层定格在准线附近', d.get('tip_left') is not None and d.get('tip_top') is not None,
                  (d.get('tip_left'), d.get('tip_top')))
            check('浮层点名两个数据并给出数值',
                  '平均风速' in html and '最大风速' in html
                  and '3.2 m/s' in html and '6.1 m/s' in html, html)
            check('移开后不再画点且浮层隐藏',
                  d.get('arcs_after_leave') == 0 and d.get('tip_hidden_after_leave') is True,
                  (d.get('arcs_after_leave'), d.get('tip_hidden_after_leave')))

print('\n%d 通过 / %d 失败 / %d 跳过' % (OK[0], len(FAIL), len(SKIP)))
for x in FAIL:
    print('  FAIL %s' % x)
sys.exit(1 if FAIL else 0)

# -*- coding: utf-8 -*-
"""网页对讲「录音后上传」发射链自测（离线，跑在开发机/板上都行）。

为什么要有这份测试 —— 这三条播放路径原先都只写 `play_audio_async(path)`，
而该函数的 `ptt` 默认 False：于是"上传后播放"只是把音频灌进 3.5mm AUX（接的是
电台 MIC 口），电台根本没被 key。现场现象就是「操作上传后播放测试音，电台毫无反应」。

这类"漏传一个关键字参数"的缺陷有个恶劣性质：**跑起来不报错、日志也正常**，
只有真机发射才看得出来。所以这里做三件事：

  * **源码级断言**：三条端点必须经 `_intercom_play`（内部 ptt=True + 前导）走发射路径；
  * **反证**：这三条端点里**不允许**再出现裸 `play_audio_async(path)`（老写法）；
  * **真实执行** `_intercom_want_tx`（把它的源码抠出来 exec），验证 dry 的各种写法
    都能关掉发射、不写 dry 时默认发射。

真机行为（PTT 到底拉起来没有）不在这里证明，见 `verify_intercom_ptt.py`（板上跑，
会实际发射一次并轮询 /api/ptt/status）。**源码断言不能替代真机验证** —— 这正是
GNSS 那次"自造夹具自洽通过、真机全灭"的教训。
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent
BOARD = HERE.parent
APP_PY = BOARD / 'app.py'
APP_JS = BOARD / 'static' / 'js' / 'app.js'
DASH = BOARD / 'templates' / 'dashboard.html'
BASE = BOARD / 'templates' / 'base.html'
README = BOARD / 'README.md'

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


def grab_func(src, name):
    """抠出一个顶层函数（含 def 行）的源码，遇到下一个顶格行就停。"""
    out, on = [], False
    for ln in src.splitlines():
        if not on:
            if re.match(r'^def %s\(' % re.escape(name), ln):
                on = True
                out.append(ln)
            continue
        if ln and not ln[0].isspace():
            break
        out.append(ln)
    return '\n'.join(out)


APP = read(APP_PY)
JS = read(APP_JS)
DASH_HTML = read(DASH)
BASE_HTML = read(BASE)
RDM = read(README)

print('=== 1. 三条对讲播放端点必须走发射路径（这是本次修的 bug）===')
ENDPOINTS = ('api_intercom_upload', 'api_intercom_play', 'api_intercom_test_tone')
for fn in ENDPOINTS:
    body = grab_func(APP, fn)
    check('%s 存在' % fn, bool(body))
    if not body:
        continue
    check('%s 经 _intercom_play 发射' % fn, '_intercom_play(' in body)
    check('%s 用 _intercom_want_tx 判 dry' % fn, '_intercom_want_tx(' in body)
    # 反证：老写法（裸 play_audio_async(path)）必须已经消失
    check('%s 不再有裸 play_audio_async(path)' % fn,
          not re.search(r'play_audio_async\(\s*path\s*\)', body),
          re.findall(r'play_audio_async\([^)]*\)', body))

print('\n=== 2. _intercom_play 必须真的传 ptt=True 并带前导 ===')
play_helper = grab_func(APP, '_intercom_play')
check('_intercom_play 存在', bool(play_helper))
check('传 ptt=True', 'ptt=True' in play_helper)
check('带 INTERCOM_PTT_LEAD 前导', 'lead=INTERCOM_PTT_LEAD' in play_helper)
check('dry 分支只本地播放（不拉 PTT）',
      bool(re.search(r'else:\s*\n\s*play_audio_async\(path\)', play_helper)), play_helper[-120:])

print('\n=== 3. play_audio_async 支持前导，且默认行为不变（默认 0）===')
pa = grab_func(APP, 'play_audio_async')
check('签名含 lead 参数', re.search(r'def play_audio_async\(path,\s*ptt=False,\s*lead=0\.0\)', pa), pa.splitlines()[:1])
check('ptt 且 lead>0 时先睡再出声',
      bool(re.search(r'if ptt and lead > 0:\s*\n\s*time\.sleep\(lead\)\s*\n\s*proc = _play_file_locked\(path\)', pa)))
check('默认 lead=0.0（既有调用者行为不变）', 'lead=0.0' in pa)

print('\n=== 4. PTT 前导常量存在且为正 ===')
m = re.search(r"INTERCOM_PTT_LEAD = float\(os\.environ\.get\('RELAY_INTERCOM_PTT_LEAD',\s*'([0-9.]+)'\)", APP)
check('常量从 RELAY_INTERCOM_PTT_LEAD 读，默认 0.3s', bool(m), m and m.group(1))
check('默认前导 > 0（否则首字会被电台起键吃掉）', bool(m) and float(m.group(1)) > 0,
      m and m.group(1))

print('\n=== 5. 真实执行 _intercom_want_tx：dry 各种写法 ===')


class FakeArgs(dict):
    def get(self, k, d=None):
        return dict.get(self, k, d)


class FakeReq(object):
    def __init__(self, qs=None):
        self.args = FakeArgs(qs or {})


body = grab_func(APP, '_intercom_want_tx')
if not body:
    check('_intercom_want_tx 可抠出', False)
else:
    ns = {'_DRY_TRUE': ('1', 'true', 'yes', 'on'), 'request': FakeReq()}
    exec(compile(body, '<_intercom_want_tx>', 'exec'), ns)
    want = ns['_intercom_want_tx']
    check('不写 dry → 发射（默认）', want({}) is True, want({}))
    check('dry 缺省字段 → 发射', want({'other': 'x'}) is True)
    check("dry='1' → 不发射", want({'dry': '1'}) is False)
    check("dry='true' → 不发射", want({'dry': 'true'}) is False)
    check("dry='TRUE'（大写）→ 不发射", want({'dry': 'TRUE'}) is False)
    check("dry='yes' → 不发射", want({'dry': 'yes'}) is False)
    check("dry='on' → 不发射", want({'dry': 'on'}) is False)
    check("dry='0' → 仍发射", want({'dry': '0'}) is True)
    check("dry='' → 仍发射", want({'dry': ''}) is True)
    ns['request'] = FakeReq({'dry': '1'})
    check('查询串 dry=1 也生效（extra=None）', want(None) is False)
    ns['request'] = FakeReq({})
    check('查询串无 dry 时按 extra 判', want({'dry': '1'}) is False and want({}) is True)

print('\n=== 6. 返回体要如实回报（不能只说"已发送"）===')
for fn, needle in (('api_intercom_test_tone', 'tx=tx, ptt=ptt'),
                   ('api_intercom_play', 'tx=tx, ptt=ptt'),
                   ('api_intercom_upload', 'tx=tx, ptt=ptt')):
    check('%s 返回 tx/ptt' % fn, needle in grab_func(APP, fn))
check('test-tone 返回前导毫秒数', 'lead_ms=' in grab_func(APP, 'api_intercom_test_tone'))

print('\n=== 7. 前端：开关存在、默认勾选、三处都要带 dry ===')
check('dashboard.html 有 #record-tx-ptt 开关', 'id="record-tx-ptt"' in DASH_HTML)
check('开关默认勾选（checked）',
      bool(re.search(r'id="record-tx-ptt"[^>]*checked', DASH_HTML))
      or bool(re.search(r'checked[^>]*id="record-tx-ptt"', DASH_HTML)))
check('app.js 有 recordTxEnabled（取不到元素时默认发射）',
      bool(re.search(r'function recordTxEnabled\(\)\s*\{[^}]*return el \? !!el\.checked : true', JS, re.S)))
check('app.js 测试音带 dry 字段', bool(re.search(r"test-tone'[\s\S]{0,160}dry:", JS)))
check('app.js 录音上传带 dry 字段（FormData）', JS.count("fd.append('dry', '1')") >= 2,
      JS.count("fd.append('dry', '1')"))
check('app.js toast 反映 PTT 实际状态', 'recordTxToast' in JS and 'ptt.high' in JS)

print('\n=== 8. 缓存版本号必须 bump（否则浏览器还跑旧 app.js）===')
check('base.html 的 app.js 带 v= 版本参数', bool(re.search(r"js/app\.js',\s*v='[^']+'", BASE_HTML)))
check('版本号已不是改动前的 20260930o', "v='20260930o'" not in BASE_HTML)

print('\n=== 9. 文档与实现一致 ===')
for ep in ('/api/intercom/upload', '/api/intercom/play/<id>', '/api/intercom/test-tone'):
    row = [ln for ln in RDM.splitlines() if ln.startswith('| `%s`' % ep)]
    check('README 中 %s 说明含"发射"' % ep, bool(row) and '发射' in row[0], row[:1])

print('\n%d 通过 / %d 失败 / %d 跳过' % (OK[0], len(FAIL), len(SKIP)))
for x in FAIL:
    print('  FAIL %s' % x)
sys.exit(1 if FAIL else 0)

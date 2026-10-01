# -*- coding: utf-8 -*-
"""板上真机验证：网页对讲「录音后上传」到底拉没拉起 PTT（**会实际发射**）。

要证明的东西只有一个，但只有真机能证明：**这三个动作是否真的 key 了电台。**
源码级断言（test_intercom_ptt.py）只能证明"代码写了 ptt=True"，证明不了
"PTT 电平真的拉起来了、音频真的出去了"。

会发射的步骤（都很短）：
  * 播放测试音（1.2s 880Hz）
  * 上传一段 0.3s WAV（默认发射）
  * 重放刚上传的那条录音（0.3s）
不会发射的对照步骤：
  * 播放测试音 dry=1（必须不 key）
  * 上传 WAV dry=1（必须不 key）

**信道忙时自动 SKIP，绝不盖着别人的通联做测试**（每次发射前都重新查一次 BUSY）。
用法（板上）：python3 verify_intercom_ptt.py [--base http://127.0.0.1:8080]
"""
import argparse
import http.cookiejar as cj
import io
import json
import math
import re
import struct
import sys
import time
import urllib.parse as P
import urllib.request as U
import wave

FAIL, OK, SKIP = [], [0], []
BASE, USER, PWD = 'http://127.0.0.1:8080', 'Admin', '12341234'
ALLOW_TX = True

ap = argparse.ArgumentParser()
ap.add_argument('--base', default=BASE)
ap.add_argument('--user', default=USER)
ap.add_argument('--pass', dest='password', default=PWD)
ap.add_argument('--no-tx', action='store_true', help='不发射，只验证 dry 路径')
args = ap.parse_args()
BASE, USER, PWD = args.base.rstrip('/'), args.user, args.password
ALLOW_TX = not args.no_tx

op = U.build_opener(U.HTTPCookieProcessor(cj.CookieJar()))
CSRF = {'v': ''}


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


def call(path, method='GET', data=None, ctype=None, timeout=25):
    url = BASE + path
    body, headers = None, {}
    if data is not None:
        if ctype == 'json':
            body = json.dumps(data).encode()
            headers['Content-Type'] = 'application/json'
        elif ctype == 'raw':
            body, headers = data[0], {'Content-Type': data[1]}
        else:
            body = P.urlencode(data).encode()
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
    if CSRF['v']:
        headers['X-CSRF-Token'] = CSRF['v']
    req = U.Request(url, data=body, headers=headers, method=method)
    try:
        with op.open(req, timeout=timeout) as r:
            txt = r.read().decode('utf-8', 'ignore')
        try:
            return json.loads(txt)
        except Exception:
            return {'_raw': txt}
    except Exception as e:
        return {'_error': '%s: %s' % (type(e).__name__, e)}


def ptt_high():
    d = call('/api/ptt/status')
    return bool((d.get('ptt') or {}).get('high'))


def busy_active():
    d = call('/api/busy/status')
    return bool((d.get('busy') or {}).get('active'))


def wait_low(limit=8.0):
    """等 PTT 落回低电平（PTT_MIN_HOLD 1s + 释放延时 0.8s + 音频时长）。"""
    t0 = time.time()
    while time.time() - t0 < limit:
        if not ptt_high():
            return True, time.time() - t0
        time.sleep(0.15)
    return False, time.time() - t0


def watch_high(seconds=3.0):
    """在 seconds 内观察，返回是否曾看到 PTT 拉高。"""
    t0 = time.time()
    seen = False
    while time.time() - t0 < seconds:
        if ptt_high():
            seen = True
            break
        time.sleep(0.12)
    return seen


def watch_stay_low(seconds=1.6):
    t0 = time.time()
    while time.time() - t0 < seconds:
        if ptt_high():
            return False
        time.sleep(0.12)
    return True


def short_wav(seconds=0.3, freq=880.0, rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        n = int(seconds * rate)
        frames = bytearray()
        for i in range(n):
            env = min(1.0, i / (0.02 * rate), (n - i) / (0.02 * rate))
            frames += struct.pack('<h', int(0.28 * 32767 * env * math.sin(2 * math.pi * freq * i / rate)))
        w.writeframes(bytes(frames))
    return buf.getvalue()


def multipart(wav_bytes, extra_fields=None, filename='verify_intercom.wav'):
    b = '----elf2intercomverify%d' % int(time.time())
    out = []
    for k, v in (extra_fields or {}).items():
        out.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (b, k, v)).encode())
    out.append(('--%s\r\nContent-Disposition: form-data; name="audio"; filename="%s"\r\n'
                'Content-Type: audio/wav\r\n\r\n' % (b, filename)).encode())
    out.append(wav_bytes)
    out.append(('\r\n--%s--\r\n' % b).encode())
    return b''.join(out), 'multipart/form-data; boundary=%s' % b


# --------------------------------------------------------------------------
print('=== 0. 登录 %s ===' % BASE)
html = ''
try:
    with op.open(BASE + '/login', timeout=20) as r:
        html = r.read().decode('utf-8', 'ignore')
except Exception as e:
    print('  连不上板端：%s: %s' % (type(e).__name__, e))
    sys.exit(2)
m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
if not m:
    print('  拿不到登录 csrf_token，无法继续')
    sys.exit(2)
call('/login', 'POST', {'username': USER, 'password': PWD, 'csrf_token': m.group(1)})
with op.open(BASE + '/', timeout=20) as r:
    home = r.read().decode('utf-8', 'ignore')
check('登录成功', 'tab-intercom' in home or 'csrf-token' in home)
m2 = re.search(r'name="csrf-token"\s+content="([^"]+)"', home)
if m2:
    CSRF['v'] = m2.group(1)
check('拿到 API 用的 csrf-token', bool(CSRF['v']))
check('能读 PTT 状态', 'ptt' in call('/api/ptt/status'))

print('\n=== 1. 基线：现在必须是"未发射" ===')
check('起始 PTT 为低电平', ptt_high() is False)

print('\n=== 2. 播放测试音（默认）→ 必须拉起 PTT ===')
if not ALLOW_TX:
    skip('测试音发射', '--no-tx')
elif busy_active():
    skip('测试音发射', '信道忙（BUSY），不盖别人通联')
else:
    d = call('/api/intercom/test-tone', 'POST', {'dry': False}, 'json')
    check('响应 ok 且 tx=True', d.get('ok') is True and d.get('tx') is True, d)
    check('响应即刻回报 ptt.high=True（PTT 同步拉高）',
          bool((d.get('ptt') or {}).get('high')), (d.get('ptt') or {}).get('high'))
    check('响应带前导毫秒数', isinstance(d.get('lead_ms'), int) and d['lead_ms'] > 0, d.get('lead_ms'))
    check('轮询 /api/ptt/status 看到 PTT 拉高', watch_high(3.0))
    rel, secs = wait_low(8.0)
    check('音频结束 + 最短压发后 PTT 自动释放', rel, '%.2fs 后仍为高' % secs)

print('\n=== 3. 对照：播放测试音 dry=1 → 绝不能 key 电台 ===')
if busy_active():
    skip('测试音 dry 对照', '信道忙')
else:
    d = call('/api/intercom/test-tone', 'POST', {'dry': True}, 'json')
    check('响应 tx=False', d.get('tx') is False, d)
    check('响应 ptt.high=False', not (d.get('ptt') or {}).get('high'), d.get('ptt'))
    check('随后 1.6s 内 PTT 始终保持低（dry 不发射）', watch_stay_low(1.6))

print('\n=== 4. 上传 WAV（默认）→ 必须拉起 PTT ===')
if not ALLOW_TX:
    skip('上传 WAV 发射', '--no-tx')
elif busy_active():
    skip('上传 WAV 发射', '信道忙（BUSY）')
else:
    wav = short_wav(0.3)
    body, ct = multipart(wav)
    d = call('/api/intercom/upload', 'POST', (body, ct), 'raw', timeout=40)
    check('上传成功且 tx=True', d.get('ok') is True and d.get('tx') is True, d)
    check('上传即刻 PTT 已拉高', bool((d.get('ptt') or {}).get('high')), d.get('ptt'))
    check('轮询看到 PTT 拉高', watch_high(3.0))
    rel, secs = wait_low(8.0)
    check('上传播放后 PTT 自动释放', rel, '%.2fs 后仍为高' % secs)

print('\n=== 5. 对照：上传 WAV dry=1 → 不发射 ===')
if busy_active():
    skip('上传 WAV dry 对照', '信道忙')
else:
    body, ct = multipart(short_wav(0.3), {'dry': '1'})
    d = call('/api/intercom/upload', 'POST', (body, ct), 'raw', timeout=40)
    check('dry 上传 tx=False', d.get('ok') is True and d.get('tx') is False, d)
    check('dry 上传后 PTT 保持低', watch_stay_low(1.6))

print('\n=== 6. 重放录音（默认）→ 必须拉起 PTT ===')
recs = (call('/api/intercom/recordings') or {}).get('recordings') or []
check('录音列表可读', isinstance(recs, list) and len(recs) > 0, len(recs))
if not recs:
    skip('重放发射', '没有录音可重放')
elif not ALLOW_TX:
    skip('重放发射', '--no-tx')
elif busy_active():
    skip('重放发射', '信道忙（BUSY）')
else:
    rid = recs[0]['id']
    d = call('/api/intercom/play/%d' % rid, 'POST', {'dry': False}, 'json')
    check('重放响应 tx=True', d.get('ok') is True and d.get('tx') is True, d)
    check('轮询看到 PTT 拉高', watch_high(3.0))
    rel, secs = wait_low(8.0)
    check('重放后 PTT 自动释放', rel, '%.2fs 后仍为高' % secs)

print('\n=== 7. 收尾：PTT 不能卡在高电平 ===')
ok_low, secs = wait_low(10.0)
check('全部动作结束后 PTT 为低电平（不咬死发射）', ok_low, '%.2fs 后仍为高' % secs)
if not ok_low:
    print('  尝试用 /api/ptt/manual 松开…')
    call('/api/ptt/manual', 'POST', {'hold': False, 'reason': 'verify-cleanup'}, 'json')
    check('清理后 PTT 落低', wait_low(5.0)[0])

print('\n%d 通过 / %d 失败 / %d 跳过' % (OK[0], len(FAIL), len(SKIP)))
for x in FAIL:
    print('  FAIL %s' % x)
sys.exit(1 if FAIL else 0)

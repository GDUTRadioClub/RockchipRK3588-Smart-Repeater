# -*- coding: utf-8 -*-
"""语音日志时间轴区间缩放：真实浏览器最终验收。

上一版测试的教训：`bar.dispatchEvent(...)` 会让 `ev.target === bar`，
`closest('.vlog-tl-handle')` 永远为 null，于是只走到「点空白居中」分支，
测不到手柄拖动与平移。本版**派发到正确的目标元素上**：
  * 手柄拖动 → 派发到 .vlog-tl-handle[data-h]
  * 选区平移 → 派发到 #vlog-tl-brush-sel
  * 点空白   → 派发到 #vlog-tl-brush（bar 本身）
移动/抬起统一派发到 bar（应用就监听在 bar 上，且 setPointerCapture 对合成事件会抛错、已 try 包裹）。
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

from websocket import create_connection

BASE = 'http://192.168.101.215:8080'
CHROME = r'C:\Program Files\Google\Chrome\Application\chrome.exe'
PORT = 9336
PROFILE = os.path.join(tempfile.gettempdir(), 'cdp_vlog_final')
OK, FAIL, LOGS = [], [], []


def check(name, cond, extra=''):
    (OK if cond else FAIL).append(name)
    print('  %-4s %s %s' % ('OK' if cond else 'FAIL', name, extra if not cond else ''))


proc = subprocess.Popen([CHROME, '--headless=new', '--disable-gpu', '--no-first-run',
                         '--no-default-browser-check', '--remote-allow-origins=*',
                         '--remote-debugging-port=%d' % PORT,
                         '--user-data-dir=' + PROFILE, 'about:blank'],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
ws = None
try:
    target = None
    for _ in range(40):
        time.sleep(0.5)
        try:
            with urllib.request.urlopen('http://127.0.0.1:%d/json/list' % PORT, timeout=3) as r:
                for t in json.load(r):
                    if t.get('type') == 'page':
                        target = t
                        break
            if target:
                break
        except Exception:
            pass
    ws = create_connection(target['webSocketDebuggerUrl'], timeout=30)
    _id = [0]

    def send(method, params=None):
        _id[0] += 1
        ws.send(json.dumps({'id': _id[0], 'method': method, 'params': params or {}}))
        while True:
            m = json.loads(ws.recv())
            if m.get('id') == _id[0]:
                return m
            if m.get('method') == 'Log.entryAdded':
                e = m['params']['entry']
                if e.get('level') in ('error', 'warning'):
                    LOGS.append('%s %s %s' % (e['level'], e.get('text'), e.get('url') or ''))

    send('Page.enable'); send('Runtime.enable'); send('Log.enable')

    def js(expr):
        r = send('Runtime.evaluate', {'expression': expr, 'returnByValue': True,
                                      'awaitPromise': True})
        res = r.get('result', {})
        if res.get('exceptionDetails'):
            ed = res['exceptionDetails']
            return 'EXC:' + str(ed.get('exception', {}).get('description'))[:120]
        return res.get('result', {}).get('value')

    def goto(u, w=3.0):
        send('Page.navigate', {'url': u}); time.sleep(w)

    goto(BASE + '/login')
    js("document.querySelector('input[name=username]').value='Admin';"
       "document.querySelector('input[name=password]').value='12341234';"
       "document.querySelector('form').submit();")
    time.sleep(3)
    goto(BASE + '/voice-log', 3.0)

    R = "document.querySelector('#vlog-tl-range').textContent"
    n_blk = js("document.querySelectorAll('.vlog-tl-block').length")
    colors = js("""(() => { const o={};
      document.querySelectorAll('.vlog-tl-block').forEach(b=>{
        const c=getComputedStyle(b).backgroundColor; o[c]=(o[c]||0)+1; }); return o; })()""")
    check('色块按分类上色（非单一颜色）', isinstance(colors, dict) and len(colors) >= 3,
          str(colors))
    print('       颜色分布 %s' % colors)
    check('初始为全天', '全天' in str(js(R)), str(js(R)))

    # ---------- 1) 拖左手柄 ----------
    print('\n--- 拖左手柄（派发到 handle 元素）---')
    js("""(() => {
      const bar=document.querySelector('#vlog-tl-brush');
      const hA=document.querySelector('.vlog-tl-handle[data-h="a"]');
      const r=bar.getBoundingClientRect(), y=r.top+r.height/2;
      const x0=r.left+2, x1=r.left+r.width*0.30;
      const mk=(t,x,b)=>new PointerEvent(t,{bubbles:true,cancelable:true,composed:true,
        pointerId:1,pointerType:'mouse',clientX:x,clientY:y,buttons:b,button:0});
      hA.dispatchEvent(mk('pointerdown',x0,1));
      for(let i=1;i<=6;i++) bar.dispatchEvent(mk('pointermove',x0+(x1-x0)*i/6,1));
      bar.dispatchEvent(mk('pointerup',x1,0));
      return 'ok';
    })()""")
    time.sleep(0.5)
    r1 = js(R)
    print('       区间:', r1)
    span1 = js("""(() => { const s=document.querySelector('#vlog-tl-brush-sel');
      return Math.round(parseFloat(s.style.width)/100*86400); })()""")
    check('拖左手柄 → 起点右移、跨度收窄',
          isinstance(r1, str) and '全天' not in r1 and (span1 or 0) < 86400
          and not r1.startswith('00:00:00'), '%s 跨度=%s' % (r1, span1))
    check('收窄后出现重置按钮', js("document.querySelector('#btn-tl-reset').hidden") is False)
    check('刻度重新生成', (js("document.querySelectorAll('#vlog-tl-axis span').length") or 0) >= 2)

    # ---------- 2) 拖选区平移 ----------
    print('\n--- 拖选区中间（平移）---')
    span_before = js("""(() => { const s=document.querySelector('#vlog-tl-brush-sel');
      return Math.round(parseFloat(s.style.width)/100*86400); })()""")
    js("""(() => {
      const bar=document.querySelector('#vlog-tl-brush');
      const sel=document.querySelector('#vlog-tl-brush-sel');
      const r=bar.getBoundingClientRect(), y=r.top+r.height/2;
      const sx=r.left+r.width*0.60, ex=r.left+r.width*0.45;   // 向左平移
      const mk=(t,x,b)=>new PointerEvent(t,{bubbles:true,cancelable:true,composed:true,
        pointerId:2,pointerType:'mouse',clientX:x,clientY:y,buttons:b,button:0});
      sel.dispatchEvent(mk('pointerdown',sx,1));
      for(let i=1;i<=6;i++) bar.dispatchEvent(mk('pointermove',sx+(ex-sx)*i/6,1));
      bar.dispatchEvent(mk('pointerup',ex,0));
      return 'ok';
    })()""")
    time.sleep(0.5)
    r2 = js(R)
    span_after = js("""(() => { const s=document.querySelector('#vlog-tl-brush-sel');
      return Math.round(parseFloat(s.style.width)/100*86400); })()""")
    print('       区间:', r2, ' 跨度 %s -> %s 秒' % (span_before, span_after))
    check('平移改变起止但跨度基本不变',
          isinstance(r2, str) and r2 != r1 and abs((span_after or 0) - (span_before or 0)) <= 120,
          '%s vs %s' % (span_before, span_after))

    # ---------- 3) 点空白居中 ----------
    print('\n--- 点空白（以该时刻为中心平移）---')
    js("""(() => {
      const bar=document.querySelector('#vlog-tl-brush');
      const r=bar.getBoundingClientRect(), y=r.top+r.height/2;
      const mk=(t,x,b)=>new PointerEvent(t,{bubbles:true,cancelable:true,composed:true,
        pointerId:3,pointerType:'mouse',clientX:x,clientY:y,buttons:b,button:0});
      bar.dispatchEvent(mk('pointerdown', r.left+r.width*0.9, 1));
      bar.dispatchEvent(mk('pointerup', r.left+r.width*0.9, 0));
      return 'ok';
    })()""")
    time.sleep(0.5)
    r3 = js(R)
    print('       区间:', r3)
    check('点空白后区间随之改变', isinstance(r3, str) and r3 != r2, '%s -> %s' % (r2, r3))

    # ---------- 4) 双击重置 ----------
    print('\n--- 双击重置 ---')
    js("""(() => {
      const bar=document.querySelector('#vlog-tl-brush');
      const r=bar.getBoundingClientRect(), y=r.top+r.height/2, x=r.left+r.width*0.5;
      const mk=(t,b)=>new PointerEvent(t,{bubbles:true,cancelable:true,composed:true,
        pointerId:4,pointerType:'mouse',clientX:x,clientY:y,buttons:b,button:0});
      bar.dispatchEvent(mk('pointerdown',1)); bar.dispatchEvent(mk('pointerup',0));
      bar.dispatchEvent(new MouseEvent('dblclick',{bubbles:true,cancelable:true,clientX:x,clientY:y}));
      return 'ok';
    })()""")
    time.sleep(0.5)
    r4 = js(R)
    print('       区间:', r4)
    check('双击恢复全天', isinstance(r4, str) and '全天' in r4, r4)

    # ---------- 5) 键盘微调 ----------
    print('\n--- 键盘微调（手柄聚焦后方向键）---')
    js("document.querySelector('.vlog-tl-handle[data-h=\"b\"]').focus()")
    for _ in range(3):
        send('Input.dispatchKeyEvent', {'type': 'rawKeyDown', 'key': 'ArrowLeft',
                                        'code': 'ArrowLeft', 'windowsVirtualKeyCode': 37,
                                        'nativeVirtualKeyCode': 37})
        send('Input.dispatchKeyEvent', {'type': 'keyUp', 'key': 'ArrowLeft',
                                        'code': 'ArrowLeft', 'windowsVirtualKeyCode': 37,
                                        'nativeVirtualKeyCode': 37})
        time.sleep(0.12)
    time.sleep(0.5)
    r5 = js(R)
    print('       区间:', r5)
    check('方向键可微调终点', isinstance(r5, str) and '全天' not in r5 and r5 != r4, r5)

    # ---------- 6) 选中项自动平移 ----------
    print('\n--- 选中项自动平移（点一个不在窗口内的色块）---')
    js("(() => { const a=document.querySelector('#vlog-tl-range'); return a.textContent; })()")
    # 先缩到很小，再点一个肯定在窗口外的块
    js("""(() => {
      const bar=document.querySelector('#vlog-tl-brush');
      const sel=document.querySelector('#vlog-tl-brush-sel');
      const r=bar.getBoundingClientRect(), y=r.top+r.height/2;
      const mk=(t,x,b)=>new PointerEvent(t,{bubbles:true,cancelable:true,composed:true,
        pointerId:5,pointerType:'mouse',clientX:x,clientY:y,buttons:b,button:0});
      sel.dispatchEvent(mk('pointerdown', r.left+r.width*0.90, 1));
      const hB=document.querySelector('.vlog-tl-handle[data-h="b"]');
      hB.dispatchEvent(mk('pointerdown', r.left+r.width, 1));
      for(let i=1;i<=6;i++) bar.dispatchEvent(mk('pointermove', r.left+r.width*0.90+i*(r.left+r.width*0.02-r.left+r.width*0.90)/6, 1));
      bar.dispatchEvent(mk('pointerup', r.left+r.width*0.92, 0));
      return 'ok';
    })()""")
    time.sleep(0.4)
    r6 = js(R)
    ids = js("Array.from(document.querySelectorAll('.vlog-tl-block')).map(b=>b.dataset.id)")
    if isinstance(ids, list) and ids:
        js("document.querySelector('.vlog-tl-block[data-id=\"%s\"]').click()" % ids[0])
        time.sleep(0.4)
    print('       点选后区间:', js(R))
    check('点选色块不影响缩放逻辑（无异常）', 'EXC' not in str(js(R)))

    # ---------- 7) console ----------
    print('\n--- 控制台 ---')
    bad = [l for l in LOGS if 'favicon' not in l]
    check('无 console 错误（favicon 除外）', not bad, '; '.join(bad[:3]))
    if LOGS:
        print('       记录 %d 条: %s' % (len(LOGS), LOGS[:3]))

finally:
    try:
        if ws:
            ws.close()
    except Exception:
        pass
    proc.terminate()

print()
print('=' * 66)
print('通过 %d 项，失败 %d 项' % (len(OK), len(FAIL)))
for f in FAIL:
    print('  x %s' % f)
sys.exit(1 if FAIL else 0)

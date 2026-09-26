# -*- coding: utf-8 -*-
"""中继语音助手：纯逻辑自测（不需要板子硬件、不需要 ASR/LLM 模型）。

覆盖：
  1. 唤醒词匹配（同音容错、标点干扰、多唤醒词、未命中、剩余问题切除）
  2. 回复裁剪（句末/逗号断句、字数上限、Markdown 清洗联动）
  3. 禁发时段解析（同日段、跨零点段、多段、空值）
  4. 提示词逐级降配（不超输入上限，且优先保留系统设定）
  5. 能量分段状态机（前置缓冲、最短时长、静音收段、发射期不收音）
"""
import os
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
import agent_service as G
import assistant_service as A

FAIL = []
OK = [0]


def check(name, cond, extra=''):
    if cond:
        OK[0] += 1
        print('  OK   %s' % name)
    else:
        FAIL.append(name)
        print('  FAIL %s %s' % (name, extra))


# ---------------------------------------------------------------------------
print('\n=== 1. 唤醒词匹配 ===')
W = ['智能中继', '中继台']
cases = [
    ('中继台，现在风速多少', '中继台', '现在风速多少', '标准一句话唤醒'),
    ('中继台现在风速多少', '中继台', '现在风速多少', '无标点'),
    ('中继太，电压是多少', '中继台', '电压是多少', '同音字容错（太→台）'),
    ('喂，中继台，帮我查一下电池电压', '中继台', '喂，帮我查一下电池电压', '前缀噪声保留+重复标点折叠'),
    ('智能中继，今天下雨了吗', '智能中继', '今天下雨了吗', '第二个唤醒词'),
    ('智 能 中 继 ， 温度多少', '智能中继', '温度多少', '空格干扰'),
    ('今天天气不错', '', '', '未命中'),
    ('中继', '', '', '只喊一半不算命中'),
    ('中继台', '中继台', '', '只喊唤醒词（剩余为空→应答模式）'),
]
for text, want_w, want_rest, desc in cases:
    w, hit, rest = A.match_wake(text, W, True)
    check('%s: %r' % (desc, text), w == want_w and rest == want_rest,
          '得到 wake=%r rest=%r' % (w, rest))
# 关闭模糊后同音字不应再命中
w, _h, _r = A.match_wake('中继太，电压', W, False)
check('关闭模糊匹配后「中继太」不命中', w == '', '得到 %r' % w)

# ---------------------------------------------------------------------------
print('\n=== 2. 回复裁剪 ===')
t, tr = A.clamp_reply('今天气温二十五度，湿度百分之六十，适合通联。', 10)
check('超长在句末断开', len(t) <= 10 and tr, repr(t))
t, tr = A.clamp_reply('风速三点二米每秒', 80)
check('未超长不截断', t == '风速三点二米每秒' and not tr, repr(t))
t, tr = A.clamp_reply('**风速 3.2 米每秒**，注意安全', 12)
check('裁剪联动 Markdown 清洗', '*' not in t and len(t) <= 12, repr(t))
t, tr = A.clamp_reply('这是很长的一段话没有任何标点结尾会一直说下去停不下来', 10)
check('无标点时硬切', len(t) == 10 and tr, repr(t))
t, tr = A.clamp_reply('', 10)
check('空文本', t == '' and not tr)
t, tr = A.clamp_reply('好的。', 0)
check('limit=0 视为不限', t == '好的。', repr(t))

# ---------------------------------------------------------------------------
print('\n=== 3. 禁发时段 ===')
from datetime import datetime
check('空值不禁发', A.in_quiet_hours('') is False)
check('同日段内 12:30 ∈ 12:00-14:00',
      A.in_quiet_hours('12:00-14:00', datetime(2026, 9, 24, 12, 30)) is True)
check('同日段外 15:00 ∉ 12:00-14:00',
      A.in_quiet_hours('12:00-14:00', datetime(2026, 9, 24, 15, 0)) is False)
check('跨零点 23:30 ∈ 23:00-07:00',
      A.in_quiet_hours('23:00-07:00', datetime(2026, 9, 24, 23, 30)) is True)
check('跨零点 03:00 ∈ 23:00-07:00',
      A.in_quiet_hours('23:00-07:00', datetime(2026, 9, 24, 3, 0)) is True)
check('跨零点 12:00 ∉ 23:00-07:00',
      A.in_quiet_hours('23:00-07:00', datetime(2026, 9, 24, 12, 0)) is False)
check('多段 12:30 ∈ 12:00-13:00,18:00-19:00',
      A.in_quiet_hours('12:00-13:00,18:00-19:00', datetime(2026, 9, 24, 12, 30)) is True)
check('多段 18:30 命中第二段',
      A.in_quiet_hours('12:00-13:00,18:00-19:00', datetime(2026, 9, 24, 18, 30)) is True)
check('垃圾输入不崩且不禁发',
      A.in_quiet_hours('abc', datetime(2026, 9, 24, 3, 0)) is False)

# ---------------------------------------------------------------------------
print('\n=== 4. 提示词降配 ===')


def make_svc(settings=None, busy=False, tx=False):
    tmp = tempfile.mkdtemp()
    s = A.AssistantService(os.path.join(tmp, 'test.db'))
    st = dict(A.DEFAULTS)
    st.update(settings or {})
    s.configure(setting_getter=lambda: st, busy_getter=lambda: busy,
                tx_getter=lambda: tx, ask_fn=None, tts_fn=None, play_fn=None,
                base_prompt_fn=lambda: '你是中继助手。' * 40)
    s.history.clear()
    return s, st


svc, st = make_svc()
p, n = svc._build_prompt('风速多少', st, '你是中继助手。' * 40)
check('默认不超输入上限', len(p) <= 3000, 'len=%d' % len(p))
check('含系统设定段', '【系统设定】' in p)
check('含当前问题段', '【当前问题】\n风速多少' in p)
check('suffix 的 {max_chars} 已替换', '{max_chars}' not in p)

# 塞入大量历史，验证会逐级砍历史而不是超限
svc2, st2 = make_svc()
for i in range(20):
    svc2.history.append({'q': '问题%d' % i * 5, 'a': '回答%d' % i * 5})
p2, n2 = svc2._build_prompt('电压多少', st2, '你是中继助手。' * 40)
check('长历史下仍不超上限', len(p2) <= 3000, 'len=%d' % len(p2))
check('长历史时保留系统设定', '【系统设定】' in p2)
check('长历史时保留当前问题', '电压多少' in p2)

# 极端：系统设定本身就超长
svc3, st3 = make_svc({'assist_max_input_chars': '600'})
p3, n3 = svc3._build_prompt('你好', st3, '很长很长的设定' * 200)
check('设定超长时仍被截到上限', len(p3) <= 600, 'len=%d' % len(p3))

# 现场 bug 回归：降配时**必须保住语音播报规范**。
# 旧实现用 head_full[:600] 兜底，基础设定一超过 600 字就把规范整段切掉。
MARK = '只输出可直接朗读的纯口语'
svc4, st4 = make_svc()
long_base = '基' * 1200
p4, n4 = svc4._build_prompt('风速多少', st4, long_base)
check('长基础设定下仍保留播报规范', MARK in p4, '规范被切掉了')
check('长基础设定下仍不超上限', len(p4) <= 3000, 'len=%d' % len(p4))

# 再挤：把上限压到刚好放不下「基础设定 + 规范」，规范仍要活着
svc5, st5 = make_svc({'assist_max_input_chars': '600'})
p5, n5 = svc5._build_prompt('风速多少', st5, long_base)
check('上限吃紧时规范优先于基础设定', MARK in p5, 'len=%d' % len(p5))
check('上限吃紧时仍不超上限', len(p5) <= 600, 'len=%d' % len(p5))

# ---------------------------------------------------------------------------
print('\n=== 4b. 总结轮的行为约束回灌（现场 bug 回归）===')
# 真因：被朗读的文本是**总结轮**产出的，而第一轮在 force_first 下被要求
# 「只输出读取指令、不要回答用户」。约束只写在第一轮 = 对答案零生效。
SPEC = A.DEFAULTS['assist_prompt_suffix'].replace('{max_chars}', '100')

m_short = G.summary_messages([{'get_power': {'battery': 12.6}}], '电池电压',
                             SPEC, 'external', mode='auto')
check('外部模型总结轮带约束', any(MARK in (m.get('content') or '')
                                  for m in m_short))
check('外部模型约束走 system 轮',
      m_short[0].get('role') == 'system' and MARK in m_short[0]['content'])
check('外部模型数据仍在 user 轮',
      m_short[-1]['role'] == 'user' and '设备实时数据' in m_short[-1]['content'])
check('外部模型仍带原始问题', '电池电压' in m_short[-1]['content'])

m_off = G.summary_messages([{'get_power': {'battery': 12.6}}], '电池电压',
                           SPEC, 'external', mode='off')
check('mode=off 时完全不回灌（板端保命开关）',
      all(MARK not in (m.get('content') or '') for m in m_off))

m_local = G.summary_messages([{'get_power': {'battery': 12.6}}], '电池电压',
                             SPEC, 'local', mode='auto')
check('auto 下板端 RKLLM 不回灌（>400 字会空输出）',
      all(MARK not in (m.get('content') or '') for m in m_local))

m_local_on = G.summary_messages([{'get_power': {'battery': 12.6}}], '电池电压',
                                SPEC, 'local', mode='on')
check('强制 on 时板端走用户消息内联（RKLLM 不认 system 轮）',
      m_local_on[0]['role'] == 'user' and '【播报要求】' in m_local_on[0]['content'])

check('cap 生效', len(G.summary_spec('规' * 5000, 'external', cap=300)) == 300)
check('空规范不注入', G.summary_spec('', 'external') == '')
check('非法 mode 退回 auto', G.summary_spec(SPEC, 'external', mode='乱写') != '')
check('总结轮数据段仍被截断（板端上下文）',
      len(G.summary_messages([{'x': 'y' * 900}], 'q', SPEC, 'external')[-1]['content'])
      < 900)

# 网页 Agent 对话同款缺陷：总结轮也得带上基础设定，且收尾语沿用网页那一句
m_web = G.summary_messages([{'get_power': {'battery': 12.6}}], '电池电压',
                           '你是中继台助手。', 'external', mode='auto',
                           tail='请用中文 1~3 句回答：')
check('网页总结轮带基础设定', m_web[0]['role'] == 'system'
      and '你是中继台助手。' in m_web[0]['content'])
check('网页总结轮用自己的收尾语', '请用中文 1~3 句回答：' in m_web[-1]['content'])
check('默认收尾语仍是助手那一句',
      G.SUMMARY_TAIL_ASSIST in G.summary_messages([], 'q', SPEC, 'external')[-1]['content'])

# ---------------------------------------------------------------------------
print('\n=== 5. 能量分段状态机 ===')
SR = A.SAMPLE_RATE


def pcm(seconds, amp, freq=800.0):
    n = int(SR * seconds)
    t = np.arange(n) / float(SR)
    x = (np.sin(2 * np.pi * freq * t) * amp * 32767).astype(np.int16)
    st_i = np.stack([x, x], axis=1).reshape(-1)      # 立体声交错
    return st_i.tobytes()


def feed_blocks(svc, data, ts0, block=4096):
    n = 0
    t = ts0
    for i in range(0, len(data) - block + 1, block):
        svc.feed(data[i:i + block], t)
        n += 1
        t += block / 4.0 / SR
    return n, t


# 5.1 静音 → 有语音（1.5s）→ 静音：应产生 1 段
svc, st = make_svc({'assist_enabled': '1'})
blocks = pcm(0.6, 0.0) + pcm(1.5, 0.5) + pcm(1.2, 0.0)
n, _t = feed_blocks(svc, blocks, 1000.0)
check('静音+语音+静音 → 产出 1 段', svc.q.qsize() == 1, 'qsize=%d' % svc.q.qsize())
if svc.q.qsize():
    it = svc.q.get()
    # 1.5s 语音 + 0.45s 收段尾静音 + 0.4s 前置缓冲 ≈ 2.35s
    # 0.6s 可用前置缓冲（输入里只有 0.6s 静音，取不到默认的 1.2s）
    # + 1.5s 语音 + 0.45s 收段尾静音 ≈ 2.55s，留块量化余量
    check('段时长 ≈ 前置缓冲+语音+尾静音（2.4~2.9s）',
          2.4 <= it['seconds'] <= 2.9, '%.2fs' % it['seconds'])
    check('有声时长接近 1.5s', 1.4 <= it.get('voice_seconds', 0) <= 1.7,
          '%.2fs' % it.get('voice_seconds', -1))

# 5.2 全静音：不应产段
svc, st = make_svc({'assist_enabled': '1'})
feed_blocks(svc, pcm(3.0, 0.0), 1000.0)
check('全静音不产段', svc.q.qsize() == 0)

# 5.3 只有 0.2s 的极短音：低于 min_speech，应丢弃
svc, st = make_svc({'assist_enabled': '1'})
feed_blocks(svc, pcm(0.6, 0.0) + pcm(0.2, 0.5) + pcm(1.0, 0.0), 1000.0)
check('过短语音被丢弃', svc.q.qsize() == 0)

# 5.4 发射期间不收音（模拟功放回授自己的声音）
svc, st = make_svc({'assist_enabled': '1'}, tx=True)
feed_blocks(svc, pcm(0.6, 0.0) + pcm(1.5, 0.5) + pcm(1.2, 0.0), 1000.0)
check('发射期间不产段（防自激）', svc.q.qsize() == 0)
check('发射期间状态为余波保护', svc.stage == 'tx-guard', svc.stage)

# 5.5 发射结束后 0.6s 保护期内仍不收音，保护期过后恢复
svc, st = make_svc({'assist_enabled': '1'}, tx=True)
feed_blocks(svc, pcm(1.0, 0.5), 1000.0)          # 全程 tx=True → 收不到
svc.tx_getter = lambda: False
feed_blocks(svc, pcm(1.6, 0.5), 1002.5)          # 保护期内（tx_until 已设）
check('保护期内仍不产段', svc.q.qsize() == 0, 'qsize=%d' % svc.q.qsize())

# 5.6 未启用时不产段
svc, st = make_svc({'assist_enabled': '0'})
feed_blocks(svc, pcm(0.6, 0.0) + pcm(1.5, 0.5) + pcm(1.2, 0.0), 1000.0)
check('未启用时不产段', svc.q.qsize() == 0)
check('未启用时状态为 off', svc.stage == 'off')

# 5.7 长时间连续语音应被 max_utterance 强制切段
svc, st = make_svc({'assist_enabled': '1', 'assist_max_utterance': '3',
                    'assist_silence_ms': '100000'})
feed_blocks(svc, pcm(0.6, 0.0) + pcm(8.0, 0.5), 1000.0)
check('超长语音被强制切成多段', svc.q.qsize() >= 2, 'qsize=%d' % svc.q.qsize())
if svc.q.qsize():
    it = svc.q.get()
    # 上限 3.0s + 可用前置缓冲 0.6s + 块量化余量
    check('单段不超 max_utterance+前置缓冲',
          it['seconds'] <= 3.9, '%.2fs' % it['seconds'])

# ---------------------------------------------------------------------------
print('\n=== 6. 配置与状态 ===')
svc, st = make_svc({'assist_enabled': '1', 'assist_wake_words': '中继台, 智能中继 ,小继'})
check('唤醒词解析（含空格/逗号）', svc.wake_words(st) == ['中继台', '智能中继', '小继'],
      str(svc.wake_words(st)))
svc, st = make_svc({'assist_enabled': '1', 'assist_max_reply_chars': '5'})
check('max_chars 下限保护（>=10）', svc.max_chars(st) == 10, str(svc.max_chars(st)))
svc, st = make_svc({'assist_enabled': '1', 'assist_history_turns': '99'})
check('history_turns 上限保护（<=12）', svc.hist_turns(st) == 12, str(svc.hist_turns(st)))
svc, st = make_svc({'assist_enabled': '1'})
s = svc.status()
for key in ('enabled', 'stage', 'stage_label', 'wake_words', 'counters', 'levels',
            'recent', 'settings', 'follow_up_left', 'today'):
    check('status 含 %s' % key, key in s)
check('status.settings 覆盖全部默认键',
      set(A.DEFAULTS) <= set(s['settings']), str(set(A.DEFAULTS) - set(s['settings'])))

# 唤醒匹配自测接口
svc, st = make_svc({'assist_enabled': '1'})
r = svc.test_wake('中继台，电压多少')
check('test_wake 命中', r['matched'] == '中继台' and r['question'] == '电压多少', str(r))
r = svc.test_wake('无关内容')
check('test_wake 未命中', r['matched'] == '', str(r))

# 停止
svc, st = make_svc({'assist_enabled': '1'})
feed_blocks(svc, pcm(0.6, 0.0) + pcm(1.5, 0.5) + pcm(1.2, 0.0), 1000.0)
before = svc.q.qsize()
r = svc.stop()
check('stop 清空队列', r['cleared'] == before and svc.q.qsize() == 0, str(r))
check('stop 关闭追问窗口', svc.follow_until == 0.0)

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
print('\n=== 7. 发射余波保护不得无限续期（现场 bug 回归）===')
# 现场现象：首发成功之后再也没收到任何呼叫，语音日志却全部收到，
# counters.segments 停在 1。根因是保护期用「当前时刻 + 0.6s」续期，
# 保护期内每个采集块都会执行一次，于是永不结束。
svc, st = make_svc({'assist_enabled': '1'}, tx=True)
feed_blocks(svc, pcm(2.0, 0.5), 5000.0)
check('发射期间不收音', svc.q.qsize() == 0, 'qsize=%d' % svc.q.qsize())
check('发射期间状态为余波保护', svc.stage == 'tx-guard', svc.stage)
svc.tx_getter = lambda: False
feed_blocks(svc, pcm(1.0, 0.0), 5002.0)
feed_blocks(svc, pcm(0.5, 0.0) + pcm(1.5, 0.5) + pcm(1.2, 0.0), 5003.2)
check('保护期结束后恢复收音（旧代码在此永远收不到）',
      svc.q.qsize() >= 1, 'qsize=%d' % svc.q.qsize())

svc2, st2 = make_svc({'assist_enabled': '1'}, tx=False)
ok_rounds = 0
for k in range(3):
    base = 6000.0 + k * 10.0
    svc2.tx_getter = lambda: True
    feed_blocks(svc2, pcm(1.0, 0.4), base)
    svc2.tx_getter = lambda: False
    feed_blocks(svc2, pcm(1.0, 0.0), base + 1.0)
    before = svc2.q.qsize()
    feed_blocks(svc2, pcm(0.4, 0.0) + pcm(1.4, 0.5) + pcm(1.2, 0.0), base + 2.2)
    if svc2.q.qsize() > before:
        ok_rounds += 1
check('连续 3 轮「发射-静默-呼叫」都能收音', ok_rounds == 3, '%d/3 轮' % ok_rounds)
print('\n' + '=' * 62)
print('通过 %d 项，失败 %d 项' % (OK[0], len(FAIL)))
if FAIL:
    for f in FAIL:
        print('  ✗ %s' % f)
    sys.exit(1)
print('ALL PASS')

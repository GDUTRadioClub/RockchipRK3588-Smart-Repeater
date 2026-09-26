# -*- coding: utf-8 -*-
"""中继语音助手：BUSY 语音唤醒 → ASR → 本地 LLM → TTS → 受控发射。

架构位置
--------
nau8822 采集设备**独占**，全板只有 app.py 的 `_mic_capture_loop` 一路 arecord。
本模块作为该采集中枢的第三个消费者挂上去（前两个是 voice_service 语音日志、
aprs_service TNC），与它们共用同一份原始立体声块：

    _mic_capture_loop →┬→ voice_service.feed()   语音日志
                       ├→ aprs_service.feed()    APRS TNC
                       └→ assistant_service.feed() ← 本模块

为什么不自带第二路 arecord：设备独占，第二路会直接 `Device or resource busy`。

为什么不直接复用语音日志的 ASR 结果
-----------------------------------
ASR 模型确实是同一份（`asr_service.ENGINE` 单例 + 解码锁，**没有**重复加载），
但唤醒对**时机**的要求和归档完全不同，直接挂 vlog 结果会踩两个坑：

  1. 归档 `vlog_post_roll=2.0s` 才收段，唤醒会白白多等 2 秒；
  2. 归档 `vlog_min_seconds=1.0`，短于 1 秒的「中继台」会被判成 jitter，且
     `transcribe_one` 里 `segs` 为空导致**根本不跑 ASR**——单独喊唤醒词永远唤不醒。

因此本模块自带一套轻量分段（能量迟滞 + 静音判尾 + 前置缓冲），共用
`voice_service.Vad` 与 `asr_service` 做边界校正与识别，既能 0.45s 收段，
也不受语音日志开关与门槛影响。

安全红线（全部对应可调设置项）
----------------------------
  * BUSY 有效时**绝不**发射，最多等 `assist_busy_wait_seconds` 秒后放弃本次回答；
  * 与上次发射间隔不足 `assist_min_gap_seconds` 时强制等待；
  * 单条回答硬上限 `assist_max_tx_seconds` 秒，超时由 app.py 侧 kill aplay；
  * 自己发射期间 `assist_tx_guard_ms` 内不收音，防止自激；
  * 可选禁发时段 `assist_quiet_hours`；
  * 网页可一键停止，`stop()` 会立刻打断播放并清空待处理队列。
"""
import json
import math
import inspect
import queue
import re
import sqlite3
import threading
import time
import wave
from collections import deque
from datetime import datetime
from pathlib import Path

import numpy as np

LOG = '[ASSIST]'

SAMPLE_RATE = 16000
FRAME_BYTES = 2                       # 16bit 单声道
WORK_DIR = Path('/opt/ai/relay_assist')

# ---------------------------------------------------------------------------
# 默认设置（全部可在「中继语音助手」页面在线修改，即改即生效）
# ---------------------------------------------------------------------------
DEFAULT_SUFFIX = (
    '你是中继台的语音助手，回复会被合成语音后发射出去。\n'
    '只输出可直接朗读的纯口语，不要 Markdown、星号、井号、列表、emoji；\n'
    '不超过 {max_chars} 字，一句答完，不复述问题、不解释过程；\n'
    '没数据就说不知道，不要编造。'
)

DEFAULTS = {
    'assist_enabled': '0',
    'assist_wake_words': '智能中继,中继台',
    'assist_wake_fuzzy': '1',
    'assist_channel': 'left',
    # 分段
    'assist_dbfs_open': '-50',
    'assist_dbfs_close': '-56',
    'assist_preroll_ms': '1200',
    'assist_silence_ms': '450',
    'assist_min_speech_ms': '350',
    'assist_max_utterance': '15',
    # 交互
    'assist_followup_seconds': '30',
    'assist_ack_reply': '请讲',
    'assist_use_vad': '1',
    'assist_enhance': '1',
    # 发射安全
    'assist_max_tx_seconds': '30',
    'assist_min_gap_seconds': '15',
    'assist_busy_wait_seconds': '8',
    'assist_tx_guard_ms': '600',
    'assist_quiet_hours': '',
    'assist_test_mode': '0',
    # LLM 预算
    'assist_max_reply_chars': '80',
    'assist_max_tokens': '256',
    'assist_history_turns': '6',
    'assist_max_input_chars': '3000',
    'assist_temperature': '0.3',
    'assist_provider': 'local',
    'assist_use_tools': '1',
    'assist_agent_iters': '2',
    'assist_keep_llm_warm': '1',
    'assist_llm_wait': '25',
    # 语音
    'assist_prompt_suffix': DEFAULT_SUFFIX,
    'assist_voice': '',
    # 归档
    'assist_retention_days': '30',
    'assist_debug_keep': '12',   # 识别音频留档条数（0=不留）
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS assist_turns(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT, kind TEXT, wake TEXT, heard TEXT, reply TEXT, action TEXT,
  tx_seconds REAL DEFAULT 0, truncated INTEGER DEFAULT 0, error TEXT DEFAULT '',
  asr_ms INTEGER DEFAULT 0, llm_ms INTEGER DEFAULT 0, tts_ms INTEGER DEFAULT 0,
  wait_s REAL DEFAULT 0, rx_wav TEXT DEFAULT '', tx_wav TEXT DEFAULT '',
  prompt_chars INTEGER DEFAULT 0, reply_chars INTEGER DEFAULT 0,
  provider TEXT DEFAULT '', model TEXT DEFAULT '', iters INTEGER DEFAULT 0,
  tools TEXT DEFAULT '', dbfs REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_assist_ts ON assist_turns(ts);
"""


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _f(v, d=0.0):
    try:
        if v is None or v == '':
            return d
        return float(v)
    except Exception:
        return d


def _flag(v, d=False):
    if v is None:
        return d
    return str(v).strip().lower() in ('1', 'true', 'yes', 'on', 'y', 't')


def _now_iso():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _takes_kw(fn, name):
    """fn 是否接受名为 name 的关键字参数（旧签名/测试替身返回 False）。"""
    if fn is None:
        return False
    try:
        return name in inspect.signature(fn).parameters
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 唤醒词匹配
# ---------------------------------------------------------------------------
# ASR 对同音字经常选错（实测「中继台」会出「中继太」）。这里只给**唤醒词用字**
# 开放同音集合，不做通用纠错——通用纠错会显著抬高误触发率。
# 只给**唤醒词用字**开放同音集合，不做通用纠错——通用纠错会显著抬高误触发率。
# 「机」是实测命中的：真实空口录音里「中继台」被识别成「中机台」，
# 而原来的集合里没有 jī 的常用字，唤醒直接落空。
_HOMOPHONE = {
    '台': '台太抬臺苔',
    '太': '台太抬臺苔',
    '智': '智志治至知',
    '志': '智志治至知',
    '继': '继记纪计技济机基击急即集几己纪技',
    '记': '继记纪计技济机基击急即集几己',
    '计': '继记纪计技济机基击急即集几己',
    '中': '中钟忠终',
    '钟': '中钟忠终',
}

# 匹配前丢弃的标点/空白：ASR 可能给出任意断句，不能因此漏唤醒
_SKIP_CHARS = set(' \t\n\r，。！？、；：,.!?;:""\'\'“”‘’《》()（）[]【】{}<>·—－-…')


def _compact(text):
    """去掉标点空白并返回 (紧凑串, 每个字符在原文中的下标)。"""
    chars, idx = [], []
    for i, ch in enumerate(text or ''):
        if ch.isspace() or ch in _SKIP_CHARS:
            continue
        chars.append(ch)
        idx.append(i)
    return ''.join(chars), idx


# 唤醒词前后的标点拼接后会留下「，，」这类重复，统一折叠
_DUP_PUNC_RE = re.compile(r'([，。！？、；：,.!?;:])\1+')


def _wake_regex(word, fuzzy=True):
    if not word:
        return None
    parts = []
    for ch in str(word):
        if fuzzy and ch in _HOMOPHONE:
            parts.append('[' + re.escape(_HOMOPHONE[ch]) + ']')
        else:
            parts.append(re.escape(ch))
    try:
        return re.compile(''.join(parts))
    except Exception:
        return None


def match_wake(text, words, fuzzy=True):
    """在识别文本里找唤醒词。

    返回 (配置里的唤醒词, 实际命中的原文片段, 剥掉唤醒词后剩余的问题文本)。
    未命中返回 ('', '', '')。
    """
    text = text or ''
    if not text.strip():
        return '', '', ''
    comp, idx = _compact(text)
    if not comp:
        return '', '', ''
    for w in words:
        rx = _wake_regex(w, fuzzy)
        if rx is None:
            continue
        m = rx.search(comp)
        if not m:
            continue
        a, b = m.start(), m.end() - 1
        if a >= len(idx) or b >= len(idx):
            continue
        oa, ob = idx[a], idx[b]
        rest = (text[:oa] + text[ob + 1:]).strip(' \t，。！？、；：,.!?;:""\'\'（）()')
        rest = _DUP_PUNC_RE.sub(r'\1', rest)
        return str(w), text[oa:ob + 1], rest
    return '', '', ''


def clamp_reply(text, limit):
    """把回复裁到 limit 字以内，尽量在句末/逗号处断开。返回 (文本, 是否被截断)。"""
    from tts_service import clean_for_tts
    s = clean_for_tts(text)
    if not s:
        return '', False
    limit = int(limit or 0)
    if limit <= 0 or len(s) <= limit:
        return s, False
    cut = s[:limit]
    best = max((i for i, c in enumerate(cut) if c in '。！？；.!?;'), default=-1)
    if best >= int(limit * 0.4):
        return cut[:best + 1], True
    best = max((i for i, c in enumerate(cut) if c in '，,、'), default=-1)
    if best >= int(limit * 0.6):
        return cut[:best + 1], True
    return cut, True


def in_quiet_hours(spec, now=None):
    """spec 形如 '23:00-07:00'（支持逗号分隔多段）。跨零点自动处理。"""
    spec = (spec or '').strip()
    if not spec:
        return False
    t = now or datetime.now()
    cur = t.hour * 60 + t.minute
    for piece in re.split(r'[,;、]', spec):
        piece = piece.strip()
        m = re.match(r'^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$', piece)
        if not m:
            continue
        a = int(m.group(1)) * 60 + int(m.group(2))
        b = int(m.group(3)) * 60 + int(m.group(4))
        if a == b:
            continue
        if a < b:
            if a <= cur < b:
                return True
        else:                       # 跨零点
            if cur >= a or cur < b:
                return True
    return False


# ---------------------------------------------------------------------------
# silero VAD：全局单例（复用 voice_service 的封装与模型缓存）
# ---------------------------------------------------------------------------
_VAD_LOCK = threading.Lock()
_VAD = None


def _get_vad():
    """懒加载 VAD；加载失败返回 None（调用方退回能量边界，不报错）。"""
    global _VAD
    with _VAD_LOCK:
        if _VAD is None:
            try:
                import voice_service
                _VAD = voice_service.Vad()
                print('%s VAD 已挂载（复用 voice_service 封装）' % LOG, flush=True)
            except Exception as e:
                print('%s VAD 不可用，退回能量边界：%s' % (LOG, e), flush=True)
                _VAD = False
        return _VAD or None


# ---------------------------------------------------------------------------
# 存储（只存「真实答过的轮次」；未命中的实况文本只留在内存里供页面观察）
# ---------------------------------------------------------------------------
class Store:
    def __init__(self, db_path):
        self.db_path = str(db_path)
        self._local = threading.local()
        self.init()

    def _conn(self):
        c = getattr(self._local, 'conn', None)
        if c is None:
            c = sqlite3.connect(self.db_path, timeout=20.0)
            c.row_factory = sqlite3.Row
            try:
                c.execute('PRAGMA journal_mode=WAL')
            except Exception:
                pass
            self._local.conn = c
        return c

    def init(self):
        c = self._conn()
        c.executescript(SCHEMA)
        c.commit()

    def exec(self, sql, args=()):
        c = self._conn()
        cur = c.execute(sql, args)
        c.commit()
        return cur

    def query(self, sql, args=()):
        return [dict(r) for r in self._conn().execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        r = self._conn().execute(sql, args).fetchone()
        return dict(r) if r else None


# ---------------------------------------------------------------------------
# 服务主体
# ---------------------------------------------------------------------------
class AssistantService:
    def __init__(self, db_path):
        self.store = Store(db_path)
        self.lock = threading.RLock()
        self.q = queue.Queue(maxsize=8)
        self.recent = deque(maxlen=80)          # 实况识别流（含未命中唤醒词）
        self.levels = deque(maxlen=260)         # 电平曲线（约 20 秒）
        self.history = deque(maxlen=20)         # 多轮上下文
        self.events = deque(maxlen=200)
        self.counters = {
            'segments': 0, 'asr_empty': 0, 'wakes': 0, 'ignored': 0,
            'turns': 0, 'tx': 0, 'tx_seconds': 0.0, 'busy_defers': 0,
            'gap_waits': 0, 'truncated': 0, 'errors': 0, 'aborted': 0,
            'quiet_blocked': 0, 'test_turns': 0,
        }
        self.stage = 'off'
        self.stage_since = time.time()
        self.stage_detail = ''
        self.follow_until = 0.0
        self.last_tx = 0.0
        self.tx_until = 0.0
        # 最后一次观测到 PTT 有效的时刻：余波保护从这里起算，
        # 绝不能从「当前时刻」起算（那会导致保护期无限续期）。
        self.tx_last_ts = 0.0
        self.stop_flag = False
        self.last_error = ''
        self.asr_last = {}
        self.llm_warm = {'ok': None, 'ts': 0.0, 'msg': '未探测'}
        self.started_at = time.time()
        self._threads_started = False
        self.settings_cache = {}
        self.settings_ts = 0.0
        # 依赖注入
        self._setting_getter = None
        self.busy_getter = lambda: False
        self.tx_getter = lambda: False
        self.ask_fn = None
        self.tts_fn = None
        self.play_fn = None
        self.stop_play_fn = lambda: None
        self.base_prompt_fn = lambda: ''
        self.expand_fn = lambda t: t
        # 分段状态
        self.seg = None
        self.pre = deque()
        self.pre_bytes = 0
        self.last_dbfs = -120.0
        self._level_ts = 0.0
        self._last_heard = ''
        self._last_heard_ts = 0.0
        self._warned = ''

    # -- 配置 / 依赖注入 ---------------------------------------------------
    def configure(self, setting_getter, busy_getter, tx_getter, ask_fn, tts_fn,
                  play_fn, base_prompt_fn, stop_play_fn=None, expand_fn=None):
        self._setting_getter = setting_getter
        self.busy_getter = busy_getter or (lambda: False)
        self.tx_getter = tx_getter or (lambda: False)
        self.ask_fn = ask_fn
        self.tts_fn = tts_fn
        self.play_fn = play_fn
        self.stop_play_fn = stop_play_fn or (lambda: None)
        self.base_prompt_fn = base_prompt_fn or (lambda: '')
        self.expand_fn = expand_fn or (lambda t: t)
        # ask_fn 要不要收 sysprompt=：测试替身用的是旧签名，硬传会 TypeError
        self._ask_takes_sysprompt = _takes_kw(ask_fn, 'sysprompt')

    def settings(self, force=False):
        now = time.time()
        with self.lock:
            if not force and self.settings_cache and (now - self.settings_ts) < 4.0:
                return self.settings_cache
        st = dict(DEFAULTS)
        try:
            if self._setting_getter:
                rows = self._setting_getter()
                if isinstance(rows, dict):
                    st.update({k: v for k, v in rows.items() if v is not None})
        except Exception:
            pass
        with self.lock:
            self.settings_cache = st
            self.settings_ts = now
        return st

    def invalidate(self):
        with self.lock:
            self.settings_cache = {}
            self.settings_ts = 0.0

    def enabled(self):
        return _flag(self.settings().get('assist_enabled'), False)

    def wake_words(self, st=None):
        st = st or self.settings()
        raw = st.get('assist_wake_words') or ''
        out = [w.strip() for w in re.split(r'[,;、\s]+', raw) if w.strip()]
        return out[:8]

    def max_chars(self, st=None):
        st = st or self.settings()
        return max(10, int(_f(st.get('assist_max_reply_chars'), 80)))

    def hist_turns(self, st=None):
        st = st or self.settings()
        return max(0, min(12, int(_f(st.get('assist_history_turns'), 6))))

    # -- 生命周期 ----------------------------------------------------------
    def start(self):
        if self._threads_started:
            return
        self._threads_started = True
        for fn, name in ((self._worker, 'assist-worker'),
                         (self._maintenance, 'assist-maint')):
            t = threading.Thread(target=fn, name=name, daemon=True)
            t.start()
        print('%s 服务已启动（工作目录 %s）' % (LOG, WORK_DIR), flush=True)

    def _maintenance(self):
        time.sleep(12)
        while True:
            try:
                st = self.settings()
                if self.enabled() and _flag(st.get('assist_keep_llm_warm'), True):
                    # 语音助手不能等 20~40 秒冷启动：启用期间保持 LLM 常驻
                    if time.time() - float(self.llm_warm.get('ts') or 0) > 60:
                        self._warm_llm()
                self._cleanup()
            except Exception as e:
                print('%s 维护线程异常：%s: %s' % (LOG, type(e).__name__, e), flush=True)
            time.sleep(30)

    def _warm_llm(self):
        ok, msg = None, ''
        try:
            import voice_service
            # 注意：voice_service.llm_start() 返回 **bool**；
            # 返回 (ok, msg) 元组的是 VoiceService.ensure_llm_ready()，别搞混。
            wait = float(_f(self.settings().get('assist_llm_wait'), 25))
            ok = bool(voice_service.llm_start(wait=wait))
            msg = '' if ok else '本地 LLM 启动/等待就绪超时（%.0fs）' % wait
        except Exception as e:
            ok, msg = False, '%s: %s' % (type(e).__name__, e)
        self.llm_warm = {'ok': bool(ok), 'ts': time.time(), 'msg': str(msg or '')[:200]}
        if not ok:
            self.last_error = '本地 LLM 未就绪：%s' % self.llm_warm['msg']
        return bool(ok)

    def _cleanup(self):
        st = self.settings()
        days = max(1, int(_f(st.get('assist_retention_days'), 30)))
        cutoff = time.time() - days * 86400
        if not WORK_DIR.exists():
            return 0
        removed = 0
        for day in sorted(WORK_DIR.iterdir()):
            if not day.is_dir():
                continue
            try:
                if datetime.strptime(day.name, '%Y-%m-%d').timestamp() < cutoff:
                    for f in day.iterdir():
                        try:
                            f.unlink()
                        except Exception:
                            pass
                    day.rmdir()
                    removed += 1
            except Exception:
                continue
        if removed:
            print('%s 清理 %d 天前的助手录音' % (LOG, removed), flush=True)
        return removed

    # -- 采集入口（由 app.py 的 _mic_capture_loop 调用）--------------------
    def feed(self, raw_stereo, ts=None):
        if not self.enabled():
            if self.stage != 'off':
                self._set_stage('off')
            return
        ts = ts or time.time()
        st = self.settings()
        try:
            mono = self._pick(raw_stereo, st.get('assist_channel', 'left'))
        except Exception:
            return
        if not mono:
            return
        x = np.frombuffer(mono, dtype=np.int16).astype(np.float32) / 32768.0
        if x.size == 0:
            return
        rms = float(np.sqrt((x * x).mean()))
        dbfs = 20.0 * math.log10(max(rms, 1e-6))
        self.last_dbfs = dbfs
        now = time.time()
        if now - self._level_ts >= 0.08:
            self._level_ts = now
            self.levels.append([round(now, 2), round(dbfs, 1)])
        try:
            with self.lock:
                self._segment(x, mono, ts, dbfs, st)
        except Exception as e:
            self.last_error = '%s: %s' % (type(e).__name__, e)

    @staticmethod
    def _pick(raw, channel):
        """从 16k/立体声 S16_LE 里取指定声道，返回单声道 bytes。"""
        usable = len(raw) - (len(raw) % 4)
        if usable <= 0:
            return b''
        raw = raw[:usable]
        if channel == 'mix':
            a = np.frombuffer(raw, dtype=np.int16).reshape(-1, 2).astype(np.int32)
            return ((a[:, 0] + a[:, 1]) // 2).astype(np.int16).tobytes()
        idx = 0 if channel != 'right' else 1
        a = np.frombuffer(raw, dtype=np.int16).reshape(-1, 2)[:, idx]
        return np.ascontiguousarray(a).tobytes()

    def _segment(self, x, mono, ts, dbfs, st):
        """能量迟滞分段：够响就开段，静音够久就收段。"""
        # 自己发射期间及其余波：不收音（否则自己的声音会被当成对方说话 → 自激）。
        # 余波保护从**最后一次观测到 PTT 有效**起算。曾经写成
        #   self.tx_until = max(self.tx_until, ts + guard)
        # 它是「当前时刻 + 0.6s」，而保护期内每个采集块都会执行一次，
        # 于是截止时间被无限往后推、保护期永不结束 —— 实测表现为助手首发
        # 成功之后再也没收到任何呼叫（连丢三次，语音日志却全部收到）。
        guard = max(0.0, _f(st.get('assist_tx_guard_ms'), 600.0)) / 1000.0
        tx_now = bool(self.tx_getter())
        if tx_now:
            self.tx_last_ts = ts
        if tx_now or (self.tx_last_ts and (ts - self.tx_last_ts) < guard):
            if self.seg is not None:
                self.seg = None
            self.pre.clear()
            self.pre_bytes = 0
            self._set_stage('tx-guard', '发射中/余波保护')
            return

        open_db = _f(st.get('assist_dbfs_open'), -40.0)
        close_db = _f(st.get('assist_dbfs_close'), -46.0)
        if close_db > open_db:                      # 容错：关闭门限必须低于打开门限
            close_db = open_db - 6.0
        preroll_ms = max(0.0, _f(st.get('assist_preroll_ms'), 400.0))
        silence = max(0.15, _f(st.get('assist_silence_ms'), 450.0)) / 1000.0
        min_speech = max(0.1, _f(st.get('assist_min_speech_ms'), 350.0)) / 1000.0
        max_utt = max(2.0, _f(st.get('assist_max_utterance'), 15.0))

        if self.seg is None:
            if dbfs >= open_db:
                seg = {'start': ts, 'last_voice': ts, 'buf': [], 'n': 0,
                       'pre': list(self.pre), 'busy': bool(self.busy_getter()),
                       'peak': dbfs, 'voice_n': 0}
                seg['buf'].append(mono)
                seg['n'] += len(mono)
                seg['voice_n'] += len(mono)
                self.seg = seg
                self._set_stage('speech', '检测到语音')
            else:
                self._push_pre(ts, mono, preroll_ms)
                # 启用后一直没人讲话时，必须把状态机从初始的 'off' 推进到 'idle'，
                # 否则页面一直显示「未启用」，与事实不符（实测踩到）。
                self._set_stage('idle', '监听中')
            return

        seg = self.seg
        seg['buf'].append(mono)
        seg['n'] += len(mono)
        if dbfs > seg['peak']:
            seg['peak'] = dbfs
        if dbfs >= close_db:
            seg['last_voice'] = ts
            seg['voice_n'] += len(mono)

        too_long = (ts - seg['start']) >= max_utt
        silent = (ts - seg['last_voice']) >= silence
        if not (too_long or silent):
            return

        # 收段
        self.seg = None
        self._push_pre(ts, mono, preroll_ms)
        data = b''.join([m for (_t, m) in seg['pre']] + seg['buf'])
        seconds = len(data) / float(SAMPLE_RATE * FRAME_BYTES)
        # 必须用**实际有声时长**判定，不能用缓冲区长度：缓冲区里还含着
        # 0.4s 前置缓冲与 0.45s 收段尾静音，用总长度判会让一个 0.2 秒的
        # 咔哒声凑成 1.05 秒蒙混过关（自测就是这么抓出来的）。
        voice_seconds = seg['voice_n'] / float(SAMPLE_RATE * FRAME_BYTES)
        if voice_seconds < min_speech or len(data) < int(0.15 * SAMPLE_RATE) * FRAME_BYTES:
            self._set_stage('idle', '过短丢弃（有声 %.2fs）' % voice_seconds)
            return
        self.counters['segments'] += 1
        try:
            self.q.put_nowait({'data': data, 'ts': seg['start'],
                               'seconds': seconds, 'voice_seconds': voice_seconds,
                               'dbfs': seg['peak'], 'busy': seg.get('busy', False)})
        except queue.Full:
            self.counters['ignored'] += 1
            self._set_stage('idle', '队列已满，丢弃最旧')
            try:
                self.q.get_nowait()
                self.q.put_nowait({'data': data, 'ts': seg['start'],
                                   'seconds': seconds, 'voice_seconds': voice_seconds,
                                   'dbfs': seg['peak'], 'busy': seg.get('busy', False)})
            except Exception:
                pass

    def _push_pre(self, ts, mono, preroll_ms):
        if preroll_ms <= 0:
            return
        self.pre.append((ts, mono))
        self.pre_bytes += len(mono)
        cap = int(preroll_ms / 1000.0 * SAMPLE_RATE) * FRAME_BYTES
        while self.pre_bytes > cap and self.pre:
            _t, old = self.pre.popleft()
            self.pre_bytes -= len(old)

    # -- 工作线程 ----------------------------------------------------------
    def _worker(self):
        try:
            import os
            os.nice(5)
        except Exception:
            pass
        while True:
            item = self.q.get()
            try:
                if self.enabled():
                    self._handle(item)
            except Exception as e:
                self.counters['errors'] += 1
                self.last_error = '%s: %s' % (type(e).__name__, e)
                print('%s 处理失败：%s' % (LOG, self.last_error), flush=True)
            finally:
                self.q.task_done()

    def _set_stage(self, stage, detail=''):
        with self.lock:
            if stage != self.stage:
                self.stage = stage
                self.stage_since = time.time()
            self.stage_detail = detail or ''

    def _stage_label(self):
        return {
            'off': '未启用', 'idle': '监听中', 'speech': '检测到语音',
            'asr': '识别中', 'think': '思考中', 'synth': '语音合成中',
            'wait': '等待信道', 'tx': '发射中', 'tx-guard': '发射余波保护',
            'followup': '追问窗口', 'error': '异常',
        }.get(self.stage, self.stage)

    # -- 单轮处理 ----------------------------------------------------------
    def _handle(self, item):
        st = self.settings()
        x, sr = self._decode(item['data'])
        if x is None or x.size < int(0.1 * SAMPLE_RATE):
            return
        # 直接调用语音日志的共用识别入口：预处理 → silero VAD 切分 →
        # merge_segments 合并 → SenseVoice。两边同一份实现，不会再漂移。
        # 这里刻意不再自己做 VAD 边界裁剪——实测 silero 报出的首段起点比真实
        # 语音起点晚约 320ms，自己裁会把「中继台」的开头两个字剪掉。
        self._set_stage('asr', '识别中')
        text, asr_ms, err = self._asr_shared(x, sr, st)
        self.asr_last = {'text': text, 'ms': asr_ms, 'ts': time.strftime('%H:%M:%S'),
                         'seconds': item['seconds'], 'error': err}
        rec = {'ts': time.strftime('%H:%M:%S'), 'heard': text, 'wake': '',
               'action': 'ignored', 'error': err, 'asr_ms': asr_ms,
               'seconds': round(item['seconds'], 2), 'dbfs': round(item['dbfs'], 1),
               'reply': '', 'id': 0}

        def keep(action, wake=''):
            if rec.get('_kept'):
                return
            rec['_kept'] = True
            rec['dbg'] = self._dbg_keep(item['data'], text, action, wake,
                                        item.get('dbfs') or 0)
        if err:
            rec['action'] = 'error'
            self.counters['errors'] += 1
            keep('error')
            self._push_recent(rec)
            self._set_stage('idle', '识别失败')
            return
        if not text:
            rec['action'] = 'empty'
            self.counters['asr_empty'] += 1
            keep('empty')
            self._push_recent(rec)
            self._set_stage('idle', '无有效语音')
            return
        # 去重：同一句话在 3 秒内重复出现（B 段/尾音重录）只处理一次
        now = time.time()
        if text == self._last_heard and (now - self._last_heard_ts) < 3.0:
            rec['action'] = 'duplicate'
            keep('duplicate')
            self._push_recent(rec)
            return
        self._last_heard, self._last_heard_ts = text, now

        wake, hit, rest = match_wake(
            text, self.wake_words(st), _flag(st.get('assist_wake_fuzzy'), True))
        in_window = now <= self.follow_until
        if wake:
            rec['wake'] = hit
            self.counters['wakes'] += 1
            kind = 'wake'
        elif in_window:
            rec['wake'] = '(追问窗口)'
            kind = 'followup'
            rest = text
        else:
            rec['action'] = 'ignored'
            self.counters['ignored'] += 1
            keep('ignored')
            self._push_recent(rec)
            self._set_stage('idle', '未命中唤醒词')
            return

        if not rest:
            # 只喊了唤醒词：回一句「请讲」，并打开追问窗口
            ack = (st.get('assist_ack_reply') or '').strip() or '请讲'
            rec['action'] = 'ack'
            rec['reply'] = ack
            keep('ack', rec['wake'])
            self._push_recent(rec)
            self._answer(ack, st, kind='ack', heard=text, asr_ms=asr_ms,
                         rx_bytes=item['data'], dbfs=item['dbfs'], wake=hit)
            return

        rec['action'] = 'answer'
        keep('answer', rec['wake'])
        self._push_recent(rec)
        self._answer(rest, st, kind=kind, heard=text, asr_ms=asr_ms,
                     rx_bytes=item['data'], dbfs=item['dbfs'], wake=hit)

    def _decode(self, data):
        try:
            x = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
            return x, SAMPLE_RATE
        except Exception:
            return None, SAMPLE_RATE

    def _vad_trim(self, x, sr, seconds):
        """用 silero VAD 裁掉首尾静音。

        只做**边界收紧**，不做语音/非语音判决：判空时原样返回。
        这一点很重要——silero 偶尔会把只有 0.5~0.8 秒的「中继台」判成非语音，
        若据此丢弃整段，唤醒就再也唤不醒了。宁可多跑一次 ASR。
        """
        if seconds < 0.4:
            return x
        vad = _get_vad()
        if vad is None:
            return x
        try:
            segs = vad.split(x, sr)
        except Exception:
            return x
        if not segs:
            return x
        a = int(max(0.0, segs[0][0] - 0.05) * sr)
        b = int(min(len(x) / float(sr), segs[-1][1] + 0.10) * sr)
        if b - a < int(0.15 * sr):
            return x
        return x[a:b]

    def _asr_shared(self, x, sr, st):
        """调用 voice_service.transcribe_pcm（语音日志的同一套识别流程）。"""
        t0 = time.time()
        try:
            import voice_service
            text, ms = voice_service.transcribe_pcm_text(
                x, sr, enhance=_flag(st.get('assist_enhance'), True),
                min_seconds=0.30, vad_empty_fallback=True)
            return text, int(ms or (time.time() - t0) * 1000), ''
        except Exception as e:
            return '', int((time.time() - t0) * 1000), '%s: %s' % (type(e).__name__, e)

    def _push_recent(self, rec):
        with self.lock:
            self.recent.append(dict(rec))

    # -- 回答（LLM → TTS → 发射）------------------------------------------
    def _answer(self, question, st, kind='wake', heard='', asr_ms=0,
                rx_bytes=None, dbfs=0.0, wake='', no_tx=False):
        """完整跑一轮回答。kind: wake / followup / ack / test。"""
        self.stop_flag = False
        if kind != 'ack':
            self._set_stage('think', '思考中')
        base = ''
        try:
            base = (self.base_prompt_fn() or '').strip()
        except Exception:
            base = ''
        prompt, prompt_chars = self._build_prompt(question, st, base)
        # 总结轮要**重新**拿到「基础设定 + 语音播报规范」：真正被朗读的文本是
        # 总结轮产出的，而第一轮在 force_first 下被要求「只输出读取指令、不要
        # 回答用户」。约束只留在第一轮 = 对最终答案零生效（现场 bug）。
        sysprompt = '\n'.join([x for x in (base, self._spec_text(st)) if x])
        # 只有 ack 是固定短语，不必过 LLM；其余一律走 LLM
        reply, llm_ms, provider, model, iters, tools, lerr = '', 0, '', '', 0, '', ''
        if kind == 'ack':
            reply = (st.get('assist_ack_reply') or '').strip() or '请讲'
        else:
            if self.ask_fn is None:
                lerr = 'LLM 调用未注入'
            else:
                try:
                    kw = ({'sysprompt': sysprompt}
                          if self._ask_takes_sysprompt else {})
                    r = self.ask_fn(prompt, question,
                                    int(_f(st.get('assist_max_tokens'), 256)),
                                    _f(st.get('assist_temperature'), 0.3),
                                    _flag(st.get('assist_use_tools'), True),
                                    int(_f(st.get('assist_agent_iters'), 2)),
                                    **kw) or {}
                except Exception as e:
                    r = {'ok': False, 'error': '%s: %s' % (type(e).__name__, e)}
                llm_ms = int(r.get('ms') or 0)
                provider = str(r.get('provider') or '')
                model = str(r.get('model') or '')
                iters = int(r.get('iters') or 0)
                tools = str(r.get('tools') or '')
                if r.get('ok'):
                    reply = (r.get('reply') or '').strip()
                else:
                    lerr = str(r.get('error') or 'LLM 失败')[:200]
        if lerr or not reply:
            self.counters['errors'] += 1
            self.last_error = lerr or 'LLM 返回空内容'
            self._set_stage('idle', '回答失败')
            self._save_turn(kind, heard, '', 'error', error=self.last_error, wake=wake,
                            asr_ms=asr_ms, llm_ms=llm_ms, prompt_chars=prompt_chars,
                            provider=provider, model=model, iters=iters, tools=tools,
                            rx_bytes=rx_bytes, dbfs=dbfs)
            return
        limit = self.max_chars(st)
        speak, truncated = clamp_reply(reply, limit)
        if not speak:
            self.last_error = '回复清洗后为空'
            self._save_turn(kind, heard, reply, 'error', error=self.last_error, wake=wake,
                            asr_ms=asr_ms, llm_ms=llm_ms, prompt_chars=prompt_chars,
                            provider=provider, model=model, iters=iters, tools=tools,
                            rx_bytes=rx_bytes, dbfs=dbfs)
            return
        if truncated:
            self.counters['truncated'] += 1
        # 记入多轮上下文（用完的原文，便于追问指代）
        if kind != 'ack':
            with self.lock:
                self.history.append({'q': question[:160], 'a': speak[:160]})
        # 合成
        self._set_stage('synth', '语音合成中')
        t0 = time.time()
        wav_path, terr = '', ''
        try:
            wav_path = self.tts_fn(speak, (st.get('assist_voice') or '').strip())
        except Exception as e:
            terr = '%s: %s' % (type(e).__name__, e)
        tts_ms = int((time.time() - t0) * 1000)
        if terr or not wav_path:
            self.counters['errors'] += 1
            self.last_error = '语音合成失败：%s' % (terr or '无输出')
            self._set_stage('idle', '合成失败')
            self._save_turn(kind, heard, speak, 'error', error=self.last_error, wake=wake,
                            asr_ms=asr_ms, llm_ms=llm_ms, tts_ms=tts_ms,
                            prompt_chars=prompt_chars, provider=provider, model=model,
                            iters=iters, tools=tools, rx_bytes=rx_bytes, dbfs=dbfs)
            return
        # 发射
        t_tx = time.time()
        res = self._transmit(wav_path, st, no_tx=no_tx)
        tx_seconds = float(res.get('seconds') or 0.0)
        ok = bool(res.get('ok'))
        skipped = bool(res.get('skipped'))
        action = 'sent' if ok else ('skipped' if skipped else 'failed')
        if ok:
            self.counters['tx'] += 1
            self.counters['tx_seconds'] += tx_seconds
            self.last_tx = time.time()
            self.tx_last_ts = self.last_tx
            self.tx_until = self.last_tx + max(
                0.0, _f(st.get('assist_tx_guard_ms'), 600.0)) / 1000.0
            self.follow_until = time.time() + max(0.0, _f(st.get('assist_followup_seconds'), 30.0))
            self._set_stage('followup', '追问窗口')
        elif skipped:
            self.counters['aborted'] += 1
            if '禁发时段' in str(res.get('error') or ''):
                self.counters['quiet_blocked'] += 1
            self._set_stage('idle', str(res.get('error') or '已跳过'))
        else:
            self.counters['errors'] += 1
            self.last_error = str(res.get('error') or '发射失败')
            self._set_stage('error', self.last_error)
        self.counters['turns'] += 1
        self._save_turn(kind, heard, speak, action, wake=wake,
                        error=str(res.get('error') or ''),
                        tx_seconds=tx_seconds, truncated=truncated,
                        asr_ms=asr_ms, llm_ms=llm_ms, tts_ms=tts_ms,
                        wait_s=float(res.get('waited') or 0.0),
                        prompt_chars=prompt_chars, reply_chars=len(speak),
                        provider=provider, model=model, iters=iters, tools=tools,
                        rx_bytes=rx_bytes, tx_wav=wav_path, dbfs=dbfs)
        print('%s [%s] %s → %s（发射 %.1fs%s）' % (
            LOG, kind, heard[:40], speak[:60], tx_seconds,
            '，已截断' if truncated else ''), flush=True)

    def _spec_text(self, st):
        """展开后的「语音播报约束」原文（{max_chars} 已代入）。

        _build_prompt 与 _answer 必须用**同一份**文本，否则总结轮回灌的约束
        会和第一轮说明的不一致。
        """
        suffix = (st.get('assist_prompt_suffix') or '').strip()
        if not suffix:
            return ''
        try:
            suffix = self.expand_fn(suffix)
        except Exception:
            pass
        return suffix.replace('{max_chars}', str(self.max_chars(st)))

    def _build_prompt(self, question, st, base=''):
        """拼提示词并逐级降配，保证输入不超上限（防止挤爆本地上下文）。"""
        suffix = self._spec_text(st)
        head_full = '\n'.join([x for x in (base, suffix) if x])
        # 降配版头部：**优先保住规范**，宁可砍共用基础设定。
        # 旧实现是 head_full[:600]，只要基础设定本身 ≥600 字，这一刀就把规范
        # 整段切掉（规范在基础设定之后），模型等于完全没被约束过。
        if suffix:
            room = max(0, 600 - len(suffix) - 1)
            head_short = '\n'.join(
                [x for x in ((base[:room] if room else ''), suffix) if x])
        else:
            head_short = base[:600]
        max_in = max(400, int(_f(st.get('assist_max_input_chars'), 3000)))
        n = self.hist_turns(st)
        q = (question or '').strip()[:400]
        variants = []
        for hist_n in (n, min(n, 3), min(n, 1), 0):
            for head in (head_full, head_short, suffix, ''):
                parts = []
                if head:
                    parts.append('【系统设定】\n' + head)
                h = self._history_text(st, hist_n)
                if h:
                    parts.append('【对话历史】\n' + h)
                parts.append('【当前问题】\n' + q)
                variants.append('\n\n'.join(parts))
        for p in variants:
            if len(p) <= max_in:
                return p, len(p)
        return variants[-1][:max_in], max_in

    def _history_text(self, st, turns):
        if turns <= 0 or not self.history:
            return ''
        lines = []
        for h in list(self.history)[-turns:]:
            lines.append('对方：' + (h.get('q') or '')[:120])
            lines.append('我：' + (h.get('a') or '')[:120])
        return '\n'.join(lines)

    def _transmit(self, wav_path, st, no_tx=False):
        """受控发射：禁发时段 → 等信道空闲+间隔 → 硬超时播放。

        no_tx=True 时**无条件**不发射（手动回环测试用），优先级高于一切设置项：
        页面上写着「不发射」就必须真的不发射，不能靠设置项巧合成立。
        """
        if no_tx:
            return {'ok': False, 'skipped': True,
                    'error': '本地回环测试：只合成试听，不发射'}
        qh = (st.get('assist_quiet_hours') or '').strip()
        if qh and in_quiet_hours(qh):
            return {'ok': False, 'skipped': True,
                    'error': '当前处于禁发时段（%s）' % qh}
        if _flag(st.get('assist_test_mode'), False):
            return {'ok': False, 'skipped': True, 'error': '测试模式：仅网页试听，不发射'}
        want_gap = max(0.0, _f(st.get('assist_min_gap_seconds'), 15.0))
        budget = max(3.0, _f(st.get('assist_busy_wait_seconds'), 8.0))
        budget = max(budget, want_gap + 3.0)
        max_tx = max(3.0, _f(st.get('assist_max_tx_seconds'), 30.0))
        tw = time.time()
        busy_seen = False
        while True:
            if self.stop_flag:
                return {'ok': False, 'skipped': True, 'error': '已被手动停止'}
            now = time.time()
            left = want_gap - (now - self.last_tx) if self.last_tx else 0.0
            busy = bool(self.busy_getter())
            if busy:
                busy_seen = True
            if not busy and left <= 0:
                break
            if now - tw >= budget:
                if busy:
                    self.counters['busy_defers'] += 1
                    return {'ok': False, 'skipped': True,
                            'error': '信道忙（BUSY 有效），已放弃本次回答'}
                self.counters['gap_waits'] += 1
                return {'ok': False, 'skipped': True,
                        'error': '距上次发射不足 %.0fs，已放弃本次回答' % want_gap}
            self._set_stage('wait', '信道忙' if busy else '等待最小间隔')
            time.sleep(0.15)
        waited = round(time.time() - tw, 2)
        self._set_stage('tx', '发射中')
        try:
            res = self.play_fn(wav_path, max_tx) or {}
        except Exception as e:
            res = {'ok': False, 'error': '%s: %s' % (type(e).__name__, e)}
        res = dict(res)
        res['waited'] = waited
        if busy_seen:
            res['busy_seen'] = True
        return res

    def _save_turn(self, kind, heard, reply, action, error='', tx_seconds=0.0,
                   wake='',
                   truncated=False, asr_ms=0, llm_ms=0, tts_ms=0, wait_s=0.0,
                   prompt_chars=0, reply_chars=0, provider='', model='', iters=0,
                   tools='', rx_bytes=None, tx_wav='', dbfs=0.0):
        rx_name = ''
        try:
            rx_name = self._store_wav(rx_bytes, 'rx')
        except Exception:
            rx_name = ''
        tx_name = ''
        try:
            if tx_wav:
                tx_name = self._store_wav_file(tx_wav, 'tx')
        except Exception:
            tx_name = ''
        try:
            cur = self.store.exec(
                'INSERT INTO assist_turns(ts,kind,wake,heard,reply,action,tx_seconds,'
                'truncated,error,asr_ms,llm_ms,tts_ms,wait_s,rx_wav,tx_wav,'
                'prompt_chars,reply_chars,provider,model,iters,tools,dbfs) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (_now_iso(), kind, wake, heard, reply, action, float(tx_seconds),
                 1 if truncated else 0, error[:300], int(asr_ms), int(llm_ms),
                 int(tts_ms), float(wait_s), rx_name, tx_name, int(prompt_chars),
                 int(reply_chars), provider, model, int(iters), tools, float(dbfs)))
            return int(getattr(cur, 'lastrowid', 0) or 0)
        except Exception as e:
            print('%s 保存轮次失败：%s' % (LOG, e), flush=True)
            return 0

    def _dbg_keep(self, data, text, action, wake, dbfs):
        """环形保留最近 N 条识别音频，便于事后核对助手到底听到了什么。

        未命中的语音段原本既不落库也不落盘，排查「唤醒词丢字」只能靠反推，
        实测绕了四轮，因此加这个留档环。每条约 200KB，默认只留 12 条。
        """
        try:
            n = int(_f(self.settings().get('assist_debug_keep'), 12))
        except Exception:
            n = 12
        if n <= 0 or not data:
            return ''
        try:
            d = WORK_DIR / 'debug'
            d.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime('%m%d_%H%M%S')
            name = 'dbg_%s_%s.wav' % (stamp, action)
            with wave.open(str(d / name), 'wb') as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SAMPLE_RATE)
                w.writeframes(data)
            # 留档元信息写同名 .txt，含识别原文与判定，便于直接对比
            (d / (name[:-4] + '.txt')).write_text(
                '时间: %s\naction: %s\n唤醒: %s\n电平: %.1f dBFS\n识别: %s\n'
                % (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), action, wake or '-',
                   dbfs, text or '(空)'), encoding='utf-8')
            for old in sorted(d.glob('dbg_*.wav'))[:-max(1, n)]:
                try:
                    old.unlink()
                    t = old.with_suffix('.txt')
                    if t.exists():
                        t.unlink()
                except Exception:
                    pass
            return name
        except Exception as e:
            print('%s 留档失败：%s' % (LOG, e), flush=True)
            return ''

    def debug_list(self):
        """列出留档（新的在前）。"""
        d = WORK_DIR / 'debug'
        out = []
        if not d.exists():
            return out
        for f in sorted(d.glob('dbg_*.wav'), reverse=True):
            info = {}
            t = f.with_suffix('.txt')
            if t.exists():
                try:
                    for ln in t.read_text(encoding='utf-8').splitlines():
                        if ':' in ln:
                            k, v = ln.split(':', 1)
                            info[k.strip()] = v.strip()
                except Exception:
                    pass
            out.append({'name': f.name, 'size': f.stat().st_size,
                        'ts': info.get('时间', ''), 'action': info.get('action', ''),
                        'wake': info.get('唤醒', ''), 'text': info.get('识别', ''),
                        'dbfs': info.get('电平', '')})
        return out

    def debug_path(self, name):
        name = str(name or '')
        if not name.startswith('dbg_') or not name.endswith('.wav') or '/' in name or '..' in name:
            return None
        f = WORK_DIR / 'debug' / name
        return f if f.exists() else None

    def _day_dir(self):
        d = WORK_DIR / datetime.now().strftime('%Y-%m-%d')
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _store_wav(self, data, tag):
        if not data:
            return ''
        try:
            d = self._day_dir()
            name = 'aturn_%s_%d_%s.wav' % (datetime.now().strftime('%H%M%S'), int(time.time() * 1000) % 100000, tag)
            with wave.open(str(d / name), 'wb') as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SAMPLE_RATE)
                w.writeframes(data)
            return name
        except Exception:
            return ''

    def _store_wav_file(self, src, tag):
        try:
            src = Path(src)
            if not src.exists():
                return ''
            d = self._day_dir()
            name = 'aturn_%s_%s.wav' % (datetime.now().strftime('%H%M%S'), tag)
            dst = d / name
            dst.write_bytes(src.read_bytes())
            return name
        except Exception:
            return ''

    def wav_path(self, name):
        """把库里存的文件名解析成绝对路径（供 /api/assist/<id>/audio 使用）。"""
        name = str(name or '').strip()
        if not name or '/' in name or '\\' in name or '..' in name:
            return None
        if not name.endswith('.wav') or not name.startswith('aturn_'):
            return None
        for d in sorted(WORK_DIR.glob('*'), reverse=True):
            if d.is_dir() and (d / name).exists():
                return d / name
        return None

    # -- 对外接口 ----------------------------------------------------------
    def stop(self):
        """一键停止：打断当前播放、清空队列、关闭追问窗口。"""
        self.stop_flag = True
        self.follow_until = 0.0
        n = 0
        while True:
            try:
                self.q.get_nowait()
                self.q.task_done()
                n += 1
            except queue.Empty:
                break
        try:
            self.stop_play_fn()
        except Exception:
            pass
        self._set_stage('idle', '已手动停止')
        print('%s 手动停止：清空 %d 条待处理语音' % (LOG, n), flush=True)
        return {'stopped': True, 'cleared': n}

    def test_turn(self, text):
        """本地回环测试：走完整链路（ASR 可跳过），默认不发射。"""
        text = (text or '').strip()
        if not text:
            return {'ok': False, 'error': '测试文本为空'}
        st = self.settings()

        def _run():
            try:
                self.counters['test_turns'] += 1
                self._last_heard, self._last_heard_ts = text, time.time()
                self._answer(text, st, kind='test', heard=text, asr_ms=0,
                             rx_bytes=None, dbfs=0.0, no_tx=True)
            except Exception as e:
                self.last_error = '%s: %s' % (type(e).__name__, e)
                print('%s 测试轮失败：%s' % (LOG, self.last_error), flush=True)

        threading.Thread(target=_run, daemon=True, name='assist-test').start()
        return {'ok': True, 'queued': True, 'test_mode': _flag(st.get('assist_test_mode'), False)}

    def test_wake(self, text):
        """只做唤醒词匹配测试，不调用 LLM、不发射。"""
        st = self.settings()
        wake, hit, rest = match_wake(text, self.wake_words(st),
                                     _flag(st.get('assist_wake_fuzzy'), True))
        return {'words': self.wake_words(st), 'matched': wake, 'hit': hit,
                'question': rest, 'fuzzy': _flag(st.get('assist_wake_fuzzy'), True)}

    def list_turns(self, day=None, limit=100, offset=0):
        day = day or datetime.now().strftime('%Y-%m-%d')
        rows = self.store.query(
            'SELECT * FROM assist_turns WHERE ts LIKE ? ORDER BY id DESC LIMIT ? OFFSET ?',
            (day + '%', int(limit), int(offset)))
        return rows

    def day_stats(self, day=None):
        day = day or datetime.now().strftime('%Y-%m-%d')
        r = self.store.one(
            "SELECT COUNT(*) AS turns, SUM(tx_seconds) AS tx_seconds,"
            " SUM(CASE WHEN action='sent' THEN 1 ELSE 0 END) AS sent,"
            " SUM(CASE WHEN action='skipped' THEN 1 ELSE 0 END) AS skipped,"
            " SUM(CASE WHEN truncated=1 THEN 1 ELSE 0 END) AS truncated,"
            " AVG(llm_ms) AS avg_llm, AVG(asr_ms) AS avg_asr"
            " FROM assist_turns WHERE ts LIKE ?", (day + '%',))
        return {k: (round(v, 1) if isinstance(v, float) else (v or 0))
                for k, v in (r or {}).items()}

    def status(self):
        st = self.settings()
        now = time.time()
        with self.lock:
            qsize = self.q.qsize()
            recent = list(self.recent)[-30:]
            levels = list(self.levels)[-120:]
            hist = len(self.history)
        return {
            'enabled': self.enabled(),
            'stage': self.stage,
            'stage_label': self._stage_label(),
            'stage_detail': self.stage_detail,
            'stage_seconds': round(now - self.stage_since, 1),
            'wake_words': self.wake_words(st),
            'fuzzy': _flag(st.get('assist_wake_fuzzy'), True),
            'follow_up_left': round(max(0.0, self.follow_until - now), 1),
            'history_turns': hist,
            'last_tx_ago': round(now - self.last_tx, 1) if self.last_tx else -1,
            'dbfs': round(self.last_dbfs, 1),
            'levels': levels,
            'recent': recent,
            'queue': qsize,
            'asr_last': self.asr_last,
            'llm_warm': dict(self.llm_warm,
                             age=round(now - float(self.llm_warm.get('ts') or now), 1)),
            'counters': dict(self.counters),
            'today': self.day_stats(),
            'last_error': self.last_error,
            'uptime': round(now - self.started_at, 1),
            'settings': {k: st.get(k) for k in DEFAULTS},
        }

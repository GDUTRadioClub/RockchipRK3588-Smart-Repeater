# -*- coding: utf-8 -*-
"""中继语音日志服务：BUSY/PTT 触发录音 + 异步 ASR + 智能分类 + 每日总结。

设计要点
--------
1. **单一采集中枢**：nau8822 的采集通道同一时刻只允许一个进程打开
   （实测第二路报 `Device or resource busy`），因此本模块不自己开 arecord，
   而是由 app.py 的采集中枢（`_mic_capture_loop`）把每个 16k/立体声块
   调用 `VoiceService.feed()` 喂进来，避免与网页实时对讲互抢。
2. **触发状态机**：BUSY(GPIO101) 或 PTT(GPIO97) 任一有效即起录，带 pre-roll
   （环形缓冲保留最近 N 秒）与尾音（状态结束后继续录 M 秒）。同一"会话"内
   状态组合变化（只收 / 只发 / 收发同时）会切成独立分段，共享 session_id。
3. **异步 ASR**：落盘后立刻入队，由**单线程低优先级**worker 串行处理，
   避免与 1080p 循环录像争抢 CPU（实测录像已占 ~194% CPU）。
4. **智能分类**：silero VAD 切分出语音段 → 只对语音段做 SenseVoice 识别
   并保留段级时间戳；非语音按频谱特征细分为 APRS / 单音 / 噪声 / 静音 /
   抖动，**不出文字**，避免 SenseVoice 对噪声产生幻觉（如"谢谢观看"）。
5. **每日总结**：默认本地 rkllm（NPU）分块 map-reduce，超限或指定时切外部
   OpenAI 兼容 API（DeepSeek）。本地 LLM 常驻要占 1.6GB dma-buf，因此支持
   按需启停 + 空闲自动卸载。
"""

import json
import math
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import wave
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

try:
    import numpy as np
except Exception:                                     # pragma: no cover
    np = None

try:
    import requests
except Exception:                                     # pragma: no cover
    requests = None

LOG = '[VLOG]'

# ---------------------------------------------------------------------------
# 默认设置（可在「设置 → 语音日志」里改；全部走 settings 表 vlog_* 键）
# ---------------------------------------------------------------------------
DEFAULTS = {
    'vlog_enabled': '1',
    'vlog_dir': '/opt/ai/relay_voice',
    'vlog_channel': 'left',          # left / right / mix
    'vlog_pre_roll': '3.0',
    'vlog_post_roll': '2.0',
    'vlog_min_seconds': '1.0',
    'vlog_max_seconds': '300',
    'vlog_silence_dbfs': '-48',
    'vlog_asr_enabled': '1',
    'vlog_vad_enabled': '1',
    'vlog_enhance': '1',             # 识别前去直流+高通+峰值归一化（不改存档音频）
    'vlog_keep_transient': '0',      # 1=抖动/噪声片段也保留文件
    'vlog_retention_days': '30',
    'vlog_retention_mb': '20480',
    'vlog_summary_enabled': '1',
    'vlog_summary_time': '23:30',
    'vlog_summary_provider': 'auto',  # auto / local / external
    'vlog_llm_on_demand': '1',
    'vlog_llm_idle_unload': '300',
    # 常用呼号白名单（逗号分隔）。字母解释法逐字识别会丢字（实测 BI7KHI → BI7HI），
    # 用编辑距离把它纠回白名单里的呼号。
    'vlog_callsign_whitelist': 'BI7KHI',
    'vlog_callsign_max_dist': '0',   # 0=只做「丢字」纠错；1/2 才启用编辑距离纠错（有误纠风险）
}

CATEGORY_LABEL = {
    'voice': '语音',
    'aprs': 'APRS/数据',
    'tone': '单音/信标',
    'noise': '噪声',
    'silence': '静音',
    'jitter': '静噪抖动',
    '': '待识别',
}

KIND_LABEL = {'rx': '接收', 'tx': '本机发射', 'both': '收发同时'}

# ICAO / 北约字母解释法 → 字母。中继通联里呼号普遍用字母解释法念，
# SenseVoice 会输出 "Bravo Italy number 7 Hotel India" 这种文本，
# 需要还原成 BI7HI 才能检索、统计和做日报。
ICAO_ALPHABET = {
    'alpha': 'A', 'alfa': 'A', 'bravo': 'B', 'charlie': 'C', 'delta': 'D', 'echo': 'E',
    'foxtrot': 'F', 'golf': 'G', 'hotel': 'H', 'india': 'I', 'italy': 'I', 'italia': 'I',
    'juliet': 'J', 'juliett': 'J', 'juliette': 'J', 'kilo': 'K', 'lima': 'L', 'mike': 'M',
    'november': 'N', 'oscar': 'O', 'papa': 'P', 'quebec': 'Q', 'romeo': 'R', 'sierra': 'S',
    'tango': 'T', 'uniform': 'U', 'victor': 'V', 'whiskey': 'W', 'whisky': 'W',
    'xray': 'X', 'yankee': 'Y', 'zulu': 'Z',
}
CALLSIGN_RE = re.compile(r'\b([A-Z]{1,2}\d[A-Z]{1,4})\b')
ICAO_SEQ_RE = re.compile(
    r'\b((?:' + '|'.join(sorted(ICAO_ALPHABET, key=len, reverse=True)) + r')(?:[\s,\-]+'
    r'(?:number|digit|numba)?[\s,]*(?:\d{1,2}|one|two|three|four|five|six|seven|eight|nine|zero))?'
    r'(?:[\s,\-]+(?:' + '|'.join(sorted(ICAO_ALPHABET, key=len, reverse=True)) + r')){0,5})\b',
    re.IGNORECASE)
NUM_WORD = {'zero': '0', 'one': '1', 'two': '2', 'three': '3', 'four': '4',
            'five': '5', 'six': '6', 'seven': '7', 'eight': '8', 'nine': '9'}


def extract_callsigns(text):
    """从识别文本里还原呼号（含 ICAO 字母解释法展开）。

    例：'Bravo Italy number 7, below Hotel India radio test' → ['BI7HI']
    （严格模式按连续序列匹配，宽松模式把全文所有 ICAO 词/数字按出现顺序抽出来拼接，
      用来对付字母之间夹了识别噪声词的情况。）
    """
    if not text:
        return []
    out = []

    def _add(cand):
        if cand and cand not in out:
            out.append(cand)

    # 1) 直接写出的呼号
    for m in CALLSIGN_RE.findall(text.upper()):
        _add(m)

    # 2) 严格模式：连续的 ICAO 词（可夹 number/digit + 数字），展开后再找呼号
    def expand(m):
        s = re.sub(r'\b(number|digit|numba)\b', ' ', m.group(0), flags=re.I)
        buf = []
        for p in re.split(r'[\s,\-]+', s.strip()):
            pl = p.lower()
            if pl in ICAO_ALPHABET:
                buf.append(ICAO_ALPHABET[pl])
            elif pl in NUM_WORD:
                buf.append(NUM_WORD[pl])
            elif p.isdigit():
                buf.append(p)
        return ''.join(buf)

    for m in CALLSIGN_RE.findall(ICAO_SEQ_RE.sub(lambda x: ' ' + expand(x) + ' ', text).upper()):
        _add(m)

    # 3) 宽松模式：全文所有 ICAO 词/数字按顺序拼接（忽略中间的普通词）
    letters = []
    for tok in re.findall(r"[A-Za-z]+|\d+", text):
        tl = tok.lower()
        if tl in ICAO_ALPHABET:
            letters.append(ICAO_ALPHABET[tl])
        elif tl in NUM_WORD:
            letters.append(NUM_WORD[tl])
        elif tok.isdigit():
            letters.append(tok)
    joined = ''.join(letters)
    for m in re.findall(r'[A-Z]{1,2}\d[A-Z]{1,4}', joined):
        _add(m)
    return out[:5]


def normalize_icao(text):
    """把字母解释法展开成紧凑串，便于搜索（保留原文另存）。"""
    if not text:
        return ''

    def expand(m):
        return ' ' + ''.join(
            ICAO_ALPHABET.get(p.lower(), NUM_WORD.get(p.lower(), ''))
            for p in re.split(r'[\s,\-]+', re.sub(r'\b(number|digit)\b', ' ', m.group(0),
                                                 flags=re.I).strip()))
    return ICAO_SEQ_RE.sub(expand, text).strip()


def levenshtein(a, b):
    """编辑距离（呼号纠错用，字符串很短，直接 DP）。"""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def is_subsequence(a, b):
    """a 是否为 b 的子序列（用于判断"丢字"）。"""
    it = iter(b)
    return all(ch in it for ch in a)


def correct_callsigns(cands, whitelist, max_dist=0):
    """把还原出的呼号向白名单纠错。

    实测：真实录音里 BI7KHI 被识别成 "Bravo Italy number 7, below Hotel India"，
    只还原出 BI7HI（丢了 1 个字母 K）。

    **默认只做"丢字"纠错（候选是白名单呼号的子序列）**：
    因为替换型纠错很危险 —— BG7KHI 与 BI7KHI 的编辑距离只有 1，而 BG7 是国内
    极常见前缀，按编辑距离纠错会把别的电台的呼号改错。把 max_dist 显式设为
    1/2 才会额外启用编辑距离纠错。

    返回 (corrected, fixes)。
    """
    wl = [str(x).strip().upper() for x in (whitelist or []) if str(x).strip()]
    out = []
    fixes = []
    for c in cands:
        c = str(c).upper()
        if not wl:
            out.append(c)
            continue
        if c in wl:
            out.append(c)
            continue
        # ① 丢字纠错：候选是某个白名单呼号的真子序列，且唯一命中
        subs = [w for w in wl if len(c) < len(w) and is_subsequence(c, w)]
        if len(subs) == 1:
            out.append(subs[0])
            fixes.append({'from': c, 'to': subs[0], 'dist': len(subs[0]) - len(c),
                          'mode': 'subsequence'})
            continue
        # ② 可选：编辑距离纠错（默认关闭，需显式调大 max_dist）
        if max_dist > 0:
            best, best_d = None, 999
            for w in wl:
                d = levenshtein(c, w)
                if d < best_d:
                    best, best_d = w, d
            if best and best_d <= max_dist and abs(len(best) - len(c)) <= 1 and len(c) >= 4:
                out.append(best)
                fixes.append({'from': c, 'to': best, 'dist': best_d, 'mode': 'edit'})
                continue
        out.append(c)
    seen = set()
    uniq = []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq, fixes

LLM_SERVICE = 'rkllm-server'
LLM_HEALTH_URL = 'http://127.0.0.1:8001/v1/models'
LLM_LEASE_FILE = '/tmp/elf2-llm-lease'      # 跨进程「正在使用」租约
SAMPLE_RATE = 16000
FRAME_BYTES = 2                                        # S16_LE 单声道


def _now_iso():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def _f(v, d=0.0):
    try:
        return float(v)
    except Exception:
        return d


def _flag(v, d=False):
    if v is None:
        return d
    return str(v).strip().lower() in ('1', 'true', 'yes', 'on')


# ---------------------------------------------------------------------------
# 数据库（后台线程用独立连接，WAL 允许与 Flask 并发）
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS voice_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, ts_epoch REAL, end_epoch REAL,
    session_id TEXT, seq INTEGER,
    kind TEXT, category TEXT,
    seconds REAL, rms REAL, peak INTEGER, dbfs REAL,
    filename TEXT, path TEXT, bytes INTEGER,
    asr_status TEXT DEFAULT 'pending',
    asr_text TEXT, asr_json TEXT, asr_ms INTEGER, rtf REAL,
    feature_json TEXT, note TEXT, created TEXT
);
CREATE INDEX IF NOT EXISTS idx_voice_ts ON voice_logs(ts_epoch DESC);
CREATE INDEX IF NOT EXISTS idx_voice_cat ON voice_logs(category);
CREATE INDEX IF NOT EXISTS idx_voice_sess ON voice_logs(session_id);
CREATE TABLE IF NOT EXISTS voice_daily (
    day TEXT PRIMARY KEY,
    ts TEXT, segments INTEGER, voice_count INTEGER, seconds REAL,
    transcript TEXT, summary TEXT,
    provider TEXT, model TEXT, status TEXT, error TEXT,
    elapsed REAL, chunks INTEGER, updated TEXT
);
"""


class Store:
    """voice_logs / voice_daily 的轻量访问层（线程安全，每次开新连接）。"""

    def __init__(self, db_path):
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        self.init()

    def _conn(self):
        c = sqlite3.connect(self.db_path, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('PRAGMA synchronous=NORMAL')
        return c

    def init(self):
        with self._lock:
            c = self._conn()
            try:
                c.executescript(SCHEMA)
                for col in ('callsigns', 'callsigns_raw'):
                    try:
                        c.execute('ALTER TABLE voice_logs ADD COLUMN %s TEXT' % col)
                    except Exception:
                        pass
                # 段内 APRS 位置标记。经纬度必须是 REAL：统一按 TEXT 加列会把
                # 22.5333 存成字符串，后面算距离就得处处 float() 兜底。
                for col, typ in (('aprs_call', 'TEXT'), ('aprs_lat', 'REAL'),
                                 ('aprs_lon', 'REAL'),
                                 ('aprs_pos', 'INTEGER DEFAULT 0')):
                    try:
                        c.execute('ALTER TABLE voice_logs ADD COLUMN %s %s' % (col, typ))
                    except Exception:
                        pass
                try:
                    c.execute('CREATE INDEX IF NOT EXISTS idx_voice_aprs '
                              'ON voice_logs(aprs_pos, aprs_call)')
                except Exception:
                    pass
                c.commit()
            finally:
                c.close()

    def exec(self, sql, args=()):
        with self._lock:
            c = self._conn()
            try:
                cur = c.execute(sql, args)
                c.commit()
                return cur.lastrowid
            finally:
                c.close()

    def query(self, sql, args=()):
        with self._lock:
            c = self._conn()
            try:
                return [dict(r) for r in c.execute(sql, args).fetchall()]
            finally:
                c.close()

    def one(self, sql, args=()):
        rows = self.query(sql, args)
        return rows[0] if rows else None


# ---------------------------------------------------------------------------
# 音频特征与 VAD
# ---------------------------------------------------------------------------
def read_wav_mono(path):
    """读 WAV → (float32 波形 [-1,1], 采样率)。多声道取平均。"""
    with wave.open(str(path), 'rb') as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        width = w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width == 2:
        a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif width == 1:
        a = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 4:
        a = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        raise ValueError('不支持的位宽：%d' % width)
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    return a, sr


def active_span(x, sr=SAMPLE_RATE, frame_ms=100.0, rel=0.15):
    """返回信号"活跃段" (i0, i1)；找不到返回 None。

    频谱特征必须在活跃段上算：整段含前后静音（pre-roll/尾音常占 4~5 秒）会把频谱
    稀释得很平，实测把静噪噪声误判成"单音/信标"。
    """
    n = len(x)
    step = max(1, int(sr * frame_ms / 1000.0))
    if n < step * 2:
        return None
    k = n // step
    e = np.abs(x[:k * step]).reshape(k, step)
    rms = np.sqrt((e.astype(np.float64) ** 2).mean(axis=1))
    pk = float(rms.max())
    if pk <= 0:
        return None
    idx = np.where(rms >= pk * rel)[0]
    if idx.size == 0:
        return None
    i0 = int(idx[0]) * step
    i1 = min(n, (int(idx[-1]) + 1) * step)
    if i1 - i0 < int(0.2 * sr):
        return None
    return i0, i1


def analyze(x, sr=SAMPLE_RATE):
    """时域 + 频域特征，供分类使用。"""
    n = len(x)
    out = {'seconds': round(n / float(sr), 3), 'rms': 0.0, 'peak': 0, 'dbfs': -120.0,
           'zcr': 0.0, 'tone_ratio': 0.0, 'peak_hz': 0, 'flatness': 0.0}
    if n == 0:
        return out
    rms_f = float(np.sqrt((x ** 2).mean()))
    peak_f = float(np.abs(x).max())
    # 统一用 int16 标度入库（与录像/对讲模块一致），dBFS 由浮点值算
    out['rms'] = round(rms_f * 32768.0, 1)
    out['peak'] = int(round(peak_f * 32768.0))
    out['dbfs'] = round(20.0 * math.log10(max(rms_f, 1e-6)), 1)
    if n > 1:
        out['zcr'] = round(float((np.diff(np.sign(x)) != 0).mean()), 4)
    if n < 256:
        return out

    span = active_span(x, sr)
    seg = x[span[0]:span[1]] if span else x
    out['active_seconds'] = round(len(seg) / float(sr), 2)
    if len(seg) > int(8 * sr):                      # 最长取 8 秒，避免 FFT 过大
        seg = seg[:int(8 * sr)]
    if seg.size < 256:
        return out
    win = seg * np.hanning(len(seg))
    spec = np.abs(np.fft.rfft(win)) + 1e-9
    freq = np.fft.rfftfreq(len(seg), 1.0 / sr)
    total = float(spec.sum())
    out['peak_hz'] = int(freq[int(np.argmax(spec))])
    out['active_zcr'] = round(float((np.diff(np.sign(seg)) != 0).mean()), 4)

    def band(lo, hi):
        m = (freq >= lo) & (freq < hi)
        return float(spec[m].sum())

    voice_band = band(300, 3400)
    out['voice_band'] = round(voice_band / total, 4)
    # APRS / Bell202：mark 1200Hz + space 2200Hz
    out['tone_ratio'] = round((band(1100, 1300) + band(2100, 2300)) / total, 4)
    # 频谱平坦度（噪声→接近 1，单音/语音→低）
    ps = spec ** 2
    ps = ps[ps > 0]
    if len(ps) > 8:
        out['flatness'] = round(float(np.exp(np.log(ps).mean()) / ps.mean()), 4)
    return out


class Vad:
    """silero VAD（sherpa-onnx 内置），懒加载；不可用时降级为能量门限。"""

    def __init__(self, model_path='/opt/ai/asr/silero_vad.onnx'):
        self.model_path = model_path
        self._lock = threading.Lock()
        self._cfg = None
        self._error = ''
        self.available = False

    def _ensure(self):
        with self._lock:
            if self._cfg is not None:
                return self._cfg
            if self._error:
                return None
            try:
                import sherpa_onnx
                if not Path(self.model_path).exists():
                    raise RuntimeError('未找到 VAD 模型 %s' % self.model_path)
                cfg = sherpa_onnx.VadModelConfig()
                cfg.silero_vad.model = self.model_path
                cfg.silero_vad.threshold = 0.5
                cfg.silero_vad.min_silence_duration = 0.25
                cfg.silero_vad.min_speech_duration = 0.25
                cfg.sample_rate = SAMPLE_RATE
                self._cfg = cfg
                self.available = True
                print('%s VAD 就绪：%s' % (LOG, self.model_path), flush=True)
            except Exception as e:
                self._error = '%s: %s' % (type(e).__name__, e)
                print('%s VAD 不可用（降级为能量门限）：%s' % (LOG, self._error), flush=True)
            return self._cfg

    def split(self, x, sr=SAMPLE_RATE):
        """返回 [(start_s, end_s), ...]；无语音返回 []。"""
        cfg = self._ensure()
        if cfg is None or len(x) < sr // 10:
            return []
        try:
            import sherpa_onnx
            vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=300)
            win = 512
            for i in range(0, len(x) - win, win):
                vad.accept_waveform(x[i:i + win])
            vad.flush()
            segs = []
            while not vad.empty():
                s = vad.front
                segs.append((s.start / float(sr), (s.start + len(s.samples)) / float(sr)))
                vad.pop()
            return segs
        except Exception as e:
            print('%s VAD 分割失败：%s' % (LOG, e), flush=True)
            return []


def enhance_for_asr(x, sr=SAMPLE_RATE, hp_hz=250.0, target_peak=0.72):
    """识别前的软件预处理：去直流 + 高通 + 峰值归一化。

    只用于 VAD/ASR，不改动存档 WAV。中继音频的两大干扰是：
    ① 低频轰鸣/工频（实测未接信号时 peak_hz 落在 42~513 Hz）；
    ② 电台音量飘忽导致忽大忽小。
    用 FFT 斜坡高通（避免依赖 scipy）+ 峰值归一化一次性解决。
    """
    if x is None or x.size < 256:
        return x
    x = (x - float(x.mean())).astype(np.float32)
    n = x.size
    spec = np.fft.rfft(x)
    freq = np.fft.rfftfreq(n, 1.0 / sr)
    lo = max(1.0, hp_hz * 0.32)
    gain = np.clip((freq - lo) / max(1.0, hp_hz - lo), 0.0, 1.0)
    y = np.fft.irfft(spec * gain, n=n).astype(np.float32)
    pk = float(np.abs(y).max())
    if pk > 1e-6:
        y = y * (target_peak / pk)          # 太响压下来、太轻推上去
    return np.clip(y, -1.0, 1.0).astype(np.float32)


def merge_segments(segs, gap=0.45, pad=0.15, max_len=12.0):
    """合并相邻语音段（间隔小于 gap 认为同一句），并限制单句最大长度。"""
    if not segs:
        return []
    segs = sorted(segs)
    out = [list(segs[0])]
    for a, b in segs[1:]:
        if a - out[-1][1] <= gap and (b - out[-1][0]) <= max_len:
            out[-1][1] = b
        else:
            out.append([a, b])
    res = []
    for a, b in out:
        a = max(0.0, a - pad)
        b = b + pad
        # 超长句切块，避免单次 ASR 过长
        while b - a > max_len:
            res.append((a, a + max_len))
            a += max_len
        if b - a >= 0.3:
            res.append((a, b))
    return res


# ---------------------------------------------------------------------------
# 录音状态机
# ---------------------------------------------------------------------------
class Segment:
    __slots__ = ('path', 'fh', 'kind', 'start_ts', 'frames', 'sumsq', 'peak', 'seq')

    def __init__(self, path, fh, kind, start_ts, seq):
        self.path = path
        self.fh = fh
        self.kind = kind
        self.start_ts = start_ts
        self.frames = 0
        self.sumsq = 0.0
        self.peak = 0
        self.seq = seq


class Recorder:
    """BUSY/PTT 驱动的录音状态机（由采集中枢线程调用 feed）。"""

    def __init__(self, svc):
        self.svc = svc
        self.lock = threading.RLock()
        self.active = False
        self.session_id = ''
        self.seq = 0
        self.seg = None
        self.tail_until = 0.0
        self.rx = False
        self.tx = False
        self.pre = deque()
        self.pre_bytes = 0
        self.stats = {'sessions': 0, 'segments': 0, 'seconds': 0.0, 'dropped': 0,
                      'last_start': 0.0, 'last_kind': '', 'error': ''}

    # -- 采集块入口 --------------------------------------------------------
    def feed(self, raw_stereo, ts):
        st = self.svc.settings()
        try:
            mono = self._pick(raw_stereo, st.get('vlog_channel', 'left'))
        except Exception as e:
            self.stats['error'] = '%s: %s' % (type(e).__name__, e)
            return
        if not mono:
            return
        rx = bool(self.svc.get_rx())
        tx = bool(self.svc.get_tx())
        with self.lock:
            try:
                self._drive(mono, ts, rx, tx, st)
            except Exception as e:
                self.stats['error'] = '%s: %s' % (type(e).__name__, e)

    @staticmethod
    def _pick(raw, channel):
        """从 16k/立体声 S16_LE 里取指定声道，返回单声道 bytes。"""
        usable = len(raw) - (len(raw) % 4)
        if usable <= 0:
            return b''
        raw = raw[:usable]
        if channel == 'mix':
            a = np.frombuffer(raw, dtype=np.int16).reshape(-1, 2).astype(np.int32)
            m = ((a[:, 0] + a[:, 1]) // 2).astype(np.int16)
            return m.tobytes()
        idx = 0 if channel != 'right' else 1
        a = np.frombuffer(raw, dtype=np.int16).reshape(-1, 2)[:, idx]
        return np.ascontiguousarray(a).tobytes()

    def _push_pre(self, ts, mono):
        self.pre.append((ts, mono))
        self.pre_bytes += len(mono)
        cap = int(max(0.0, _f(self.svc.settings().get('vlog_pre_roll'), 3.0)) * SAMPLE_RATE) * FRAME_BYTES
        while self.pre_bytes > cap and self.pre:
            _, old = self.pre.popleft()
            self.pre_bytes -= len(old)

    def _reset_pre(self):
        self.pre.clear()
        self.pre_bytes = 0

    @staticmethod
    def _kind(rx, tx):
        if rx and tx:
            return 'both'
        return 'tx' if tx else 'rx'

    def _drive(self, mono, ts, rx, tx, st):
        want = rx or tx
        if not self.active:
            self._push_pre(ts, mono)
            if want:
                self._start_session(ts, rx, tx, st)
            return

        max_len = max(10.0, _f(st.get('vlog_max_seconds'), 300.0))

        if not want:
            # 尾音阶段：状态已结束，继续把当前分段写完，不再按 kind 切段
            if self.tail_until <= 0.0:
                self.tail_until = ts + max(0.0, _f(st.get('vlog_post_roll'), 2.0))
            if ts >= self.tail_until:
                self._close_segment(ts)
                self.active = False
                self.tail_until = 0.0
                self._reset_pre()
                return
            if self.seg is not None:
                if (ts - self.seg.start_ts) >= max_len:
                    # 尾音太长（超过单段上限）就直接收尾
                    self._close_segment(ts)
                    self.active = False
                    self.tail_until = 0.0
                    self._reset_pre()
                    return
                self._write(mono)
            return

        self.tail_until = 0.0
        kind = self._kind(rx, tx)
        if self.seg is not None:
            if self.seg.kind != kind or (ts - self.seg.start_ts) >= max_len:
                self._close_segment(ts)
                self._open_segment(ts, kind, st)
        if self.seg is not None:
            self._write(mono)

    def _start_session(self, ts, rx, tx, st):
        self.active = True
        self.seq = 0
        self.session_id = time.strftime('%Y%m%d_%H%M%S', time.localtime(ts))
        self.tail_until = 0.0
        self.stats['sessions'] += 1
        self.stats['last_start'] = ts
        kind = self._kind(rx, tx)
        self.stats['last_kind'] = kind
        self._open_segment(ts, kind, st)
        # 把 pre-roll 补进去
        while self.pre:
            _, old = self.pre.popleft()
            self._write(old)
        self.pre_bytes = 0

    def _open_segment(self, ts, kind, st):
        base = Path(st.get('vlog_dir') or DEFAULTS['vlog_dir'])
        day = time.strftime('%Y-%m-%d', time.localtime(ts))
        d = base / day
        d.mkdir(parents=True, exist_ok=True)
        seq = self.seq
        self.seq += 1
        name = 'vlog_%s_%s_%02d.wav' % (time.strftime('%H%M%S', time.localtime(ts)),
                                        kind, seq)
        path = d / name
        fh = wave.open(str(path), 'wb')
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(SAMPLE_RATE)
        self.seg = Segment(path, fh, kind, ts, seq)
        self.rx, self.tx = bool(self.svc.get_rx()), bool(self.svc.get_tx())

    def _write(self, mono):
        if self.seg is None or not mono:
            return
        try:
            self.seg.fh.writeframes(mono)
        except Exception as e:
            self.stats['error'] = '写入失败 %s: %s' % (self.seg.path, e)
            return
        a = np.frombuffer(mono, dtype=np.int16).astype(np.float32)
        self.seg.frames += len(a)
        self.seg.sumsq += float((a ** 2).sum())
        p = float(np.abs(a).max()) if len(a) else 0.0
        if p > self.seg.peak:
            self.seg.peak = int(p)

    def _close_segment(self, ts):
        seg = self.seg
        self.seg = None
        if seg is None:
            return
        try:
            seg.fh.close()
        except Exception:
            pass
        frames = seg.frames
        if frames <= 0:
            try:
                Path(seg.path).unlink()
            except Exception:
                pass
            return
        seconds = frames / float(SAMPLE_RATE)
        rms = math.sqrt(seg.sumsq / frames) if frames else 0.0
        dbfs = 20.0 * math.log10(max(rms, 1e-6) / 32768.0)
        st = self.svc.settings()
        min_s = max(0.0, _f(st.get('vlog_min_seconds'), 1.0))
        keep_transient = _flag(st.get('vlog_keep_transient'), False)
        try:
            size = Path(seg.path).stat().st_size
        except Exception:
            size = 0
        category = 'jitter' if seconds < min_s else ''
        if category == 'jitter' and not keep_transient:
            # 抖动片段默认只计数不落盘（避免日志被静噪毛刺淹没）
            try:
                Path(seg.path).unlink()
            except Exception:
                pass
            self.svc.counters['jitter'] = int(self.svc.counters.get('jitter', 0)) + 1
            self.svc.counters['jitter_seconds'] = round(
                float(self.svc.counters.get('jitter_seconds', 0.0)) + seconds, 1)
            self.stats['segments'] += 1
            return
        # 段内 APRS 位置标记：录完就判一次，不依赖 ASR（关掉 ASR 也要能标记）。
        # 尾音信标通常在本段录音窗口内先解出来，所以这里多数情况已经能命中；
        # ASR 之后还会再刷一次，兜住「解包比收段慢一点」的竞态。
        pos, pcall, plat, plon = self.svc._fill_aprs_pos(
            seg.start_ts, seconds, seg.kind)
        rid = self.svc.store.exec(
            'INSERT INTO voice_logs(ts,ts_epoch,end_epoch,session_id,seq,kind,category,'
            'seconds,rms,peak,dbfs,filename,path,bytes,asr_status,created,'
            'aprs_pos,aprs_call,aprs_lat,aprs_lon) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (datetime.fromtimestamp(seg.start_ts).astimezone().isoformat(timespec='seconds'),
             seg.start_ts, ts, self.session_id, seg.seq, seg.kind, category,
             round(seconds, 2), round(rms, 1), seg.peak, round(dbfs, 1),
             Path(seg.path).name, str(seg.path), size, 'pending', _now_iso(),
             pos, pcall, plat, plon))
        self.stats['segments'] += 1
        self.stats['seconds'] = round(float(self.stats['seconds']) + seconds, 1)
        self.svc.counters['segments'] = int(self.svc.counters.get('segments', 0)) + 1
        if _flag(st.get('vlog_asr_enabled'), True) and rid:
            self.svc.enqueue_asr(rid)

    def force_close(self):
        with self.lock:
            if self.seg is not None:
                self._close_segment(time.time())
            self.active = False
            self.tail_until = 0.0
            self._reset_pre()


# ---------------------------------------------------------------------------
# 每日总结
# ---------------------------------------------------------------------------
MAP_PROMPT = (
    '把下面这段无线电中继的通联语音转写压缩成不超过 80 字的要点。\n'
    '要求：只输出要点本身；不要提问；不要复述原文；不要编造原文里没有的呼号或地点。\n'
    '转写：\n%s\n要点：'
)

REDUCE_PROMPT = (
    '下面是同一天若干时段的中继通联摘要，请合并成一份不超过 200 字的当日简报。\n'
    '要求：只输出简报正文，不要提问，不要复述原文；'
    '按「通联概况」「主要通联」「异常与干扰」三段写，没有内容的段可省略。\n'
    '摘要：\n%s\n简报：'
)


# ---------------------------------------------------------------------------
# 共用识别入口：语音日志与中继语音助手都走这里
# ---------------------------------------------------------------------------
_SHARED_VAD = None
_SHARED_VAD_LOCK = threading.Lock()


def shared_vad():
    """全局唯一的 silero VAD 实例（懒加载）。"""
    global _SHARED_VAD
    with _SHARED_VAD_LOCK:
        if _SHARED_VAD is None:
            _SHARED_VAD = Vad()
        return _SHARED_VAD


def transcribe_pcm(x, sr=SAMPLE_RATE, enhance=True, min_seconds=0.30,
                   vad_empty_fallback=False):
    """把一段单声道波形按**语音日志的同一套流程**识别。

    返回 (segments, asr_ms)：
        segments = [{'start','end','text'}, ...]，按时间升序
        asr_ms   = 该段累计识别耗时

    这是语音日志与中继语音助手共用的**唯一识别入口**。之所以要共用：
    实测同一条录音在两处识别出不同结果（一处「中继台…」一处「一台…」），
    原因就是两边各写了一套预处理/VAD/合并逻辑并逐渐漂移。
    """
    import asr_service
    if x is None or len(x) < int(0.10 * sr):
        return [], 0
    xa = enhance_for_asr(x, sr) if enhance else x
    total_ms = 0

    def _one(sig):
        nonlocal total_ms
        res = asr_service.transcribe_samples(sig, sr)
        total_ms += int(res.get('ms') or 0)
        return (res.get('text') or '').strip() if res.get('ok') else ''

    segs = shared_vad().split(xa, sr)
    if not segs:
        # vad_empty_fallback：VAD 判空时整段送识别。
        #   助手必须开 —— 只有 0.5~0.8 秒的「中继台」偶尔被 silero 判成非语音，
        #     丢了就永远唤不醒；
        #   语音日志必须关 —— 那里宁可不写也不写垃圾（实测开着的后果是
        #     单音/信标段被写进一个「.」，污染归档）。
        if not vad_empty_fallback:
            return [], total_ms
        t = _one(xa)
        return ([{'start': 0.0, 'end': round(len(x) / float(sr), 2), 'text': t}]
                if t else []), total_ms
    out = []
    for (a, b) in merge_segments(segs):
        i0 = max(0, int(a * sr))
        i1 = min(len(xa), int(b * sr))
        if i1 - i0 < int(max(0.05, min_seconds) * sr):
            continue
        t = _one(xa[i0:i1])
        if t:
            out.append({'start': round(a, 2), 'end': round(b, 2), 'text': t})
    return out, total_ms


def transcribe_pcm_text(x, sr=SAMPLE_RATE, enhance=True, min_seconds=0.30,
                        vad_empty_fallback=False):
    """只要拼接后的文本。"""
    segs, ms = transcribe_pcm(x, sr, enhance=enhance, min_seconds=min_seconds,
                              vad_empty_fallback=vad_empty_fallback)
    return ' '.join(s['text'] for s in segs).strip(), ms


def chunk_text(text, size):
    """按行切块，尽量不切断一行。"""
    parts = []
    cur = []
    cur_len = 0
    for line in text.split('\n'):
        ln = len(line) + 1
        if cur and cur_len + ln > size:
            parts.append('\n'.join(cur))
            cur, cur_len = [], 0
        cur.append(line)
        cur_len += ln
    if cur:
        parts.append('\n'.join(cur))
    return parts or ['']


def llm_active():
    try:
        r = subprocess.run(['systemctl', 'is-active', LLM_SERVICE],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
        return r.stdout.decode('utf-8', 'replace').strip() == 'active'
    except Exception:
        return False


def llm_ready(timeout=2.0):
    """真正可用 = HTTP 接口能应答。

    注意：rkllm-server 的 systemd 单元是 Type=simple，进程一起来 systemd 就报
    active，但此时模型还没加载完、8001 端口还没绑定。必须用 HTTP 探活，
    否则第一次请求会直接 Connection refused。
    """
    if requests is None:
        return llm_active()
    try:
        r = requests.get(LLM_HEALTH_URL, timeout=timeout)
        return r.status_code < 500
    except Exception:
        return False


def llm_lease(seconds=600.0):
    """申请/续期「本地 LLM 正在使用」租约。

    空闲卸载可能发生在**另一个进程**里（网页服务与一次性脚本各自持有一个
    VoiceService 实例，互相看不见对方的 last_llm_used），实测出现过日报
    生成到一半被网页服务的维护线程 stop 掉。因此用文件租约做跨进程协调。
    """
    try:
        Path(LLM_LEASE_FILE).write_text(str(time.time() + float(seconds)), encoding='utf-8')
    except Exception:
        pass


def llm_lease_until():
    try:
        return float(Path(LLM_LEASE_FILE).read_text(encoding='utf-8').strip() or 0.0)
    except Exception:
        return 0.0


def llm_start(wait=90.0):
    llm_lease(max(180.0, wait + 60.0))
    if llm_ready():
        return True
    if not llm_active():
        try:
            subprocess.run(['sudo', '-n', 'systemctl', 'start', LLM_SERVICE],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        except Exception as e:
            print('%s 启动本地 LLM 失败：%s' % (LOG, e), flush=True)
            return False
    t0 = time.time()
    while time.time() - t0 < wait:
        if llm_ready():
            return True
        llm_lease(120.0)
        time.sleep(1.5)
    print('%s 本地 LLM 等待就绪超时（%.0fs）' % (LOG, wait), flush=True)
    return False


def llm_stop():
    if not llm_active():
        return True
    try:
        subprocess.run(['sudo', '-n', 'systemctl', 'stop', LLM_SERVICE],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        return True
    except Exception as e:
        print('%s 停止本地 LLM 失败：%s' % (LOG, e), flush=True)
        return False


def llm_chat(cfg, prompt, max_tokens=420, temperature=0.25, timeout=300, lease=900.0):
    """调用 OpenAI 兼容接口，返回文本。cfg 来自 provider_config()。"""
    if requests is None:
        raise RuntimeError('requests 不可用')
    url = (cfg or {}).get('url') or ''
    if not url:
        raise RuntimeError('未配置 LLM base_url（provider=%s）' % (cfg or {}).get('provider'))
    llm_lease(lease)          # 请求期间禁止被空闲卸载抢停
    headers = {'Content-Type': 'application/json'}
    key = (cfg or {}).get('api_key') or ''
    if key:
        headers['Authorization'] = 'Bearer ' + key

    def _post(p):
        body = {
            'model': cfg.get('model') or 'qwen2.5-1.5b',
            'messages': [{'role': 'user', 'content': p}],
            'max_tokens': max_tokens,
            'temperature': temperature,
            'stream': False,
        }
        resp = requests.post(url, json=body, headers=headers, timeout=timeout)
        if resp.status_code >= 400:
            raise RuntimeError('HTTP %s: %s' % (resp.status_code, resp.text[:200]))
        d = resp.json()
        try:
            return (d['choices'][0]['message']['content'] or '').strip()
        except Exception:
            raise RuntimeError('返回结构异常：%s' % str(d)[:200])

    try:
        text = _post(prompt)
    finally:
        llm_lease(lease)
    # 本地 rkllm 在提示词打满上下文时会直接返回空串，截断后重试一次
    if not text and len(prompt) > 1200:
        shorter = prompt[:int(len(prompt) * 0.6)]
        print('%s LLM 空输出，截断到 %d 字重试' % (LOG, len(shorter)), flush=True)
        llm_lease(lease)
        try:
            text = _post(shorter)
        finally:
            llm_lease(lease)
    return text


# ---------------------------------------------------------------------------
# 服务门面
# ---------------------------------------------------------------------------
class VoiceService:
    def __init__(self, db_path):
        self.store = Store(db_path)
        self.lock = threading.RLock()
        self.counters = {'segments': 0, 'jitter': 0, 'jitter_seconds': 0.0,
                         'asr_done': 0, 'asr_error': 0, 'asr_skip': 0,
                         'seconds': 0.0}
        self.settings_cache = {}
        self.settings_ts = 0.0
        self.get_rx = lambda: False
        self.get_tx = lambda: False
        self.provider_config = lambda p=None: {}
        self.recorder = Recorder(self)
        self.vad = Vad()
        self.asr_q = queue.Queue(maxsize=500)
        self.asr_running = False
        self.asr_current = {}
        self.asr_last = {}
        self.asr_error = ''
        self.summary_state = {'running': False, 'day': '', 'stage': '', 'error': '',
                              'elapsed': 0.0, 'chunks': 0, 'ts': 0.0}
        self.last_llm_used = 0.0
        self.started_at = time.time()
        self._threads_started = False
        self.started = False
        # 目录用量扫描是 O(文件数)，而 status() 被前端每 3s 轮询一次：
        # 加一层短 TTL 缓存，避免反复遍历整个语音日志目录。
        self._usage_lock = threading.Lock()
        self._usage_cache = None
        self._usage_ttl = 15.0

    # -- 配置/依赖注入 -----------------------------------------------------
    def configure(self, get_rx, get_tx, provider_config, setting_getter=None,
                  log=None):
        self.get_rx = get_rx or (lambda: False)
        self.get_tx = get_tx or (lambda: False)
        self.provider_config = provider_config or (lambda p=None: {})
        self._setting_getter = setting_getter
        self._log = log

    def settings(self, force=False):
        now = time.time()
        with self.lock:
            if not force and self.settings_cache and (now - self.settings_ts) < 5.0:
                return self.settings_cache
        st = dict(DEFAULTS)
        try:
            if self._setting_getter:
                rows = self._setting_getter()
                if isinstance(rows, dict):
                    st.update({k: v for k, v in rows.items() if v is not None})
            else:
                rows = self.store.query("SELECT key,value FROM settings WHERE key LIKE 'vlog_%'")
                st.update({r['key']: r['value'] for r in rows})
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
        return _flag(self.settings().get('vlog_enabled'), True)

    # -- 启动 --------------------------------------------------------------
    def start(self):
        if self._threads_started:
            return
        self._threads_started = True
        for fn, name in ((self._asr_worker, 'vlog-asr'),
                         (self._maintenance, 'vlog-maint'),
                         (self._summary_scheduler, 'vlog-summy')):
            t = threading.Thread(target=fn, name=name, daemon=True)
            t.start()
        print('%s 服务已启动（目录 %s）' % (LOG, self.settings().get('vlog_dir')), flush=True)

    # -- 采集入口 ----------------------------------------------------------
    def feed(self, raw_stereo, ts=None):
        if not self.enabled():
            return
        try:
            self.recorder.feed(raw_stereo, ts or time.time())
        except Exception as e:
            self.recorder.stats['error'] = '%s: %s' % (type(e).__name__, e)

    def enqueue_asr(self, rid):
        try:
            self.asr_q.put_nowait(rid)
        except queue.Full:
            self.counters['asr_skip'] = int(self.counters.get('asr_skip', 0)) + 1
            try:
                self.store.exec("UPDATE voice_logs SET asr_status='skip' WHERE id=?", (rid,))
            except Exception:
                pass

    # -- ASR worker --------------------------------------------------------
    def _asr_worker(self):
        try:
            os.nice(10)
        except Exception:
            pass
        while True:
            rid = self.asr_q.get()
            if rid is None:
                continue
            self.asr_running = True
            try:
                self.transcribe_one(rid)
            except Exception as e:
                self.asr_error = '%s: %s' % (type(e).__name__, e)
                print('%s ASR 处理 #%s 失败：%s' % (LOG, rid, self.asr_error), flush=True)
                try:
                    self.store.exec("UPDATE voice_logs SET asr_status='error' WHERE id=?", (rid,))
                except Exception:
                    pass
                self.counters['asr_error'] = int(self.counters.get('asr_error', 0)) + 1
            finally:
                self.asr_running = False
                self.asr_current = {}
                self.asr_q.task_done()

    def transcribe_one(self, rid):
        row = self.store.one('SELECT * FROM voice_logs WHERE id=?', (rid,))
        if not row:
            return
        st = self.settings()
        path = row.get('path') or ''
        if not path or not Path(path).exists():
            self.store.exec("UPDATE voice_logs SET asr_status='error',note='文件缺失' WHERE id=?", (rid,))
            return
        self.asr_current = {'id': rid, 'file': Path(path).name, 'since': time.time()}
        self.store.exec("UPDATE voice_logs SET asr_status='running' WHERE id=?", (rid,))
        import asr_service
        x, sr = read_wav_mono(path)
        feat = analyze(x, sr)          # 特征/电平按原始录音算，保证 dBFS 反映实际存档
        xa = enhance_for_asr(x, sr) if _flag(st.get('vlog_enhance'), True) else x
        min_s = max(0.0, _f(st.get('vlog_min_seconds'), 1.0))
        silence_db = _f(st.get('vlog_silence_dbfs'), -48.0)
        # VAD 切分现在在 transcribe_pcm 内部完成，这里不再预切
        segs = []
        # 走共用识别入口（与中继语音助手同一份实现，避免两处逻辑漂移）。
        # 注意：segs 为空也照样送识别——VAD 可能把短句判成非语音，不能丢。
        texts = []
        asr_ms = 0
        rtf = 0.0
        if _flag(st.get('vlog_asr_enabled'), True):
            texts, asr_ms = transcribe_pcm(
                x, sr, enhance=_flag(st.get('vlog_enhance'), True), min_seconds=0.30)
        text = ' '.join(t['text'] for t in texts)
        raw_calls = extract_callsigns(text) if text else []
        wl = [x for x in re.split(r'[,;\s]+', st.get('vlog_callsign_whitelist') or '') if x]
        calls, fixes = correct_callsigns(
            raw_calls, wl, int(_f(st.get('vlog_callsign_max_dist'), 0)))
        if calls:
            msg = '%s #%s 还原呼号：%s' % (LOG, rid, ','.join(calls))
            if fixes:
                msg += '（纠错 %s）' % '; '.join(
                    '%s→%s(d=%d)' % (f['from'], f['to'], f['dist']) for f in fixes)
            print(msg, flush=True)
        if asr_ms and x.size:
            rtf = round(asr_ms / 1000.0 / (len(x) / float(sr)), 3)
        category = row.get('category') or ''
        if not category:
            if feat['seconds'] < min_s:
                category = 'jitter'
            elif text:
                category = 'voice'
            elif self._aprs_overlap(row.get('ts_epoch'), row.get('seconds'),
                                    row.get('kind') or ''):
                # APRS：优先用收发时间交叉判定。放在 text 之后，确保「人在说话」
                # 的段永远归语音；放在其它启发式之前，避开削顶导致的误判。
                category = 'aprs'
            elif feat['dbfs'] <= silence_db:
                category = 'silence'
            elif feat.get('tone_ratio', 0) >= 0.45:
                category = 'aprs'
            elif feat.get('flatness', 1.0) <= 0.25:
                category = 'tone'
            else:
                category = 'noise'
        status = 'done' if _flag(st.get('vlog_asr_enabled'), True) else 'disabled'
        if category in ('jitter', 'silence'):
            status = 'skip'
        # 再刷一次位置标记：解包可能比收段慢一拍，落段时那次没命中，这里补上。
        # 只增不减——已经有标记的段不因为这次没查到就被抹掉。
        pos, pcall, plat, plon = self._fill_aprs_pos(
            row.get('ts_epoch'), row.get('seconds'), row.get('kind') or '')
        if not pos and (row.get('aprs_pos') or 0):
            pos, pcall, plat, plon = (row.get('aprs_pos'), row.get('aprs_call'),
                                      row.get('aprs_lat'), row.get('aprs_lon'))
        self.store.exec(
            'UPDATE voice_logs SET category=?, asr_status=?, asr_text=?, asr_json=?, '
            'asr_ms=?, rtf=?, feature_json=?, rms=?, peak=?, dbfs=?, '
            'callsigns=?, callsigns_raw=?, aprs_pos=?, aprs_call=?, aprs_lat=?, '
            'aprs_lon=? WHERE id=?',
            (category, status, text or '', json.dumps(texts, ensure_ascii=False) if texts else '',
             asr_ms, rtf, json.dumps(feat, ensure_ascii=False),
             feat['rms'], feat['peak'], feat['dbfs'], ','.join(calls),
             ','.join(raw_calls), pos, pcall, plat, plon, rid))
        self.counters['asr_done'] = int(self.counters.get('asr_done', 0)) + 1
        self.asr_last = {'id': rid, 'category': category, 'seconds': feat['seconds'],
                         'ms': asr_ms, 'rtf': rtf, 'text': text[:80],
                         'ts': time.strftime('%H:%M:%S')}
        print('%s #%s %s %.1fs → %s（%d字, %dms）' % (
            LOG, rid, CATEGORY_LABEL.get(category, category), feat['seconds'],
            CATEGORY_LABEL.get(category, category), len(text), asr_ms), flush=True)

    def reclassify_aprs(self, day=None, dry=False):
        """对历史记录重跑 APRS 时间交叉判定，修正被削顶带偏的分类。

        只做「改成 aprs」或「从 aprs 改回启发式结果」，不重跑 ASR，代价极低。

        顺带**回填位置标记**：这一步要覆盖**有识别文字的段**——「人说话 + 尾音
        带位置信标」正是它的主体，而下面的分类循环会 continue 掉这些段。
        """
        cols = ('id,category,kind,ts_epoch,seconds,asr_text,path,feature_json,'
                'aprs_pos,aprs_call')
        base = self.store.query(
            'SELECT %s FROM voice_logs WHERE substr(ts,1,10)=? OR ? IS NULL' % cols,
            (day or '', day)) if day else self.store.query(
            'SELECT %s FROM voice_logs' % cols)
        changed = []
        for r in base:
            if (r.get('asr_text') or '').strip():
                continue                       # 有识别文字的是语音，不动分类
            hit = self._aprs_overlap(r.get('ts_epoch'), r.get('seconds'),
                                     r.get('kind') or '')
            old = r.get('category') or ''
            if hit and old != 'aprs':
                changed.append((r['id'], old, 'aprs'))
            elif (not hit) and old == 'aprs':
                changed.append((r['id'], old, 'tone'))
        pos_n = 0
        for r in base:
            pos, pcall, plat, plon = self._fill_aprs_pos(
                r.get('ts_epoch'), r.get('seconds'), r.get('kind') or '')
            if not pos:
                continue
            if int(r.get('aprs_pos') or 0) and (r.get('aprs_call') or '') == pcall:
                continue                       # 已经标好且没变，不用写
            pos_n += 1
            if not dry:
                self.store.exec(
                    'UPDATE voice_logs SET aprs_pos=?,aprs_call=?,aprs_lat=?,'
                    'aprs_lon=? WHERE id=?', (pos, pcall, plat, plon, r['id']))
        if not dry:
            for rid, _old, new in changed:
                self.store.exec('UPDATE voice_logs SET category=? WHERE id=?', (new, rid))
        if pos_n:
            print('%s 位置标记%s：%d 段' % (LOG, '预演' if dry else '完成', pos_n),
                  flush=True)
        return changed

    def retranscribe(self, rid):
        self.store.exec("UPDATE voice_logs SET asr_status='pending', category='' WHERE id=?", (rid,))
        self.enqueue_asr(rid)

    # -- 维护：清理 / 每日总结调度 ----------------------------------------
    def _maintenance(self):
        time.sleep(20)
        last_cleanup = 0.0
        while True:
            try:
                now = time.time()
                if now - last_cleanup > 600:
                    last_cleanup = now
                    self.cleanup()
                self._maybe_unload_llm(now)
            except Exception as e:
                print('%s 维护线程异常：%s' % (LOG, e), flush=True)
            time.sleep(30)

    def cleanup(self):
        st = self.settings()
        base = Path(st.get('vlog_dir') or DEFAULTS['vlog_dir'])
        if not base.exists():
            return 0
        days = int(_f(st.get('vlog_retention_days'), 30) or 30)
        max_mb = int(_f(st.get('vlog_retention_mb'), 20480) or 20480)
        cutoff = time.time() - days * 86400
        removed = 0
        rows = self.store.query('SELECT id,path,bytes,ts_epoch FROM voice_logs ORDER BY ts_epoch DESC')
        total = 0
        for r in rows:
            p = r.get('path') or ''
            sz = int(r.get('bytes') or 0)
            stale = _f(r.get('ts_epoch'), 0) < cutoff
            over = (total + sz) > max_mb * 1024 * 1024
            if stale or over:
                try:
                    if p and Path(p).exists():
                        Path(p).unlink()
                except Exception:
                    pass
                self.store.exec('DELETE FROM voice_logs WHERE id=?', (r['id'],))
                removed += 1
                continue
            total += sz
        # 清掉磁盘上没有记录的残留（例如进程被杀）
        known = set()
        for r in self.store.query('SELECT path FROM voice_logs'):
            known.add(r.get('path') or '')
        for f in base.rglob('*.wav'):
            if str(f) not in known:
                try:
                    if time.time() - f.stat().st_mtime > 3600:
                        f.unlink()
                        removed += 1
                except Exception:
                    pass
        if removed:
            print('%s 清理完成：删除 %d 个文件/记录，保留 %.1f MB' % (LOG, removed, total / 1048576.0),
                  flush=True)
        self.counters['retention_mb'] = round(total / 1048576.0, 1)
        return removed

    def ensure_llm_ready(self, wait=60.0):
        """确保本地 LLM 可用（按需加载）；返回 (ok, msg)。

        本地 rkllm 常驻要占约 2.3GB RSS / 1.6GB dma-buf，会挤占录音与 ASR
        的可用内存，所以默认按需启停：网页对话或日报要用时再拉起。
        """
        idle = max(60.0, _f(self.settings().get('vlog_llm_idle_unload'), 300.0))
        llm_lease(max(600.0, idle * 2))
        self.last_llm_used = time.time()      # 先占位，防止空闲卸载线程抢停
        if llm_ready():
            return True, ''
        if not _flag(self.settings().get('vlog_llm_on_demand'), True):
            return False, '本地 LLM 未运行，且未开启「按需启动」'
        if llm_start(wait=wait):
            self.last_llm_used = time.time()
            return True, ''
        return False, '本地 LLM 启动超时（检查 rkllm-server 服务与 sudoers 免密规则）'

    def _last_llm_activity(self):
        """最近一次 LLM 使用时间（取本进程记录与 llm_stats 表的最大值）。"""
        last = float(self.last_llm_used or 0.0)
        try:
            row = self.store.one('SELECT ts FROM llm_stats ORDER BY id DESC LIMIT 1')
            if row and row.get('ts'):
                t = datetime.fromisoformat(row['ts'])
                e = t.timestamp()
                if e > last:
                    last = e
        except Exception:
            return None
        if last <= 0:
            # 从未有人用过：以服务启动时间为基准，避免开机常驻白占内存
            last = self.started_at
        return last

    def _maybe_unload_llm(self, now):
        st = self.settings()
        if not _flag(st.get('vlog_llm_on_demand'), True):
            return
        if not llm_active():
            return
        idle = max(60.0, _f(st.get('vlog_llm_idle_unload'), 300.0))
        if llm_lease_until() > now:
            return                      # 有进程正在用（含其它进程），不许停
        last = self._last_llm_activity()
        if last is None:
            return
        if now - last > idle:
            print('%s 本地 LLM 空闲 %.0fs，卸载以释放内存（约 2.3GB）' % (LOG, now - last),
                  flush=True)
            llm_stop()

    def _summary_scheduler(self):
        time.sleep(45)
        done_days = set()
        while True:
            try:
                st = self.settings()
                if _flag(st.get('vlog_summary_enabled'), True):
                    hhmm = str(st.get('vlog_summary_time') or '23:30')
                    now = datetime.now()
                    today = now.strftime('%Y-%m-%d')
                    want = now.strftime('%H:%M')
                    if want == hhmm and today not in done_days:
                        done_days.add(today)
                        print('%s 到达每日总结时间 %s，开始生成日报' % (LOG, hhmm), flush=True)
                        self.run_summary(today)
            except Exception as e:
                print('%s 总结调度异常：%s' % (LOG, e), flush=True)
            time.sleep(25)

    # -- 日报生成 ----------------------------------------------------------
    def day_transcript(self, day):
        rows = self.store.query(
            "SELECT * FROM voice_logs WHERE substr(ts,1,10)=? AND category='voice' "
            "AND asr_text IS NOT NULL AND asr_text<>'' ORDER BY ts_epoch ASC", (day,))
        lines = []
        for r in rows:
            t = (r.get('ts') or '')[11:19]
            lines.append('[%s] %s：%s' % (t, KIND_LABEL.get(r.get('kind'), r.get('kind') or ''),
                                          (r.get('asr_text') or '').strip()))
        stat = self.store.one(
            "SELECT COUNT(*) AS n, SUM(seconds) AS sec FROM voice_logs WHERE substr(ts,1,10)=?",
            (day,)) or {}
        cat = self.store.query(
            "SELECT category, COUNT(*) AS n, SUM(seconds) AS sec FROM voice_logs "
            "WHERE substr(ts,1,10)=? GROUP BY category ORDER BY n DESC", (day,))
        return {'day': day, 'lines': lines, 'text': '\n'.join(lines),
                'voice_count': len(lines), 'total': int(stat.get('n') or 0),
                'seconds': round(float(stat.get('sec') or 0.0), 1), 'cats': cat}

    def _pick_provider(self, text_len, want=None):
        st = self.settings()
        want = (want or st.get('vlog_summary_provider') or 'auto').strip().lower()
        local = self.provider_config('local')
        external = self.provider_config('external')
        if want == 'local':
            return local, 'local'
        if want == 'external':
            return external, 'external'
        # auto：短文本用本地，长文本若已配外部则用外部
        if text_len > 5000 and (external or {}).get('api_key') and (external or {}).get('url'):
            return external, 'external'
        if (local or {}).get('url'):
            return local, 'local'
        return (external if (external or {}).get('url') else local), 'external'

    def _reduce_summaries(self, cfg, items, pname):
        """分层把分段摘要合并成日报。

        本地 1.5B 模型上下文只有 4096 token，摘要一多就必须先分组再合并；
        单块时外层会直接采用 map 结果，不走这里。
        """
        limit = 3000 if pname == 'local' else 40000
        for _ in range(3):
            body = '\n'.join('- %s' % x for x in items)
            if len(body) <= limit:
                self.summary_state['stage'] = '汇总日报'
                return llm_chat(cfg, REDUCE_PROMPT % body, max_tokens=600, timeout=420)
            groups = chunk_text(body, limit)
            self.summary_state['stage'] = '分层汇总（%d 组）' % len(groups)
            items = [llm_chat(cfg, REDUCE_PROMPT % g, max_tokens=500, timeout=420)
                     for g in groups]
            self.last_llm_used = time.time()
        body = '\n'.join('- %s' % x for x in items)[-limit:]
        self.summary_state['stage'] = '汇总日报'
        return llm_chat(cfg, REDUCE_PROMPT % body, max_tokens=600, timeout=420)

    def run_summary(self, day=None, force=False, provider=None):
        day = day or datetime.now().strftime('%Y-%m-%d')
        with self.lock:
            if self.summary_state.get('running'):
                return {'ok': False, 'error': '日报生成中'}
            self.summary_state.update({'running': True, 'day': day, 'stage': '准备',
                                       'error': '', 'elapsed': 0.0, 'chunks': 0,
                                       'ts': time.time()})
        t0 = time.time()
        try:
            data = self.day_transcript(day)
            store_row = {'day': day, 'segments': data['total'], 'voice_count': data['voice_count'],
                         'seconds': data['seconds'], 'transcript': data['text']}
            if not data['lines']:
                self.store.exec(
                    'INSERT INTO voice_daily(day,ts,segments,voice_count,seconds,transcript,'
                    'summary,status,updated) VALUES(?,?,?,?,?,?,?,?,?) '
                    'ON CONFLICT(day) DO UPDATE SET ts=excluded.ts,segments=excluded.segments,'
                    'voice_count=excluded.voice_count,seconds=excluded.seconds,'
                    'transcript=excluded.transcript,summary=excluded.summary,'
                    'status=excluded.status,updated=excluded.updated',
                    (day, _now_iso(), data['total'], 0, data['seconds'], '', '当天没有可总结的语音通联内容。',
                     'empty', _now_iso()))
                self.summary_state.update({'running': False, 'stage': '完成'})
                return {'ok': True, 'empty': True, 'day': day}
            cfg, pname = self._pick_provider(len(data['text']), provider)
            chunk_chars = 2200 if pname == 'local' else 12000
            chunks = chunk_text(data['text'], chunk_chars)
            self.summary_state.update({'stage': '分段总结', 'chunks': len(chunks),
                                       'provider': pname})
            self.last_llm_used = time.time()      # 先占位，防止空闲卸载抢停
            if pname == 'local':
                self.summary_state['stage'] = '启动本地 LLM'
                if not llm_start():
                    raise RuntimeError('本地 LLM 未启动（检查 rkllm-server 与 sudoers 免密）')
            self.last_llm_used = time.time()
            maps = []
            for i, ch in enumerate(chunks, 1):
                self.summary_state['stage'] = '分段总结 %d/%d' % (i, len(chunks))
                maps.append(llm_chat(cfg, MAP_PROMPT % ch, max_tokens=300, timeout=420))
                self.last_llm_used = time.time()
            if len(maps) == 1:
                self.summary_state['stage'] = '汇总日报'
                summary = maps[0]
            else:
                summary = self._reduce_summaries(cfg, maps, pname)
            self.last_llm_used = time.time()
            if pname == 'local' and _flag(self.settings().get('vlog_llm_on_demand'), True):
                self.last_llm_used = time.time()          # 交给空闲卸载
            elapsed = round(time.time() - t0, 1)
            self.store.exec(
                'INSERT INTO voice_daily(day,ts,segments,voice_count,seconds,transcript,summary,'
                'provider,model,status,error,elapsed,chunks,updated) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) '
                'ON CONFLICT(day) DO UPDATE SET ts=excluded.ts,segments=excluded.segments,'
                'voice_count=excluded.voice_count,seconds=excluded.seconds,'
                'transcript=excluded.transcript,summary=excluded.summary,provider=excluded.provider,'
                'model=excluded.model,status=excluded.status,error=excluded.error,'
                'elapsed=excluded.elapsed,chunks=excluded.chunks,updated=excluded.updated',
                (day, _now_iso(), data['total'], data['voice_count'], data['seconds'],
                 data['text'], summary, pname, (cfg or {}).get('model') or '',
                 'done', '', elapsed, len(chunks), _now_iso()))
            self.summary_state.update({'running': False, 'stage': '完成', 'elapsed': elapsed})
            print('%s 日报 %s 生成完成：%d 段语音 / %d 块 / %s / %.1fs' % (
                LOG, day, data['voice_count'], len(chunks), pname, elapsed), flush=True)
            return {'ok': True, 'day': day, 'elapsed': elapsed, 'chunks': len(chunks),
                    'provider': pname, 'summary': summary}
        except Exception as e:
            err = '%s: %s' % (type(e).__name__, e)
            elapsed = round(time.time() - t0, 1)
            self.summary_state.update({'running': False, 'stage': '失败', 'error': err,
                                       'elapsed': elapsed})
            try:
                self.store.exec(
                    'INSERT INTO voice_daily(day,ts,status,error,elapsed,updated) '
                    'VALUES(?,?,?,?,?,?) ON CONFLICT(day) DO UPDATE SET status=excluded.status,'
                    'error=excluded.error,elapsed=excluded.elapsed,updated=excluded.updated',
                    (day, _now_iso(), 'error', err, elapsed, _now_iso()))
            except Exception:
                pass
            print('%s 日报生成失败：%s' % (LOG, err), flush=True)
            return {'ok': False, 'error': err}

    # -- 查询接口 ----------------------------------------------------------
    def list_logs(self, day=None, category=None, kind=None, q=None, pos=None,
                  limit=200, offset=0):
        """pos: 'only' 只看含 APRS 位置的段 / 'none' 只看不含的 / None 不过滤。"""
        sql = 'SELECT id,ts,ts_epoch,seconds,kind,category,rms,peak,dbfs,filename,path,' \
              'bytes,asr_status,asr_text,asr_json,asr_ms,rtf,session_id,seq,feature_json,' \
              'callsigns,callsigns_raw,aprs_pos,aprs_call,aprs_lat,aprs_lon ' \
              'FROM voice_logs WHERE 1=1'
        args = []
        if day:
            sql += ' AND substr(ts,1,10)=?'
            args.append(day)
        if category:
            if category == 'voice':
                sql += " AND category='voice'"
            elif category == 'nonspeech':
                sql += " AND category IN ('noise','aprs','tone','silence')"
            else:
                sql += ' AND category=?'
                args.append(category)
        if kind:
            sql += ' AND kind=?'
            args.append(kind)
        if pos == 'only':
            sql += ' AND IFNULL(aprs_pos,0)=1'
        elif pos == 'none':
            sql += ' AND IFNULL(aprs_pos,0)=0'
        if q:
            sql += (' AND (asr_text LIKE ? OR filename LIKE ? OR callsigns LIKE ?'
                    ' OR aprs_call LIKE ?)')
            args += ['%%%s%%' % q] * 4
        sql += ' ORDER BY ts_epoch DESC LIMIT ? OFFSET ?'
        args += [int(limit), int(offset)]
        rows = self.store.query(sql, args)
        out = []
        for r in rows:
            segs = []
            try:
                segs = json.loads(r.get('asr_json') or '[]')
            except Exception:
                segs = []
            out.append({
                'id': r['id'], 'ts': r['ts'], 'epoch': r['ts_epoch'],
                'seconds': r['seconds'], 'kind': r['kind'],
                'kind_label': KIND_LABEL.get(r['kind'], r['kind']),
                'category': r['category'] or '',
                'category_label': CATEGORY_LABEL.get(r['category'] or '', r['category'] or '待识别'),
                'rms': r['rms'], 'peak': r['peak'], 'dbfs': r['dbfs'],
                'filename': r['filename'], 'bytes': r['bytes'],
                'asr_status': r['asr_status'], 'text': r['asr_text'] or '',
                'segments': segs, 'ms': r['asr_ms'], 'rtf': r['rtf'],
                'session_id': r['session_id'], 'seq': r['seq'],
                'callsigns': (r.get('callsigns') or '').split(',') if r.get('callsigns') else [],
                'callsigns_raw': (r.get('callsigns_raw') or '').split(',')
                                 if r.get('callsigns_raw') else [],
                # 段内 APRS 位置：尾音里解出的对方信标（呼号 + 坐标）
                'aprs_pos': bool(r.get('aprs_pos') or 0),
                'aprs_call': r.get('aprs_call') or '',
                'aprs_lat': r.get('aprs_lat'),
                'aprs_lon': r.get('aprs_lon'),
            })
        return out

    def day_stats(self, day=None):
        day = day or datetime.now().strftime('%Y-%m-%d')
        rows = self.store.query(
            'SELECT category, COUNT(*) AS n, SUM(seconds) AS sec FROM voice_logs '
            'WHERE substr(ts,1,10)=? GROUP BY category', (day,))
        stats = {r['category'] or 'pending': {'n': r['n'], 'sec': round(float(r['sec'] or 0), 1)}
                 for r in rows}
        tot = self.store.one(
            'SELECT COUNT(*) AS n, SUM(seconds) AS sec, SUM(bytes) AS b,'
            ' SUM(CASE WHEN IFNULL(aprs_pos,0)=1 THEN 1 ELSE 0 END) AS pos'
            ' FROM voice_logs WHERE substr(ts,1,10)=?', (day,)) or {}
        return {'day': day, 'categories': stats, 'total': int(tot.get('n') or 0),
                'seconds': round(float(tot.get('sec') or 0.0), 1),
                'bytes': int(tot.get('b') or 0),
                'aprs_pos': int(tot.get('pos') or 0)}

    def peaks(self, rid, n=600):
        row = self.store.one('SELECT path FROM voice_logs WHERE id=?', (rid,))
        if not row or not row.get('path') or not Path(row['path']).exists():
            return []
        x, _ = read_wav_mono(row['path'])
        if x.size == 0:
            return []
        # 用原始 int16 幅度做包络，避免 float 缩放
        a = np.abs(x) * 32768.0
        step = max(1, int(math.ceil(a.size / float(n))))
        out = []
        for i in range(0, a.size, step):
            blk = a[i:i + step]
            out.append(int(blk.max()) if blk.size else 0)
        return out

    def day_peaks(self, day=None, n=1400):
        """全天时间轴概览：返回按时间排序的段列表。

        **必须带上 category / kind** —— 前端时间轴靠它们上色。
        曾经只返回 id/epoch/seconds/peak，导致 'cat-' + undefined 全部落到
        cat-empty，整条时间轴的色块全灰（图例里的 5 类形同虚设）。
        """
        rows = self.store.query(
            'SELECT id,ts,ts_epoch,seconds,peak,category,kind FROM voice_logs '
            'WHERE substr(ts,1,10)=? ORDER BY ts_epoch ASC',
            (day or datetime.now().strftime('%Y-%m-%d'),))
        out = []
        for r in rows:
            cat = r.get('category') or ''
            kind = r.get('kind') or ''
            out.append({
                'id': r['id'], 'epoch': r['ts_epoch'], 'seconds': r['seconds'],
                'peak': r['peak'], 'ts': (r.get('ts') or '')[11:19],
                'category': cat, 'kind': kind,
                'category_label': CATEGORY_LABEL.get(cat, cat or '待识别'),
                'kind_label': KIND_LABEL.get(kind, kind),
            })
        return out

    # -- APRS 时间交叉判定 -------------------------------------------------
    def _aprs_overlap(self, ts_epoch, seconds, kind=''):
        """按时间窗与 APRS 收发记录交叉判定，返回 'tx' / 'rx' / ''。

        为什么不用频谱分类：本机 APRS 发射电平高（发射期间 RMS 约 -4 dBFS），
        自收听经采集 PGA +16.5 dB 后削顶；削顶把能量摊平，tone_ratio 从应有的
        高值掉到 0.45 以下，于是一律被判成「单音/信标」而非「APRS」。
        APRS 服务自己记录了每次解码（aprs_packets）与每次发射（aprs_tx）的
        时间戳，直接比对准确得多。

        收窄误判的两条约束：
          * 本机发射（kind='tx'）只认 aprs_tx，且这是**我们主动发的**，可信；
          * 接收（kind='rx'）只认 aprs_packets，且调用方保证该段没有识别文字
            （有文字说明是人在说话，不该判成 APRS）。
        """
        try:
            t0 = float(ts_epoch)
        except Exception:
            return ''
        if not t0:
            return ''
        t1 = t0 + max(1.0, float(seconds or 0)) + 0.5
        t0 -= 0.5
        if kind == 'tx':
            try:
                if self.store.one(
                        'SELECT id FROM aprs_tx WHERE ts_epoch BETWEEN ? AND ? '
                        'AND IFNULL(ok,1)=1 LIMIT 1', (t0, t1)):
                    return 'tx'
            except Exception:
                pass
            return ''
        try:
            if self.store.one('SELECT id FROM aprs_packets WHERE ts_epoch BETWEEN ? AND ? '
                              'LIMIT 1', (t0, t1)):
                return 'rx'
        except Exception:
            pass
        return ''

    def _aprs_position_hit(self, ts_epoch, seconds, kind=''):
        """找与本段重叠的**位置**报文（带经纬度），返回 dict 或 None。

        与 _aprs_overlap 共用同一套时间窗，理由是实测证据：APRS 爆发紧跟话音
        尾部、落在**同一段录音内**（#51 落在 8.72s / 录音长 11.25s）。

        与 _aprs_overlap 的关键区别：
          * 只认带 lat/lon 的包——状态包、遥测包带不来位置，标记它们没意义；
          * **不要求本段没有识别文字**。「人说话 + 尾音带位置信标」正是要标记的
            对象，而 _aprs_overlap 因为 text 优先，永远不会碰这种段。

        本机发射（kind='tx'）不算：那是我们自己的信标；同理排除本站呼号，
        否则自收听会把每个 tx 段都标成「含对方位置」。
        """
        if kind == 'tx':
            return None
        try:
            t0 = float(ts_epoch)
        except (TypeError, ValueError):
            return None
        if not t0:
            return None
        t1 = t0 + max(1.0, float(seconds or 0)) + 0.5
        t0 -= 0.5
        # 本站自己的呼号要在 SQL 里就排掉，不能取回一条再判断：只取 1 条的话，
        # 本机信标会把同一时刻用户的那一条顶掉，于是整段判成「没有位置」。
        # 只排**精确**呼号（mycall 与 mycall-ssid）——同一操作者的其它 SSID
        # （BI7KHI-9）往往就是用户手上那台，按主呼号排会把它一起误伤。
        mine = []
        try:
            st = self.settings() or {}
            my = str(st.get('aprs_mycall') or '').strip().upper()
            ssid = str(st.get('aprs_ssid') or '').strip()
            if my:
                mine.append(my)
                if ssid not in ('', '0'):
                    mine.append('%s-%s' % (my, ssid))
        except Exception:
            mine = []
        sql = ('SELECT id,src,lat,lon FROM aprs_packets '
               'WHERE ts_epoch BETWEEN ? AND ? AND lat IS NOT NULL '
               'AND lon IS NOT NULL')
        args = [t0, t1]
        if mine:
            sql += (" AND UPPER(IFNULL(src,'')) NOT IN (%s)"
                    % ','.join('?' * len(mine)))
            args += mine
        sql += ' ORDER BY ts_epoch DESC LIMIT 1'
        try:
            row = self.store.one(sql, tuple(args))
        except Exception:
            return None
        if not row:
            return None
        return {'id': row.get('id'),
                'call': str(row.get('src') or '').strip(),
                'lat': row.get('lat'), 'lon': row.get('lon')}

    def _fill_aprs_pos(self, ts_epoch, seconds, kind=''):
        """给一段算出位置标记，返回可直接 UPDATE 的四个值。"""
        hit = self._aprs_position_hit(ts_epoch, seconds, kind)
        if not hit:
            return (0, None, None, None)
        try:
            lat = round(float(hit['lat']), 5)
            lon = round(float(hit['lon']), 5)
        except (TypeError, ValueError):
            return (0, None, None, None)
        return (1, hit.get('call') or '', lat, lon)

    def _dir_usage(self, base):
        """统计目录下 .wav 的数量与总大小（带短 TTL 缓存）。

        这是 O(文件数) 的递归遍历，而 status() 会被前端每 3s 轮询一次。
        缓存 15s：对轮询来说足够新，又把重复扫描降一个数量级。
        """
        key = str(base)
        now = time.time()
        with self._usage_lock:
            c = self._usage_cache
            if c and c[0] == key and (now - c[1]) < self._usage_ttl:
                return c[2], c[3]
        size_mb = 0.0
        files = 0
        try:
            if base.exists():
                for f in base.rglob('*.wav'):
                    try:
                        size_mb += f.stat().st_size / 1048576.0
                        files += 1
                    except Exception:
                        pass
        except Exception:
            pass
        with self._usage_lock:
            self._usage_cache = (key, now, size_mb, files)
        return size_mb, files

    def status(self):
        st = self.settings()
        base = Path(st.get('vlog_dir') or DEFAULTS['vlog_dir'])
        size_mb, files = self._dir_usage(base)
        rec = self.recorder
        today = datetime.now().strftime('%Y-%m-%d')
        recent = self.store.query(
            'SELECT id,ts,seconds,category,rms,peak,dbfs FROM voice_logs '
            'ORDER BY id DESC LIMIT 8')
        clipped = [r for r in recent if int(r.get('peak') or 0) >= 32000]
        too_loud = [r for r in recent if _f(r.get('dbfs'), -99) > -12]
        return {
            'enabled': self.enabled(),
            'dir': str(base),
            'channel': st.get('vlog_channel'),
            'pre_roll': _f(st.get('vlog_pre_roll'), 3.0),
            'post_roll': _f(st.get('vlog_post_roll'), 2.0),
            'min_seconds': _f(st.get('vlog_min_seconds'), 1.0),
            'recording': bool(rec.seg is not None),
            'active': bool(rec.active),
            'kind': rec.seg.kind if rec.seg else '',
            'rx': bool(self.get_rx()),
            'tx': bool(self.get_tx()),
            'files': files,
            'size_mb': round(size_mb, 1),
            'queue': self.asr_q.qsize(),
            'asr_running': self.asr_running,
            'asr_current': self.asr_current,
            'asr_last': self.asr_last,
            'asr_error': self.asr_error,
            'vad': {'available': self.vad.available,
                    'model': self.vad.model_path,
                    'model_exists': Path(self.vad.model_path).exists(),
                    'state': ('已加载' if self.vad.available
                              else ('待首次使用' if Path(self.vad.model_path).exists()
                                    else '模型缺失')),
                    'error': self.vad._error},
            'counters': dict(self.counters),
            'stats': dict(rec.stats),
            'today': self.day_stats(today),
            'recent': recent,
            'level': {
                'clipped': len(clipped),
                'too_loud': len(too_loud),
                'recent_peak': int(recent[0]['peak']) if recent else 0,
                'recent_dbfs': recent[0]['dbfs'] if recent else -120,
                'hint': ('检测到 %d 条录音削顶：请降低麦克风 PGA 增益（设置页「麦克风输入源与增益」），'
                         '健康电平应让语音 rms 落在 -30~-20 dBFS、峰值不超过 -6 dBFS。'
                         % len(clipped)) if clipped else '',
            },
            'retention': {'days': int(_f(st.get('vlog_retention_days'), 30)),
                          'mb': int(_f(st.get('vlog_retention_mb'), 20480))},
            'summary': dict(self.summary_state),
            'summary_time': st.get('vlog_summary_time'),
            'summary_provider': st.get('vlog_summary_provider'),
            'llm': {'running': llm_active(), 'ready': llm_ready(),
                    'on_demand': _flag(st.get('vlog_llm_on_demand'), True),
                    'last_used': self.last_llm_used},
            'ts': _now_iso(),
        }

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""APRS 收发子系统（纯 Python 自研 1200bps Bell202 软件 TNC）。

设计要点
--------
1) **不抢占音频设备**：NAU88C22 的 capture 通道是独占的，第二路 arecord 直接
   `Device or resource busy`。所以本模块不自己开声卡，而是被 app.py 的采集中枢
   （`_mic_capture_loop`）在已有的 `voice_service.feed()` 旁边一起喂数据。

2) **常驻连续解码**：不受 BUSY 触发与录音分段影响。实测 APRS 爆发紧跟话音尾部
   （#51 落在 8.72s / 录音长 11.25s，余量仅 2.5s），依赖 BUSY 分段必然会漏包。

3) **解码判据是 CRC-16/X-25 校验通过**，不是能量或相似度。因此误报率极低，
   突发检测只用来省 CPU，不影响正确性。

链路：audio -> 带通 -> 双音相关 -> 判决变量 -> 突发段内做比特相位搜索
      -> NRZI 解码 -> HDLC 去填充/定界 -> AX.25 拆帧 -> CRC 校验 -> 入库
"""

import json
import math
import os
import random
import re
import sqlite3
import threading
import time
import wave
from collections import deque
from datetime import datetime, timedelta

try:
    import numpy as np
except Exception:                                     # pragma: no cover
    np = None

LOG = '[APRS]'

# ---------------------------------------------------------------------------
# 物理层常量（APRS = 1200 bps Bell 202 AFSK）
# ---------------------------------------------------------------------------
SAMPLE_RATE = 16000
BAUD = 1200.0
MARK_HZ = 1200.0          # NRZI 电平 1
SPACE_HZ = 2200.0         # NRZI 电平 0
# 带通范围：实测取自 10 段真实中继录音的灵敏度对比。
# 400-3400 只能解出 #51（1/2 个含 APRS 的文件）；
# 放宽到 300-3600 后 #46 的弱突发也能解出（2/2）。窄带会削掉 2200Hz 音调的
# 边带与时钟分量，对贴地信号是致命的。
BP_LO = 300.0
BP_HI = 3600.0
FLAG = [0, 1, 1, 1, 1, 1, 1, 0]

# 发射侧
AFSK_AMP = 0.55           # 满量程比例（实测 -2.9 dBFS 峰值，含余量不削顶）
TX_LEAD_SILENCE = 0.30    # 音频前导静音：等 PTT 与功放稳定
TX_TAIL_SILENCE = 0.30    # 音频尾部静音：保证末位不被切掉
TX_FLAGS = 40             # 前导标志数量（标准要求 ≥ 12；多给一些给对端 AGC 收敛）

FCS_RESIDUE = 0xF0B8
FCS_RESIDUE_SW = 0x0F47   # 本实现 CRC 的低字节序残留（两者都接受）

# ---------------------------------------------------------------------------
# 默认设置（settings 表 aprs_* 键）
# ---------------------------------------------------------------------------
DEFAULTS = {
    'aprs_enabled': '1',
    'aprs_channel': 'left',            # left / right / mix（与 vlog_channel 同理）

    # —— 本机标识 ——
    'aprs_mycall': 'BI7KHI',
    'aprs_ssid': '10',                 # 中继/固定台惯例：-3 中继台，-10 气象/iGate
    'aprs_dest': 'APRS',               # 目的地址（TOCALL）
    'aprs_path': 'WIDE1-1,WIDE2-1',    # digipeater 路径；留空=不发路径

    # —— 位置 ——
    'aprs_pos_source': 'manual',       # manual / nmea（ZED-F9P 预留）
    'aprs_lat': '22.533300',
    'aprs_lon': '114.050000',
    'aprs_alt_m': '',
    'aprs_symbol_table': '/',
    'aprs_symbol_code': '-',           # '-' = 房子（固定台）；'_' = 气象站
    'aprs_comment': '',
    'aprs_pos_ambiguity': '0',         # 位置模糊位数 0-4（隐私，0=精确）
    'aprs_gps_port': '',               # 例 /dev/ttyACM0（ZED-F9P 接上后填）
    'aprs_gps_baud': '38400',

    # —— 发射内容开关 ——
    'aprs_beacon_enabled': '1',
    'aprs_beacon_interval': '1800',    # 秒
    'aprs_weather_enabled': '1',
    'aprs_weather_interval': '600',    # 秒
    'aprs_telemetry_enabled': '1',
    'aprs_telemetry_interval': '3600', # 秒
    'aprs_status_enabled': '0',
    'aprs_status_interval': '3600',
    'aprs_status_text': '',
    'aprs_jitter': '20',               # 定时发射随机抖动上限（秒）

    # —— 发射闸门 ——
    'aprs_carrier_sense': '1',         # 1=检测到 BUSY/PTT 就暂缓
    'aprs_defer_max': '120',           # 最长顺延（秒），超过则放弃本次
    'aprs_defer_jitter': '3',          # 信道空闲后再随机等 0..N 秒
    'aprs_min_gap': '20',              # 两次发射最小间隔（秒）

    # —— 解码 ——
    'aprs_burst_threshold': '0.30',    # 双音纯度阈值（0.5=纯音，0.154=白噪声）
    'aprs_burst_min_ms': '70',         # 突发最短持续（毫秒）
    'aprs_phase_trials': '64',         # 比特相位搜索份数
    'aprs_dedup_window': '10',         # 同内容去重窗口（秒）；同站周期性重发不应被吞

    # —— 遥测通道映射（JSON）——
    'aprs_telemetry_map': json.dumps({
        'a1': {'src': 'battery_v', 'scale': 10, 'offset': 0, 'name': 'Battery', 'unit': 'V'},
        'a2': {'src': 'pv_v', 'scale': 10, 'offset': 0, 'name': 'Solar', 'unit': 'V'},
        'a3': {'src': 'cpu_temp', 'scale': 1, 'offset': 50, 'name': 'CpuTemp', 'unit': 'C'},
        'a4': {'src': 'tx_count', 'scale': 0.1, 'offset': 0, 'name': 'TxCount', 'unit': ''},
        'a5': {'src': 'wind_ms', 'scale': 10, 'offset': 0, 'name': 'Wind', 'unit': 'm/s'},
        'd1': {'src': 'ptt', 'name': 'PTT'},
        'd2': {'src': 'busy', 'name': 'BUSY'},
        'd3': {'src': 'wx_online', 'name': 'WX'},
        'd4': {'src': 'aprs_enabled', 'name': 'APRS'},
    }, ensure_ascii=False),

    # —— 地图 ——
    'aprs_map_provider': 'tianditu',   # tianditu / none（离线网格兜底）
    # 天地图 key 分两种权限类型，用途不同（实测确认）：
    #   服务端 key：不需要 Referer，供板端代理取瓦片（key 不外泄、可落盘缓存）
    #   浏览器端 key：天地图会校验 Referer，只能由浏览器直连，代理请求会 403
    'aprs_map_tk': 'eaa1673e60065f76cbf3c970063ec475',
    'aprs_map_tk_browser': '1272652cf6df51e27dd58a8e198912c1',
    'aprs_map_layers': 'img,cva',      # img=影像 cva=影像注记 vec/cva ter/cta
    'aprs_map_cache_mb': '512',
    'aprs_map_coord': 'auto',          # auto / wgs84 / gcj02 / bd09
    'aprs_track_points': '300',        # 每台保存的轨迹点上限

    'aprs_retention_days': '90',
}

SYMBOL_TABLE_ORDER = ' /\\0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'

TYPE_LABEL = {
    'position': '位置', 'weather': '气象', 'status': '状态', 'telemetry': '遥测',
    'message': '消息', 'object': '对象', 'item': '物品', 'query': '查询',
    'mice': 'Mic-E', 'nmea': 'NMEA', 'userdef': '自定义', 'unknown': '未知',
}


def _now_iso():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


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
    return str(v).strip().lower() in ('1', 'true', 'yes', 'on', '是', '开')


# ===========================================================================
# 一、AX.25 / HDLC / CRC
# ===========================================================================
def crc16_x25(data):
    """CRC-16/X-25（反射多项式 0x8408，初值 0xFFFF，输出取反）。
    标准测试向量：b'123456789' -> 0x906E"""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if (crc & 1) else (crc >> 1)
    return crc ^ 0xFFFF


def ax25_addr(call, ssid=0, last=False, h=False):
    """构造 7 字节 AX.25 地址字段。"""
    call = (call or '').upper()
    c = (call + ' ' * 6)[:6]
    out = bytearray()
    for ch in c:
        out.append((ord(ch) & 0x7F) << 1)
    v = 0x60 | ((int(ssid) & 0x0F) << 1) | (0x01 if last else 0x00)
    if h:
        v |= 0x80
    out.append(v)
    return bytes(out)


def parse_path(text):
    """'WIDE1-1,WIDE2-1' -> [('WIDE1',1), ('WIDE2',1)]"""
    out = []
    for part in (text or '').split(','):
        part = part.strip().upper()
        if not part:
            continue
        if '-' in part:
            call, _, ssid = part.partition('-')
            try:
                s = int(ssid)
            except Exception:
                s = 0
        elif part in ('WIDE1', 'WIDE2', 'TRACE', 'RELAY'):
            call, s = part, 0
        else:
            call, s = part, 0
        out.append((call.strip(), max(0, min(15, s))))
    return out


def build_frame(src, dst, path=(), info=b'', src_ssid=0, dst_ssid=0, ctrl=0x03, pid=0xF0):
    """组装完整 AX.25 UI 帧（含 FCS）。"""
    if isinstance(info, str):
        info = info.encode('utf-8', 'replace')
    p = bytearray()
    p += ax25_addr(dst, dst_ssid, last=False)
    p += ax25_addr(src, src_ssid, last=(len(path) == 0))
    for i, (c, s) in enumerate(path):
        p += ax25_addr(c, s, last=(i == len(path) - 1))
    p += bytes([ctrl, pid]) + info
    fcs = crc16_x25(bytes(p))
    p += bytes([fcs & 0xFF, (fcs >> 8) & 0xFF])
    return bytes(p)


def parse_frame(pkt):
    """解析 AX.25 帧 -> dict；失败返回 None。"""
    if not pkt or len(pkt) < 17:
        return None
    addrs = []
    i = 0
    while i + 7 <= len(pkt) - 1:
        chunk = pkt[i:i + 7]
        call = ''.join(chr((c >> 1) & 0x7F) for c in chunk[:6]).strip()
        ssid = (chunk[6] >> 1) & 0x0F
        last = bool(chunk[6] & 0x01)
        h = bool(chunk[6] & 0x80)
        addrs.append({'call': call, 'ssid': ssid, 'last': last, 'h': h})
        i += 7
        if last:
            break
    if i >= len(pkt):
        return None
    body = pkt[i:]
    ctrl = body[0]
    pid = None
    if (ctrl & 0x03) == 0x03 and (ctrl & 0x10) == 0:     # UI 帧
        if len(body) >= 2:
            pid = body[1]
            info = body[2:-2] if len(body) >= 4 else b''
        else:
            info = b''
    else:
        info = body[1:-2] if len(body) >= 3 else b''
    return {'addrs': addrs, 'ctrl': ctrl, 'pid': pid, 'info': info,
            'len': len(pkt), 'src': addrs[1] if len(addrs) > 1 else (addrs[0] if addrs else None),
            'dst': addrs[0] if addrs else None, 'digis': addrs[2:]}


def fmt_addr(a):
    if not a:
        return '?'
    return a['call'] if not a.get('ssid') else '%s-%d' % (a['call'], a['ssid'])


def addr_text(addrs):
    """DEST,SRC,DIGI1*,DIGI2  形式（* 表示已被中继）。"""
    if not addrs:
        return ''
    out = []
    for a in addrs:
        out.append(fmt_addr(a) + ('*' if a.get('h') else ''))
    return ','.join(out)


# ---------------------------------------------------------------------------
# HDLC 位层
# ---------------------------------------------------------------------------
def hdlc_wrap(data, flags=TX_FLAGS):
    """字节流 -> 带标志与比特填充的串行位流（字节内 LSB 先行）。"""
    bits = []
    for b in data:
        for i in range(8):
            bits.append((b >> i) & 1)
    out = []
    ones = 0
    for b in bits:
        out.append(b)
        if b:
            ones += 1
            if ones == 5:
                out.append(0)          # 比特填充
                ones = 0
        else:
            ones = 0
    return FLAG * flags + out + FLAG * 2


def nrzi_encode(levels_bits):
    """HDLC 位 -> NRZI 电平（0=翻转，1=保持）。"""
    out = []
    cur = 1
    for b in levels_bits:
        if b == 0:
            cur ^= 1
        out.append(cur)
    return out


def nrzi_decode(levels):
    """NRZI 电平 -> HDLC 位。"""
    out = []
    prev = 1
    for b in levels:
        out.append(1 if b == prev else 0)
        prev = b
    return out


def bits_to_frames(bits):
    """串行位流 -> [(frame_bytes, start_bit, nbits)]，只返回 CRC 通过的帧。"""
    frames = []
    n = len(bits)
    if n < 8:
        return frames
    b = np.asarray(bits, dtype=np.uint8)

    def is_flag(p):
        return (p + 8 <= n and b[p] == 0 and b[p + 7] == 0
                and bool(b[p + 1:p + 7].all()))

    j = 0
    while j + 8 <= n:
        if not is_flag(j):
            j += 1
            continue
        k = j + 8
        while is_flag(k):                 # 跳过连续共享标志
            k += 8
        s0 = k
        out = bytearray()
        ones = 0
        closed = False
        while k < n:
            # 标志检查必须早于去填充：收尾标志本身含 6 个连续 1，
            # 若先做「5 个 1 后必为 0」的判定会把正常收尾误判成 abort。
            if ones < 5 and is_flag(k):
                closed = True
                break
            bit = int(b[k])
            if ones == 5:
                if bit == 1:              # 非标志的 6+ 连续 1 => 中止序列
                    break
                ones = 0                  # 填充位，丢弃
                k += 1
                continue
            ones = ones + 1 if bit else 0
            out.append(bit)
            k += 1
        if closed and len(out) >= 136:
            m = len(out) - (len(out) % 8)
            bs = bytes(sum((out[i + t] & 1) << t for t in range(8))
                       for i in range(0, m, 8))
            r = crc16_x25(bs)
            if r == FCS_RESIDUE or r == FCS_RESIDUE_SW:
                frames.append((bs, s0, len(out)))
        j = k if k > j else j + 1
    return frames


# ===========================================================================
# 二、Bell 202 AFSK 调制 / 解调
# ===========================================================================
def modulate(levels, sr=SAMPLE_RATE, baud=BAUD, f_mark=MARK_HZ, f_space=SPACE_HZ,
             amp=AFSK_AMP, phase0=0.0):
    """NRZI 电平序列 -> 连续相位 AFSK 波形（float32）。"""
    lv = np.asarray(levels, dtype=np.uint8)
    spb = sr / baud
    n = int(round(len(lv) * spb))
    if n <= 0:
        return np.zeros(0, dtype=np.float32)
    idx = np.minimum((np.arange(n) / spb).astype(np.int64), len(lv) - 1)
    ft = np.where(lv[idx] > 0, f_mark, f_space).astype(np.float64)
    ph = phase0 + 2.0 * np.pi * np.cumsum(ft) / sr
    return (amp * np.sin(ph)).astype(np.float32)


def build_afsk(frame_bytes, sr=SAMPLE_RATE, amp=AFSK_AMP, flags=TX_FLAGS,
               lead=TX_LEAD_SILENCE, tail=TX_TAIL_SILENCE):
    """完整帧字节 -> 可直接播放的 float32 PCM（含前后静音）。"""
    bits = hdlc_wrap(frame_bytes, flags=flags)
    levels = nrzi_encode(bits)
    wave_ = modulate(levels, sr=sr, amp=amp)
    return np.concatenate([
        np.zeros(int(round(lead * sr)), dtype=np.float32),
        wave_,
        np.zeros(int(round(tail * sr)), dtype=np.float32),
    ])


def _tone_kernel(f, W, sr):
    t = np.arange(W) / sr
    return np.exp(-2j * np.pi * f * t)


class Tnc:
    """流式 1200bps Bell202 解调器。

    喂入的单声道 float 样本会被：带通 -> 双音相关 -> 判决变量累积；
    当检测到「类 AFSK 突发」时，在突发所在时间窗内做比特相位搜索，
    由 HDLC + CRC 定帧。CPU 峰值只出现在真正有 APRS 的时刻。
    """

    def __init__(self, sr=SAMPLE_RATE, nphase=64, threshold=0.30,
                 burst_min_ms=70.0, window_s=1.8):
        if np is None:
            raise RuntimeError('numpy 不可用')
        self.sr = int(sr)
        self.spb = self.sr / BAUD
        self.W = max(4, int(round(self.spb)))
        self.k0 = _tone_kernel(MARK_HZ, self.W, self.sr)
        self.k1 = _tone_kernel(SPACE_HZ, self.W, self.sr)
        self.ones = np.ones(self.W)
        self.nphase = max(8, int(nphase))
        self.threshold = float(threshold)
        self.burst_min = max(8, int(round(burst_min_ms / 1000.0 * self.sr / self.spb)))
        self.window_s = float(window_s)

        self.n_phase_max = 1.0
        self.total = 0                      # 已处理样本数
        self.d_hist = []
        self.d_len = 0
        self.d_base = 0                     # d_hist[0][0] 对应的全局样本下标
        self.tail_raw = np.zeros(0, dtype=np.float64)   # 带通后待重叠样本
        self.bp_zi = None
        self.bp_ba = None
        self._init_bp()

        self.ratio_hist = []
        self.ratio_len = 0
        self.r_base = 0
        self.train = deque(maxlen=64)       # 最近判决变量（用于触发的定位）
        self.stats = {'samples': 0, 'bursts': 0, 'frames': 0, 'unique': 0,
                      'cpu_ms': 0.0, 'last_frame': 0.0, 'max_ratio': 0.0,
                      'ratio_hi_ratio': 0.0}
        self._recent_marker = 0
        self._pending_bursts = []           # [(t0_sample, t1_sample)]
        self._seen = {}                     # 内容去重
        self._lock_out = []

    # ---------- 带通（流式 IIR） ----------
    def _init_bp(self):
        try:
            from scipy.signal import butter
            self.bp_ba = butter(4, [BP_LO / (self.sr / 2.0), BP_HI / (self.sr / 2.0)],
                                btype='band')
            from scipy.signal import lfilter_zi
            self.bp_zi = lfilter_zi(*self.bp_ba) * 0.0
        except Exception:
            self.bp_ba = None
            self.bp_zi = None

    def _bandpass(self, x):
        if self.bp_ba is None:
            return x
        from scipy.signal import lfilter
        y, self.bp_zi = lfilter(self.bp_ba[0], self.bp_ba[1], x, zi=self.bp_zi)
        return y

    # ---------- 关键：判决变量 ----------
    def _push(self, arr, store, base_attr, len_attr, chunk_base):
        """把本块的数组追加进去，并维护「数组首元素对应的全局样本下标」。
        只从尾部截断，因此基准下标只会单调前移。"""
        n = len(arr)
        if n == 0:
            return
        if getattr(self, len_attr) == 0:
            setattr(self, base_attr, chunk_base)
        store.append(arr)
        setattr(self, len_attr, getattr(self, len_attr) + n)
        cap = int(self.window_s * self.sr * 3)
        while getattr(self, len_attr) > cap and len(store) > 1:
            old = store.pop(0)
            setattr(self, len_attr, getattr(self, len_attr) - len(old))
            setattr(self, base_attr, getattr(self, base_attr) + len(old))

    def _slice(self, store, base, start, end):
        """按「全局样本下标」取 [start, end) 的数组。"""
        out = []
        a0 = base
        for a in store:
            a1 = a0 + len(a)
            if a1 > start and a0 < end:
                out.append(a[max(0, start - a0):min(len(a), end - a0)])
            a0 = a1
        if not out:
            return np.zeros(0, dtype=np.float64)
        return np.concatenate(out)

    def feed(self, mono):
        """mono: 单声道 float（[-1,1]）或 int16 数组。返回本次新解出的原始帧列表。"""
        t0 = time.time()
        x = np.asarray(mono)
        if x.dtype == np.int16:
            x = x.astype(np.float64) / 32768.0
        else:
            x = x.astype(np.float64, copy=False)
        if x.size == 0:
            return []
        self.total += len(x)
        self.stats['samples'] += len(x)

        xf = self._bandpass(x)
        # —— 带重叠的相关（y[n] 对应以 n 为起点的 W 样本窗）——
        # d 数组第 0 个元素对应的全局样本下标，必须把重叠保留的 tail 算进去，
        # 否则时间戳会整体偏移 (W-1) 个样本，突发窗口也会取偏。
        chunk_base = (self.total - len(x)) - len(self.tail_raw)
        seg = np.concatenate([self.tail_raw, xf]) if len(self.tail_raw) else xf
        if len(seg) < self.W:
            self.tail_raw = seg
            return []
        y0 = np.correlate(seg, self.k0, 'valid')
        y1 = np.correlate(seg, self.k1, 'valid')
        e = np.convolve(seg * seg, self.ones, 'valid')
        m = min(len(y0), len(y1), len(e), len(xf))
        y0, y1, e, xf_out = y0[:m], y1[:m], e[:m], xf[:m]
        p = (y0.real ** 2 + y0.imag ** 2) + (y1.real ** 2 + y1.imag ** 2)
        with np.errstate(divide='ignore', invalid='ignore'):
            ratio = p / (self.W * e + 1e-12)
        d = (y0.real ** 2 + y0.imag ** 2) - (y1.real ** 2 + y1.imag ** 2)

        start_idx = chunk_base
        self._push(d.astype(np.float64), self.d_hist, 'd_base', 'd_len', chunk_base)
        self._push(np.nan_to_num(ratio, nan=0.0, posinf=0.0),
                   self.ratio_hist, 'r_base', 'ratio_len', chunk_base)
        self.tail_raw = seg[-(self.W - 1):] if self.W > 1 else np.zeros(0)

        if len(ratio):
            mx = float(np.max(ratio))
            self.stats['max_ratio'] = max(self.stats['max_ratio'], mx)

        # —— 突发检测：ratio 连续超阈 ——
        hot = ratio > self.threshold
        if hot.any():
            a = start_idx
            gap = int(0.20 * self.sr)
            idx = np.flatnonzero(np.diff(np.concatenate([[0], hot.view(np.int8), [0]])))
            for q in range(0, len(idx) - 1, 2):
                s, t = int(idx[q]), int(idx[q + 1])
                if t - s < self.burst_min:
                    continue
                s, t = a + s, a + t
                # 关键：必须在「追加时就」把相邻突发接成一段。
                # 采集每块只有 1024 样本，一个 0.9s 的 APRS 突发会被切成几十个
                # 分片。若留到 _service_bursts 里再合并，每次只有一个分片够旧被
                # 消费出去，合并永远不发生，于是每片各搜一次、而每个窗口都装不下
                # 完整帧 —— 结果就是「能测到突发却一帧都解不出来」。
                if self._pending_bursts and s - self._pending_bursts[-1][1] <= gap:
                    self._pending_bursts[-1] = (self._pending_bursts[-1][0], t)
                else:
                    self._pending_bursts.append((s, t))
                self.stats['bursts'] += 1

        frames = self._service_bursts()
        self.stats['cpu_ms'] += (time.time() - t0) * 1000.0
        return frames

    def flush(self):
        """强制把仍在等待的突发全部解出（信号结束/离线分析时用）。"""
        return self._service_bursts(force=True)

    def _service_bursts(self, force=False):
        if not self._pending_bursts:
            return []
        margin = 0 if force else int(0.12 * self.sr)
        # 只挑「尾部样本已经收齐」的突发；剩下的留到下次 feed。
        # （早先版本用「最后一个突发够不够旧」做整体判断，而最后一个突发
        #   必然贴着信号末尾，导致永远提前返回、一个突发都不处理。）
        ready, wait = [], []
        for b in self._pending_bursts:
            (ready if self.total - b[1] >= margin else wait).append(b)
        self._pending_bursts = wait
        if not ready:
            return []
        # 合并相距很近的突发：一个 AFSK 突发常被阈值切成若干小段，
        # 分段各做一次相位搜索既慢又重复，合并后只搜一次。
        ready.sort()
        merged = []
        for (s, t) in ready:
            if merged and s - merged[-1][1] < int(0.20 * self.sr):
                merged[-1] = (merged[-1][0], max(merged[-1][1], t))
            else:
                merged.append((s, t))
        got = []
        pad = int(0.30 * self.sr)
        for (s, t) in merged:
            a = max(0, s - pad)
            b = t + pad
            for (ts, fr) in self._search(a, b):
                got.append((ts, fr))
        fresh = []
        now = time.time()
        # TNC 级去重窗口只用来合并「同一突发的多相位/双极性解出结果」，
        # 必须很短：否则同站台周期性重发的相同信标（内容一致）会被误吞。
        for (ts, fr) in got:
            key = bytes(fr)
            prev = self._seen.get(key)
            if prev is not None and (now - prev) < 2.5:
                continue
            self._seen[key] = now
            self.stats['frames'] += 1
            fresh.append((ts, fr))
        if len(self._seen) > 500:
            cut = now - 300
            self._seen = {k: v for k, v in self._seen.items() if v >= cut}
        if fresh:
            self.stats['unique'] += len(fresh)
            self.stats['last_frame'] = now
        return fresh

    def _search(self, s0, s1):
        """在 [s0,s1) 内做比特相位 × 双极性搜索，返回 [(t_sec, frame)]。"""
        d = self._slice(self.d_hist, self.d_base, s0, s1)
        n = len(d)
        out = []
        if n < 60:
            return out
        for pol in (1.0, -1.0):
            dd = d * pol
            for p in range(self.nphase):
                off = p * self.spb / self.nphase
                pos = np.arange(off, n - 1.001, self.spb)
                if len(pos) < 40:
                    continue
                i0 = pos.astype(np.int64)
                frac = pos - i0
                v = dd[i0] * (1.0 - frac) + dd[i0 + 1] * frac
                bits = nrzi_decode((v > 0).astype(np.uint8))
                for (fr, bs, nb) in bits_to_frames(bits):
                    t = (s0 + pos[bs] if bs < len(pos) else s0 + pos[-1]) / float(self.sr)
                    out.append((t, fr))
        return out

    def status(self):
        return dict(self.stats)


# ===========================================================================
# 三、APRS 报文生成（发射侧）
# ===========================================================================
def _latlon_str(lat, lon, ambiguity=0):
    """APRS 未压缩经纬度字符串。ambiguity=模糊位数(0-4)。"""
    amb = max(0, min(4, int(ambiguity or 0)))

    def fmt(v, width, deg_w, hemi_p, hemi_n):
        hemi = hemi_p if v >= 0 else hemi_n
        v = abs(v)
        deg = int(v)
        minute = (v - deg) * 60.0
        s = '%0*d%05.2f' % (deg_w, deg, minute)
        if amb:
            keep = max(0, len(s) - amb)
            s = s[:keep] + ' ' * (len(s) - keep)
        return s + hemi

    return fmt(lat, 8, 2, 'N', 'S'), fmt(lon, 9, 3, 'E', 'W')


def _aprs_time(ts=None, style='z'):
    d = datetime.fromtimestamp(ts) if ts else datetime.now()
    if style == 'z':
        return d.strftime('%d%H%Mz')
    if style == 'slash':
        return d.strftime('%d%H%M/%H')
    return d.strftime('%m%d%H%M')


def aprs_position(lat, lon, symbol_table='/', symbol_code='-', comment='',
                  ts=None, ambiguity=0, messaging=False, timestamped=True, alt_m=None):
    """位置报文。messaging=True 用 '='/'@'（支持 APRS 消息），否则 '!'/'/'。"""
    la, lo = _latlon_str(lat, lon, ambiguity)
    if timestamped:
        head = ('@' if messaging else '/') + _aprs_time(ts)
    else:
        head = '=' if messaging else '!'
    body = la + symbol_table + lo + symbol_code
    txt = (comment or '')
    if alt_m is not None:
        try:
            txt = '/A=%06d%s' % (int(round(float(alt_m) * 3.28084)), txt)
        except Exception:
            pass
    return (head + body + txt).encode('utf-8', 'replace')


WX_RE = {
    'wind': re.compile(r'_(?P<dir>\d{3})/(?P<spd>\d{3})'),
    'gust': re.compile(r'g(?P<v>\d{3})'),
    'temp': re.compile(r't(?P<v>-?\d{3})'),
    'rain1h': re.compile(r'r(?P<v>\d{3})'),
    'rain24': re.compile(r'p(?P<v>\d{3})'),
    'rainsince': re.compile(r'P(?P<v>\d{3})'),
    'hum': re.compile(r'h(?P<v>\d{2})'),
    'baro': re.compile(r'b(?P<v>\d{5})'),
    'lum': re.compile(r'L(?P<v>\d{3})'),
    'solar': re.compile(r's(?P<v>\d{3})'),
}


def wx_comment(wx):
    """把物理量字典转成 APRS 气象后缀（长度 < 60 字符）。

    入参（可缺项）：wind_ms 风速 m/s, wind_dir 度, gust_ms, temp_c,
    rain_1h_mm, rain_24h_mm, rain_today_mm, humidity %, pressure_hpa
    单位按 APRS 规范：风速 mph、温度 °F、雨量 1/100 英寸、气压 0.1hPa。
    """
    out = []

    def mph(ms):
        return max(0, min(999, int(round(ms * 2.23694))))

    def rain_in(v_mm):
        return max(0, min(999, int(round(v_mm / 0.254))))

    d = wx.get('wind_dir')
    sp = wx.get('wind_ms')
    if d is not None or sp is not None:
        dd = 0 if d is None else max(0, min(360, int(round(d))))
        ss = 0 if sp is None else mph(sp)
        out.append('_%03d/%03d' % (dd, ss))
    if wx.get('gust_ms') is not None:
        out.append('g%03d' % mph(wx['gust_ms']))
    if wx.get('temp_c') is not None:
        tf = int(round(float(wx['temp_c']) * 9.0 / 5.0 + 32.0))
        out.append('t%03d' % max(-99, min(999, tf)))
    if wx.get('rain_1h_mm') is not None:
        out.append('r%03d' % rain_in(wx['rain_1h_mm']))
    if wx.get('rain_24h_mm') is not None:
        out.append('p%03d' % rain_in(wx['rain_24h_mm']))
    if wx.get('rain_today_mm') is not None:
        out.append('P%03d' % rain_in(wx['rain_today_mm']))
    if wx.get('humidity') is not None:
        out.append('h%02d' % max(0, min(100, int(round(wx['humidity'])))))
    if wx.get('pressure_hpa') is not None:
        p = float(wx['pressure_hpa'])
        if p > 1000:
            out.append('b1%04d' % int(round((p - 1000.0) * 10.0)))
        else:
            out.append('b0%04d' % int(round((p - 900.0) * 10.0)))
    return ''.join(out)


def aprs_weather(wx, lat, lon, symbol_table='/', symbol_code='_', comment='',
                 ts=None, ambiguity=0, with_position=True):
    """完整气象报文。with_position=True 用「位置+气象后缀」（兼容性最好）。"""
    wx_text = wx_comment(wx)
    if with_position:
        return aprs_position(lat, lon, symbol_table, symbol_code,
                             (comment or '') + wx_text, ts=ts, messaging=False,
                             timestamped=True, ambiguity=ambiguity)
    head = '_' + _aprs_time(ts, 'slash')
    wind = wx.get('wind_dir')
    sp = wx.get('wind_ms')
    extra = ''
    if wind is not None or sp is not None:
        extra += 'c%03d' % (0 if wind is None else max(0, min(360, int(round(wind)))))
        extra += 's%03d' % (0 if sp is None else max(0, min(999, int(round(sp * 2.23694)))))
    rest = wx_text
    if rest.startswith('_') and '/' in rest[:8]:
        rest = rest[8:]                     # 去掉 _ddd/sss（位置型写法）
    return (head + extra + rest).encode('utf-8', 'replace')


def aprs_status(text, ts=None):
    """状态报文。'>>' 前缀表示可被询问（本站不响应查询，用单 '>'）。"""
    head = '>'
    if ts:
        head += _aprs_time(ts, 'z')
    return (head + (text or '')).encode('utf-8', 'replace')


def aprs_telemetry(seq, analogs, digital):
    """遥测报文 T#SSS,a1..a5,dddddddd。analogs 为 0-255 整数，digital 为位串。"""
    a = [max(0, min(255, int(round(v or 0)))) for v in (list(analogs) + [0] * 5)[:5]]
    if isinstance(digital, str):
        ds = (digital + '00000000')[:8]
    else:
        ds = ''.join('1' if (int(digital) >> (7 - i)) & 1 else '0' for i in range(8))
    return ('T#%03d,%d,%d,%d,%d,%d,%s' % (int(seq) % 1000, a[0], a[1], a[2], a[3], a[4], ds)
            ).encode('ascii')


def aprs_message(addressee, text, msgid=None):
    """APRS 文本消息。addressee 补到 9 字符，正文 ≤ 67。"""
    to = (addressee or '').upper()[:9].ljust(9)
    body = (text or '')[:67]
    tail = ('{%s' % msgid) if msgid is not None else ''
    return (':' + to + ':' + body + tail).encode('utf-8', 'replace')


def aprs_object(name, lat, lon, symbol_table='/', symbol_code='-', comment='',
                ts=None, alive=True):
    """对象报文（name 补到 9 字符）。"""
    nm = (name or '')[:9].ljust(9)
    la, lo = _latlon_str(lat, lon, 0)
    st = '*' if alive else '_'
    head = ';' + nm + st + _aprs_time(ts, 'z')
    return (head + la + symbol_table + lo + symbol_code + (comment or '')
            ).encode('utf-8', 'replace')


# ===========================================================================
# 四、APRS 报文解析（接收侧）
# ===========================================================================
def _b91(s, i):
    return (ord(s[i]) - 33) if 33 <= ord(s[i]) <= 123 else 0


_UNCOMP_RE = re.compile(r'^\d{4}\.\d{2}[NS]')


def _parse_compressed(body):
    """压缩位置。body 从「符号表字符」开始：<table><4位纬度><4位经度><符号码>...

    纬经度用 base-91 编码（除数 380926 / 190463），这部分各实现一致。
    body[10:12] 是 course/speed 或高度域，各软件实现存在分歧，**不做猜测**，
    原样存进 cs_raw 供人工判读——宁可不显示，也不能把错的数值当真值展示。
    """
    if len(body) < 10:
        return None
    lat = 90.0 - (_b91(body, 1) * 91 ** 3 + _b91(body, 2) * 91 ** 2
                  + _b91(body, 3) * 91 + _b91(body, 4)) / 380926.0
    lon = -180.0 + (_b91(body, 5) * 91 ** 3 + _b91(body, 6) * 91 ** 2
                    + _b91(body, 7) * 91 + _b91(body, 8)) / 190463.0
    return {'lat': round(lat, 6), 'lon': round(lon, 6),
            'symbol_table': body[0], 'symbol_code': body[9],
            'cs_raw': body[10:12], 'comment': body[12:], 'compressed': True}


def _parse_uncompressed(body):
    """未压缩位置。body 从「纬度首字符」开始（data type 与时间戳已剥掉）：
    DDMM.mmN<符号表>DDDMM.mmE<符号码><注释>"""
    if len(body) < 19:
        return None
    la, lo = body[0:8], body[9:18]

    def dec(txt, deg_w, hemi_n):
        hemi = txt[deg_w + 5].upper()   # DDMM.mmN 中 N 的下标 = deg_w+5
        try:
            deg = int(txt[:deg_w])
            minute = float(txt[deg_w:deg_w + 5])
        except Exception:
            return None
        v = deg + minute / 60.0
        return -v if hemi == hemi_n else v

    lat = dec(la, 2, 'S')
    lon = dec(lo, 3, 'W')
    if lat is None or lon is None:
        return None
    amb = len(la) - len(la.rstrip())
    return {'lat': round(lat, 6), 'lon': round(lon, 6),
            'symbol_table': body[8], 'symbol_code': body[18], 'comment': body[19:],
            'ambiguity': amb}


def parse_wx_text(text):
    """从报文正文里抽取气象字段，返回物理量字典（SI 单位）。"""
    wx = {}
    m = WX_RE['wind'].search(text) or re.search(r'c(\d{3})s(\d{3})', text)
    if m:
        g = m.groupdict()
        if 'dir' in g:
            wx['wind_dir'] = int(g['dir'])
            wx['wind_ms'] = round(int(g['spd']) / 2.23694, 2)
        else:
            wx['wind_dir'] = int(m.group(1))
            wx['wind_ms'] = round(int(m.group(2)) / 2.23694, 2)
    m = WX_RE['gust'].search(text)
    if m:
        wx['gust_ms'] = round(int(m.group('v')) / 2.23694, 2)
    m = WX_RE['temp'].search(text)
    if m:
        wx['temp_c'] = round((int(m.group('v')) - 32) * 5.0 / 9.0, 1)
    for k, mm in (('rain1h', 'rain_1h_mm'), ('rain24', 'rain_24h_mm'),
                  ('rainsince', 'rain_today_mm')):
        m = WX_RE[k].search(text)
        if m:
            wx[mm] = round(int(m.group('v')) * 0.254, 2)
    m = WX_RE['hum'].search(text)
    if m:
        wx['humidity'] = int(m.group('v'))
    m = WX_RE['baro'].search(text)
    if m:
        v = m.group('v')
        wx['pressure_hpa'] = round((1000.0 if v[0] == '1' else 900.0)
                                   + int(v[1:]) / 10.0, 1)
    m = WX_RE['solar'].search(text[1:] if text[:1] == '_' else text)
    if m:
        wx['solar_wm2'] = int(m.group('v'))
    if not wx:
        return None
    wx['is_weather'] = True
    return wx


# --- Mic-E（APRS 101 §10）------------------------------------------------
# 编码要点：6 个目的地址字符承载纬度 DDMMhh 与三个消息位、南北半球、经度 +100
# 偏移、东西半球；信息字段前 8 字节承载经度(3)、速度/航向(3)、符号码、符号表。
# 逐位算法对照 direwolf `mic_e_digit()` / `aprs_mic_e()`。
MIC_E_STD_MSG = ['紧急 Emergency', '优先 Priority', '特别 Special', '待命 Committed',
                 '返程 Returning', '值勤 In Service', '在途 En Route', '下班 Off Duty']
MIC_E_CUST_MSG = ['紧急 Emergency', '自定义-6', '自定义-5', '自定义-4',
                  '自定义-3', '自定义-2', '自定义-1', '自定义-0']


def _mic_e_digit(c, mask, std_msg, cust_msg):
    """目的地址字符 -> (纬度数字, std_msg, cust_msg)。

    只有 'K'（自定义集）与 'Z'（标准集）会点亮对应的消息位；
    '0'-'9' / 'A'-'J' / 'P'-'Y' 三段的数字都是各自段的 0..9。
    """
    o = ord(c) if c else 0
    if 0x30 <= o <= 0x39:                      # '0'-'9'
        return o - 0x30, std_msg, cust_msg
    if 0x41 <= o <= 0x4A:                      # 'A'-'J'
        return o - 0x41, std_msg, cust_msg
    if 0x50 <= o <= 0x59:                      # 'P'-'Y'
        return o - 0x50, std_msg, cust_msg
    if c == 'K':
        return 0, std_msg, cust_msg | mask
    if c == 'Z':
        return 0, std_msg | mask, cust_msg
    if c == 'L':
        return 0, std_msg, cust_msg
    return 0, std_msg, cust_msg                # 非法字符按 0 处理并继续


def parse_mic_e(dest, s):
    """解析 Mic-E：dest 为目的地址呼号（纬度来源），s 为完整信息字段。

    返回 dict（含 lat/lon/symbol/速度航向/消息位），无法定位时 lat/lon 为 None。
    """
    dest = (dest or '').upper().ljust(6)
    out = {'dtype': 'mice', 'comment': '', 'mice_dest': dest.strip()}
    if len(s) < 9:
        return out
    out['mice_dti'] = s[0]
    out['mice_msg_capable'] = (s[0] == '`')    # ` = 支持消息, ' = 单向追踪器

    std_msg = cust_msg = 0
    dg = []
    for i, mask in enumerate((4, 2, 1)):
        d, std_msg, cust_msg = _mic_e_digit(dest[i], mask, std_msg, cust_msg)
        dg.append(d)
    for i in range(3, 6):
        d, std_msg, cust_msg = _mic_e_digit(dest[i], 0, std_msg, cust_msg)
        dg.append(d)

    # 纬度 = DD + MMhh/6000（digits[2:6] 组成 MMhh 四位）
    lat = dg[0] * 10 + dg[1] + (dg[2] * 1000 + dg[3] * 100 + dg[4] * 10 + dg[5]) / 6000.0
    # 南北：'0'-'9' / 'L' 为南，'P'-'Z' 为北（与东西方向相反，是 Mic-E 的固有约定）
    if (dest[3].isdigit() or dest[3] == 'L'):
        lat = -lat
    out['mice_lat_ns'] = 'S' if (dest[3].isdigit() or dest[3] == 'L') else 'N'

    # 经度 +100 偏移位：dest[4] 在 'P'-'Z' 时为 1
    offset = 1 if ('P' <= dest[4] <= 'Z') else 0
    out['mice_lon_offset'] = offset

    lon = None
    ch = ord(s[1])
    if offset and 118 <= ch <= 127:
        lon = ch - 118
    elif (not offset) and 38 <= ch <= 127:
        lon = (ch - 38) + 10
    elif offset and 108 <= ch <= 117:
        lon = (ch - 108) + 100
    elif offset and 38 <= ch <= 107:
        lon = (ch - 38) + 110
    out['mice_lon'] = lon

    if lon is not None:
        ch = ord(s[2])
        if 88 <= ch <= 97:
            lon += (ch - 88) / 60.0
        elif 38 <= ch <= 87:
            lon += ((ch - 38) + 10) / 60.0
        else:
            lon = None
    if lon is not None:
        ch = ord(s[3])
        if 28 <= ch <= 127:
            lon += (ch - 28) / 6000.0
        else:
            lon = None
    # 东西：'0'-'9' / 'L' 为东，'P'-'Z' 为西
    out['mice_lon_ew'] = 'W' if ('P' <= dest[5] <= 'Z') else 'E'
    if lon is not None and out['mice_lon_ew'] == 'W':
        lon = -lon

    if lon is not None:
        out['lat'] = round(lat, 6)
        out['lon'] = round(lon, 6)

    # 速度/航向：两段各 3 字节交错，需做 800 / 400 回绕修正
    sc = [ord(s[4]), ord(s[5]), ord(s[6])]
    n = (sc[0] - 28) * 10 + (sc[1] - 28) // 10
    if n >= 800:
        n -= 800
    out['speed_kt'] = n
    n2 = ((sc[1] - 28) % 10) * 100 + (sc[2] - 28)
    if n2 >= 400:
        n2 -= 400
    out['course'] = None if n2 == 0 else (0 if n2 == 360 else n2)

    # 符号：Mic-E 里「符号码在前、符号表在后」，与常规位置报文相反
    out['symbol_code'] = s[7]
    out['symbol_table'] = s[8]
    text = s[9:]

    # 可选高度：文本最前面是 3 个 base-91 字符 + '}'（单位米，基值 10000）
    m = re.match(r'^([\x21-\x7b]{3})\}', text)
    if m:
        v = 0
        for ch2 in m.group(1):
            v = v * 91 + (ord(ch2) - 33)
        out['alt_m'] = v - 10000
        text = text[m.end():]

    out['comment'] = text
    out['mice_std_msg'] = std_msg
    out['mice_cust_msg'] = cust_msg
    if std_msg == 0 and cust_msg == 0:
        out['mice_status'] = '未指定（消息位全 0）'
    elif std_msg and not cust_msg:
        out['mice_status'] = MIC_E_STD_MSG[std_msg & 7]
    elif cust_msg and not std_msg:
        out['mice_status'] = MIC_E_CUST_MSG[cust_msg & 7]
    else:
        out['mice_status'] = '非法组合（标准集与自定义集混用）'
    return out


def parse_info(info, dest=''):
    """解析信息字段 -> 结构化字典。永远保留原文，未知类型不丢数据。"""
    if isinstance(info, bytes):
        raw = info
        try:
            s = info.decode('utf-8')
        except UnicodeDecodeError:
            s = info.decode('latin-1')
    else:
        s, raw = str(info), str(info).encode('utf-8', 'replace')
    out = {'dtype': 'unknown', 'raw_text': s, 'comment': ''}
    if not s:
        return out
    c = s[0]

    if c in '!=/@':
        # '/' 与 '@' 在纬度前面还有 7 字符时间戳（DDHHMMz 或 DDHHMM/），
        # 必须先剥掉；否则时间戳会被当成纬度前两位，解出完全错误的坐标。
        body = s[1:]
        if c in '/@':
            if len(s) < 8:
                return out
            out['aprs_time'] = s[1:8]
            out['has_timestamp'] = True
            body = s[8:]
        if not body:
            return out
        pos = _parse_uncompressed(body) if _UNCOMP_RE.match(body) \
            else _parse_compressed(body)
        if not pos:
            return out
        out.update(pos)
        out['dtype'] = 'position'
        if c in '=/@':
            out['messaging'] = True
        wx = parse_wx_text(out.get('comment') or '')
        if wx:
            out['dtype'] = 'weather'
            out['wx'] = wx
        return out

    if c == '_':
        out['dtype'] = 'weather'
        out['comment'] = s[1:]
        wx = parse_wx_text(s)
        if wx:
            out['wx'] = wx
        return out

    if c == '>':
        out['dtype'] = 'status'
        body = s[1:]
        m = re.match(r'^(\d{6,7}[z/])?(.*)$', body)
        if m:
            out['comment'] = m.group(2)
        else:
            out['comment'] = body
        return out

    if c == ':':
        out['dtype'] = 'message'
        if len(s) >= 11:
            out['msg_to'] = s[1:10].strip()
            rest = s[11:]
            m = re.match(r'^(.*?)\{([A-Za-z0-9]{1,5})$', rest)
            if m:
                out['msg_text'] = m.group(1)
                out['msg_id'] = m.group(2)
            else:
                out['msg_text'] = rest
            out['comment'] = out['msg_text']
            if 'msg_id' in out and out.get('msg_text', '') == 'ack':
                out['msg_ack_id'] = out.get('msg_id')
        return out

    if c == ';':
        out['dtype'] = 'object'
        # 对象格式：';' + 名称(9) + 存活标志(1) + 时间戳(7) + 位置(19) + 注释
        if len(s) >= 29:
            out['obj_name'] = s[1:10].strip()
            out['obj_alive'] = (s[10:11] != '_')
            out['obj_time'] = s[11:18]
            pos = _parse_uncompressed(s[18:])
            if pos:
                out.update(pos)
                out['comment'] = pos.get('comment', '')
        return out

    if c == ')':
        out['dtype'] = 'item'
        out['comment'] = s[1:]
        return out

    if c == 'T' and s[1:2] == '#':
        out['dtype'] = 'telemetry'
        parts = s[2:].split(',')
        if len(parts) >= 2:
            out['seq'] = parts[0].strip()
            vals = []
            for p in parts[1:6]:
                try:
                    vals.append(int(p.strip()[:3].strip() or 0))
                except Exception:
                    vals.append(None)
            out['analogs'] = vals
            if len(parts) >= 7:
                out['digital'] = parts[6].strip()
        return out

    if c == '?':
        out['dtype'] = 'query'
        out['comment'] = s[1:]
        return out

    if c in '`\'':
        out['mice_raw'] = s
        mic = parse_mic_e(dest, s)
        for k, v in mic.items():
            if v is not None:
                out[k] = v
        out['dtype'] = 'mice'
        return out

    if c == '$':
        out['dtype'] = 'nmea'
        out['comment'] = s[1:]
        return out

    if c == '{':
        out['dtype'] = 'userdef'
        out['comment'] = s[1:]
        return out

    out['comment'] = s[1:]
    return out


def parse_packet(pkt):
    """完整 AX.25 帧 -> 统一的结构化记录（供入库）。"""
    fr = parse_frame(pkt)
    if not fr:
        return None
    # Mic-E 的纬度编码在目的地址里，必须把目的呼号一起传进去
    info = parse_info(fr['info'], (fr['dst'] or {}).get('call', ''))
    rec = {
        'src': fmt_addr(fr['src']), 'src_call': (fr['src'] or {}).get('call', ''),
        'src_ssid': (fr['src'] or {}).get('ssid', 0),
        'dst': fmt_addr(fr['dst']), 'dst_call': (fr['dst'] or {}).get('call', ''),
        'path': addr_text(fr['addrs']),
        'digis': [fmt_addr(a) + ('*' if a.get('h') else '') for a in fr['digis']],
        'ctrl': fr['ctrl'], 'pid': fr['pid'],
        'frame_len': fr['len'],
        'raw': bytes(pkt),
        'raw_hex': bytes(pkt).hex().upper(),
        'info_raw': fr['info'],
        'info': info.get('raw_text', ''),
        'dtype': info.get('dtype', 'unknown'),
        'digi_count': sum(1 for a in fr['digis'] if a.get('h')),
        'direct': not any(a.get('h') for a in fr['digis']),
    }
    for k in ('lat', 'lon', 'symbol_table', 'symbol_code', 'comment', 'wx',
              'analogs', 'digital', 'seq', 'msg_to', 'msg_text', 'msg_id',
              'obj_name', 'course', 'speed_kt', 'alt_m', 'ambiguity',
              'mice_dti', 'mice_dest', 'mice_lat_ns', 'mice_lon_ew',
              'mice_lon_offset', 'mice_status', 'mice_std_msg', 'mice_cust_msg',
              'mice_msg_capable', 'mice_raw'):
        if k in info:
            rec[k] = info[k]
    rec['dtype_label'] = TYPE_LABEL.get(rec['dtype'], rec['dtype'])
    return rec


# ===========================================================================
# 五、位置来源抽象（手动固定坐标 / NMEA，ZED-F9P 预留）
# ===========================================================================
class PositionProvider:
    """位置提供者。manual 走设置；nmea 走串口 GPS（ZED-F9P RTK 基准站预留）。

    换 GPS 时只需把 aprs_pos_source 改成 nmea 并填 aprs_gps_port，
    其余代码（信标、气象、遥测）无需改动。
    """

    def __init__(self, svc):
        self.svc = svc
        self.lock = threading.Lock()
        self.ser = None
        self.last = None
        self._open_key = None
        self.stat = {'source': 'manual', 'ok': False, 'err': '', 'ts': 0,
                     'sats': None, 'hdop': None, 'fix': ''}

    def _setting(self, k, d=''):
        return self.svc.setting(k, d)

    def get(self):
        src = (self._setting('aprs_pos_source', 'manual') or 'manual').lower()
        if src == 'nmea':
            r = self._from_nmea()
            if r:
                return r
        lat = _f(self._setting('aprs_lat', DEFAULTS['aprs_lat']), 0.0)
        lon = _f(self._setting('aprs_lon', DEFAULTS['aprs_lon']), 0.0)
        with self.lock:
            self.stat.update(source='manual', ok=bool(lat or lon), err='', ts=time.time())
        return {'lat': lat, 'lon': lon, 'alt_m': _f(self._setting('aprs_alt_m', ''), None),
                'source': 'manual', 'valid': True, 'ts': time.time(),
                'sats': None, 'hdop': None, 'fix': 'manual'}

    # ---- NMEA（预留给 ZED-F9P）----
    def _from_nmea(self):
        port = (self._setting('aprs_gps_port', '') or '').strip()
        if not port:
            with self.lock:
                self.stat.update(source='nmea', ok=False, err='未配置 aprs_gps_port')
            return None
        baud = int(_f(self._setting('aprs_gps_baud', '38400'), 38400))
        try:
            import serial
        except Exception as e:
            with self.lock:
                self.stat.update(source='nmea', ok=False, err='pyserial 不可用：%s' % e)
            return None
        key = '%s@%d' % (port, baud)
        if self.ser is None or self._open_key != key:
            try:
                if self.ser:
                    self.ser.close()
            except Exception:
                pass
            try:
                self.ser = serial.Serial(port, baud, timeout=1.0)
                self._open_key = key
            except Exception as e:
                with self.lock:
                    self.stat.update(source='nmea', ok=False, err='打开 %s 失败：%s' % (port, e))
                return None
        deadline = time.time() + 2.5
        try:
            while time.time() < deadline:
                line = self.ser.readline().decode('ascii', 'ignore').strip()
                if not line.startswith('$'):
                    continue
                fixed = self._nmea_line(line)
                if fixed:
                    with self.lock:
                        self.last = fixed
                        self.stat.update(source='nmea', ok=True, err='', ts=time.time(),
                                         sats=fixed.get('sats'), hdop=fixed.get('hdop'),
                                         fix=fixed.get('fix', ''))
                    return fixed
        except Exception as e:
            with self.lock:
                self.stat.update(source='nmea', ok=False, err='%s' % e)
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None
            return None
        return self.last

    @staticmethod
    def _nmea_deg(v, hemi):
        if not v:
            return None
        try:
            dot = v.index('.')
            deg = int(v[:dot - 2])
            minute = float(v[dot - 2:])
        except Exception:
            return None
        d = deg + minute / 60.0
        return -d if hemi in ('S', 'W') else d

    def _nmea_line(self, line):
        try:
            body = line.split('*')[0]
            parts = body.split(',')
            tag = parts[0][3:]
            if tag == 'GGA' and len(parts) >= 10 and parts[6] not in ('', '0'):
                return {'lat': round(self._nmea_deg(parts[2], parts[3]), 7),
                        'lon': round(self._nmea_deg(parts[4], parts[5]), 7),
                        'alt_m': _f(parts[9], None), 'sats': int(parts[7] or 0),
                        'hdop': _f(parts[8], None), 'fix': 'GGA',
                        'ts': time.time(), 'source': 'nmea', 'valid': True}
            if tag == 'RMC' and len(parts) >= 8 and parts[2] == 'A':
                return {'lat': round(self._nmea_deg(parts[3], parts[4]), 7),
                        'lon': round(self._nmea_deg(parts[5], parts[6]), 7),
                        'speed_kt': _f(parts[7], 0.0), 'fix': 'RMC',
                        'ts': time.time(), 'source': 'nmea', 'valid': True}
        except Exception:
            return None
        return None


# ===========================================================================
# 六、存储层
# ===========================================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS aprs_packets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, ts_epoch REAL,
    audio_t REAL,
    src TEXT, src_call TEXT, src_ssid INTEGER,
    dst TEXT, dst_call TEXT,
    path TEXT, digi_count INTEGER, direct INTEGER,
    ctrl INTEGER, pid INTEGER, frame_len INTEGER,
    dtype TEXT, dtype_label TEXT,
    info TEXT, info_hex TEXT, raw_hex TEXT,
    lat REAL, lon REAL, symbol_table TEXT, symbol_code TEXT,
    comment TEXT, ambiguity INTEGER,
    course REAL, speed_kt REAL, alt_m REAL,
    wx_json TEXT, telemetry_json TEXT, mice_json TEXT,
    msg_to TEXT, msg_text TEXT, msg_id TEXT,
    obj_name TEXT,
    source TEXT DEFAULT 'rx',
    created TEXT
);
CREATE INDEX IF NOT EXISTS idx_aprs_ts ON aprs_packets(ts_epoch);
CREATE INDEX IF NOT EXISTS idx_aprs_src ON aprs_packets(src);
CREATE INDEX IF NOT EXISTS idx_aprs_pos ON aprs_packets(lat, lon);

CREATE TABLE IF NOT EXISTS aprs_tx (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, ts_epoch REAL,
    trigger TEXT, ptype TEXT,
    to_call TEXT,
    info TEXT, frame_hex TEXT,
    ok INTEGER, error TEXT,
    defer_ms INTEGER, cs_wait_ms INTEGER, audio_ms INTEGER, ptt_ms INTEGER,
    created TEXT
);
CREATE INDEX IF NOT EXISTS idx_aprstx_ts ON aprs_tx(ts_epoch);
"""


# ===========================================================================
# 七、位置检索：距离 / 方位 / 站点反查（语音助手的位置类工具复用这里）
# ===========================================================================
# 全部是纯函数：不碰数据库、不碰线程，便于离线自测。
# 目的：让中继台「知道」某台在哪，并用中文口语说出距离与方位
# （「BI7KHI-9 在东北方向 3.2 公里」），供助手语音引导。

_COMPASS_CN = ('北', '东北', '东', '东南', '南', '西南', '西', '西北')


def haversine_km(lat1, lon1, lat2, lon2):
    """两点大圆距离（公里）。无效输入返回 None。"""
    try:
        la1, lo1 = float(lat1), float(lon1)
        la2, lo2 = float(lat2), float(lon2)
    except (TypeError, ValueError):
        return None
    r = 6371.0088
    p1, p2 = math.radians(la1), math.radians(la2)
    dp = p2 - p1
    dl = math.radians(lo2 - lo1)
    a = (math.sin(dp / 2.0) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2)
    return 2.0 * r * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1, lon1, lat2, lon2):
    """起点→终点的初始方位角（0=正北，顺时针）。无效输入返回 None。"""
    try:
        la1, lo1 = float(lat1), float(lon1)
        la2, lo2 = float(lat2), float(lon2)
    except (TypeError, ValueError):
        return None
    p1, p2 = math.radians(la1), math.radians(la2)
    dl = math.radians(lo2 - lo1)
    y = math.sin(dl) * math.cos(p2)
    x = (math.cos(p1) * math.sin(p2)
         - math.sin(p1) * math.cos(p2) * math.cos(dl))
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def compass_cn(deg):
    """方位角 → 中文八方位（北/东北/…）。"""
    try:
        d = float(deg) % 360.0
    except (TypeError, ValueError):
        return ''
    return _COMPASS_CN[int(d / 45.0 + 0.5) % 8]


def call_base(call):
    """呼号去掉 SSID 后缀并大写：'bi7khi-9' → 'BI7KHI'。"""
    return str(call or '').strip().upper().split('-')[0]


def call_match(stored, want):
    """呼号匹配：完全相同，或主呼号相同（BI7KHI 能匹配上 BI7KHI-9）。

    空 want 视为「任意」——语音里用户往往只报主呼号。
    """
    a = str(stored or '').strip().upper()
    b = str(want or '').strip().upper()
    if not b:
        return bool(a)
    if not a:
        return False
    return a == b or call_base(a) == call_base(b)


def call_exact(stored, want):
    """呼号**完全**相同（含 SSID）。

    专给「排除本站自己」用：这里绝不能用 call_match —— 它按主呼号匹配，会把
    同一操作者的其它 SSID 一起排掉，而 BI7KHI-9 往往正是用户手上那台，
    排掉它等于把要查的目标丢了。
    """
    return str(stored or '').strip().upper() == str(want or '').strip().upper()


def pick_latest_station(items, want=''):
    """从位置记录里挑出呼号匹配且 ts_epoch 最新的一条（无匹配返回 None）。

    给了带 SSID 的呼号（BI7KHI-9）时**优先精确命中**：不然同操作者的
    BI7KHI-10 只要比它新一点就会把答案顶掉，问谁答谁就不成立了。
    精确的一个都没有时，才退回到「主呼号相同」的宽匹配。
    空 want = 任意，取全局最新。
    """
    want = str(want or '').strip()
    exact, loose = [], []
    for it in (items or ()):
        if not isinstance(it, dict):
            continue
        call = it.get('call') or it.get('src')
        if not want:
            loose.append(it)
        elif call_exact(call, want):
            exact.append(it)
        elif call_match(call, want):
            loose.append(it)
    best = None
    for it in (exact or loose):
        if best is None or _f(it.get('ts_epoch'), 0.0) > _f(best.get('ts_epoch'), 0.0):
            best = it
    return best


def station_brief(rec, home=None, now=None, exclude=()):
    """把一条位置记录整理成**紧凑**结果（板端模型只看数值，别给整段 JSON）。

    home 为本站位置 {lat,lon}；给了就附上距离与中文方位。
    """
    if not isinstance(rec, dict):
        return None
    call = str(rec.get('call') or rec.get('src') or '').strip()
    if not call:
        return None
    for ex in (exclude or ()):
        if ex and call_exact(call, ex):
            return None
    now = time.time() if now is None else now
    lat, lon = _f(rec.get('lat'), None), _f(rec.get('lon'), None)
    out = {'call': call, 'lat': round(lat, 5) if lat is not None else None,
           'lon': round(lon, 5) if lon is not None else None}
    se = _f(rec.get('ts_epoch'), 0.0)
    if se > 0:
        out['age_min'] = int(max(0.0, now - se) / 60.0)
    if rec.get('comment'):
        out['comment'] = str(rec['comment'])[:40]
    if _f(rec.get('speed_kt'), None) is not None:
        out['speed_kt'] = round(_f(rec['speed_kt'], 0.0), 1)
    hl, ho = None, None
    if isinstance(home, dict):
        hl, ho = _f(home.get('lat'), None), _f(home.get('lon'), None)
    if lat is not None and lon is not None and hl is not None and ho is not None:
        km = haversine_km(hl, ho, lat, lon)
        if km is not None:
            out['km'] = round(km, 1)
            out['dir'] = compass_cn(bearing_deg(hl, ho, lat, lon))
    return out


def nearest_stations(items, home=None, km=50.0, limit=5, exclude=()):
    """按呼号去重（取最新），以 home 为中心挑出半径内最近的若干台。

    home 缺失时退化为「最近听到的若干台」，不带距离。
    """
    try:
        limit = max(1, min(20, int(limit)))
    except (TypeError, ValueError):
        limit = 5
    try:
        km = float(km)
    except (TypeError, ValueError):
        km = 50.0
    best = {}
    for it in (items or ()):
        if not isinstance(it, dict):
            continue
        call = str(it.get('call') or it.get('src') or '').strip()
        if not call:
            continue
        cur = best.get(call)
        if cur is None or _f(it.get('ts_epoch'), 0.0) > _f(cur.get('ts_epoch'), 0.0):
            best[call] = it
    out = []
    for call, it in best.items():
        b = station_brief(it, home=home, exclude=exclude)
        if b is None:
            continue
        if 'km' in b and km > 0 and b['km'] > km:
            continue
        out.append(b)
    # 有距离的按距离排；没有距离（无本站坐标）时按时间新→旧
    out.sort(key=lambda x: (x.get('km') if x.get('km') is not None else 1e9,
                            x.get('age_min', 1e9)))
    return out[:limit]


class Store:
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
                # 已有库补列：CREATE TABLE IF NOT EXISTS 不会给已存在的表加列，
                # 因此新增字段必须显式 ALTER TABLE 做迁移。
                for col in ('mice_json',):
                    try:
                        c.execute('ALTER TABLE aprs_packets ADD COLUMN %s TEXT' % col)
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


# ===========================================================================
# 八、服务主体
# ===========================================================================
class AprsService:
    """APRS 收发服务。

    configure() 由 app.py 注入：
        carrier_busy()  -> bool  信道是否忙（BUSY 或 PTT 高）
        play_raw(pcm, rate, hold_ptt) -> dict  播放原始 PCM（自带 PTT 保持）
        wx_getter()     -> dict  气象数据（来自 Modbus 气象站）
        power_getter()  -> dict  电压数据
        temp_getter()   -> float CPU 温度
        setting_getter  -> 读设置
    """

    def __init__(self, db_path):
        self.store = Store(db_path)
        self.lock = threading.RLock()
        self.cfg = {
            'carrier_busy': None, 'play_raw': None, 'wx_getter': None,
            'power_getter': None, 'temp_getter': None, 'setting_getter': None,
            'stats_getter': None,
        }
        self._settings_cache = {}
        self._settings_ts = 0.0
        self.tnc = None
        self.position = PositionProvider(self)
        self.run_flag = True
        self.tx_lock = threading.Lock()
        self.next_tx = {}            # ptype -> 下次发射 epoch
        self._sched_iv = {}          # ptype -> 上次生效的间隔（秒），用于检测设置变更
        self.stats = {
            'rx_total': 0, 'rx_dropped': 0, 'tx_ok': 0, 'tx_fail': 0,
            'tx_deferred': 0, 'tx_skipped': 0, 'audio_s': 0.0,
            'last_rx': 0.0, 'last_tx': 0.0, 'last_error': '', 'started': time.time(),
            'stations': 0, 'positions': 0, 'weather': 0, 'telemetry': 0,
            'messages': 0, 'bursts': 0, 'frames_raw': 0,
        }
        self._stations = {}          # call -> {lat,lon,ts,...} 最近位置（地图用）
        self._track = deque(maxlen=3000)
        self._seq = self.store.one('SELECT COALESCE(MAX(id),0) AS n FROM aprs_tx') or {'n': 0}
        self.telemetry_seq = int(self._seq['n'])
        self._dedup = {}
        self._init_threads()

    # ---------------- 配置 / 设置 ----------------
    def configure(self, carrier_busy=None, play_raw=None, wx_getter=None,
                  power_getter=None, temp_getter=None, setting_getter=None,
                  stats_getter=None):
        self.cfg.update(carrier_busy=carrier_busy, play_raw=play_raw,
                        wx_getter=wx_getter, power_getter=power_getter,
                        temp_getter=temp_getter, setting_getter=setting_getter,
                        stats_getter=stats_getter)

    def setting(self, key, default=''):
        g = self.cfg.get('setting_getter')
        if callable(g):
            try:
                v = g(key, default)
                return default if v is None or v == '' else v
            except Exception:
                return default
        return default

    def settings(self, force=False):
        now = time.time()
        if force or not self._settings_cache or now - self._settings_ts > 5.0:
            self._settings_cache = {k: self.setting(k, v) for k, v in DEFAULTS.items()}
            self._settings_ts = now
        return self._settings_cache

    def invalidate(self):
        self._settings_ts = 0.0

    def enabled(self):
        return _flag(self.settings().get('aprs_enabled'), True)

    def _ensure_tnc(self, st=None):
        st = st or self.settings()
        thr = _f(st.get('aprs_burst_threshold'), 0.30)
        nph = int(_f(st.get('aprs_phase_trials'), 64))
        bms = _f(st.get('aprs_burst_min_ms'), 70.0)
        if self.tnc is None:
            self.tnc = Tnc(SAMPLE_RATE, nphase=nph, threshold=thr, burst_min_ms=bms)
        else:
            self.tnc.nphase = max(8, nph)
            self.tnc.threshold = thr
            self.tnc.burst_min = max(8, int(round(bms / 1000.0 * SAMPLE_RATE / self.tnc.spb)))
        return self.tnc

    # ---------------- 采集中枢喂入 ----------------
    @staticmethod
    def pick_channel(raw, channel='left'):
        usable = len(raw) - (len(raw) % 4)
        if usable <= 0:
            return None
        raw = raw[:usable]
        a = np.frombuffer(raw, dtype=np.int16)
        if channel == 'mix':
            b = a.reshape(-1, 2).astype(np.int32)
            return ((b[:, 0] + b[:, 1]) // 2).astype(np.float64) / 32768.0
        idx = 0 if channel != 'right' else 1
        return a.reshape(-1, 2)[:, idx].astype(np.float64) / 32768.0

    def feed(self, raw_stereo, ts=None):
        """由 app.py 的采集中枢调用（与 voice_service.feed 并列）。"""
        if not self.enabled():
            return 0
        try:
            st = self.settings()
            mono = self.pick_channel(raw_stereo, st.get('aprs_channel', 'left'))
            if mono is None or mono.size == 0:
                return 0
            tnc = self._ensure_tnc(st)
            frames = tnc.feed(mono)
        except Exception as e:
            self.stats['last_error'] = 'feed: %s: %s' % (type(e).__name__, e)
            return 0
        n = 0
        for (t, fr) in frames:
            try:
                if self._ingest(t, fr):
                    n += 1
            except Exception as e:
                self.stats['last_error'] = 'ingest: %s: %s' % (type(e).__name__, e)
        self.stats['bursts'] = tnc.stats.get('bursts', 0)
        self.stats['frames_raw'] = tnc.stats.get('unique', 0)
        return n

    def _ingest(self, audio_t, frame):
        rec = parse_packet(frame)
        if not rec:
            return False
        now = time.time()
        st = self.settings()
        win = _f(st.get('aprs_dedup_window'), 45)
        key = '%s|%s' % (rec['src'], rec['info'])
        with self.lock:
            prev = self._dedup.get(key)
            if prev and now - prev < win:
                self.stats['rx_dropped'] += 1
                return False
            self._dedup[key] = now
            if len(self._dedup) > 2000:
                cut = now - win * 4
                self._dedup = {k: v for k, v in self._dedup.items() if v >= cut}
        rec.update(ts=_now_iso(), ts_epoch=now, audio_t=round(float(audio_t), 3),
                   source='rx', created=_now_iso())
        rid = self._insert(rec)
        rec['id'] = rid
        self.stats['rx_total'] += 1
        self.stats['last_rx'] = now
        if rec.get('lat') is not None and rec.get('lon') is not None:
            self.stats['positions'] += 1
            self._remember_station(rec)
        if rec['dtype'] == 'weather':
            self.stats['weather'] += 1
        elif rec['dtype'] == 'telemetry':
            self.stats['telemetry'] += 1
        elif rec['dtype'] == 'message':
            self.stats['messages'] += 1
        return True

    def _remember_station(self, rec):
        call = rec.get('src') or '?'
        with self.lock:
            self._stations[call] = {
                'call': call, 'lat': rec.get('lat'), 'lon': rec.get('lon'),
                'symbol_table': rec.get('symbol_table', '/'),
                'symbol_code': rec.get('symbol_code', '>'),
                'dtype': rec.get('dtype'), 'ts': rec.get('ts'),
                'ts_epoch': rec.get('ts_epoch'), 'comment': rec.get('comment', '')[:60],
                'speed_kt': rec.get('speed_kt'), 'alt_m': rec.get('alt_m'),
                'path': rec.get('path', ''), 'wx': rec.get('wx'),
            }
            self.stats['stations'] = len(self._stations)
            self._track.append((call, rec.get('lat'), rec.get('lon'),
                                rec.get('ts_epoch'), rec.get('speed_kt')))
            cap = int(_f(self.settings().get('aprs_track_points'), 300))
            while len(self._track) > cap * 8:
                self._track.popleft()

    def _insert(self, rec):
        cols = ('ts', 'ts_epoch', 'audio_t', 'src', 'src_call', 'src_ssid', 'dst',
                'dst_call', 'path', 'digi_count', 'direct', 'ctrl', 'pid',
                'frame_len', 'dtype', 'dtype_label', 'info', 'info_hex', 'raw_hex',
                'lat', 'lon', 'symbol_table', 'symbol_code', 'comment', 'ambiguity',
                'course', 'speed_kt', 'alt_m', 'wx_json', 'telemetry_json',
                'mice_json',
                'msg_to', 'msg_text', 'msg_id', 'obj_name', 'source', 'created')
        vals = []
        for c in cols:
            v = rec.get(c)
            if c == 'wx_json':
                v = json.dumps(rec.get('wx'), ensure_ascii=False) if rec.get('wx') else None
            elif c == 'mice_json':
                mice = {k: rec.get(k) for k in (
                    'mice_dti', 'mice_dest', 'mice_lat_ns', 'mice_lon_ew',
                    'mice_lon_offset', 'mice_status', 'mice_std_msg',
                    'mice_cust_msg', 'mice_msg_capable') if rec.get(k) is not None}
                v = json.dumps(mice, ensure_ascii=False) if mice else None
            elif c == 'telemetry_json':
                tel = {}
                if rec.get('analogs') or rec.get('digital') or rec.get('seq'):
                    tel = {'seq': rec.get('seq'), 'analogs': rec.get('analogs'),
                           'digital': rec.get('digital')}
                v = json.dumps(tel, ensure_ascii=False) if tel else None
            elif c == 'info_hex':
                v = rec.get('info_raw', b'').hex().upper()
            elif c == 'direct':
                v = 1 if rec.get('direct') else 0
            elif c in ('ctrl', 'pid', 'frame_len', 'src_ssid'):
                v = int(v) if v is not None else None
            vals.append(v)
        sql = ('INSERT INTO aprs_packets (%s) VALUES (%s)'
               % (','.join(cols), ','.join(['?'] * len(cols))))
        return self.store.exec(sql, tuple(vals))

    # ---------------- 发射 ----------------
    def _analog_value(self, spec):
        src = (spec or {}).get('src', '')
        scale = _f((spec or {}).get('scale'), 1.0)
        offset = _f((spec or {}).get('offset'), 0.0)
        raw = self._metric(src)
        if raw is None:
            return 0
        return int(round(_f(raw, 0.0) * scale + offset))

    def _metric(self, name):
        cfg = self.cfg
        if name in ('battery_v', 'pv_v'):
            g = cfg.get('power_getter')
            if callable(g):
                try:
                    return (g() or {}).get(name)
                except Exception:
                    return None
            return None
        if name == 'cpu_temp':
            g = cfg.get('temp_getter')
            if callable(g):
                try:
                    return g()
                except Exception:
                    return None
            return None
        if name == 'wind_ms':
            w = self._weather() or {}
            return w.get('wind_ms')
        if name == 'tx_count':
            return self.stats.get('tx_ok', 0)
        if name == 'rx_count':
            return self.stats.get('rx_total', 0)
        return None

    def _weather(self):
        g = self.cfg.get('wx_getter')
        if not callable(g):
            return None
        try:
            w = g() or {}
            return w if w else None
        except Exception:
            return None

    def _digital_bits(self, tmap):
        bits = 0
        st = self.settings()
        for i in range(1, 9):
            spec = tmap.get('d%d' % i)
            if not spec:
                continue
            src = spec.get('src', '')
            val = 0
            if src == 'ptt':
                cb = self.cfg.get('carrier_busy')
                val = 1 if (callable(cb) and self._ptt_high()) else 0
            elif src == 'busy':
                cb = self.cfg.get('carrier_busy')
                val = 1 if (callable(cb) and self._busy_only()) else 0
            elif src == 'wx_online':
                w = self._weather() or {}
                val = 1 if w.get('online') else 0
            elif src == 'aprs_enabled':
                val = 1 if _flag(st.get('aprs_enabled'), True) else 0
            elif src == 'beacon':
                val = 1 if _flag(st.get('aprs_beacon_enabled'), True) else 0
            if val:
                bits |= (1 << (8 - i))
        return bits

    def _ptt_high(self):
        g = self.cfg.get('ptt_level')
        if callable(g):
            try:
                return bool(g())
            except Exception:
                return False
        return False

    def _busy_only(self):
        g = self.cfg.get('busy_level')
        if callable(g):
            try:
                return bool(g())
            except Exception:
                return False
        return False

    def build_packet(self, ptype, **kw):
        """生成 (frame_bytes, info_text, to_call)。"""
        st = self.settings()
        call = (st.get('aprs_mycall') or 'BI7KHI').upper()
        ssid = int(_f(st.get('aprs_ssid'), 0))
        dst = (st.get('aprs_dest') or 'APRS').upper()
        path = parse_path(st.get('aprs_path', ''))
        pos = self.position.get()
        sym_t = st.get('aprs_symbol_table', '/') or '/'
        sym_c = st.get('aprs_symbol_code', '-') or '-'
        amb = int(_f(st.get('aprs_pos_ambiguity'), 0))
        base_comment = st.get('aprs_comment', '') or ''

        if ptype == 'position':
            info = aprs_position(pos['lat'], pos['lon'], sym_t, sym_c, base_comment,
                                 ambiguity=amb, messaging=False, timestamped=True,
                                 alt_m=pos.get('alt_m'))
        elif ptype == 'weather':
            wx = self._build_wx()
            # 气象站离线时不要发「不含任何气象字段的气象包」——接收方会看到一条
            # 名义上是 _WX 却没有测量值的记录，比不发更糟。直接取消并说明原因。
            if not wx:
                raise ValueError('气象数据源无数据（Modbus 气象站离线或未接入），已取消本次发射')
            info = aprs_weather(wx, pos['lat'], pos['lon'], sym_t,
                                kw.get('symbol_code') or '_',
                                comment=base_comment, ambiguity=amb,
                                with_position=kw.get('with_position', True))
        elif ptype == 'status':
            info = aprs_status(kw.get('text') or st.get('aprs_status_text', '') or '')
        elif ptype == 'telemetry':
            tmap = self._telemetry_map()
            analogs = [self._analog_value(tmap.get('a%d' % i)) for i in range(1, 6)]
            self.telemetry_seq = (self.telemetry_seq + 1) % 1000
            info = aprs_telemetry(self.telemetry_seq, analogs, self._digital_bits(tmap))
        elif ptype == 'message':
            info = aprs_message(kw.get('to') or call, kw.get('text') or '', kw.get('msgid'))
        elif ptype == 'raw':
            info = (kw.get('info') or '').encode('utf-8', 'replace')
        else:
            raise ValueError('未知报文类型：%s' % ptype)

        frame = build_frame(call, dst, path, info, src_ssid=ssid,
                            pid=int(kw.get('pid', 0xF0)))
        to_call = kw.get('to') or ''
        return frame, info.decode('utf-8', 'replace'), to_call

    def _telemetry_map(self):
        try:
            return json.loads(self.settings().get('aprs_telemetry_map') or '{}') or {}
        except Exception:
            return {}

    def _build_wx(self):
        """把气象站数据整理成 SI 单位字典（缺项留空，APRS 允许省略）。"""
        w = self._weather() or {}
        out = {}
        for k_src, k_out in (('wind_ms', 'wind_ms'), ('wind_dir', 'wind_dir'),
                             ('gust_ms', 'gust_ms'), ('temp_c', 'temp_c'),
                             ('humidity', 'humidity'), ('pressure_hpa', 'pressure_hpa')):
            if w.get(k_src) is not None:
                out[k_out] = w[k_src]
        if w.get('rain_1h_mm') is not None:
            out['rain_1h_mm'] = w['rain_1h_mm']
        if w.get('rain_24h_mm') is not None:
            out['rain_24h_mm'] = w['rain_24h_mm']
        if w.get('rain_today_mm') is not None:
            out['rain_today_mm'] = w['rain_today_mm']
        return out

    def carrier_sense_wait(self, st=None, log=None):
        """载波侦听：信道忙则等待；返回 (等待毫秒, 是否可发)。

        发射前必须让路——不只是礼貌问题：语音与 AFSK 叠加会让两边都解不出来。
        """
        st = st or self.settings()
        if not _flag(st.get('aprs_carrier_sense'), True):
            return 0, True
        cb = self.cfg.get('carrier_busy')
        if not callable(cb):
            return 0, True
        max_wait = _f(st.get('aprs_defer_max'), 120.0)
        t0 = time.time()
        while self.run_flag:
            try:
                busy = bool(cb())
            except Exception:
                busy = False
            if not busy:
                jit = _f(st.get('aprs_defer_jitter'), 3.0)
                if jit > 0:
                    time.sleep(random.uniform(0, jit))
                return int((time.time() - t0) * 1000), True
            if time.time() - t0 > max_wait:
                return int((time.time() - t0) * 1000), False
            time.sleep(0.25)
        return int((time.time() - t0) * 1000), False

    def send(self, ptype, trigger='manual', **kw):
        """发射一份 APRS 报文。返回结果字典。"""
        if not self.tx_lock.acquire(timeout=1.0):
            return {'ok': False, 'error': '另一个发射任务正在进行'}
        t0 = time.time()
        defer_ms = 0
        try:
            st = self.settings()
            gap = _f(st.get('aprs_min_gap'), 20.0)
            last = self.stats.get('last_tx') or 0
            if last and (t0 - last) < gap:
                wait = gap - (t0 - last)
                print('%s 距上次发射仅 %.1fs，强制等待 %.1fs' % (LOG, t0 - last, wait), flush=True)
                time.sleep(wait)
            defer_ms, ok = self.carrier_sense_wait(st)
            if not ok:
                self.stats['tx_skipped'] += 1
                self._log_tx(trigger, ptype, '', '', '', 1,
                             '信道持续忙 %.0fs，超过 aprs_defer_max，放弃本次' % (defer_ms / 1000.0),
                             defer_ms, defer_ms)
                return {'ok': False, 'error': '信道忙，放弃', 'defer_ms': defer_ms}
            if defer_ms:
                self.stats['tx_deferred'] += 1

            frame, info_text, to_call = self.build_packet(ptype, **kw)
            pcm = build_afsk(frame, SAMPLE_RATE)
            player = self.cfg.get('play_raw')
            if not callable(player):
                raise RuntimeError('未注入音频播放器（play_raw）')
            pcm16 = np.clip(pcm, -1.0, 1.0)
            raw = (pcm16 * 32767.0).astype('<i2').tobytes()
            audio_s = len(pcm) / float(SAMPLE_RATE)
            res = player(raw, SAMPLE_RATE, True) or {}
            ok = bool(res.get('ok', True))
            err = res.get('error', '')
            self._log_tx(trigger, ptype, to_call, info_text, frame.hex().upper(),
                         1 if ok else 0, err, defer_ms, int(res.get('cs_wait_ms') or defer_ms),
                         int(audio_s * 1000), int(res.get('ptt_ms') or 0))
            if ok:
                self.stats['tx_ok'] += 1
                self.stats['last_tx'] = time.time()
                self.stats['audio_s'] = self.stats.get('audio_s', 0.0) + audio_s
                print('%s 发射 %s（%s）: %s  [%.2fs 音频, 顺延 %.1fs]'
                      % (LOG, ptype, trigger, info_text[:80], audio_s, defer_ms / 1000.0),
                      flush=True)
            else:
                self.stats['tx_fail'] += 1
                self.stats['last_error'] = err
                print('%s 发射失败 %s: %s' % (LOG, ptype, err), flush=True)
            return {'ok': ok, 'error': err, 'info': info_text, 'type': ptype,
                    'frame_hex': frame.hex().upper(), 'defer_ms': defer_ms,
                    'audio_s': round(audio_s, 2), 'frames': 1}
        except Exception as e:
            self.stats['tx_fail'] += 1
            msg = '%s: %s' % (type(e).__name__, e)
            self.stats['last_error'] = msg
            try:
                self._log_tx(trigger, ptype, '', '', '', 0, msg, defer_ms, defer_ms)
            except Exception:
                pass
            print('%s 发射异常 %s: %s' % (LOG, ptype, msg), flush=True)
            return {'ok': False, 'error': msg}
        finally:
            self.tx_lock.release()

    def _log_tx(self, trigger, ptype, to_call, info, hexs, ok, error,
                defer_ms=0, cs_wait_ms=0, audio_ms=0, ptt_ms=0):
        self.store.exec(
            'INSERT INTO aprs_tx (ts,ts_epoch,trigger,ptype,to_call,info,frame_hex,'
            'ok,error,defer_ms,cs_wait_ms,audio_ms,ptt_ms,created) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (_now_iso(), time.time(), trigger, ptype, to_call, info, hexs,
             1 if ok else 0, (error or '')[:400], int(defer_ms), int(cs_wait_ms),
             int(audio_ms), int(ptt_ms), _now_iso()))

    # ---------------- 定时调度 ----------------
    def _sched_plan(self, st):
        now = time.time()
        jit = _f(st.get('aprs_jitter'), 20.0)
        plan = []
        for ptype, flag_key, iv_key in (
                ('weather', 'aprs_weather_enabled', 'aprs_weather_interval'),
                ('telemetry', 'aprs_telemetry_enabled', 'aprs_telemetry_interval'),
                ('position', 'aprs_beacon_enabled', 'aprs_beacon_interval'),
                ('status', 'aprs_status_enabled', 'aprs_status_interval')):
            if not _flag(st.get(flag_key), False):
                continue
            iv = max(30.0, _f(st.get(iv_key), 1800.0))
            if self._sched_iv.get(ptype) != iv:
                # 间隔设置被改动（含首次）→ 立即按新间隔重新计时。
                # 否则用户改完间隔要等旧周期走完才生效，看起来像“设置没起作用”。
                self._sched_iv[ptype] = iv
                self.next_tx[ptype] = now + iv + random.uniform(0, jit)
                continue
            nxt = self.next_tx.get(ptype)
            if nxt is None:
                self.next_tx[ptype] = now + iv + random.uniform(0, jit)
                continue
            if now >= nxt:
                plan.append((ptype, iv))
        return plan

    def _scheduler(self):
        time.sleep(20)
        while self.run_flag:
            try:
                st = self.settings()
                if self.enabled():
                    for (ptype, iv) in self._sched_plan(st):
                        jit = _f(st.get('aprs_jitter'), 20.0)
                        self.next_tx[ptype] = time.time() + iv + random.uniform(0, jit)
                        print('%s 定时触发 %s 发射' % (LOG, ptype), flush=True)
                        self.send(ptype, trigger='timer')
            except Exception as e:
                self.stats['last_error'] = 'scheduler: %s: %s' % (type(e).__name__, e)
                print('%s 调度异常: %s' % (LOG, e), flush=True)
            time.sleep(5)

    def _maintenance(self):
        while self.run_flag:
            try:
                days = int(_f(self.settings().get('aprs_retention_days'), 90))
                if days > 0:
                    cut = time.time() - days * 86400
                    self.store.exec('DELETE FROM aprs_packets WHERE ts_epoch < ?', (cut,))
                    self.store.exec('DELETE FROM aprs_tx WHERE ts_epoch < ?', (cut,))
            except Exception:
                pass
            time.sleep(3600)

    def _init_threads(self):
        self._dedup = {}          # 同内容去重表：key -> 上次入库 epoch
        for fn in (self._scheduler, self._maintenance):
            t = threading.Thread(target=fn, daemon=True)
            t.start()

    def stop(self):
        self.run_flag = False

    # ---------------- 查询 ----------------
    def list_packets(self, day=None, src=None, dtype=None, limit=200, offset=0,
                     pos_only=False, since_id=0):
        sql = 'SELECT * FROM aprs_packets WHERE 1=1'
        args = []
        if day:
            sql += ' AND ts LIKE ?'
            args.append(day + '%')
        if src:
            sql += ' AND src LIKE ?'
            args.append('%' + src + '%')
        if dtype:
            sql += ' AND dtype = ?'
            args.append(dtype)
        if pos_only:
            sql += ' AND lat IS NOT NULL AND lon IS NOT NULL'
        if since_id:
            sql += ' AND id > ?'
            args.append(int(since_id))
        sql += ' ORDER BY ts_epoch DESC, id DESC LIMIT ? OFFSET ?'
        args.extend([int(limit), int(offset)])
        rows = self.store.query(sql, args)
        for r in rows:
            if r.get('raw_hex') and not r.get('info_hex'):
                r['info_hex'] = ''
            for k in ('lat', 'lon'):
                if r.get(k) is not None:
                    r[k] = float(r[k])
        return rows

    def packets_geo(self, day=None, minutes=180, limit=500):
        """地图用：最近时间窗内所有带位置的点（含轨迹）。"""
        cut = time.time() - max(5, int(minutes)) * 60
        sql = ('SELECT id,ts,ts_epoch,src,lat,lon,symbol_table,symbol_code,dtype,'
               'comment,speed_kt,alt_m,path,wx_json FROM aprs_packets '
               'WHERE lat IS NOT NULL AND lon IS NOT NULL AND ts_epoch >= ?')
        args = [cut]
        if day:
            sql += ' AND ts LIKE ?'
            args.append(day + '%')
        sql += ' ORDER BY ts_epoch ASC LIMIT ?'
        args.append(int(limit))
        rows = self.store.query(sql, args)
        tracks = {}
        for r in rows:
            tracks.setdefault(r['src'], []).append(
                [round(float(r['lat']), 6), round(float(r['lon']), 6), r['ts_epoch']])
        stations = {}
        for r in rows:
            s = r['src']
            if s not in stations or (r['ts_epoch'] or 0) > (stations[s].get('ts_epoch') or 0):
                stations[s] = r
        return {'stations': list(stations.values()),
                'tracks': tracks, 'count': len(rows)}

    def stats_payload(self):
        st = self.settings()
        today = datetime.now().strftime('%Y-%m-%d')
        by_type = self.store.query(
            'SELECT dtype, COUNT(*) n FROM aprs_packets WHERE ts LIKE ? '
            'GROUP BY dtype ORDER BY n DESC', (today + '%',))
        top = self.store.query(
            'SELECT src, COUNT(*) n, MAX(ts_epoch) last FROM aprs_packets '
            'WHERE ts LIKE ? GROUP BY src ORDER BY n DESC LIMIT 20', (today + '%',))
        tx_today = self.store.one(
            'SELECT COUNT(*) n, SUM(ok) ok FROM aprs_tx WHERE ts LIKE ?',
            (today + '%',)) or {}
        pos = self.position.get()
        tnc = self.tnc.status() if self.tnc else {}
        return {
            'ok': True,
            'enabled': self.enabled(),
            'stats': dict(self.stats),
            'tnc': tnc,
            'by_type': by_type,
            'top_stations': top,
            'tx_today': {'total': int(tx_today.get('n') or 0),
                         'ok': int(tx_today.get('ok') or 0)},
            'position': pos,
            'position_stat': dict(self.position.stat),
            'settings': {k: st.get(k) for k in (
                'aprs_mycall', 'aprs_ssid', 'aprs_dest', 'aprs_path',
                'aprs_beacon_enabled', 'aprs_beacon_interval', 'aprs_weather_enabled',
                'aprs_weather_interval', 'aprs_telemetry_enabled',
                'aprs_telemetry_interval', 'aprs_status_enabled', 'aprs_status_interval',
                'aprs_carrier_sense', 'aprs_lat', 'aprs_lon', 'aprs_pos_source',
                'aprs_map_provider', 'aprs_map_layers', 'aprs_map_tk',
                'aprs_symbol_table', 'aprs_symbol_code')},
            'next_tx': {k: round(v, 1) for k, v in self.next_tx.items()},
            'weather_source': self._weather(),
        }

    def stations(self):
        with self.lock:
            return list(self._stations.values())

    # ---------------- 位置检索（语音助手的位置类工具）----------------
    def home_position(self):
        """本站自身位置（手填坐标或 NMEA GPS）。"""
        try:
            p = self.position.get() or {}
        except Exception:
            p = {}
        lat, lon = _f(p.get('lat'), None), _f(p.get('lon'), None)
        return {'lat': round(lat, 5) if lat is not None else None,
                'lon': round(lon, 5) if lon is not None else None,
                'alt_m': p.get('alt_m'), 'source': p.get('source') or '',
                'valid': bool(p.get('valid'))}

    def self_calls(self):
        """本站自己的呼号（含 SSID 变体）；列「附近有谁」时要排除掉自己。"""
        base = str(self.setting('aprs_mycall', '') or '').strip()
        if not base:
            return ()
        ssid = str(self.setting('aprs_ssid', '') or '').strip()
        out = [base]
        if ssid not in ('', '0'):
            out.append('%s-%s' % (base, ssid))
        return tuple(out)

    def _history_positions(self, hours=24, limit=2000):
        """库里最近的位置包。按呼号去重交给纯函数做，这里只管取。"""
        cut = time.time() - max(1.0, _f(hours, 24.0)) * 3600.0
        try:
            return self.store.query(
                'SELECT src AS call, lat, lon, symbol_code, comment, ts, ts_epoch,'
                ' speed_kt, alt_m FROM aprs_packets'
                ' WHERE lat IS NOT NULL AND lon IS NOT NULL AND ts_epoch >= ?'
                ' ORDER BY ts_epoch DESC LIMIT ?', (cut, int(limit))) or []
        except Exception:
            return []

    def _station_pool(self, hours=24, limit=2000):
        """内存最近表 ∪ 库历史。内存里的更新（刚收到就立刻可查），放前面。"""
        pool = []
        with self.lock:
            pool.extend(dict(v) for v in self._stations.values())
        pool.extend(self._history_positions(hours=hours, limit=limit))
        return pool

    def station_position(self, call=''):
        """按呼号取最后已知位置；call 留空 = 最近听到的那一个台。

        先查内存 _stations（新鲜、重启即失），再回落 aprs_packets 历史。
        找不到返回 None——工具层要把它转成一句人话，而不是空字典。

        本站自己的信标**一律**先排除：这个工具回答的是「**用户**在哪」，中继台
        自己的坐标另有 get_home_position 工具，不该在这里冒充用户位置。
        排除用 call_exact 而非 call_match——同一操作者的 BI7KHI-9 往往正是
        用户手上那台，按主呼号排会把它一起误伤。

        给了带 SSID 的呼号时优先精确命中，见 pick_latest_station。
        """
        want = str(call or '').strip()
        ex = self.self_calls()
        pool = [r for r in self._station_pool()
                if not any(call_exact(r.get('call') or r.get('src'), x) for x in ex)]
        rec = pick_latest_station(pool, want)
        if rec is None:
            return None
        return station_brief(rec, home=self.home_position())

    def nearby_stations(self, km=50.0, limit=5, hours=24):
        """以本站为中心列出半径内最近的若干台（含距离与中文八方位）。"""
        return nearest_stations(self._station_pool(hours=hours),
                                home=self.home_position(), km=km,
                                limit=limit, exclude=self.self_calls())


# ===========================================================================
# 九、自检
# ===========================================================================
def _selftest():
    """合成信号回环自检：构造报文 -> 调制 -> 解调 -> 逐字段比对。"""
    import sys
    print('--- CRC 标准向量 ---')
    print('   crc16_x25(b"123456789") = 0x%04X  (期望 0x906E)' % crc16_x25(b'123456789'))
    ok = crc16_x25(b'123456789') == 0x906E

    print('--- 各类型报文生成 ---')
    cases = [
        ('position', aprs_position(22.5333, 114.0500, '/', '-', 'Relay BI7KHI')),
        ('weather', aprs_weather({'wind_ms': 3.5, 'wind_dir': 220, 'gust_ms': 6.0,
                                  'temp_c': 28.4, 'humidity': 65,
                                  'pressure_hpa': 1013.2, 'rain_1h_mm': 0.5,
                                  'rain_today_mm': 2.0},
                                 22.5333, 114.0500, '/', '_', 'WX')),
        ('status', aprs_status('relay online')),
        ('telemetry', aprs_telemetry(5, [138, 120, 78, 200, 35], '10110000')),
        ('message', aprs_message('BI7KHI', 'test de BI7KHI-10', 1)),
    ]
    for nm, info in cases:
        print('   %-10s %s' % (nm, info.decode('utf-8', 'replace')))

    print('--- 回环：调制 -> 解调 ---')
    for sr in (16000, 48000, 8000):
        for nm, info in cases:
            frame = build_frame('BI7KHI', 'APRS', [('WIDE1-1', 1)], info,
                                src_ssid=10)
            w = build_afsk(frame, sr=sr)
            dec = Tnc(sr)
            got = []
            for i in range(0, len(w), 1024):
                got += dec.feed(w[i:i + 1024].astype(np.float64))
            got += dec.flush()
            hit = any(bytes(fr) == frame for (_t, fr) in got)
            print('   sr=%-6d %-10s frames=%-3d EXACT=%s' % (sr, nm, len(got), hit))
            ok = ok and hit
    print('--- 噪声鲁棒性 @16000（带 1.5s 语音前导）---')
    voice = np.random.randn(int(1.5 * 16000)).astype(np.float32) * 0.05
    for snr in (20, 10, 6, 3, 0):
        frame = build_frame('BI7KHI', 'APRS', [], cases[0][1], src_ssid=10)
        w = build_afsk(frame, sr=16000)
        p = float(np.mean(w ** 2))
        n = np.random.randn(len(w)).astype(np.float32) * math.sqrt(p / (10 ** (snr / 10.0)))
        mix = np.concatenate([voice, (w + n).astype(np.float32)])
        dec = Tnc(16000)
        got = []
        for i in range(0, len(mix), 1024):
            got += dec.feed(mix[i:i + 1024])
        hit = any(bytes(fr) == frame for (_t, fr) in got)
        print('   snr=%-3d frames=%-3d EXACT=%s  bursts=%d' % (
            snr, len(got), hit, dec.stats['bursts']))
        ok = ok and hit
    print('--- 解析回环 ---')
    frame = build_frame('BI7KHI', 'APRS', [], cases[1][1], src_ssid=10)
    rec = parse_packet(frame)
    print('   dtype=%s src=%s lat=%s lon=%s' % (
        rec['dtype'], rec['src'], rec.get('lat'), rec.get('lon')))
    print('   wx=%s' % json.dumps(rec.get('wx'), ensure_ascii=False))
    ok = ok and rec['dtype'] == 'weather' and rec['lat'] is not None
    print('=== 自检结果：%s ===' % ('全部通过' if ok else '存在失败'))
    return 0 if ok else 1


if __name__ == '__main__':
    import sys
    sys.exit(_selftest())

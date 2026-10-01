# -*- coding: utf-8 -*-
"""GNSS 位置读取：同一个串口同时认 NMEA 与 UBX，且不依赖 pyserial。

为什么自己写而不是用 pyserial+gpsd：
  * 板端不一定装了 pyserial（原来 `aprs_service._from_nmea()` 就依赖它，实测这台模块
    一个 NMEA 句都不发，那条预留给 ZED-F9P 的通道其实一直是死的）；
  * 用 os.open + termios + select 就够了，少一个依赖、少一层进程；
  * **关键在于协议**：u-blox ZED-F9P 在 USB 口上默认只吐 UBX 二进制，不吐 NMEA。
    所以解析器必须在**同一段字节流里**同时认两种帧，而不是靠一个"模式"开关。

解析口径有两条是踩过坑写下来的，别改：
  1. UBX 校验和必须覆盖 CLASS、ID、**长度两字节**、payload。漏长度字节会得到
     "帧头与长度全对、校验和全 False"的现场表现，而用同一个错函数造的合成帧还能自洽通过；
  2. 候选帧头重同步只能 `i += 2` 继续找，不能 break —— 混合流里巧合的 `B5 62` 很常见，
     break 会让"后面全是好帧"也一个都解不出来。
回归夹具用真机抓包（`board/deploy/test_gnss_service.py` 里内嵌了一帧真实 NAV-PVT）。
"""
import os
import re
import select
import struct
import time

# 定位类型（UBX NAV-PVT fixType）
FIXTYPE = {0: '无定位', 1: '死推算', 2: '二维定位', 3: '三维定位',
           4: 'GNSS+DR', 5: '仅时间'}
# NMEA GGA 定位质量
GGA_Q = {0: '无效', 1: '单点', 2: '差分', 3: 'PPS', 4: 'RTK 固定解',
         5: 'RTK 浮动解', 6: '估算中', 7: '手动', 8: '模拟'}
CARR = {0: '无载波解', 1: '浮点解', 2: '固定解'}


def ubx_checksum(payload, cls_, mid):
    """8 位 Fletcher：覆盖 CLASS + ID + 长度两字节 + payload（**长度也算**）。"""
    ck_a = ck_b = 0
    body = bytes([cls_, mid]) + struct.pack('<H', len(payload)) + payload
    for x in body:
        ck_a = (ck_a + x) & 0xFF
        ck_b = (ck_b + ck_a) & 0xFF
    return bytes([ck_a, ck_b])


def ubx_frames(buf):
    out, i = [], 0
    while True:
        i = buf.find(b'\xb5\x62', i)
        if i < 0 or i + 6 > len(buf):
            break
        cls_, mid = buf[i + 2], buf[i + 3]
        ln = struct.unpack('<H', buf[i + 4:i + 6])[0]
        end = i + 6 + ln
        if end + 2 > len(buf) or ln > 4096:
            i += 2                       # 重同步：别 break
            continue
        payload = buf[i + 6:end]
        if ubx_checksum(payload, cls_, mid) == buf[end:end + 2]:
            out.append((cls_, mid, payload))
            i = end + 2
        else:
            i += 2
    return out


def ubx_poll(cls_, mid):
    return b'\xb5\x62' + bytes([cls_, mid]) + struct.pack('<H', 0) + ubx_checksum(b'', cls_, mid)


def parse_nav_pvt(p):
    """UBX-NAV-PVT → 定位事实（含 RTK 载波解状态与精度估计）。"""
    if len(p) < 92:
        return None
    o = struct.unpack_from
    flags = p[21]
    carr = (flags >> 6) & 0x03
    hacc = o('<I', p, 40)[0] / 1000.0
    vacc = o('<I', p, 44)[0] / 1000.0
    return {
        'proto': 'ubx', 'ts': time.time(),
        'utc': '%04d-%02d-%02d %02d:%02d:%02d' % (o('<H', p, 4)[0], p[6], p[7],
                                                  p[8], p[9], p[10]),
        'fix_type': p[20], 'fix': FIXTYPE.get(p[20], '?'),
        'fix_ok': bool(flags & 0x01), 'diff': bool((flags >> 1) & 0x01),
        'carr': carr, 'carr_label': CARR.get(carr, '?'),
        'sats': p[23],
        'lat': round(o('<i', p, 28)[0] * 1e-7, 7),
        'lon': round(o('<i', p, 24)[0] * 1e-7, 7),
        'alt_m': round(o('<i', p, 36)[0] / 1000.0, 2),      # 海拔（椭球高见 height_mm）
        'height_m': round(o('<i', p, 32)[0] / 1000.0, 2),
        'hacc_m': round(hacc, 2), 'vacc_m': round(vacc, 2),
        'hdop': round(o('<H', p, 76)[0] * 0.01, 2),
    }


NMEA_RE = re.compile(rb'\$([A-Z]{2})([GNR][A-Z]{2}),([^\r\n*]*)(?:\*([0-9A-Fa-f]{2}))?')


def nmea_checksum_ok(sentence):
    """NMEA 的 XOR 校验；句子里没带 *CS 时返回 None（混合流里可能被截断）。"""
    if b'*' not in sentence:
        return None
    body, _, cs = sentence[1:].partition(b'*')
    if len(cs) < 2:
        return None
    try:
        want = int(cs[:2], 16)
    except Exception:
        return None
    got = 0
    for ch in body:
        got ^= ch
    return got == want


def _num(v, d=None):
    """NMEA 字段转数字：字段可能是空串或单位字母（如 'M'），转不了就返回默认值。

    单独抽出来是为了**不让一个坏字段把整句作废** —— 之前 f[9]='M' 抛异常
    被外层 except 吞掉，结果 GGA 整句被判成解析不出来。
    """
    try:
        return float(v)
    except Exception:
        return d


def _deg(v, hemi):
    try:
        x = float(v)
    except Exception:
        return None
    d = int(x / 100)
    dec = d + (x - d * 100) / 60.0
    return round(-dec if hemi in ('S', 'W') else dec, 7)


def parse_nmea(buf):
    """从缓冲里取最新一条有效 NMEA 定位（GGA 优先，RMC 兜底）。"""
    gga = rmc = None
    seen = set()
    for m in NMEA_RE.finditer(buf):
        raw = m.group(0)
        ok = nmea_checksum_ok(raw)
        if ok is False:
            continue                     # 校验和明确不对就丢
        kind = m.group(2).decode()
        f = m.group(3).decode('ascii', 'ignore').split(',')
        seen.add(kind)
        try:
            if kind == 'GGA' and len(f) >= 10:
                # 注意字段偏移：句子类型已经被正则拆走，所以 f[0] 就是 UTC 时间。
                # 海拔是 f[8]、单位在 f[9]（曾经按 f[9] 取海拔，float('M') 抛异常
                # 被外层 except 吞掉，整句作废 —— 这种"静默丢整句"最难查）。
                q = int(f[5] or 0)
                gga = {'proto': 'nmea', 'ts': time.time(), 'utc': f[0],
                       'lat': _deg(f[1], f[2]), 'lon': _deg(f[3], f[4]),
                       'fix_type': q, 'q': q, 'fix': GGA_Q.get(q, '?'),
                       'fix_ok': q > 0,
                       'sats': int(f[6] or 0), 'hdop': _num(f[7]),
                       'alt_m': _num(f[8]),
                       'hacc_m': None, 'vacc_m': None, 'carr': 0, 'carr_label': ''}
            elif kind == 'RMC' and len(f) >= 9 and f[1] == 'A':
                rmc = {'proto': 'nmea', 'ts': time.time(), 'utc': f[0],
                       'lat': _deg(f[2], f[3]), 'lon': _deg(f[4], f[5]),
                       'fix_type': 2, 'fix': 'RMC 有效', 'fix_ok': True,
                       'sats': None, 'hdop': None,
                       'alt_m': None, 'hacc_m': None, 'vacc_m': None,
                       'speed_kn': f[6] if len(f) > 6 else '',
                       'carr': 0, 'carr_label': ''}
        except Exception:
            continue
    fix = gga if (gga and gga.get('lat') is not None and gga.get('lon') is not None) else rmc
    if fix is None:
        return None, seen
    return fix, seen


def crc24q(data):
    crc = 0
    for b in data:
        crc ^= b << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= 0x1864CFB
    return crc & 0xFFFFFF


def rtcm_frames(buf):
    """合法 RTCM3 帧（按 CRC-24Q 认帧，别只数 0xD3 字节）。"""
    out, i = [], 0
    while True:
        i = buf.find(b'\xd3', i)
        if i < 0 or i + 3 > len(buf):
            break
        ln = ((buf[i + 1] & 0x03) << 8) | buf[i + 2]
        end = i + 3 + ln
        if ln > 1023 or end + 3 > len(buf):
            i += 1
            continue
        if crc24q(buf[i:end]) == int.from_bytes(buf[end:end + 3], 'big'):
            out.append((buf[i + 3] << 4) | (buf[i + 4] >> 4))
            i = end + 3
        else:
            i += 1
    return out


def fix_acceptable(fix, max_hacc_m=0):
    """这道门是为"信标别发垃圾坐标"设的。

    fixType=1（死推算）与 GGA quality=0 都不算定位；hAcc 超过上限也不要——
    APRS 信标发出去的坐标就是别人眼里的"你在哪"，宁可不发。
    """
    if not fix or fix.get('lat') is None or fix.get('lon') is None:
        return False, '没有坐标'
    if not fix.get('fix_ok'):
        return False, '未定位（%s）' % fix.get('fix', '?')
    if fix.get('fix_type', 0) < 2 and fix.get('proto') == 'ubx':
        return False, '定位类型不足（%s）' % fix.get('fix', '?')
    ha = fix.get('hacc_m')
    if max_hacc_m and ha is not None and ha > max_hacc_m:
        return False, '水平精度 %.1f m 超过门限 %s m' % (ha, max_hacc_m)
    return True, ''


class GnssReader:
    """一个串口的 GNSS 读取器：**保留打开的 fd 复用**，读不到就报告原因。

    不做后台线程：调用频率由上层决定（信标/气象/页面查询），
    每轮最多阻塞 max_seconds；fd 复用，避免每次重新打开丢数据。
    """

    def __init__(self):
        self.fd = None
        self.key = None
        self.buf = b''
        self.last = None
        self.stat = {'ok': False, 'err': '未读取', 'port': '', 'baud': 0,
                     'proto': '', 'ts': 0, 'reads': 0}

    # ---- 内部 ----
    def _close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except Exception:
                pass
        self.fd = None
        self.key = None

    def _open(self, port, baud):
        import termios
        fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            a = termios.tcgetattr(fd)
            cc = list(a[6])
            cc[termios.VMIN] = 0
            cc[termios.VTIME] = 0
            b = getattr(termios, 'B%d' % int(baud), termios.B38400)
            termios.tcsetattr(fd, termios.TCSANOW,
                              [0, 0, termios.CS8 | termios.CREAD | termios.CLOCAL, 0, b, b, cc])
        except Exception:
            pass                     # CDC-ACM 上可能不支持，读照样能读
        self.fd = fd
        self.key = '%s@%s' % (port, baud)

    def _read_some(self, seconds):
        """读满 seconds 或读到一段非空数据就返回（CDC-ACM 上 baud 无意义）。"""
        out = b''
        t0 = time.time()
        while time.time() - t0 < seconds:
            r, _, _ = select.select([self.fd], [], [], 0.3)
            if not r:
                continue
            try:
                chunk = os.read(self.fd, 8192)
            except BlockingIOError:
                continue
            except OSError:
                break
            if chunk:
                out += chunk
                if len(out) > 65536:
                    break
        return out

    # ---- 对外 ----
    def read(self, port, baud=38400, max_seconds=2.5, want='gnss'):
        """读一次并返回**可用**的定位（不合格返回上次的合格值或 None）。

        want: 'gnss'（NMEA/UBX 都认）| 'ubx' | 'nmea'
        """
        port = (port or '').strip()
        if not port:
            self.stat.update(ok=False, err='未配置串口', ts=time.time())
            return None
        key = '%s@%s' % (port, baud)
        if self.fd is None or self.key != key:
            self._close()
            try:
                self._open(port, int(baud))
            except Exception as e:
                self.stat.update(ok=False, err='打开 %s 失败：%s: %s'
                                 % (port, type(e).__name__, e), ts=time.time())
                return None
        try:
            chunk = self._read_some(max_seconds)
        except Exception as e:
            self.stat.update(ok=False, err='读取失败：%s: %s' % (type(e).__name__, e),
                             ts=time.time())
            self._close()
            return None
        self.buf = (self.buf + chunk)[-32768:]
        self.stat['reads'] = self.stat.get('reads', 0) + 1
        self.stat.update(port=port, baud=int(baud), got_bytes=len(chunk))

        cands = []
        frames = ubx_frames(self.buf)
        if want in ('gnss', 'ubx'):
            for c, m, p in frames:
                if (c, m) == (0x01, 0x07):
                    f = parse_nav_pvt(p)
                    if f:
                        cands.append(f)
        nmea_fix, nmea_seen = parse_nmea(self.buf)
        if want in ('gnss', 'nmea') and nmea_fix:
            cands.append(nmea_fix)
        # 协议判定要看**解析出来的东西**，不能看字节里有没有 `$`：
        # UBX 负载里天然会出现 0x24（`$`）与 0xD3，按字节数会把纯 UBX 的口报成"ubx+nmea"。
        protos = []
        if frames:
            protos.append('ubx')
        if nmea_seen:
            protos.append('nmea')
        if rtcm_frames(self.buf[-4096:]):
            protos.append('rtcm3')
        self.stat['proto'] = '+'.join(protos) or '?'
        self.stat['buffered'] = len(self.buf)
        self.stat['ubx_frames'] = len(frames)

        if not cands:
            self.stat.update(ok=False, err='读到 %d 字节但没有有效定位帧'
                             % len(chunk), ts=time.time())
            return None
        newest = max(cands, key=lambda x: x.get('ts', 0))
        self.last = newest
        self.stat.update(ok=True, err='', ts=newest.get('ts', time.time()),
                         fix=newest.get('fix'), fix_type=newest.get('fix_type'),
                         sats=newest.get('sats'), hacc_m=newest.get('hacc_m'),
                         carr=newest.get('carr_label', ''))
        # 主动问一次更强的定位信息（NAV-PVT 只在被动没看到时轮询，避免打扰流）
        return newest

    def status(self):
        s = dict(self.stat)
        if s.get('ts'):
            s['age_s'] = round(time.time() - s['ts'], 1)
        return s


# 模块级单例：串口 fd 复用，避免每次调用重开（重开会丢正在流的数据）
READER = GnssReader()

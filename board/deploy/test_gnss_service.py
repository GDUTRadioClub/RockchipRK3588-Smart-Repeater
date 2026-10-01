# -*- coding: utf-8 -*-
"""GNSS 位置读取自测（纯本地，夹具是真机帧与公开样例，不是自造帧）。

为什么这么较真夹具来源：这套解析口径最初把 **UBX 校验和算错了（漏掉长度两字节）**，
而当时的"合成帧"测试用同一个错函数生成校验和 —— 生成与校验一起错，自洽通过、真机全灭
（现场表现：帧头与长度全对、校验和全 False，一度判成"认不出协议"）。
所以：
  * UBX 用 `_diag/gnss_real_capture.bin` 里抠出来的**真实 NAV-PVT 帧**（内嵌 HEX）；
  * NMEA 用**公开文档里的样例句及其公布校验和**（独立参考，不是自己算的）；
  * 另加一条反证：按"漏长度字节"的写法必须与真机帧不符。

覆盖：两种协议解析、校验和口径、定位质量门、来源回落与缓存。
"""
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
import gnss_service as G          # noqa: E402
import aprs_service as A          # noqa: E402

FAIL, OK = [], [0]


def check(name, cond, extra=''):
    if cond:
        OK[0] += 1
        print('  OK   %s' % name)
    else:
        FAIL.append(name)
        print('  FAIL %s %s' % (name, extra))


# 真机抓包里的一帧 UBX-NAV-PVT（2026-09-30 04:46:10 UTC，广州一带，21 星，三维定位）
REAL_PVT_HEX = ('b56201075c00f85a7910ea07091e042e0a372700000022edbd230301ea15ea08'
                '9643254bbc0d543c00005f500000b4050000ec080000f4fffffffdffffffe9ffff'
                'ff0d000000b030c701d200000080a81201a90000005c414f2100000000000000001c47')
# 公开文档里的 NMEA 样例（含公布的 *47 校验和），不是本脚本算出来的
NMEA_GGA = (b'$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47\r\n')
NMEA_RMC = (b'$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A\r\n')

print('=== 1. UBX：真机帧必须解出（校验和含长度两字节）===')
frame = bytes.fromhex(REAL_PVT_HEX)
check('夹具长度 100 字节（6 头 + 92 负载 + 2 校验）', len(frame) == 100, len(frame))
frames = G.ubx_frames(frame)
check('真机帧能被认出（1 帧）', len(frames) == 1, len(frames))
pvt = G.parse_nav_pvt(frames[0][2]) if frames else None
print('  解析：%s' % pvt)
check('经纬度与真机一致（23.0443813 / 113.3906154）',
      pvt and abs(pvt['lat'] - 23.0443813) < 1e-6 and abs(pvt['lon'] - 113.3906154) < 1e-6,
      pvt)
check('三維定位 / 21 星 / hAcc 1.46 m / 载波解=0',
      pvt and pvt['fix_type'] == 3 and pvt['sats'] == 21
      and abs(pvt['hacc_m'] - 1.46) < 0.01 and pvt['carr'] == 0, pvt)
check('UTC 时间戳解析正确（2026-09-30 04:46:10）',
      pvt and pvt['utc'] == '2026-09-30 04:46:10', pvt and pvt['utc'])

print('\n=== 2. 反证：漏长度字节的算法必须与真机帧不符（当初就错在这）===')
cls_, mid = frame[2], frame[3]
payload = frame[6:98]
ck_a = ck_b = 0
for x in bytes([cls_, mid]) + payload:          # 漏掉长度两字节的写法
    ck_a = (ck_a + x) & 0xFF
    ck_b = (ck_b + ck_a) & 0xFF
check('正确写法 == 帧尾校验和', G.ubx_checksum(payload, cls_, mid) == frame[98:100])
check('错误写法 != 帧尾校验和', bytes([ck_a, ck_b]) != frame[98:100],
      bytes([ck_a, ck_b]).hex())

print('\n=== 3. 混合流重同步（巧合帧头不能挡住后面的好帧）===')
mixed = b'\xb5\x62\xff\xff\x00\x20' + b'\x00' * 10 + frame + frame
check('前面塞一个假帧头，仍能解出 2 帧', len(G.ubx_frames(mixed)) == 2,
      len(G.ubx_frames(mixed)))
check('截断尾部后仍能解出前面的完整帧', len(G.ubx_frames(frame + frame[:-5])) == 1)

print('\n=== 4. NMEA：公开样例 + 校验和判定 ===')
fix, seen = G.parse_nmea(NMEA_GGA)
print('  GGA：%s' % fix)
check('GGA 解析出 48.1173 / 11.5167', fix and abs(fix['lat'] - 48.1173) < 1e-4
      and abs(fix['lon'] - 11.5167) < 1e-4, fix)
check('GGA 定位质量=1（单点）/ 8 星 / 海拔 545.4',
      fix and fix['q'] == 1 and fix['sats'] == 8 and abs(fix['alt_m'] - 545.4) < 0.01, fix)
check('公开样例的校验和判定为真', G.nmea_checksum_ok(NMEA_GGA.strip()) is True)
bad = NMEA_GGA.replace(b'*47', b'*48')
check('校验和改错后该句被丢弃', G.parse_nmea(bad)[0] is None)
check('没有 *CS 的截断句仍可用（混合流里常被截断）',
      G.parse_nmea(b'$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,')[0]
      is not None)
rmc, _ = G.parse_nmea(NMEA_RMC)
check('RMC 兜底也能出坐标', rmc and abs(rmc['lat'] - 48.1173) < 1e-4, rmc)

print('\n=== 5. 定位质量门（别把差定位发出去）===')
good = {'lat': 23.0, 'lon': 113.0, 'fix_ok': True, 'fix_type': 3, 'proto': 'ubx',
        'hacc_m': 1.5, 'fix': '三维定位'}
check('好定位通过', G.fix_acceptable(good) == (True, ''))
check('hAcc 超门限被拦', G.fix_acceptable(dict(good, hacc_m=250), 100)[0] is False)
check('门限=0 表示不限', G.fix_acceptable(dict(good, hacc_m=999), 0)[0] is True)
check('死推算（fixType=1）被拦',
      G.fix_acceptable(dict(good, fix_type=1, fix='死推算'))[0] is False)
check('未定位（fix_ok=False）被拦', G.fix_acceptable(dict(good, fix_ok=False))[0] is False)
check('没坐标被拦', G.fix_acceptable({'lat': None, 'lon': None})[0] is False)

print('\n=== 6. 位置来源：GNSS 优先、取不到回落固定坐标、缓存限频 ===')


class FakeSvc:
    def __init__(self, d):
        self.d = dict(d)

    def setting(self, k, d=''):
        return self.d.get(k, d)


class FakeReader:
    """假读取器：可控"有没有定位"和调用次数。"""

    def __init__(self, fix):
        self.fix = fix
        self.calls = 0

    def read(self, port, baud=38400, max_seconds=2.5, want='gnss'):
        self.calls += 1
        return self.fix

    def status(self):
        return {'ok': bool(self.fix), 'err': '' if self.fix else '假读取器：没有有效定位',
                'proto': 'ubx', 'ts': time.time()}


real_reader = G.READER
try:
    svc = FakeSvc({'aprs_pos_source': 'gnss', 'aprs_gps_port': '/dev/ttyACM0',
                   'aprs_gps_baud': '38400', 'aprs_lat': '22.5', 'aprs_lon': '114.0',
                   'aprs_gnss_max_hacc_m': '100'})
    prov = A.PositionProvider(svc)
    G.READER = FakeReader(dict(good, ts=time.time()))
    p1 = prov.get()
    check('GNSS 有定位时用它', p1['source'] == 'gnss' and p1['lat'] == 23.0, p1)
    p2 = prov.get()
    check('5 秒内第二次调用走缓存（不重复读串口）', G.READER.calls == 1, G.READER.calls)
    p3 = prov.get(force=True)
    check('force=True 会真读一次', G.READER.calls == 2 and p3['source'] == 'gnss',
          G.READER.calls)

    G.READER = FakeReader(None)
    prov2 = A.PositionProvider(svc)
    p4 = prov2.get()
    check('GNSS 取不到 → 回落固定坐标', p4['source'] == 'manual' and p4['lat'] == 22.5, p4)
    check('回落原因留在 stat 里给页面看', '没有有效定位' in (prov2.status().get('err') or ''),
          prov2.status())

    svc3 = FakeSvc({'aprs_pos_source': 'ubx', 'aprs_gps_port': '',
                    'aprs_lat': '1.0', 'aprs_lon': '2.0'})
    prov3 = A.PositionProvider(svc3)
    G.READER = FakeReader(dict(good))
    p5 = prov3.get()
    check('没配串口 → 回落并报"未配置串口"',
          p5['source'] == 'manual' and '未配置串口' in (prov3.status().get('err') or ''),
          prov3.status())

    svc4 = FakeSvc({'aprs_pos_source': 'gnss', 'aprs_gps_port': '/dev/ttyACM0',
                    'aprs_gnss_max_hacc_m': '1', 'aprs_lat': '9.0', 'aprs_lon': '9.0'})
    prov4 = A.PositionProvider(svc4)
    G.READER = FakeReader(dict(good, hacc_m=50.0))
    p6 = prov4.get()
    check('定位精度超门限 → 不发，回落固定坐标',
          p6['source'] == 'manual' and '门限' in (prov4.status().get('err') or ''),
          prov4.status())

    # cached_only：保存设置/页面轮询这条路**绝不能**去读串口
    svc5 = FakeSvc({'aprs_pos_source': 'gnss', 'aprs_gps_port': '/dev/ttyACM0',
                    'aprs_lat': '7.0', 'aprs_lon': '8.0'})
    prov5 = A.PositionProvider(svc5)
    fake5 = FakeReader(dict(good))
    G.READER = fake5
    p7 = prov5.get(cached_only=True)
    check('cached_only=True 不读串口（调用次数保持 0）', fake5.calls == 0, fake5.calls)
    check('cached_only 时回落固定坐标并说明"尚未读取"',
          p7['source'] == 'manual' and '尚未读取' in (p7.get('gnss_error') or ''), p7)
    p8 = prov5.get(force=True)
    check('随后 force=True 才真读（1 次）且拿到 GNSS 位置',
          fake5.calls == 1 and p8['source'] == 'gnss', (fake5.calls, p8.get('source')))
    p9 = prov5.get(cached_only=True)
    check('已有缓存时 cached_only 直接给缓存（仍不读串口）',
          fake5.calls == 1 and p9['source'] == 'gnss', (fake5.calls, p9.get('source')))
finally:
    G.READER = real_reader

print('\n%d 通过 / %d 失败' % (OK[0], len(FAIL)))
for x in FAIL:
    print('  FAIL %s' % x)
sys.exit(1 if FAIL else 0)

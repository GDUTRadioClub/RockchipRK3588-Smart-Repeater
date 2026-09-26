# -*- coding: utf-8 -*-
"""APRS 位置能力自测（无需板子、无需硬件、无需网络）。

覆盖两块新能力：
  1. 语音助手的位置类工具底层：距离/方位/呼号反查/附近电台
  2. 语音日志的「段内含 APRS 位置」标记（尾音里的对方位置信标）
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import aprs_service as AP
import voice_service as V

FAIL, OK = [], [0]


def check(name, cond, extra=''):
    if cond:
        OK[0] += 1
        print('  OK   %s' % name)
    else:
        FAIL.append(name)
        print('  FAIL %s %s' % (name, extra))


# ---------------------------------------------------------------------------
print('\n=== 1. 大圆距离 / 方位 ===')
check('同一点距离为 0', AP.haversine_km(22.5, 114.0, 22.5, 114.0) < 1e-6)
# 参考值：R=6371.0088 时 1° 纬度 = 111.195 km；赤道上 1° 经度同值
check('1° 纬度 ≈ 111.195 km', abs(AP.haversine_km(0, 0, 1, 0) - 111.195) < 0.05,
      '%.3f' % AP.haversine_km(0, 0, 1, 0))
check('赤道 1° 经度 ≈ 111.195 km', abs(AP.haversine_km(0, 0, 0, 1) - 111.195) < 0.05,
      '%.3f' % AP.haversine_km(0, 0, 0, 1))
check('对极点 = 半周长 ≈ 20015 km',
      abs(AP.haversine_km(0, 0, 0, 180) - 20015.1) < 2.0,
      '%.1f' % AP.haversine_km(0, 0, 0, 180))
check('高纬 1° 经度更短（cos 效应）',
      AP.haversine_km(60, 0, 60, 1) < AP.haversine_km(0, 0, 0, 1) * 0.51)
check('垃圾输入不崩', AP.haversine_km('x', None, 1, 2) is None)

check('正北 0°', abs(AP.bearing_deg(0, 0, 1, 0) - 0) < 0.5)
check('正东 90°', abs(AP.bearing_deg(0, 0, 0, 1) - 90) < 0.5)
check('正南 180°', abs(AP.bearing_deg(0, 0, -1, 0) - 180) < 0.5)
check('正西 270°', abs(AP.bearing_deg(0, 0, 0, -1) - 270) < 0.5)
check('八方位：北', AP.compass_cn(0) == '北' and AP.compass_cn(359) == '北')
check('八方位：东北', AP.compass_cn(45) == '东北')
check('八方位：东', AP.compass_cn(90) == '东')
check('八方位：西南', AP.compass_cn(225) == '西南')
check('八方位：西', AP.compass_cn(270) == '西')
check('八方位：垃圾输入返回空', AP.compass_cn('x') == '')

# ---------------------------------------------------------------------------
print('\n=== 2. 呼号匹配与挑最新 ===')
check('主呼号匹配带 SSID 的台', AP.call_match('BI7KHI-9', 'BI7KHI'))
check('大小写无关', AP.call_match('bi7khi-9', 'BI7KHI-9'))
check('不同呼号不匹配', not AP.call_match('BI7KHI-9', 'BA1ABC'))
check('空 want = 任意', AP.call_match('BI7KHI-9', ''))
check('空 stored 不匹配具体呼号', not AP.call_match('', 'BI7KHI'))
check('call_base 去 SSID', AP.call_base('bi7khi-9') == 'BI7KHI')

_items = [{'call': 'A', 'ts_epoch': 100}, {'call': 'A', 'ts_epoch': 300},
          {'call': 'B', 'ts_epoch': 200}]
check('挑出该呼号最新的一条',
      AP.pick_latest_station(_items, 'A')['ts_epoch'] == 300)
check('空呼号挑全局最新',
      AP.pick_latest_station(_items, '')['ts_epoch'] == 300)
check('无匹配返回 None', AP.pick_latest_station(_items, 'ZZ') is None)
check('垃圾条目被跳过', AP.pick_latest_station([None, 'x', {'call': 'C'}], 'C'))

# ---------------------------------------------------------------------------
print('\n=== 3. 紧凑结果 / 附近电台 ===')
HOME = {'lat': 22.5333, 'lon': 114.05}
near = {'call': 'BI7KHI-9', 'lat': 22.60, 'lon': 114.10, 'ts_epoch': 1000,
        'comment': 'test', 'speed_kt': 12.3}
far = {'call': 'BG2XYZ', 'lat': 39.9, 'lon': 116.4, 'ts_epoch': 1000}
b = AP.station_brief(near, home=HOME, now=1300)
check('brief 带呼号', b['call'] == 'BI7KHI-9')
check('brief 带距离', b.get('km') is not None and 6 < b['km'] < 12, str(b.get('km')))
check('brief 带中文方位（东北方向）', b.get('dir') == '东北', str(b.get('dir')))
check('brief 带「多久前」', b.get('age_min') == 5, str(b.get('age_min')))
check('brief 坐标已四舍五入', b['lat'] == 22.6)
check('无 home 时不给距离', 'km' not in AP.station_brief(near, now=1300))
check('排除名单精确匹配：同名操作者其它 SSID 不受牵连',
      AP.station_brief(near, home=HOME, exclude=('BI7KHI-10',)) is not None)
check('排除名单命中时返回 None',
      AP.station_brief(near, home=HOME, exclude=('bi7khi-9',)) is None)
check('无呼号的条目被丢弃', AP.station_brief({'lat': 1, 'lon': 2}) is None)

ns = AP.nearest_stations([near, far, {'call': 'BI7KHI-9', 'lat': 22.7,
                                      'lon': 114.2, 'ts_epoch': 1100}],
                         home=HOME, km=50, limit=5)
check('附近只留半径内的', [x['call'] for x in ns] == ['BI7KHI-9'])
check('同呼号取最新一条（距离按新坐标算）', ns[0]['km'] > b['km'])
check('limit 生效',
      len(AP.nearest_stations([near, far], home=HOME, km=5000, limit=1)) == 1)
check('按距离升序',
      [x['km'] for x in AP.nearest_stations([far, near], home=HOME, km=9999)]
      == sorted([x['km'] for x in AP.nearest_stations([far, near], home=HOME,
                                                      km=9999)]))

# ---------------------------------------------------------------------------
print('\n=== 4. AprsService 位置反查（临时库）===')
tmp = tempfile.mkdtemp()
db = os.path.join(tmp, 'relay.db')
ST = {'aprs_mycall': 'BI7KHI', 'aprs_ssid': '10',
      'aprs_pos_source': 'manual', 'aprs_lat': '22.533300', 'aprs_lon': '114.050000'}
svc = AP.AprsService(db)
svc.run_flag = False                      # 立刻停掉调度/维护线程，别真去发射
svc.configure(setting_getter=lambda k, d='': ST.get(k, d))
try:
    now = time.time()

    def add(src, lat, lon, ago, comment=''):
        svc.store.exec(
            'INSERT INTO aprs_packets(ts,ts_epoch,src,src_call,lat,lon,comment,source)'
            ' VALUES(?,?,?,?,?,?,?,?)',
            (time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now - ago)),
             now - ago, src, src.split('-')[0], lat, lon, comment, 'rx'))

    add('BI7KHI-10', 22.5333, 114.05, 10)          # 本站自己的信标，最新
    add('BI7KHI-9', 22.60, 114.10, 300, 'on the road')
    add('BA1ABC', 22.50, 114.00, 3600)
    add('BG2XYZ', 39.90, 116.40, 3600)             # 北京，50km 外

    check('self_calls 含 SSID 变体', set(svc.self_calls()) == {'BI7KHI', 'BI7KHI-10'})
    hp = svc.home_position()
    check('home_position 读手填坐标',
          abs(hp['lat'] - 22.5333) < 1e-6 and hp['source'] == 'manual', str(hp))

    p = svc.station_position('')
    check('空呼号 = 最近听到的台，且**跳过本站自己**',
          p and p['call'] == 'BI7KHI-9', str(p))
    check('结果含距离与方位', p and p.get('km') and p.get('dir'), str(p))
    check('结果含注释', p and p.get('comment') == 'on the road', str(p))

    p2 = svc.station_position('bi7khi-9')
    check('小写呼号也能查到', p2 and p2['call'] == 'BI7KHI-9')
    p3 = svc.station_position('bi7khi')
    check('只报主呼号也能查到', p3 and p3['call'] == 'BI7KHI-9')
    p_self = svc.station_position('BI7KHI-10')
    check('本站自己的信标不会当成用户位置返回',
          p_self is None or p_self['call'] != 'BI7KHI-10', str(p_self))
    check('问本站自己的号时退回到同一操作者的另一台（有答案好过说不知道）',
          p_self and p_self['call'] == 'BI7KHI-9', str(p_self))

    nb = svc.nearby_stations(km=50, limit=5)
    check('附近电台排除本站与超距台',
          sorted(x['call'] for x in nb) == ['BA1ABC', 'BI7KHI-9'],
          str([x['call'] for x in nb]))
    check('附近电台按距离升序',
          all(nb[i]['km'] <= nb[i + 1]['km'] for i in range(len(nb) - 1)), str(nb))
    nb2 = svc.nearby_stations(km=5000, limit=5)
    check('放大半径能把远台收进来', len(nb2) == 3, str(len(nb2)))

    # 内存最近表优先：刚收到还没落库（或库被清）也要能查到
    with svc.lock:
        svc._stations['BD7NEW'] = {'call': 'BD7NEW', 'lat': 22.55, 'lon': 114.06,
                                   'ts_epoch': now, 'comment': ''}
    pm = svc.station_position('BD7NEW')
    check('内存最近表也能查到', pm and pm['call'] == 'BD7NEW', str(pm))
    pm2 = svc.station_position('')
    check('内存里的台时间更新，会被当成「最近听到的那个」',
          pm2 and pm2['call'] == 'BD7NEW', str(pm2))

    check('查不到的呼号返回 None', svc.station_position('N0CALL') is None)
finally:
    svc.stop()

# ---------------------------------------------------------------------------
print('\n=== 5. 语音日志：段内 APRS 位置标记 ===')
vdb = os.path.join(tmp, 'voice.db')
vstore = V.Store(vdb)
astore = AP.Store(vdb)                    # 生产里两个 store 指向同一个 relay.db


class _Stub(V.VoiceService):
    """绕开 __init__（本地没有 silero VAD 模型），只给两个依赖：store 与 settings()。

    继承而不是鸭子类型：被测方法内部会互相调用（_fill_aprs_pos →
    _aprs_position_hit），纯鸭子对象会 AttributeError。
    """

    def __init__(self, store, st):
        self.store = store
        self._st = st

    def settings(self):
        return self._st


vst = {'aprs_mycall': 'BI7KHI', 'aprs_ssid': '10'}
stub = _Stub(vstore, vst)


def add_pkt(src, lat, lon, epoch):
    astore.exec('INSERT INTO aprs_packets(ts,ts_epoch,src,src_call,lat,lon,source)'
                ' VALUES(?,?,?,?,?,?,?)',
                ('t', epoch, src, src.split('-')[0], lat, lon, 'rx'))


T = 1700000000.0
add_pkt('BI7KHI-9', 22.60, 114.10, T)                 # 位置包
astore.exec('INSERT INTO aprs_packets(ts,ts_epoch,src,lat,lon,source)'
            ' VALUES(?,?,?,?,?,?)', ('t', T, 'BA1ABC', None, None, 'rx'))  # 无位置
add_pkt('BI7KHI-10', 22.53, 114.05, T + 1)            # 本站自己，且**更新**

fill = V.VoiceService._fill_aprs_pos
hit = fill(stub, T - 3.0, 5.0, 'rx')
check('段窗口覆盖到位置包 → 标记上', hit[0] == 1, str(hit))
check('标记带呼号', hit[1] == 'BI7KHI-9', str(hit))
check('标记带经纬度', abs(hit[2] - 22.6) < 1e-6 and abs(hit[3] - 114.1) < 1e-6, str(hit))
check('尾音在段末（包在 8.7s / 段长 11.2s）也能命中',
      fill(stub, T - 8.7, 11.25, 'rx')[0] == 1)
check('段窗口之外不标记', fill(stub, T + 100.0, 5.0, 'rx')[0] == 0)
check('本机发射段不标记（那是我们自己的信标）',
      fill(stub, T - 3.0, 5.0, 'tx')[0] == 0)
check('只有带经纬度的包才算位置', fill(stub, T - 3.0, 5.0, 'rx')[1] == 'BI7KHI-9')
check('本站信标更新时也不会顶掉用户的台（SQL 里就排掉，不是取回来再判）',
      fill(stub, T - 3.0, 5.0, 'rx')[1] == 'BI7KHI-9')
check('空库不崩', fill(_Stub(AP.Store(os.path.join(tmp, 'empty.db')), {}),
                      T, 5.0, 'rx')[0] == 0)

# list_logs 的 pos 过滤 + 返回字段
def add_vlog(ts_epoch, pos, call):
    vstore.exec(
        'INSERT INTO voice_logs(ts,ts_epoch,kind,category,seconds,asr_status,'
        'aprs_pos,aprs_call,aprs_lat,aprs_lon) VALUES(?,?,?,?,?,?,?,?,?,?)',
        ('2023-11-15 06:13:20', ts_epoch, 'rx', 'voice', 5.0, 'done',
         pos, call, 22.6 if pos else None, 114.1 if pos else None))


add_vlog(T + 10, 1, 'BI7KHI-9')
add_vlog(T + 20, 0, None)
rows_all = vstore.query('SELECT id FROM voice_logs')
check('voice_logs 能写入新列', len(rows_all) == 2)

logs = stub.list_logs
only = logs(pos='only')
check('pos=only 只出带位置的段', len(only) == 1 and only[0]['aprs_call'] == 'BI7KHI-9',
      str(len(only)))
check('列表带 aprs_pos 标志', only[0]['aprs_pos'] is True)
check('列表带坐标', only[0]['aprs_lat'] == 22.6 and only[0]['aprs_lon'] == 114.1)
none = logs(pos='none')
check('pos=none 只出不带位置的段', len(none) == 1 and none[0]['aprs_pos'] is False,
      str(len(none)))
check('不传 pos 时不过滤', len(logs()) == 2)
check('按呼号也能搜到', len(logs(q='BI7KHI')) == 1, str(len(logs(q='BI7KHI'))))

print('\n' + '=' * 62)
print('通过 %d 项，失败 %d 项' % (OK[0], len(FAIL)))
for f in FAIL:
    print('  ✗ %s' % f)
sys.exit(1 if FAIL else 0)

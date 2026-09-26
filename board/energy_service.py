# -*- coding: utf-8 -*-
"""能量统计（电池 / 光伏电压时间序列）的纯计算层。

只做三件事，**不依赖 Flask、不碰硬件**，便于离线自测：
  * points_from_rows —— 原始采样点按 N 分钟分桶成曲线点
  * day_stats        —— 当日最大/最小/平均及其发生时刻（含压降）
  * rows_to_csv      —— 整日序列导出

为什么要先给电压落库：`voltage_payload()` 是按需读 ADC、算完即弃，relay.db
里原先只有气象/温湿度/雨量，**没有任何电压历史**（见项目记忆里那条待办）。
所以「全日时间轴」的前提是先有采样器，历史补不回来。
"""
from datetime import datetime

UNIT = 'V'


def clamp_interval(minutes):
    """时间轴分桶间隔（分钟）钳到 1..120。"""
    try:
        v = int(float(minutes))
    except (TypeError, ValueError):
        v = 5
    return max(1, min(120, v))


def clamp_sample_sec(sec):
    """采样间隔（秒）钳到 10..600。"""
    try:
        v = int(float(sec))
    except (TypeError, ValueError):
        v = 60
    return max(10, min(600, v))


def clamp_retention_days(days):
    """保留天数钳到 1..3650。"""
    try:
        v = int(float(days))
    except (TypeError, ValueError):
        v = 365
    return max(1, min(3650, v))


def _f(v):
    try:
        return None if v is None or v == '' else float(v)
    except (TypeError, ValueError):
        return None


def _avg(nums):
    return round(sum(nums) / len(nums), 3) if nums else None


def points_from_rows(rows, interval_minutes=5):
    """原始采样 → 按 interval_minutes 分桶的曲线点（按时间升序）。

    **空桶不补值**：电压 0 是合法读数（电池断开 / 被负载拉死），补 0 会画出一条
    掉到零的假线。缺桶就让它缺，图上的空档老实表达「这段时间没采到」。
    """
    step = clamp_interval(interval_minutes) * 60
    buckets = {}
    for r in rows or ():
        e = _f(r.get('ts_epoch'))
        if not e:
            continue
        b = int(e) // step * step
        buckets.setdefault(b, []).append(r)
    out = []
    for b in sorted(buckets):
        grp = buckets[b]
        bs = [v for v in (_f(x.get('battery')) for x in grp) if v is not None]
        ps = [v for v in (_f(x.get('pv')) for x in grp) if v is not None]
        try:
            t = datetime.fromtimestamp(b)
            minute, hm = t.strftime('%Y-%m-%d %H:%M'), t.strftime('%H:%M')
        except (ValueError, OSError, OverflowError):
            minute, hm = '', ''
        out.append({
            'epoch': b, 'minute': minute, 'time': hm,
            'battery': _avg(bs), 'pv': _avg(ps),
            'battery_min': round(min(bs), 3) if bs else None,
            'battery_max': round(max(bs), 3) if bs else None,
            'pv_min': round(min(ps), 3) if ps else None,
            'pv_max': round(max(ps), 3) if ps else None,
            'n': len(grp),
        })
    return out


def _channel_stats(vals):
    """vals: [(ts, value)] → {min,max,avg,min_ts,max_ts}（无值全 None）。"""
    nums = [v for _, v in vals]
    if not nums:
        return {'min': None, 'max': None, 'avg': None, 'min_ts': '', 'max_ts': '',
                'n': 0}
    lo, hi = min(nums), max(nums)
    return {
        'min': round(lo, 3), 'max': round(hi, 3), 'avg': _avg(nums),
        'min_ts': next(t for t, v in vals if v == lo),
        'max_ts': next(t for t, v in vals if v == hi),
        'n': len(nums),
    }


def day_stats(rows):
    """当日统计。min/max 取**原始采样**而不是分桶均值——均值会把尖峰抹平，
    而电压的尖峰（发射瞬间的压降）恰恰是最该看到的。"""
    rows = list(rows or ())
    bat = [(r.get('ts') or '', _f(r.get('battery'))) for r in rows]
    pv = [(r.get('ts') or '', _f(r.get('pv'))) for r in rows]
    b = _channel_stats([(t, v) for t, v in bat if v is not None])
    p = _channel_stats([(t, v) for t, v in pv if v is not None])
    drop = None
    if b['max'] is not None and b['min'] is not None:
        drop = round(b['max'] - b['min'], 3)
    return {'points': len(rows), 'battery': b, 'pv': p, 'battery_drop': drop,
            'first_ts': rows[0].get('ts', '') if rows else '',
            'last_ts': rows[-1].get('ts', '') if rows else ''}


CSV_HEADER = '时间,时间戳,电池电压V,光伏电压V,电池ADC,光伏ADC\n'


def rows_to_csv(rows):
    """整日序列导出。含 BOM 由调用方加——Excel 打开中文表头才不会乱码。"""
    out = [CSV_HEADER]
    for r in rows or ():
        b = _f(r.get('battery'))
        p = _f(r.get('pv'))
        out.append('%s,%s,%s,%s,%s,%s\n' % (
            r.get('ts') or '', r.get('ts_epoch') or '',
            '' if b is None else ('%.4f' % b),
            '' if p is None else ('%.4f' % p),
            '' if r.get('battery_raw') is None else r.get('battery_raw'),
            '' if r.get('pv_raw') is None else r.get('pv_raw')))
    return ''.join(out)

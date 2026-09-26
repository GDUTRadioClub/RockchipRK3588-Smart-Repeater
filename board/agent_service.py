# -*- coding: utf-8 -*-
"""端侧 LLM 的类 Agent 能力：技能/工具注册、提示词构建、工具调用解析、生成速率统计。

设计说明
--------
板端 RKLLM（Qwen2.5-1.5B）并不稳定支持原生 function calling，因此这里采用
「提示词约定 + 文本协议」的方案：把工具清单写进 system prompt，模型需要实时数据时输出

    <tool_call>{"name": "get_weather", "arguments": {}}</tool_call>

app.py 解析后执行对应读取函数，把结果回灌给模型，再让模型用自然语言总结。
本模块不依赖 Flask：所有数据读取由 app.py 通过 ctx 注入（工具名 -> 可调用对象）。
"""
import json
import math
import re
import time

# ---------------------------------------------------------------------------
# 工具（技能）清单
# ---------------------------------------------------------------------------
TOOL_SPECS = [
    {
        'name': 'get_weather',
        'title': '风速风向',
        'desc': '读取当前风速、风向与气象采集服务状态（含最后一次 Modbus 原始报文、错误计数）。注意：风速风向属于本工具，与降雨量无关。',
        'params': {},
        'action': False,
    },
    {
        'name': 'get_rain',
        'title': '降雨量',
        'desc': '读取翻斗式雨量计数据：今日累计降水量、最近一小时降水量。',
        'params': {
            'hours': {'required': False, 'desc': '统计最近多少小时，默认 1，最大 24'},
        },
        'action': False,
    },
    {
        'name': 'get_power',
        'title': '电压电池',
        'desc': '读取电池电压、光伏电压（含 SARADC 原始值、引脚电压、倍率、满量程）。',
        'params': {},
        'action': False,
    },
    {
        'name': 'get_system',
        'title': '温度内存负载',
        'desc': '读取 SoC/CPU 温度、CPU 占用、负载、内存、磁盘与开机时长。',
        'params': {},
        'action': False,
    },
    {
        'name': 'get_radio',
        'title': '发射PTT状态',
        'desc': '读取 PTT 电平、是否正在发射、音频输出设备、手动发射与最近一次发射自检状态。',
        'params': {},
        'action': False,
    },
    {
        'name': 'get_camera',
        'title': '摄像头',
        'desc': '读取摄像头设备、分辨率、录像服务运行状态与录制文件数量/占用空间。',
        'params': {},
        'action': False,
    },
    {
        'name': 'get_time',
        'title': '时间',
        'desc': '读取开发板当前日期、时间与星期（北京时间）。',
        'params': {},
        'action': False,
    },
    {
        'name': 'get_station_position',
        'title': '呼号经纬度距离方位',
        'desc': ('查某个电台呼号最后一次出现的位置：经纬度、距本站多少公里、'
                 '在哪个方位、几分钟前。用户报了呼号就用这个；'
                 '呼号留空则查最近一次听到的电台（用户说「我在哪」时用它）。'),
        'params': {
            'call': {'required': False,
                     'desc': '呼号，如 BI7KHI-9。留空=最近听到的那个台'},
        },
        'action': False,
    },
    {
        'name': 'get_nearby_stations',
        'title': '附近电台呼号距离方位',
        'desc': ('以本站为中心列出最近的几个 APRS 电台：呼号、距离公里、方位、'
                 '多久前收到。问「附近有谁」「谁在附近」时用它。'),
        'params': {
            'km': {'required': False, 'desc': '搜索半径公里，默认 50'},
            'limit': {'required': False, 'desc': '最多返回几个，默认 5，最大 20'},
        },
        'action': False,
    },
    {
        'name': 'get_home_position',
        'title': '本站经纬度海拔',
        'desc': '读本站（中继台）自己的经纬度与海拔，以及位置来自手填还是 GPS。',
        'params': {},
        'action': False,
    },
    {
        'name': 'speak',
        'title': '语音播报',
        'desc': '让中继台把一段文字用本地 TTS 从 3.5mm AUX 播报出去（会占用 PTT 发射，谨慎使用）。',
        'params': {
            'text': {'required': True, 'desc': '要播报的中文内容，建议不超过 60 字'},
        },
        'action': True,
    },
]

TOOL_CALL_OPEN = '<tool_call>'
TOOL_CALL_CLOSE = '</tool_call>'


def tool_index():
    return {t['name']: t for t in TOOL_SPECS}


def enabled_tools(enabled=None):
    """enabled 为空/None 时表示全部启用。"""
    if not enabled:
        return list(TOOL_SPECS)
    idx = tool_index()
    out = []
    for name in enabled:
        t = idx.get(str(name).strip())
        if t and t not in out:
            out.append(t)
    return out


def tools_prompt(enabled=None):
    """工具清单 + READ 协议（**务必保持精简**）。

    实测两条硬约束：
      1. 板端 RKLLM（Qwen2.5-1.5B）提示词过长（>约 400 字）会直接空输出；
      2. **示例一多，模型会去照抄示例而不是按标题匹配工具**——曾给出 6 条
         「关键词→READ」示例，探针命中率反而从 6/7 掉到 5/9（问「在发射吗」
         输出 `发射→READ get_power {}`）。因此只保留一条示例。
    工具标题必须**字面点名它提供的数据字段**（如「温度内存负载」），
    模型只能靠字面匹配来选工具。
    """
    tools = enabled_tools(enabled)
    items = []
    for t in tools:
        params = t.get('params') or {}
        if params:
            items.append('%s(%s,%s)' % (t['name'], t['title'],
                                        ','.join(list(params.keys()))))
        else:
            items.append('%s(%s)' % (t['name'], t['title']))
    return '\n'.join([
        '你是 ELF2 中继台的端侧助手，能直接读取本机与电台的实时数据（不要反问设备型号）。',
        '需要实时数据时只输出一行读取指令，不要解释、不要直接回答：READ 名称 {}',
        '多项数据可输出多行。可用名称：' + ' '.join(items),
        '例：READ get_power {}',
    ])


def compose_user_prompt(base_prompt='', question='', enabled=None):
    """把（可选）用户自定义提示词、工具协议、实际问题合成一条 user 消息。"""
    parts = []
    if base_prompt and base_prompt.strip():
        parts.append('【系统设定】\n' + base_prompt.strip())
    parts.append(tools_prompt(enabled))
    parts.append('【用户问题】\n' + (question or '').strip())
    return '\n\n'.join(parts)


DATA_KEYWORDS = (
    '电压', '电池', '光伏', '电源', '电量', '温度', 'cpu', '负载', '内存', '磁盘', '存储',
    '风速', '风', '气象', '雨', '降水', '天气', '湿度',
    '时间', '日期', '几点', '星期',
    '状态', '摄像头', '录像', '发射', 'ptt', '中继', '设备', '运行', '电量', '电流', '功率',
    # 位置类：命中就走一轮工具，否则模型会凭想象编坐标
    '位置', '经纬度', '坐标', '在哪', '哪里', '哪儿', '附近', '距离', '方位',
    '多远', '呼号', 'aprs', '定位', 'gps', '导航', '引导',
)


def wants_realtime(text):
    """问句是否涉及实时数据（用于强制第一轮先调用工具）。"""
    t = (text or '').lower()
    return any(k in t for k in DATA_KEYWORDS)


# ---------------------------------------------------------------------------
# 总结轮（拿到工具数据后那一次调用）——行为约束必须在这里重新出现
# ---------------------------------------------------------------------------
# 为什么要有这一节：**真正被朗读/展示的文本是总结轮产出的**。第一轮在
# wants_realtime() 命中时会被明确要求「只输出一行读取指令，不要回答用户」，
# 于是把行为约束（语音播报规范）只写进第一轮提示词，等于对最终答案完全没生效
# ——页面上的「注入预览」看着一切正常，模型却当没看见。
#
# 实测（2026-09-26，provider=external / deepseek-chat）：assist_prompt_suffix
# 里的「全中文单位（伏特/摄氏度）」「每句输出后加喵」全部未执行，回复仍是
# 「当前电池电压为 10.8006 V。」——因为总结轮的消息体里一个字的约束都没有。
#
# 板端 RKLLM（Qwen2.5-1.5B）是另一套约束：实测提示词 >约 400 字直接空输出，
# 而数据段本身已接近该上限，所以默认（auto）只回灌给外部云模型。
SUMMARY_SPEC_MODES = ('auto', 'on', 'off')

# 总结轮收尾语（中继语音助手用；网页 Agent 对话传自己的）
SUMMARY_TAIL_ASSIST = ('直接给结论，不要说「根据数据」「根据您提供的数据」这类开场白，'
                       '不要复述问题，用中文 1~2 句回答：')


def summary_spec(spec='', provider='local', mode='auto', cap=1200):
    """决定总结轮要回灌多少「行为约束」，返回要回灌的文本（'' = 不回灌）。

    mode:
      auto（默认）—— 只回灌给外部云模型；板端 RKLLM 不回灌（见上）。
      on / off    —— 强制回灌 / 强制不回灌。
    cap: 回灌字符上限，防止长规范把板端模型顶到空输出。
    """
    s = (spec or '').strip()
    if not s:
        return ''
    m = str(mode or 'auto').strip().lower()
    if m not in SUMMARY_SPEC_MODES:
        m = 'auto'
    if m == 'off' or (m == 'auto' and str(provider) != 'external'):
        return ''
    try:
        cap = max(120, min(4000, int(cap)))
    except Exception:
        cap = 1200
    return s[:cap]


def summary_messages(collected, question, spec='', provider='local',
                     mode='auto', cap=1200, data_cap=280, tail=None):
    """拼「总结轮」的消息体：拿到工具数据 → 要一句最终回答。

    外部云模型把约束放进**真正的 system 轮**：权威性高，也不会被前面的数据段
    冲淡；板端 RKLLM 实测不认 system 轮（所以第一轮才把指令并进用户消息），
    在它身上只能把约束并进同一条用户消息。
    """
    tail = tail or SUMMARY_TAIL_ASSIST
    data = ('设备实时数据：'
            + json.dumps(collected, ensure_ascii=False)[:int(data_cap)])
    sp = summary_spec(spec, provider, mode=mode, cap=cap)
    if not sp:
        return [{'role': 'user', 'content': data + '\n' + tail + (question or '')}]
    if str(provider) == 'external':
        return [{'role': 'system', 'content': sp},
                {'role': 'user', 'content': data + '\n' + tail + (question or '')}]
    return [{'role': 'user',
             'content': '【播报要求】' + sp + '\n' + data + '\n' + tail + (question or '')}]


def build_agent_prompt(base_prompt='', enabled=None):
    """基础提示词（用户注入） + 工具说明。"""
    parts = []
    if base_prompt and base_prompt.strip():
        parts.append(base_prompt.strip())
    parts.append(tools_prompt(enabled))
    return '\n\n'.join(parts)


# ---------------------------------------------------------------------------
# 工具调用解析
# ---------------------------------------------------------------------------
# READ 协议（主）：READ get_power  /  READ get_rain {"hours": 3}  /  read=get_power
_READ_RE = re.compile(r'\bREAD\s+([A-Za-z_][A-Za-z0-9_]*)\s*(\{[^\n]{0,300}?\})?', re.I)
_READ_EQ_RE = re.compile(r'\bread\s*[=:：]\s*([A-Za-z_][A-Za-z0-9_]*)', re.I)
# 允许缺少闭合标签（stream stop 截断时）
_TC_RE = re.compile(r'<tool_call>\s*(\{.*?\})(?:\s*</tool_call>|$)', re.S)
_FENCE_RE = re.compile(r'```(?:json|tool_call|tool)?\s*(\{.*?\})\s*```', re.S)
def _iter_json_objects(text):
    """扫描文本中所有大括号配平的 {...} 片段（考虑字符串与转义）。"""
    s = text or ''
    i, n = 0, len(s)
    while i < n:
        start = s.find('{', i)
        if start < 0:
            return
        depth, instr, esc = 0, False, False
        j, end = start, -1
        while j < n:
            ch = s[j]
            if instr:
                if esc:
                    esc = False
                elif ch == '\\':
                    esc = True
                elif ch == '"':
                    instr = False
            elif ch == '"':
                instr = True
            elif ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    end = j
                    break
            j += 1
        if end < 0:
            return
        yield s[start:end + 1]
        i = end + 1


def _loads(s):
    try:
        return json.loads(s)
    except Exception:
        pass
    try:
        return json.loads(s.replace("'", '"'))
    except Exception:
        return None


def _norm(obj):
    if not isinstance(obj, dict):
        return None
    name = obj.get('name') or obj.get('tool') or obj.get('function')
    args = obj.get('arguments', obj.get('args', {}))
    if isinstance(args, str):
        args = _loads(args) or {}
    if isinstance(args, list):
        args = {}
    if not name or not isinstance(args, dict):
        return None
    return {'name': str(name).strip(), 'arguments': args}


# 模型常自造工具名（READ get_battery_voltage {}），这里做别名/关键词归一
ALIASES = {
    'get_battery': 'get_power', 'get_battery_voltage': 'get_power',
    'get_pv_voltage': 'get_power', 'get_voltage': 'get_power',
    'battery_voltage': 'get_power', 'pv_voltage': 'get_power',
    'get_power_voltage': 'get_power', 'get_power_status': 'get_power',
    'get_cpu_temperature': 'get_system', 'get_temperature': 'get_system',
    'get_temp': 'get_system', 'get_cpu': 'get_system', 'get_cpu_temp': 'get_system',
    'get_system_status': 'get_system', 'get_load': 'get_system',
    'get_wind_speed': 'get_weather', 'get_wind': 'get_weather',
    'get_weather_data': 'get_weather', 'get_weather_status': 'get_weather',
    'get_rainfall': 'get_rain', 'get_rain_data': 'get_rain',
    'get_precipitation': 'get_rain',
    'get_ptt': 'get_radio', 'get_ptt_status': 'get_radio',
    'get_transmit_status': 'get_radio', 'get_radio_status': 'get_radio',
    'get_camera_status': 'get_camera', 'get_video': 'get_camera',
    'get_current_time': 'get_time', 'get_datetime': 'get_time',
    'get_date': 'get_time', 'get_now': 'get_time',
    'say': 'speak', 'tts': 'speak', 'tts_speak': 'speak',
    'play_voice': 'speak', 'broadcast': 'speak',
}

KEYWORDS = (
    (('battery', 'pv', 'voltage', 'volt', 'power', 'electric'), 'get_power'),
    (('temp', 'cpu', 'load', 'memory', 'disk', 'system', 'uptime'), 'get_system'),
    (('wind', 'weather', 'meteor'), 'get_weather'),
    (('rain', 'precip'), 'get_rain'),
    (('ptt', 'radio', 'transmit', 'tx'), 'get_radio'),
    (('camera', 'video', 'record'), 'get_camera'),
    (('time', 'date', 'clock'), 'get_time'),
    (('speak', 'say', 'tts', 'voice', 'broadcast'), 'speak'),
)


def resolve_tool(name, valid=None):
    """把模型给出的（可能自造的）工具名归一成真实工具名。"""
    n = re.sub(r'[^a-z0-9_]', '', str(name or '').lower())
    if not n:
        return None
    vset = set(valid) if valid else set(tool_index().keys())
    if n in vset:
        return n
    a = ALIASES.get(n)
    if a and a in vset:
        return a
    for keys, tool in KEYWORDS:
        if any(k in n for k in keys) and tool in vset:
            return tool
    for t in vset:
        if t in n or n in t:
            return t
    return None


def parse_tool_calls(text, valid=None):
    """解析模型输出里的读取指令，返回 [{'name':…, 'arguments':{…}}]。

    valid：可接受的工具名集合（None 表示不校验）。
    """
    text = text or ''
    vset = set(valid) if valid else None
    out = []
    # 1) READ 名称 {参数}（允许模型自造名字，用别名/关键词归一）
    for m in _READ_RE.finditer(text):
        name = resolve_tool(m.group(1), valid)
        if not name:
            continue
        args = _loads(m.group(2)) if m.group(2) else {}
        item = {'name': name, 'arguments': args if isinstance(args, dict) else {}}
        if item not in out:
            out.append(item)
    if out:
        return out
    # 2) read=名称 / read: 名称
    for m in _READ_EQ_RE.finditer(text):
        name = resolve_tool(m.group(1), valid)
        if name and {'name': name, 'arguments': {}} not in out:
            out.append({'name': name, 'arguments': {}})
    if out:
        return out
    # 3) 裸工具名（只认精确名字，避免正文误触发）
    if vset:
        for tok in re.findall(r'[A-Za-z_][A-Za-z0-9_]*', text):
            if tok in vset:
                return [{'name': tok, 'arguments': {}}]
    for m in _TC_RE.finditer(text or ''):
        o = _norm(_loads(m.group(1)))
        if o:
            out.append(o)
    if not out:
        for m in _FENCE_RE.finditer(text or ''):
            o = _norm(_loads(m.group(1)))
            if o:
                out.append(o)
    if not out:
        for frag in _iter_json_objects(text):
            o = _norm(_loads(frag))
            if o:
                out.append(o)
                break            # 裸 JSON 只认第一个工具调用
    return out


def strip_tool_calls(text):
    """去掉读取指令片段，得到给用户看的正文。"""
    text = _READ_RE.sub('', text or '')
    text = _READ_EQ_RE.sub('', text)
    text = _TC_RE.sub('', text)
    text = _FENCE_RE.sub('', text)
    return text.strip()


class StreamFilter:
    """流式过滤：把 <tool_call>…</tool_call> 从展示流中剔除（支持跨 chunk）。"""

    def __init__(self):
        self.buf = ''
        self.in_tc = False

    def feed(self, chunk, final=False):
        self.buf += (chunk or '')
        out = []
        while True:
            if self.in_tc:
                i = self.buf.find(TOOL_CALL_CLOSE)
                if i < 0:
                    if len(self.buf) > 8192:      # 防御异常超长
                        self.buf = ''
                    break
                self.buf = self.buf[i + len(TOOL_CALL_CLOSE):]
                self.in_tc = False
                continue
            i = self.buf.find(TOOL_CALL_OPEN)
            if i < 0:
                keep = 0
                for k in range(1, min(len(self.buf), len(TOOL_CALL_OPEN)) + 1):
                    if TOOL_CALL_OPEN.startswith(self.buf[-k:]):
                        keep = k
                out.append(self.buf[:-keep] if keep else self.buf)
                self.buf = self.buf[-keep:] if keep else ''
                break
            out.append(self.buf[:i])
            self.buf = self.buf[i + len(TOOL_CALL_OPEN):]
            self.in_tc = True
        if final:
            if not self.in_tc:
                out.append(self.buf)
            self.buf = ''
        return ''.join(out)


# ---------------------------------------------------------------------------
# 生成速率统计
# ---------------------------------------------------------------------------
def estimate_tokens(text):
    """粗略 token 估算：CJK/全角按 1 token/字，其余按 ~4 字符/token。"""
    if not text:
        return 0
    cjk = other = 0
    for ch in text:
        o = ord(ch)
        if 0x2E80 <= o <= 0x9FFF or 0xFF00 <= o <= 0xFFEF or 0x3000 <= o <= 0x303F:
            cjk += 1
        else:
            other += 1
    return cjk + int(math.ceil(other / 4.0))


class RateMeter:
    """实时生成速率：TTFT（首 token 延迟）、tokens、tok/s。"""

    def __init__(self):
        self.t0 = time.time()
        self.ttft = None
        self.tokens = 0
        self.chars = 0

    def add(self, text):
        if not text:
            return
        if self.ttft is None:
            self.ttft = time.time() - self.t0
        self.chars += len(text)
        self.tokens += estimate_tokens(text)

    def snapshot(self, extra=None):
        now = time.time()
        el = now - self.t0
        ttft = self.ttft if self.ttft is not None else el
        gen = max(0.001, el - ttft)
        out = {
            'ttft_ms': round(ttft * 1000),
            'tokens': self.tokens,
            'chars': self.chars,
            'elapsed_ms': round(el * 1000),
            'gen_ms': round(gen * 1000),
            'tok_per_s': round(self.tokens / gen, 2) if self.tokens else 0.0,
        }
        if extra:
            out.update(extra)
        return out


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------
def execute_tool(name, args, ctx):
    """执行工具。ctx: {工具名: callable(**args)}。返回 (ok, result_dict)。"""
    name = str(name or '').strip()
    args = args if isinstance(args, dict) else {}
    fn = (ctx or {}).get(name)
    if fn is None:
        return False, {'error': f'未知工具：{name}',
                       'available': sorted((ctx or {}).keys())}
    try:
        res = fn(**args)
        return True, res if isinstance(res, dict) else {'result': res}
    except TypeError as e:
        return False, {'error': f'参数不匹配：{e}'}
    except Exception as e:
        return False, {'error': f'{type(e).__name__}: {e}'}


def tool_result_message(results):
    """把工具结果整理成回灌给模型的文本。"""
    lines = ['工具执行结果（JSON，请据此回答，不要编造）：']
    for r in results:
        lines.append(json.dumps(r, ensure_ascii=False))
    return '\n'.join(lines)

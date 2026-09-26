#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ELF2 智能中继网页控制中心 (Flask)
运行在 ELF2/RK3588 上，提供：
- 登录 / 用户管理 (SQLite, 密码哈希, 会话签名)
- 光伏 / 电池电压 ADC 读取与零点/倍率校准
- CPU / 内存 / 温度负载
- LLM 对话，本地/外部 OpenAI 兼容 API 切换
- 网页对讲录音分段，并播放到板载 3.5mm AUX
- 摄像头 / 气象预留页

作者：智能中继项目
"""

import array
import atexit
import csv
import io
import json
import math
import os
import queue
import re
import secrets
import traceback
from collections import deque
import shutil
import sqlite3
import struct
import subprocess
import threading
import time
import uuid
import wave
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

import requests
from flask import (Flask, Response, abort, flash, g, jsonify, redirect,
                   render_template, request, send_file, session, url_for,
                   stream_with_context)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

import tts_service
import agent_service
import asr_service
import camera_service
import weather_service
import voice_service
import assistant_service
import aprs_service
import energy_service

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / 'relay.db'
RECORDINGS_DIR = BASE_DIR / 'recordings'
ASR_RECORD_DIR = BASE_DIR / 'asr_recordings'      # 语音识别录音留档
INSTANCE_DIR = BASE_DIR / 'instance'
SECRET_FILE = INSTANCE_DIR / 'secret.key'

ADC_DEVICE = Path('/sys/bus/iio/devices/iio:device0')
ADC_SCALE_RAW = ADC_DEVICE / 'in_voltage_scale'
# RK3588 SARADC：12bit、输入 0~1.8V（AVDD_1V8），LSB = 1.8/4096 = 0.439453125 mV
ADC_FULL_SCALE_V = 1.8
ADC_BITS = 12
ADC_MULT_UNIT_FLAG = 'adc_mult_unit'      # 口径迁移标记（旧值 V/raw → 新值 V/引脚电压）
ADC_MULT_UNIT_VALUE = 'vin'
ADC_CHANNELS = {
    'battery': {
        'label': '电池电压',
        'raw_file': 'in_voltage4_raw',
        'default_channel': 4,               # SARADC_VIN4（ELF2 P1_36）
        'default_zero': 0.0,
        # 分压比 = R下 /(R14+R15+R16) = 10k/101.1k = 0.098912
        # 倍率 = 1/分压比 = (R14+R15+R16)/R16 = 101.1k/10k = 10.11 V/引脚电压
        'default_multiplier': 10.11,
        'divider_upper_ohm': 100 + 91000,   # R14 100Ω + R15 91kΩ
        'divider_lower_ohm': 10000,         # R16 10kΩ
        'divider_desc': 'R14 100Ω + R15 91kΩ / R16 10kΩ',
        'unit': 'V',
    },
    'pv': {
        'label': '光伏电压',
        'raw_file': 'in_voltage6_raw',
        'default_channel': 6,               # SARADC_VIN6（按设计若接 P1_38=VIN5 可改 5）
        'default_zero': 0.0,
        # 分压比 = 10k/170.1k = 0.058789；倍率 = 170.1k/10k = 17.01 V/引脚电压
        'default_multiplier': 17.01,
        'divider_upper_ohm': 100 + 160000,  # 100Ω + 160kΩ
        'divider_lower_ohm': 10000,         # 10kΩ
        'divider_desc': '100Ω + 160kΩ / 10kΩ',
        'unit': 'V',
    },
}

AUDIO_DEVICE = os.environ.get('RELAY_AUDIO_DEVICE', 'plughw:CARD=rockchipnau8822,DEV=0')
weather_service_instance = weather_service.WeatherService(DB_PATH)
voice_service_instance = voice_service.VoiceService(DB_PATH)
aprs_service_instance = aprs_service.AprsService(DB_PATH)
assistant_service_instance = assistant_service.AssistantService(DB_PATH)
MAX_RECORDING_BYTES = 32 * 1024 * 1024          # 单次录音上传上限
# 音色包 / 训练数据 zip 可以很大（Rosmontis 音色包约 56MB），这里单独放宽
MAX_UPLOAD_BYTES = int(os.environ.get('RELAY_MAX_UPLOAD_MB', '512') or 512) * 1024 * 1024
PLAY_LOCK = threading.Lock()
CURRENT_PLAY_PROC = None


def _stop_proc(proc, grace=1.0):
    """终止一个正在播放的 aplay：先 TERM，超时再 KILL。

    同一张声卡同一时刻只允许一路 aplay，否则第二路会因设备忙直接失败。
    """
    if proc is None:
        return
    try:
        if proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=grace)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    except Exception:
        pass
CPU_LOCK = threading.Lock()
CPU_PREV = {'idle': 0, 'total': 0}

# PTT 控制：GPIO3_A1 -> Linux 全局 GPIO 97（gpiochip3 base 96 + A1=1）
PTT_GPIO_NUM = int(os.environ.get('RELAY_PTT_GPIO', '97') or 97)
PTT_GPIO_ACTIVE_HIGH = os.environ.get('RELAY_PTT_ACTIVE_HIGH', '1') not in ('0', 'false', 'False', 'no', 'off')
PTT_GPIO_DIR = Path(f'/sys/class/gpio/gpio{PTT_GPIO_NUM}')
PTT_LOCK = threading.Lock()
PTT_INITED = False
PTT_LEVEL = False
PTT_HOLD_COUNT = 0
PTT_RELEASE_TIMER = None
PTT_LAST_ERROR = ''
PTT_ON_SINCE = 0.0
# 最短压发时间（秒）：抑制「极短时间内反复触发 PTT」（继电器/功放最怕连续 key）
PTT_MIN_HOLD = float(os.environ.get('RELAY_PTT_MIN_HOLD', '1.0') or 1.0)

# 设置/校准页「按住发射」硬件自检：前端每 1s 续一次心跳，超时/超上限自动松开
PTT_MANUAL = {
    'held': False,          # 前端是否仍按住
    'counted': False,       # 是否已 _ptt_retain（引用计数已 +1）
    'until': 0.0,           # 心跳过期时间
    'since': 0.0,           # 本次按住开始时间
    'client': '',
}
PTT_MANUAL_HEARTBEAT = float(os.environ.get('RELAY_PTT_HEARTBEAT', '8') or 8)   # 心跳超时（秒）
PTT_MANUAL_MAX = float(os.environ.get('RELAY_PTT_MANUAL_MAX', '30') or 30)      # 单次最长压发（秒）

# PTT 事件环形缓冲：每次拉高/拉低都记录「动作 + 原因 + 调用者」，
# 便于区分「软件反复拉低」还是「硬件侧电平抖动」。GET /api/ptt/diag 可见。
PTT_EVENTS = deque(maxlen=300)
_PTT_EVENT_SKIP = {
    '_ptt_caller', '_ptt_event', '_ptt_set_level', '_ptt_retain', '_ptt_release',
    '_ptt_release_now', '_ptt_force_low', '_ptt_manual_start', '_ptt_manual_stop',
    '_intercom_push_stop', 'ptt_audio_hold', '_play_audio_wait',
}


def _ptt_caller():
    """记录真实调用者（函数名:行号），跳过本文件内部的 PTT 辅助函数。"""
    try:
        for fr in reversed(traceback.extract_stack()[:-2]):
            if fr.name not in _PTT_EVENT_SKIP:
                return f'{fr.name}:{fr.lineno}'
    except Exception:
        pass
    return ''


def _ptt_event(action, reason=''):
    try:
        PTT_EVENTS.append({
            't': time.strftime('%H:%M:%S'),
            'ms': int((time.time() % 1) * 1000),
            'action': action,
            'reason': str(reason)[:60],
            'level': 1 if PTT_LEVEL else 0,
            'hold': PTT_HOLD_COUNT,
            'by': _ptt_caller(),
        })
    except Exception:
        pass


# ---------------------------------------------------------------------------
# BUSY 输入检测：GPIO3_A5 -> Linux 全局 GPIO 101（gpiochip3 base 96 + A5=5）
# 控制板 BUSY 经光耦/分压后输入 ELF2（本模块只读不写）。极性由设置项
# busy_active_low 决定（默认 0=高有效，与 2026-09-23 实测空闲电平 0 一致），
# 可在「设置与电压校准 → BUSY 接收状态」里一键切换。功能：
#   1) 总览页「中继状态」实时显示 BUSY 接收状态；
#   2) 与 PTT 组合判断「自己正在发射却仍然 BUSY」的自激/串音风险；
#   3) 记录电平沿与触发时长，便于硬件排查（/api/busy/diag）。
# 注意：3.3V 数字输入判据为 VIL≈0.99V / VIH≈2.31V。触发时若电平停在
#       1.0~2.3V 之间属于「不确定区」，sysfs 读数不可靠；/api/busy/status
#       的 edges（电平变化次数）与 sysfs_value（原始电平）用来判断接线是否有效。
# ---------------------------------------------------------------------------
BUSY_GPIO_NUM = int(os.environ.get('RELAY_BUSY_GPIO', '101') or 101)
BUSY_ACTIVE_LOW = os.environ.get('RELAY_BUSY_ACTIVE_LOW', '0') not in ('0', 'false', 'False', 'no', 'off')
BUSY_GPIO_DIR = Path(f'/sys/class/gpio/gpio{BUSY_GPIO_NUM}')
BUSY_POLL = float(os.environ.get('RELAY_BUSY_POLL', '0.25') or 0.25)
BUSY_EVENTS = deque(maxlen=300)
BUSY_LOCK = threading.Lock()
BUSY_STATE = {
    'level': None,        # sysfs 原始电平 0/1，None = 尚未读到
    'active': False,      # 是否处于「BUSY 触发（收到信号）」
    'since': 0.0,         # 本次触发开始时间
    'last_change': 0.0,   # 最近一次电平变化时间
    'edges': 0,           # 原始电平变化次数（判断引脚有没有真的动）
    'count': 0,           # 触发次数（未触发 -> 触发）
    'total': 0.0,         # 累计触发时长（秒）
    'exported': False,    # GPIO 是否已导出为输入
    'tx_conflict': False,  # 发射期间仍 BUSY
    'error': '',
}


def _busy_sysfs_read(name):
    try:
        p = BUSY_GPIO_DIR / name
        return p.read_text(encoding='utf-8').strip() if p.exists() else ''
    except Exception as e:
        return f'ERR:{type(e).__name__}'


def _busy_export():
    '''确保 BUSY GPIO 已导出为输入。

    正常由 elf2-ptt-gpio.service 开机完成（root 导出 + chown elf），
    这里只是兜底：进程若以 root 运行也能自愈。
    '''
    try:
        if not BUSY_GPIO_DIR.exists():
            with open('/sys/class/gpio/export', 'w') as f:
                f.write(str(BUSY_GPIO_NUM))
            time.sleep(0.2)
        ok = BUSY_GPIO_DIR.exists()
        if ok:
            try:
                (BUSY_GPIO_DIR / 'direction').write_text('in', encoding='utf-8')
            except Exception:
                pass
        with BUSY_LOCK:
            BUSY_STATE['exported'] = bool(ok)
            if ok:
                BUSY_STATE['error'] = ''
            else:
                BUSY_STATE['error'] = f'GPIO {BUSY_GPIO_NUM} 未导出（可能是权限或引脚被占用）'
        return bool(ok)
    except Exception as e:
        with BUSY_LOCK:
            BUSY_STATE['exported'] = False
            BUSY_STATE['error'] = f'导出失败: {type(e).__name__}: {e}'
        return False


def _busy_read_level():
    try:
        return int((BUSY_GPIO_DIR / 'value').read_text(encoding='utf-8').strip())
    except Exception as e:
        with BUSY_LOCK:
            BUSY_STATE['error'] = f'读取失败: {type(e).__name__}'
        return None


def _busy_active_low():
    '''BUSY 有效极性：环境变量优先，其次读设置项（默认低有效）。'''
    env = os.environ.get('RELAY_BUSY_ACTIVE_LOW')
    if env is not None:
        return env not in ('0', 'false', 'False', 'no', 'off')
    try:
        return _setting_direct('busy_active_low', '0') not in ('0', 'false', 'False', 'no', 'off')
    except Exception:
        return True


def _busy_active_from_level(level):
    if level is None:
        return False
    return (level == 0) if _busy_active_low() else (level == 1)


def _busy_event(action, level=None, extra=''):
    try:
        BUSY_EVENTS.append({
            't': time.strftime('%H:%M:%S'),
            'ms': int((time.time() % 1) * 1000),
            'action': action,
            'level': level,
            'extra': str(extra)[:60],
        })
    except Exception:
        pass


def _busy_watchdog():
    '''常驻线程：轮询 BUSY 引脚，记录触发沿、时长与事件。'''
    _busy_export()
    while True:
        try:
            level = _busy_read_level()
            if level is None:
                _busy_export()
            else:
                now = time.time()
                active = _busy_active_from_level(level)
                with BUSY_LOCK:
                    prev = BUSY_STATE['level']
                    if prev != level:
                        BUSY_STATE['edges'] = int(BUSY_STATE.get('edges') or 0) + 1
                        BUSY_STATE['last_change'] = now
                        BUSY_STATE['level'] = level
                        if prev is not None:
                            _busy_event('level-change', level)
                    was_active = bool(BUSY_STATE['active'])
                    BUSY_STATE['active'] = active
                    if active and not was_active:
                        BUSY_STATE['since'] = now
                        BUSY_STATE['count'] = int(BUSY_STATE.get('count') or 0) + 1
                        _busy_event('busy-on', level)
                    elif was_active and not active:
                        if BUSY_STATE.get('since'):
                            BUSY_STATE['total'] = float(BUSY_STATE.get('total') or 0.0) + (now - float(BUSY_STATE['since']))
                        BUSY_STATE['since'] = 0.0
                        _busy_event('busy-off', level)
                    BUSY_STATE['tx_conflict'] = bool(active and PTT_LEVEL)
                    BUSY_STATE['error'] = ''
        except Exception as e:
            with BUSY_LOCK:
                BUSY_STATE['error'] = f'{type(e).__name__}: {e}'
        time.sleep(max(0.05, BUSY_POLL))


def busy_status():
    '''总览页「BUSY 接收状态」数据源。'''
    with BUSY_LOCK:
        st = dict(BUSY_STATE)
    now = time.time()
    st.update({
        'gpio': BUSY_GPIO_NUM,
        'chip': 'gpiochip3',
        'line': 5,
        'active_low': _busy_active_low(),
        'sysfs': str(BUSY_GPIO_DIR),
        'sysfs_value': _busy_sysfs_read('value'),
        'direction': _busy_sysfs_read('direction'),
        'on_for': round(now - float(st['since']), 1) if st.get('since') else 0,
        'idle_for': round(now - float(st['last_change']), 1) if st.get('last_change') else 0,
        'total': round(float(st.get('total') or 0.0), 1),
        'tx_conflict': bool(st.get('tx_conflict')),
    })
    return st


def busy_diag():
    '''BUSY 链路自检（硬件排查：引脚有没有电平变化、光耦是否真的拉低）。'''
    st = busy_status()
    level = _busy_read_level()
    return {
        'busy': st,
        'gpio_num': BUSY_GPIO_NUM,
        'chip': 'gpiochip3',
        'line': 5,
        'active_low': _busy_active_low(),
        'poll': BUSY_POLL,
        'raw_level': level,
        'hint': ('引脚电平始终为 1 且 edges=0：要么 BUSY 没触发，要么光耦未把电平拉到数字低门限'
                 '（3.3V 系统 VIL≈0.99V）。可加对地下拉/提高光耦驱动电流后重测。'),
        'events': list(BUSY_EVENTS)[-60:],
    }


threading.Thread(target=_busy_watchdog, daemon=True).start()

# ---------------------------------------------------------------------------
# Flask 应用
# ---------------------------------------------------------------------------
app = Flask(__name__, static_folder='static', template_folder='templates')
app.config.update(
    MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    PERMANENT_SESSION_LIFETIME=8 * 3600,
    JSON_AS_ASCII=False,
)


@app.errorhandler(413)
def _too_large(_e):
    """请求体超限时返回 JSON（否则浏览器只能看到 “Failed to fetch/连接被重置”）。"""
    limit_mb = MAX_UPLOAD_BYTES / 1024 / 1024
    return api_err(f'上传内容过大（上限 {limit_mb:.0f} MB）。'
                   f'音色包较大的话可设置环境变量 RELAY_MAX_UPLOAD_MB 提高上限后重启服务。', 413)


@app.after_request
def _no_cache_dynamic_html(resp):
    """避免浏览器缓存模板页，部署后立刻看到最新界面。"""
    if resp.mimetype == 'text/html':
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        resp.headers['Pragma'] = 'no-cache'
    return resp


def _load_secret_key():
    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
    if SECRET_FILE.exists():
        return SECRET_FILE.read_text(encoding='utf-8').strip()
    key = secrets.token_urlsafe(48)
    SECRET_FILE.write_text(key, encoding='utf-8')
    os.chmod(SECRET_FILE, 0o600)
    return key


app.secret_key = _load_secret_key()
RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
ASR_RECORD_DIR.mkdir(parents=True, exist_ok=True)

# nginx 反向代理（80 -> 443 -> 127.0.0.1:8080）时，让 request.scheme/remote_addr 反映真实来源
try:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
except Exception as _e:  # pragma: no cover
    print(f'[warn] ProxyFix 未启用: {_e}')


# ---------------------------------------------------------------------------
# 数据库
# ---------------------------------------------------------------------------
def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(str(DB_PATH), timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA journal_mode=WAL')
        g.db.execute('PRAGMA synchronous=NORMAL')
        g.db.execute('PRAGMA foreign_keys=ON')
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')


def _set_default_settings(db):
    defaults = {
        'llm_provider': 'local',
        'local_base_url': 'http://127.0.0.1:8001/v1',
        'local_model': 'qwen2.5-1.5b',
        'local_api_key': '',
        'external_base_url': 'https://api.deepseek.com/v1',
        'external_model': 'deepseek-chat',
        'external_api_key': '',
        'battery_adc_channel': str(ADC_CHANNELS['battery']['default_channel']),
        'battery_zero_raw': str(ADC_CHANNELS['battery']['default_zero']),
        'battery_multiplier': str(ADC_CHANNELS['battery']['default_multiplier']),
        'pv_adc_channel': str(ADC_CHANNELS['pv']['default_channel']),
        'pv_zero_raw': str(ADC_CHANNELS['pv']['default_zero']),
        'pv_multiplier': str(ADC_CHANNELS['pv']['default_multiplier']),
        # 能量统计：电压此前不落库，全天时间轴靠这个采样器攒
        'energy_log_enabled': '1',
        'energy_sample_sec': '60',
        'energy_retention_days': '365',
        'record_auto_play': '1',
        'site_title': 'ELF2 智能中继控制中心',
        # 提示词注入 / Agent 工具
        'llm_system_prompt': '',
        'llm_system_prompt_on': '1',
        'llm_prompt_vars': '1',
        'agent_enabled': '1',
        'agent_max_iters': '3',
        'agent_tools': '',
        # 中继语音助手（BUSY 语音唤醒 → ASR → LLM → TTS → 受控发射）
        'assist_enabled': '0',
        'assist_wake_words': '智能中继,中继台',
        'assist_wake_fuzzy': '1',
        'assist_channel': 'left',
        'assist_dbfs_open': '-50',
        'assist_dbfs_close': '-56',
        'assist_preroll_ms': '1200',
        'assist_silence_ms': '450',
        'assist_min_speech_ms': '350',
        'assist_max_utterance': '15',
        'assist_followup_seconds': '30',
        'assist_ack_reply': '请讲',
        'assist_use_vad': '1',
        'assist_enhance': '1',
        'assist_max_tx_seconds': '30',
        'assist_min_gap_seconds': '15',
        'assist_busy_wait_seconds': '8',
        'assist_tx_guard_ms': '600',
        'assist_quiet_hours': '',
        'assist_test_mode': '0',
        'assist_max_reply_chars': '80',
        'assist_max_tokens': '256',
        'assist_history_turns': '6',
        'assist_max_input_chars': '3000',
        'assist_temperature': '0.3',
        'assist_use_tools': '1',
        'assist_agent_iters': '2',
        'assist_keep_llm_warm': '1',
        'assist_llm_wait': '25',
        'assist_prompt_suffix': '你是中继台的语音助手，回复会被合成语音后发射出去。\n只输出可直接朗读的纯口语，不要 Markdown、星号、井号、列表、emoji；\n不超过 {max_chars} 字，一句答完，不复述问题、不解释过程；\n没数据就说不知道，不要编造。',
        'assist_voice': '',
        'assist_retention_days': '30',
        'assist_debug_keep': '12',
        # 端侧语音识别（ASR）
        'asr_enabled': '1',
        'asr_model_dir': '',
        'asr_language': 'auto',
        # 中继语音日志（BUSY/PTT 触发录音 + 异步 ASR + 每日总结）
        'vlog_enabled': '1',
        'vlog_enhance': '1',
        'vlog_dir': '/opt/ai/relay_voice',
        'vlog_channel': 'left',
        'vlog_pre_roll': '3.0',
        'vlog_post_roll': '2.0',
        'vlog_min_seconds': '1.0',
        'vlog_max_seconds': '300',
        'vlog_silence_dbfs': '-48',
        'vlog_asr_enabled': '1',
        'vlog_vad_enabled': '1',
        'vlog_keep_transient': '0',
        'vlog_retention_days': '30',
        'vlog_retention_mb': '20480',
        'vlog_summary_enabled': '1',
        'vlog_summary_time': '23:30',
        'vlog_summary_provider': 'auto',
        'vlog_llm_on_demand': '1',
        'vlog_llm_idle_unload': '300',
        'vlog_callsign_whitelist': 'BI7KHI',
        'vlog_callsign_max_dist': '0',
        # APRS 收发（自研 1200bps Bell202 软件 TNC）
        'aprs_enabled': '1',
        'aprs_mycall': 'BI7KHI',
        'aprs_ssid': '10',
        'aprs_dest': 'APRS',
        'aprs_path': 'WIDE1-1,WIDE2-1',
        'aprs_lat': '22.533300',
        'aprs_lon': '114.050000',
        'aprs_beacon_enabled': '1',
        'aprs_weather_enabled': '1',
        'aprs_telemetry_enabled': '1',
        'aprs_map_provider': 'tianditu',
        'aprs_map_tk': 'eaa1673e60065f76cbf3c970063ec475',
        'aprs_map_layers': 'img,cva',
        # 全局音频
        'audio_volume_percent': '80',
        'audio_muted': '0',
        # 麦克风输入源与增益
        'mic_source': 'headset',
        'mic_channel': 'left',
        'mic_pga_percent': '60',
        'mic_adc_percent': '100',
        'mic_pga_boost': '1',
        'mic_l2r2_percent': '0',
        'mic_aux_boost_percent': '0',
        # 摄像头 / 录像 / OSD / RTMP
        'camera_device': '/dev/video21',
        'camera_resolution': '640x480',
        'camera_fps': '15',
        'camera_quality': '5',
        'camera_osd_enabled': '1',
        'camera_osd_text': 'ELF2 RELAY',
        'camera_osd_show_time': '1',
        'camera_osd_position': 'top-left',
        'camera_osd_fontsize': '18',
        'camera_osd_color': 'white',
        'camera_record_dir': '/www/camera_recordings',
        'camera_loop_seconds': '60',
        'camera_loop_max_mb': '2048',
        'camera_loop_max_files': '100',
        'camera_storage_max_mb': '8192',
        'camera_loop_autostart': '1',
        'camera_rtmp_url': '',
        # 气象 RS485 / Modbus RTU
        'weather_enabled': '1',
        'weather_port': '/dev/ttyS9',
        'weather_baud': '9600',
        'weather_parity': 'N',
        'weather_stopbits': '1',
        'weather_timeout': '1.0',
        'weather_slave': '1',
        'weather_function': '3',
        'weather_register': '0',
        'weather_quantity': '1',
        'weather_scale': '0.1',
        'weather_poll_interval': '2',
        # 翻斗式降水量传感器（与风力变送器共用 RS485 总线，站号不同）
        'rain_enabled': '1',
        'rain_slave': '23',
        'rain_function': '3',
        'rain_register': '0',
        'rain_quantity': '1',
        'rain_scale': '0.1',
        'rain_cumulative': '1',
        # 温湿度变送器（Modbus RTU 9600 8N1，从站地址 03；2026-09 起预留，未接线）
        'th_enabled': '0',
        'th_slave': '3',
        'th_function': '4',
        'th_register': '1',
        'th_quantity': '2',
        'th_scale': '0.1',
        'th_humi_scale': '0.1',
        'th_temp_offset': '0.0',
        # BUSY 输入极性：1=低有效（触发时引脚被拉低），0=高有效（触发时引脚被拉高）
        # 2026-09-23 实测：未触发时引脚电平为 0（3.3V 数字输入判为低），
        # 因此默认按「高有效」判定，即触发时引脚被拉到约 2.7V（>VIH 2.31V）判为接收中。
        # 若实际是「触发拉低」的接法，在 设置与电压校准 → BUSY 接收状态 里一键切换。
        'busy_active_low': '0',
        # TTS（仅本地 Piper 离线模型，外部 OpenAI 兼容 TTS 已下线）
        'tts_provider': 'local',
        'tts_local_voice': 'zh_CN-huayan-medium',
        'tts_en_voice': '',        # 英文片段使用的音色（空=自动挑 language 为 en 的音色）
        'tts_icao': '1',           # 呼号/单字母按 ICAO 字母解释法朗读
        'tts_icao_voice': '',      # ICAO 字母串专用音色（空=自动优先 lessac 等官方英文音色）
        # 默认常开：LLM 流式输出时边出字边用 Piper 朗读。
        # 这是「运行策略」而不是靠前端复选框——前端同步一旦失败就会静默关掉流式朗读。
        'tts_auto_speak': '1',
        # 定时重启计划：每天多个 HH:MM（逗号分隔），到点前先语音播报再重启。
        # 重启走 /usr/local/sbin/elf2-reboot.sh 的 sudoers 白名单（见 board/deploy/）。
        'reboot_enabled': '0',
        'reboot_times': '',
        'reboot_notice_sec': '30',
        'reboot_text': '中继台即将重启，请稍候。',
    }
    for k, v in defaults.items():
        db.execute('INSERT OR IGNORE INTO settings(key, value) VALUES(?,?)', (k, v))

    # 一次性迁移：旧口径倍率（V/raw，系数≈0.004~0.009）→ 新口径（V/引脚电压，≈10.11/17.01）。
    # 分压采集板已改为 R14 100Ω+R15 91kΩ/R16 10kΩ（电池）与 100Ω+160kΩ/10kΩ（光伏），
    # 旧校准值不再适用，首次升级时按设计值重置并打标记，之后不再覆盖用户校准值。
    _row = db.execute('SELECT value FROM settings WHERE key=?', (ADC_MULT_UNIT_FLAG,)).fetchone()
    _flag = (_row[0] if _row else '') if not hasattr(_row, 'keys') else _row['value']
    if _flag != ADC_MULT_UNIT_VALUE:
        for _key, _ch in ADC_CHANNELS.items():
            db.execute('INSERT OR REPLACE INTO settings(key, value) VALUES(?,?)',
                       (f'{_key}_multiplier', str(_ch['default_multiplier'])))
        db.execute('INSERT OR REPLACE INTO settings(key, value) VALUES(?,?)',
                   (ADC_MULT_UNIT_FLAG, ADC_MULT_UNIT_VALUE))


def init_db():
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.executescript(
        '''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE COLLATE NOCASE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            last_login TEXT
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            username TEXT,
            action TEXT NOT NULL,
            detail TEXT,
            ip TEXT
        );
        CREATE TABLE IF NOT EXISTS recordings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            username TEXT,
            filename TEXT NOT NULL,
            duration_ms INTEGER DEFAULT 0,
            size_bytes INTEGER DEFAULT 0,
            note TEXT
        );
        CREATE TABLE IF NOT EXISTS chat_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            username TEXT,
            provider TEXT,
            model TEXT,
            role TEXT,
            content TEXT
        );
        CREATE TABLE IF NOT EXISTS asr_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            username TEXT,
            filename TEXT,
            seconds REAL DEFAULT 0,
            ms INTEGER DEFAULT 0,
            rtf REAL DEFAULT 0,
            text TEXT
        );
        CREATE TABLE IF NOT EXISTS llm_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            provider TEXT,
            model TEXT,
            mode TEXT,
            ttft_ms INTEGER DEFAULT 0,
            tokens INTEGER DEFAULT 0,
            elapsed_ms INTEGER DEFAULT 0,
            tok_per_s REAL DEFAULT 0,
            iters INTEGER DEFAULT 0,
            tools TEXT DEFAULT '',
            ok INTEGER DEFAULT 1,
            note TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS voltage_readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            ts_epoch REAL NOT NULL,
            battery REAL,
            pv REAL,
            battery_raw INTEGER,
            pv_raw INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_volt_ts_epoch ON voltage_readings(ts_epoch);
        CREATE INDEX IF NOT EXISTS idx_volt_ts ON voltage_readings(ts);
        '''
    )
    db.execute('PRAGMA journal_mode=WAL')
    _set_default_settings(db)
    admin = db.execute('SELECT id FROM users WHERE username = ?', ('Admin',)).fetchone()
    if not admin:
        initial_admin_password = os.environ.get('RELAY_INIT_ADMIN_PASSWORD', '12341234')
        db.execute(
            'INSERT INTO users(username,password_hash,role,is_active,created_at) VALUES(?,?,?,?,?)',
            ('Admin', generate_password_hash(initial_admin_password), 'admin', 1, now_iso()),
        )
    db.commit()
    db.close()


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def db_exec(query, args=()):
    db = get_db()
    cur = db.execute(query, args)
    db.commit()
    return cur


def audit(action, detail='', username=None, ip=None):
    try:
        db = get_db()
        db.execute(
            'INSERT INTO audit_log(ts,username,action,detail,ip) VALUES(?,?,?,?,?)',
            (now_iso(), username or session.get('username', 'anonymous'), action, detail,
             ip or request.remote_addr if request else ''),
        )
        db.commit()
    except Exception:
        pass


def get_setting(key, default=''):
    try:
        row = get_db().execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return row['value'] if row else default
    except RuntimeError:
        # 后台线程 / 启动阶段没有 Flask 应用上下文，回退为直连数据库读取
        return _setting_direct(key, default)


def set_setting(key, value):
    db_exec('INSERT INTO settings(key,value) VALUES(?,?) '
            'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))


def bool_setting(key, default=True):
    return get_setting(key, '1' if default else '0') in ('1', 'true', 'True', 'yes', 'on')


def csrf_token():
    token = session.get('csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['csrf_token'] = token
    return token


app.jinja_env.globals['csrf_token'] = csrf_token


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get('user_id'):
            if request.path.startswith('/api/'):
                return jsonify({'ok': False, 'error': '未登录'}), 401
            return redirect(url_for('login', next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get('role') != 'admin':
            if request.path.startswith('/api/'):
                return jsonify({'ok': False, 'error': '需要管理员权限'}), 403
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def api_ok(**kwargs):
    payload = {'ok': True}
    payload.update(kwargs)
    return jsonify(payload)


def api_err(message, status=400):
    return jsonify({'ok': False, 'error': message}), status


def _csrf_from_request():
    """从请求里取 CSRF token。

    注意：不能用 `request.json` 直接判断 —— 对 multipart / octet-stream 等
    非 JSON 请求体，Flask 访问 request.json 会直接抛 400 BadRequest，
    导致所有"非 JSON 的 POST"（音色包上传、实时对讲 PCM 推流等）都失败。
    """
    token = request.headers.get('X-CSRF-Token')
    if token:
        return token
    try:
        if request.is_json:
            return (request.get_json(silent=True) or {}).get('csrf_token')
        if request.form:
            return request.form.get('csrf_token')
    except Exception:
        return None
    return None


@app.before_request
def _csrf_protect():
    if request.method in ('POST', 'PUT', 'DELETE', 'PATCH') and request.path.startswith('/api/'):
        # 登录接口不需要登录态，但必须带登录页生成的 CSRF token
        if request.path in ('/api/login',):
            if _csrf_from_request() != session.get('csrf_token'):
                return api_err('CSRF 校验失败', 403)
            return None
        if not session.get('user_id'):
            return api_err('未登录', 401)
        token = _csrf_from_request()
        if not token or token != session.get('csrf_token'):
            return api_err('CSRF 校验失败', 403)
    return None


# ---------------------------------------------------------------------------
# 页面路由
# ---------------------------------------------------------------------------
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        token = request.form.get('csrf_token') or ''
        if token != session.get('csrf_token'):
            flash('会话已过期，请重试', 'error')
            return render_template('login.html', csrf_token=csrf_token())
        row = get_db().execute(
            'SELECT * FROM users WHERE username=? AND is_active=1', (username,)
        ).fetchone()
        if row and check_password_hash(row['password_hash'], password):
            session.permanent = True
            session['user_id'] = row['id']
            session['username'] = row['username']
            session['role'] = row['role']
            session['csrf_token'] = secrets.token_urlsafe(32)
            db_exec('UPDATE users SET last_login=? WHERE id=?', (now_iso(), row['id']))
            audit('login_ok', f'用户 {row["username"]} 登录', row['username'])
            return redirect(url_for('index'))
        audit('login_fail', f'登录失败：{username}')
        flash('用户名或密码错误', 'error')
    return render_template('login.html', csrf_token=csrf_token())


@app.route('/logout', methods=['POST'])
@login_required
def logout():
    audit('logout', '用户退出登录')
    session.clear()
    return redirect(url_for('login'))


@app.route('/')
@login_required
def index():
    # tts_auto_speak 一并在首屏渲染：策略状态不依赖前端 JS 同步成功
    return render_template('dashboard.html', user=session.get('username'),
                           role=session.get('role'),
                           tts_auto_speak=bool_setting('tts_auto_speak', True))


# ---------------------------------------------------------------------------
# 状态 / 电压
# ---------------------------------------------------------------------------
def read_cpu_percent():
    global CPU_PREV
    try:
        with open('/proc/stat', 'r', encoding='utf-8') as f:
            parts = f.readline().split()
        nums = list(map(int, parts[1:]))
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
        total = sum(nums)
        with CPU_LOCK:
            prev = CPU_PREV
            CPU_PREV = {'idle': idle, 'total': total}
        d_total = total - prev['total']
        d_idle = idle - prev['idle']
        if d_total <= 0:
            return 0.0
        return round(100.0 * (d_total - d_idle) / d_total, 1)
    except Exception:
        return 0.0


def read_temperature():
    vals = []
    base = Path('/sys/class/thermal')
    if base.exists():
        for zone in sorted(base.glob('thermal_zone*')):
            try:
                name = (zone / 'type').read_text().strip()
                temp = int((zone / 'temp').read_text().strip()) / 1000.0
                if -40 <= temp <= 150:
                    vals.append({'name': name, 'celsius': round(temp, 1)})
            except Exception:
                continue
    return vals


def read_uptime():
    try:
        sec = float(Path('/proc/uptime').read_text().split()[0])
        return sec
    except Exception:
        return 0


def read_memory():
    info = {}
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            k, v = line.split(':', 1)
            info[k] = int(v.strip().split()[0]) * 1024
    except Exception:
        return {}
    total = info.get('MemTotal', 0)
    available = info.get('MemAvailable', 0)
    used = max(0, total - available)
    return {
        'total': total,
        'available': available,
        'used': used,
        'percent': round(100.0 * used / total, 1) if total else 0,
    }


def adc_channel_no(channel_key):
    """该通道占用的 SARADC 编号（0~7），可用设置 <key>_adc_channel 覆盖。"""
    ch = ADC_CHANNELS[channel_key]
    try:
        no = int(float(get_setting(f'{channel_key}_adc_channel', str(ch.get('default_channel', 0)))))
    except Exception:
        no = int(ch.get('default_channel', 0))
    return max(0, min(7, no))


def read_adc_raw(channel_key):
    path = ADC_DEVICE / f'in_voltage{adc_channel_no(channel_key)}_raw'
    try:
        raw = int(path.read_text().strip())
    except Exception:
        return None
    return raw


def v_per_lsb():
    """单 LSB 对应的引脚电压（V）：RK3588 SARADC 12bit 0~1.8V → 0.439453125 mV/LSB。"""
    scale_mv = adc_scale_mv()
    if scale_mv and scale_mv > 0:
        return scale_mv / 1000.0
    return ADC_FULL_SCALE_V / (1 << ADC_BITS)


def calc_pin_voltage(channel_key, raw, zero=None):
    """引脚电压 = (raw - 零点) × 每 LSB 电压。"""
    if raw is None:
        return None
    ch = ADC_CHANNELS[channel_key]
    if zero is None:
        zero = float(get_setting(f'{channel_key}_zero_raw', str(ch['default_zero'])))
    return max(0.0, (raw - zero)) * v_per_lsb()


def calc_voltage(channel_key, raw, zero=None, mult=None):
    """实际电压 = 引脚电压 × 倍率（倍率单位 V/引脚电压 = (R上+R下)/R下 = 1/分压比）。"""
    ch = ADC_CHANNELS[channel_key]
    if mult is None:
        mult = float(get_setting(f'{channel_key}_multiplier', str(ch['default_multiplier'])))
    pin_v = calc_pin_voltage(channel_key, raw, zero)
    if pin_v is None:
        return None
    return max(0.0, pin_v * mult)


def adc_scale_mv():
    try:
        return float(ADC_SCALE_RAW.read_text().strip())
    except Exception:
        return 0.439453125


def voltage_payload():
    scale_mv = adc_scale_mv()
    vlsb = scale_mv / 1000.0 if scale_mv else ADC_FULL_SCALE_V / (1 << ADC_BITS)
    out = {}
    for key, ch in ADC_CHANNELS.items():
        raw = read_adc_raw(key)
        pin_v = (raw * vlsb) if raw is not None else None
        mult = float(get_setting(f'{key}_multiplier', ch['default_multiplier']))
        upper = float(ch.get('divider_upper_ohm') or 0)
        lower = float(ch.get('divider_lower_ohm') or 0)
        ratio = (lower / (upper + lower)) if (upper + lower) > 0 else 0.0
        design = float(ch['default_multiplier'])
        out[key] = {
            'label': ch['label'],
            'adc_channel': adc_channel_no(key),
            'raw_file': f'in_voltage{adc_channel_no(key)}_raw',
            'raw': raw,
            'pin_voltage': round(pin_v, 4) if pin_v is not None else None,
            'voltage': round(calc_voltage(key, raw), 4) if raw is not None else None,
            'zero_raw': float(get_setting(f'{key}_zero_raw', ch['default_zero'])),
            'multiplier': mult,
            'unit': 'V',
            # —— 分压设计信息（供「校准/验算」展示）——
            'multiplier_unit': 'V/V（V/引脚电压）',
            'divider_desc': ch.get('divider_desc', ''),
            'divider_upper_ohm': upper,
            'divider_lower_ohm': lower,
            'divider_ratio': round(ratio, 6),
            'design_multiplier': design,
            'design_divider_ratio': round(1.0 / design, 6) if design else None,
            'full_scale': round(vlsb * (1 << ADC_BITS) * mult, 3),
            'design_full_scale': round(vlsb * (1 << ADC_BITS) * design, 3),
            'mv_per_lsb': round(vlsb * mult * 1000.0, 4),
            'v_per_lsb': round(vlsb, 9),
        }
    return out


# ---------------------------------------------------------------------------
# 能量统计：电池/光伏电压采样落库 + 全日时间轴
# ---------------------------------------------------------------------------
# 电压此前**完全没落库**（voltage_payload 按需读 ADC、算完即弃），所以「全天
# 时间轴」的前提是先攒数据；历史补不回来，图表从部署后开始积累。
def _db_direct():
    """后台线程/非请求路径用的直连。

    get_db() 把连接挂在 Flask 的 g 上，脱离请求上下文就会炸，所以采样线程
    必须自己开连接。
    """
    db = sqlite3.connect(str(DB_PATH), timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=NORMAL')
    return db


def _energy_sample_once():
    """采一次电压入库，返回是否写入。"""
    pw = voltage_payload()
    b = pw.get('battery') or {}
    p = pw.get('pv') or {}
    bv, pv = b.get('voltage'), p.get('voltage')
    if bv is None and pv is None:
        return False        # ADC 读不到就别写空行，免得时间轴被一堆空洞占满
    db = _db_direct()
    try:
        db.execute(
            'INSERT INTO voltage_readings(ts,ts_epoch,battery,pv,battery_raw,pv_raw)'
            ' VALUES(?,?,?,?,?,?)',
            (now_iso(), time.time(), bv, pv, b.get('raw'), p.get('raw')))
        db.commit()
        return True
    finally:
        db.close()


def _energy_purge(days):
    """按保留天数清理旧采样。"""
    cut = time.time() - energy_service.clamp_retention_days(days) * 86400.0
    db = _db_direct()
    try:
        cur = db.execute('DELETE FROM voltage_readings WHERE ts_epoch < ?', (cut,))
        db.commit()
        return cur.rowcount or 0
    finally:
        db.close()


def _energy_sampler():
    """后台采样线程：间隔与保留天数都是设置项，改完下一轮即生效。"""
    time.sleep(15)                 # 先让 ADC 与电压校准就绪
    last_purge = 0.0
    while True:
        try:
            if bool_setting('energy_log_enabled', True):
                _energy_sample_once()
                now = time.time()
                if now - last_purge > 3600:
                    last_purge = now
                    n = _energy_purge(_setting_direct('energy_retention_days', '365'))
                    if n:
                        print('[ENERGY] 清理 %d 条过期电压采样' % n, flush=True)
        except Exception as e:
            print('[ENERGY] 采样异常: %s: %s' % (type(e).__name__, e), flush=True)
        time.sleep(energy_service.clamp_sample_sec(
            _setting_direct('energy_sample_sec', '60')))


def _energy_day_rows(day):
    """取某天的原始采样（升序）。一天按 60s 采样也就 1440 行，直接全取。"""
    db = get_db()
    return [dict(r) for r in db.execute(
        'SELECT ts,ts_epoch,battery,pv,battery_raw,pv_raw FROM voltage_readings '
        'WHERE ts LIKE ? ORDER BY ts_epoch ASC LIMIT 20000',
        (str(day)[:10] + '%',)).fetchall()]


def _energy_days(limit=120):
    """有采样的日期列表（新→旧），供前端日期下拉。"""
    db = get_db()
    return [r['day'] for r in db.execute(
        "SELECT substr(ts,1,10) AS day FROM voltage_readings "
        "GROUP BY day ORDER BY day DESC LIMIT ?", (int(limit),)).fetchall()]


@app.route('/api/energy/day')
@login_required
def api_energy_day():
    """某天的电压时间轴 + 当日统计。"""
    day = (request.args.get('day') or '').strip()[:10] or \
        datetime.now().strftime('%Y-%m-%d')
    interval = energy_service.clamp_interval(request.args.get('interval') or 5)
    rows = _energy_day_rows(day)
    return api_ok(day=day, interval=interval,
                  points=energy_service.points_from_rows(rows, interval),
                  stats=energy_service.day_stats(rows),
                  days=_energy_days(),
                  logging={
                      'enabled': bool_setting('energy_log_enabled', True),
                      'sample_sec': energy_service.clamp_sample_sec(
                          _setting_direct('energy_sample_sec', '60')),
                      'retention_days': energy_service.clamp_retention_days(
                          _setting_direct('energy_retention_days', '365')),
                  })


@app.route('/api/energy/export')
@login_required
def api_energy_export():
    """整日序列导出 CSV。带 BOM，Excel 打开中文表头才不乱码。"""
    day = (request.args.get('day') or '').strip()[:10] or \
        datetime.now().strftime('%Y-%m-%d')
    body = '\ufeff' + energy_service.rows_to_csv(_energy_day_rows(day))
    return Response(
        body, mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition':
                 'attachment; filename=energy_%s.csv' % day})


@app.route('/api/status')
@login_required
def api_status():
    loadavg = os.getloadavg()
    return api_ok(
        time=now_iso(),
        cpu_percent=read_cpu_percent(),
        loadavg={'1m': loadavg[0], '5m': loadavg[1], '15m': loadavg[2]},
        memory=read_memory(),
        temperatures=read_temperature(),
        uptime_seconds=read_uptime(),
        voltages=voltage_payload(),
        llm={'provider': get_setting('llm_provider', 'local')},
        ptt=ptt_status(),
        busy=busy_status(),
        voice={'asr_enabled': _setting_direct('asr_enabled', '1') in ('1', 'true', 'True', 'on')},
    )


@app.route('/api/voltage/calibrate', methods=['POST'])
@login_required
def api_voltage_calibrate():
    data = request.get_json(silent=True) or {}
    updated = {}
    for key in ADC_CHANNELS:
        if f'{key}_zero_raw' in data:
            try:
                val = float(data[f'{key}_zero_raw'])
                if not (-10000 <= val <= 10000):
                    return api_err('零点数值超出范围')
                set_setting(f'{key}_zero_raw', val)
                updated[f'{key}_zero_raw'] = val
            except (TypeError, ValueError):
                return api_err('零点必须是数字')
        if f'{key}_adc_channel' in data:
            try:
                chv = int(float(data[f'{key}_adc_channel']))
                if not (0 <= chv <= 7):
                    return api_err('ADC 通道必须是 0~7')
                set_setting(f'{key}_adc_channel', chv)
                updated[f'{key}_adc_channel'] = chv
            except (TypeError, ValueError):
                return api_err('ADC 通道必须是数字')
        if f'{key}_multiplier' in data:
            try:
                val = float(data[f'{key}_multiplier'])
                if not (0 < val <= 200):
                    return api_err('倍率（V/引脚电压）必须大于 0 且不超过 200')
                set_setting(f'{key}_multiplier', val)
                updated[f'{key}_multiplier'] = val
            except (TypeError, ValueError):
                return api_err('倍率必须是数字')
    audit('voltage_calibrate', json.dumps(updated, ensure_ascii=False))
    return api_ok(updated=updated, voltages=voltage_payload())


@app.route('/api/settings', methods=['GET'])
@login_required
def api_settings_get():
    keys = [
        'llm_provider', 'local_base_url', 'local_model', 'local_api_key',
        'external_base_url', 'external_model', 'external_api_key',
        'record_auto_play', 'site_title',
        'tts_provider', 'tts_local_voice', 'tts_en_voice', 'tts_icao', 'tts_icao_voice', 'tts_auto_speak',
        'llm_system_prompt', 'llm_system_prompt_on', 'llm_prompt_vars',
        'agent_enabled', 'agent_max_iters', 'agent_tools',
        'assist_enabled',
        'assist_wake_words',
        'assist_wake_fuzzy',
        'assist_channel',
        'assist_dbfs_open',
        'assist_dbfs_close',
        'assist_preroll_ms',
        'assist_silence_ms',
        'assist_min_speech_ms',
        'assist_max_utterance',
        'assist_followup_seconds',
        'assist_ack_reply',
        'assist_use_vad',
        'assist_max_tx_seconds',
        'assist_min_gap_seconds',
        'assist_busy_wait_seconds',
        'assist_tx_guard_ms',
        'assist_quiet_hours',
        'assist_test_mode',
        'assist_max_reply_chars',
        'assist_max_tokens',
        'assist_history_turns',
        'assist_max_input_chars',
        'assist_temperature',
        'assist_use_tools',
        'assist_agent_iters',
        'assist_keep_llm_warm',
        'assist_llm_wait',
        'assist_prompt_suffix',
        'assist_voice',
        'assist_retention_days',
        'vlog_enabled', 'vlog_dir', 'vlog_channel', 'vlog_pre_roll', 'vlog_post_roll',
        'vlog_min_seconds', 'vlog_max_seconds', 'vlog_silence_dbfs',
        'vlog_asr_enabled', 'vlog_vad_enabled', 'vlog_enhance', 'vlog_keep_transient',
        'vlog_retention_days', 'vlog_retention_mb',
        'vlog_summary_enabled', 'vlog_summary_time', 'vlog_summary_provider',
        'vlog_llm_on_demand', 'vlog_llm_idle_unload',
        'vlog_callsign_whitelist', 'vlog_callsign_max_dist',
        'aprs_enabled', 'aprs_mycall', 'aprs_ssid', 'aprs_dest', 'aprs_path',
        'aprs_lat', 'aprs_lon', 'aprs_alt_m', 'aprs_pos_source', 'aprs_gps_port',
        'aprs_gps_baud', 'aprs_symbol_table', 'aprs_symbol_code', 'aprs_comment',
        'aprs_pos_ambiguity', 'aprs_channel',
        'aprs_beacon_enabled', 'aprs_beacon_interval',
        'aprs_weather_enabled', 'aprs_weather_interval',
        'aprs_telemetry_enabled', 'aprs_telemetry_interval',
        'aprs_status_enabled', 'aprs_status_interval', 'aprs_status_text',
        'aprs_jitter', 'aprs_carrier_sense', 'aprs_defer_max',
        'aprs_defer_jitter', 'aprs_min_gap',
        'aprs_burst_threshold', 'aprs_burst_min_ms', 'aprs_phase_trials',
        'aprs_dedup_window', 'aprs_telemetry_map', 'aprs_retention_days',
        'aprs_map_provider', 'aprs_map_tk', 'aprs_map_tk_browser',
        'aprs_map_layers', 'aprs_map_cache_mb', 'aprs_track_points',
        'reboot_enabled', 'reboot_times', 'reboot_notice_sec', 'reboot_text',
        'energy_log_enabled', 'energy_sample_sec', 'energy_retention_days',
    ]
    out = {k: get_setting(k) for k in keys}
    out['local_api_key_set'] = bool(out.get('local_api_key'))
    out['external_api_key_set'] = bool(out.get('external_api_key'))
    out['local_api_key'] = '***' if out.get('local_api_key') else ''
    out['external_api_key'] = '***' if out.get('external_api_key') else ''
    return api_ok(settings=out)


@app.route('/api/settings', methods=['POST'])
@login_required
@admin_required
def api_settings_set():
    data = request.get_json(silent=True) or {}
    _bool_caster = lambda v: '1' if str(v) in ('1', 'true', 'True', 'on') else '0'
    whitelist = {
        'llm_provider': lambda v: v if v in ('local', 'external') else None,
        'local_base_url': lambda v: str(v).strip(),
        'local_model': lambda v: str(v).strip(),
        'local_api_key': lambda v: str(v),
        'external_base_url': lambda v: str(v).strip(),
        'external_model': lambda v: str(v).strip(),
        'external_api_key': lambda v: str(v),
        'record_auto_play': lambda v: '1' if str(v) in ('1', 'true', 'True', 'on') else '0',
        # 能量统计（电压采样）
        'energy_log_enabled': _bool_caster,
        'energy_sample_sec': lambda v: str(energy_service.clamp_sample_sec(v)),
        'energy_retention_days': lambda v: str(energy_service.clamp_retention_days(v)),
        'site_title': lambda v: str(v).strip()[:80],
        'tts_provider': lambda v: str(v).strip(),
        'tts_local_voice': lambda v: str(v).strip()[:80],
        'tts_en_voice': lambda v: str(v).strip()[:80],
        'tts_icao': lambda v: '1' if str(v) in ('1', 'true', 'True', 'on') else '0',
        'tts_icao_voice': lambda v: str(v).strip()[:80],
        'tts_auto_speak': lambda v: '1' if str(v) in ('1', 'true', 'True', 'on') else '0',
        # 定时重启：只接受 HH:MM，去重并保留顺序；播报提前量与文本都做钳制
        'reboot_enabled': _bool_caster,
        'reboot_times': lambda v: ','.join(
            x.strip() for x in re.split(r'[,;，；\s]+', str(v))
            if re.match(r'^\d{1,2}:\d{2}$', x.strip())
            and 0 <= int(x.split(':')[0]) <= 23 and 0 <= int(x.split(':')[1]) <= 59)[:120],
        'reboot_notice_sec': lambda v: str(max(0, min(600, int(float(v))))),
        'reboot_text': lambda v: (str(v).strip()[:80] or '中继台即将重启，请稍候。'),
        'tts_provider': lambda v: 'local',   # 外部 TTS 已下线，强制 local
        'llm_system_prompt': lambda v: str(v)[:4000],
        'llm_system_prompt_on': lambda v: '1' if str(v) in ('1', 'true', 'True', 'on') else '0',
        'llm_prompt_vars': lambda v: '1' if str(v) in ('1', 'true', 'True', 'on') else '0',
        'agent_enabled': lambda v: '1' if str(v) in ('1', 'true', 'True', 'on') else '0',
        'agent_max_iters': lambda v: str(max(1, min(5, int(float(v))))),
        'agent_tools': lambda v: ','.join(
            [x.strip() for x in re.split(r'[,;\s]+', str(v)) if x.strip()][:20]),
        # 中继语音助手
        'assist_enabled': _bool_caster,
        'assist_use_vad': _bool_caster,
        'assist_enhance': _bool_caster,
        'assist_wake_fuzzy': _bool_caster,
        'assist_use_tools': _bool_caster,
        'assist_keep_llm_warm': _bool_caster,
        'assist_test_mode': _bool_caster,
        'assist_wake_words': lambda v: ','.join(
            [x.strip() for x in re.split(r'[,;\u3001\s]+', str(v)) if x.strip()][:8]
            ) or '\u667a\u80fd\u4e2d\u7ee7,\u4e2d\u7ee7\u53f0',
        'assist_channel': lambda v: v if v in ('left', 'right', 'mix') else 'left',
        'assist_dbfs_open': lambda v: str(round(max(-80.0, min(-5.0, float(v))), 1)),
        'assist_dbfs_close': lambda v: str(round(max(-85.0, min(-5.0, float(v))), 1)),
        'assist_preroll_ms': lambda v: str(int(max(0, min(3000, int(float(v)))))),
        'assist_silence_ms': lambda v: str(int(max(150, min(5000, int(float(v)))))),
        'assist_min_speech_ms': lambda v: str(int(max(100, min(5000, int(float(v)))))),
        'assist_max_utterance': lambda v: str(round(max(2.0, min(120.0, float(v))), 1)),
        'assist_followup_seconds': lambda v: str(int(max(0, min(600, int(float(v)))))),
        'assist_ack_reply': lambda v: str(v).strip()[:40] or '\u8bf7\u8bb2',
        'assist_max_tx_seconds': lambda v: str(int(max(3, min(300, int(float(v)))))),
        'assist_min_gap_seconds': lambda v: str(int(max(0, min(600, int(float(v)))))),
        'assist_busy_wait_seconds': lambda v: str(int(max(1, min(120, int(float(v)))))),
        'assist_tx_guard_ms': lambda v: str(int(max(0, min(5000, int(float(v)))))),
        'assist_quiet_hours': lambda v: str(v).strip()[:80],
        'assist_max_reply_chars': lambda v: str(int(max(10, min(300, int(float(v)))))),
        'assist_max_tokens': lambda v: str(int(max(32, min(512, int(float(v)))))),
        'assist_history_turns': lambda v: str(int(max(0, min(12, int(float(v)))))),
        'assist_max_input_chars': lambda v: str(int(max(400, min(8000, int(float(v)))))),
        'assist_temperature': lambda v: str(round(max(0.0, min(1.5, float(v))), 2)),
        'assist_agent_iters': lambda v: str(int(max(0, min(4, int(float(v)))))),
        'assist_llm_wait': lambda v: str(int(max(5, min(120, int(float(v)))))),
        'assist_prompt_suffix': lambda v: str(v)[:2000],
        'assist_voice': lambda v: str(v).strip()[:80],
        'assist_retention_days': lambda v: str(int(max(1, min(3650, int(float(v)))))),
        'assist_debug_keep': lambda v: str(int(max(0, min(200, int(float(v)))))),
        # 中继语音日志
        'vlog_dir': lambda v: str(v).strip()[:120] or '/opt/ai/relay_voice',
        'vlog_channel': lambda v: v if v in ('left', 'right', 'mix') else 'left',
        'vlog_pre_roll': lambda v: str(round(max(0.0, min(30.0, float(v))), 1)),
        'vlog_post_roll': lambda v: str(round(max(0.0, min(30.0, float(v))), 1)),
        'vlog_min_seconds': lambda v: str(round(max(0.0, min(60.0, float(v))), 1)),
        'vlog_max_seconds': lambda v: str(int(max(10, min(3600, int(float(v)))))),
        'vlog_silence_dbfs': lambda v: str(round(max(-90.0, min(-10.0, float(v))), 1)),
        'vlog_retention_days': lambda v: str(max(0, min(3650, int(float(v))))),
        'vlog_retention_mb': lambda v: str(max(100, min(1000000, int(float(v))))),
        'vlog_summary_time': lambda v: (str(v).strip()
            if re.fullmatch(r'\d{1,2}:\d{2}', str(v).strip()) else '23:30'),
        'vlog_summary_provider': lambda v: v if v in ('auto', 'local', 'external') else 'auto',
        'vlog_llm_idle_unload': lambda v: str(max(60, min(3600, int(float(v))))),
        'vlog_callsign_whitelist': lambda v: ','.join(
            [x.strip().upper() for x in re.split(r'[,;\s]+', str(v)) if x.strip()][:50])[:400],
        'vlog_callsign_max_dist': lambda v: str(max(0, min(3, int(float(v))))),
        'vlog_enabled': _bool_caster,
        'vlog_asr_enabled': _bool_caster,
        'vlog_vad_enabled': _bool_caster,
        'vlog_enhance': _bool_caster,
        'vlog_keep_transient': _bool_caster,
        'vlog_summary_enabled': _bool_caster,
        'vlog_llm_on_demand': _bool_caster,
        # —— APRS ——
        'aprs_mycall': lambda v: (''.join(ch for ch in str(v).upper()
            if ch.isalnum()))[:6] or 'BI7KHI',
        'aprs_ssid': lambda v: str(max(0, min(15, int(float(v))))),
        'aprs_dest': lambda v: (str(v).upper().strip() or 'APRS')[:6],
        'aprs_path': lambda v: ','.join(
            [x.strip().upper() for x in str(v).split(',') if x.strip()][:8])[:80],
        'aprs_lat': lambda v: str(round(max(-90.0, min(90.0, float(v))), 6)),
        'aprs_lon': lambda v: str(round(max(-180.0, min(180.0, float(v))), 6)),
        'aprs_alt_m': lambda v: ('' if str(v).strip() == ''
            else str(round(max(-500.0, min(9000.0, float(v))), 1))),
        'aprs_pos_source': lambda v: v if v in ('manual', 'nmea') else 'manual',
        'aprs_gps_port': lambda v: str(v).strip()[:60],
        'aprs_gps_baud': lambda v: str(max(1200, min(921600, int(float(v))))),
        'aprs_symbol_table': lambda v: v if v in ('/', '\\') else '/',
        'aprs_symbol_code': lambda v: (str(v)[:1] or '-'),
        'aprs_comment': lambda v: str(v).strip()[:60],
        'aprs_pos_ambiguity': lambda v: str(max(0, min(4, int(float(v))))),
        'aprs_channel': lambda v: v if v in ('left', 'right', 'mix') else 'left',
        'aprs_beacon_interval': lambda v: str(max(60, min(86400, int(float(v))))),
        'aprs_weather_interval': lambda v: str(max(60, min(86400, int(float(v))))),
        'aprs_telemetry_interval': lambda v: str(max(60, min(86400, int(float(v))))),
        'aprs_status_interval': lambda v: str(max(60, min(86400, int(float(v))))),
        'aprs_status_text': lambda v: str(v).strip()[:60],
        'aprs_jitter': lambda v: str(max(0, min(600, int(float(v))))),
        'aprs_defer_max': lambda v: str(max(0, min(3600, int(float(v))))),
        'aprs_defer_jitter': lambda v: str(max(0, min(60, int(float(v))))),
        'aprs_min_gap': lambda v: str(max(0, min(600, int(float(v))))),
        'aprs_burst_threshold': lambda v: str(round(max(0.05, min(0.9, float(v))), 3)),
        'aprs_burst_min_ms': lambda v: str(max(20, min(2000, int(float(v))))),
        'aprs_phase_trials': lambda v: str(max(8, min(256, int(float(v))))),
        'aprs_dedup_window': lambda v: str(max(0, min(600, int(float(v))))),
        'aprs_retention_days': lambda v: str(max(0, min(3650, int(float(v))))),
        'aprs_track_points': lambda v: str(max(10, min(5000, int(float(v))))),
        'aprs_map_cache_mb': lambda v: str(max(0, min(20480, int(float(v))))),
        'aprs_map_provider': lambda v: v if v in ('tianditu', 'none') else 'tianditu',
        'aprs_map_tk': lambda v: str(v).strip()[:64],
        'aprs_map_tk_browser': lambda v: str(v).strip()[:64],
        'aprs_map_layers': lambda v: ','.join(
            [x.strip() for x in str(v).split(',') if x.strip()
             and x.strip() in ('img', 'vec', 'ter', 'cva', 'cia', 'cta')][:4]),
        'aprs_telemetry_map': lambda v: (str(v)[:2000] if str(v).strip().startswith('{')
            else aprs_service.DEFAULTS['aprs_telemetry_map']),
        'aprs_enabled': _bool_caster,
        'aprs_beacon_enabled': _bool_caster,
        'aprs_weather_enabled': _bool_caster,
        'aprs_telemetry_enabled': _bool_caster,
        'aprs_status_enabled': _bool_caster,
        'aprs_carrier_sense': _bool_caster,
    }
    changed = {}
    for k, caster in whitelist.items():
        if k not in data:
            continue
        try:
            val = caster(data[k])
        except Exception:
            continue
        if val is None:
            continue
        if k.endswith('_api_key') and str(val) == '***':
            continue  # 保持原值
        set_setting(k, str(val))
        changed[k] = '***' if k.endswith('_api_key') and val else val
    audit('settings_update', json.dumps(changed, ensure_ascii=False))
    try:
        voice_service_instance.invalidate()
    except Exception:
        pass
    try:
        aprs_service_instance.invalidate()
    except Exception:
        pass
    try:
        assistant_service_instance.invalidate()
    except Exception:
        pass
    return api_ok(changed=changed)


# ---------------------------------------------------------------------------
# 用户管理
# ---------------------------------------------------------------------------
@app.route('/api/users', methods=['GET'])
@login_required
@admin_required
def api_users_list():
    rows = get_db().execute(
        'SELECT id,username,role,is_active,created_at,last_login FROM users ORDER BY id'
    ).fetchall()
    return api_ok(users=[dict(r) for r in rows])


@app.route('/api/users', methods=['POST'])
@login_required
@admin_required
def api_users_add():
    data = request.get_json(silent=True) or {}
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    role = data.get('role', 'user')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{2,32}', username):
        return api_err('用户名只能包含字母数字._-，长度2-32')
    if len(password) < 8:
        return api_err('密码至少 8 位')
    if role not in ('admin', 'user'):
        return api_err('角色不合法')
    try:
        db_exec('INSERT INTO users(username,password_hash,role,is_active,created_at) VALUES(?,?,?,?,?)',
                (username, generate_password_hash(password), role, 1, now_iso()))
    except sqlite3.IntegrityError:
        return api_err('用户名已存在')
    audit('user_add', f'新增用户 {username}')
    return api_ok()


@app.route('/api/users/<int:uid>', methods=['DELETE'])
@login_required
@admin_required
def api_users_delete(uid):
    if uid == session.get('user_id'):
        return api_err('不能删除当前登录用户')
    row = get_db().execute('SELECT username,role FROM users WHERE id=?', (uid,)).fetchone()
    if not row:
        return api_err('用户不存在', 404)
    if row['username'].lower() == 'admin':
        return api_err('默认管理员不可删除')
    db_exec('DELETE FROM users WHERE id=?', (uid,))
    audit('user_delete', f'删除用户 {row["username"]}')
    return api_ok()


@app.route('/api/users/<int:uid>/password', methods=['POST'])
@login_required
@admin_required
def api_users_reset_password(uid):
    data = request.get_json(silent=True) or {}
    password = data.get('password') or ''
    if len(password) < 8:
        return api_err('密码至少 8 位')
    db_exec('UPDATE users SET password_hash=? WHERE id=?', (generate_password_hash(password), uid))
    audit('user_reset_password', f'重置用户 ID {uid} 密码')
    return api_ok()


@app.route('/api/me/password', methods=['POST'])
@login_required
def api_me_password():
    data = request.get_json(silent=True) or {}
    old = data.get('old_password') or ''
    new = data.get('new_password') or ''
    if len(new) < 8:
        return api_err('新密码至少 8 位')
    row = get_db().execute('SELECT password_hash FROM users WHERE id=?', (session['user_id'],)).fetchone()
    if not row or not check_password_hash(row['password_hash'], old):
        return api_err('原密码错误')
    db_exec('UPDATE users SET password_hash=? WHERE id=?', (generate_password_hash(new), session['user_id']))
    audit('change_password', '修改本人密码')
    return api_ok()


# ---------------------------------------------------------------------------
# LLM 对话
# ---------------------------------------------------------------------------
def openai_chat_url(base_url):
    base = (base_url or '').strip().rstrip('/')
    if not base:
        return ''
    if base.endswith('/chat/completions'):
        return base
    if base.endswith('/v1'):
        return base + '/chat/completions'
    return base + '/v1/chat/completions'


def provider_config(provider=None):
    provider = provider or get_setting('llm_provider', 'local')
    if provider not in ('local', 'external'):
        provider = 'local'
    if provider == 'local':
        base = get_setting('local_base_url', 'http://127.0.0.1:8001/v1')
        model = get_setting('local_model', 'qwen2.5-1.5b')
        key = get_setting('local_api_key', '')
    else:
        base = get_setting('external_base_url', '')
        model = get_setting('external_model', 'deepseek-chat')
        key = get_setting('external_api_key', '')
    return {'provider': provider, 'base_url': base, 'model': model, 'api_key': key,
            'url': openai_chat_url(base)}


def _sse(obj):
    """统一的 SSE 数据帧。"""
    return 'data: ' + json.dumps(obj, ensure_ascii=False) + '\n\n'


def _prompt_vars():
    """提示词注入可用的实时变量（{battery} {pv} {cpu_temp} {wind} …）。"""
    v = {
        'time': datetime.now().strftime('%H:%M:%S'),
        'date': datetime.now().strftime('%Y-%m-%d'),
        'weekday': '一二三四五六日'[datetime.now().weekday()],
        'site': _setting_direct('site_title', 'ELF2 智能中继控制中心'),
    }
    try:
        pw = voltage_payload()
        for k in ('battery', 'pv'):
            item = pw.get(k) or {}
            v[k] = item.get('voltage')
            v[k + '_raw'] = item.get('raw')
    except Exception:
        pass
    try:
        temps = read_temperature()
        cpu = None
        for t in temps:
            nm = str(t.get('name') or '')
            if 'cpu' in nm or 'soc' in nm:
                cpu = t.get('celsius')
                break
        v['cpu_temp'] = cpu if cpu is not None else (temps[0]['celsius'] if temps else None)
    except Exception:
        pass
    try:
        st = weather_service_instance.realtime()
        v['wind'] = st.get('last_speed')
    except Exception:
        pass
    try:
        day = datetime.now().strftime('%Y-%m-%d')
        v['rain_today'] = weather_service_instance.rain_stats(day).get('total_mm')
    except Exception:
        pass
    try:
        _wst = weather_service_instance.realtime()
        v['temperature'] = _wst.get('th_temperature')
        v['humidity'] = _wst.get('th_humidity')
    except Exception:
        pass
    return v


def _expand_vars(text):
    """把 {var} 换成实时值；未知变量原样保留。"""
    if not text:
        return ''
    vals = _prompt_vars()

    def rep(m):
        val = vals.get(m.group(1))
        return m.group(0) if val is None else str(val)

    return re.sub(r'\{([a-z_][a-z0-9_]*)\}', rep, text)


def _agent_enabled():
    """后台设置里启用的工具名列表；空 = 全部启用。"""
    raw = _setting_direct('agent_tools', '') or ''
    names = [x.strip() for x in re.split(r'[,;\s]+', raw) if x.strip()]
    valid = set(agent_service.tool_index().keys())
    names = [n for n in names if n in valid]
    return names or None


def _injected_system_prompt(agent=False, enabled_tools=None):
    """拼出要注入的 system prompt（含变量展开；agent 模式再附工具清单）。"""
    base = ''
    if _setting_direct('llm_system_prompt_on', '1') in ('1', 'true', 'True', 'on'):
        base = (_setting_direct('llm_system_prompt', '') or '').strip()
        if base and _setting_direct('llm_prompt_vars', '1') in ('1', 'true', 'True', 'on'):
            try:
                base = _expand_vars(base)
            except Exception:
                pass
    if not agent:
        return base
    enabled = enabled_tools if enabled_tools is not None else _agent_enabled()
    return agent_service.build_agent_prompt(base, enabled)


def _llm_stat_record(mode, cfg, model, snap, ok=True, note='', iters=0, tools=''):
    """把一次生成速率写入 llm_stats（页面统计用）。"""
    try:
        db_exec('INSERT INTO llm_stats(ts,provider,model,mode,ttft_ms,tokens,elapsed_ms,'
                'tok_per_s,iters,tools,ok,note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (now_iso(), (cfg or {}).get('provider', ''), model or '', mode,
                 int(snap.get('ttft_ms') or 0), int(snap.get('tokens') or 0),
                 int(snap.get('elapsed_ms') or 0), float(snap.get('tok_per_s') or 0),
                 int(iters or 0), tools or '', 1 if ok else 0, note or ''))
    except Exception:
        pass
    try:
        print('[LLM] %s %s tokens=%s ttft=%sms %.2f tok/s iters=%s tools=%s' % (
            mode, model, snap.get('tokens'), snap.get('ttft_ms'),
            float(snap.get('tok_per_s') or 0), iters, tools or '-'), flush=True)
    except Exception:
        pass


def _agent_ctx():
    """技能（工具）实现：返回**紧凑**结果。

    注意：板端 RKLLM 在提示词过长（>约 400 字）时会直接空输出，
    因此工具结果必须精简——只给模型需要的数值，不要整段 JSON。
    """
    def get_weather():
        st = weather_service_instance.realtime()
        return {'wind_ms': st.get('last_speed'), 'running': bool(st.get('running')),
                'error': (st.get('last_error') or '')[:40]}

    def get_rain(hours=1):
        day = datetime.now().strftime('%Y-%m-%d')
        try:
            s = weather_service_instance.rain_stats(day)
        except Exception:
            s = {}
        try:
            recent = weather_service_instance.rain_recent_hour()
        except Exception:
            recent = None
        return {'today_mm': s.get('total_mm'), 'recent_hour_mm': recent}

    def get_power():
        pw = voltage_payload()
        out = {}
        for k in ('battery', 'pv'):
            it = pw.get(k) or {}
            out[k + '_v'] = it.get('voltage')
            out[k + '_raw'] = it.get('raw')
        return out

    def get_system():
        du = shutil.disk_usage('/')
        temps = read_temperature() or []
        cpu = None
        for item in temps:
            nm = str(item.get('name') or '')
            if 'cpu' in nm or 'soc' in nm:
                cpu = item.get('celsius')
                break
        if cpu is None and temps:
            cpu = temps[0].get('celsius')
        mem = read_memory() or {}
        return {'cpu_temp_c': cpu, 'cpu_percent': read_cpu_percent(),
                'load1': round(os.getloadavg()[0], 2),
                'mem_percent': round(mem.get('used', 0) / max(1, mem.get('total', 1)) * 100, 1),
                'disk_free_gb': round(du.free / 1e9, 1),
                'uptime_h': round(read_uptime() / 3600.0, 1)}

    def get_radio():
        st = ptt_status()
        return {'ptt_high': bool(st.get('high')), 'gpio': st.get('gpio'),
                'hold': st.get('hold_count')}

    def get_camera():
        recs = _camera_list_recordings()
        try:
            st = camera_service.camera_service.status()
        except Exception:
            st = {}
        return {'video_devices': len([str(x) for x in sorted(Path('/dev').glob('video*'))]),
                'recordings': len(recs), 'running': bool(st.get('running'))}

    def get_time():
        now = datetime.now()
        return {'datetime': now.strftime('%Y-%m-%d %H:%M:%S'),
                'weekday': '星期' + '一二三四五六日'[now.weekday()]}

    def get_home_position():
        """本站自身位置。坐标没配就问不出来——必须返回一句人话，别给空字典。"""
        try:
            p = aprs_service_instance.home_position()
        except Exception as e:
            return {'error': '%s: %s' % (type(e).__name__, e)}
        if p.get('lat') is None and p.get('lon') is None:
            return {'error': '本站坐标未配置（设置 → APRS 位置来源）'}
        return p

    def get_station_position(call=''):
        """按呼号查最后位置；呼号留空 = 最近听到的那个台。

        只查得到**收到过 APRS 信标**的台：语音里报的呼号如果从没发过包，
        这里就是不认识——要如实说，不能让模型编一个坐标出来。
        """
        want = str(call or '').strip()
        try:
            r = aprs_service_instance.station_position(want)
        except Exception as e:
            return {'error': '%s: %s' % (type(e).__name__, e)}
        if not r:
            return {'error': ('没收到过 %s 的位置信标' % want) if want
                    else '本机还没收到过任何带位置的信标'}
        return r

    def get_nearby_stations(km=50, limit=5):
        """附近电台排行。结果条数要压住——板端模型看不了长列表。"""
        try:
            items = aprs_service_instance.nearby_stations(km=km, limit=limit)
        except Exception as e:
            return {'error': '%s: %s' % (type(e).__name__, e)}
        home = {}
        try:
            home = aprs_service_instance.home_position()
        except Exception:
            pass
        if not items:
            return {'home_valid': bool(home.get('valid')),
                    'count': 0, 'stations': [],
                    'note': '最近 %s 小时内没有收到带位置的 APRS 信标' % 24}
        # 最多 5 条进提示词；每条只留模型真正要说的字段
        keep = ('call', 'km', 'dir', 'age_min', 'speed_kt')
        return {'home_valid': bool(home.get('valid')),
                'count': len(items),
                'stations': [{k: it[k] for k in keep if k in it}
                             for it in items[:5]]}

    def speak(text=''):
        body = str(text or '').strip()[:200]
        if not body:
            return {'error': 'text 为空'}
        wav = tts_service.synthesize_multilingual(
            body, get_setting('tts_local_voice', 'zh_CN-huayan-medium'),
            en_voice=(get_setting('tts_en_voice', '') or '').strip() or None,
            icao=bool_setting('tts_icao', True),
            icao_voice=(get_setting('tts_icao_voice', '') or '').strip() or None)
        name = 'tts_%s_agent.wav' % datetime.now().strftime('%Y%m%d_%H%M%S')
        out = RECORDINGS_DIR / name
        shutil.copyfile(wav, out)
        play_audio_async(out, ptt=True)
        return {'spoken': body[:30], 'file': name}

    return {'get_weather': get_weather, 'get_rain': get_rain, 'get_power': get_power,
            'get_system': get_system, 'get_radio': get_radio, 'get_camera': get_camera,
            'get_time': get_time, 'get_home_position': get_home_position,
            'get_station_position': get_station_position,
            'get_nearby_stations': get_nearby_stations, 'speak': speak}


def _llm_headers(key):
    headers = {'Content-Type': 'application/json'}
    if key:
        headers['Authorization'] = f'Bearer {key}'
    return headers


@app.route('/api/chat/providers')
@login_required
def api_chat_providers():
    out = {}
    for p in ('local', 'external'):
        cfg = provider_config(p)
        out[p] = {
            'provider': p,
            'base_url': cfg['base_url'],
            'model': cfg['model'],
            'configured': bool(cfg['base_url']),
            'api_key_set': bool(cfg['api_key']),
            'url': cfg['url'],
        }
    return api_ok(providers=out, current=get_setting('llm_provider', 'local'))


@app.route('/api/chat', methods=['POST'])
@login_required
def api_chat():
    t_req_start = time.time()
    data = request.get_json(silent=True) or {}
    messages = data.get('messages') or []
    if not isinstance(messages, list) or not messages:
        return api_err('messages 不能为空')
    provider = data.get('provider') or get_setting('llm_provider', 'local')
    cfg = provider_config(provider)
    if not cfg['url']:
        return api_err('LLM API 地址未配置')
    if cfg['provider'] == 'local':
        _llm_ok, _llm_msg = voice_service_instance.ensure_llm_ready()
        if not _llm_ok:
            return api_err('本地 LLM 未就绪：%s' % _llm_msg)
    model = data.get('model') or cfg['model']
    stream = bool(data.get('stream', False))
    # 限制上下文，避免失控
    clean = []
    for m in messages[-20:]:
        if not isinstance(m, dict) or 'role' not in m or 'content' not in m:
            continue
        clean.append({'role': str(m['role']), 'content': str(m['content'])})
    if not clean:
        return api_err('messages 无效')
    # 提示词注入：客户端未指定 system 时，注入后台配置的系统提示词
    #（可含 {battery}/{pv}/{cpu_temp}/{wind}/{time} 等实时变量，由 _expand_vars 展开）
    inject = data.get('system')
    if inject is None:
        inject = _injected_system_prompt(agent=False)
    if inject:
        inject = str(inject)[:4000]
        if any(m.get('role') == 'system' for m in clean):
            pass                                     # 客户端自带 system，不重复注入
        elif cfg['provider'] == 'local':
            # 板端 RKLLM 会忽略 system 角色：把设定并入最后一条用户消息
            for _i in range(len(clean) - 1, -1, -1):
                if clean[_i].get('role') == 'user':
                    clean[_i] = {'role': 'user', 'content':
                                 '【系统设定】\n' + inject + '\n\n【用户问题】\n' +
                                 str(clean[_i].get('content') or '')}
                    break
        else:
            clean.insert(0, {'role': 'system', 'content': inject})
    payload = {
        'model': model,
        'messages': clean,
        'stream': stream,
        'temperature': float(data.get('temperature', 0.7)),
        'max_tokens': int(data.get('max_tokens', 1024)),
    }
    try:
        if stream:
            def generate():
                # 边转发边统计：首 token 延迟(TTFT)、token 数、tokens/s
                meter = agent_service.RateMeter()
                ok, note = True, ''
                try:
                    r = requests.post(cfg['url'], json=payload,
                                      headers=_llm_headers(cfg['api_key']),
                                      stream=True, timeout=(5, 300))
                    if r.status_code != 200:
                        ok, note = False, f'HTTP {r.status_code}'
                        yield _sse({'error': f'LLM HTTP {r.status_code}: {r.text[:200]}'})
                        yield 'data: [DONE]\n\n'
                        return
                    buf = ''
                    for chunk in r.iter_content(chunk_size=None):
                        if not chunk:
                            continue
                        try:
                            buf += chunk.decode('utf-8', 'ignore')
                            while '\n' in buf:
                                line, buf = buf.split('\n', 1)
                                line = line.strip()
                                if not line.startswith('data:'):
                                    continue
                                pl = line[5:].strip()
                                if not pl or pl == '[DONE]':
                                    continue
                                try:
                                    obj = json.loads(pl)
                                except Exception:
                                    continue
                                d = ((obj.get('choices') or [{}])[0].get('delta') or {}).get('content')
                                if d:
                                    meter.add(d)
                        except Exception:
                            pass
                        yield chunk
                except Exception as e:
                    ok, note = False, type(e).__name__
                    yield _sse({'error': f'LLM 请求失败: {e}'})
                snap = meter.snapshot()
                _llm_stat_record('chat', cfg, model, snap, ok=ok, note=note)
                yield _sse({'usage': snap})
                yield 'data: [DONE]\n\n'
            return Response(stream_with_context(generate()), mimetype='text/event-stream',
                            headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

        r = requests.post(cfg['url'], json=payload,
                          headers=_llm_headers(cfg['api_key']), timeout=(5, 300))
        if r.status_code != 200:
            return api_err(f'LLM HTTP {r.status_code}: {r.text[:300]}', 502)
        result = r.json()
        content = ''
        try:
            content = result['choices'][0]['message']['content']
        except Exception:
            pass
        try:
            db_exec('INSERT INTO chat_logs(ts,username,provider,model,role,content) VALUES(?,?,?,?,?,?)',
                    (now_iso(), session.get('username'), cfg['provider'], model, 'user',
                     json.dumps(clean, ensure_ascii=False)[:4000]))
            db_exec('INSERT INTO chat_logs(ts,username,provider,model,role,content) VALUES(?,?,?,?,?,?)',
                    (now_iso(), session.get('username'), cfg['provider'], model, 'assistant', content[:8000]))
        except Exception:
            pass
        meter = agent_service.RateMeter()
        meter.t0 = t_req_start          # 非流式：以请求开始计时
        meter.ttft = 0.0                # 无首 token 概念，避免 gen≈0 导致 tok/s 爆表
        meter.add(content)
        snap = meter.snapshot()
        _llm_stat_record('chat', cfg, model, snap, ok=True, note='non-stream')
        return api_ok(result=result, content=content, provider=cfg['provider'], model=model,
                      usage=snap)
    except requests.exceptions.Timeout:
        return api_err('LLM 请求超时', 504)
    except Exception as e:
        return api_err(f'LLM 调用失败: {e}', 502)


# ---------------------------------------------------------------------------
# 端侧 LLM：技能/工具（Agent）+ 生成速率监测
# ---------------------------------------------------------------------------
@app.route('/api/llm/stats')
@login_required
def api_llm_stats():
    """生成速率统计：最近 N 次 + 今日汇总。"""
    limit = max(1, min(200, int(float(request.args.get('limit', 20) or 20))))
    rows = [dict(r) for r in get_db().execute(
        'SELECT * FROM llm_stats ORDER BY id DESC LIMIT ?', (limit,)).fetchall()]
    day = datetime.now().strftime('%Y-%m-%d')
    agg = get_db().execute(
        'SELECT COUNT(*) AS runs, AVG(tok_per_s) AS avg_tps, MAX(tok_per_s) AS max_tps,'
        ' AVG(ttft_ms) AS avg_ttft, SUM(tokens) AS tokens, SUM(elapsed_ms) AS elapsed_ms'
        ' FROM llm_stats WHERE ts LIKE ? AND tok_per_s > 0 AND tok_per_s < 200',
        (day + '%',)).fetchone()
    return api_ok(today={k: (round(agg[k], 2) if isinstance(agg[k], float) else agg[k])
                         for k in agg.keys()},
                  recent=rows)


@app.route('/api/llm/stats', methods=['DELETE'])
@login_required
@admin_required
def api_llm_stats_clear():
    db_exec('DELETE FROM llm_stats')
    return api_ok(cleared=True)


@app.route('/api/agent/tools')
@login_required
def api_agent_tools():
    """可用技能（工具）清单，供前端展示与开关。"""
    enabled = _agent_enabled()
    tools = []
    for t in agent_service.TOOL_SPECS:
        tools.append({
            'name': t['name'], 'title': t['title'], 'desc': t['desc'],
            'params': t.get('params') or {}, 'action': bool(t.get('action')),
            'enabled': (not enabled) or (t['name'] in enabled),
        })
    return api_ok(tools=tools, enabled=enabled or 'all',
                  agent_enabled=_setting_direct('agent_enabled', '1') in ('1', 'true', 'True', 'on'),
                  max_iters=int(float(_setting_direct('agent_max_iters', '3') or 3)),
                  prompt_on=_setting_direct('llm_system_prompt_on', '1') in ('1', 'true', 'True', 'on'),
                  vars=({k: str(v) for k, v in _prompt_vars().items()}))


@app.route('/api/agent/chat', methods=['POST'])
@login_required
def api_agent_chat():
    """类 Agent 对话：端侧 LLM 自动调用技能/工具读取实时数据后再回答（SSE）。

    事件流：iter / delta / tool_start / tool_result / usage / usage_total / error / [DONE]
    """
    data = request.get_json(silent=True) or {}
    messages = data.get('messages') or []
    if not isinstance(messages, list) or not messages:
        return api_err('messages 不能为空')
    provider = data.get('provider') or get_setting('llm_provider', 'local')
    cfg = provider_config(provider)
    if not cfg['url']:
        return api_err('LLM API 地址未配置')
    if cfg['provider'] == 'local':
        _llm_ok, _llm_msg = voice_service_instance.ensure_llm_ready()
        if not _llm_ok:
            return api_err('本地 LLM 未就绪：%s' % _llm_msg)
    model = data.get('model') or cfg['model']
    clean = []
    for m in messages[-20:]:
        if isinstance(m, dict) and str(m.get('role')) in ('user', 'assistant') and m.get('content'):
            clean.append({'role': str(m['role']), 'content': str(m['content'])})
    if not clean:
        return api_err('messages 无效')
    if not (_setting_direct('agent_enabled', '1') in ('1', 'true', 'True', 'on')):
        return api_err('Agent 能力已关闭（设置 / 校准 → LLM 提示词与 Agent）', 403)
    enabled = _agent_enabled()
    try:
        max_iters = int(float(data.get('max_iters') or _setting_direct('agent_max_iters', '3') or 3))
    except Exception:
        max_iters = 3
    max_iters = max(1, min(5, max_iters))
    # 用户自定义提示词（含变量展开）——板端 RKLLM 忽略 system，
    # 因此这里只取正文，稍后由 compose_user_prompt 一起并入用户消息
    base_prompt = ''
    if _setting_direct('llm_system_prompt_on', '1') in ('1', 'true', 'True', 'on'):
        base_prompt = (_setting_direct('llm_system_prompt', '') or '').strip()
        if base_prompt and _setting_direct('llm_prompt_vars', '1') in ('1', 'true', 'True', 'on'):
            try:
                base_prompt = _expand_vars(base_prompt)
            except Exception:
                pass
    base_prompt = base_prompt[:4000]
    temperature = float(data.get('temperature', 0.3))
    max_tokens = int(data.get('max_tokens', 1024))
    # 总结轮要不要回灌基础设定：见 agent_service.summary_spec（auto = 只给外部云模型）
    sp_mode = (os.environ.get('RELAY_ASSIST_SUMMARY_SPEC') or 'auto').strip().lower()
    if sp_mode not in agent_service.SUMMARY_SPEC_MODES:
        sp_mode = 'auto'
    try:
        sp_cap = max(120, min(4000, int(
            os.environ.get('RELAY_ASSIST_SUMMARY_SPEC_MAX') or 1200)))
    except Exception:
        sp_cap = 1200

    def generate():
        meter_all = agent_service.RateMeter()
        used, ok, note = [], True, ''
        ctx = _agent_ctx()
        last_user = ''
        for _m in reversed(clean):
            if _m.get('role') == 'user':
                last_user = _m.get('content') or ''
                break
        valid_tools = [t['name'] for t in agent_service.enabled_tools(enabled)]
        # 指令（含工具协议）并入用户消息：实测板端模型只有这样才能照做
        question = last_user
        collected = []          # 累积的紧凑读取结果（回灌给模型的唯一数据源）
        # 数据类问句：第一轮强制先取数（提示词里再加一条硬性要求，且该轮不向用户输出文字）
        force_first = agent_service.wants_realtime(last_user)
        for it in range(max_iters + 1):
            force = bool(it == 0 and force_first and not used)
            yield _sse({'type': 'iter', 'iter': it + 1, 'max': max_iters + 1,
                        'force_tool': force})
            if it == 0:
                content = agent_service.compose_user_prompt(base_prompt, question, enabled)
                if force:
                    content += '\n现在只输出一行读取指令（格式 READ 名称 {}），不要回答用户。'
                msgs = [{'role': 'user', 'content': content}]
            else:
                # 数据已拿到：让模型只做总结。约束必须在这一轮重新出现——
                # **这一轮产出的字才是用户真正看到的**（第一轮被要求只输出读取指令）。
                msgs = agent_service.summary_messages(
                    collected, question[:100], base_prompt, provider,
                    mode=sp_mode, cap=sp_cap,
                    tail='请用中文 1~3 句回答：')
            payload = {'model': model, 'messages': msgs, 'stream': True,
                       'temperature': temperature, 'max_tokens': max_tokens}
            text = ''
            buf_show = ''                     # 强制轮的可见文本先缓冲，没解析到工具调用就照常展示
            filt = agent_service.StreamFilter()
            meter = agent_service.RateMeter()
            try:
                r = requests.post(cfg['url'], json=payload,
                                  headers=_llm_headers(cfg['api_key']),
                                  stream=True, timeout=(5, 300))
                if r.status_code != 200:
                    ok, note = False, f'HTTP {r.status_code}'
                    yield _sse({'type': 'error', 'error': f'LLM HTTP {r.status_code}: {r.text[:200]}'})
                    break
                buf = ''
                for chunk in r.iter_content(chunk_size=None):
                    if not chunk:
                        continue
                    try:
                        txt = chunk.decode('utf-8', 'ignore')
                    except Exception:
                        continue
                    buf += txt
                    while '\n' in buf:
                        line, buf = buf.split('\n', 1)
                        line = line.strip()
                        if not line.startswith('data:'):
                            continue
                        pl = line[5:].strip()
                        if not pl or pl == '[DONE]':
                            continue
                        try:
                            obj = json.loads(pl)
                        except Exception:
                            continue
                        if obj.get('error'):
                            yield _sse({'type': 'error', 'error': str(obj['error'])[:300]})
                        d = ((obj.get('choices') or [{}])[0].get('delta') or {}).get('content')
                        if d:
                            text += d
                            meter.add(d)
                            meter_all.add(d)
                            show = filt.feed(d)
                            # 强制取数的那一轮：模型应该只输出读取指令，正文先缓冲不展示
                            if show:
                                if force:
                                    buf_show += show
                                else:
                                    yield _sse({'type': 'delta', 'content': show})
            except Exception as e:
                ok, note = False, type(e).__name__
                yield _sse({'type': 'error', 'error': f'LLM 请求失败: {e}'})
                break
            tail = filt.feed('', final=True)
            if tail:
                if force:
                    buf_show += tail
                else:
                    yield _sse({'type': 'delta', 'content': tail})
            yield _sse({'type': 'usage', **meter.snapshot({'iter': it + 1, 'force_tool': force})})
            calls = agent_service.parse_tool_calls(text, valid=valid_tools)
            if not calls:
                # 强制轮没给出读取指令：把模型自己的回答展示出来，避免"静默空回答"
                if force and buf_show.strip():
                    yield _sse({'type': 'delta', 'content': buf_show.strip()})
                break
            if it >= max_iters:
                yield _sse({'type': 'notice',
                            'text': f'已达最大工具轮次 {max_iters}，直接汇总回答'})
                break
            results = []
            for c in calls:
                yield _sse({'type': 'tool_start', 'name': c['name'], 'arguments': c['arguments']})
                t0 = time.time()
                okk, res = agent_service.execute_tool(c['name'], c['arguments'], ctx)
                used.append(c['name'])
                yield _sse({'type': 'tool_result', 'name': c['name'], 'ok': bool(okk),
                            'ms': round((time.time() - t0) * 1000), 'result': res})
                results.append({'name': c['name'], 'ok': bool(okk),
                                'arguments': c['arguments'], 'result': res})
            for r in results:
                collected.append({r['name']: r.get('result')})
        snap = meter_all.snapshot({'provider': cfg['provider'], 'model': model,
                                   'tools': ','.join(used), 'iters': len(used)})
        _llm_stat_record('agent', cfg, model, snap, ok=ok, note=note,
                         iters=len(used), tools=','.join(used))
        yield _sse({'type': 'usage_total', **snap})
        yield 'data: [DONE]\n\n'

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


# ---------------------------------------------------------------------------
# 中继语音日志：BUSY/PTT 触发录音 + 异步 ASR + 智能分类 + 每日总结
# ---------------------------------------------------------------------------
def _vlog_settings_direct():
    """无 app context 读取全部 vlog_* 设置（供语音服务后台线程使用）。

    连接必须在 finally 里关（原先 close() 在 try 体内，异常路径会漏连接/fd）。
    """
    out = {}
    db = None
    try:
        db = sqlite3.connect(str(DB_PATH), timeout=3)
        for k, v in db.execute("SELECT key,value FROM settings WHERE key LIKE 'vlog_%'"):
            out[k] = v
    except Exception:
        pass
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass
    return out


def _vlog_capture_guard():
    """采集中枢守护：语音日志启用期间保证 arecord 常驻。

    nau8822 同一时刻只允许一路采集，所以网页实时对讲与本录音共用同一条流：
    这里只负责「确保在跑」，停止与否由 stop_mic_capture 统一判断。
    """
    time.sleep(8)
    while True:
        try:
            # 语音日志**或**中继语音助手任一启用，就必须保证 arecord 常驻：
            # 关掉语音日志时助手不能跟着失聪。
            if voice_service_instance.enabled() or assistant_service_instance.enabled():
                with MIC_CAPTURE_LOCK:
                    running = bool(MIC_CAPTURE.get('running'))
                if not running:
                    ok, msg = start_mic_capture()
                    print('[VLOG] 采集中枢拉起：%s / %s' % (ok, msg), flush=True)
        except Exception as e:
            print('[VLOG] 采集中枢守护异常：%s: %s' % (type(e).__name__, e), flush=True)
        time.sleep(15)


voice_service_instance.configure(
    get_rx=lambda: bool(BUSY_STATE.get('active')),
    get_tx=lambda: bool(PTT_LEVEL),
    provider_config=lambda p=None: provider_config(p),
    setting_getter=_vlog_settings_direct,
)
# 让 ASR 语言跟随设置项（SenseVoice 的 auto 对中文无线电语音容易判成英文）
asr_service.set_language_getter(lambda: _setting_direct('asr_language', 'auto'))
voice_service_instance.start()
threading.Thread(target=_vlog_capture_guard, daemon=True, name='vlog-capture').start()

# APRS：注入信道仲裁 / 音频发射 / 数据源（lambda 延迟解析，
# 因为被引用的函数定义在文件更后面）
aprs_service_instance.configure(
    setting_getter=lambda k, d='': _setting_direct(k, d),
    carrier_busy=lambda: _aprs_carrier_busy(),
    play_raw=lambda raw, rate, hold: _aprs_play_raw(raw, rate, hold),
    wx_getter=lambda: _aprs_weather_source(),
    power_getter=lambda: _aprs_power_source(),
    temp_getter=lambda: _aprs_cpu_temp(),
)


@app.route('/voice-log')
@login_required
def voice_log_page():
    return render_template('voice_log.html', user=session.get('username'),
                           role=session.get('role'))


@app.route('/api/voice/status')
@login_required
def api_voice_status():
    return api_ok(**voice_service_instance.status())


@app.route('/api/voice/list')
@login_required
def api_voice_list():
    day = (request.args.get('day') or '').strip() or None
    category = (request.args.get('category') or '').strip() or None
    kind = (request.args.get('kind') or '').strip() or None
    q = (request.args.get('q') or '').strip() or None
    # 只看含 APRS 位置的段（尾音里解出对方信标），便于标记与查找
    pos = (request.args.get('pos') or '').strip() or None
    if pos not in ('only', 'none'):
        pos = None
    try:
        limit = max(1, min(1000, int(request.args.get('limit') or 200)))
    except Exception:
        limit = 200
    try:
        offset = max(0, int(request.args.get('offset') or 0))
    except Exception:
        offset = 0
    items = voice_service_instance.list_logs(day=day, category=category,
                                             kind=kind, q=q, pos=pos,
                                             limit=limit, offset=offset)
    return api_ok(items=items, day=day, limit=limit, offset=offset,
                  stats=voice_service_instance.day_stats(day))


@app.route('/api/voice/days')
@login_required
def api_voice_days():
    rows = voice_service_instance.store.query(
        "SELECT substr(ts,1,10) AS day, COUNT(*) AS n, SUM(seconds) AS sec, SUM(bytes) AS b "
        "FROM voice_logs GROUP BY day ORDER BY day DESC LIMIT 180")
    return api_ok(days=[{'day': r['day'], 'n': r['n'],
                         'seconds': round(float(r['sec'] or 0.0), 1),
                         'mb': round(float(r['b'] or 0) / 1048576.0, 1)} for r in rows])


@app.route('/api/voice/timeline')
@login_required
def api_voice_timeline():
    day = (request.args.get('day') or '').strip() or None
    return api_ok(items=voice_service_instance.day_peaks(day))


@app.route('/api/voice/<int:rid>/audio')
@login_required
def api_voice_audio(rid):
    row = voice_service_instance.store.one('SELECT path,filename FROM voice_logs WHERE id=?', (rid,))
    if not row or not row.get('path') or not Path(row['path']).exists():
        abort(404)
    resp = send_file(row['path'], mimetype='audio/wav', conditional=True)
    resp.headers['Accept-Ranges'] = 'bytes'
    return resp


@app.route('/api/voice/<int:rid>/download')
@login_required
def api_voice_download(rid):
    row = voice_service_instance.store.one('SELECT path,filename FROM voice_logs WHERE id=?', (rid,))
    if not row or not row.get('path') or not Path(row['path']).exists():
        abort(404)
    return send_file(row['path'], mimetype='audio/wav', as_attachment=True,
                     download_name=row.get('filename') or ('vlog_%d.wav' % rid))


@app.route('/api/voice/<int:rid>/peaks')
@login_required
def api_voice_peaks(rid):
    try:
        n = max(64, min(2000, int(request.args.get('n') or 600)))
    except Exception:
        n = 600
    return api_ok(peaks=voice_service_instance.peaks(rid, n))


@app.route('/api/voice/<int:rid>/retranscribe', methods=['POST'])
@login_required
def api_voice_retranscribe(rid):
    voice_service_instance.retranscribe(rid)
    audit('voice_retranscribe', 'id=%s' % rid)
    return api_ok(id=rid)


@app.route('/api/voice/<int:rid>/delete', methods=['POST'])
@login_required
@admin_required
def api_voice_delete(rid):
    row = voice_service_instance.store.one('SELECT path FROM voice_logs WHERE id=?', (rid,))
    if row and row.get('path'):
        try:
            Path(row['path']).unlink()
        except Exception:
            pass
    voice_service_instance.store.exec('DELETE FROM voice_logs WHERE id=?', (rid,))
    audit('voice_delete', 'id=%s' % rid)
    return api_ok(id=rid)


@app.route('/api/voice/export')
@login_required
def api_voice_export():
    """导出某天语音日志：fmt=txt|srt|csv。"""
    day = (request.args.get('day') or datetime.now().strftime('%Y-%m-%d')).strip()
    fmt = (request.args.get('fmt') or 'txt').strip().lower()
    if fmt not in ('txt', 'srt', 'csv'):
        fmt = 'txt'
    rows = sorted(voice_service_instance.list_logs(day=day, limit=2000),
                  key=lambda r: r.get('epoch') or 0)
    lines = []
    if fmt == 'csv':
        lines.append('id,时间,类型,类别,时长s,识别文字')
        for r in rows:
            txt = (r.get('text') or '').replace('"', '""').replace('\n', ' ')
            lines.append('%s,%s,%s,%s,%s,"%s"' % (
                r['id'], r['ts'], r['kind_label'], r['category_label'], r['seconds'], txt))
        text = '\n'.join(lines) + '\n'
    elif fmt == 'srt':
        idx = 1
        for r in rows:
            if not (r.get('text') or '').strip():
                continue
            base = float(r.get('epoch') or 0)

            def _t(sec):
                sec = max(0.0, sec)
                h = int(sec // 3600)
                m = int((sec % 3600) // 60)
                s = sec % 60
                return '%02d:%02d:%06.3f' % (h, m, s)

            if r.get('segments'):
                for sg in r['segments']:
                    lines.append(str(idx))
                    lines.append('%s --> %s' % (_t(base + float(sg.get('start') or 0)),
                                                _t(base + float(sg.get('end') or 0))))
                    lines.append(sg.get('text') or '')
                    lines.append('')
                    idx += 1
            else:
                lines.append(str(idx))
                lines.append('%s --> %s' % (_t(base), _t(base + float(r.get('seconds') or 0))))
                lines.append(r['text'])
                lines.append('')
                idx += 1
        text = '\n'.join(lines) + '\n'
    else:
        lines.append('中继语音日志 %s（共 %d 条）' % (day, len(rows)))
        lines.append('=' * 46)
        for r in rows:
            t = (r.get('ts') or '')[11:19]
            lines.append('[%s] %s/%s %ss' % (t, r['kind_label'], r['category_label'], r['seconds']))
            if (r.get('text') or '').strip():
                for sg in (r.get('segments') or []):
                    lines.append('    %5.1fs %s' % (float(sg.get('start') or 0), sg.get('text')))
                if not r.get('segments'):
                    lines.append('    ' + r['text'])
        text = '\n'.join(lines) + '\n'
    buf = io.BytesIO(text.encode('utf-8-sig'))
    buf.seek(0)
    return send_file(buf, mimetype='text/plain; charset=utf-8', as_attachment=True,
                     download_name='voice_%s.%s' % (day, fmt))


@app.route('/api/voice/summary')
@login_required
def api_voice_summary():
    day = (request.args.get('day') or datetime.now().strftime('%Y-%m-%d')).strip()
    row = voice_service_instance.store.one('SELECT * FROM voice_daily WHERE day=?', (day,))
    data = voice_service_instance.day_transcript(day) if not row else None
    return api_ok(day=day, summary=row,
                  transcript=(row or {}).get('transcript') or (data or {}).get('text', ''),
                  state=dict(voice_service_instance.summary_state))


@app.route('/api/voice/summary/list')
@login_required
def api_voice_summary_list():
    rows = voice_service_instance.store.query(
        'SELECT day,ts,segments,voice_count,seconds,provider,model,status,error,elapsed,chunks '
        'FROM voice_daily ORDER BY day DESC LIMIT 180')
    return api_ok(items=rows)


@app.route('/api/voice/summary/run', methods=['POST'])
@login_required
@admin_required
def api_voice_summary_run():
    data = request.get_json(silent=True) or {}
    day = (data.get('day') or request.args.get('day')
           or datetime.now().strftime('%Y-%m-%d')).strip()
    if voice_service_instance.summary_state.get('running'):
        return api_err('日报正在生成中，请稍候')
    provider = (data.get('provider') or '').strip() or None
    if provider not in (None, 'local', 'external'):
        provider = None
    threading.Thread(target=voice_service_instance.run_summary, args=(day, False, provider),
                     daemon=True, name='vlog-summary-once').start()
    audit('voice_summary_run', 'day=%s' % day)
    return api_ok(started=True, day=day)


@app.route('/api/voice/reclassify', methods=['POST'])
@login_required
@admin_required
def api_voice_reclassify():
    """按 APRS 收发记录重跑时间交叉判定，修正被削顶带偏的分类（不重跑 ASR）。"""
    data = request.get_json(silent=True) or {}
    day = (data.get('day') or '').strip() or None
    dry = bool(data.get('dry'))
    changed = voice_service_instance.reclassify_aprs(day, dry=dry)
    audit('voice_reclassify', '%s dry=%s n=%d' % (day or 'all', dry, len(changed)))
    return api_ok(changed=len(changed), dry=dry,
                  items=[{'id': i, 'from': o, 'to': n} for i, o, n in changed[:200]])


@app.route('/api/voice/cleanup', methods=['POST'])
@login_required
@admin_required
def api_voice_cleanup():
    n = voice_service_instance.cleanup()
    audit('voice_cleanup', 'removed=%s' % n)
    return api_ok(removed=n, status=voice_service_instance.status())


# ---------------------------------------------------------------------------
# APRS 收发：自研 1200bps Bell202 软件 TNC（接收）+ 天地图（地图）
# 合规说明：底图使用「天地图」（国家地理信息公共服务平台，地图内容已审核、
# 自带审图号，资质由平台承担）；不使用 OSM（其国界画法不符合我国《公开地图
# 内容表示规范》，且本网络内 tile.openstreetmap.org 不可达）。天地图为 CGCS2000
# 坐标系，与 WGS-84 实用精度一致，因此 APRS 坐标可直接绘制，无需 GCJ-02 偏移
# 转换（高德/百度若不转换会有 300~600 米偏移）。
# ---------------------------------------------------------------------------
APRS_TILE_DIR = Path(os.environ.get('RELAY_APRS_TILE_DIR', '/opt/ai/aprs_tiles'))
TIANDITU_HOSTS = ['t%d.tianditu.gov.cn' % i for i in range(8)]
APRS_LAYERS = {
    'img': '影像底图', 'vec': '矢量底图', 'ter': '地形晕渲',
    'cva': '影像注记', 'cia': '矢量注记', 'cta': '地形注记',
}
_APRS_TILE_N = {'n': 0}


def _aprs_carrier_busy():
    """信道忙：BUSY 有效（电台上收到信号）或本机 PTT 正在发射。"""
    try:
        if bool(BUSY_STATE.get('active')):
            return True
    except Exception:
        pass
    try:
        return bool(PTT_LEVEL)
    except Exception:
        return False


def _aprs_weather_source():
    """气象站数据 -> APRS 需要的物理量。缺项为 None（APRS 允许省略字段）。"""
    out = {'online': False}
    try:
        st = weather_service_instance.realtime() or {}
    except Exception:
        st = {}
    out['online'] = bool(st.get('running')) and not st.get('last_error')

    def num(*keys):
        for k in keys:
            v = st.get(k)
            if v is not None and v != '':
                try:
                    return float(v)
                except Exception:
                    pass
        return None

    out['wind_ms'] = num('last_speed', 'wind_speed')
    out['wind_dir'] = num('wind_dir', 'last_dir', 'direction', 'dir')
    out['gust_ms'] = num('gust_ms', 'last_gust')
    out['temp_c'] = num('th_temperature', 'temperature', 'temp_c')
    out['humidity'] = num('th_humidity', 'humidity')
    out['pressure_hpa'] = num('pressure_hpa', 'pressure')
    if out['wind_dir'] is None:
        try:
            out['wind_dir'] = float(_setting_direct('aprs_wx_dir_fixed', '') or 0) or None
        except Exception:
            pass
    try:
        out['rain_1h_mm'] = float(weather_service_instance.rain_recent_hour())
    except Exception:
        pass
    try:
        rs = weather_service_instance.rain_stats(datetime.now().strftime('%Y-%m-%d'))
        if rs and rs.get('total_mm') is not None:
            out['rain_today_mm'] = float(rs['total_mm'])
    except Exception:
        pass
    return out


def _aprs_power_source():
    try:
        pw = voltage_payload() or {}
        return {'battery_v': (pw.get('battery') or {}).get('voltage'),
                'pv_v': (pw.get('pv') or {}).get('voltage')}
    except Exception:
        return {}


def _aprs_cpu_temp():
    try:
        for item in (read_temperature() or []):
            nm = str(item.get('name') or '').lower()
            if 'soc' in nm or 'cpu' in nm:
                return item.get('celsius')
        temps = read_temperature() or []
        return temps[0].get('celsius') if temps else None
    except Exception:
        return None


def _aprs_play_raw(pcm_bytes, rate=16000, hold_ptt=True):
    """把原始 PCM 送上 AUX 发射（自带 PTT 保持与设备抢占）。

    PTT 必须在音频之前拉起、在音频之后放下；同时要抢占上一路 aplay
    （同一张声卡同一时刻只允许一路），否则会因设备忙直接失败。
    """
    global CURRENT_PLAY_PROC
    t0 = time.time()
    ptt_on = False
    try:
        _ensure_audio_unmuted()
        if hold_ptt:
            _ptt_retain()
            ptt_on = True
            time.sleep(0.12)          # 等功放/继电器稳定再送音频
        with PLAY_LOCK:
            _stop_proc(CURRENT_PLAY_PROC)
            cmd = ['aplay', '-D', AUDIO_DEVICE, '-q', '-t', 'raw',
                   '-f', 'S16_LE', '-r', str(int(rate)), '-c', '1']
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE)
            CURRENT_PLAY_PROC = proc
            try:
                proc.stdin.write(pcm_bytes)
                proc.stdin.close()
            except Exception as e:
                _stop_proc(proc)
                return {'ok': False, 'error': '写入音频失败：%s' % e}
            limit = max(10.0, len(pcm_bytes) / float(rate * 2) + 10.0)
            try:
                proc.wait(timeout=limit)
            except Exception:
                _stop_proc(proc)
                return {'ok': False, 'error': '播放超时'}
            if CURRENT_PLAY_PROC is proc:
                CURRENT_PLAY_PROC = None
        rc = proc.returncode
        return {'ok': rc == 0, 'error': '' if rc == 0 else 'aplay 退出码 %s' % rc,
                'ptt_ms': int((time.time() - t0) * 1000)}
    except Exception as e:
        return {'ok': False, 'error': '%s: %s' % (type(e).__name__, e)}
    finally:
        if ptt_on:
            try:
                _ptt_release()
            except Exception:
                pass


def _aprs_tile_path(layer, z, x, y):
    return APRS_TILE_DIR / layer / str(z) / str(x) / str(y)


def _aprs_tile_trim():
    """按总量上限清理瓦片缓存（最旧的先删）。"""
    try:
        cap = int(float(_setting_direct('aprs_map_cache_mb', '512') or 512)) * 1024 * 1024
    except Exception:
        cap = 512 * 1024 * 1024
    if cap <= 0 or not APRS_TILE_DIR.exists():
        return 0
    items = []
    total = 0
    for p in APRS_TILE_DIR.rglob('*'):
        if p.is_file() and not p.name.endswith('.type'):
            try:
                stt = p.stat()
            except Exception:
                continue
            items.append((stt.st_mtime, stt.st_size, p))
            total += stt.st_size
    if total <= cap:
        return 0
    items.sort()
    freed = 0
    for (_m, sz, p) in items:
        if total - freed <= cap * 0.9:
            break
        try:
            p.unlink()
            p.with_suffix('.type').unlink()
            freed += sz
        except Exception:
            pass
    return freed


def _aprs_tile_fetch(layer, z, x, y):
    """向天地图取瓦片。使用「服务端」类型 tk（不需要 Referer）。"""
    tk = (_setting_direct('aprs_map_tk', '') or '').strip()
    if not tk:
        return None, '未配置天地图服务端 key'
    host = TIANDITU_HOSTS[(int(z) + int(x) + int(y)) % len(TIANDITU_HOSTS)]
    url = ('https://%s/%s_w/wmts?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0'
           '&LAYER=%s&STYLE=default&TILEMATRIXSET=w&FORMAT=tiles'
           '&TILEMATRIX=%d&TILEROW=%d&TILECOL=%d&tk=%s'
           % (host, layer, layer, int(z), int(y), int(x), tk))
    try:
        r = requests.get(url, timeout=12,
                         headers={'User-Agent': 'ELF2-Relay/1.0'})
    except Exception as e:
        return None, '%s: %s' % (type(e).__name__, e)
    if r.status_code != 200:
        body = ''
        try:
            body = r.text[:120]
        except Exception:
            pass
        return None, '天地图返回 %s %s' % (r.status_code, body)
    if len(r.content) < 200:
        return None, '天地图返回内容异常（%d 字节）' % len(r.content)
    ct = (r.headers.get('Content-Type') or 'image/jpeg').split(';')[0].strip()
    if not ct.startswith('image/'):
        ct = 'image/jpeg'
    return r.content, ct


@app.route('/aprs')
@login_required
def aprs_page():
    return render_template('aprs.html', user=session.get('username'),
                           role=session.get('role'))


@app.route('/api/aprs/status')
@login_required
def api_aprs_status():
    payload = aprs_service_instance.stats_payload()
    payload['busy'] = _aprs_carrier_busy()
    payload['ptt'] = bool(PTT_LEVEL)
    payload['tile_dir'] = str(APRS_TILE_DIR)
    payload['layers'] = APRS_LAYERS
    return api_ok(**payload)


@app.route('/api/aprs/list')
@login_required
def api_aprs_list():
    def _int(name, dflt, lo, hi):
        try:
            return max(lo, min(hi, int(request.args.get(name) or dflt)))
        except Exception:
            return dflt

    rows = aprs_service_instance.list_packets(
        day=(request.args.get('day') or '').strip() or None,
        src=(request.args.get('src') or '').strip() or None,
        dtype=(request.args.get('dtype') or '').strip() or None,
        limit=_int('limit', 100, 1, 500),
        offset=_int('offset', 0, 0, 1000000),
        pos_only=request.args.get('pos_only') in ('1', 'true'),
        since_id=_int('since_id', 0, 0, 10 ** 9))
    for r in rows:
        r.pop('raw_hex', None)       # 列表不返回完整帧，详情接口才给
    return api_ok(packets=rows, count=len(rows))


@app.route('/api/aprs/geo')
@login_required
def api_aprs_geo():
    """地图数据：时间窗内有位置的站点 + 轨迹。"""
    try:
        minutes = max(5, min(10080, int(request.args.get('minutes') or 180)))
    except Exception:
        minutes = 180
    data = aprs_service_instance.packets_geo(
        day=(request.args.get('day') or '').strip() or None, minutes=minutes)
    data['home'] = aprs_service_instance.position.get()
    return api_ok(**data)


@app.route('/api/aprs/stations')
@login_required
def api_aprs_stations():
    return api_ok(stations=aprs_service_instance.stations())


@app.route('/api/aprs/<int:rid>')
@login_required
def api_aprs_detail(rid):
    row = aprs_service_instance.store.one(
        'SELECT * FROM aprs_packets WHERE id=?', (int(rid),))
    if not row:
        return api_err('记录不存在', 404)
    return api_ok(packet=row)


@app.route('/api/aprs/<int:rid>/raw')
@login_required
def api_aprs_raw(rid):
    """下载原始 AX.25 帧（含 FCS），用于外部工具交叉校验。"""
    row = aprs_service_instance.store.one(
        'SELECT id,ts,src,raw_hex FROM aprs_packets WHERE id=?', (int(rid),))
    if not row:
        abort(404)
    try:
        blob = bytes.fromhex(row['raw_hex'] or '')
    except Exception:
        blob = b''
    fn = 'aprs_%d_%s.bin' % (rid, re.sub(r'[^A-Za-z0-9-]', '', row['src'] or 'unk'))
    return Response(blob, mimetype='application/octet-stream',
                    headers={'Content-Disposition': 'attachment; filename=%s' % fn})


@app.route('/api/aprs/tx/list')
@login_required
def api_aprs_tx_list():
    try:
        limit = max(1, min(500, int(request.args.get('limit') or 60)))
    except Exception:
        limit = 60
    rows = aprs_service_instance.store.query(
        'SELECT * FROM aprs_tx ORDER BY id DESC LIMIT ?', (limit,))
    return api_ok(items=rows, count=len(rows))


@app.route('/api/aprs/tx', methods=['POST'])
@login_required
@admin_required
def api_aprs_tx():
    """手动发射一份 APRS 报文。"""
    data = request.get_json(silent=True) or {}
    ptype = (data.get('type') or '').strip()
    if ptype not in ('position', 'weather', 'status', 'telemetry', 'message'):
        return api_err('不支持的报文类型：%s' % ptype)
    kw = {}
    if ptype == 'message':
        kw['to'] = (data.get('to') or '').strip().upper()[:9]
        kw['text'] = (data.get('text') or '')[:67]
        if not kw['to']:
            return api_err('消息必须填写收件呼号')
    if ptype == 'status':
        kw['text'] = (data.get('text') or '')[:60]
    if not aprs_service_instance.enabled():
        return api_err('APRS 未启用，请先在设置中打开')
    res = aprs_service_instance.send(ptype, trigger='manual', **kw)
    audit('aprs_tx', '%s %s' % (ptype, (kw.get('to') or kw.get('text') or '')[:40]))
    if not res.get('ok'):
        return api_err(res.get('error') or '发射失败')
    return api_ok(**res)


@app.route('/api/aprs/pos', methods=['POST'])
@login_required
@admin_required
def api_aprs_pos():
    """保存本机位置与站点标识（地图上的「本站」，也是信标/气象源）。"""
    data = request.get_json(silent=True) or {}
    saved = {}
    for k, cast in (
            ('aprs_lat', lambda v: str(round(max(-90.0, min(90.0, float(v))), 6))),
            ('aprs_lon', lambda v: str(round(max(-180.0, min(180.0, float(v))), 6))),
            ('aprs_alt_m', lambda v: ('' if str(v).strip() == ''
                                      else str(round(float(v), 1)))),
            ('aprs_mycall', lambda v: (''.join(c for c in str(v).upper()
                                               if c.isalnum()))[:6]),
            ('aprs_ssid', lambda v: str(max(0, min(15, int(float(v)))))),
            ('aprs_comment', lambda v: str(v).strip()[:60])):
        if k in data:
            try:
                set_setting(k, cast(data[k]))
                saved[k] = get_setting(k)
            except Exception as e:
                return api_err('%s 无效：%s' % (k, e))
    if saved:
        aprs_service_instance.invalidate()
        audit('aprs_pos', json.dumps(saved, ensure_ascii=False))
    return api_ok(saved=saved, position=aprs_service_instance.position.get())


@app.route('/api/aprs/export')
@login_required
def api_aprs_export():
    """导出收包记录：txt / csv / json。"""
    day = (request.args.get('day') or datetime.now().strftime('%Y-%m-%d')).strip()
    fmt = (request.args.get('format') or 'txt').lower()
    rows = aprs_service_instance.list_packets(day=day, limit=5000)
    if fmt == 'json':
        return Response(json.dumps(rows, ensure_ascii=False, indent=1),
                        mimetype='application/json',
                        headers={'Content-Disposition':
                                 'attachment; filename=aprs_%s.json' % day})
    if fmt == 'csv':
        import csv
        import io
        buf = io.StringIO()
        cols = ['ts', 'src', 'dst', 'path', 'dtype', 'info', 'lat', 'lon',
                'comment', 'raw_hex']
        wtr = csv.writer(buf)
        wtr.writerow(cols)
        for r in rows:
            wtr.writerow([r.get(c) for c in cols])
        return Response('\ufeff' + buf.getvalue(), mimetype='text/csv',
                        headers={'Content-Disposition':
                                 'attachment; filename=aprs_%s.csv' % day})
    lines = []
    for r in rows:
        pos = ''
        if r.get('lat') is not None:
            pos = ' [%.5f,%.5f]' % (r['lat'], r['lon'])
        lines.append('%s  %-12s %-9s %s%s' % (
            r.get('ts') or '', r.get('src') or '', r.get('dtype_label') or '',
            (r.get('info') or '').replace('\n', ' '), pos))
    return Response('\n'.join(lines), mimetype='text/plain; charset=utf-8',
                    headers={'Content-Disposition':
                             'attachment; filename=aprs_%s.txt' % day})


@app.route('/api/aprs/cleanup', methods=['POST'])
@login_required
@admin_required
def api_aprs_cleanup():
    data = request.get_json(silent=True) or {}
    days = int(float(data.get('days') or 0))
    n = m = 0
    if days > 0:
        cut = time.time() - days * 86400
        n = aprs_service_instance.store.exec(
            'DELETE FROM aprs_packets WHERE ts_epoch < ?', (cut,)) or 0
        m = aprs_service_instance.store.exec(
            'DELETE FROM aprs_tx WHERE ts_epoch < ?', (cut,)) or 0
    freed = _aprs_tile_trim()
    audit('aprs_cleanup', 'days=%s packets=%s tx=%s' % (days, n, m))
    return api_ok(packets=n, tx=m, tile_freed=freed)


@app.route('/api/aprs/tile/<layer>/<int:z>/<int:x>/<int:y>')
@login_required
def api_aprs_tile(layer, z, x, y):
    """天地图瓦片代理 + 磁盘缓存。

    走服务端代理而不是让浏览器直连，好处有三：
      1) 服务端类型 tk 不需要 Referer，且 key 不会出现在前端；
      2) 可落盘缓存 —— 断网时已浏览过的区域仍能显示；
      3) 受登录保护，避免 key 被公开滥用（合规上更稳妥）。
    """
    if layer not in APRS_LAYERS:
        abort(404)
    if not (0 <= z <= 18) or x < 0 or y < 0 or x >= (1 << z) or y >= (1 << z):
        abort(404)
    p = _aprs_tile_path(layer, z, x, y)
    if p.exists():
        ct = 'image/jpeg'
        try:
            ct = (p.with_suffix('.type')).read_text(encoding='utf-8').strip() or ct
        except Exception:
            pass
        try:
            return Response(p.read_bytes(), mimetype=ct,
                            headers={'Cache-Control': 'public, max-age=86400',
                                     'X-Tile-Source': 'cache'})
        except Exception:
            pass
    data, ct = _aprs_tile_fetch(layer, z, x, y)
    if data is None:
        return api_err('瓦片获取失败：%s' % ct, 502)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        p.with_suffix('.type').write_text(ct, encoding='utf-8')
        _APRS_TILE_N['n'] += 1
        if _APRS_TILE_N['n'] % 200 == 0:
            _aprs_tile_trim()
    except Exception:
        pass
    return Response(data, mimetype=ct,
                    headers={'Cache-Control': 'public, max-age=86400',
                             'X-Tile-Source': 'upstream'})


# ---------------------------------------------------------------------------
# 端侧语音识别（ASR）：sherpa-onnx + SenseVoice（离线）
# ---------------------------------------------------------------------------
def _asr_enabled():
    return _setting_direct('asr_enabled', '1') in ('1', 'true', 'True', 'on')


@app.route('/api/asr/status')
@login_required
def api_asr_status():
    """ASR 引擎状态（模型文件、是否已加载、最近一次识别）。"""
    st = asr_service.status()
    md = (_setting_direct('asr_model_dir', '') or '').strip()
    if md:
        st['model_dir'] = md
        ok, model, tokens = asr_service.model_ready(md)
        st.update({'files_ok': bool(ok), 'model': Path(model).name if model else '',
                   'tokens': Path(tokens).name if tokens else ''})
    st['enabled'] = _asr_enabled()
    st['language'] = (_setting_direct('asr_language', '') or st.get('language') or 'auto')
    try:
        row = get_db().execute('SELECT * FROM asr_logs ORDER BY id DESC LIMIT 1').fetchone()
        st['last_log'] = dict(row) if row else {}
    except Exception:
        st['last_log'] = {}
    return api_ok(**st)


@app.route('/api/asr/transcribe', methods=['POST'])
@login_required
def api_asr_transcribe():
    """语音转文字：接收前端录音（multipart 字段 audio/file），返回识别文本。

    也支持 JSON {"path": "..."} 直接识别板端已有音频文件。
    """
    if not _asr_enabled():
        return api_err('语音识别未启用', 403)
    tmp = None
    saved = None
    try:
        f = request.files.get('audio') or request.files.get('file')
        if f is not None:
            raw = f.read()
            if not raw:
                return api_err('音频为空')
            if len(raw) > 32 * 1024 * 1024:
                return api_err('音频过大（上限 32MB）', 413)
            ext = (Path(f.filename or 'rec.webm').suffix or '.webm').lower()
            if ext not in ('.wav', '.webm', '.ogg', '.opus', '.mp3', '.m4a', '.mp4', '.flac'):
                ext = '.webm'
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            tmp = Path('/tmp') / ('asr_up_%s%s' % (uuid.uuid4().hex[:8], ext))
            tmp.write_bytes(raw)
            ASR_RECORD_DIR.mkdir(parents=True, exist_ok=True)
            saved = ASR_RECORD_DIR / ('asr_%s%s' % (ts, ext))
            try:
                shutil.copyfile(tmp, saved)
            except Exception:
                saved = None
        else:
            data = request.get_json(silent=True) or {}
            src = str(data.get('path') or '').strip()
            if not src or not Path(src).exists():
                return api_err('缺少音频（multipart 字段 audio，或 JSON path）')
            tmp = Path(src)
            saved = None

        res = asr_service.transcribe(str(tmp))
        if not res.get('ok'):
            return api_err('识别失败：%s' % res.get('error'), 500)
        text = (res.get('text') or '').strip()
        try:
            db_exec('INSERT INTO asr_logs(ts,username,filename,seconds,ms,rtf,text) '
                    'VALUES(?,?,?,?,?,?,?)',
                    (now_iso(), session.get('username'), saved.name if saved else '',
                     float(res.get('seconds') or 0), int(res.get('ms') or 0),
                     float(res.get('rtf') or 0), text[:2000]))
        except Exception:
            pass
        try:
            audit('asr_transcribe', 'chars=%d ms=%s rtf=%s' % (len(text), res.get('ms'),
                                                               res.get('rtf')))
        except Exception:
            pass
        return api_ok(text=text, ms=res.get('ms'), seconds=res.get('seconds'),
                      rtf=res.get('rtf'), filename=saved.name if saved else '',
                      engine='sherpa-onnx-sensevoice')
    finally:
        try:
            if tmp is not None and str(tmp).startswith('/tmp') and Path(tmp).exists():
                Path(tmp).unlink()
        except Exception:
            pass


@app.route('/api/asr/recordings')
@login_required
def api_asr_recordings():
    """已留档的语音识别录音 + 最近识别记录。"""
    items = []
    try:
        ASR_RECORD_DIR.mkdir(parents=True, exist_ok=True)
        for f in sorted(ASR_RECORD_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True)[:100]:
            if f.is_file():
                st = f.stat()
                items.append({'name': f.name, 'size': st.st_size,
                              'ts': datetime.fromtimestamp(st.st_mtime).strftime('%Y-%m-%d %H:%M:%S')})
    except Exception:
        pass
    logs = []
    try:
        logs = [dict(r) for r in get_db().execute(
            'SELECT id,ts,username,filename,seconds,ms,rtf,text FROM asr_logs '
            'ORDER BY id DESC LIMIT 50').fetchall()]
    except Exception:
        pass
    return api_ok(recordings=items, logs=logs, dir=str(ASR_RECORD_DIR))


# ---------------------------------------------------------------------------
# 网页对讲 / 录音分段 / AUX 播放
# ---------------------------------------------------------------------------
def _setting_direct(key, default=''):
    """不依赖 Flask app context 读取设置，供启动阶段/播放线程使用。

    连接必须在 finally 里关：原先 db.close() 直接写在 try 体内，
    execute 一抛异常就漏一个 SQLite 连接（连带 fd）——实测进程里积了 60 个。
    """
    db = None
    try:
        db = sqlite3.connect(str(DB_PATH), timeout=3)
        row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return row[0] if row else default
    except Exception:
        return default
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass


def _amixer_sget(control):
    """读取 amixer 简单混音器，返回 (percent, on) 或 None。"""
    try:
        proc = subprocess.run(['amixer', '-c', '1', 'sget', control],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=3)
        text = proc.stdout.decode('utf-8', errors='replace')
        m = re.search(r'\[(\d+)%\]', text)
        on = 'Playback [on]' in text or 'Playback [off]' not in text
        if 'Playback [off]' in text:
            on = False
        return {'percent': int(m.group(1)) if m else None, 'on': on}
    except Exception:
        return None


def _apply_audio_volume(percent=None, muted=None):
    """设置 NAU88C22 Headphone/Speaker 全局音量与静音。"""
    if percent is None:
        percent = int(float(_setting_direct('audio_volume_percent', '80') or 80))
    percent = max(0, min(100, int(percent)))
    if muted is None:
        muted = _setting_direct('audio_muted', '0') in ('1', 'true', 'True', 'on')
    for ctrl in ('Headphone', 'Speaker'):
        try:
            subprocess.run(['amixer', '-c', '1', 'sset', ctrl, f'{percent}%'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
            action = 'mute' if muted else 'unmute'
            subprocess.run(['amixer', '-c', '1', 'sset', ctrl, action],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
        except Exception:
            pass
    return {'percent': percent, 'muted': muted}


def _ensure_audio_unmuted():
    """启动或播放前应用数据库中的全局音量与静音状态。"""
    return _apply_audio_volume()


# ---------------------------------------------------------------------------
# PTT GPIO3_A1 控制
# ---------------------------------------------------------------------------
def _ptt_ensure():
    """确保 GPIO 已 export 且方向为输出。"""
    global PTT_INITED, PTT_LAST_ERROR
    if PTT_INITED:
        return True
    try:
        if not PTT_GPIO_DIR.exists():
            with open('/sys/class/gpio/export', 'w') as f:
                f.write(str(PTT_GPIO_NUM))
            time.sleep(0.08)
        if not PTT_GPIO_DIR.exists():
            raise FileNotFoundError(f'{PTT_GPIO_DIR} 不存在，GPIO 可能已被占用或编号不正确')
        with open(PTT_GPIO_DIR / 'direction', 'w') as f:
            f.write('out')
        PTT_INITED = True
        PTT_LAST_ERROR = ''
        return True
    except Exception as e:
        PTT_LAST_ERROR = f'{type(e).__name__}: {e}'
        return False


def _ptt_set_level(active):
    """设置 PTT 电平，active=True 表示开发板输出音频、PTT 有效。"""
    global PTT_LEVEL, PTT_LAST_ERROR, PTT_ON_SINCE
    level = bool(active) if PTT_GPIO_ACTIVE_HIGH else (not bool(active))
    try:
        if not _ptt_ensure():
            return False
        with open(PTT_GPIO_DIR / 'value', 'w') as f:
            f.write('1' if level else '0')
        if bool(active) and not PTT_LEVEL:
            PTT_ON_SINCE = time.time()
        PTT_LEVEL = bool(active)
        _ptt_event('gpio_write', f'->{1 if level else 0}')
        return True
    except Exception as e:
        PTT_LAST_ERROR = f'{type(e).__name__}: {e}'
        return False


def _ptt_force_low():
    """立即拉低 PTT，取消 pending 释放定时器。"""
    global PTT_HOLD_COUNT, PTT_RELEASE_TIMER
    with PTT_LOCK:
        PTT_HOLD_COUNT = 0
        if PTT_RELEASE_TIMER is not None:
            try:
                PTT_RELEASE_TIMER.cancel()
            except Exception:
                pass
            PTT_RELEASE_TIMER = None
    _ptt_event('force_low')
    _ptt_set_level(False)


def _ptt_release_now():
    global PTT_RELEASE_TIMER
    with PTT_LOCK:
        PTT_RELEASE_TIMER = None
        if PTT_HOLD_COUNT > 0:
            _ptt_event('release_now', 'hold>0 取消拉低')
            return
        # 最短压发时间保护：不足 PTT_MIN_HOLD 就延后拉低，避免短音频把继电器来回 key
        left = PTT_MIN_HOLD - (time.time() - PTT_ON_SINCE) if PTT_ON_SINCE else 0.0
        if left > 0.02:
            _ptt_event('release_now', f'最短压发未到，延后 {left:.2f}s')
            PTT_RELEASE_TIMER = threading.Timer(left, _ptt_release_now)
            PTT_RELEASE_TIMER.daemon = True
            PTT_RELEASE_TIMER.start()
            return
    _ptt_set_level(False)


def _ptt_retain():
    """音频开始：引用计数 +1，第一个引用立即拉高 PTT。"""
    global PTT_HOLD_COUNT, PTT_RELEASE_TIMER
    with PTT_LOCK:
        PTT_HOLD_COUNT += 1
        if PTT_RELEASE_TIMER is not None:
            try:
                PTT_RELEASE_TIMER.cancel()
            except Exception:
                pass
            PTT_RELEASE_TIMER = None
        first = PTT_HOLD_COUNT == 1
    _ptt_event('retain', f'count={PTT_HOLD_COUNT}')
    if first:
        _ptt_set_level(True)


def _ptt_release():
    """音频结束：引用计数 -1，归零后延迟拉低，桥接流式 TTS 片段间隙。"""
    global PTT_HOLD_COUNT, PTT_RELEASE_TIMER
    with PTT_LOCK:
        if PTT_HOLD_COUNT > 0:
            PTT_HOLD_COUNT -= 1
        if PTT_HOLD_COUNT == 0:
            if PTT_RELEASE_TIMER is not None:
                try:
                    PTT_RELEASE_TIMER.cancel()
                except Exception:
                    pass
            _ptt_event('release', 'count=0，0.8s 后拉低')
            PTT_RELEASE_TIMER = threading.Timer(0.8, _ptt_release_now)
            PTT_RELEASE_TIMER.daemon = True
            PTT_RELEASE_TIMER.start()


@contextmanager
def ptt_audio_hold():
    """音频播放期间保持 PTT 高；多个并发/连续片段共用引用计数。"""
    _ptt_retain()
    try:
        yield
    finally:
        _ptt_release()


def _ptt_sysfs_read(name):
    try:
        p = PTT_GPIO_DIR / name
        return p.read_text(encoding='utf-8').strip() if p.exists() else ''
    except Exception as e:
        return f'ERR:{type(e).__name__}'


def ptt_status():
    with PTT_LOCK:
        count = PTT_HOLD_COUNT
        pending = PTT_RELEASE_TIMER is not None and count == 0
    return {
        'gpio': PTT_GPIO_NUM,
        'chip': f'gpiochip3',
        'line': 1,
        'active_high': PTT_GPIO_ACTIVE_HIGH,
        'high': bool(PTT_LEVEL),
        'hold_count': count,
        'release_pending': bool(pending),
        'error': PTT_LAST_ERROR,
        'sysfs': str(PTT_GPIO_DIR),
        # 实测回读：软件状态之外，直接读引脚值（硬件排查时用它判断"写没写下去"）
        'sysfs_value': _ptt_sysfs_read('value'),
        'direction': _ptt_sysfs_read('direction'),
        'manual_hold': bool(PTT_MANUAL.get('counted')),
        'on_since': round(PTT_ON_SINCE, 1) if PTT_LEVEL else 0,
    }


atexit.register(_ptt_force_low)


def _ptt_manual_start(client=''):
    """按住发射：引用计数 +1（幂等），并刷新心跳过期时间。"""
    if not PTT_MANUAL.get('counted'):
        _ptt_retain()
        PTT_MANUAL['counted'] = True
        PTT_MANUAL['since'] = time.time()
        print(f'[PTT] 手动发射开始（GPIO {PTT_GPIO_NUM} 拉高）', flush=True)
        try:
            audit('ptt_manual_start', f'gpio={PTT_GPIO_NUM}')
        except Exception:
            pass
    PTT_MANUAL['held'] = True
    PTT_MANUAL['client'] = client
    PTT_MANUAL['until'] = time.time() + PTT_MANUAL_HEARTBEAT
    _ptt_event('manual_heartbeat', f'client={client or "-"}')
    return True


def _ptt_manual_stop(reason=''):
    """松开手动发射（幂等）。"""
    if not PTT_MANUAL.get('counted'):
        PTT_MANUAL['held'] = False
        PTT_MANUAL['until'] = 0.0
        return False
    PTT_MANUAL['counted'] = False
    PTT_MANUAL['held'] = False
    PTT_MANUAL['until'] = 0.0
    _ptt_event('manual_stop', reason or 'release')
    try:
        _ptt_release()
    except Exception:
        pass
    print(f'[PTT] 手动发射结束（{reason or "release"}）', flush=True)
    try:
        audit('ptt_manual_stop', reason or 'release')
    except Exception:
        pass
    return True


def _ptt_manual_watchdog():
    """看门狗：心跳超时或超最长时限就强制松开，避免网页关掉后一直压着信道。"""
    while True:
        time.sleep(0.5)
        try:
            if not PTT_MANUAL.get('counted'):
                continue
            now = time.time()
            if now > float(PTT_MANUAL.get('until') or 0):
                print('[PTT] 手动发射心跳超时（页面可能已关闭），强制松开', flush=True)
                _ptt_manual_stop('heartbeat-timeout')
            elif now - float(PTT_MANUAL.get('since') or now) > PTT_MANUAL_MAX:
                print(f'[PTT] 手动发射超过 {PTT_MANUAL_MAX:.0f}s 上限，强制松开', flush=True)
                _ptt_manual_stop('max-hold')
        except Exception:
            pass


threading.Thread(target=_ptt_manual_watchdog, daemon=True).start()


def ptt_diag():
    """PTT 链路自检信息（硬件排查用）：软件状态 + sysfs 实测 + 手动发射状态。"""
    return {
        'ptt': ptt_status(),
        'gpio_num': PTT_GPIO_NUM,
        'chip': 'gpiochip3',
        'line': 1,
        'active_high': PTT_GPIO_ACTIVE_HIGH,
        'device': AUDIO_DEVICE,
        'manual': {
            'held': bool(PTT_MANUAL.get('held')),
            'counted': bool(PTT_MANUAL.get('counted')),
            'since': round(PTT_MANUAL.get('since') or 0, 1),
            'until': round(PTT_MANUAL.get('until') or 0, 1),
            'heartbeat': PTT_MANUAL_HEARTBEAT,
            'max_hold': PTT_MANUAL_MAX,
        },
        'events': list(PTT_EVENTS)[-60:],
    }


def _play_file_locked(path):
    global CURRENT_PLAY_PROC
    _ensure_audio_unmuted()
    with PLAY_LOCK:
        # 结束上一个播放（含流式朗读片段）：声卡同一时刻只允许一路 aplay，
        # 否则第二路会因设备忙直接失败，表现为「播放错误」/声音断续
        _stop_proc(CURRENT_PLAY_PROC)
        cmd = ['aplay', '-D', AUDIO_DEVICE, '-q', str(path)]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            CURRENT_PLAY_PROC = proc
            return proc
        except Exception:
            return None


def play_audio_async(path, ptt=False):
    if ptt:
        _ptt_retain()

    def _run():
        try:
            proc = _play_file_locked(path)
            if proc:
                try:
                    proc.wait(timeout=300)
                except Exception:
                    pass
        finally:
            if ptt:
                _ptt_release()
    try:
        threading.Thread(target=_run, daemon=True).start()
    except Exception:
        if ptt:
            _ptt_release()
        raise


# ---------------------------------------------------------------------------
# 定时重启计划：每天多个 HH:MM，到点前先语音播报，准点重启
#
# 重启需要 root，而本服务跑在 elf 用户下：走 /usr/local/sbin/elf2-reboot.sh 的
# sudoers 白名单（部署文件 board/deploy/elf2-reboot.sh + 99-elf2-reboot.sudoers）。
# helper 支持 --check，用于在不重启的前提下验证 sudoers 配没配好。
# ---------------------------------------------------------------------------
REBOOT_HELPER = '/usr/local/sbin/elf2-reboot.sh'
REBOOT_MIN_UPTIME = 300     # 开机 5 分钟内不触发，避免「重启后补触发」滚成重启循环
REBOOT_FIRE_WINDOW = 90     # 到点后多久内仍允许触发（秒）
BOOT_TS = time.time()
_reboot_state = {}


def _reboot_times():
    """解析设置里的 HH:MM 列表，返回排序去重后的 [(h, m)]。"""
    raw = _setting_direct('reboot_times', '') or ''
    out = []
    for part in re.split(r'[,;，；\s]+', raw):
        m = re.match(r'^(\d{1,2}):(\d{2})$', part.strip())
        if not m:
            continue
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59 and (h, mi) not in out:
            out.append((h, mi))
    return sorted(out)


def _reboot_helper(args=None):
    """同步调用重启 helper，返回 (ok, 输出)。带 --check 时只验证权限不重启。"""
    cmd = ['sudo', '-n', REBOOT_HELPER] + list(args or [])
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=15)
        return p.returncode == 0, p.stdout.decode('utf-8', 'replace').strip()
    except Exception as e:
        return False, f'{type(e).__name__}: {e}'


def _reboot_announce(text):
    """阻塞播报（AUX + PTT），返回 (ok, err)。重启前通告必须等它播完。"""
    try:
        path = tts_service.synthesize_multilingual(
            text,
            _setting_direct('tts_local_voice', 'zh_CN-huayan-medium'),
            en_voice=(_setting_direct('tts_en_voice', '') or None),
            icao=str(_setting_direct('tts_icao', '1')) in ('1', 'true', 'True', 'on'),
            icao_voice=(_setting_direct('tts_icao_voice', '') or None))
    except Exception as e:
        return False, f'合成失败：{e}'
    _ptt_retain()
    try:
        proc = _play_file_locked(path)
        if proc:
            try:
                proc.wait(timeout=120)
            except Exception:
                _stop_proc(proc)
    finally:
        _ptt_release()
    return True, ''


def _reboot_scheduler():
    time.sleep(30)          # 等服务起来，别在启动风暴里抢资源
    while True:
        try:
            if (str(_setting_direct('reboot_enabled', '0')) in ('1', 'true', 'True', 'on')
                    and (time.time() - BOOT_TS) > REBOOT_MIN_UPTIME):
                now = datetime.now()
                notice = int(float(_setting_direct('reboot_notice_sec', '30') or 30))
                text = _setting_direct('reboot_text', '') or '中继台即将重启，请稍候。'
                for (h, mi) in _reboot_times():
                    due = now.replace(hour=h, minute=mi, second=0, microsecond=0)
                    key = due.strftime('%Y-%m-%d %H:%M')
                    st = _reboot_state.setdefault(key, {'announced': False, 'fired': False})
                    delta = (now - due).total_seconds()
                    # 播报窗：due - notice <= now < due
                    if not st['announced'] and -notice <= delta < 0:
                        st['announced'] = True
                        ok, err = _reboot_announce(text)
                        print(f'[REBOOT] {key} 播报{"成功" if ok else "失败 " + err}', flush=True)
                    # 触发窗：due <= now < due + 90s
                    if not st['fired'] and 0 <= delta < REBOOT_FIRE_WINDOW:
                        _ptt_force_low()
                        ok, out = _reboot_helper()
                        st['fired'] = ok
                        print(f'[REBOOT] {key} 触发重启 ok={ok} {out}', flush=True)
                today = now.strftime('%Y-%m-%d')
                for k in [k for k in _reboot_state if not k.startswith(today)]:
                    _reboot_state.pop(k, None)
        except Exception as e:
            print(f'[REBOOT] 调度异常: {e}', flush=True)
        time.sleep(20)


threading.Thread(target=_reboot_scheduler, daemon=True).start()


@app.route('/api/reboot/check')
@login_required
@admin_required
def api_reboot_check():
    """只验证 sudoers 权限，不重启。"""
    ok, out = _reboot_helper(['--check'])
    return api_ok(ok=ok, output=out, helper=REBOOT_HELPER,
                  times=[f'{h:02d}:{m:02d}' for h, m in _reboot_times()])


@app.route('/api/reboot/now', methods=['POST'])
@login_required
@admin_required
def api_reboot_now():
    """手动立即重启。先同步验证权限，再延迟下发，好让响应能发出去。"""
    ok, out = _reboot_helper(['--check'])
    if not ok:
        return api_err(f'重启权限未就绪（检查 sudoers）：{out}')
    audit('reboot_now', 'manual')
    _ptt_force_low()

    def _go():
        time.sleep(1.5)
        _reboot_helper()

    threading.Thread(target=_go, daemon=True).start()
    return api_ok(ok=True, note='已下发重启命令，连接会中断')


# ---------------------------------------------------------------------------
# 开发板 3.5mm 耳机/麦克风输入采集：电平监视 + 实时 PCM 流到网页
# ---------------------------------------------------------------------------
def _amixer_set(control, value):
    try:
        subprocess.run(['amixer', '-c', '1', 'sset', control, str(value)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
    except Exception:
        pass


def _apply_mic_settings(source=None, channel=None,
                        pga_percent=None, adc_percent=None, pga_boost=None,
                        l2r2_percent=None, aux_boost_percent=None):
    """应用麦克风输入源、ADC 声道和采集增益。"""
    if source is None:
        source = _setting_direct('mic_source', 'main')
    if channel is None:
        channel = _setting_direct('mic_channel', 'right')
    if pga_percent is None:
        pga_percent = int(float(_setting_direct('mic_pga_percent', '25') or 25))
    if adc_percent is None:
        adc_percent = int(float(_setting_direct('mic_adc_percent', '100') or 100))
    if pga_boost is None:
        pga_boost = _setting_direct('mic_pga_boost', '1') in ('1', 'true', 'True', 'on')
    if l2r2_percent is None:
        l2r2_percent = int(float(_setting_direct('mic_l2r2_percent', '0') or 0))
    if aux_boost_percent is None:
        aux_boost_percent = int(float(_setting_direct('mic_aux_boost_percent', '0') or 0))

    source = 'headset' if source == 'headset' else 'main'
    channel = channel if channel in ('left', 'right', 'mix') else 'right'
    pga_percent = max(0, min(100, int(pga_percent)))
    adc_percent = max(0, min(100, int(adc_percent)))
    l2r2_percent = max(0, min(100, int(l2r2_percent)))
    aux_boost_percent = max(0, min(100, int(aux_boost_percent)))

    _amixer_set('Main Mic', 'on' if source == 'main' else 'off')
    _amixer_set('Headset Mic', 'on' if source == 'headset' else 'off')
    _amixer_set('PGA', str(round(pga_percent / 100.0 * 63)))
    _amixer_set('ADC', str(round(adc_percent / 100.0 * 255)))
    _amixer_set('PGA Boost', '1' if pga_boost else '0')
    _amixer_set('L2/R2 Boost', str(round(l2r2_percent / 100.0 * 7)))
    _amixer_set('Aux Boost', str(round(aux_boost_percent / 100.0 * 7)))
    with MIC_CAPTURE_LOCK:
        MIC_CAPTURE['channel'] = channel
        MIC_CAPTURE['source'] = source
    return {
        'source': source,
        'channel': channel,
        'pga_percent': pga_percent,
        'adc_percent': adc_percent,
        'pga_boost': bool(pga_boost),
        'l2r2_percent': l2r2_percent,
        'aux_boost_percent': aux_boost_percent,
    }


def _read_mic_settings():
    return {
        'source': _setting_direct('mic_source', 'main'),
        'channel': _setting_direct('mic_channel', 'right'),
        'pga_percent': int(float(_setting_direct('mic_pga_percent', '25') or 25)),
        'adc_percent': int(float(_setting_direct('mic_adc_percent', '100') or 100)),
        'pga_boost': _setting_direct('mic_pga_boost', '1') in ('1', 'true', 'True', 'on'),
        'l2r2_percent': int(float(_setting_direct('mic_l2r2_percent', '0') or 0)),
        'aux_boost_percent': int(float(_setting_direct('mic_aux_boost_percent', '0') or 0)),
        'mixer': {
            'PGA': _amixer_sget('PGA'),
            'ADC': _amixer_sget('ADC'),
            'PGA Boost': _amixer_sget('PGA Boost'),
            'L2/R2 Boost': _amixer_sget('L2/R2 Boost'),
            'Aux Boost': _amixer_sget('Aux Boost'),
            'Main Mic': _amixer_sget('Main Mic'),
            'Headset Mic': _amixer_sget('Headset Mic'),
        },
    }


MIC_CAPTURE_LOCK = threading.Lock()
MIC_CAPTURE = {
    'running': False,
    'proc': None,
    'thread': None,
    'clients': set(),
    'rms': 0.0,
    'peak': 0,
    'dbfs': -120.0,
    'level': 0,
    'ts': 0.0,
    'device': AUDIO_DEVICE,
    'rate': 16000,
    'channels': 2,
    'sample_width': 2,
    'source': 'main',
    'channel': 'right',
    'left_rms': 0.0,
    'right_rms': 0.0,
    'left_peak': 0,
    'right_peak': 0,
}


def _mic_broadcast(chunk):
    with MIC_CAPTURE_LOCK:
        clients = list(MIC_CAPTURE['clients'])
    for q in clients:
        try:
            q.put_nowait(chunk)
        except queue.Full:
            try:
                q.get_nowait()
            except queue.Empty:
                pass
            try:
                q.put_nowait(chunk)
            except queue.Full:
                pass


def _rms_peak(samples):
    if not samples:
        return 0.0, 0
    rms = math.sqrt(sum(x * x for x in samples) / len(samples))
    peak = max(max(samples), abs(min(samples)))
    return rms, peak


def _mic_capture_loop():
    state = MIC_CAPTURE
    proc = state['proc']
    buf = b''
    chunk_size = 4096
    try:
        while True:
            data = proc.stdout.read(chunk_size) if proc and proc.stdout else b''
            if not data:
                break
            buf += data
            usable = len(buf) - (len(buf) % 4)  # 16-bit stereo = 4 bytes/frame
            if usable <= 0:
                continue
            chunk = buf[:usable]
            buf = buf[usable:]
            # 中继语音日志：原始立体声块交给触发状态机（BUSY/PTT 决定是否落盘）
            try:
                voice_service_instance.feed(chunk, time.time())
            except Exception:
                pass
            # APRS 常驻解码：挂在同一个采集中枢上（采集设备独占，
            # 不能再开第二路 arecord）。不受 BUSY/分段影响，全程监听。
            try:
                aprs_service_instance.feed(chunk, time.time())
            except Exception:
                pass
            # 中继语音助手：同样挂在采集中枢上。它自己按能量分段、
            # 自己判发射余波，绝不与语音日志/APRS 抢设备。
            try:
                assistant_service_instance.feed(chunk, time.time())
            except Exception:
                pass
            try:
                arr = array.array('h')
                arr.frombytes(chunk)
                left = arr[0::2]
                right = arr[1::2]
                left_rms, left_peak = _rms_peak(left)
                right_rms, right_peak = _rms_peak(right)
                channel = state.get('channel', 'right')
                if channel == 'left':
                    out = left
                elif channel == 'mix':
                    out = [(a + b) // 2 for a, b in zip(left, right)]
                else:
                    out = right
                out_arr = array.array('h', out)
                out_bytes = out_arr.tobytes()
                rms, peak = _rms_peak(out)
            except Exception:
                left_rms = right_rms = 0.0
                left_peak = right_peak = 0
                out_bytes = b''
                rms, peak = 0.0, 0
            dbfs = 20.0 * math.log10(max(rms, 1.0) / 32768.0)
            level = 0 if dbfs <= -60 else int(max(0, min(100, (dbfs + 60.0) / 60.0 * 100)))
            with MIC_CAPTURE_LOCK:
                state.update(rms=rms, peak=peak, dbfs=dbfs, level=level, ts=time.time(),
                             left_rms=left_rms, right_rms=right_rms,
                             left_peak=left_peak, right_peak=right_peak)
            if out_bytes:
                _mic_broadcast(out_bytes)
    except Exception:
        pass
    finally:
        with MIC_CAPTURE_LOCK:
            state['running'] = False
            state['proc'] = None
        _mic_broadcast(None)


def start_mic_capture():
    _apply_mic_settings()
    with MIC_CAPTURE_LOCK:
        if MIC_CAPTURE['running']:
            return True, 'already running'
        cmd = [
            'arecord', '-D', AUDIO_DEVICE,
            '-f', 'S16_LE', '-r', '16000', '-c', '2',
            '-t', 'raw', '-q', '-',
        ]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    stdin=subprocess.DEVNULL, bufsize=0)
        except Exception as e:
            return False, f'启动 arecord 失败：{e}'
        time.sleep(0.25)
        if proc.poll() is not None:
            return False, 'arecord 启动失败，请检查 3.5mm 耳机是否插入'
        MIC_CAPTURE.update(running=True, proc=proc, rms=0.0, peak=0, dbfs=-120.0,
                           level=0, ts=time.time(), clients=set(),
                           left_rms=0.0, right_rms=0.0, left_peak=0, right_peak=0)
        t = threading.Thread(target=_mic_capture_loop, daemon=True)
        MIC_CAPTURE['thread'] = t
        t.start()
        return True, 'started'


def stop_mic_capture(force=False):
    # 语音日志启用时采集中枢必须常驻：网页对讲关掉不代表录音可以停
    if not force and voice_service_instance.enabled():
        return True, u"语音日志启用中，采集中枢保持运行（录音依赖它）"
    with MIC_CAPTURE_LOCK:
        proc = MIC_CAPTURE.get('proc')
        MIC_CAPTURE['running'] = False
    if proc:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    with MIC_CAPTURE_LOCK:
        MIC_CAPTURE.update(rms=0.0, peak=0, dbfs=-120.0, level=0,
                           left_rms=0.0, right_rms=0.0, left_peak=0, right_peak=0)
    _mic_broadcast(None)
    return True, 'stopped'


def mic_level_payload():
    with MIC_CAPTURE_LOCK:
        return {
            'running': MIC_CAPTURE['running'],
            'rms': round(MIC_CAPTURE['rms'], 1),
            'peak': MIC_CAPTURE['peak'],
            'dbfs': round(MIC_CAPTURE['dbfs'], 1),
            'level': MIC_CAPTURE['level'],
            'left_rms': round(MIC_CAPTURE.get('left_rms', 0.0), 1),
            'right_rms': round(MIC_CAPTURE.get('right_rms', 0.0), 1),
            'left_peak': MIC_CAPTURE.get('left_peak', 0),
            'right_peak': MIC_CAPTURE.get('right_peak', 0),
            'source': MIC_CAPTURE.get('source', 'main'),
            'channel': MIC_CAPTURE.get('channel', 'right'),
            'ts': MIC_CAPTURE['ts'],
            'device': MIC_CAPTURE['device'],
            'rate': MIC_CAPTURE['rate'],
            'channels': MIC_CAPTURE['channels'],
            'sample_width': MIC_CAPTURE['sample_width'],
        }


def _mic_stream_queue():
    q = queue.Queue(maxsize=50)
    with MIC_CAPTURE_LOCK:
        MIC_CAPTURE['clients'].add(q)
    return q


def _mic_stream_unregister(q):
    with MIC_CAPTURE_LOCK:
        MIC_CAPTURE['clients'].discard(q)


def _mic_stream_generator(q):
    try:
        while True:
            try:
                chunk = q.get(timeout=5)
            except queue.Empty:
                with MIC_CAPTURE_LOCK:
                    if not MIC_CAPTURE['running']:
                        break
                continue
            if chunk is None:
                break
            yield chunk
    finally:
        _mic_stream_unregister(q)


def make_test_tone(path, seconds=1.2, freq=880.0, rate=16000):
    n = int(seconds * rate)
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(n):
            # 淡入淡出，避免爆音
            env = min(1.0, i / (0.05 * rate), (n - i) / (0.05 * rate))
            val = int(0.32 * 32767 * env * math.sin(2 * math.pi * freq * i / rate))
            frames += struct.pack('<h', val)
        w.writeframes(bytes(frames))


@app.route('/api/intercom/upload', methods=['POST'])
@login_required
def api_intercom_upload():
    if 'audio' not in request.files:
        return api_err('缺少 audio 文件')
    f = request.files['audio']
    if not f or not f.filename:
        return api_err('音频文件无效')
    if request.content_length and request.content_length > MAX_RECORDING_BYTES:
        return api_err('录音文件过大')
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    safe_user = re.sub(r'[^A-Za-z0-9_.-]', '_', session.get('username', 'user'))
    filename = f'rec_{ts}_{safe_user}.wav'
    path = RECORDINGS_DIR / filename
    f.save(str(path))
    size = path.stat().st_size
    # 基本 WAV 头校验，避免非 WAV 文件被直接播放
    if size < 44:
        path.unlink(missing_ok=True)
        return api_err('不是有效的 WAV 文件')
    with open(path, 'rb') as fp:
        head = fp.read(12)
        if head[:4] != b'RIFF' or head[8:12] != b'WAVE':
            path.unlink(missing_ok=True)
            return api_err('不是有效的 WAV 文件')
    duration_ms = 0
    try:
        with wave.open(str(path), 'rb') as w:
            duration_ms = int(1000 * w.getnframes() / max(1, w.getframerate()))
    except Exception:
        pass
    db_exec('INSERT INTO recordings(ts,username,filename,duration_ms,size_bytes,note) VALUES(?,?,?,?,?,?)',
            (now_iso(), session.get('username'), filename, duration_ms, size, 'web-intercom'))
    audit('intercom_upload', f'{filename} {size} bytes {duration_ms} ms')
    auto_play = bool_setting('record_auto_play', True)
    if auto_play:
        play_audio_async(path)
    return api_ok(filename=filename, size=size, duration_ms=duration_ms, auto_play=auto_play)


@app.route('/api/intercom/recordings')
@login_required
def api_intercom_recordings():
    rows = get_db().execute(
        'SELECT id,ts,username,filename,duration_ms,size_bytes,note FROM recordings ORDER BY id DESC LIMIT 50'
    ).fetchall()
    return api_ok(recordings=[dict(r) for r in rows])


@app.route('/api/intercom/play/<int:rid>', methods=['POST'])
@login_required
def api_intercom_play(rid):
    row = get_db().execute('SELECT filename FROM recordings WHERE id=?', (rid,)).fetchone()
    if not row:
        return api_err('录音不存在', 404)
    path = RECORDINGS_DIR / secure_filename(row['filename'])
    if not path.exists():
        return api_err('录音文件已丢失', 404)
    play_audio_async(path)
    return api_ok(playing=row['filename'])


@app.route('/api/intercom/test-tone', methods=['POST'])
@login_required
def api_intercom_test_tone():
    path = RECORDINGS_DIR / '_test_tone.wav'
    make_test_tone(path)
    play_audio_async(path)
    return api_ok(playing='_test_tone.wav', device=AUDIO_DEVICE)


@app.route('/api/intercom/status')
@login_required
def api_intercom_status():
    global CURRENT_PLAY_PROC
    playing = False
    try:
        playing = bool(CURRENT_PLAY_PROC and CURRENT_PLAY_PROC.poll() is None)
    except Exception:
        playing = False
    return api_ok(device=AUDIO_DEVICE, playing=playing, ptt=ptt_status())


@app.route('/api/ptt/status')
@login_required
def api_ptt_status():
    return api_ok(ptt=ptt_status(), busy=busy_status())


@app.route('/api/ptt/diag')
@login_required
def api_ptt_diag():
    """PTT 全链路自检信息（设置/校准页展示，硬件排查用）。"""
    return api_ok(**ptt_diag())


@app.route('/api/busy/status')
@login_required
def api_busy_status():
    """BUSY 接收状态（GPIO3_A5 / 全局 GPIO 101，低有效）。"""
    return api_ok(busy=busy_status())


@app.route('/api/busy/diag')
@login_required
def api_busy_diag():
    """BUSY 链路自检：原始电平、电平沿计数、触发事件。"""
    return api_ok(**busy_diag())


@app.route('/api/busy/polarity', methods=['POST'])
@login_required
@admin_required
def api_busy_polarity():
    """设置 BUSY 有效极性（1=低有效 / 0=高有效），立即生效并重新判定当前状态。"""
    data = request.get_json(silent=True) or {}
    if 'active_low' not in data:
        return api_err('缺少 active_low 参数')
    val = '1' if data['active_low'] in (True, '1', 1, 'true', 'on') else '0'
    set_setting('busy_active_low', val)
    with BUSY_LOCK:
        BUSY_STATE['active'] = _busy_active_from_level(BUSY_STATE.get('level'))
        BUSY_STATE['since'] = time.time() if BUSY_STATE['active'] else 0.0
    audit('busy_polarity', f'active_low={val}')
    return api_ok(busy=busy_status())


@app.route('/api/ptt/manual', methods=['POST'])
@login_required
@admin_required
def api_ptt_manual():
    """设置/校准页「按住发射」：手动拉高 PTT 排查硬件，不接音频。

    body: {"hold": true}  按住（前端每 1s 续一次心跳，3s 无心跳自动松开）
          {"hold": false} 松开
    """
    data = request.get_json(silent=True) or {}
    hold = bool(data.get('hold', True))
    client = str(data.get('client_id') or '')[:64]
    if hold:
        _ptt_manual_start(client)
    else:
        _ptt_manual_stop(str(data.get('reason') or 'user-release')[:40])
    return api_ok(**ptt_diag())


# ---------------------------------------------------------------------------
# 实时对讲：网页麦克风 → 板端 3.5mm AUX（连续推流 + 自动 PTT）
# ---------------------------------------------------------------------------
INTERCOM_PUSH = {
    'proc': None,
    'token': None,
    'lock': threading.Lock(),
    'last': 0.0,
    'started': 0.0,
    'bytes': 0,
    'ptt': False,
    'restarts': 0,
}
INTERCOM_PUSH_SR = int(os.environ.get('RELAY_INTERCOM_RATE', '16000') or 16000)
INTERCOM_PUSH_IDLE = float(os.environ.get('RELAY_INTERCOM_IDLE', '8') or 8)


def _intercom_push_stop(reason='', release_ptt=True, wait=2.0):
    """停止推流：关闭 aplay。

    release_ptt=False 用于「aplay 意外退出后重启」：只换播放进程，PTT 保持不松手。
    否则每重启一次就 release+retain 一次，听感/观感就是「PTT 反复触发」。
    """
    st = INTERCOM_PUSH
    proc = st.get('proc')
    st['proc'] = None
    if proc is not None:
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=wait)
        except Exception:
            _stop_proc(proc)
    if release_ptt and st.get('ptt'):
        try:
            _ptt_release()
        except Exception:
            pass
        st['ptt'] = False
    if reason:
        try:
            audit('intercom_push_stop', reason)
        except Exception:
            pass


def _intercom_push_watchdog():
    """看门狗：静默超过 INTERCOM_PUSH_IDLE 秒强制停止，避免 PTT 卡死。"""
    while True:
        time.sleep(1.0)
        try:
            st = INTERCOM_PUSH
            if st.get('proc') is not None and (time.time() - st.get('last', 0)) > INTERCOM_PUSH_IDLE:
                with st['lock']:
                    _intercom_push_stop('idle-timeout')
        except Exception:
            pass


threading.Thread(target=_intercom_push_watchdog, daemon=True).start()


@app.route('/api/intercom/push', methods=['POST'])
@login_required
def api_intercom_push():
    """接收网页麦克风 PCM（S16LE / 16k / mono，每块约 250ms）。
    第一块启动 aplay 并拉起 PTT，最后用 ?end=1 收尾。"""
    token = (request.args.get('token') or 'default')[:64]
    end = request.args.get('end') in ('1', 'true', 'yes')
    data = request.get_data(cache=False) or b''
    if len(data) % 2:
        data = data[:-1]
    st = INTERCOM_PUSH
    with st['lock']:
        if end:
            sent = st['bytes'] if st.get('token') == token else 0
            _intercom_push_stop('client-end')
            st['bytes'] = 0
            return api_ok(ended=True, sent=sent)
        if st['proc'] is None or st.get('token') != token or st['proc'].poll() is not None:
            died = st['proc'] is not None and st['proc'].poll() is not None
            if died:
                st['restarts'] = st.get('restarts', 0) + 1
                if st['restarts'] in (1, 2, 5, 10, 50, 100):
                    print(f'[INTERCOM] aplay 意外退出，第 {st["restarts"]} 次重启（PTT 保持不松）',
                          flush=True)
            # 死掉的 aplay 只换进程，不松 PTT（同一次发言里 PTT 连续保持）
            _intercom_push_stop('restart', release_ptt=False, wait=0.4)
            if not st.get('ptt'):
                try:
                    _ptt_retain()
                except Exception as e:
                    return api_err(f'PTT 占用失败：{e}', 500)
                st['ptt'] = True
            cmd = ['aplay', '-D', AUDIO_DEVICE, '-q', '-t', 'raw',
                   '-f', 'S16_LE', '-r', str(INTERCOM_PUSH_SR), '-c', '1']
            try:
                st['proc'] = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                _ptt_release()
                return api_err(f'启动音频输出失败：{e}', 500)
            st['token'] = token
            st['ptt'] = True
            st['bytes'] = 0
            st['started'] = time.time()
        if data:
            try:
                st['proc'].stdin.write(data)
                st['proc'].stdin.flush()
            except Exception as e:
                # aplay 中途退出（设备被抢占/出错）：只丢弃本块并重启播放进程，
                # PTT 继续保持，客户端不需要中断重来
                _intercom_push_stop('write-error', release_ptt=False, wait=0.3)
                st['restarts'] = st.get('restarts', 0) + 1
                st['last'] = time.time()        # 别让看门狗把这次发言判成"空闲"
                print(f'[INTERCOM] 写入失败({type(e).__name__})，重启 aplay 并保持 PTT', flush=True)
                return api_ok(sent=0, total=st.get('bytes', 0), restarted=True,
                              restarts=st['restarts'])
            st['bytes'] += len(data)
        st['last'] = time.time()
        total = st['bytes']
    return api_ok(sent=len(data), total=total, active=True)


@app.route('/api/intercom/push/status')
@login_required
def api_intercom_push_status():
    st = INTERCOM_PUSH
    now = time.time()
    return api_ok(active=st.get('proc') is not None, total=st.get('bytes', 0),
                  restarts=st.get('restarts', 0),
                  seconds=round(now - st['started'], 1) if st.get('started') else 0,
                  idle=round(now - st['last'], 1) if st.get('last') else 0,
                  ptt=ptt_status(), device=AUDIO_DEVICE, rate=INTERCOM_PUSH_SR)


@app.route('/api/intercom/push/stop', methods=['POST'])
@login_required
def api_intercom_push_stop():
    st = INTERCOM_PUSH
    with st['lock']:
        sent = st.get('bytes', 0)
        _intercom_push_stop('api-stop')
        st['bytes'] = 0
    return api_ok(stopped=True, sent=sent)


# ---------------------------------------------------------------------------
# TTS：本地 Piper + 外部 OpenAI 兼容语音 API
# ---------------------------------------------------------------------------
def _tts_external_apis():
    """外部 TTS 已下线，保留桩函数仅为兼容旧调用点。"""
    return []


@app.route('/api/tts/providers')
@login_required
def api_tts_providers():
    # 只保留本地 Piper 模型：不再提供外部 OpenAI 兼容 TTS 选项
    return api_ok(
        current='local',
        auto_speak=bool_setting('tts_auto_speak', False),
        local={
            'id': 'local',
            'name': '本地 Piper（板端离线）',
            'provider': 'local',
            'voice': get_setting('tts_local_voice', 'zh_CN-huayan-medium'),
            'installed': tts_service.PIPER_BIN.exists(),
        },
        external=[],
        voices=tts_service.list_voices(),
    )


@app.route('/api/tts/voices')
@login_required
def api_tts_voices():
    return api_ok(voices=tts_service.list_voices())


@app.route('/api/tts/voice/<voice_id>', methods=['DELETE'])
@login_required
@admin_required
def api_tts_voice_delete(voice_id):
    """删除音色包（内置基座音色受保护）。"""
    try:
        result = tts_service.delete_voice(voice_id)
    except Exception as e:
        return api_err(f'删除音色失败：{e}', 400)
    audit('tts_delete_voice', voice_id)
    return api_ok(result=result, voices=tts_service.list_voices())


@app.route('/api/tts/speak', methods=['POST'])
@login_required
def api_tts_speak():
    data = request.get_json(silent=True) or {}
    text = (data.get('text') or '').strip()
    # 只允许本地 Piper（外部 TTS 已下线；请求里带 provider/model 也会被忽略）
    provider = 'local'
    voice = data.get('voice') or get_setting('tts_local_voice', 'zh_CN-huayan-medium')
    en_voice = (data.get('en_voice') or get_setting('tts_en_voice', '') or '').strip() or None
    icao = data.get('icao')
    icao = bool_setting('tts_icao', True) if icao is None else str(icao) in ('1', 'true', 'True', 'on')
    auto_play = bool(data.get('auto_play', True))
    if not text:
        return api_err('朗读文本不能为空')
    if len(text) > 2000:
        return api_err('朗读文本过长（最多 2000 字）')
    icao_voice = (data.get('icao_voice') or get_setting('tts_icao_voice', '') or '').strip() or None
    try:
        # 中英混读：按语言分段，各用对应音色后拼接；ICAO 开启时先展开呼号
        wav_path = tts_service.synthesize_multilingual(
            text, voice, en_voice=en_voice, icao=icao, icao_voice=icao_voice)
    except Exception as e:
        return api_err(f'TTS 合成失败：{e}', 502)
    # 保存为可回放录音
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    out_name = f'tts_{ts}_{uuid.uuid4().hex[:6]}.wav'
    out_path = RECORDINGS_DIR / out_name
    try:
        shutil.copyfile(wav_path, out_path)
        size = out_path.stat().st_size
        duration_ms = 0
        try:
            with wave.open(str(out_path), 'rb') as w:
                duration_ms = int(1000 * w.getnframes() / max(1, w.getframerate()))
        except Exception:
            pass
        db_exec('INSERT INTO recordings(ts,username,filename,duration_ms,size_bytes,note) VALUES(?,?,?,?,?,?)',
                (now_iso(), session.get('username'), out_name, duration_ms, size, f'tts:{provider}'))
    except Exception:
        pass
    if auto_play:
        play_audio_async(out_path, ptt=True)
    audit('tts_speak', f'{provider} voice={voice} len={len(text)}')
    return api_ok(filename=out_name, provider=provider, voice=voice,
                  size=out_path.stat().st_size if out_path.exists() else 0,
                  auto_play=auto_play)


@app.route('/api/tts/upload_voice', methods=['POST'])
@login_required
@admin_required
def api_tts_upload_voice():
    if 'voice_pack' not in request.files:
        return api_err('请上传音色包 zip（包含 model.onnx 与 model.onnx.json）')
    f = request.files['voice_pack']
    voice_id = request.form.get('voice_id') or (Path(f.filename or 'voice').stem)
    try:
        result = tts_service.save_voice_zip(f, voice_id)
    except Exception as e:
        return api_err(f'音色包上传失败：{e}', 400)
    audit('tts_upload_voice', f'{result["id"]}')
    return api_ok(result=result, voices=tts_service.list_voices())


@app.route('/recordings/<path:filename>')
@login_required
def recordings_file(filename):
    safe = secure_filename(filename)
    path = RECORDINGS_DIR / safe
    if not path.exists():
        abort(404)
    return send_file(str(path), mimetype='audio/wav')


# ---------------------------------------------------------------------------
# 系统全局音量控制
# ---------------------------------------------------------------------------
@app.route('/api/audio/volume')
@login_required
@admin_required
def api_audio_volume_get():
    hp = _amixer_sget('Headphone') or {}
    sp = _amixer_sget('Speaker') or {}
    pcm = _amixer_sget('PCM') or {}
    return api_ok(
        headphone=hp,
        speaker=sp,
        pcm=pcm,
        percent=int(float(_setting_direct('audio_volume_percent', '80') or 80)),
        muted=_setting_direct('audio_muted', '0') in ('1', 'true', 'True', 'on'),
        device=AUDIO_DEVICE,
    )


@app.route('/api/audio/volume', methods=['POST'])
@login_required
@admin_required
def api_audio_volume_set():
    data = request.get_json(silent=True) or {}
    try:
        percent = int(float(data.get('percent', 80)))
    except (TypeError, ValueError):
        return api_err('音量必须是 0~100 的数字')
    if not (0 <= percent <= 100):
        return api_err('音量必须在 0~100 之间')
    muted = bool(data.get('muted', False))
    set_setting('audio_volume_percent', percent)
    set_setting('audio_muted', '1' if muted else '0')
    result = _apply_audio_volume(percent, muted)
    audit('audio_volume', f'percent={percent} muted={muted}')
    return api_ok(**result, headphone=_amixer_sget('Headphone'), speaker=_amixer_sget('Speaker'))


# ---------------------------------------------------------------------------
# TTS 流式朗读会话：LLM 边生成边合成、边播放
# ---------------------------------------------------------------------------
TTS_STREAMS = {}
TTS_STREAMS_LOCK = threading.Lock()
# 流式朗读空闲上限（秒）：超时无新片段/无播放则自动停会话并释放 PTT，防止长时间占用信道
TTS_STREAM_IDLE = float(os.environ.get('RELAY_TTS_STREAM_IDLE', '20') or 20)


def _cleanup_tts_cache(max_age=1800):
    try:
        for p in Path('/www/tts_cache').glob('*.wav'):
            try:
                if time.time() - p.stat().st_mtime > max_age:
                    p.unlink()
            except Exception:
                pass
    except Exception:
        pass


def _wav_seconds(path):
    """读取 WAV 时长（秒）；失败返回 0。"""
    try:
        with wave.open(str(path), 'rb') as w:
            return w.getnframes() / float(max(1, w.getframerate()))
    except Exception:
        return 0.0


def _play_audio_wait(sess, path):
    """流式朗读的单段播放。

    发射逻辑与「测试朗读（板端 AUX）」完全一致：
        _ptt_retain() -> _play_file_locked()（同一把 PLAY_LOCK，自动抢占上一路音频）
                      -> 播完 _ptt_release()
    区别只有 PTT 连续性：整条回复由会话级 PTT 保持，分片间隙不松手。
    """
    global CURRENT_PLAY_PROC
    _ensure_audio_unmuted()
    if not sess.get('active'):
        return
    _ptt_retain()
    proc = None
    try:
        proc = _play_file_locked(path)          # 与手动试听同一个播放函数
        if proc is None:
            sess['last_error'] = '启动音频输出失败（aplay 未能启动）'
            print('[TTS-STREAM] aplay 启动失败', flush=True)
            return
        sess['proc'] = proc
        # 播放限时：时长 + 15s 余量（未知时长时用 300s）。
        # aplay 卡死必须有人收尸，否则 PTT 会被一直占住、把信道堵死。
        dur = _wav_seconds(path)
        limit = 300.0 if dur <= 0 else min(300.0, max(20.0, dur + 15.0))
        try:
            proc.wait(timeout=limit)
        except Exception:
            print(f'[TTS-STREAM] aplay 超时 {limit:.0f}s，强制结束', flush=True)
            _stop_proc(proc)
    finally:
        if CURRENT_PLAY_PROC is proc:
            CURRENT_PLAY_PROC = None
        sess['proc'] = None
        _ptt_release()


def _stream_voice_opts(data=None):
    """流式朗读音色选项：请求参数优先，缺省读库。

    可在无 Flask 应用上下文的后台线程中调用，因此只能用 _setting_direct。
    返回 (en_voice, icao, icao_voice)。
    """
    data = data or {}

    def pick(key, default=''):
        val = data.get(key)
        if val is None or val == '':
            val = _setting_direct(key, default)
        return '' if val is None else str(val)

    en_voice = pick('en_voice').strip() or None
    icao_voice = pick('icao_voice').strip() or None
    icao_raw = data.get('icao')
    if icao_raw is None or icao_raw == '':
        icao = pick('tts_icao', '1') in ('1', 'true', 'True', 'yes', 'on')
    else:
        icao = str(icao_raw) in ('1', 'true', 'True', 'yes', 'on')
    return en_voice, icao, icao_voice


def _tts_stream_normalize_item(item):
    """兼容旧格式 (text, provider, voice) 与新格式 (text, voice, en, icao, icao_voice)。"""
    if not isinstance(item, (tuple, list)) or not item:
        return None
    if len(item) >= 5:
        text, voice = item[0], item[1]
        en_voice, icao, icao_voice = item[2], item[3], item[4]
    elif len(item) == 3:                      # 旧格式：中间一项是 provider，忽略
        text, voice = item[0], item[2]
        en_voice = icao = icao_voice = None
    else:
        text = item[0]
        voice = item[1] if len(item) > 1 else ''
        en_voice = icao = icao_voice = None
    return (str(text or ''), str(voice or ''), en_voice, icao, icao_voice)


def _tts_stream_synth_worker(sess):
    while True:
        item = sess['text_q'].get()
        if item is None:
            break
        if not sess.get('active'):
            break
        norm = _tts_stream_normalize_item(item)
        if not norm:
            continue
        text, voice, en_voice, icao, icao_voice = norm
        if not text:
            continue
        # 本函数运行在后台线程，没有 Flask 应用上下文：
        # 设置必须用 _setting_direct 读取（get_setting 会抛 RuntimeError，导致整段朗读被吞掉）
        if en_voice is None or icao is None or icao_voice is None:
            d_en, d_icao, d_icao_voice = _stream_voice_opts({})
            en_voice = d_en if en_voice is None else en_voice
            icao = d_icao if icao is None else icao
            icao_voice = d_icao_voice if icao_voice is None else icao_voice
        sess['last_activity'] = time.time()
        try:
            path = tts_service.synthesize_multilingual(
                text, voice, en_voice=en_voice, icao=icao, icao_voice=icao_voice)
            sess['play_q'].put(path)
            sess['last_activity'] = time.time()
        except Exception as e:
            sess['last_error'] = f'{type(e).__name__}: {e}'
            print(f'[TTS-STREAM] 合成失败：{sess["last_error"]}', flush=True)
    sess['play_q'].put(None)


def _tts_stream_play_worker(sess):
    while True:
        path = sess['play_q'].get()
        if path is None or not sess.get('active'):
            if path:
                try:
                    os.remove(path)
                except Exception:
                    pass
            break
        # 会话级 PTT：整段回复只发一次载波，片段间隙不松手
        if not sess.get('ptt_held'):
            try:
                _ptt_retain()
                sess['ptt_held'] = True
            except Exception as e:
                sess['last_error'] = f'PTT 使能失败: {e}'
        sess['last_activity'] = time.time()
        try:
            _play_audio_wait(sess, path)
        finally:
            try:
                os.remove(path)
            except Exception:
                pass
            sess['last_activity'] = time.time()
    # 队列结束 / 会话停止：释放会话级 PTT（dict.pop 保证只释放一次）
    if sess.pop('ptt_held', False):
        try:
            _ptt_release()
        except Exception:
            pass


def _tts_stream_stop(session_id):
    with TTS_STREAMS_LOCK:
        sess = TTS_STREAMS.pop(session_id, None)
    if not sess:
        return False
    sess['active'] = False
    _stop_proc(sess.get('proc'))            # TERM -> 1s -> KILL，立刻还回声卡
    try:
        sess['text_q'].put_nowait(None)
    except Exception:
        pass
    try:
        sess['play_q'].put_nowait(None)
    except Exception:
        pass
    # 立即释放会话级 PTT（播放线程退出时会兜底，dict.pop 保证不会重复释放）
    if sess.pop('ptt_held', False):
        try:
            _ptt_release()
        except Exception:
            pass
    return True


def _tts_stream_watchdog():
    """看门狗：流式会话长时间无新片段/无播放时自动停止，避免 PTT 一直占用信道。"""
    while True:
        time.sleep(2.0)
        try:
            now = time.time()
            with TTS_STREAMS_LOCK:
                items = list(TTS_STREAMS.items())
            for sid, sess in items:
                if not sess.get('active'):
                    continue
                # 正在播放、或仍有排队片段（含正在合成）时不算空闲：
                # 长句的合成+播放耗时会超过空闲阈值，不能据此判定会话卡死
                proc = sess.get('proc')
                if proc is not None and proc.poll() is None:
                    continue
                if not sess['text_q'].empty() or not sess['play_q'].empty():
                    continue
                if not sess['synth_thread'].is_alive() and not sess['play_thread'].is_alive():
                    # 已播完、线程退出的残余会话条目：静默回收（正常路径不会打印告警）
                    with TTS_STREAMS_LOCK:
                        TTS_STREAMS.pop(sid, None)
                    if sess.pop('ptt_held', False):
                        try:
                            _ptt_release()
                        except Exception:
                            pass
                    continue
                last = float(sess.get('last_activity') or sess.get('created') or now)
                if now - last > TTS_STREAM_IDLE:
                    print(f'[TTS-STREAM] 会话 {sid} 空闲 {now - last:.0f}s，自动停止并释放 PTT',
                          flush=True)
                    _tts_stream_stop(sid)
        except Exception:
            pass


threading.Thread(target=_tts_stream_watchdog, daemon=True).start()


@app.route('/api/tts/stream/start', methods=['POST'])
@login_required
def api_tts_stream_start():
    # 清理过期缓存，避免长期占用磁盘
    _cleanup_tts_cache()
    data = request.get_json(silent=True) or {}
    # 只停掉同一个客户端（浏览器标签页）的旧会话：
    # 否则别的标签页/另一台设备一开始朗读，就会把正在播的回复掐断（断音 + 反复 key）
    client = str(data.get('client_id') or '')[:64]
    with TTS_STREAMS_LOCK:
        old_ids = [sid for sid, s in TTS_STREAMS.items() if s.get('client', '') == client]
    for sid in old_ids:
        _tts_stream_stop(sid)
    sid = uuid.uuid4().hex[:12]
    sess = {
        'id': sid,
        'client': client,
        'text_q': queue.Queue(),
        'play_q': queue.Queue(),
        'active': True,
        'proc': None,
        'created': time.time(),
        'last_activity': time.time(),
        'last_error': '',
        'ptt_held': False,
    }
    sess['synth_thread'] = threading.Thread(target=_tts_stream_synth_worker, args=(sess,), daemon=True)
    sess['play_thread'] = threading.Thread(target=_tts_stream_play_worker, args=(sess,), daemon=True)
    sess['synth_thread'].start()
    sess['play_thread'].start()
    with TTS_STREAMS_LOCK:
        TTS_STREAMS[sid] = sess
    return api_ok(session_id=sid)


@app.route('/api/tts/stream/chunk', methods=['POST'])
@login_required
def api_tts_stream_chunk():
    data = request.get_json(silent=True) or {}
    sid = data.get('session_id')
    text = (data.get('text') or '').strip()
    if not sid or sid not in TTS_STREAMS:
        return api_err('TTS 流式会话不存在或已结束', 404)
    client = str(data.get('client_id') or '')[:64]
    sess_client = TTS_STREAMS[sid].get('client', '')
    if sess_client and sess_client != client:
        return api_err('TTS 流式会话不属于当前客户端', 409)
    if not text:
        return api_ok(queued=False)
    if len(text) > 2000:
        return api_err('单段文本过长')
    provider = data.get('provider') or get_setting('tts_provider', 'local')
    voice = data.get('voice') or get_setting('tts_local_voice', 'zh_CN-huayan-medium')
    # 音色选项：请求可覆盖（聊天页的英文音色 / ICAO 开关对流式朗读同样生效）
    en_voice, icao, icao_voice = _stream_voice_opts(data)
    try:
        sess = TTS_STREAMS[sid]
        sess['text_q'].put_nowait((text, voice, en_voice, icao, icao_voice))
        sess['last_activity'] = time.time()
        sess['last_error'] = ''
    except Exception as e:
        return api_err(f'入队失败：{e}', 500)
    return api_ok(queued=True, session_id=sid)


@app.route('/api/tts/stream/end', methods=['POST'])
@login_required
def api_tts_stream_end():
    data = request.get_json(silent=True) or {}
    sid = data.get('session_id')
    sess = TTS_STREAMS.get(sid)
    if not sess:
        return api_ok(ended=False)
    try:
        sess['text_q'].put_nowait(None)
    except Exception:
        pass

    def _cleanup():
        time.sleep(15)
        with TTS_STREAMS_LOCK:
            s = TTS_STREAMS.get(sid)
            if s and not s['synth_thread'].is_alive() and not s['play_thread'].is_alive():
                TTS_STREAMS.pop(sid, None)
                if s.pop('ptt_held', False):
                    try:
                        _ptt_release()
                    except Exception:
                        pass
    threading.Thread(target=_cleanup, daemon=True).start()
    return api_ok(ended=True, session_id=sid, last_error=sess.get('last_error') or '',
                  ptt=ptt_status())


@app.route('/api/tts/stream/status')
@login_required
def api_tts_stream_status():
    sid = request.args.get('session_id')
    with TTS_STREAMS_LOCK:
        if sid:
            items = [(sid, TTS_STREAMS[sid])] if sid in TTS_STREAMS else []
        else:
            items = list(TTS_STREAMS.items())
    now = time.time()
    return api_ok(sessions=[{
        'session_id': s['id'],
        'active': bool(s.get('active')),
        'queued_text': s['text_q'].qsize(),
        'queued_play': s['play_q'].qsize(),
        'ptt_held': bool(s.get('ptt_held')),
        'last_error': s.get('last_error') or '',
        'idle': round(now - float(s.get('last_activity') or s.get('created') or now), 1),
    } for _sid, s in items], ptt=ptt_status(), idle_limit=TTS_STREAM_IDLE)


@app.route('/api/tts/stream/stop', methods=['POST'])
@login_required
def api_tts_stream_stop():
    data = request.get_json(silent=True) or {}
    sid = data.get('session_id')
    if sid:
        ok = _tts_stream_stop(sid)
        return api_ok(stopped=ok)
    client = str(data.get('client_id') or '')[:64]
    with TTS_STREAMS_LOCK:
        if client:
            ids = [x for x, s in TTS_STREAMS.items() if s.get('client', '') == client]
        else:
            ids = list(TTS_STREAMS.keys())      # 不带 client_id：全部停止（应急全停）
    for x in ids:
        _tts_stream_stop(x)
    return api_ok(stopped=True, count=len(ids))


# ---------------------------------------------------------------------------
# 开发板麦克风采集控制 / 实时电平 / PCM 流
# ---------------------------------------------------------------------------
@app.route('/api/mic/settings', methods=['GET'])
@login_required
def api_mic_settings_get():
    return api_ok(settings=_read_mic_settings())


@app.route('/api/mic/settings', methods=['POST'])
@login_required
def api_mic_settings_set():
    data = request.get_json(silent=True) or {}
    source = data.get('source', _setting_direct('mic_source', 'main'))
    channel = data.get('channel', _setting_direct('mic_channel', 'right'))
    try:
        pga = int(data.get('pga_percent', _setting_direct('mic_pga_percent', '25')))
        adc = int(data.get('adc_percent', _setting_direct('mic_adc_percent', '100')))
        l2r2 = int(data.get('l2r2_percent', _setting_direct('mic_l2r2_percent', '0')))
        aux = int(data.get('aux_boost_percent', _setting_direct('mic_aux_boost_percent', '0')))
    except (TypeError, ValueError):
        return api_err('增益必须是数字')
    for name, val in [('PGA', pga), ('ADC', adc), ('L2/R2 Boost', l2r2), ('Aux Boost', aux)]:
        if not (0 <= val <= 100):
            return api_err(f'{name} 必须在 0~100 之间')
    if source not in ('main', 'headset'):
        return api_err('输入源必须是 main 或 headset')
    if channel not in ('left', 'right', 'mix'):
        return api_err('监听声道必须是 left/right/mix')
    pga_boost = bool(data.get('pga_boost', _setting_direct('mic_pga_boost', '1') in ('1', 'true', 'True', 'on')))
    for k, v in [('mic_source', source), ('mic_channel', channel),
                 ('mic_pga_percent', pga), ('mic_adc_percent', adc),
                 ('mic_pga_boost', '1' if pga_boost else '0'),
                 ('mic_l2r2_percent', l2r2), ('mic_aux_boost_percent', aux)]:
        set_setting(k, v)
    applied = _apply_mic_settings(source=source, channel=channel, pga_percent=pga,
                                  adc_percent=adc, pga_boost=pga_boost,
                                  l2r2_percent=l2r2, aux_boost_percent=aux)
    audit('mic_settings', json.dumps(applied, ensure_ascii=False))
    return api_ok(applied=applied, settings=_read_mic_settings(), level=mic_level_payload())


@app.route('/api/mic/capture/start', methods=['POST'])
@login_required
def api_mic_capture_start():
    ok, message = start_mic_capture()
    if not ok:
        return api_err(message, 500)
    audit('mic_capture_start', message)
    return api_ok(message=message, level=mic_level_payload())


@app.route('/api/mic/capture/stop', methods=['POST'])
@login_required
def api_mic_capture_stop():
    ok, message = stop_mic_capture()
    audit('mic_capture_stop', message)
    return api_ok(message=message, level=mic_level_payload())


@app.route('/api/mic/level')
@login_required
def api_mic_level():
    return api_ok(level=mic_level_payload())


@app.route('/api/mic/stream')
@login_required
def api_mic_stream():
    with MIC_CAPTURE_LOCK:
        if not MIC_CAPTURE['running']:
            return api_err('麦克风采集未启动，请先点击“开始采集”', 409)
    q = _mic_stream_queue()
    return Response(stream_with_context(_mic_stream_generator(q)),
                    mimetype='audio/L16; rate=16000; channels=1',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


# ---------------------------------------------------------------------------
# 摄像头 / 气象 预留
# ---------------------------------------------------------------------------
def _camera_cfg():
    return {
        'device': get_setting('camera_device', '/dev/video21'),
        'resolution': get_setting('camera_resolution', '640x480'),
        'fps': int(float(get_setting('camera_fps', '15') or 15)),
        'quality': int(float(get_setting('camera_quality', '5') or 5)),
        'record_dir': get_setting('camera_record_dir', '/www/camera_recordings'),
        'loop_seconds': int(float(get_setting('camera_loop_seconds', '60') or 60)),
        'loop_max_mb': int(float(get_setting('camera_loop_max_mb', '2048') or 2048)),
        'loop_max_files': int(float(get_setting('camera_loop_max_files', '100') or 100)),
        'storage_max_mb': int(float(get_setting('camera_storage_max_mb', '8192') or 8192)),
        'loop_autostart': bool_setting('camera_loop_autostart', True),
        'rtmp_url': get_setting('camera_rtmp_url', ''),
    }


def _camera_osd_cfg():
    return {
        'enabled': bool_setting('camera_osd_enabled', True),
        'text': get_setting('camera_osd_text', 'ELF2 RELAY'),
        'show_time': bool_setting('camera_osd_show_time', True),
        'position': get_setting('camera_osd_position', 'top-left'),
        'fontsize': int(float(get_setting('camera_osd_fontsize', '18') or 18)),
        'color': get_setting('camera_osd_color', 'white'),
    }


def _camera_record_dir():
    d = Path(_camera_cfg()['record_dir'])
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# 循环录像：开机自动启用 + 意外退出自愈
# 摄像头的核心业务是「常年循环录像」，实时预览只是旁路观察，因此：
#   * 开机（relay-web 启动）后自动拉起 采集 + 循环分段录像 + 容量清理线程；
#   * 每 30s 检查一次，录像进程意外退出会自动重新拉起；
#   * 用户在页面上手动点「停止循环录像」后，本次运行内不再自动拉起（重启恢复）。
# 环境变量：RELAY_CAM_AUTOSTART_DELAY 开机延迟秒数（默认 8s，等摄像头枚举完成）
# ---------------------------------------------------------------------------
CAM_LOOP_MANUAL_STOP = False
CAM_LOOP_WATCH_LOCK = threading.Lock()


def _cam_loop_running():
    try:
        st = camera_service.camera_service.status()
    except Exception:
        return False
    return bool(st.get('recording')) and str(st.get('recording_label') or '') == 'loop'


def _cam_loop_output(cfg):
    d = _camera_record_dir()
    seconds = max(5, int(cfg['loop_seconds']))
    pattern = str(d / 'loop_%Y%m%d_%H%M%S.mp4')
    out = ['-f', 'segment', '-segment_time', str(seconds), '-reset_timestamps', '1',
           '-strftime', '1', pattern]
    return out, seconds, d


def _cam_loop_ensure(reason='autostart'):
    '''确保采集进程 + 循环录像 + 清理线程都在运行。'''
    global CAM_LOOP_MANUAL_STOP
    with CAM_LOOP_WATCH_LOCK:
        if CAM_LOOP_MANUAL_STOP:
            return False, '用户已手动停止循环录像，跳过自愈'
        cfg = _camera_cfg()
        svc = camera_service.camera_service
        if not svc.status().get('running'):
            ok, msg = svc.start(cfg)
            if not ok:
                return False, msg
        out, seconds, d = _cam_loop_output(cfg)
        if not _cam_loop_running():
            ok, msg = svc.start_recording(cfg, out, camera_service.osd_filter(_camera_osd_cfg()), 'loop')
            if not ok:
                return False, msg
            print(f'[CAM] 循环录像已启动（{reason}，分段 {seconds}s）', flush=True)
            try:
                audit('camera_loop_start', f'{reason} segment={seconds}s')
            except Exception:
                pass
        svc.start_loop_cleaner(str(d), cfg['loop_max_mb'], cfg['loop_max_files'], cfg['storage_max_mb'])
        return True, 'running'


def _camera_autostart_worker():
    '''开机自动循环录像 + 掉线自愈。'''
    try:
        time.sleep(float(os.environ.get('RELAY_CAM_AUTOSTART_DELAY', '8') or 8))
    except Exception:
        time.sleep(8)
    fails = 0
    while True:
        try:
            if bool_setting('camera_loop_autostart', True):
                ok, msg = _cam_loop_ensure('boot' if fails == 0 else 'heal')
                if not ok:
                    fails += 1
                    if fails <= 3 or fails % 20 == 0:
                        print(f'[CAM] 自动循环录像失败：{msg}', flush=True)
                else:
                    fails = 0
        except Exception as e:
            print(f'[CAM] 自动循环录像异常: {type(e).__name__}: {e}', flush=True)
        time.sleep(30)


threading.Thread(target=_camera_autostart_worker, daemon=True).start()


def _camera_stream_generator(q):
    boundary = b'--frame'
    try:
        while True:
            frame = q.get(timeout=10)
            if frame is None:
                break
            yield (boundary + b'\r\nContent-Type: image/jpeg\r\nContent-Length: ' +
                   str(len(frame)).encode() + b'\r\n\r\n' + frame + b'\r\n')
    finally:
        camera_service.camera_service.remove_client(q)


_CAMERA_NAME_RE = re.compile(
    r'^(?P<kind>loop|manual|snapshot|record|rec)[_-](?P<date>\d{8})[_-](?P<time>\d{6})'
    r'(?:[_-](?P<seq>\d+))?\.(?P<ext>mp4|jpg|jpeg)$', re.I)
_CAMERA_MP4_CACHE = {}
_CAMERA_MP4_CACHE_LOCK = threading.Lock()


def _camera_kind(name):
    lower = str(name or '').lower()
    if lower.startswith('loop_'):
        return 'loop'
    if lower.startswith('manual_'):
        return 'manual'
    if lower.startswith('snapshot_'):
        return 'snapshot'
    if lower.startswith('record_') or lower.startswith('rec_'):
        return 'manual'
    return 'other'


def _camera_parse_name(name):
    """从录像文件名解析录制开始时间。支持 loop_/manual_/record_/rec_ + YYYYmmdd_HHMMSS。"""
    try:
        m = _CAMERA_NAME_RE.match(str(name or ''))
    except Exception:
        return None
    if not m:
        return None
    kind = m.group('kind').lower()
    if kind in ('record', 'rec'):
        kind = 'manual'
    try:
        dt = datetime.strptime(m.group('date') + m.group('time'), '%Y%m%d%H%M%S')
        ts = dt.timestamp()
    except Exception:
        return None
    return {'kind': kind, 'start_ms': int(ts * 1000), 'seq': m.group('seq')}


def _read_mp4_duration_ms(path):
    """直接解析 MP4 moov/mvhd box，避免依赖板端可能没有的 ffprobe。"""
    try:
        size = path.stat().st_size
        if size < 32:
            return None
        with open(path, 'rb') as f:
            def _box_at(off):
                f.seek(off)
                h = f.read(16)
                if len(h) < 8:
                    return None
                box_size = int.from_bytes(h[:4], 'big')
                box_type = h[4:8]
                header = 8
                if box_size == 1:
                    if len(h) < 16:
                        return None
                    box_size = int.from_bytes(h[8:16], 'big')
                    header = 16
                elif box_size == 0:
                    box_size = size - off
                return box_size, box_type, header

            def _parse(off, end, depth=0):
                if depth > 8:
                    return None
                while off + 8 <= end:
                    box = _box_at(off)
                    if not box:
                        return None
                    box_size, box_type, header = box
                    if box_size < header or off + box_size > size + 8:
                        return None
                    box_end = off + box_size
                    if box_type == b'moov':
                        found = _parse(off + header, box_end, depth + 1)
                        if found is not None:
                            return found
                    elif box_type == b'mvhd':
                        f.seek(off + header)
                        data = f.read(min(max(0, box_size - header), 120))
                        version = data[0] if data else 0
                        if version == 1 and len(data) >= 32:
                            timescale = int.from_bytes(data[20:24], 'big')
                            duration = int.from_bytes(data[24:32], 'big')
                        elif len(data) >= 20:
                            timescale = int.from_bytes(data[12:16], 'big')
                            duration = int.from_bytes(data[16:20], 'big')
                        else:
                            return None
                        if timescale > 0 and duration > 0:
                            return int(duration * 1000 / timescale)
                        return None
                    off = box_end
                return None

            return _parse(0, size)
    except Exception:
        return None


def _camera_mp4_duration_ms(path):
    try:
        st = path.stat()
    except Exception:
        return None
    key = str(path.resolve())
    with _CAMERA_MP4_CACHE_LOCK:
        cached = _CAMERA_MP4_CACHE.get(key)
        if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
            return cached[2]
    duration = _read_mp4_duration_ms(path)
    with _CAMERA_MP4_CACHE_LOCK:
        _CAMERA_MP4_CACHE[key] = (st.st_mtime_ns, st.st_size, duration)
        if len(_CAMERA_MP4_CACHE) > 2000:
            for k in list(_CAMERA_MP4_CACHE.keys())[:500]:
                _CAMERA_MP4_CACHE.pop(k, None)
    return duration


def _camera_recording_record(path, loop_seconds=60):
    try:
        st = path.stat()
    except Exception:
        return None
    kind = _camera_kind(path.name)
    parsed = _camera_parse_name(path.name)
    duration_ms = _camera_mp4_duration_ms(path)
    start_ms = parsed['start_ms'] if parsed else None
    if duration_ms is None:
        # MP4 尚未写 moov（正在录制中）或不是标准 MP4，用分段时长/mtime 估算
        if start_ms:
            elapsed_ms = max(1000, int((st.st_mtime - start_ms / 1000.0) * 1000))
            if kind == 'loop':
                duration_ms = min(elapsed_ms, max(1000, int(loop_seconds) * 1000))
            else:
                duration_ms = elapsed_ms
        else:
            duration_ms = 1000
    if start_ms is None:
        start_ms = max(0, int(st.st_mtime * 1000) - duration_ms)
    end_ms = start_ms + max(0, duration_ms)
    start_dt = datetime.fromtimestamp(start_ms / 1000.0)
    end_dt = datetime.fromtimestamp(end_ms / 1000.0)
    return {
        'filename': path.name,
        'type': kind,
        'kind': kind,
        'size': st.st_size,
        'mtime': st.st_mtime,
        'start_ms': start_ms,
        'end_ms': end_ms,
        'duration_ms': max(0, duration_ms),
        'start': start_dt.strftime('%Y-%m-%d %H:%M:%S'),
        'end': end_dt.strftime('%Y-%m-%d %H:%M:%S'),
        'start_hm': start_dt.strftime('%H:%M:%S'),
        'end_hm': end_dt.strftime('%H:%M:%S'),
        'date': start_dt.strftime('%Y-%m-%d'),
        'duration': max(0, round(duration_ms / 1000.0, 3)),
        'url': f'/api/camera/recordings/{path.name}',
    }


def _camera_list_recordings(limit=0):
    d = _camera_record_dir()
    try:
        loop_seconds = int(_camera_cfg().get('loop_seconds', 60) or 60)
    except Exception:
        loop_seconds = 60
    items = []
    try:
        paths = list(d.glob('*.mp4'))
    except Exception:
        paths = []
    for p in paths:
        record = _camera_recording_record(p, loop_seconds)
        if record:
            items.append(record)
    items.sort(key=lambda r: (r['start_ms'], r['filename']), reverse=True)
    ascending = sorted(items, key=lambda r: (r['start_ms'], r['filename']))
    for index, record in enumerate(ascending):
        record['index'] = index
        record['is_latest'] = index == len(ascending) - 1
    if limit and limit > 0:
        return items[:limit]
    return items


def _camera_storage_stats(recordings=None):
    """汇总磁盘、循环容量、存储容量上限等图形化所需数据。"""
    d = _camera_record_dir()
    cfg = _camera_cfg()
    recs = list(recordings) if recordings is not None else _camera_list_recordings()
    loop_recs = [r for r in recs if r.get('type') == 'loop']
    manual_recs = [r for r in recs if r.get('type') == 'manual']
    loop_bytes = sum(int(r.get('size') or 0) for r in loop_recs)
    manual_bytes = sum(int(r.get('size') or 0) for r in manual_recs)
    other_bytes = sum(int(r.get('size') or 0) for r in recs
                      if r.get('type') not in ('loop', 'manual'))
    snapshot_bytes = 0
    snapshot_count = 0
    try:
        for p in d.glob('snapshot_*.*'):
            if p.suffix.lower() in ('.jpg', '.jpeg'):
                try:
                    snapshot_bytes += p.stat().st_size
                    snapshot_count += 1
                except Exception:
                    continue
    except Exception:
        pass
    media_bytes = loop_bytes + manual_bytes + other_bytes + snapshot_bytes
    total_duration_ms = sum(int(r.get('duration_ms') or 0) for r in recs)
    try:
        disk = shutil.disk_usage(str(d))
        disk_total, disk_used, disk_free = int(disk.total), int(disk.used), int(disk.free)
    except Exception:
        disk_total = disk_used = disk_free = 0

    loop_cap_bytes = max(0, int(cfg.get('loop_max_mb') or 0)) * 1024 * 1024
    storage_cap_mb = max(0, int(cfg.get('storage_max_mb') or 0))
    storage_cap_bytes = storage_cap_mb * 1024 * 1024
    cap_source = 'setting'
    if storage_cap_bytes <= 0:
        storage_cap_bytes = disk_total
        cap_source = 'disk'
    elif disk_total > 0:
        storage_cap_bytes = min(storage_cap_bytes, disk_total)

    def _pct(value, cap):
        if cap <= 0:
            return 0.0
        return round(max(0.0, min(100.0, value * 100.0 / cap)), 2)

    days = {}
    for r in recs:
        key = r.get('date') or ''
        if not key:
            continue
        item = days.setdefault(key, {
            'date': key, 'count': 0, 'size': 0,
            'loop_count': 0, 'manual_count': 0,
            'duration_ms': 0,
        })
        item['count'] += 1
        item['size'] += int(r.get('size') or 0)
        item['duration_ms'] += int(r.get('duration_ms') or 0)
        if r.get('type') == 'loop':
            item['loop_count'] += 1
        elif r.get('type') == 'manual':
            item['manual_count'] += 1
    timeline_days = sorted(days.values(), key=lambda x: x['date'], reverse=True)

    oldest_ms = min([r.get('start_ms') for r in recs if r.get('start_ms')], default=0)
    newest_ms = max([r.get('end_ms') for r in recs if r.get('end_ms')], default=0)
    return {
        'path': str(d),
        'disk': {
            'total': disk_total,
            'used': disk_used,
            'free': disk_free,
            'percent': _pct(disk_used, disk_total),
        },
        'recordings': {
            'count': len(recs),
            'size': loop_bytes + manual_bytes + other_bytes,
            'loop_size': loop_bytes,
            'loop_count': len(loop_recs),
            'manual_size': manual_bytes,
            'manual_count': len(manual_recs),
            'snapshot_size': snapshot_bytes,
            'snapshot_count': snapshot_count,
            'media_size': media_bytes,
            'total_duration_ms': total_duration_ms,
            'oldest_ms': oldest_ms,
            'newest_ms': newest_ms,
        },
        'limits': {
            'loop_max_mb': int(cfg.get('loop_max_mb') or 0),
            'loop_max_bytes': loop_cap_bytes,
            'loop_max_files': int(cfg.get('loop_max_files') or 0),
            'storage_max_mb': storage_cap_mb,
            'storage_max_bytes': storage_cap_bytes,
            'storage_cap_source': cap_source,
        },
        'usage': {
            'disk_percent': _pct(disk_used, disk_total),
            'loop_percent': _pct(loop_bytes, loop_cap_bytes),
            'storage_percent': _pct(media_bytes, storage_cap_bytes),
            'snapshot_percent': _pct(snapshot_bytes, storage_cap_bytes),
            'free_bytes': max(0, storage_cap_bytes - media_bytes),
        },
        'timeline_days': timeline_days,
        'updated_ms': int(time.time() * 1000),
    }


@app.route('/api/camera/status')
@login_required
def api_camera_status():
    devices = [str(p) for p in sorted(Path('/dev').glob('video*'))]
    recs = _camera_list_recordings()
    return api_ok(
        status='ok',
        devices=devices,
        service=camera_service.camera_service.status(),
        settings=_camera_cfg(),
        osd=_camera_osd_cfg(),
        recordings_dir=str(_camera_record_dir()),
        recordings=recs[:200],
        storage=_camera_storage_stats(recs),
        loop_running=_cam_loop_running(),
        loop_autostart=bool_setting('camera_loop_autostart', True),
        loop_manual_stop=bool(CAM_LOOP_MANUAL_STOP),
    )


@app.route('/api/camera/start', methods=['POST'])
@login_required
def api_camera_start():
    ok, message = camera_service.camera_service.start(_camera_cfg())
    if not ok:
        return api_err(message, 500)
    return api_ok(message=message, service=camera_service.camera_service.status())


@app.route('/api/camera/stop', methods=['POST'])
@login_required
def api_camera_stop():
    camera_service.camera_service.stop_recording()
    camera_service.camera_service.stop_rtmp()
    camera_service.camera_service.stop_loop_cleaner()
    camera_service.camera_service.stop()
    global CAM_LOOP_MANUAL_STOP
    CAM_LOOP_MANUAL_STOP = True
    return api_ok(message='stopped')


@app.route('/api/camera/stream')
@login_required
def api_camera_stream():
    svc = camera_service.camera_service
    if not svc.status()['running']:
        ok, msg = svc.start(_camera_cfg())
        if not ok:
            return api_err(msg, 503)
    q = svc.add_client()
    return Response(stream_with_context(_camera_stream_generator(q)),
                    mimetype='multipart/x-mixed-replace; boundary=frame',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/internal/camera.mjpg')
def internal_camera_stream():
    # 仅供本机 ffmpeg 读取
    if request.remote_addr not in ('127.0.0.1', '::1'):
        abort(403)
    svc = camera_service.camera_service
    if not svc.status()['running']:
        ok, msg = svc.start(_camera_cfg())
        if not ok:
            return msg, 503
    q = svc.add_client()
    return Response(stream_with_context(_camera_stream_generator(q)),
                    mimetype='multipart/x-mixed-replace; boundary=frame',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/api/camera/settings', methods=['GET'])
@login_required
def api_camera_settings_get():
    return api_ok(settings=_camera_cfg(), osd=_camera_osd_cfg())


@app.route('/api/camera/settings', methods=['POST'])
@login_required
@admin_required
def api_camera_settings_set():
    data = request.get_json(silent=True) or {}
    mapping = {
        'device': 'camera_device',
        'resolution': 'camera_resolution',
        'fps': 'camera_fps',
        'quality': 'camera_quality',
        'record_dir': 'camera_record_dir',
        'loop_seconds': 'camera_loop_seconds',
        'loop_max_mb': 'camera_loop_max_mb',
        'loop_max_files': 'camera_loop_max_files',
        'storage_max_mb': 'camera_storage_max_mb',
        'rtmp_url': 'camera_rtmp_url',
    }
    for k, sk in mapping.items():
        if k in data:
            set_setting(sk, data[k])
    if 'loop_autostart' in data:
        set_setting('camera_loop_autostart',
                    '1' if data['loop_autostart'] in (True, '1', 'true', 'on', 1) else '0')
    osd = data.get('osd') or {}
    for k, sk in {
        'enabled': 'camera_osd_enabled',
        'text': 'camera_osd_text',
        'show_time': 'camera_osd_show_time',
        'position': 'camera_osd_position',
        'fontsize': 'camera_osd_fontsize',
        'color': 'camera_osd_color',
    }.items():
        if k in osd:
            set_setting(sk, '1' if osd[k] is True else ('0' if osd[k] is False else osd[k]))
    svc = camera_service.camera_service
    # 摄像头采集参数变化时重启采集
    if svc.status()['running']:
        svc.stop_recording()
        svc.stop_rtmp()
        svc.stop_loop_cleaner()
        svc.stop()
        camera_service.camera_service.start(_camera_cfg())
    # 改完参数后立刻恢复循环录像（原来要等 30s 自愈线程，会丢一段录像）
    if bool_setting('camera_loop_autostart', True):
        try:
            _cam_loop_ensure('settings-saved')
        except Exception:
            pass
    return api_ok(settings=_camera_cfg(), osd=_camera_osd_cfg())


@app.route('/api/camera/snapshot', methods=['POST'])
@login_required
def api_camera_snapshot():
    svc = camera_service.camera_service
    if not svc.status()['running']:
        ok, msg = svc.start(_camera_cfg())
        if not ok:
            return api_err(msg, 500)
    frame = svc.get_frame(timeout=5)
    if not frame:
        return api_err('获取摄像头帧失败', 500)
    d = _camera_record_dir()
    name = f'snapshot_{time.strftime("%Y%m%d_%H%M%S")}.jpg'
    path = d / name
    path.write_bytes(frame)
    return api_ok(filename=name, url=f'/api/camera/recordings/{name}', size=len(frame))


@app.route('/api/camera/record/manual/start', methods=['POST'])
@login_required
def api_camera_manual_start():
    d = _camera_record_dir()
    name = f'manual_{time.strftime("%Y%m%d_%H%M%S")}.mp4'
    path = d / name
    ok, msg = camera_service.camera_service.start_recording(
        _camera_cfg(),
        ['-f', 'mp4', '-movflags', '+faststart', str(path)],
        camera_service.osd_filter(_camera_osd_cfg()),
        'manual',
    )
    if not ok:
        return api_err(msg, 500)
    return api_ok(filename=name, path=str(path), message='recording started')


@app.route('/api/camera/record/manual/stop', methods=['POST'])
@login_required
def api_camera_manual_stop():
    ok, msg = camera_service.camera_service.stop_recording()
    return api_ok(message=msg, recordings=_camera_list_recordings())


@app.route('/api/camera/loop/start', methods=['POST'])
@login_required
def api_camera_loop_start():
    global CAM_LOOP_MANUAL_STOP
    CAM_LOOP_MANUAL_STOP = False
    cfg = _camera_cfg()
    out, seconds, d = _cam_loop_output(cfg)
    ok, msg = camera_service.camera_service.start_recording(
        cfg, out, camera_service.osd_filter(_camera_osd_cfg()), 'loop')
    if not ok:
        return api_err(msg, 500)
    camera_service.camera_service.start_loop_cleaner(
        str(d), cfg['loop_max_mb'], cfg['loop_max_files'], cfg['storage_max_mb'])
    audit('camera_loop_start', f'manual segment={seconds}s')
    return api_ok(message='loop recording started', segment_seconds=seconds)


@app.route('/api/camera/loop/stop', methods=['POST'])
@login_required
def api_camera_loop_stop():
    global CAM_LOOP_MANUAL_STOP
    CAM_LOOP_MANUAL_STOP = True
    audit('camera_loop_stop', 'manual')
    ok, msg = camera_service.camera_service.stop_recording()
    camera_service.camera_service.stop_loop_cleaner()
    return api_ok(message=msg, recordings=_camera_list_recordings())


@app.route('/api/camera/rtmp/start', methods=['POST'])
@login_required
def api_camera_rtmp_start():
    data = request.get_json(silent=True) or {}
    url = data.get('url') or get_setting('camera_rtmp_url', '')
    if not url:
        return api_err('RTMP 地址不能为空')
    set_setting('camera_rtmp_url', url)
    ok, msg = camera_service.camera_service.start_rtmp(
        _camera_cfg(), url, camera_service.osd_filter(_camera_osd_cfg()))
    if not ok:
        return api_err(msg, 500)
    return api_ok(message=msg, url=url)


@app.route('/api/camera/rtmp/stop', methods=['POST'])
@login_required
def api_camera_rtmp_stop():
    ok, msg = camera_service.camera_service.stop_rtmp()
    return api_ok(message=msg)


def _safe_camera_filename(filename):
    safe = secure_filename(str(filename or ''))
    if not safe or safe != Path(safe).name or safe in ('.', '..'):
        return None
    return safe


@app.route('/api/camera/recordings')
@login_required
def api_camera_recordings():
    try:
        limit = int(request.args.get('limit', '0') or 0)
    except Exception:
        limit = 0
    recs = _camera_list_recordings(limit=limit)
    return api_ok(recordings=recs, storage=_camera_storage_stats(recs))


@app.route('/api/camera/segments')
@login_required
def api_camera_segments():
    """分片管理接口：支持日期、类型、搜索、排序和分页。"""
    recs = _camera_list_recordings()
    date_filter = (request.args.get('date') or '').strip()
    type_filter = (request.args.get('type') or 'all').strip()
    query = (request.args.get('q') or '').strip().lower()
    sort = 'asc' if (request.args.get('sort') or 'desc').lower() == 'asc' else 'desc'
    try:
        page = max(1, int(request.args.get('page', '1') or 1))
    except Exception:
        page = 1
    try:
        page_size = max(1, min(1000, int(request.args.get('page_size', '200') or 200)))
    except Exception:
        page_size = 200

    filtered = []
    for r in recs:
        if date_filter and r.get('date') != date_filter:
            continue
        if type_filter not in ('', 'all') and r.get('type') != type_filter:
            continue
        if query and query not in r.get('filename', '').lower():
            continue
        filtered.append(r)
    filtered.sort(key=lambda r: (r.get('start_ms') or 0, r.get('filename') or ''),
                  reverse=(sort == 'desc'))
    total = len(filtered)
    start = (page - 1) * page_size
    page_items = filtered[start:start + page_size]
    dates = sorted({r.get('date') for r in recs if r.get('date')}, reverse=True)
    return api_ok(
        segments=page_items,
        total=total,
        page=page,
        page_size=page_size,
        dates=dates,
        storage=_camera_storage_stats(recs),
    )


@app.route('/api/camera/timeline')
@login_required
def api_camera_timeline():
    """按日期返回时间轴分片，附带分片统计和存储图形数据。"""
    date_filter = (request.args.get('date') or datetime.now().strftime('%Y-%m-%d')).strip()
    type_filter = (request.args.get('type') or 'all').strip()
    recs = _camera_list_recordings()
    segments = [r for r in recs if r.get('date') == date_filter
                and (type_filter in ('', 'all') or r.get('type') == type_filter)]
    segments.sort(key=lambda r: (r.get('start_ms') or 0, r.get('filename') or ''))
    total_size = sum(int(r.get('size') or 0) for r in segments)
    total_duration_ms = sum(int(r.get('duration_ms') or 0) for r in segments)
    return api_ok(
        date=date_filter,
        type=type_filter,
        segments=segments,
        segment_count=len(segments),
        total_size=total_size,
        total_duration_ms=total_duration_ms,
        storage=_camera_storage_stats(recs),
    )


@app.route('/api/camera/storage')
@login_required
def api_camera_storage():
    return api_ok(storage=_camera_storage_stats())


@app.route('/api/camera/recordings/cleanup', methods=['POST'])
@login_required
def api_camera_recordings_cleanup():
    cfg = _camera_cfg()
    d = _camera_record_dir()
    result = camera_service.clean_loop_dir(
        str(d), cfg['loop_max_mb'], cfg['loop_max_files'], cfg['storage_max_mb'])
    audit('camera_cleanup', json.dumps(result, ensure_ascii=False))
    return api_ok(cleanup=result, storage=_camera_storage_stats())


@app.route('/api/camera/recordings/batch_delete', methods=['POST'])
@login_required
def api_camera_recordings_batch_delete():
    data = request.get_json(silent=True) or {}
    names = data.get('files') if isinstance(data.get('files'), list) else []
    d = _camera_record_dir()
    deleted, missing, failed = [], [], []
    for name in names[:500]:
        safe = _safe_camera_filename(name)
        if not safe:
            failed.append(str(name))
            continue
        path = d / safe
        if not path.exists():
            missing.append(safe)
            continue
        if path.suffix.lower() not in ('.mp4', '.jpg', '.jpeg'):
            failed.append(safe)
            continue
        try:
            path.unlink()
            deleted.append(safe)
        except Exception:
            failed.append(safe)
    if deleted:
        audit('camera_batch_delete', ','.join(deleted))
    return api_ok(deleted=deleted, missing=missing, failed=failed,
                  storage=_camera_storage_stats())


@app.route('/api/camera/recordings/<path:filename>')
@login_required
def api_camera_recording_file(filename):
    safe = _safe_camera_filename(filename)
    if not safe:
        abort(404)
    path = _camera_record_dir() / safe
    if not path.exists():
        abort(404)
    download = request.args.get('download') in ('1', 'true', 'yes')
    return send_file(
        str(path),
        mimetype='video/mp4' if path.suffix.lower() == '.mp4' else 'image/jpeg',
        as_attachment=download,
        download_name=path.name if download else None,
    )


@app.route('/api/camera/recordings/<path:filename>', methods=['DELETE'])
@login_required
def api_camera_recording_delete(filename):
    safe = _safe_camera_filename(filename)
    if not safe:
        return api_err('文件不存在', 404)
    path = _camera_record_dir() / safe
    if not path.exists():
        return api_err('文件不存在', 404)
    try:
        path.unlink()
    except Exception as e:
        return api_err(f'删除失败：{e}', 500)
    audit('camera_delete', safe)
    return api_ok(deleted=safe, recordings=_camera_list_recordings(),
                  storage=_camera_storage_stats())


def _weather_settings():
    # 使用 _setting_direct，保证在启动阶段（无 Flask app context）也能读取
    return {
        'enabled': _setting_direct('weather_enabled', '1') in ('1', 'true', 'True', 'on'),
        'port': _setting_direct('weather_port', '/dev/ttyS9'),
        'baud': int(float(_setting_direct('weather_baud', '9600') or 9600)),
        'parity': _setting_direct('weather_parity', 'N'),
        'stopbits': int(float(_setting_direct('weather_stopbits', '1') or 1)),
        'timeout': float(_setting_direct('weather_timeout', '1.0') or 1.0),
        'slave': int(float(_setting_direct('weather_slave', '1') or 1)),
        'function': int(float(_setting_direct('weather_function', '3') or 3)),
        'register': int(float(_setting_direct('weather_register', '0') or 0)),
        'quantity': int(float(_setting_direct('weather_quantity', '1') or 1)),
        'scale': float(_setting_direct('weather_scale', '0.1') or 0.1),
        'poll_interval': float(_setting_direct('weather_poll_interval', '2') or 2),
        'rain_enabled': _setting_direct('rain_enabled', '1') in ('1', 'true', 'True', 'on'),
        'rain_slave': int(float(_setting_direct('rain_slave', '23') or 23)),
        'rain_function': int(float(_setting_direct('rain_function', '3') or 3)),
        'rain_register': int(float(_setting_direct('rain_register', '0') or 0)),
        'rain_quantity': int(float(_setting_direct('rain_quantity', '1') or 1)),
        'rain_scale': float(_setting_direct('rain_scale', '0.1') or 0.1),
        'rain_cumulative': _setting_direct('rain_cumulative', '1') in ('1', 'true', 'True', 'on'),
        'th_enabled': _setting_direct('th_enabled', '0') in ('1', 'true', 'True', 'on'),
        'th_slave': int(float(_setting_direct('th_slave', '3') or 3)),
        'th_function': int(float(_setting_direct('th_function', '4') or 4)),
        'th_register': int(float(_setting_direct('th_register', '1') or 1)),
        'th_quantity': int(float(_setting_direct('th_quantity', '2') or 2)),
        'th_scale': float(_setting_direct('th_scale', '0.1') or 0.1),
        'th_humi_scale': float(_setting_direct('th_humi_scale', '0.1') or 0.1),
        'th_temp_offset': float(_setting_direct('th_temp_offset', '0.0') or 0.0),
    }


@app.route('/api/weather/th')
@login_required
def api_weather_th():
    '''温湿度变送器实时值 + 当日统计（从站 03，暂未接线时返回错误信息）。'''
    st = weather_service_instance.realtime()
    day = datetime.now().strftime('%Y-%m-%d')
    try:
        stats = weather_service_instance.th_stats(day)
    except Exception as e:
        stats = {'error': str(e)}
    return api_ok(realtime=st, settings=_weather_settings(), today=stats,
                  key='th_temperature')


@app.route('/api/weather/th/history')
@login_required
def api_weather_th_history():
    date_str = request.args.get('date') or datetime.now().strftime('%Y-%m-%d')
    try:
        points = weather_service_instance.th_history(date_str)
        stats = weather_service_instance.th_stats(date_str)
    except Exception as e:
        return api_err(f'查询失败: {e}', 500)
    return api_ok(date=date_str, points=points, stats=stats)


@app.route('/api/weather/th/read', methods=['POST'])
@login_required
@admin_required
def api_weather_th_read():
    '''立即读取一次温湿度（调试用，未接线时会报 Modbus 超时）。'''
    try:
        result = weather_service_instance.read_th_once(_weather_settings())
    except Exception as e:
        return api_err(str(e), 502)
    return api_ok(result=result, realtime=weather_service_instance.realtime())


@app.route('/api/weather/realtime')
@login_required
def api_weather_realtime():
    return api_ok(realtime=weather_service_instance.realtime(), settings=_weather_settings())


@app.route('/api/weather/history')
@login_required
def api_weather_history():
    date_str = request.args.get('date') or datetime.now().strftime('%Y-%m-%d')
    mode = request.args.get('mode', 'raw')
    if mode == 'minute':
        interval = int(float(request.args.get('interval', 5)))
        return api_ok(date=date_str, interval=interval,
                      points=weather_service_instance.history_minute(date_str, interval))
    return api_ok(date=date_str, points=weather_service_instance.history(date_str))


@app.route('/api/weather/history_range')
@login_required
def api_weather_history_range():
    try:
        days = int(float(request.args.get('days', 7)))
        interval = int(float(request.args.get('interval', 10)))
    except (TypeError, ValueError):
        return api_err('days/interval 参数不合法')
    points = weather_service_instance.history_range(days=days, interval_minutes=interval)
    return api_ok(days=days, interval=interval, points=points)


@app.route('/api/weather/export.csv')
@login_required
def api_weather_export_csv():
    try:
        days = int(float(request.args.get('days', 7)))
        interval = int(float(request.args.get('interval', 10)))
    except (TypeError, ValueError):
        return api_err('days/interval 参数不合法')
    points = weather_service_instance.history_range(days=days, interval_minutes=interval)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['时间', '平均风速(m/s)', '最大风速(m/s)', '最小风速(m/s)', '样本数'])
    for p in points:
        writer.writerow([p.get('ts', ''), p.get('avg_speed', ''), p.get('max_speed', ''),
                         p.get('min_speed', ''), p.get('n', '')])
    data = buf.getvalue().encode('utf-8-sig')
    return Response(data, mimetype='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename=wind_history_{days}d_{interval}min.csv'})


@app.route('/api/weather/stats')
@login_required
def api_weather_stats():
    date_str = request.args.get('date') or datetime.now().strftime('%Y-%m-%d')
    return api_ok(date=date_str, stats=weather_service_instance.stats(date_str))


@app.route('/api/rain/realtime')
@login_required
def api_rain_realtime():
    st = weather_service_instance.realtime()
    settings = _weather_settings()
    today = datetime.now().strftime('%Y-%m-%d')
    status = {
        'enabled': settings.get('rain_enabled', True),
        'running': st.get('running', False),
        'last_ok': st.get('rain_last_ok', 0),
        'last_error': st.get('rain_last_error', ''),
        'last_raw': st.get('rain_last_raw'),
        'last_total': st.get('rain_last_total'),
        'last_ts': st.get('rain_last_ts', ''),
        'last_tx_hex': st.get('rain_last_tx_hex', ''),
        'last_rx_hex': st.get('rain_last_rx_hex', ''),
        'poll_count': st.get('rain_poll_count', 0),
        'error_count': st.get('rain_error_count', 0),
    }
    return api_ok(date=today, realtime=status,
                  today=weather_service_instance.rain_stats(today),
                  recent_hour_mm=weather_service_instance.rain_recent_hour(),
                  settings=settings)


@app.route('/api/rain/hourly')
@login_required
def api_rain_hourly():
    date_str = request.args.get('date') or datetime.now().strftime('%Y-%m-%d')
    return api_ok(date=date_str, stats=weather_service_instance.rain_stats(date_str))


@app.route('/api/rain/history')
@login_required
def api_rain_history():
    date_str = request.args.get('date') or datetime.now().strftime('%Y-%m-%d')
    limit = int(float(request.args.get('limit', 5000)))
    with weather_service_instance._conn() as c:
        rows = c.execute(
            'SELECT ts,ts_epoch,rain_total,raw,unit FROM rain_readings '
            'WHERE ts LIKE ? ORDER BY ts_epoch ASC LIMIT ?',
            (date_str + '%', limit)
        ).fetchall()
    return api_ok(date=date_str, points=[dict(r) for r in rows])


@app.route('/api/rain/export.csv')
@login_required
def api_rain_export_csv():
    date_str = request.args.get('date') or datetime.now().strftime('%Y-%m-%d')
    stats = weather_service_instance.rain_stats(date_str)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['时间', '小时降水量(mm)', '样本数'])
    for p in stats.get('points', []):
        writer.writerow([p.get('hour', ''), p.get('rain_mm', ''), p.get('samples', '')])
    data = buf.getvalue().encode('utf-8-sig')
    return Response(data, mimetype='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename=rain_hourly_{date_str}.csv'})


@app.route('/api/weather/settings', methods=['GET'])
@login_required
def api_weather_settings_get():
    return api_ok(settings=_weather_settings(), realtime=weather_service_instance.realtime())


@app.route('/api/weather/settings', methods=['POST'])
@login_required
@admin_required
def api_weather_settings_set():
    data = request.get_json(silent=True) or {}
    mapping = {
        'enabled': ('weather_enabled', lambda v: '1' if v in (True, '1', 'true', 'on') else '0'),
        'port': ('weather_port', str),
        'baud': ('weather_baud', str),
        'parity': ('weather_parity', str),
        'stopbits': ('weather_stopbits', str),
        'timeout': ('weather_timeout', str),
        'slave': ('weather_slave', str),
        'function': ('weather_function', str),
        'register': ('weather_register', str),
        'quantity': ('weather_quantity', str),
        'scale': ('weather_scale', str),
        'poll_interval': ('weather_poll_interval', str),
        'rain_enabled': ('rain_enabled', lambda v: '1' if v in (True, '1', 'true', 'on') else '0'),
        'rain_slave': ('rain_slave', str),
        'rain_function': ('rain_function', str),
        'rain_register': ('rain_register', str),
        'rain_quantity': ('rain_quantity', str),
        'rain_scale': ('rain_scale', str),
        'rain_cumulative': ('rain_cumulative', lambda v: '1' if v in (True, '1', 'true', 'on') else '0'),
        'th_enabled': ('th_enabled', lambda v: '1' if v in (True, '1', 'true', 'on') else '0'),
        'th_slave': ('th_slave', str),
        'th_function': ('th_function', str),
        'th_register': ('th_register', str),
        'th_quantity': ('th_quantity', str),
        'th_scale': ('th_scale', str),
        'th_humi_scale': ('th_humi_scale', str),
        'th_temp_offset': ('th_temp_offset', str),
    }
    for k, (sk, caster) in mapping.items():
        if k in data:
            set_setting(sk, caster(data[k]))
    cfg = _weather_settings()
    weather_service_instance.update_settings({
        'port': cfg['port'], 'baud': cfg['baud'], 'parity': cfg['parity'],
        'stopbits': cfg['stopbits'], 'timeout': cfg['timeout'], 'slave': cfg['slave'],
        'function': cfg['function'], 'register': cfg['register'], 'quantity': cfg['quantity'],
        'scale': cfg['scale'], 'poll_interval': cfg['poll_interval'],
        'rain_enabled': cfg['rain_enabled'], 'rain_slave': cfg['rain_slave'],
        'rain_function': cfg['rain_function'], 'rain_register': cfg['rain_register'],
        'rain_quantity': cfg['rain_quantity'], 'rain_scale': cfg['rain_scale'],
        'rain_cumulative': cfg['rain_cumulative'],
        'th_enabled': cfg['th_enabled'], 'th_slave': cfg['th_slave'],
        'th_function': cfg['th_function'], 'th_register': cfg['th_register'],
        'th_quantity': cfg['th_quantity'], 'th_scale': cfg['th_scale'],
        'th_humi_scale': cfg['th_humi_scale'], 'th_temp_offset': cfg['th_temp_offset'],
    })
    if cfg['enabled']:
        if not weather_service_instance.realtime().get('running'):
            weather_service_instance.start(cfg)
    else:
        weather_service_instance.stop()
    return api_ok(settings=cfg, realtime=weather_service_instance.realtime())


@app.route('/api/weather/serial_debug', methods=['POST'])
@login_required
@admin_required
def api_weather_serial_debug():
    data = request.get_json(silent=True) or {}
    tx_hex = (data.get('tx_hex') or '010300000001840a').replace(' ', '')
    wait_ms = int(data.get('wait_ms', 1500))
    try:
        tx = bytes.fromhex(tx_hex)
    except Exception:
        return api_err('tx_hex 不是合法十六进制字符串')
    cfg = _weather_settings()
    try:
        import serial
        ser = serial.Serial(port=cfg['port'], baudrate=cfg['baud'], bytesize=8,
                            parity=cfg['parity'], stopbits=cfg['stopbits'],
                            timeout=0.05)
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        ser.write(tx)
        ser.flush()
        deadline = time.time() + max(0.2, wait_ms / 1000.0)
        rx = b''
        while time.time() < deadline:
            b = ser.read(256)
            if b:
                rx += b
        ser.close()
        return api_ok(tx=tx.hex(), rx=rx.hex(), rx_len=len(rx), wait_ms=wait_ms,
                      expected='01 03 02 XX XX CRC_LO CRC_HI')
    except Exception as e:
        return api_err(f'串口调试失败：{e}', 500)


@app.route('/api/weather')
@login_required
def api_weather():
    # 兼容旧页面调用
    return api_ok(status='ok', realtime=weather_service_instance.realtime(),
                  settings=_weather_settings())


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 中继语音助手：BUSY 语音唤醒 → ASR → LLM → TTS → 受控发射
# ---------------------------------------------------------------------------
def _assist_settings_direct():
    """无 app context 读取全部 assist_* 设置（供助手后台线程使用）。

    连接必须在 finally 里关（原先 close() 在 try 体内，异常路径会漏连接/fd）。
    """
    out = {}
    db = None
    try:
        db = sqlite3.connect(str(DB_PATH), timeout=3)
        for k, v in db.execute("SELECT key,value FROM settings WHERE key LIKE 'assist_%'"):
            out[k] = v
    except Exception:
        pass
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass
    return out


def _assist_prompt_base():
    """助手与网页对话**共用**的基础系统提示词（含实时变量展开）。

    变量展开后可能变长，因此仍按 llm_system_prompt 的长度上限截断，
    真正的输入长度控制由 assistant_service._build_prompt 的逐级降配负责。
    """
    if _setting_direct('llm_system_prompt_on', '1') not in ('1', 'true', 'True', 'on'):
        return ''
    base = (_setting_direct('llm_system_prompt', '') or '').strip()
    if not base:
        return ''
    if _setting_direct('llm_prompt_vars', '1') in ('1', 'true', 'True', 'on'):
        try:
            base = _expand_vars(base)
        except Exception:
            pass
    return base[:1200]


def _assist_channel_busy():
    """信道忙判据：硬件 BUSY 有效，或开发板正在发射。

    「正在发射」包含手动 PTT、网页对讲、流式朗读、APRS 发射四路——只要
    aplay 还活着或 PTT 还压着，助手就必须让路，否则会把别人的音频切掉。
    """
    try:
        if BUSY_STATE.get('active'):
            return True
    except Exception:
        pass
    try:
        if PTT_LEVEL:
            return True
    except Exception:
        pass
    try:
        with PLAY_LOCK:
            p = CURRENT_PLAY_PROC
        if p is not None and p.poll() is None:
            return True
    except Exception:
        pass
    return False


def _assist_ask(prompt, question='', max_tokens=256, temperature=0.3,
                use_tools=True, max_iters=2, sysprompt=''):
    """阻塞式 LLM 调用（可选 Agent 工具循环），供中继语音助手后台线程使用。

    参数说明（两者不能混用）：
      prompt   —— assistant_service 已经拼好的「系统设定+对话历史+当前问题」，
                  只用于**第一轮**。绝不能再拿它去拼第二轮，否则模型会把
                  系统设定当成用户问题照抄回来（实测踩过）。
      question —— 用户那一句短问题，只用于**拿到数据后的总结轮**。
      sysprompt—— 基础设定+语音播报规范的**原文**（不含历史与问题）。总结轮要
                  靠它重新约束输出：真正被朗读的文本是总结轮产出的，而第一轮在
                  force_first 下被要求「只输出读取指令、不要回答用户」，约束若
                  只出现在第一轮，模型就当没看见（2026-09-26 实测：规范里的
                  「全中文单位」「每句加喵」对 deepseek-chat 全部未生效）。

    为什么不用 /api/agent/chat 那套 SSE：助手要的是**完整一句话**才能合成语音，
    流式只增加复杂度没有收益，而且这里必须在后台线程里同步拿到结果。
    工具协议仍复用 agent_service，保证和网页 Agent 对话行为一致。
    """
    t0 = time.time()
    out = {'ok': False, 'reply': '', 'ms': 0, 'provider': '', 'model': '',
           'iters': 0, 'tools': '', 'error': ''}
    try:
        # LLM 提供方跟随「设置 / 校准」里的全局 llm_provider：
        # 助手页已不再单独设置，避免两处各说各话。
        provider = (_setting_direct('llm_provider', 'local') or 'local').strip()
        if provider not in ('local', 'external'):
            provider = 'local'
        cfg = provider_config(provider) or {}
        if not cfg.get('url'):
            out['error'] = 'LLM 地址未配置（provider=%s）' % provider
            return out
        out['provider'] = cfg.get('provider') or provider
        out['model'] = cfg.get('model') or ''
        if (cfg.get('provider') or provider) == 'local':
            ok, msg = voice_service_instance.ensure_llm_ready(
                wait=float(_setting_direct('assist_llm_wait', '25') or 25))
            if not ok:
                out['error'] = '本地 LLM 未就绪：%s' % msg
                return out
        agent_on = bool(use_tools) and _setting_direct(
            'agent_enabled', '1') in ('1', 'true', 'True', 'on')
        valid, ctx = [], {}
        if agent_on:
            # 只取只读工具。speak 之类 action=True 的工具会直接压 PTT 发射，
            # 绕过本模块所有的发射安全线（BUSY 让路/最短间隔/单次上限/
            # 禁发时段/测试模式），对无人值守的语音助手是致命的，必须排除。
            valid = [t['name'] for t in agent_service.enabled_tools(_agent_enabled())
                     if not t.get('action')]
            if not valid:
                agent_on = False
            else:
                _all = _agent_ctx() or {}
                ctx = {k: v for k, v in _all.items() if k in set(valid)}
        # 注意：enabled_tools([]) 会因空列表为假而返回**全部**工具，
        # 所以这里只能传非空列表或 None，不能传空列表。
        enabled = valid if agent_on else None
        max_iters = max(0, min(4, int(max_iters or 0)))
        # q_short 只用于总结轮；prompt 只用于第一轮
        q_short = (question or '').strip() or (prompt or '')[:120]
        q_short = q_short[:120]
        # 总结轮要不要回灌约束、回灌多少：见 agent_service.summary_spec 的说明。
        # auto = 只给外部云模型（板端 RKLLM 提示词一长就空输出）。
        sp_mode = (os.environ.get('RELAY_ASSIST_SUMMARY_SPEC') or 'auto').strip().lower()
        if sp_mode not in agent_service.SUMMARY_SPEC_MODES:
            sp_mode = 'auto'
        try:
            sp_cap = max(120, min(4000, int(
                os.environ.get('RELAY_ASSIST_SUMMARY_SPEC_MAX') or 1200)))
        except Exception:
            sp_cap = 1200
        collected, used = [], []
        text = ''
        force_first = agent_on and agent_service.wants_realtime(q_short or prompt)
        it = 0
        while True:
            voice_service.llm_lease(300.0)
            if it == 0:
                content = prompt
                if agent_on:
                    # 工具协议附在**拼好的提示词之后**，不再二次包装
                    content = prompt + '\n\n' + agent_service.tools_prompt(enabled)
                    if force_first:
                        content += ('\n现在只输出一行读取指令'
                                    '（格式 READ 名称 {}），不要回答用户。')
                msgs = [{'role': 'user', 'content': content}]
            else:
                # 数据已拿到：让模型只做「总结成一句话」这一件事。
                # 行为约束必须在这一轮重新出现：**这一轮产出的才是被朗读的文本**。
                msgs = agent_service.summary_messages(
                    collected, q_short, sysprompt, provider,
                    mode=sp_mode, cap=sp_cap)
            body = {'model': cfg.get('model') or 'qwen2.5-1.5b',
                    'messages': msgs,
                    'max_tokens': int(max_tokens), 'temperature': float(temperature),
                    'stream': False}
            r = requests.post(cfg['url'], json=body,
                              headers=_llm_headers(cfg.get('api_key')), timeout=(5, 180))
            voice_service.llm_lease(300.0)
            if r.status_code != 200:
                out['error'] = 'HTTP %s: %s' % (r.status_code, r.text[:160])
                out['ms'] = int((time.time() - t0) * 1000)
                return out
            try:
                text = (r.json()['choices'][0]['message']['content'] or '').strip()
            except Exception:
                out['error'] = 'LLM 返回结构异常'
                out['ms'] = int((time.time() - t0) * 1000)
                return out
            out['iters'] = it + 1
            if not agent_on:
                break
            calls = agent_service.parse_tool_calls(text, valid=valid)
            if not calls or it >= max_iters:
                break
            for c in calls[:3]:
                if c['name'] in used:
                    continue
                used.append(c['name'])
                try:
                    fn = ctx.get(c['name'])
                    res = fn(**(c.get('arguments') or {})) if fn else {'error': '未知工具'}
                except Exception as e:
                    res = {'error': '%s: %s' % (type(e).__name__, e)}
                # 工具报错必须留痕。模型拿到 error 会如实答「不知道」，而
                # 「调用了工具」这个事实照样成立——没有这一行，外部完全分辨
                # 不出「工具抛异常」和「工具没数据」。实测踩过：板端
                # aprs_service 漏部署，三个位置问题全答「不知道」，而验证脚本
                # 只看 tools 字段，20 项全绿却掩盖了故障。
                if isinstance(res, dict) and res.get('error'):
                    print('[ASSIST] 工具 %s 出错：%s' % (
                        c['name'], str(res.get('error'))[:160]), flush=True)
                collected.append({c['name']: res})
            out['tools'] = ','.join(used)
            if not collected:
                break
            it += 1
        # 收尾：拿到数据但最后一轮还是工具指令（或空），补一次总结
        if collected and (not text or agent_service.parse_tool_calls(text, valid=valid)):
            voice_service.llm_lease(300.0)
            body = {'model': cfg.get('model') or 'qwen2.5-1.5b',
                    'messages': agent_service.summary_messages(
                        collected, q_short, sysprompt, provider,
                        mode=sp_mode, cap=sp_cap),
                    'max_tokens': int(max_tokens), 'temperature': float(temperature),
                    'stream': False}
            r = requests.post(cfg['url'], json=body,
                              headers=_llm_headers(cfg.get('api_key')), timeout=(5, 180))
            voice_service.llm_lease(300.0)
            if r.status_code == 200:
                try:
                    text = (r.json()['choices'][0]['message']['content'] or '').strip()
                    out['iters'] = int(out['iters']) + 1
                except Exception:
                    pass
        out['reply'] = text
        out['ok'] = bool(text)
        if not text:
            out['error'] = out['error'] or 'LLM 返回空内容'
    except Exception as e:
        out['error'] = '%s: %s' % (type(e).__name__, e)
    out['ms'] = int((time.time() - t0) * 1000)
    print('[ASSIST] LLM %s %dms iters=%s tools=%s reply=%d字%s' % (
        'OK' if out['ok'] else 'FAIL', out['ms'], out['iters'], out['tools'] or '-',
        len(out['reply']), (' ' + out['error'][:60]) if out['error'] else ''), flush=True)
    return out


def _assist_tts(text, voice=''):
    """把回复合成成 WAV。与网页朗读共用同一条 Piper 中英混读路径。

    清洗（去 Markdown/emoji）已在 tts_service 内部强制生效，
    这里不再重复处理，避免两处规则不一致。
    """
    voice = (voice or '').strip() or (
        _setting_direct('tts_local_voice', 'zh_CN-huayan-medium') or 'zh_CN-huayan-medium')
    en_voice = (_setting_direct('tts_en_voice', '') or '').strip() or None
    icao_voice = (_setting_direct('tts_icao_voice', '') or '').strip() or None
    icao = _setting_direct('tts_icao', '1') in ('1', 'true', 'True', 'on')
    return tts_service.synthesize_multilingual(text, voice, en_voice=en_voice,
                                               icao=icao, icao_voice=icao_voice)


def _assist_play(wav_path, max_seconds=30.0):
    """把 WAV 送上发射机，带**硬超时**保护。

    与 play_audio_async 的关键区别：助手是自动发射，旁边没有人盯着，所以必须
    能保证「最多占用信道 N 秒」——超时立刻 kill aplay 并松 PTT，绝不允许因为
    某个异常把信道一直压住。
    """
    result = {'ok': False, 'error': '', 'seconds': 0.0, 'truncated': False}
    t0 = time.time()
    ptt_on = False
    try:
        if not Path(wav_path).exists():
            result['error'] = '音频文件不存在'
            return result
        _ensure_audio_unmuted()
        _ptt_retain()                       # 先拉 PTT，再送音频
        ptt_on = True
        time.sleep(0.12)                    # 等继电器/功放稳定
        proc = _play_file_locked(wav_path)
        if proc is None:
            result['error'] = 'aplay 启动失败'
            return result
        limit = max(3.0, float(max_seconds))
        try:
            proc.wait(timeout=limit)
            if proc.returncode == 0:
                result['ok'] = True
            else:
                result['error'] = 'aplay 退出码 %s' % proc.returncode
        except Exception:
            _stop_proc(proc)                # 超时：硬截断
            global CURRENT_PLAY_PROC
            try:
                if CURRENT_PLAY_PROC is proc:
                    CURRENT_PLAY_PROC = None
            except Exception:
                pass
            result['ok'] = True
            result['truncated'] = True
            result['error'] = '超过单次发射上限 %.0fs，已强制截断' % limit
        return result
    except Exception as e:
        result['error'] = '%s: %s' % (type(e).__name__, e)
        return result
    finally:
        result['seconds'] = round(time.time() - t0, 2)
        if ptt_on:
            try:
                _ptt_release()
            except Exception:
                pass
        try:
            _ptt_event('assist_tx_done', '%.1fs' % result['seconds'])
            print('[ASSIST] 发射结束 %.1fs ok=%s %s' % (
                result['seconds'], result['ok'], result['error'][:80]), flush=True)
        except Exception:
            pass


def _assist_stop_play():
    """手动停止：立刻打断播放。PTT 由 _ptt_release 的引用计数负责落下来。"""
    try:
        with PLAY_LOCK:
            proc = CURRENT_PLAY_PROC
        _stop_proc(proc)
    except Exception:
        pass


assistant_service_instance.configure(
    setting_getter=_assist_settings_direct,
    busy_getter=_assist_channel_busy,
    tx_getter=lambda: bool(PTT_LEVEL),
    ask_fn=_assist_ask,
    tts_fn=_assist_tts,
    play_fn=_assist_play,
    base_prompt_fn=_assist_prompt_base,
    stop_play_fn=_assist_stop_play,
    expand_fn=_expand_vars,
)
assistant_service_instance.start()


@app.route('/assistant')
@login_required
def assistant_page():
    return render_template('assistant.html', user=session.get('username'),
                           role=session.get('role'))


@app.route('/api/assist/status')
@login_required
def api_assist_status():
    st = assistant_service_instance.status()
    st['busy'] = bool(BUSY_STATE.get('active'))
    st['ptt'] = bool(PTT_LEVEL)
    st['vlog_enabled'] = voice_service_instance.enabled()
    try:
        st['mic_running'] = bool(MIC_CAPTURE.get('running'))
    except Exception:
        st['mic_running'] = False
    return api_ok(**st)


@app.route('/api/assist/list')
@login_required
def api_assist_list():
    day = (request.args.get('day') or '').strip() or datetime.now().strftime('%Y-%m-%d')
    try:
        limit = max(1, min(500, int(float(request.args.get('limit', 100) or 100))))
    except Exception:
        limit = 100
    items = assistant_service_instance.list_turns(day=day, limit=limit)
    return api_ok(day=day, items=items,
                  stats=assistant_service_instance.day_stats(day))


@app.route('/api/assist/<int:rid>/audio')
@login_required
def api_assist_audio(rid):
    row = assistant_service_instance.store.one(
        'SELECT rx_wav,tx_wav FROM assist_turns WHERE id=?', (rid,))
    if not row:
        return api_err('记录不存在', 404)
    which = (request.args.get('which') or 'rx').strip()
    name = row.get('tx_wav') if which == 'tx' else row.get('rx_wav')
    p = assistant_service_instance.wav_path(name)
    if not p or not Path(p).exists():
        return api_err('音频不存在', 404)
    return send_file(str(p), mimetype='audio/wav', conditional=True)


@app.route('/api/assist/test', methods=['POST'])
@login_required
def api_assist_test():
    """本地回环测试：走完整链路（提示词→LLM→TTS），默认只网页试听不发射。"""
    data = request.get_json(silent=True) or {}
    text = (data.get('text') or '').strip()
    if not text:
        return api_err('测试文本不能为空')
    audit('assist_test', text[:120])
    return api_ok(**assistant_service_instance.test_turn(text))


@app.route('/api/assist/wake', methods=['POST'])
@login_required
def api_assist_wake():
    """只做唤醒词匹配自测：不调 LLM、不发射，用于调唤醒词与容错。"""
    data = request.get_json(silent=True) or {}
    return api_ok(**assistant_service_instance.test_wake((data.get('text') or '').strip()))


@app.route('/api/assist/stop', methods=['POST'])
@login_required
def api_assist_stop():
    audit('assist_stop', '')
    return api_ok(**assistant_service_instance.stop())


@app.route('/api/assist/clear', methods=['POST'])
@login_required
@admin_required
def api_assist_clear():
    data = request.get_json(silent=True) or {}
    day = (data.get('day') or '').strip()
    if day:
        n = assistant_service_instance.store.exec(
            'DELETE FROM assist_turns WHERE ts LIKE ?', (day + '%',)).rowcount
    else:
        n = assistant_service_instance.store.exec('DELETE FROM assist_turns').rowcount
    audit('assist_clear', day or 'all')
    return api_ok(removed=int(n or 0))


@app.route('/api/assist/prompt')
@login_required
def api_assist_prompt():
    """预览「运行前实际注入的提示词」——网页 LLM 对话与中继助手各一份。

    这是排查「为什么模型不照做」最直接的工具：直接看到最终拼出来的文本。
    """
    st = assistant_service_instance.settings(force=True)
    base = _assist_prompt_base()
    suffix = (st.get('assist_prompt_suffix') or '').strip()
    try:
        suffix = _expand_vars(suffix)
    except Exception:
        pass
    suffix = suffix.replace('{max_chars}',
                            str(assistant_service_instance.max_chars(st)))
    q = (request.args.get('q') or '现在风速多少').strip()
    prompt, chars = assistant_service_instance._build_prompt(q, st, base)
    clean_demo = ''
    try:
        clean_demo = tts_service.clean_for_tts(
            request.args.get('demo') or
            '**风速 3.2 米每秒**，请注意！\n- 电压 12.6V\n### 结束')
    except Exception:
        pass
    return api_ok(base=base, suffix=suffix, question=q, prompt=prompt,
                  prompt_chars=chars,
                  max_input=int(float(st.get('assist_max_input_chars') or 3000)),
                  max_reply=assistant_service_instance.max_chars(st),
                  max_tokens=int(float(st.get('assist_max_tokens') or 256)),
                  history_turns=assistant_service_instance.hist_turns(st),
                  clean_demo=clean_demo)


@app.route('/api/assist/debug')
@login_required
def api_assist_debug():
    """识别音频留档：直接看助手每一段到底听到了什么（含未命中唤醒词的）。"""
    return api_ok(items=assistant_service_instance.debug_list(),
                  keep=assistant_service_instance.settings().get('assist_debug_keep'))


@app.route('/api/assist/debug/<name>')
@login_required
def api_assist_debug_audio(name):
    p = assistant_service_instance.debug_path(name)
    if not p:
        return api_err('留档不存在', 404)
    return send_file(str(p), mimetype='audio/wav', conditional=True)


@app.route('/api/assist/clean', methods=['POST'])
@login_required
def api_assist_clean():
    """预览 TTS 清洗效果：看到「模型原文」和「实际会念出来的文本」。"""
    data = request.get_json(silent=True) or {}
    text = str(data.get('text') or '')
    try:
        cleaned = tts_service.clean_for_tts(text)
    except Exception as e:
        return api_err('清洗失败：%s' % e, 500)
    return api_ok(raw=text, cleaned=cleaned, raw_len=len(text), cleaned_len=len(cleaned))


class _ReleaseOnClose:
    """包住 WSGI 可迭代对象，在响应真正结束时才释放并发额度。

    流式响应（MJPEG / PCM / SSE）的迭代体是在中间件的 __call__ 返回之后
    才被消费的，所以在 __call__ 的 finally 里释放会让流式请求完全不占额度。
    """

    def __init__(self, iterable, sem):
        self._it = iter(iterable)
        self._sem = sem
        self._done = False

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return next(self._it)
        except StopIteration:
            self._release()
            raise

    def close(self):
        self._release()
        closer = getattr(self._it, 'close', None)
        if closer is not None:
            try:
                closer()
            except Exception:
                pass

    def _release(self):
        if not self._done:
            self._done = True
            self._sem.release()


class _BoundedConcurrency:
    """WSGI 中间件：限制**同时**进入 Flask 的请求数。

    板端是单进程 Flask + GIL。Werkzeug 开发服务器是 thread-per-connection
    且没有上限，前端一旦出现轮询重叠，积压就直接变成线程数：几百个线程抢
    一把 GIL，接口从 40ms 劣化到 10~27 秒。这里把并发锁死，超载时是排队
    （或明确 503），而不是无限起线程。
    """

    def __init__(self, inner, limit):
        self.inner = inner
        self.sem = threading.BoundedSemaphore(max(1, int(limit)))
        try:
            self.timeout = float(os.environ.get('RELAY_WEB_QUEUE_TIMEOUT', '30') or 30)
        except Exception:
            self.timeout = 30.0

    def __call__(self, environ, start_response):
        if not self.sem.acquire(timeout=self.timeout):
            start_response('503 Service Unavailable',
                           [('Content-Type', 'text/plain; charset=utf-8'),
                            ('Retry-After', '5')])
            return [b'busy: too many concurrent requests\n']
        try:
            iterable = self.inner(environ, start_response)
        except Exception:
            self.sem.release()
            raise
        return _ReleaseOnClose(iterable, self.sem)


def _serve():
    """启动 Web 服务（有界并发）。

    优先用 waitress（真正的有界线程池）；没装就退回 Werkzeug，但套一层信号量
    中间件把**同时执行**的请求数限住，保证「超载 = 排队」而不是
    「超载 = 线程无限增长」。

    环境变量：
      RELAY_WEB_SERVER=werkzeug   强制回退（waitress 若在某场景有问题时的后路）
      RELAY_WEB_SERVER=waitress   强制用 waitress（未安装则报错并回退）
      RELAY_WEB_THREADS           并发上限，默认 16
    """
    port = int(os.environ.get('RELAY_WEB_PORT', '8080'))
    try:
        threads = max(2, int(os.environ.get('RELAY_WEB_THREADS', '16') or 16))
    except Exception:
        threads = 16

    prefer = (os.environ.get('RELAY_WEB_SERVER') or 'auto').strip().lower()
    waitress_serve = None
    if prefer != 'werkzeug':
        try:
            from waitress import serve as waitress_serve
        except ImportError:
            waitress_serve = None
            if prefer == 'waitress':
                print('[WEB] 指定了 waitress 但未安装，回退 Werkzeug', flush=True)

    if waitress_serve is None:
        print('[WEB] Werkzeug + 并发信号量阀（同时请求上限 %d）；'
              '安装 waitress 可换成真正的有界线程池' % threads, flush=True)
        app.wsgi_app = _BoundedConcurrency(app.wsgi_app, threads)
        app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
        return

    print('[WEB] waitress 有界线程池启动：0.0.0.0:%d threads=%d' % (port, threads),
          flush=True)
    waitress_serve(app, host='0.0.0.0', port=port, threads=threads,
                   connection_limit=max(threads * 4, 64),
                   channel_timeout=900, ident='elf2-relay-web')


if __name__ == '__main__':
    init_db()
    _ensure_audio_unmuted()
    _apply_mic_settings()
    try:
        _ptt_force_low()
    except Exception as e:
        print(f'[PTT] 初始化失败: {e}')
    try:
        cfg = _weather_settings()
        if cfg.get('enabled'):
            weather_service_instance.update_settings(cfg)
            weather_service_instance.start(cfg)
    except Exception:
        pass
    # 循环录像守护线程已在模块加载时启动（见 _camera_autostart_worker）
    # 能量统计采样线程：表已由上面的 init_db() 建好，这里起最稳
    threading.Thread(target=_energy_sampler, daemon=True,
                     name='energy-sampler').start()
    _serve()

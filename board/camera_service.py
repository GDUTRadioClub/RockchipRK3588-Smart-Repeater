#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ELF2 UVC 摄像头服务：单进程采集 + MJPEG 预览 + 循环/手动录像 + RTMP 推流。"""
import os
import queue
import re
import signal
import subprocess
import threading
import time
from pathlib import Path


_SW_H264 = ('libx264', ['-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '30'])
_H264_ENCODER_CACHE = {}

# 硬件编码器（rkmpp / v4l2m2m）不支持 -crf，只能走码率控制。
# 标定到与原先 libx264 -crf 30 相当的体积：实测 720p15 约 1.2~1.7 Mbit/s
# （9~13 MB/分钟）。注意 -b:v 4M 会让每段涨到 ~30MB，存储直接翻三倍。
_HW_BITRATE = (os.environ.get('RELAY_CAM_BITRATE') or '1500k').strip()


def _encoder_works(args):
    """试编一帧 64x64 黑场，确认这个编码器在本机真的能用。"""
    try:
        proc = subprocess.run(
            ['ffmpeg', '-hide_banner', '-loglevel', 'error',
             '-f', 'lavfi', '-i', 'testsrc=size=64x64:rate=1', '-frames:v', '1']
            + list(args) + ['-pix_fmt', 'yuv420p', '-f', 'null', '-'],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=25)
        return proc.returncode == 0
    except Exception:
        return False


def _h264_candidates():
    """按优先级给出候选编码器。"""
    forced = (os.environ.get('RELAY_CAM_ENCODER') or '').strip()
    if forced:
        return [(forced, ['-c:v', forced])]
    if (os.environ.get('RELAY_CAM_HWENC') or '').strip() == '0':
        return [_SW_H264]
    try:
        proc = subprocess.run(['ffmpeg', '-hide_banner', '-encoders'],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=10)
        listing = proc.stdout.decode('utf-8', 'replace')
    except Exception:
        listing = ''
    cands = []
    if 'h264_rkmpp' in listing:
        cands.append(('h264_rkmpp', ['-c:v', 'h264_rkmpp', '-b:v', _HW_BITRATE]))
    if 'h264_v4l2m2m' in listing:
        cands.append(('h264_v4l2m2m', ['-c:v', 'h264_v4l2m2m', '-b:v', _HW_BITRATE]))
    cands.append(_SW_H264)
    return cands


def pick_h264_encoder():
    """挑一个可用的 H.264 编码器，返回 (名字, ffmpeg 参数)。只探测一次。

    RK3588 有硬件 H.264 编码器；用软件 libx264 会**持续吃掉约 0.8 个核**
    （实测 1280x720@15fps 常驻编码）。但各版本 BSP 的 rkmpp 参数不一致，
    所以这里先**试编一帧**再决定：探测到但实际不能用的话，
    绝不拿用户的录像去冒险，直接退回 libx264。

    环境变量：
      RELAY_CAM_HWENC=0     强制软件编码
      RELAY_CAM_ENCODER=xx  直接指定编码器，跳过探测与试编
    """
    if 'enc' in _H264_ENCODER_CACHE:
        return _H264_ENCODER_CACHE['enc']
    chosen = _SW_H264
    for name, args in _h264_candidates():
        if name == _SW_H264[0] or _encoder_works(args):
            chosen = (name, args)
            break
    _H264_ENCODER_CACHE['enc'] = chosen
    print('[CAM] H.264 编码器：%s' % chosen[0], flush=True)
    return chosen


class FfmpegRecorder:
    """从摄像头服务的 JPEG 帧队列读取，交给 ffmpeg 转码/封装/推流。"""

    def __init__(self, camera, output_args, osd_filter='', label='rec'):
        self.camera = camera
        self.label = label
        self.queue = queue.Queue(maxsize=80)
        self.active = True
        self.error = ''
        cmd = [
            'ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin',
            '-f', 'image2pipe', '-vcodec', 'mjpeg', '-i', '-',
        ]
        if osd_filter:
            cmd += ['-vf', osd_filter]
        cmd += pick_h264_encoder()[1] + [
            '-pix_fmt', 'yuv420p',
        ] + output_args
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, bufsize=0)
        camera.add_frame_queue(self.queue)
        self.feeder = threading.Thread(target=self._feed, daemon=True)
        self.feeder.start()
        self.waiter = threading.Thread(target=self._wait_proc, daemon=True)
        self.waiter.start()
        self.err_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self.err_thread.start()

    def _read_stderr(self):
        try:
            while self.proc and self.proc.stderr:
                line = self.proc.stderr.readline()
                if not line:
                    break
                self.error = line.decode('utf-8', errors='replace')[-500:]
        except Exception:
            pass

    def _feed(self):
        while self.active:
            try:
                frame = self.queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if frame is None:
                break
            try:
                if self.proc and self.proc.stdin:
                    self.proc.stdin.write(frame)
                    self.proc.stdin.flush()
            except Exception:
                break
        try:
            if self.proc and self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
        except Exception:
            pass

    def _wait_proc(self):
        try:
            if self.proc:
                self.proc.wait()
        except Exception:
            pass

    def stop(self):
        self.active = False
        try:
            self.queue.put_nowait(None)
        except Exception:
            pass
        self.camera.remove_frame_queue(self.queue)
        # 先等待 feeder 退出，关闭 stdin 让 ffmpeg 正常收尾/封口
        try:
            self.feeder.join(timeout=3)
        except Exception:
            pass
        if self.proc and self.proc.poll() is None:
            try:
                if self.proc.stdin and not self.proc.stdin.closed:
                    self.proc.stdin.close()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=10)
            except Exception:
                try:
                    self.proc.send_signal(signal.SIGINT)
                    self.proc.wait(timeout=5)
                except Exception:
                    try:
                        self.proc.kill()
                    except Exception:
                        pass
        return self.error


class CameraService:
    def __init__(self):
        self.lock = threading.RLock()
        self.proc = None
        self.reader = None
        self.running = False
        self.clients = set()
        self.frame_queues = set()
        self.recorder = None
        self.rtmp = None
        self.last_error = ''
        self.config = {}
        # 清理线程的停止信号必须「每代一个」：若共享同一个 Event，
        # start_loop_cleaner() 里的 stop()->clear() 会在老线程观察到停止信号
        # 之前就把它抹掉，老线程因此永远活着 —— 每调用一次漏一个线程，
        # 且此后每 10s 全扫一次录像目录。实测 2 小时漏掉约 240 个线程，
        # 把 GIL 抢死，接口从 40ms 劣化到 10~27s，重启才恢复。
        self.cleaner_stop = None
        self.cleaner_thread = None
        self.cleaner_cfg = None

    # ---- core capture ----
    def _build_capture_cmd(self, cfg):
        device = cfg.get('device', '/dev/video21')
        size = cfg.get('resolution', '640x480')
        fps = str(cfg.get('fps', 15))
        quality = str(cfg.get('quality', 5))
        return [
            'ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin',
            '-f', 'v4l2', '-input_format', 'mjpeg',
            '-video_size', size, '-framerate', fps, '-i', device,
            '-c:v', 'copy', '-f', 'image2pipe', '-'
        ]

    def start(self, cfg):
        with self.lock:
            if self.running:
                return True, 'already running'
            self.config = dict(cfg)
            self.last_error = ''
            cmd = self._build_capture_cmd(cfg)
            try:
                self.proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL, bufsize=0)
                time.sleep(0.3)
                if self.proc.poll() is not None:
                    err = self.proc.stderr.read().decode('utf-8', errors='replace')[-500:] if self.proc.stderr else ''
                    self.last_error = err
                    return False, f'摄像头启动失败: {err[:200]}'
            except Exception as e:
                self.last_error = str(e)
                return False, f'摄像头启动失败: {e}'
            self.running = True
            self.reader = threading.Thread(target=self._reader_loop, daemon=True)
            self.reader.start()
            return True, 'started'

    def _reader_loop(self):
        buf = b''
        proc = self.proc
        while self.running and proc and proc.poll() is None:
            try:
                chunk = proc.stdout.read(65536)
            except Exception:
                break
            if not chunk:
                break
            buf += chunk
            while True:
                start = buf.find(b'\xff\xd8')
                if start < 0:
                    buf = buf[-1:] if buf else b''
                    break
                end = buf.find(b'\xff\xd9', start + 2)
                if end < 0:
                    if start > 0:
                        buf = buf[start:]
                    break
                frame = buf[start:end + 2]
                buf = buf[end + 2:]
                self._broadcast(frame)
        with self.lock:
            self.running = False
            if self.proc and self.proc.poll() is None:
                try:
                    self.proc.terminate()
                except Exception:
                    pass
            self.proc = None
        self._broadcast(None)

    def stop(self):
        with self.lock:
            self.running = False
            proc = self.proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._broadcast(None)
        return True, 'stopped'

    # ---- preview clients ----
    def add_client(self):
        q = queue.Queue(maxsize=60)
        with self.lock:
            self.clients.add(q)
        return q

    def remove_client(self, q):
        with self.lock:
            self.clients.discard(q)

    def add_frame_queue(self, q):
        with self.lock:
            self.frame_queues.add(q)

    def remove_frame_queue(self, q):
        with self.lock:
            self.frame_queues.discard(q)

    def _broadcast(self, frame):
        with self.lock:
            targets = list(self.clients) + list(self.frame_queues)
        for q in targets:
            try:
                if frame is None:
                    q.put_nowait(None)
                elif q.full():
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        pass
                    q.put_nowait(frame)
                else:
                    q.put_nowait(frame)
            except Exception:
                pass

    def get_frame(self, timeout=5):
        q = self.add_client()
        try:
            return q.get(timeout=timeout)
        except queue.Empty:
            return None
        finally:
            self.remove_client(q)

    # ---- recorders ----
    def start_recording(self, cfg, output_args, osd_filter='', label='manual'):
        if not self.running:
            ok, msg = self.start(cfg)
            if not ok:
                return False, msg
        with self.lock:
            if self.recorder:
                return False, '已有录像任务运行中'
            rec = FfmpegRecorder(self, output_args, osd_filter, label)
            self.recorder = rec
        return True, 'recording'

    def stop_recording(self):
        with self.lock:
            rec = self.recorder
            self.recorder = None
        if not rec:
            return True, 'no recorder'
        err = rec.stop()
        return True, err or 'stopped'

    def start_rtmp(self, cfg, url, osd_filter=''):
        if not self.running:
            ok, msg = self.start(cfg)
            if not ok:
                return False, msg
        with self.lock:
            if self.rtmp:
                return False, 'RTMP 推流已运行'
            self.rtmp = FfmpegRecorder(
                self, ['-f', 'flv', url], osd_filter, 'rtmp')
        return True, 'rtmp started'

    def stop_rtmp(self):
        with self.lock:
            rec = self.rtmp
            self.rtmp = None
        if not rec:
            return True, 'no rtmp'
        err = rec.stop()
        return True, err or 'stopped'

    def loop_cleaner_alive(self):
        """清理线程是否真的还活着（供上层做幂等守卫）。"""
        t = self.cleaner_thread
        return bool(t is not None and t.is_alive())

    def start_loop_cleaner(self, directory, max_mb=2048, max_files=100, storage_max_mb=0):
        """启动录像清理线程。幂等：参数没变且已在跑就复用，不重复起线程。

        上层（_cam_loop_ensure）每 30s 就会调这里一次，所以这里必须是幂等的，
        否则会变成「每 30s 漏一个线程」。
        """
        cfg_key = (str(directory), int(max_mb), int(max_files), int(storage_max_mb))
        if self.loop_cleaner_alive() and self.cleaner_cfg == cfg_key:
            return self.cleaner_thread
        self.stop_loop_cleaner()          # 收干净旧线程（含 join）
        stop = threading.Event()          # 本代专属信号，杜绝与下一代串扰
        self.cleaner_stop = stop
        self.cleaner_cfg = cfg_key

        def _clean():
            while not stop.is_set():
                try:
                    clean_loop_dir(directory, max_mb, max_files, storage_max_mb)
                except Exception:
                    pass
                stop.wait(10)

        t = threading.Thread(target=_clean, daemon=True, name='cam-cleaner')
        self.cleaner_thread = t
        t.start()
        return t

    def stop_loop_cleaner(self):
        """停止清理线程，并等它真正退出。

        只 set() 而不 join 会留下僵尸线程：老代码紧接着就 clear()，
        老线程永远等不到停止信号。
        """
        ev, t = self.cleaner_stop, self.cleaner_thread
        self.cleaner_stop = None
        self.cleaner_thread = None
        self.cleaner_cfg = None
        if ev is not None:
            ev.set()
        if t is not None and t.is_alive():
            t.join(timeout=2.0)

    def status(self):
        with self.lock:
            rec = self.recorder
            rtmp = self.rtmp
            return {
                'running': self.running,
                'clients': len(self.clients),
                'recording': bool(rec),
                'recording_label': rec.label if rec else '',
                'rtmp': bool(rtmp),
                'last_error': self.last_error,
                'config': dict(self.config),
            }


def _safe_stat(path):
    try:
        return path.stat()
    except Exception:
        return None


def _unlink_recording(path, deleted):
    try:
        size = path.stat().st_size
    except Exception:
        size = 0
    try:
        path.unlink()
        deleted.append({'filename': path.name, 'size': size})
        return size
    except Exception:
        return 0


def clean_loop_dir(directory, max_mb=2048, max_files=100, storage_max_mb=0):
    """按循环容量、循环文件数和存储容量上限清理录像。

    清理顺序：优先删除最旧的 loop_*.mp4；达到循环策略后，如总存储仍超上限，
    再删除最旧的 snapshot_*.jpg。手动录像（manual_*.mp4）不会自动删除。
    """
    d = Path(directory)
    if not d.exists():
        return {'deleted': [], 'deleted_count': 0, 'freed_bytes': 0,
                'loop_size': 0, 'media_size': 0}
    deleted = []
    try:
        loop_files = sorted(d.glob('loop_*.mp4'), key=lambda p: _safe_stat(p).st_mtime if _safe_stat(p) else 0)
    except Exception:
        loop_files = []
    # 文件数量限制
    try:
        max_files = int(max_files or 0)
    except Exception:
        max_files = 0
    if max_files > 0:
        while len(loop_files) > max_files:
            _unlink_recording(loop_files.pop(0), deleted)
    # 循环容量限制
    try:
        max_mb = int(float(max_mb or 0))
    except Exception:
        max_mb = 0
    # max_mb <= 0 视作「不限」，与上面的 max_files、下面的 storage_max_mb 保持一致。
    # 原先是 max(0, max_mb) * 1MB → 0，再用 loop_total > 0 判断，会把循环录像
    # **全部删光**；设 0 的本意显然是不限制，而不是清空。
    if max_mb > 0:
        loop_limit = max_mb * 1024 * 1024
        loop_total = 0
        for p in loop_files:
            st = _safe_stat(p)
            if st:
                loop_total += st.st_size
        while loop_files and loop_total > loop_limit:
            freed = _unlink_recording(loop_files.pop(0), deleted)
            if not freed:
                break
            loop_total -= freed
    # 总存储容量上限：只清循环分片和快照，保护手动录像
    try:
        storage_max_mb = int(float(storage_max_mb or 0))
    except Exception:
        storage_max_mb = 0
    if storage_max_mb > 0:
        cap = storage_max_mb * 1024 * 1024
        try:
            all_media = list(d.glob('loop_*.mp4')) + list(d.glob('manual_*.mp4'))
            all_media += [p for p in d.glob('snapshot_*.*')
                          if p.suffix.lower() in ('.jpg', '.jpeg')]
        except Exception:
            all_media = []
        total = 0
        for p in all_media:
            st = _safe_stat(p)
            if st:
                total += st.st_size
        if total > cap:
            # 先删除最旧的循环分片
            for p in sorted([x for x in all_media if x.name.startswith('loop_')],
                            key=lambda x: (_safe_stat(x).st_mtime if _safe_stat(x) else 0)):
                if total <= cap:
                    break
                freed = _unlink_recording(p, deleted)
                if freed:
                    total -= freed
            # 仍超限时删除最旧的快照
            for p in sorted([x for x in all_media if x.name.startswith('snapshot_')],
                            key=lambda x: (_safe_stat(x).st_mtime if _safe_stat(x) else 0)):
                if total <= cap:
                    break
                freed = _unlink_recording(p, deleted)
                if freed:
                    total -= freed
    # 统计清理后的占用，供接口/日志展示
    loop_size = 0
    media_size = 0
    try:
        for p in d.glob('loop_*.mp4'):
            st = _safe_stat(p)
            if st:
                loop_size += st.st_size
        for p in list(d.glob('*.mp4')) + [x for x in d.glob('snapshot_*.*')
                                          if x.suffix.lower() in ('.jpg', '.jpeg')]:
            st = _safe_stat(p)
            if st:
                media_size += st.st_size
    except Exception:
        pass
    return {
        'deleted': deleted,
        'deleted_count': len(deleted),
        'freed_bytes': sum(int(x.get('size') or 0) for x in deleted),
        'loop_size': loop_size,
        'media_size': media_size,
    }


def osd_filter(cfg):
    if not cfg.get('enabled', True):
        return ''
    text = str(cfg.get('text') or '')
    text = re.sub(r"[^A-Za-z0-9 ._/-]", '', text)[:60]
    if cfg.get('show_time', True):
        text = (text + ' ' if text else '') + '%{localtime}'
    if not text:
        return ''
    font = cfg.get('fontfile') or '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
    size = int(cfg.get('fontsize', 18))
    color = cfg.get('color', 'white')
    pos = cfg.get('position', 'top-left')
    if pos == 'top-right':
        x, y = 'w-tw-10', '10'
    elif pos == 'bottom-left':
        x, y = '10', 'h-th-10'
    elif pos == 'bottom-right':
        x, y = 'w-tw-10', 'h-th-10'
    else:
        x, y = '10', '10'
    return (f"drawtext=fontfile={font}:text='{text}':x={x}:y={y}:"
            f"fontsize={size}:fontcolor={color}:box=1:boxcolor=black@0.5")


camera_service = CameraService()

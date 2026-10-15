"""Bounded latest-frame RTSP capture without Python OpenCV/GI dependencies.

GStreamer performs continuous H.264 decoding and JPEG encoding in a child
process. Only the most recently received JPEG is retained. ``received_at`` is
host monotonic arrival time, not the camera exposure timestamp; stream latency
must still be measured when using these images for physical control.

Compatible with Python 3.5 on Linux. Errors never include the pipeline,
credentials or raw GStreamer diagnostics. Credentials are supplied through a
private stdin pipe, never through the worker command line. Only the worker
requires Python GI and GStreamer; the application-side module uses stdlib.
"""
from collections import namedtuple
import math
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit


Frame = namedtuple('Frame', 'jpeg sequence received_at')


class CaptureError(RuntimeError):
    """Capture failed; text is safe to include in application diagnostics."""


class CaptureTimeout(CaptureError):
    pass


class CaptureClosed(CaptureError):
    pass


class JPEGParser:
    """Split jpegenc's concatenated JPEG output across arbitrary pipe chunks.

    jpegenc does not insert arbitrary application metadata containing unescaped
    SOI/EOI bytes. Malformed/oversized frames and launch-message prefixes are
    discarded, with a bounded retained buffer and resynchronization at SOI.
    """
    def __init__(self, max_frame_bytes=12000000):
        if not isinstance(max_frame_bytes, int) or max_frame_bytes < 4:
            raise ValueError('Invalid JPEG size limit')
        self.max_frame_bytes = max_frame_bytes
        self.buffer = bytearray()

    def feed(self, chunk):
        self.buffer.extend(chunk)
        frames = []
        while self.buffer:
            start = self.buffer.find(b'\xff\xd8')
            if start < 0:
                # A marker can be split between successive pipe reads.
                tail = self.buffer[-1:] if self.buffer[-1] == 255 else b''
                self.buffer[:] = tail
                break
            if start:
                del self.buffer[:start]
            end = self.buffer.find(b'\xff\xd9', 2)
            nested = self.buffer.find(b'\xff\xd8', 2)
            if nested >= 0 and (end < 0 or nested < end):
                del self.buffer[:nested]
                continue
            if end >= 0:
                size = end + 2
                if size <= self.max_frame_bytes:
                    frames.append(bytes(self.buffer[:size]))
                del self.buffer[:size]
                continue
            if len(self.buffer) > self.max_frame_bytes:
                tail = self.buffer[-1:] if self.buffer[-1] == 255 else b''
                self.buffer[:] = tail
            break
        return frames


def _positive(value, name):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0):
        raise ValueError('Invalid ' + name)
    return float(value)


def validate_cpu_list(value):
    """Return a canonical taskset list, or None for unrestricted scheduling.

    Numeric list arguments only: no shell syntax, CPU ranges or taskset flags.
    Availability of those CPU IDs is checked by taskset on the deployment host.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 511:
        raise ValueError('Invalid RTSP CPU list')
    parts = value.split(',')
    if any(not part or any(c < '0' or c > '9' for c in part) for part in parts):
        raise ValueError('Invalid RTSP CPU list')
    cpus = [int(part) for part in parts]
    if len(set(cpus)) != len(cpus) or any(cpu > 127 for cpu in cpus):
        raise ValueError('Invalid RTSP CPU list')
    return ','.join(str(cpu) for cpu in cpus)


class RTSPCapture:
    def __init__(self, url, user, password, decoder='software', width=None,
                 height=None, fps=10, max_age=1.5, stream_timeout=5,
                 max_restarts=3, retry_delay=0.5, max_frame_bytes=12000000,
                 worker_path=None, python_executable=None, rtsp_cpus=None):
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except (ValueError, TypeError, AttributeError):
            raise ValueError('Invalid RTSP URL')
        if (parsed.scheme not in ('rtsp', 'rtsps') or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.fragment or (port is not None and not 1 <= port <= 65535)
                or any(ord(c) < 32 for c in url)):
            raise ValueError('RTSP URL must not contain credentials or fragments')
        if any(not isinstance(v, str) or not v or any(ord(c) < 32 for c in v)
               for v in (user, password)):
            raise ValueError('Invalid RTSP credentials')
        if decoder not in ('software', 'hardware'):
            raise ValueError('Decoder must be software or hardware')
        if (width is None) != (height is None):
            raise ValueError('Specify both output dimensions or neither')
        if width is not None and any(isinstance(v, bool) or not isinstance(v, int)
                                     or v < 2 or v > 8192 or v % 2
                                     for v in (width, height)):
            raise ValueError('Invalid output dimensions')
        if isinstance(fps, bool) or not isinstance(fps, int) or not 1 <= fps <= 60:
            raise ValueError('Invalid output frame rate')
        if (isinstance(max_restarts, bool) or not isinstance(max_restarts, int)
                or not 0 <= max_restarts <= 100):
            raise ValueError('Invalid restart limit')
        self.max_age = _positive(max_age, 'frame age limit')
        self.stream_timeout = _positive(stream_timeout, 'stream timeout')
        self.retry_delay = _positive(retry_delay, 'retry delay')
        JPEGParser(max_frame_bytes)  # Validate the memory bound at construction.
        self.url = url
        self._user = user
        self._password = password
        self.decoder = decoder
        self.width, self.height, self.fps = width, height, fps
        self.max_restarts = max_restarts
        self.max_frame_bytes = max_frame_bytes
        self.worker_path = str(worker_path or Path(__file__).with_name('rtsp_worker.py'))
        self.python_executable = str(python_executable or sys.executable)
        self.rtsp_cpus = validate_cpu_list(rtsp_cpus)
        # Bound startup data before spawning. Small stdin writes cannot fill an
        # empty Linux pipe while the worker imports GI (Linux PIPE_BUF >= 4096).
        if len(self._configuration()) > 4096:
            raise ValueError('RTSP configuration is too large')
        self._condition = threading.Condition()
        self._cleanup_lock = threading.Lock()
        self._quit = threading.Event()
        self._thread = None
        self._process = None
        self._frame = None
        self._sequence = 0
        self._failure = None
        self._closed = False
        self._state = 'not-started'
        self.restarts = 0

    def _command(self):
        command = [self.python_executable, self.worker_path]
        if self.rtsp_cpus is not None:
            # taskset execs only the capture child. Its GStreamer threads inherit
            # the affinity; the application and detector retain their own masks.
            return ['taskset', '-c', self.rtsp_cpus] + command
        return command

    def _configuration(self):
        return json.dumps(dict(url=self.url, user=self._user, password=self._password,
                               decoder=self.decoder, width=self.width,
                               height=self.height, fps=self.fps, latency=100,
                               tcp_timeout=max(1, int(self.stream_timeout * 1000000))),
                          ensure_ascii=False).encode('utf-8')

    def _send_configuration(self, process):
        try:
            payload = self._configuration()
            written = process.stdin.write(payload)
            if written != len(payload):
                raise CaptureError('RTSP worker configuration pipe closed')
        finally:
            process.stdin.close()

    def start(self):
        with self._condition:
            if self._closed:
                raise CaptureClosed('RTSP capture is closed')
            if self._thread is None:
                self._state = 'starting'
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()
        return self

    def _publish(self, jpeg, received_at):
        with self._condition:
            if self._closed:
                return
            self._sequence += 1
            self._frame = Frame(jpeg, self._sequence, received_at)
            self._state = 'streaming'
            self._condition.notify_all()

    def status(self):
        """Return only non-secret operational state for application logging."""
        with self._condition:
            age = None if self._frame is None else max(0, time.monotonic() - self._frame.received_at)
            return dict(frames=self._sequence, sequence=self._sequence,
                        restarts=self.restarts, state=self._state,
                        last_frame_age=age, error=self._failure)

    def read(self, after_sequence=0, timeout=2):
        if (isinstance(after_sequence, bool) or not isinstance(after_sequence, int)
                or after_sequence < 0):
            raise ValueError('Invalid frame sequence')
        deadline = time.monotonic() + _positive(timeout, 'read timeout')
        with self._condition:
            if self._thread is None and not self._closed:
                raise CaptureError('RTSP capture is not started')
            while True:
                if self._closed:
                    raise CaptureClosed('RTSP capture is closed')
                if self._failure:
                    raise CaptureError(self._failure)
                now = time.monotonic()
                frame = self._frame
                if (frame is not None and frame.sequence > after_sequence
                        and 0 <= now - frame.received_at <= self.max_age):
                    return frame
                remaining = deadline - now
                if remaining <= 0:
                    raise CaptureTimeout('Timed out waiting for a fresh RTSP frame')
                self._condition.wait(remaining)

    def _consume(self, process):
        parser = JPEGParser(self.max_frame_bytes)
        last_frame = time.monotonic()
        while not self._quit.is_set():
            if process.poll() is not None:
                return 'RTSP decoder process exited'
            now = time.monotonic()
            remaining = self.stream_timeout - (now - last_frame)
            if remaining <= 0:
                return 'RTSP stream did not produce a complete frame in time'
            readable = select.select([process.stdout], [], [], min(0.1, remaining))[0]
            if not readable:
                continue
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                return 'RTSP decoder output closed'
            frames = parser.feed(chunk)
            if frames:
                last_frame = time.monotonic()
                # Skip superseded frames delivered together in one pipe read.
                self._publish(frames[-1], last_frame)
        return 'RTSP capture stopped'

    def _stop_process(self, process):
        if process is None:
            return
        with self._cleanup_lock:
            try:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                # Never emit child argv or raw subprocess exception text.
                pass
            finally:
                if process.stdin is not None:
                    try:
                        process.stdin.close()
                    except OSError:
                        pass
                if process.stdout is not None:
                    try:
                        process.stdout.close()
                    except OSError:
                        pass

    def _run(self):
        for attempt in range(self.max_restarts + 1):
            if self._quit.is_set():
                break
            if attempt:
                with self._condition:
                    self.restarts += 1
            process = None
            reason = 'RTSP decoder could not be started or read'
            try:
                process = subprocess.Popen(self._command(), stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE,
                                           stderr=subprocess.DEVNULL, bufsize=0,
                                           close_fds=True)
                with self._condition:
                    self._process = process
                self._send_configuration(process)
                if not self._quit.is_set():
                    reason = self._consume(process)
            except Exception:
                # Spawn/read exceptions can contain the full secret-bearing
                # argv. Expose only this fixed diagnostic, including to callers.
                reason = 'RTSP decoder could not be started or read'
            finally:
                self._stop_process(process)
                with self._condition:
                    self._process = None
                    self._frame = None
                    if not self._closed:
                        self._state = 'retrying'
                    self._condition.notify_all()
            if self._quit.is_set():
                break
            if attempt == self.max_restarts:
                with self._condition:
                    self._failure = reason + '; retry limit reached'
                    self._state = 'failed'
                    self._condition.notify_all()
                break
            if self._quit.wait(self.retry_delay):
                break

    def close(self):
        with self._condition:
            self._closed = True
            self._state = 'closed'
            self._frame = None
            self._quit.set()
            process, thread = self._process, self._thread
            self._condition.notify_all()
        self._stop_process(process)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3)

    def __enter__(self):
        return self.start()

    def __exit__(self, *unused):
        self.close()

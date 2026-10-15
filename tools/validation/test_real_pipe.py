#!/usr/bin/env python3
"""Real Linux pipe faults for the packaged Engine, without any camera access.

Python 3.5+, Linux only. Creates and signals only its own detector children.
Example:
  python3 test_real_pipe.py --project-dir ~/jetson-ptz-inspection-v0.1.1 \
      --image /path/to/public/bus.jpg

JSONL goes to stdout. Exit 0 means both fault checks and cleanup passed.
This script does not validate camera motion, watchdogs, or calibration.
"""
from __future__ import print_function

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


class GuardTimeout(RuntimeError):
    pass


def emit(event, **values):
    values.update(event=event, time=time.strftime('%Y-%m-%dT%H:%M:%S%z'))
    print(json.dumps(values, sort_keys=True), flush=True)


@contextlib.contextmanager
def guard(seconds):
    """Outer bound independent of Engine's own deadline; main thread only."""
    def expired(signum, frame):
        raise GuardTimeout('Outer test guard expired after {} seconds'.format(seconds))
    previous = signal.signal(signal.SIGALRM, expired)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def close_child(engine):
    """Resume a stopped owned child before terminate; always bound the reap."""
    process = engine.p
    error = None
    try:
        with guard(5):
            if process.poll() is None:
                process.send_signal(signal.SIGCONT)
            engine.close()
    except Exception as exc:
        error = type(exc).__name__ + ': ' + str(exc)
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2)
    finally:
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()
    if process.poll() is None:
        raise RuntimeError('Test child was not reaped')
    emit('child_cleanup', pid=process.pid, returncode=process.returncode,
         fallback_error=error)
    if error is not None:
        raise RuntimeError('Engine.close needed fallback cleanup: ' + error)


def normal_infer(engine, jpeg):
    started = time.monotonic()
    with guard(20):
        result, preview = engine.infer(jpeg)
    if (not isinstance(result, dict) or not isinstance(result.get('persons'), list)
            or result.get('width', 0) <= 0 or result.get('height', 0) <= 0
            or not preview.startswith(b'\xff\xd8')):
        raise RuntimeError('Normal inference returned an invalid protocol response')
    emit('normal_infer_pass', pid=engine.p.pid,
         seconds=round(time.monotonic() - started, 4),
         persons=len(result['persons']), width=result['width'], height=result['height'])


def wait_stopped(process):
    """Wait for the actual stop event, so the write test cannot race SIGSTOP."""
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        pid, status = os.waitpid(process.pid, os.WNOHANG | os.WUNTRACED)
        if pid:
            if os.WIFSTOPPED(status):
                return
            # Do not hide a consumed exit status from Popen's later cleanup.
            if os.WIFEXITED(status):
                process.returncode = os.WEXITSTATUS(status)
            elif os.WIFSIGNALED(status):
                process.returncode = -os.WTERMSIG(status)
            raise RuntimeError('Detector exited before SIGSTOP was confirmed')
        time.sleep(.01)
    raise RuntimeError('SIGSTOP was not confirmed within two seconds')


def stalled_write(Engine, model, detector, jpeg):
    import fcntl
    with guard(10):
        engine = Engine(model, rotation=0, detector=detector)
    try:
        normal_infer(engine, jpeg)
        # Linux F_GETPIPE_SZ is 1032; old Python may not expose the named constant.
        capacity = fcntl.fcntl(engine.p.stdin.fileno(), getattr(fcntl, 'F_GETPIPE_SZ', 1032))
        # Keep the real JPEG unchanged when it already exceeds pipe capacity.
        # Otherwise append inert trailing bytes after JPEG EOI. The child is
        # stopped and the incomplete transaction is discarded, never reused.
        required = max(len(jpeg), capacity * 2 + 4096)
        if required > 12000000:
            raise RuntimeError('Pipe capacity exceeds detector protocol test limit')
        payload = jpeg + b'\0' * (required - len(jpeg))
        engine.p.send_signal(signal.SIGSTOP)
        with guard(3):
            wait_stopped(engine.p)
        started = time.monotonic()
        failure = None
        with guard(8):
            try:
                engine.infer(payload)
            except GuardTimeout:
                raise
            except RuntimeError as exc:
                failure = str(exc)
        elapsed = time.monotonic() - started
        if failure != 'Detector input timeout':
            raise RuntimeError('Expected input timeout; got {!r}'.format(failure))
        if not 2.5 <= elapsed <= 5.5:
            raise RuntimeError('Input timeout outside expected interval: {:.4f}s'.format(elapsed))
        emit('stalled_write_pass', pid=engine.p.pid, seconds=round(elapsed, 4),
             pipe_capacity=capacity, original_jpeg_bytes=len(jpeg), payload_bytes=len(payload),
             expected_error=failure)
    finally:
        close_child(engine)


def terminated_child(Engine, model, detector, jpeg):
    with guard(10):
        engine = Engine(model, rotation=0, detector=detector)
    try:
        normal_infer(engine, jpeg)
        engine.p.terminate()
        try:
            engine.p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            engine.p.kill()
            engine.p.wait(timeout=2)
        started = time.monotonic()
        failure = None
        with guard(5):
            try:
                engine.infer(jpeg)
            except GuardTimeout:
                raise
            except (OSError, RuntimeError) as exc:
                failure = type(exc).__name__ + ': ' + str(exc)
        elapsed = time.monotonic() - started
        if failure is None:
            raise RuntimeError('infer unexpectedly succeeded after child termination')
        if elapsed > 2:
            raise RuntimeError('Dead-child failure was not prompt: {:.4f}s'.format(elapsed))
        emit('terminated_child_pass', pid=engine.p.pid, seconds=round(elapsed, 4),
             expected_error=failure, returncode=engine.p.returncode)
    finally:
        close_child(engine)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-dir', required=True, help='Packaged project root on TX2')
    parser.add_argument('--image', required=True, help='Public bus.jpg, not a camera snapshot')
    args = parser.parse_args()
    if not sys.platform.startswith('linux'):
        parser.error('This test requires Linux pipes and POSIX signals')
    root = Path(args.project_dir).expanduser().resolve()
    image = Path(args.image).expanduser().resolve()
    source = root / 'src/person_tracker'
    detector = root / 'build/detector'
    model = root / 'models/yolo26n'
    for path in (source / 'server.py', detector, model / 'model.ncnn.param',
                 model / 'model.ncnn.bin', image):
        if not path.is_file():
            parser.error('Required file missing: ' + str(path))
    jpeg = image.read_bytes()
    if not jpeg.startswith(b'\xff\xd8') or not 4 <= len(jpeg) <= 12000000:
        parser.error('Expected a JPEG within the detector protocol size limit')
    sys.path.insert(0, str(source))
    from server import Engine
    emit('start', python=sys.version.split()[0], project=str(root),
         image_sha256=hashlib.sha256(jpeg).hexdigest(), camera_access=False)
    stalled_write(Engine, model, detector, jpeg)
    terminated_child(Engine, model, detector, jpeg)
    emit('all_pass', fault_checks=2, normal_inferences=2, camera_access=False)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        emit('failed', kind=type(exc).__name__, detail=str(exc))
        sys.exit(1)

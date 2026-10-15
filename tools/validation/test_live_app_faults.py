#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Real App fault checks with snapshots and zero-speed STOP only (Python 3.5).

Keep test_real_pipe.py in the same directory. No running tracking server is
started here. Supply a private runtime directory; the password is entered by
getpass and never written to logs, arguments, or environment variables.
"""
from __future__ import print_function

import argparse
import errno
import getpass
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
from urllib.error import URLError
from urllib.parse import urlsplit

from test_real_pipe import close_child, emit, guard, wait_stopped


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def capture_locked(app):
    with guard(15):
        with app.operation:
            return app.capture()


def cleanup_app(app, worker_done=None):
    """Cancel this App and reap only its own detector; no control server exists."""
    stop_ok = False
    try:
        with guard(8):
            stop_ok = app.stop('Fault-test cleanup: STOP only')
    finally:
        # Resuming precedes termination if an outer test guard interrupted a
        # worker while its detector was still SIGSTOP'ed.
        close_child(app.engine)
        if worker_done is not None and not worker_done.wait(3):
            raise RuntimeError('Calibration worker did not finish during cleanup')
        for handler in list(app.logger.handlers):
            filename = getattr(handler, 'baseFilename', '')
            if filename and Path(filename).parent == app.runtime_dir:
                app.logger.removeHandler(handler)
                handler.close()
    check(stop_ok, 'Cleanup STOP was not confirmed by the real camera')


def stop_confirmation(App, FastCamera, camera, user, password, model, detector, runtime):
    with guard(15):
        app = App(camera, user, password, model, rotation=180,
                  detector=detector, runtime_dir=runtime)
    real_motion = app.motion
    try:
        check(not app.enabled, 'App did not start in observation mode')
        capture_locked(app)
        bad_motion = FastCamera('http://127.0.0.1:1', user, password)
        original_bad_stop = bad_motion.stop
        refusal_codes = []

        def refused_stop():
            try:
                return original_bad_stop()
            except URLError as exc:
                refusal_codes.append(getattr(exc.reason, 'errno', None))
                raise

        bad_motion.stop = refused_stop
        app.motion = bad_motion
        with guard(5):
            confirmed = app.stop('Inject local TCP connection refusal')
        check(not confirmed and not app.enabled, 'Failed STOP did not disable control')
        check(errno.ECONNREFUSED in refusal_codes, 'Expected an actual TCP connection refusal')
        warning = app.snapshot().get('error')
        check(bool(warning) and bool(app.stop_error), 'Missing persistent STOP warning')
        capture_locked(app)
        check(app.snapshot().get('error') == warning, 'A successful frame cleared the STOP warning')
        for action in (app.start, app.calibrate):
            try:
                action()
            except ValueError as exc:
                check('停止尚未确认' in str(exc), 'Action was rejected for the wrong reason')
            else:
                raise AssertionError('Control action was accepted while STOP was unconfirmed')
        check(not app.enabled and not app.calibrating, 'Failed STOP permitted control state')
        app.motion = real_motion
        with guard(5):
            check(app.stop('Restore real zero-speed STOP'), 'Real STOP was not confirmed')
        check(app.stop_error is None and app.snapshot().get('error') is None,
              'Confirmed real STOP did not clear the warning')
        emit('persistent_stop_warning_pass', refused_errno=refusal_codes,
             captures=app.frames, enabled=app.enabled, warning_cleared=True)
    finally:
        # Always restore the actual camera before the final zero-speed STOP.
        app.motion = real_motion
        cleanup_app(app)


def calibration_timeout(App, EngineError, camera, user, password, model, detector, runtime):
    with guard(15):
        app = App(camera, user, password, model, rotation=180,
                  detector=detector, runtime_dir=runtime)
    done = threading.Event()
    worker_started = False
    pulse_attempts = []
    close_under_lock = []
    camera_calls = []
    original_request = app.camera.request
    original_close = app.engine.close
    original_worker = app._calibrate

    def request(*args, **kwargs):
        camera_calls.append(time.monotonic())
        return original_request(*args, **kwargs)

    def observed_close():
        acquired = app.operation.acquire(False)
        close_under_lock.append(not acquired)
        if acquired:
            app.operation.release()
        return original_close()

    def forbidden_pulse(*args, **kwargs):
        pulse_attempts.append(True)
        raise AssertionError('Calibration reached pulse: movement is forbidden in this test')

    def worker(generation):
        try:
            original_worker(generation)
        finally:
            done.set()

    app.camera.request = request
    app.engine.close = observed_close
    app.pulse = forbidden_pulse
    app._calibrate = worker
    try:
        capture_locked(app)
        check(not app.enabled and app.matrix is None, 'Unexpected initial calibration state')
        app.engine.p.send_signal(signal.SIGSTOP)
        with guard(3):
            wait_stopped(app.engine.p)
        started = time.monotonic()
        with guard(16):
            app.calibrate()
            worker_started = True
            check(done.wait(13), 'Calibration did not finish within its outer bound')
        elapsed = time.monotonic() - started
        status = app.snapshot()
        check(not pulse_attempts, 'Calibration tried to issue a movement pulse')
        check(close_under_lock == [True], 'Failed engine was not closed exactly once under operation lock')
        check(not app.calibrating and not app.enabled and app.matrix is None,
              'Calibration fault left control enabled or a usable matrix')
        check(app.engine.p.poll() is not None, 'Failed detector is still running')
        message = status.get('error') or ''
        check('Detector input timeout' in message or 'Detector timeout' in message,
              'Calibration did not fail through a detector pipe timeout')
        calls_before = len(camera_calls)
        rejected_at = time.monotonic()
        try:
            capture_locked(app)
        except EngineError:
            pass
        else:
            raise AssertionError('A later frame reused the terminated detector')
        check(len(camera_calls) == calls_before, 'Later capture contacted the camera after detector exit')
        check(time.monotonic() - rejected_at < 1, 'Dead detector rejection was not prompt')
        emit('calibration_pipe_fault_pass', seconds=round(elapsed, 4),
             close_under_operation_lock=close_under_lock, pulse_attempts=len(pulse_attempts),
             camera_requests=len(camera_calls), later_capture_camera_requests=0,
             detector_returncode=app.engine.p.returncode, enabled=app.enabled,
             calibrating=app.calibrating, calibrated=app.matrix is not None)
    finally:
        cleanup_app(app, done if worker_started else None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-dir', required=True)
    parser.add_argument('--camera', required=True, help='Camera http(s) origin, no credentials')
    parser.add_argument('--user', required=True)
    parser.add_argument('--runtime-dir', help='Parent directory for this private test log run')
    args = parser.parse_args()
    if not sys.platform.startswith('linux'):
        parser.error('Requires Linux and POSIX signals')
    parsed = urlsplit(args.camera)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
            or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        parser.error('--camera must be an http(s) origin without credentials')
    root = Path(args.project_dir).expanduser().resolve()
    model = root / 'models/yolo26n'
    detector = root / 'build/detector'
    for path in (root / 'src/person_tracker/server.py', detector,
                 model / 'model.ncnn.param', model / 'model.ncnn.bin'):
        if not path.is_file():
            parser.error('Required file missing: ' + str(path))
    # abspath supports non-existent output paths on Python 3.5.
    parent = Path(os.path.abspath(os.path.expanduser(args.runtime_dir))) if args.runtime_dir else root / 'var/checks'
    parent.mkdir(parents=True, exist_ok=True)
    runtime = Path(tempfile.mkdtemp(prefix='app-faults-', dir=str(parent)))
    sys.path.insert(0, str(root / 'src/person_tracker'))
    from server import App, EngineError, FastCamera, QUIT
    from ptz_control import Camera
    password = getpass.getpass('Camera password (memory only): ')
    original_move = Camera.move
    rejected_moves = []

    def zero_only(camera, pan=0, tilt=0, zoom=0, duration=None):
        if pan != 0 or tilt != 0 or zoom != 0:
            rejected_moves.append(True)
            raise AssertionError('Non-zero camera movement is forbidden in this test')
        return original_move(camera, pan=pan, tilt=tilt, zoom=zoom, duration=duration)

    Camera.move = zero_only
    QUIT.clear()
    emit('start', python=sys.version.split()[0], project=str(root), runtime=str(runtime),
         allowed_camera_calls='snapshot and zero-speed STOP only')
    try:
        stop_confirmation(App, FastCamera, args.camera, args.user, password,
                          model, detector, runtime / 'stop-warning')
        calibration_timeout(App, EngineError, args.camera, args.user, password,
                            model, detector, runtime / 'calibration-fault')
        check(not rejected_moves, 'An unexpected non-zero move was attempted')
        emit('all_pass', fault_checks=2, nonzero_motion_attempts=0)
    finally:
        QUIT.set()
        Camera.move = original_move
        del password


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        emit('failed', kind=type(exc).__name__, detail=str(exc))
        sys.exit(1)

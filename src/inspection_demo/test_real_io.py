"""Hardware-free adapter/API regressions; Python 3.5 standard library only.

All camera methods, detector observations and evidence subprocesses are fakes.
No sockets, RTSP processes, OpenCV import or real motion are used by this suite.
"""
import io
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from email.message import Message
from types import SimpleNamespace
import unittest
from unittest import mock
import xml.etree.ElementTree as ET

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import real_io
from real_io import RealIO
import demo_server


CAPABILITIES = b'''<PTZCap>
<AbsolutePanTiltPositionSpace><XRange><Min>0</Min><Max>3500</Max></XRange>
<YRange><Min>0</Min><Max>900</Max></YRange></AbsolutePanTiltPositionSpace>
<AbsoluteZoomPositionSpace><ZRange><Min>10</Min><Max>20</Max></ZRange>
</AbsoluteZoomPositionSpace></PTZCap>'''


class FakeCamera:
    def __init__(self):
        self.pose = dict(pan=100., tilt=30., zoom=1.)
        self.commands = []
        self.capabilities = CAPABILITIES
        self.fail_stop = False
        self.entered = None
        self.release = None

    def request(self, path):
        if path.endswith('/capabilities'):
            return self.capabilities
        raise AssertionError('Unexpected camera request: ' + path)

    def status(self):
        return dict(azimuth=self.pose['pan'] * 10,
                    elevation=self.pose['tilt'] * 10,
                    absoluteZoom=self.pose['zoom'] * 10)

    def xml(self, path, body=None):
        if path.endswith('/focusConfiguration'):
            return {'focusStyle': 'AUTO'}
        if not path.endswith('/absolute') or body is None:
            raise AssertionError('Unexpected motion: ' + path)
        values = {item.tag.split('}')[-1]: item.text for item in ET.fromstring(body).iter()}
        self.pose = {name: int(values[field]) / 10. for name, field in
                     [('pan', 'azimuth'), ('tilt', 'elevation'), ('zoom', 'absoluteZoom')]}
        self.commands.append(('move', dict(self.pose)))
        if self.entered is not None:
            self.entered.set()
            if not self.release.wait(2):
                raise AssertionError('Test failed to release fake in-flight send')
        return {'statusString': 'OK'}

    def stop(self):
        self.commands.append(('stop', None))
        if self.fail_stop:
            raise OSError('Fake STOP failure')


class ReversalDeadTravelCamera(FakeCamera):
    """Hypothetical dead travel, not a claim about the physical S6 mechanism.

    Status follows commanded position, while 1.5 degrees after a direction
    reversal are absorbed before the image moves. Same-direction travel moves
    the image normally. This reproduces the unsafe small-probe assumption.
    """
    def __init__(self):
        super(ReversalDeadTravelCamera, self).__init__()
        self.image_pose = dict(self.pose)
        self.last_direction = dict(pan=-1., tilt=-1.)

    def xml(self, path, body=None):
        before = dict(self.pose)
        result = super(ReversalDeadTravelCamera, self).xml(path, body)
        if body is not None:
            for axis in ('pan', 'tilt'):
                delta = self.pose[axis] - before[axis]
                if delta:
                    direction = 1. if delta > 0 else -1.
                    travel = abs(delta)
                    if direction != self.last_direction[axis]:
                        travel = max(0., travel - 1.5)
                    self.image_pose[axis] += direction * travel
                    self.last_direction[axis] = direction
            self.image_pose['zoom'] = self.pose['zoom']
        return result


class FakeApp:
    def __init__(self):
        self.camera = self.motion = FakeCamera()
        self.lock = threading.RLock()
        self.operation = threading.Lock()
        self.calibrating = False
        self.preview_state = {'persons': [{'bbox': [.2, .1, .5, .8], 'confidence': .9}],
                              'frames': 71, 'width': 1280, 'height': 720}
        self.jpeg = b'fake-matched-JPEG-71'
        self.jpeg_b64 = 'ZmFrZQ=='
        self.preview_received = time.monotonic()
        self.state = {'error': None}
        self.engine = SimpleNamespace(p=SimpleNamespace(poll=lambda: None))
        self.video_source = None
        self.logs = []
        self.flow_reference = None
        self.capture_modes = []
        self.flow_response = .95
        self.bad_shift = None
        self.token = 'only-a-test-token'
        self.frames = 71
        self.source_sequence = 140
        self.last_request = time.monotonic()

    def log(self, kind, **data):
        self.logs.append((kind, data))

    def capture(self, hold_reference=False):
        self.capture_modes.append(hold_reference)
        self.frames += 1
        self.source_sequence += 1
        self.last_request = time.monotonic()
        self.state['decoded_frame_age_ms'] = 1.
        pose = dict(getattr(self.camera, 'image_pose', self.camera.pose))
        if not hold_reference:
            self.flow_reference = pose
        delta_pan = pose['pan'] - self.flow_reference['pan']
        delta_tilt = pose['tilt'] - self.flow_reference['tilt']
        shift = [-.01 * delta_pan + .002 * delta_tilt,
                 .001 * delta_pan - .02 * delta_tilt]
        return {'shift': self.bad_shift if self.bad_shift is not None else shift,
                'shift_response': self.flow_response}

    def snapshot(self):
        return dict(error=None, frame_age=.1, persons=[], frames=71)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.app = FakeApp()
        self.device = RealIO(self.app)
        self.cancel = threading.Event()
        # Camera status settles immediately; skip only deterministic settle sleeps.
        self.device.wait = lambda seconds, cancel: self.device.check_cancel(cancel)

    def test_construction_checks_capabilities_without_moving(self):
        self.assertTrue(self.device.capable)
        self.assertEqual(self.app.camera.commands, [])
        self.assertEqual(self.device.anchor, {'pan': 100., 'tilt': 30., 'zoom': 1.})

    def test_unknown_capability_limits_fail_closed_without_moving(self):
        self.app.camera.capabilities = CAPABILITIES.replace(b'3500', b'3600')
        with self.assertRaises(RuntimeError):
            RealIO(self.app)
        self.assertEqual(self.app.camera.commands, [])

    def test_bounded_absolute_command_uses_tenths_and_stops(self):
        actual = self.device.move(102.3, 31.2, 1.3, self.cancel)
        self.assertEqual(actual, dict(pan=102.3, tilt=31.2, zoom=1.3))
        self.assertEqual(self.app.camera.commands[0], ('move', actual))
        self.assertEqual(self.app.camera.commands[-1][0], 'stop')

    def test_invalid_positions_never_reach_camera(self):
        invalid = [(-1, 30, 1), (351, 30, 1), (100, -1, 1), (100, 91, 1),
                   (100, 30, .9), (100, 30, 1.6), (126, 30, 1), (100, 46, 1),
                   (float('nan'), 30, 1), (100, float('inf'), 1)]
        for pose in invalid:
            with self.subTest(pose=pose):
                with self.assertRaises(ValueError):
                    self.device.move(*(pose + (self.cancel,)))
        self.assertEqual(self.app.camera.commands, [])

    def test_cancelled_command_waiting_for_lock_cannot_follow_stop(self):
        outcome = []
        def move():
            try:
                self.device.move(101, 30, 1, self.cancel)
            except RuntimeError as exc:
                outcome.append(str(exc))
        with self.device.command:
            worker = threading.Thread(target=move)
            worker.start()
            self.cancel.set()
            self.device.stop()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(outcome)
        self.assertTrue(self.app.camera.commands)
        self.assertTrue(all(item[0] == 'stop' for item in self.app.camera.commands))

    def test_stop_serializes_inflight_send_and_no_later_nonzero_command(self):
        camera = self.app.camera
        camera.entered, camera.release = threading.Event(), threading.Event()
        failures = []
        def move():
            try:
                self.device.move(101, 30, 1, self.cancel)
            except RuntimeError as exc:
                failures.append(str(exc))
        mover = threading.Thread(target=move)
        mover.start()
        self.assertTrue(camera.entered.wait(1))
        self.cancel.set()
        stopper = threading.Thread(target=self.device.stop)
        stopper.start()
        camera.release.set()
        mover.join(2)
        stopper.join(2)
        self.assertFalse(mover.is_alive() or stopper.is_alive())
        self.assertTrue(failures)
        self.assertEqual([kind for kind, unused in camera.commands].count('move'), 1)
        first_stop = next(i for i, pair in enumerate(camera.commands) if pair[0] == 'stop')
        self.assertTrue(all(pair[0] == 'stop' for pair in camera.commands[first_stop:]))

    def test_unconfirmed_stop_blocks_subsequent_motion(self):
        self.app.camera.fail_stop = True
        with self.assertRaises(RuntimeError):
            self.device.stop()
        self.assertIn('STOP unconfirmed', self.device.stop_error)
        with self.assertRaises(RuntimeError):
            self.device.move(101, 30, 1, self.cancel)
        self.assertFalse(any(item[0] == 'move' for item in self.app.camera.commands))

    def test_ambiguous_send_timeout_always_attempts_stop(self):
        original = self.app.camera.xml
        def timeout_after_accept(path, body=None):
            original(path, body)
            raise OSError('Fake response lost after command acceptance')
        self.app.camera.xml = timeout_after_accept
        with self.assertRaises((RuntimeError, OSError)):
            self.device.move(101, 30, 1, self.cancel)
        self.assertEqual(self.app.camera.commands[0][0], 'move')
        self.assertEqual(self.app.camera.commands[-1][0], 'stop')

    def test_rtsp_reconnect_invalidates_calibration_before_motion(self):
        self.device.calibrations[1.] = [[-.01, 0], [0, -.02]]
        self.app.video_source = SimpleNamespace(status=lambda: {'restarts': 1, 'last_frame_age': .1})
        with self.assertRaises(RuntimeError):
            self.device.move(101, 30, 1, self.cancel)
        self.assertEqual(self.device.calibrations, {})
        self.assertFalse(any(item[0] == 'move' for item in self.app.camera.commands))

    def test_stale_or_nonfinite_rtsp_source_cannot_start_motion(self):
        for age in (None, 2., -1., float('nan'), float('inf')):
            self.app.video_source = SimpleNamespace(status=lambda age=age: {'restarts': 0, 'last_frame_age': age})
            with self.subTest(age=age):
                with self.assertRaises(RuntimeError):
                    self.device.move(101, 30, 1, self.cancel)
        self.assertFalse(any(item[0] == 'move' for item in self.app.camera.commands))

    def test_live_rtsp_cannot_hide_detector_failure_before_move(self):
        self.app.video_source = SimpleNamespace(status=lambda: {'restarts': 0, 'last_frame_age': .1})
        self.app.engine.p.poll = lambda: 7
        with self.assertRaises(RuntimeError):
            self.device.move(101, 30, 1, self.cancel)
        self.assertFalse(any(item[0] == 'move' for item in self.app.camera.commands))

    def test_capture_fault_cannot_start_coarse_move(self):
        self.app.state['error'] = 'Detector geometry changed'
        with self.assertRaises(RuntimeError):
            self.device.move(101, 30, 1, self.cancel)
        self.assertFalse(any(item[0] == 'move' for item in self.app.camera.commands))

    def test_observation_preserves_matched_jpeg_boxes_and_sequence(self):
        stamp = self.app.preview_received
        obs = self.device.observe(after=stamp - .01, timeout=.1)
        self.assertEqual(obs['jpeg'], b'fake-matched-JPEG-71')
        self.assertEqual(obs['sequence'], 71)
        self.assertEqual(obs['persons'], self.app.preview_state['persons'])
        self.assertEqual(obs['received_at'], stamp)

    def test_pre_motion_and_old_frames_are_rejected(self):
        for age, barrier_offset in ((2, -3), (.01, 1)):
            self.app.preview_received = time.monotonic() - age
            with self.assertRaises(RuntimeError):
                self.device.observe(after=self.app.preview_received + barrier_offset, timeout=.03)

    def test_future_receive_timestamp_is_rejected(self):
        self.app.preview_received = time.monotonic() + 100
        with self.assertRaises(RuntimeError):
            self.device.observe(after=time.monotonic() - 1, timeout=.03)

    def test_capture_error_does_not_return_existing_preview(self):
        self.app.state['error'] = 'RTSP changed'
        with self.assertRaises(RuntimeError):
            self.device.observe(after=0, timeout=.03)

    def test_exited_detector_cannot_satisfy_new_frame(self):
        self.app.preview_received = 0
        self.app.engine.p.poll = lambda: 2
        with self.assertRaises(RuntimeError):
            self.device.observe(after=0, timeout=.03)

    def test_fresh_cached_preview_does_not_hide_exited_detector(self):
        self.app.engine.p.poll = lambda: 7
        with self.assertRaises(RuntimeError):
            self.device.observe(after=self.app.preview_received - .01, timeout=.03)

    def test_calibration_uses_held_reference_and_degrees_then_returns_home(self):
        matrix = self.device.calibrate(1., self.cancel)
        for got, expected in zip(matrix, [[-.01, .002], [.001, -.02]]):
            for value, reference in zip(got, expected):
                self.assertAlmostEqual(value, reference, places=8)
        self.assertEqual(self.app.capture_modes, [False] + [True] * 5 + [False] + [True] * 5)
        self.assertEqual(self.app.camera.pose, {'pan': 100., 'tilt': 30., 'zoom': 1.})
        self.assertFalse(self.app.calibrating)
        count = len(self.app.camera.commands)
        self.assertEqual(self.device.calibrate(1., self.cancel), matrix)
        self.assertEqual(len(self.app.camera.commands), count)

    def test_weak_first_attempt_rebuilds_reference_and_accepts_second(self):
        original = self.app.capture
        def weak_small_step(hold_reference=False):
            result = original(hold_reference)
            displacement = max(abs(self.app.camera.pose[axis] - self.app.flow_reference[axis])
                               for axis in ('pan', 'tilt'))
            if hold_reference and displacement < 2.:
                result['shift'] = [.0002, .0002]
            return result
        self.app.capture = weak_small_step
        matrix = self.device.calibrate(1., self.cancel)
        self.assertEqual(len(matrix), 2)
        attempts = [event for event in self.device.events if event['event'] == 'calibration-attempt']
        self.assertEqual([(e['axis'], e['attempt'], e['accepted']) for e in attempts],
                         [('pan', 1, False), ('pan', 2, True), ('tilt', 1, False), ('tilt', 2, True)])
        self.assertEqual(self.app.capture_modes, ([False] + [True] * 5) * 4)
        for event in attempts:
            expected = dict(pan=100., tilt=30., zoom=1.)
            expected[event['axis']] += 3.
            self.assertEqual(event['home'], dict(pan=100., tilt=30., zoom=1.))
            self.assertEqual(event['before'], expected)
            self.assertEqual(event['preload'], expected)
            self.assertEqual(event['reference_pose'], expected)
            self.assertEqual(event['preload_degrees'], 3.)
            self.assertAlmostEqual(event['delta'], 1.5 if event['attempt'] == 1 else 3.)
            sequences = [item['sequence'] for item in event['observations']]
            self.assertEqual(len(set(sequences)), 5)
            self.assertGreater(min(sequences), event['reference']['sequence'])
            self.assertTrue(all('received_at' in item and 'source_sequence' in item
                                for item in event['observations']))
        self.assertGreater(attempts[1]['reference']['sequence'], attempts[0]['observations'][-1]['sequence'])
        self.assertEqual(self.app.camera.pose, {'pan': 100., 'tilt': 30., 'zoom': 1.})

    def test_inconsistent_first_attempt_can_retry_without_lowering_response_threshold(self):
        original = self.app.capture
        def low_response_small_step(hold_reference=False):
            result = original(hold_reference)
            displacement = max(abs(self.app.camera.pose[axis] - self.app.flow_reference[axis])
                               for axis in ('pan', 'tilt'))
            if hold_reference and displacement < 2.:
                result['shift_response'] = .59
            return result
        self.app.capture = low_response_small_step
        self.device.calibrate(1., self.cancel)
        attempts = [e for e in self.device.events if e['event'] == 'calibration-attempt']
        self.assertEqual([e['accepted'] for e in attempts], [False, True, False, True])
        self.assertTrue(all('inconsistent' in e['failure_reason'] for e in attempts if not e['accepted']))

    def test_two_failed_attempts_stop_without_return_or_partial_cache(self):
        original = self.app.capture
        def weak_tilt(hold_reference=False):
            result = original(hold_reference)
            if hold_reference and self.app.camera.pose['tilt'] != self.app.flow_reference['tilt']:
                result['shift'] = [.0002, .0002]
            return result
        self.app.capture = weak_tilt
        with self.assertRaisesRegex(RuntimeError, 'two bounded attempts'):
            self.device.calibrate(1., self.cancel)
        attempts = [e for e in self.device.events if e['event'] == 'calibration-attempt']
        self.assertEqual([(e['axis'], e['accepted']) for e in attempts],
                         [('pan', True), ('tilt', False), ('tilt', False)])
        self.assertEqual(self.device.calibrations, {})
        self.assertFalse(self.app.calibrating)
        self.assertEqual(self.app.camera.pose['tilt'], 36.)
        self.assertEqual(self.app.camera.commands[-1][0], 'stop')
        self.assertFalse(any(e['event'] == 'calibration' for e in self.device.events))

    def test_cancellation_before_retry_does_not_move_again(self):
        self.app.bad_shift = [.0002, .0002]
        original = self.device.event
        sent_at_cancel = []
        def cancel_after_rejection(kind, **data):
            original(kind, **data)
            if kind == 'calibration-attempt' and data.get('attempt') == 1 and not data.get('accepted'):
                sent_at_cancel.append(sum(item[0] == 'move' for item in self.app.camera.commands))
                self.cancel.set()
        self.device.event = cancel_after_rejection
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            self.device.calibrate(1., self.cancel)
        self.assertEqual(sum(item[0] == 'move' for item in self.app.camera.commands), sent_at_cancel[0])
        self.assertEqual(self.device.calibrations, {})
        self.assertEqual(self.app.camera.commands[-1][0], 'stop')

    def test_detector_failure_before_retry_does_not_move_again(self):
        self.app.bad_shift = [.0002, .0002]
        original = self.device.event
        sent_at_failure = []
        def fail_after_rejection(kind, **data):
            original(kind, **data)
            if kind == 'calibration-attempt' and data.get('attempt') == 1 and not data.get('accepted'):
                sent_at_failure.append(sum(item[0] == 'move' for item in self.app.camera.commands))
                self.app.engine.p.poll = lambda: 7
        self.device.event = fail_after_rejection
        with self.assertRaisesRegex(RuntimeError, 'Detector process exited'):
            self.device.calibrate(1., self.cancel)
        self.assertEqual(sum(item[0] == 'move' for item in self.app.camera.commands), sent_at_failure[0])
        self.assertEqual(self.device.calibrations, {})
        self.assertEqual(self.app.camera.commands[-1][0], 'stop')

    def test_capture_exception_aborts_without_retry_or_return_motion(self):
        original = self.app.capture
        def fail_held_frame(hold_reference=False):
            if hold_reference:
                raise OSError('Injected capture failure')
            return original(hold_reference)
        self.app.capture = fail_held_frame
        with self.assertRaisesRegex(OSError, 'Injected capture failure'):
            self.device.calibrate(1., self.cancel)
        moves = [value for kind, value in self.app.camera.commands if kind == 'move']
        self.assertEqual(len(moves), 3)
        self.assertEqual(moves[-1]['pan'], 104.5)
        self.assertEqual(self.app.camera.commands[-1][0], 'stop')
        self.assertEqual(self.device.calibrations, {})

    def test_stale_calibration_reference_aborts_before_measurement_motion(self):
        original = self.app.capture
        def stale_capture(hold_reference=False):
            result = original(hold_reference)
            self.app.last_request = time.monotonic() - 5
            return result
        self.app.capture = stale_capture
        with self.assertRaisesRegex(RuntimeError, 'newly received'):
            self.device.calibrate(1., self.cancel)
        moves = [pose for kind, pose in self.app.camera.commands if kind == 'move']
        self.assertEqual(moves, [{'pan': 100., 'tilt': 30., 'zoom': 1.},
                                 {'pan': 103., 'tilt': 30., 'zoom': 1.}])
        self.assertEqual(self.device.calibrations, {})
        self.assertEqual(self.app.camera.commands[-1][0], 'stop')

    def test_repeated_calibration_frame_number_is_not_fresh(self):
        original = self.app.capture
        def repeated_capture(hold_reference=False):
            previous = self.app.frames
            result = original(hold_reference)
            self.app.frames = previous
            return result
        self.app.capture = repeated_capture
        with self.assertRaisesRegex(RuntimeError, 'newly received'):
            self.device.calibrate(1., self.cancel)
        self.assertEqual(self.device.calibrations, {})
        self.assertEqual(sum(item[0] == 'move' for item in self.app.camera.commands), 2)

    def test_calibration_return_failure_cannot_cache_complete_matrix(self):
        original = self.app.camera.xml
        def fail_final_return(path, body=None):
            result = original(path, body)
            if sum(item[0] == 'move' for item in self.app.camera.commands) == 7:
                raise OSError('Return response lost')
            return result
        self.app.camera.xml = fail_final_return
        with self.assertRaisesRegex(OSError, 'Return response lost'):
            self.device.calibrate(1., self.cancel)
        self.assertEqual(self.device.calibrations, {})
        self.assertEqual(self.app.camera.commands[-1][0], 'stop')

    def test_calibration_retains_matrix_angle_gate(self):
        original = self.app.capture
        def collinear_flow(hold_reference=False):
            result = original(hold_reference)
            displacement = sum(self.app.camera.pose[axis] - self.app.flow_reference[axis]
                               for axis in ('pan', 'tilt'))
            result['shift'] = [displacement * .01, 0.]
            return result
        self.app.capture = collinear_flow
        with self.assertRaisesRegex(RuntimeError, 'weak or ambiguous'):
            self.device.calibrate(1., self.cancel)
        self.assertEqual(self.device.calibrations, {})
        self.assertEqual(self.app.camera.commands[-1][0], 'stop')

    def test_attempt_direction_respects_hardware_and_anchor_margins(self):
        for home, anchor in ((dict(pan=349., tilt=89., zoom=1.), dict(pan=349., tilt=89., zoom=1.)),
                             (dict(pan=124.8, tilt=44.8, zoom=1.), dict(pan=100., tilt=30., zoom=1.))):
            with self.subTest(home=home):
                self.app.camera.pose = dict(home)
                self.device.anchor = dict(anchor)
                self.device.calibrations.clear()
                self.device.events = []
                self.app.camera.commands = []
                self.device.calibrate(1., self.cancel)
                attempts = [e for e in self.device.events if e['event'] == 'calibration-attempt']
                self.assertEqual([e['direction'] for e in attempts], [-1., -1.])
                for kind, pose in self.app.camera.commands:
                    if kind == 'move':
                        self.assertTrue(0 <= pose['pan'] <= 350 and 0 <= pose['tilt'] <= 90)
                        self.assertLessEqual(abs(pose['pan'] - anchor['pan']), 25.)
                        self.assertLessEqual(abs(pose['tilt'] - anchor['tilt']), 15.)

    def test_preload_recovers_same_direction_response_after_reversal_dead_travel(self):
        # Demonstrate why a direct 1.5-degree probe can have zero image response
        # even when status reports the full step in this hypothetical model.
        probe = FakeApp()
        probe.camera = probe.motion = ReversalDeadTravelCamera()
        probe.capture()
        device = RealIO(probe)
        device.wait = lambda seconds, cancel: device.check_cancel(cancel)
        device.move(101.5, 30., 1., self.cancel)
        self.assertEqual(probe.capture(hold_reference=True)['shift'], [0., 0.])
        self.assertEqual(device.position()['pan'], 101.5)

        self.app.camera = self.app.motion = ReversalDeadTravelCamera()
        self.device = RealIO(self.app)
        self.device.wait = lambda seconds, cancel: self.device.check_cancel(cancel)
        matrix = self.device.calibrate(1., self.cancel)
        for row, expected in zip(matrix, [[-.01, .002], [.001, -.02]]):
            for got, reference in zip(row, expected):
                self.assertAlmostEqual(got, reference, places=8)
        attempts = [e for e in self.device.events if e['event'] == 'calibration-attempt']
        self.assertEqual([(e['axis'], e['attempt'], e['accepted']) for e in attempts],
                         [('pan', 1, True), ('tilt', 1, True)])
        self.assertEqual(self.app.camera.pose, dict(pan=100., tilt=30., zoom=1.))

    def test_direction_reserves_full_preload_retry_span_and_arrival_tolerance(self):
        # At 344 degrees, +6.3 is unavailable despite the old +3.3 margin.
        self.app.camera.pose = dict(pan=344., tilt=84., zoom=1.)
        self.device.anchor = dict(self.app.camera.pose)
        self.device.calibrate(1., self.cancel)
        attempts = [e for e in self.device.events if e['event'] == 'calibration-attempt']
        self.assertEqual([e['direction'] for e in attempts], [-1., -1.])
        self.assertEqual([e['preload'][e['axis']] for e in attempts], [341., 81.])

    def test_no_safe_margin_on_either_axis_rejects_before_any_movement(self):
        # A hypothetical larger preload would fit pan's +/-25 envelope but
        # exceed tilt's +/-15 envelope. Check both axes before beginning pan.
        self.device.CALIBRATION_PRELOAD = 13.
        with self.assertRaisesRegex(RuntimeError, 'Insufficient safe range'):
            self.device.calibrate(1., self.cancel)
        self.assertFalse(any(kind == 'move' for kind, unused in self.app.camera.commands))
        self.assertEqual(self.device.calibrations, {})

    def test_position_outside_anchor_cannot_begin_preload(self):
        self.app.camera.pose['tilt'] = 46.
        with self.assertRaisesRegex(RuntimeError, 'outside safe calibration envelope'):
            self.device.calibrate(1., self.cancel)
        self.assertEqual(self.app.camera.commands, [])
        self.assertEqual(self.device.calibrations, {})

    def test_cancellation_after_preload_sends_no_measurement_or_return(self):
        original = self.device.event
        def cancel_at_preload(kind, **data):
            original(kind, **data)
            if kind == 'arrived' and data['position']['pan'] == 103.:
                self.cancel.set()
        self.device.event = cancel_at_preload
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            self.device.calibrate(1., self.cancel)
        moves = [pose for kind, pose in self.app.camera.commands if kind == 'move']
        self.assertEqual([pose['pan'] for pose in moves], [100., 103.])
        self.assertEqual(self.app.capture_modes, [])
        self.assertEqual(self.app.camera.commands[-1][0], 'stop')
        self.assertEqual(self.device.calibrations, {})

    def test_weak_calibration_not_cached(self):
        self.app.flow_response = .2
        with self.assertRaises(RuntimeError):
            self.device.calibrate(1., self.cancel)
        self.assertEqual(self.device.calibrations, {})
        self.assertFalse(self.app.calibrating)
        self.assertEqual(self.app.camera.commands[-1][0], 'stop')

    def test_nonfinite_calibration_not_cached(self):
        self.app.bad_shift = [float('nan'), float('nan')]
        with self.assertRaises((RuntimeError, ValueError)):
            self.device.calibrate(1., self.cancel)
        self.assertEqual(self.device.calibrations, {})

    def test_cancel_even_cached_calibration_never_moves(self):
        self.device.calibrations[1.] = [[-.01, 0], [0, -.02]]
        self.cancel.set()
        with self.assertRaises(RuntimeError):
            self.device.calibrate(1., self.cancel)
        self.assertEqual(self.app.camera.commands, [])

    def test_evidence_uses_observation_bytes_and_separate_python2_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            box = [.2, .1, .5, .8]
            def fake_worker(argv, timeout):
                self.assertEqual(argv[0], 'fake-python2')
                self.assertEqual(Path(argv[1]).name, 'evidence_worker.py')
                self.assertEqual(Path(argv[2]).read_bytes(), b'exact-frame-211')
                self.assertEqual(json.loads(argv[4]), box)
                self.assertEqual(timeout, 8)
                Path(argv[3]).write_bytes(b'fake-crop')
                return b'{"sharpness": 123.5}'
            with mock.patch.object(real_io.shutil, 'which', return_value='fake-python2'):
                with mock.patch.object(real_io.subprocess, 'check_output', side_effect=fake_worker):
                    record = self.device.save({'jpeg': b'exact-frame-211', 'sequence': 211},
                                              {'bbox': box, 'target_id': 'target-001'}, temporary)
            self.assertEqual(record['source_sequence'], 211)
            self.assertEqual(record['sharpness_status'], 'recorded_not_meter_validated')
            self.assertEqual(Path(temporary, record['image']).read_bytes(), b'exact-frame-211')

    def test_evidence_id_cannot_escape_output_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(real_io.subprocess, 'check_output') as child:
                with self.assertRaises(ValueError):
                    self.device.save({'jpeg': b'x', 'sequence': 1},
                                     {'bbox': [.1, .1, .5, .5], 'target_id': '../outside'}, temporary)
                child.assert_not_called()
            self.assertEqual(list(Path(temporary).iterdir()), [])


class HttpHandlerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.results = Path(self.temporary.name) / 'captures'
        self.results.mkdir()
        self.app = FakeApp()
        self.controller = SimpleNamespace(snapshot=lambda: {'results': []}, heartbeat=mock.Mock(),
                                          start=mock.Mock(), stop=mock.Mock())
        self.server = SimpleNamespace(app=self.app, controller=self.controller,
                                      device=SimpleNamespace(stop_error=None), results_dir=self.results)

    def call(self, method, path, body=b'{}', headers=None):
        handler = demo_server.Handler.__new__(demo_server.Handler)
        handler.server, handler.path = self.server, path
        handler.headers = Message()
        entries = {'X-Control-Token': self.app.token, 'Host': '127.0.0.1:8772',
                   'Origin': 'http://127.0.0.1:8772', 'Content-Length': str(len(body))}
        entries.update(headers or {})
        for key, value in entries.items():
            handler.headers[key] = value
        handler.rfile = io.BytesIO(body)
        responses = []
        handler.reply = lambda data, status=200, **extra: responses.append((status, data, extra))
        getattr(handler, 'do_' + method)()
        self.assertEqual(len(responses), 1)
        return responses[0]

    def test_valid_start_requires_boolean_and_calls_controller_once(self):
        status, unused, unused2 = self.call('POST', '/api/start', b'{"scan":true}')
        self.assertEqual(status, 200)
        self.controller.start.assert_called_once_with(scan=True)

    def test_start_rejects_wrong_types_extra_keys_and_invalid_json(self):
        for body in (b'[]', b'null', b'{"scan":1}', b'{"scan":false,"speed":100}', b'{'):
            with self.subTest(body=body):
                self.assertGreaterEqual(self.call('POST', '/api/start', body)[0], 400)
        self.controller.start.assert_not_called()

    def test_mutations_require_token_and_same_origin(self):
        for headers in ({'X-Control-Token': 'wrong'}, {'Origin': 'http://other.test'}):
            self.assertEqual(self.call('POST', '/api/start', b'{"scan":true}', headers)[0], 403)
        self.controller.start.assert_not_called()

    def test_declared_oversize_is_rejected_before_controller(self):
        self.assertEqual(self.call('POST', '/api/start', headers={'Content-Length': '1025'})[0], 413)
        self.controller.start.assert_not_called()

    def test_start_rejects_stale_frame_or_unconfirmed_stop(self):
        self.app.snapshot = lambda: {'error': None, 'frame_age': 2}
        self.assertGreaterEqual(self.call('POST', '/api/start', b'{"scan":false}')[0], 400)
        self.app.snapshot = lambda: {'error': None, 'frame_age': .1}
        self.server.device.stop_error = 'STOP unconfirmed'
        self.assertGreaterEqual(self.call('POST', '/api/start', b'{"scan":false}')[0], 400)
        self.controller.start.assert_not_called()

    def test_heartbeat_and_stop_require_empty_object(self):
        for endpoint, callback in (('/api/heartbeat', self.controller.heartbeat),
                                   ('/api/stop', self.controller.stop)):
            for body in (b'[]', b'null', b'{"unknown":true}'):
                with self.subTest(endpoint=endpoint, body=body):
                    self.assertGreaterEqual(self.call('POST', endpoint, body)[0], 400)
            callback.assert_not_called()
            self.assertEqual(self.call('POST', endpoint)[0], 200)
            self.assertEqual(callback.call_count, 1)

    def test_capture_path_rejects_encoded_traversal(self):
        for path in ('/captures/%2e%2e/secret.json', '/captures/a/%2e%2e',
                     '/captures/a%2fb/c.jpg', '/captures/a/%5csecret.jpg',
                     '/captures/a/C%3Asecret.jpg', '/captures/a/%00.jpg'):
            with self.subTest(path=path):
                self.assertGreaterEqual(self.call('GET', path)[0], 400)

    def test_capture_symlink_cannot_escape_root(self):
        outside = Path(self.temporary.name) / 'outside'
        outside.mkdir()
        (outside / 'secret.json').write_text('{"private":true}')
        try:
            os.symlink(str(outside), str(self.results / 'linked'), target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('Platform does not permit unprivileged symlinks')
        status, data, unused = self.call('GET', '/captures/linked/secret.json')
        self.assertGreaterEqual(status, 400)
        self.assertNotEqual(data, b'{"private":true}')

    def test_capture_valid_file_returns_saved_bytes(self):
        (self.results / 'run-1').mkdir()
        (self.results / 'run-1' / 'target-1.jpg').write_bytes(b'only-test-evidence')
        status, data, extra = self.call('GET', '/captures/run-1/target-1.jpg')
        self.assertEqual((status, data), (200, b'only-test-evidence'))
        self.assertEqual(extra['ctype'], 'image/jpeg')


if __name__ == '__main__':
    unittest.main()

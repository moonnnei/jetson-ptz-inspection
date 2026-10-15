"""Deterministic controller tests; no camera, network, or third-party modules."""
from __future__ import division

import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

from controller import (Controller, InspectionError, angle_distance,
                        correction_degrees, inverse_2x2, merge_sightings,
                        nearest_route, project_person, valid_person)


class FakeIO(object):
    def __init__(self, targets=None, block_move=False, fail_first_save=False):
        self.pose = {'pan': 100.0, 'tilt': 45.0, 'zoom': 1.0}
        self.targets = targets if targets is not None else [96.0, 104.0]
        self.sequence = 0
        self.moves = []
        self.calibrations = []
        self.stops = 0
        self.saved = []
        self.block_move = block_move
        self.fail_first_save = fail_first_save
        self.entered = threading.Event()

    def position(self):
        return dict(self.pose)

    def move(self, pan, tilt, zoom, cancel):
        if cancel.is_set():
            return
        self.moves.append((pan, tilt, zoom))
        self.entered.set()
        if self.block_move:
            cancel.wait(1.0)
        if not cancel.is_set():
            self.pose = {'pan': pan, 'tilt': tilt, 'zoom': zoom}

    def calibrate(self, zoom, cancel):
        self.calibrations.append(zoom)
        return [[-0.04 * zoom, 0.0], [0.0, -0.04 * zoom]]

    def observe(self, after=None, timeout=3.0):
        self.sequence += 1
        zoom = self.pose['zoom']
        people = []
        for pan in self.targets:
            x = 0.5 - 0.04 * zoom * (self.pose['pan'] - pan)
            y = 0.5 - 0.04 * zoom * (self.pose['tilt'] - 45.0)
            width, height = 0.10 * zoom, 0.30 * zoom
            people.append({'bbox': [x - width / 2, y - height / 2,
                                    x + width / 2, y + height / 2],
                           'confidence': 0.95})
        return {'sequence': self.sequence, 'received_at': time.monotonic(),
                'persons': people, 'jpeg': ('jpeg-%d' % self.sequence).encode('ascii')}

    def stop(self):
        self.stops += 1

    def save(self, observation, record, run_dir):
        if self.fail_first_save and not self.saved:
            self.fail_first_save = False
            raise IOError('simulated disk failure')
        assert observation['jpeg'] == ('jpeg-%d' % observation['sequence']).encode('ascii')
        self.saved.append((observation, dict(record)))
        return {'image': record['target_id'] + '.jpg',
                'crop': record['target_id'] + '-crop.jpg', 'sharpness': 123.0,
                'source_sequence': observation['sequence'],
                'sharpness_status': 'instrument_thresholds_not_validated',
                'focus_style': 'camera_auto_focus'}


def wait_finished(controller, heartbeat=True, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if heartbeat:
            controller.heartbeat()
        if not controller.snapshot()['active']:
            controller._thread.join(1.0)
            controller._watchdog.join(1.0)
            if controller._thread.is_alive() or controller._watchdog.is_alive():
                raise AssertionError('inspection threads did not exit')
            return controller.snapshot()
        time.sleep(0.005)
    controller.stop('test_timeout')
    raise AssertionError('controller did not finish')


class MathTests(unittest.TestCase):
    def test_full_coupled_jacobian_inverse(self):
        matrix = [[-0.04, 0.01], [0.005, -0.03]]
        movement = [2.0, -1.0]
        error = [sum(row[i] * movement[i] for i in range(2)) for row in matrix]
        correction = correction_degrees(matrix, error)
        self.assertAlmostEqual(correction[0], -2.0)
        self.assertAlmostEqual(correction[1], 1.0)

    def test_bounded_correction_preserves_direction(self):
        result = correction_degrees([[-0.01, 0], [0, -0.01]], [0.2, 0.1], limit=2)
        self.assertEqual(result, [2.0, 1.0])

    def test_invalid_calibration_rejected(self):
        for matrix in ([[0, 0], [0, 0]], [[1, 2], [2, 4]],
                       [[float('nan'), 0], [0, 1]], [[1, 0]]):
            with self.assertRaises(InspectionError):
                inverse_2x2(matrix)

    def test_project_person_estimates_fixed_bearing(self):
        person = {'bbox': [0.55, 0.35, 0.65, 0.65], 'confidence': 0.9}
        result = project_person(person, {'pan': 100, 'tilt': 45, 'zoom': 1},
                                [[-0.04, 0], [0, -0.04]])
        self.assertAlmostEqual(result['pan'], 102.5)
        self.assertAlmostEqual(result['tilt'], 45)

    def test_reject_cutoff_and_tiny_people(self):
        self.assertIsNone(valid_person({'bbox': [0, 0.2, 0.2, 0.7], 'confidence': 0.9}))
        self.assertIsNone(valid_person({'bbox': [0.4, 0.4, 0.42, 0.45], 'confidence': 0.9}))
        self.assertIsNone(valid_person({'bbox': [0.4, 0.2, 0.6, 0.8], 'confidence': 0.5}))

    def test_two_close_people_remain_two_targets(self):
        targets = []
        pose = {'pan': 100, 'tilt': 45, 'zoom': 1}
        first = [{'pan': 100, 'tilt': 45, 'bbox': [0.3, 0.2, 0.4, 0.8], 'confidence': 0.9},
                 {'pan': 102, 'tilt': 45, 'bbox': [0.5, 0.2, 0.6, 0.8], 'confidence': 0.9}]
        self.assertEqual(merge_sightings(targets, first, pose, 1), 0)
        self.assertEqual(len(targets), 2)
        self.assertEqual(merge_sightings(targets, first, pose, 2), 0)
        self.assertEqual([target['hits'] for target in targets], [2, 2])
        # A repeated frame must not confirm another sighting.
        merge_sightings(targets, first, pose, 2)
        self.assertEqual([target['hits'] for target in targets], [2, 2])
        middle = [dict(first[0], pan=101)]
        self.assertEqual(merge_sightings(targets, middle, pose, 3), 1)
        self.assertEqual(len(targets), 2)
        self.assertEqual([target['hits'] for target in targets], [2, 2])

    def test_nearest_route_and_no_wrap(self):
        targets = [{'target_id': name, 'pan': pan, 'tilt': 45}
                   for name, pan in [('a', 100), ('b', 110), ('c', 108)]]
        self.assertEqual(nearest_route(targets, {'pan': 113, 'tilt': 45}), ['b', 'c', 'a'])
        self.assertEqual(angle_distance({'pan': 350, 'tilt': 45},
                                        {'pan': 0, 'tilt': 45}), 350)


class ControllerTests(unittest.TestCase):
    def test_scan_plan_capture_and_no_fabricated_reading(self):
        with tempfile.TemporaryDirectory() as directory:
            io = FakeIO()
            controller = Controller(io, directory)
            controller.start()
            state = wait_finished(controller)
            self.assertEqual(state['phase'], 'completed', state)
            self.assertEqual(len(state['targets']), 2)
            self.assertEqual(len(io.saved), 2)
            self.assertEqual(state['route'], ['person-002', 'person-001'])
            self.assertEqual(sorted(io.calibrations), [1.0, 1.5])
            for record in state['results']:
                self.assertEqual(record['status'], 'captured')
                self.assertIsNone(record['reading'])
                self.assertEqual(record['reading_status'], 'not_applicable_person_proxy')
                self.assertEqual(record['sharpness_status'], 'instrument_thresholds_not_validated')
            self.assertGreater(io.stops, 0)
            with open(os.path.join(directory, state['run_id'], 'run.json'), encoding='utf-8') as handle:
                saved = json.load(handle)
            self.assertFalse(saved['active'])
            self.assertEqual(saved['results'], state['results'])
            self.assertFalse(os.path.exists(os.path.join(directory, state['run_id'], 'run.json.tmp')))

    def test_no_target_fails_explicitly(self):
        with tempfile.TemporaryDirectory() as directory:
            io = FakeIO(targets=[])
            controller = Controller(io, directory)
            controller.start(scan=False)
            state = wait_finished(controller)
            self.assertEqual(state['phase'], 'error')
            self.assertIn('no complete person', state['error'])
            self.assertEqual(io.saved, [])

    def test_failed_target_does_not_prevent_next_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(FakeIO(fail_first_save=True), directory)
            controller.start()
            state = wait_finished(controller)
            self.assertEqual([item['status'] for item in state['results']], ['failed', 'captured'])
            self.assertIn('simulated disk failure', state['results'][0]['error'])
            self.assertEqual(state['phase'], 'completed_with_errors')

    def test_all_targets_failed_is_not_success(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(FakeIO(targets=[100], fail_first_save=True), directory)
            controller.start(scan=False)
            state = wait_finished(controller)
            self.assertEqual(state['phase'], 'completed_with_errors')
            self.assertEqual([item['status'] for item in state['results']], ['failed'])
            self.assertIn('成功 0/1', state['message'])

    def test_stop_prevents_subsequent_motion_and_parallel_start(self):
        with tempfile.TemporaryDirectory() as directory:
            io = FakeIO(block_move=True)
            controller = Controller(io, directory)
            controller.start()
            self.assertTrue(io.entered.wait(1.0))
            with self.assertRaises(InspectionError):
                controller.start()
            controller.stop('test_stop')
            count = len(io.moves)
            state = wait_finished(controller, heartbeat=False)
            time.sleep(0.03)
            self.assertEqual(len(io.moves), count)
            self.assertEqual(io.calibrations, [])
            self.assertEqual(state['phase'], 'stopped')
            self.assertGreaterEqual(io.stops, 1)

    def test_independent_watchdog_cancels_blocked_move(self):
        with tempfile.TemporaryDirectory() as directory:
            io = FakeIO(block_move=True)
            controller = Controller(io, directory)
            controller.HEARTBEAT_TIMEOUT = 0.12
            controller.start()
            self.assertTrue(io.entered.wait(1.0))
            state = wait_finished(controller, heartbeat=False)
            self.assertEqual(state['phase'], 'stopped')
            self.assertTrue(any('heartbeat_timeout' in event['message'] for event in state['events']))
            self.assertEqual(len(io.moves), 1)
            self.assertEqual(io.calibrations, [])

    def test_total_deadline_even_with_heartbeats(self):
        with tempfile.TemporaryDirectory() as directory:
            io = FakeIO(block_move=True)
            controller = Controller(io, directory)
            controller.TOTAL_TIMEOUT = 0.12
            controller.start()
            state = wait_finished(controller, heartbeat=True)
            self.assertEqual(state['phase'], 'stopped')
            self.assertTrue(any('total_deadline' in event['message'] for event in state['events']))
            self.assertEqual(io.calibrations, [])

    def test_ambiguous_reacquisition_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(FakeIO(), directory)
            target = {'pan': 100, 'tilt': 45}
            pose = {'pan': 100, 'tilt': 45, 'zoom': 1}
            observation = {'persons': [
                {'bbox': [0.43, 0.35, 0.53, 0.65], 'confidence': 0.9},
                {'bbox': [0.47, 0.35, 0.57, 0.65], 'confidence': 0.9}]}
            with self.assertRaises(InspectionError) as raised:
                controller._select(observation, pose, [[-0.04, 0], [0, -0.04]], target)
            self.assertIn('ambiguous', str(raised.exception))

    def test_same_frame_save_mismatch_is_failed(self):
        class MismatchIO(FakeIO):
            def save(self, observation, record, run_dir):
                result = super(MismatchIO, self).save(observation, record, run_dir)
                result['source_sequence'] += 1
                return result
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(MismatchIO(targets=[100]), directory)
            controller.start(scan=False)
            state = wait_finished(controller)
            self.assertEqual(state['results'][0]['status'], 'failed')
            self.assertIn('does not match', state['results'][0]['error'])

    def test_calibration_failure_stops_and_records_error(self):
        class BadCalibrationIO(FakeIO):
            def calibrate(self, zoom, cancel):
                return [[0, 0], [0, 0]]
        with tempfile.TemporaryDirectory() as directory:
            io = BadCalibrationIO()
            controller = Controller(io, directory)
            controller.start()
            state = wait_finished(controller)
            self.assertEqual(state['phase'], 'error')
            self.assertIn('singular', state['error'])
            self.assertGreater(io.stops, 0)

    def test_final_stop_failure_interlocks_restart_until_confirmed_stop(self):
        class FailingStopIO(FakeIO):
            fail_stop = True

            def stop(self):
                super(FailingStopIO, self).stop()
                if self.fail_stop:
                    raise RuntimeError('simulated STOP failure')

        with tempfile.TemporaryDirectory() as directory:
            io = FailingStopIO(targets=[100])
            controller = Controller(io, directory)
            controller.start(scan=False)
            state = wait_finished(controller)
            self.assertEqual(state['phase'], 'error')
            self.assertIn('simulated STOP failure', state['stop_error'])
            self.assertEqual(state['results'][0]['status'], 'captured')
            self.assertEqual(len(io.saved), 1)
            self.assertIn('已保存 1/1', state['message'])
            self.assertFalse(any(event['phase'] == 'completed' for event in state['events']))
            count = len(io.moves)
            with self.assertRaises(InspectionError) as raised:
                controller.start(scan=False)
            self.assertIn('STOP not confirmed', str(raised.exception))
            self.assertEqual(len(io.moves), count)
            io.fail_stop = False
            self.assertIsNone(controller.stop()['stop_error'])
            # The previous run remains an error even after STOP is recovered.
            self.assertEqual(controller.snapshot()['phase'], 'error')
            controller.start(scan=False)
            state = wait_finished(controller)
            self.assertEqual(state['phase'], 'completed', state)

    def test_stop_failure_after_target_error_prevents_next_target(self):
        class FailingRecoveryIO(FakeIO):
            def stop(self):
                self.stops += 1
                raise RuntimeError('STOP unavailable')

        with tempfile.TemporaryDirectory() as directory:
            io = FailingRecoveryIO(fail_first_save=True)
            controller = Controller(io, directory)
            controller.start()
            state = wait_finished(controller)
            self.assertEqual(state['phase'], 'error')
            self.assertEqual(io.saved, [])
            self.assertFalse(any(event['kind'] == 'target_error' for event in state['events']))
            self.assertEqual([target['status'] for target in state['targets']], ['pending', 'failed'])

    def test_successful_cleanup_stop_does_not_erase_run_stop_failure(self):
        class OneStopFailureIO(FakeIO):
            def stop(self):
                self.stops += 1
                if self.stops == 1:
                    raise RuntimeError('one failed STOP')

        with tempfile.TemporaryDirectory() as directory:
            io = OneStopFailureIO(fail_first_save=True)
            controller = Controller(io, directory)
            controller.start()
            state = wait_finished(controller)
            self.assertGreaterEqual(io.stops, 2)
            self.assertIsNone(state['stop_error'])
            self.assertEqual(state['phase'], 'error')
            self.assertIn('one failed STOP', state['error'])
            self.assertEqual(io.saved, [])
            self.assertFalse(any(event['phase'] == 'completed' for event in state['events']))

    def test_cancel_runtime_error_does_not_become_recoverable_target_failure(self):
        class CancellingIO(FakeIO):
            def __init__(self):
                super(CancellingIO, self).__init__(targets=[100])
                self.blocked = threading.Event()

            def move(self, pan, tilt, zoom, cancel):
                if len(self.moves) == 2:
                    self.moves.append((pan, tilt, zoom))
                    self.blocked.set()
                    cancel.wait(1.0)
                    raise RuntimeError('adapter interrupted by cancellation')
                super(CancellingIO, self).move(pan, tilt, zoom, cancel)

        with tempfile.TemporaryDirectory() as directory:
            io = CancellingIO()
            controller = Controller(io, directory)
            controller.start(scan=False)
            self.assertTrue(io.blocked.wait(1.0))
            controller.stop('cancel_during_target_move')
            count = len(io.moves)
            state = wait_finished(controller, heartbeat=False)
            self.assertEqual(state['phase'], 'stopped')
            self.assertEqual(state['results'], [])
            self.assertEqual(state['targets'][0]['status'], 'cancelled')
            self.assertFalse(any(event['kind'] == 'target_error' for event in state['events']))
            self.assertEqual(len(io.moves), count)

    def test_path_input_timezone_and_repeated_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(FakeIO(targets=[100]), Path(directory))
            run_ids = []
            for unused in range(4):
                controller.start(scan=False)
                state = wait_finished(controller)
                run_ids.append(state['run_id'])
                self.assertEqual(state['phase'], 'completed', state)
                self.assertTrue(state['started_at'].endswith('+00:00'))
                self.assertTrue(state['finished_at'].endswith('+00:00'))
                self.assertTrue(state['results'][0]['captured_at'].endswith('+00:00'))
                self.assertTrue(all(event['time'].endswith('+00:00') for event in state['events']))
                self.assertFalse(state['instrument_resolution_validated'])
                with open(os.path.join(directory, state['run_id'], 'run.json'), encoding='utf-8') as handle:
                    self.assertEqual(json.load(handle)['run_id'], state['run_id'])
            self.assertEqual(len(set(run_ids)), 4)

    def test_persist_and_restart_have_no_lock_order_deadlock(self):
        class ProbedController(Controller):
            def __init__(self, io, directory):
                super(ProbedController, self).__init__(io, directory)
                self.persist_entered = threading.Event()
                self.allow_snapshot = threading.Event()

            def snapshot(self):
                if threading.current_thread().name == 'persist-probe':
                    self.persist_entered.set()
                    self.allow_snapshot.wait(1.0)
                return super(ProbedController, self).snapshot()

        with tempfile.TemporaryDirectory() as directory:
            controller = ProbedController(FakeIO(targets=[100]), directory)
            controller.start(scan=False)
            first = wait_finished(controller)
            probe = threading.Thread(target=controller._persist, name='persist-probe')
            probe.daemon = True
            probe.start()
            self.assertTrue(controller.persist_entered.wait(1.0))
            errors = []

            def restart():
                try:
                    controller.start(scan=False)
                except Exception as exc:
                    errors.append(str(exc))

            starter = threading.Thread(target=restart, name='restart-probe')
            starter.daemon = True
            starter.start()
            time.sleep(0.03)
            controller.allow_snapshot.set()
            probe.join(1.0)
            starter.join(1.0)
            self.assertFalse(probe.is_alive())
            self.assertFalse(starter.is_alive())
            self.assertEqual(errors, [])
            second = wait_finished(controller)
            self.assertNotEqual(first['run_id'], second['run_id'])
            self.assertEqual(second['phase'], 'completed', second)

    def test_start_persistence_failure_does_not_leave_phantom_active_run(self):
        class FailingPersistenceController(Controller):
            fail_persist = True

            def _persist(self):
                if self.fail_persist:
                    raise IOError('simulated run record write failure')
                return super(FailingPersistenceController, self)._persist()

        with tempfile.TemporaryDirectory() as directory:
            io = FakeIO(targets=[100])
            controller = FailingPersistenceController(io, directory)
            with self.assertRaises(IOError):
                controller.start(scan=False)
            self.assertFalse(controller.snapshot()['active'])
            self.assertEqual(io.moves, [])
            controller.fail_persist = False
            controller.start(scan=False)
            self.assertEqual(wait_finished(controller)['phase'], 'completed')


if __name__ == '__main__':
    unittest.main()

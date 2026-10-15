"""Concurrent tracking tests; mocked ISAPI only, no physical camera movement."""
import threading
import time
import unittest
from unittest import mock

from server import QUIT
from test_control import app


class GatedMotion:
    """Keep an HTTP MOVE in flight until the test explicitly releases it."""
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.moves = []
        self.stops = 0
        self.events = []

    def move(self, **kwargs):
        self.moves.append(kwargs)
        self.events.append('move')
        self.entered.set()
        if not self.release.wait(2):
            raise RuntimeError('Test MOVE gate was not released')

    def stop(self):
        self.stops += 1
        self.events.append('stop')


class Tests(unittest.TestCase):
    def setUp(self):
        QUIT.clear()
        self.apps = []
        self.threads = []

    def tearDown(self):
        QUIT.set()
        for a in self.apps:
            if a.motion_context:
                a.motion_context['cancel'].set()
            if isinstance(a.motion, GatedMotion):
                a.motion.release.set()
        for thread in self.threads:
            thread.join(2)
        for a in self.apps:
            self.assertTrue(a.wait_motion(2), 'A motion worker remained alive')

    def responsive(self):
        a = app()
        a.motion_mode = 'responsive'
        a.motion = GatedMotion()
        a.matrix = [[0.03, 0], [0, 0.03]]
        self.apps.append(a)
        return a

    def action(self, axis='pan', speed=-20, duration=500):
        return dict(axis=axis, speed=speed, duration=duration,
                    error=[0.25, 0])

    def submit(self, a, action=None):
        if action is None:
            action = self.action()
        self.assertTrue(a.update_motion(action, a.generation, time.monotonic()))
        self.assertTrue(a.motion.entered.wait(1), 'MOVE did not begin')
        return a.motion_context

    def thread(self, function):
        thread = threading.Thread(target=function)
        self.threads.append(thread)
        thread.start()
        return thread

    def test_motion_is_bounded_to_tracking_cap(self):
        a = self.responsive()
        self.submit(a)
        self.assertEqual(a.motion.moves, [dict(pan=-20, duration=200)])
        a.motion.release.set()
        self.assertTrue(a.wait_motion(1))
        self.assertGreaterEqual(a.motion.stops, 1)
        self.assertIsNone(a.motion_context)

    def test_busy_same_direction_does_not_queue_or_block(self):
        a = self.responsive()
        context = self.submit(a)
        returned = threading.Event()
        results = []
        def feedback():
            results.append(a.update_motion(self.action(duration=100),
                                           a.generation, time.monotonic()))
            returned.set()
        self.thread(feedback)
        self.assertTrue(returned.wait(0.5), 'Feedback waited for MOVE completion')
        self.assertEqual(results, [False])
        self.assertIs(a.motion_context, context)
        self.assertEqual(len(a.motion.moves), 1)
        self.assertFalse(context['cancel'].is_set())
        a.motion.release.set()
        self.assertTrue(a.wait_motion(1))
        self.assertEqual(len(a.motion.moves), 1, 'A pending action was queued')

    def test_worker_start_failure_leaves_no_busy_context_or_move(self):
        a = self.responsive()
        with mock.patch('server.threading.Thread.start', side_effect=RuntimeError('No thread resources')):
            with self.assertRaises(RuntimeError):
                a.update_motion(self.action(), a.generation, time.monotonic())
        self.assertIsNone(a.motion_context)
        self.assertIsNone(a.motion_thread)
        self.assertFalse(a.state['motion_busy'])
        self.assertEqual(a.state['motion_phase'], 'idle')
        self.assertEqual(a.motion.moves, [])
        self.assertTrue(a.wait_motion())

    def cancel_feedback(self, a, action):
        context = self.submit(a)
        generation = a.generation
        attempted_stop = threading.Event()
        original_stop = a.confirm_stop
        def confirm_stop():
            attempted_stop.set()
            return original_stop()
        a.confirm_stop = confirm_stop
        outcome = []
        thread = self.thread(lambda: outcome.append(
            a.update_motion(action, generation, time.monotonic())))
        self.assertTrue(attempted_stop.wait(1))
        self.assertTrue(context['cancel'].is_set())
        self.assertNotEqual(a.generation, generation)
        self.assertIs(a.motion_context, context)
        self.assertFalse(a.update_motion(self.action(), a.generation,
                                         time.monotonic()), 'Replacement overlapped old motion')
        a.motion.release.set()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome, [False])
        self.assertTrue(a.wait_motion(1))
        self.assertGreaterEqual(a.motion.stops, 2)
        self.assertEqual(len(a.motion.moves), 1)

    def test_target_loss_cancels_and_stops_current_motion(self):
        self.cancel_feedback(self.responsive(), None)

    def test_centered_target_cancels_and_stops_current_motion(self):
        # The loop passes None when correction returns no action in its deadband.
        a = self.responsive()
        from tracking import correction
        centered = correction([0.4, 0.4, 0.6, 0.6], a.matrix, mode='adaptive')
        self.assertIsNone(centered)
        self.cancel_feedback(a, centered)

    def test_reverse_direction_cancels_without_replacement(self):
        self.cancel_feedback(self.responsive(), self.action(speed=20))

    def test_change_of_axis_cancels_without_replacement(self):
        self.cancel_feedback(self.responsive(), self.action(axis='tilt'))

    def test_stale_feedback_cancels_current_motion(self):
        a = self.responsive()
        context = self.submit(a)
        a.motion.release.set()
        self.assertFalse(a.update_motion(self.action(), a.generation,
                                         time.monotonic() - a.max_control_age - 1))
        self.assertTrue(context['cancel'].is_set())
        self.assertTrue(a.wait_motion(1))
        self.assertGreaterEqual(a.motion.stops, 1)

    def test_stop_racing_waiting_worker_prevents_move(self):
        a = self.responsive()
        attempted_stop = threading.Event()
        original_stop = a.confirm_stop
        def confirm_stop():
            attempted_stop.set()
            return original_stop()
        a.confirm_stop = confirm_stop
        a.command.acquire()
        try:
            generation = a.generation
            self.assertTrue(a.update_motion(self.action(), generation, time.monotonic()))
            thread = self.thread(a.stop)
            self.assertTrue(attempted_stop.wait(1))
            self.assertFalse(a.enabled)
            self.assertNotEqual(a.generation, generation)
        finally:
            a.command.release()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertTrue(a.wait_motion(1))
        self.assertEqual(a.motion.moves, [])
        self.assertGreaterEqual(a.motion.stops, 1)

    def test_stop_failure_disables_control_and_rejects_new_action(self):
        a = self.responsive()
        a.motion.stop = mock.Mock(side_effect=OSError('offline'))
        self.submit(a, self.action(duration=100))
        a.motion.release.set()
        self.assertTrue(a.wait_motion(1.5))
        self.assertFalse(a.enabled)
        self.assertIsNotNone(a.stop_error)
        self.assertIn('error', a.state)
        self.assertFalse(a.update_motion(self.action(), a.generation, time.monotonic()))
        self.assertEqual(len(a.motion.moves), 1)

    def test_before_barrier_frame_cannot_start_motion(self):
        a = self.responsive()
        received = time.monotonic()
        a.source_barrier = received + 0.01
        self.assertFalse(a.update_motion(self.action(), a.generation, received))
        self.assertEqual(a.motion.moves, [])
        self.assertIsNone(a.motion_context)

    def test_new_frame_after_barrier_can_start_motion(self):
        a = self.responsive()
        a.source_barrier = time.monotonic() - 0.01
        self.submit(a)
        self.assertEqual(len(a.motion.moves), 1)
        a.motion.release.set()

    def test_barrier_advanced_while_worker_waits_rejects_old_result(self):
        a = self.responsive()
        received = time.monotonic()
        a.command.acquire()
        try:
            self.assertTrue(a.update_motion(self.action(), a.generation, received))
            # A STOP completed after submit but before this worker acquired command.
            a.source_barrier = received + 0.01
        finally:
            a.command.release()
        self.assertTrue(a.wait_motion(1))
        self.assertEqual(a.motion.moves, [])
        self.assertGreaterEqual(a.motion.stops, 1)

    def test_old_or_nonfinite_feedback_cannot_start_motion(self):
        for received in (float('nan'), float('inf'), time.monotonic() - 2):
            a = self.responsive()
            self.assertFalse(a.update_motion(self.action(), a.generation, received))
            self.assertEqual(a.motion.moves, [])

    def test_too_short_duration_and_invalid_axes_never_move(self):
        for action in (self.action(duration=50), self.action(axis='zoom'),
                       self.action(speed=21), self.action(duration=True)):
            a = self.responsive()
            with self.assertRaises(ValueError):
                a.update_motion(action, a.generation, time.monotonic())
            self.assertEqual(a.motion.moves, [])
            self.assertIsNone(a.motion_context)

    def test_calibration_refuses_old_cancelled_worker_until_finished(self):
        a = self.responsive()
        context = self.submit(a)
        a.enabled = False
        context['cancel'].set()
        with self.assertRaises(ValueError):
            a.calibrate()
        self.assertFalse(a.calibrating)
        self.assertEqual(len(a.motion.moves), 1)
        a.motion.release.set()

    def test_loop_keeps_detecting_during_motion_http(self):
        a = self.responsive()
        person = dict(confidence=0.9, bbox=[0.7, 0.4, 0.9, 0.6])
        a.target = mock.Mock()
        a.target.update.return_value = person
        captures = []
        third_frame = threading.Event()
        def capture():
            a.last_request = a.last_frame = time.monotonic()
            captures.append(a.last_request)
            if len(captures) >= 3:
                third_frame.set()
            return dict(persons=[person])
        a.capture = capture
        loop = self.thread(a.loop)
        self.assertTrue(a.motion.entered.wait(1))
        self.assertTrue(third_frame.wait(1), 'Detection blocked on the motion worker')
        self.assertEqual(len(a.motion.moves), 1)
        self.assertTrue(a.motion_context is not None)
        QUIT.set()
        a.motion.release.set()
        loop.join(1)
        self.assertFalse(loop.is_alive())

    def test_expired_lease_prevents_motion_submit(self):
        a = self.responsive()
        a.lease = time.monotonic() - 1
        self.assertFalse(a.update_motion(self.action(), a.generation, time.monotonic()))
        self.assertEqual(a.motion.moves, [])

    def test_stopped_generation_cannot_submit_after_restart(self):
        a = self.responsive()
        generation = a.generation
        a.last_request = time.monotonic()
        a.target = mock.Mock()
        self.assertTrue(a.stop())
        a.start()
        self.assertTrue(a.enabled)
        self.assertFalse(a.update_motion(self.action(), generation, time.monotonic()))
        self.assertEqual(a.motion.moves, [])


if __name__ == '__main__':
    unittest.main()

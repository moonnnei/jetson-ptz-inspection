"""Fault-injection tests: camera calls are mocked; no hardware is moved."""
import threading
import time
import unittest
from unittest import mock
from server import App, EngineError, Handler, QUIT

class Motion:
    def __init__(self):self.moves=[];self.stops=0
    def move(self,**kwargs):self.moves.append(kwargs)
    def stop(self):self.stops+=1

def app():
    a=App.__new__(App)
    a.lock=threading.RLock();a.command=threading.Lock();a.operation=threading.Lock()
    a.enabled=True;a.calibrating=False;a.generation=1
    a.lease=time.monotonic()+3;a.last_frame=time.monotonic()
    a.motion=Motion();a.state={};a.stop_error=None;a.matrix=None;a.calibration_error=None
    a.last_request=0;a.frames=0;a.jpeg=None
    a.jpeg_b64=None;a.preview_state=None;a.last_preview=0;a.preview_received=0
    a.preview_requested_until=0
    a.motion_mode='serial';a.tracking_pulse_ms=200;a.motion_speed=20;a.preview_interval=0;a.max_control_age=.5
    a.motion_thread=None;a.motion_context=None;a.live_preview=None
    a.frame_source='snapshot';a.video_source=None;a.source_sequence=0;a.source_restarts=0;a.frame_geometry=None
    a.frame_policy='fresh';a.source_barrier=0;a.control_mode='fixed';a.loop_interval=.02
    a.engine=mock.Mock();a.engine.p.poll.return_value=None
    a.engine.infer.return_value=({'persons':[]},b'preview')
    a.camera=mock.Mock();a.camera.request.return_value=b'\xff\xd8test'
    a.log=lambda *args,**kwargs:None
    return a

class Tests(unittest.TestCase):
    def setUp(self):QUIT.clear()
    def tearDown(self):QUIT.set()
    def test_old_generation_cannot_move(self):
        a=app();a.stop();self.assertFalse(a.pulse('pan',20,1))
        self.assertEqual(a.motion.moves,[])
    def test_stop_failure_still_disables(self):
        a=app()
        def fail():raise OSError('offline')
        a.motion.stop=fail;a.stop()
        self.assertFalse(a.enabled);self.assertIn('error',a.state)
    def test_stop_during_pulse(self):
        a=app();t=threading.Thread(target=a.pulse,args=('pan',20,1));t.start()
        time.sleep(.08);a.stop();t.join(1)
        self.assertFalse(t.is_alive());self.assertEqual(len(a.motion.moves),1)
        self.assertGreaterEqual(a.motion.stops,2);self.assertFalse(a.enabled)
    def check_watchdog(self,a):
        t=threading.Thread(target=a.watchdog);t.start()
        time.sleep(.3);QUIT.set();t.join(1)
        self.assertFalse(a.enabled);self.assertGreaterEqual(a.motion.stops,1)
    def test_lost_heartbeat_stops(self):
        a=app();a.lease=time.monotonic()-1;self.check_watchdog(a)
    def test_stale_frame_stops(self):
        a=app();a.last_frame=time.monotonic()-3;self.check_watchdog(a)
    def test_stop_warning_survives_new_frame(self):
        a=app();a.motion.stop=mock.Mock(side_effect=OSError('offline'))
        self.assertFalse(a.stop());warning=a.snapshot()['error']
        a.capture()
        self.assertEqual(a.snapshot()['error'],warning)
        with self.assertRaises(ValueError):a.start()
        with self.assertRaises(ValueError):a.calibrate()
        self.assertEqual(a.motion.moves,[])
    def test_confirmed_stop_clears_warning(self):
        a=app();a.motion.stop=mock.Mock(side_effect=[OSError('offline'),None])
        self.assertFalse(a.stop());self.assertTrue(a.stop())
        self.assertIsNone(a.stop_error);self.assertIsNone(a.snapshot().get('error'))
    def test_failed_calibration_remains_visible_after_normal_capture(self):
        a=app();a.enabled=False;a.calibrating=True
        a.motion.status=mock.Mock(return_value={});a.pulse=mock.Mock(return_value=True)
        a.capture=mock.Mock(return_value={'shift':[0,0],'shift_response':1})
        a._calibrate(a.generation)
        self.assertIsNone(a.matrix);self.assertFalse(a.calibrating)
        self.assertIn('movement too small',a.snapshot()['error'])
        a.capture=lambda:App.capture(a);a.capture()
        self.assertIn('movement too small',a.snapshot()['error'])
        with mock.patch('server.threading.Thread'):
            a.calibrate()
        self.assertIsNone(a.calibration_error);self.assertIsNone(a.snapshot()['error'])
    def test_failed_stop_cancels_concurrent_start(self):
        a=app();a.matrix=[[.01,0],[0,.01]];a.target=mock.Mock()
        a.last_request=time.monotonic();entered=threading.Event();release=threading.Event()
        def pending_stop():
            entered.set()
            if not release.wait(1):raise RuntimeError('Test stop wait expired')
            raise OSError('offline')
        a.motion.stop=pending_stop
        thread=threading.Thread(target=a.stop);thread.start()
        try:
            self.assertTrue(entered.wait(1));a.start()
            started_generation=a.generation
        finally:
            release.set();thread.join(2)
        self.assertFalse(thread.is_alive());self.assertFalse(a.enabled)
        self.assertNotEqual(a.generation,started_generation)
        self.assertFalse(a.valid(a.generation));self.assertEqual(a.motion.moves,[])
    def test_stop_api_reports_unconfirmed_request(self):
        a=app();a.token='test';a.motion.stop=mock.Mock(side_effect=OSError('offline'))
        handler=Handler.__new__(Handler);handler.server=mock.Mock(app=a)
        handler.path='/api/stop';handler.headers={'X-Control-Token':'test'}
        handler.reply=mock.Mock();handler.do_POST()
        payload,status=handler.reply.call_args[0]
        self.assertEqual(status,409);self.assertIn('error',payload)
        self.assertFalse(a.enabled)
    def test_calibration_discards_failed_stream_while_locked(self):
        a=app();a.enabled=False;a.calibrating=True
        a.engine.infer.side_effect=RuntimeError('partial response timeout')
        closed_under_lock=[]
        def close():
            acquired=a.operation.acquire(False)
            closed_under_lock.append(not acquired)
            if acquired:a.operation.release()
            a.engine.p.poll.return_value=1
        a.engine.close.side_effect=close
        a._calibrate(a.generation)
        self.assertEqual(closed_under_lock,[True])
        self.assertFalse(a.calibrating);self.assertFalse(a.enabled)
        self.assertEqual(a.motion.moves,[])
        self.assertGreaterEqual(a.motion.stops,1)
        a.camera.request.reset_mock();a.engine.infer.reset_mock()
        with self.assertRaises(EngineError):a.capture()
        with self.assertRaises(ValueError):a.calibrate()
        self.assertFalse(a.camera.request.called);self.assertFalse(a.engine.infer.called)

if __name__=='__main__':unittest.main()

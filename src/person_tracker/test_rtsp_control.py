"""RTSP integration checks: no camera, network, or motor movement."""
import time
import unittest
from unittest import mock
from test_control import app
from rtsp_capture import Frame,CaptureError
from server import QUIT

class RTSPControlTests(unittest.TestCase):
    def setUp(self):QUIT.clear()
    def tearDown(self):QUIT.set()
    def rtsp(self):
        a=app();a.frame_source='rtsp';a.video_source=mock.Mock()
        a.target=mock.Mock()
        a.video_source.status.return_value={'restarts':0,'last_frame_age':0.05,'frames':10}
        a.engine.infer.return_value=({'persons':[],'width':1280,'height':720},b'preview')
        return a
    def fresh(self,sequence):
        return Frame(b'\xff\xd8jpeg\xff\xd9',sequence,time.monotonic())
    def test_rejects_pre_barrier_cache_and_never_requests_snapshot(self):
        a=self.rtsp();old=Frame(b'\xff\xd8old\xff\xd9',1,time.monotonic()-1)
        # Return the second frame at call time, strictly after capture starts.
        def read(**kwargs):return old if kwargs['after_sequence']==0 else self.fresh(2)
        a.video_source.read.side_effect=read
        a.capture()
        self.assertEqual(a.video_source.read.call_count,2)
        self.assertEqual(a.engine.infer.call_args[0][0],b'\xff\xd8jpeg\xff\xd9')
        self.assertFalse(a.camera.request.called);self.assertEqual(a.source_sequence,2)
    def test_source_timeout_does_not_infer_or_move(self):
        a=self.rtsp();a.video_source.read.side_effect=CaptureError('no fresh frame')
        with self.assertRaises(CaptureError):a.capture()
        self.assertFalse(a.engine.infer.called);self.assertEqual(a.motion.moves,[])
    def test_reconnection_during_inference_invalidates_result(self):
        a=self.rtsp();a.video_source.read.side_effect=lambda **k:self.fresh(1)
        a.video_source.status.side_effect=[{'restarts':0},{'restarts':1}]
        with self.assertRaises(CaptureError):a.capture()
        self.assertEqual(a.frames,0);self.assertEqual(a.motion.moves,[])
    def test_frame_geometry_change_rejects_result(self):
        a=self.rtsp();a.frame_geometry=(704,576)
        a.video_source.read.side_effect=lambda **k:self.fresh(1)
        with self.assertRaises(CaptureError):a.capture()
        self.assertEqual(a.frames,0)
    def test_stream_fault_clears_calibration_and_leaves_control_off(self):
        a=self.rtsp();a.matrix=[[.01,0],[0,.01]]
        a.capture=mock.Mock(side_effect=CaptureError('stream disconnected'))
        def stop_wait(*unused):QUIT.set();return True
        with mock.patch.object(QUIT,'wait',side_effect=stop_wait):a.loop()
        self.assertFalse(a.enabled);self.assertIsNone(a.matrix)
        self.assertTrue(a.target.reset.called);self.assertGreater(a.motion.stops,0)
        self.assertEqual(a.motion.moves,[])
    def test_stale_or_restarted_stream_cannot_pulse(self):
        a=self.rtsp()
        for status in ({'restarts':0,'last_frame_age':2},{'restarts':1,'last_frame_age':0.01}):
            a.video_source.status.return_value=status
            self.assertFalse(a.pulse('pan',20,a.generation))
        self.assertEqual(a.motion.moves,[]);self.assertGreaterEqual(a.motion.stops,2)
    def test_processed_frame_age_is_checked(self):
        a=self.rtsp();a.video_source.read.side_effect=lambda **k:self.fresh(1)
        clocks=iter([10,10,12])
        a.video_source.read.side_effect=lambda **k:Frame(b'\xff\xd8jpeg\xff\xd9',1,10)
        with mock.patch('server.time.monotonic',side_effect=lambda:next(clocks)):
            with self.assertRaises(RuntimeError):a.capture()
        self.assertEqual(a.frames,0)

if __name__=='__main__':unittest.main()

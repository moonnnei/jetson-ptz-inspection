"""Latency changes retain calibration, motion bounds and freshness gates."""
import struct
import time
import unittest
from unittest import mock
from pathlib import Path
from server import Engine,QUIT
from test_control import app
from rtsp_capture import Frame

class OptimizationTests(unittest.TestCase):
    def setUp(self):QUIT.clear()
    def tearDown(self):QUIT.set()
    def engine(self):
        engine=Engine.__new__(Engine);engine.write=mock.Mock()
        engine.read=mock.Mock(side_effect=[struct.pack('<I',2),b'{}',struct.pack('<I',4),b'jpeg'])
        return engine
    def test_infer_legacy_keeps_plain_length(self):
        engine=self.engine();engine.infer(b'jpeg')
        self.assertEqual(engine.write.call_args[0][0],struct.pack('<I',4)+b'jpeg')
    def test_light_flag_reaches_detector_without_changing_jpeg(self):
        engine=self.engine();engine.infer(b'jpeg',include_flow=False)
        self.assertEqual(engine.write.call_args[0][0],struct.pack('<I',0x80000004)+b'jpeg')
    def test_no_preview_flag_and_empty_block_leave_next_packet_untouched(self):
        engine=self.engine();payload=b'{"preview_generated":false}'
        engine.read=mock.Mock(side_effect=[struct.pack('<I',len(payload)),payload,struct.pack('<I',0)])
        result,preview=engine.infer(b'jpeg',include_flow=False,include_preview=False)
        self.assertEqual(engine.write.call_args[0][0],struct.pack('<I',0xc0000004)+b'jpeg')
        self.assertEqual(preview,b'');self.assertFalse(result['preview_generated'])
        self.assertEqual(engine.read.call_count,3)
    def test_unrequested_preview_is_protocol_error(self):
        engine=self.engine()
        with self.assertRaisesRegex(RuntimeError,'flag mismatch'):
            engine.infer(b'jpeg',include_preview=False)
    def test_preview_is_demand_driven_and_metadata_stays_with_its_image(self):
        a=app();a.preview_interval=.35
        a.engine.infer.return_value=({'persons':[{'bbox':[.1,.1,.2,.2]}],'preview_generated':False},b'')
        a.capture();self.assertFalse(a.engine.infer.call_args[1]['include_preview'])
        self.assertIsNone(a.snapshot(True)['jpeg'])
        a.engine.infer.return_value=({'persons':[{'bbox':[.2,.2,.3,.3]}],'preview_generated':True},b'paired')
        a.capture();self.assertTrue(a.engine.infer.call_args[1]['include_preview'])
        original=a.preview_state['persons'];original_sequence=a.preview_state['frames']
        a.engine.infer.return_value=({'persons':[{'bbox':[.6,.6,.7,.7]}],'preview_generated':False},b'')
        a.capture();snapshot=a.snapshot(True)
        self.assertEqual(snapshot['preview']['persons'],original)
        self.assertEqual(snapshot['preview']['frames'],original_sequence)
        self.assertNotEqual(snapshot['persons'],original)
        self.assertEqual(snapshot['jpeg'],'cGFpcmVk')
        with mock.patch('server.base64.b64encode',side_effect=AssertionError('Must reuse cached Base64')):
            self.assertEqual(a.snapshot(True)['jpeg'],'cGFpcmVk')
    def test_normal_capture_skips_flow_but_calibration_keeps_it(self):
        a=app();a.capture()
        self.assertFalse(a.engine.infer.call_args[1]['include_flow'])
        a.calibrating=True;a.capture()
        self.assertTrue(a.engine.infer.call_args[1]['include_flow'])
    def video(self):
        a=app();a.frame_source='rtsp';a.frame_policy='latest';a.video_source=mock.Mock()
        a.video_source.status.return_value={'restarts':0,'last_frame_age':0.01}
        return a
    def test_latest_unused_frame_does_not_wait_for_an_extra_frame(self):
        a=self.video();received=time.monotonic()-.02
        a.video_source.read.return_value=Frame(b'\xff\xd8jpeg\xff\xd9',1,received)
        a.capture();self.assertEqual(a.video_source.read.call_count,1)
        self.assertEqual(a.last_request,received)
    def test_motion_barrier_discards_frame_from_before_stop(self):
        a=self.video();a.source_barrier=time.monotonic()
        old=Frame(b'\xff\xd8old\xff\xd9',1,a.source_barrier-.01)
        a.video_source.read.side_effect=lambda **kw:old if kw['after_sequence']==0 else Frame(b'\xff\xd8new\xff\xd9',2,time.monotonic())
        a.capture();self.assertEqual(a.video_source.read.call_count,2)
        self.assertIn(b'new',a.engine.infer.call_args[0][0])
    def test_calibration_uses_capture_start_barrier_even_in_latest_mode(self):
        a=self.video();a.calibrating=True
        old=Frame(b'\xff\xd8old\xff\xd9',1,time.monotonic()-.01)
        a.video_source.read.side_effect=lambda **kw:old if kw['after_sequence']==0 else Frame(b'\xff\xd8new\xff\xd9',2,time.monotonic())
        a.capture();self.assertEqual(a.video_source.read.call_count,2)
    def test_short_pulse_keeps_stop_and_waits_its_own_duration(self):
        a=app();a.calibrating=False;a.lease=time.monotonic()+3
        start=time.monotonic();self.assertTrue(a.pulse('pan',20,a.generation,duration=100))
        elapsed=time.monotonic()-start
        self.assertEqual(a.motion.moves[0]['duration'],100)
        self.assertLess(elapsed,.45);self.assertGreaterEqual(elapsed,.25)
        self.assertGreater(a.source_barrier,start);self.assertGreater(a.motion.stops,0)
    def test_calibration_and_motion_bounds_cannot_be_bypassed(self):
        a=app()
        for duration in (99,501,True,100.0):
            with self.assertRaises(ValueError):a.pulse('pan',20,a.generation,duration=duration)
        with self.assertRaises(ValueError):a.pulse('pan',20,a.generation,True,duration=100)
        with self.assertRaises(ValueError):a.pulse('pan',40,a.generation,duration=500)
        self.assertEqual(a.motion.moves,[])
    def test_cpu_affinity_is_scoped_to_detector_command(self):
        child=mock.Mock()
        with mock.patch('server.subprocess.Popen',return_value=child) as spawn, mock.patch('server.os.set_blocking',create=True):
            Engine(Path('models'),detector=Path('detector'),threads=2,opencv_threads=1,detector_cpus='1,2')
        command=spawn.call_args[0][0]
        self.assertEqual(command[:3],['taskset','-c','1,2']);self.assertEqual(command[-2:],['2','1'])

if __name__=='__main__':unittest.main()

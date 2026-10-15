"""Fixed-reference IPC and stopped-only pulse tuning; no hardware calls."""
import io
import json
import struct
import unittest
from unittest import mock
from server import Engine, Handler
from test_control import app
from tracking import correction
from app_config import parse_args


class CalibrationReferenceTests(unittest.TestCase):
    def engine(self, held):
        e=Engine.__new__(Engine)
        result=json.dumps(dict(flow_reference_held=held,preview_generated=False)).encode()
        stream=io.BytesIO(struct.pack('<I',len(result))+result+struct.pack('<I',0))
        e.read=stream.read;e.write=mock.Mock()
        return e

    def test_held_request_and_response_keep_length_and_flags(self):
        e=self.engine(True);jpeg=b'\xff\xd8test\xff\xd9'
        result,preview=e.infer(jpeg,include_preview=False,hold_reference=True)
        header=struct.unpack('<I',e.write.call_args[0][0][:4])[0]
        self.assertEqual(header,0x60000000|len(jpeg))
        self.assertTrue(result['flow_reference_held']);self.assertEqual(preview,b'')

    def test_held_request_rejects_old_detector_response(self):
        with self.assertRaisesRegex(RuntimeError,'held flow reference'):
            self.engine(False).infer(b'jpeg',include_preview=False,hold_reference=True)

    def test_held_request_requires_flow_before_writing(self):
        e=self.engine(True)
        with self.assertRaises(ValueError):e.infer(b'jpeg',include_flow=False,hold_reference=True)
        self.assertFalse(e.write.called)

    def test_pulse_limit_boundaries_never_command_camera(self):
        a=app();a.enabled=False
        for duration in (100,200,500):
            a.set_pulse_limit(duration);self.assertEqual(a.tracking_pulse_ms,duration)
        for duration in (99,501,True,200.5,'200',None):
            with self.assertRaises(ValueError):a.set_pulse_limit(duration)
        self.assertEqual(a.motion.moves,[]);self.assertEqual(a.motion.stops,0)

    def test_pulse_limit_rejects_enabled_calibration_and_worker(self):
        for name in ('enabled','calibrating','motion_context'):
            a=app();a.enabled=False;setattr(a,name,True)
            with self.assertRaisesRegex(ValueError,'Stop tracking'):a.set_pulse_limit(500)
            self.assertEqual(a.tracking_pulse_ms,200);self.assertEqual(a.motion.moves,[])

    def test_pulse_limit_api_requires_token_and_exact_json(self):
        a=app();a.enabled=False;a.token='test'
        h=Handler.__new__(Handler);h.server=mock.Mock(app=a);h.path='/api/pulse-limit';h.reply=mock.Mock()
        for payload,token,expected in (({'duration_ms':500},'wrong',403),
                                       ({'duration_ms':500,'extra':1},'test',409),
                                       ({'duration_ms':500},'test',None)):
            body=json.dumps(payload).encode();h.rfile=io.BytesIO(body)
            h.headers={'X-Control-Token':token,'Content-Length':str(len(body))}
            h.reply.reset_mock();h.do_POST()
            if expected:self.assertEqual(h.reply.call_args[0][1],expected)
            else:self.assertEqual(h.reply.call_args[0][0],{'ok':True})
        self.assertEqual(a.tracking_pulse_ms,500);self.assertEqual(a.motion.moves,[])

    def test_calibration_holds_baseline_and_uses_absolute_displacement(self):
        a=app();a.enabled=False;a.calibrating=True;a.target=mock.Mock()
        a.motion.status=mock.Mock(return_value={});a.pulse=mock.Mock(return_value=True)
        observed=[]
        def capture(hold_reference=False):
            observed.append(hold_reference)
            # Six identical held shifts must count once, not six times.
            axis=0 if len(observed)<=13 else 1
            return {'shift':[.012,0] if axis==0 else [0,.012], 'shift_response':1}
        a.capture=capture;a._calibrate(a.generation)
        self.assertEqual(a.matrix,[[.012,0],[0,.012]])
        self.assertEqual(observed,[False]+[True]*6+[False]*6+[False]+[True]*6+[False]*6)
        self.assertFalse(a.calibrating);self.assertEqual(a.motion.moves,[])

    def test_slack_pulses_are_not_divisors_of_effective_pulse(self):
        a=app();a.enabled=False;a.calibrating=True;a.target=mock.Mock()
        a.motion.status=mock.Mock(return_value={});a.pulse=mock.Mock(return_value=True)
        calls=[0]
        def capture(hold_reference=False):
            calls[0]+=1
            # Pan: one effective pulse. Tilt: two zero-displacement takeups,
            # then one effective pulse. Every attempt freezes a new baseline.
            v=[.012,0] if calls[0]<=13 else ([0,.012] if calls[0]>=29 else [0,0])
            return {'shift':v,'shift_response':1}
        a.capture=capture;a._calibrate(a.generation)
        self.assertEqual(a.matrix,[[.012,0],[0,.012]])
        self.assertEqual(a.pulse.call_count,8)

    def test_speed_change_invalidates_old_matrix_and_requires_calibration(self):
        a=app();a.enabled=False;a.target=mock.Mock();a.matrix=[[.01,0],[0,.01]]
        generation=a.generation;a.set_motion_speed(40)
        self.assertEqual(a.motion_speed,40);self.assertIsNone(a.matrix)
        self.assertGreater(a.generation,generation)
        with self.assertRaises(ValueError):a.start()
        self.assertEqual(a.motion.moves,[])
        with self.assertRaises(ValueError):a.pulse('pan',20,a.generation)
        self.assertFalse(a.pulse('pan',40,a.generation))

    def test_speed_change_rejects_active_or_unconfirmed_stop(self):
        for name in ('enabled','calibrating','motion_context','stop_error'):
            a=app();a.enabled=False;setattr(a,name,True)
            with self.assertRaises(ValueError):a.set_motion_speed(40)
            self.assertEqual(a.motion_speed,20)

    def test_speed_limits_and_config_reject_nonintegers(self):
        a=app();a.enabled=False;a.target=mock.Mock()
        for speed in (19,41,True,40.0,'40'):
            with self.assertRaises(ValueError):a.set_motion_speed(speed)
            with self.assertRaises(ValueError):correction([.6,.4,.8,.6],[[.02,0],[0,.02]],speed=speed)
        with mock.patch('sys.stderr'):
            with self.assertRaises(SystemExit):parse_args(['--user','admin','--motion-speed','41'])
        self.assertEqual(parse_args(['--user','admin','--motion-speed','40']).motion_speed,40)

    def test_correction_preserves_calibrated_sign_and_selected_speed(self):
        matrix=[[.02,0],[0,-.02]]
        self.assertEqual(correction([.7,.4,.9,.6],matrix,speed=40)['speed'],-40)
        self.assertEqual(correction([.1,.4,.3,.6],matrix,speed=40)['speed'],40)

    def test_calibration_uses_selected_speed_in_both_directions(self):
        a=app();a.enabled=False;a.calibrating=True;a.motion_speed=40;a.target=mock.Mock()
        a.motion.status=mock.Mock(return_value={});a.pulse=mock.Mock(return_value=True)
        calls=[0]
        def capture(hold_reference=False):
            calls[0]+=1
            return {'shift':[.02,0] if calls[0]<=13 else [0,.02],'shift_response':1}
        a.capture=capture;a._calibrate(a.generation)
        self.assertEqual(a.matrix,[[.02,0],[0,.02]])
        self.assertEqual([call[0][1] for call in a.pulse.call_args_list],[40,-40,40,-40])


if __name__=='__main__':unittest.main()

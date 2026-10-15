"""Checks packaging/config behavior without camera access."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from app_config import PROJECT_ROOT,parse_args

class ConfigTests(unittest.TestCase):
    def test_default_paths_are_project_relative(self):
        args=parse_args(['--user','example-operator'])
        self.assertEqual(args.model_dir,PROJECT_ROOT/'models/yolo26n')
        self.assertEqual(args.detector,PROJECT_ROOT/'build/detector')
        self.assertIsNone(args.rtsp_cpus)
    def test_cli_overrides_config(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'config.json'
            path.write_text(json.dumps({'user':'example-operator','rotation':180,'port':8767}))
            args=parse_args(['--config',str(path),'--rotation','0','--port','8780'])
            self.assertEqual(args.rotation,0);self.assertEqual(args.port,8780)
    def test_credentials_in_url_are_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):parse_args(['--user','example-operator','--camera','http://user:example@camera'])
    def test_password_config_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'config.json';path.write_text(json.dumps({'password':'not-a-real-password'}))
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):parse_args(['--config',str(path)])
    def test_rtsp_mode_uses_explicit_endpoint_and_decoder(self):
        args=parse_args(['--user','example-operator','--frame-source','rtsp',
                        '--rtsp-url','rtsp://camera:1554/Streaming/channels/101',
                        '--rtsp-decoder','hardware','--rtsp-width','1280','--rtsp-height','720'])
        self.assertEqual(args.frame_source,'rtsp');self.assertEqual(args.rtsp_decoder,'hardware')
        self.assertEqual((args.rtsp_width,args.rtsp_height),(1280,720))
    def test_rtsp_missing_endpoint_and_credentials_rejected(self):
        for extra in ([],['--rtsp-url','rtsp://user:secret@camera:1554/Streaming/channels/101']):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):parse_args(['--user','example-operator','--frame-source','rtsp']+extra)
    def test_rtsp_scaling_requires_both_dimensions(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):parse_args(['--user','example-operator','--rtsp-width','1280'])
    def test_optimized_runtime_can_be_explicitly_configured(self):
        args=parse_args(['--user','example-operator','--detector-threads','2','--opencv-threads','1',
                        '--detector-cpus','1,2','--frame-policy','latest','--control-mode','adaptive'])
        self.assertEqual(args.detector_threads,2);self.assertEqual(args.detector_cpus,'1,2')
        self.assertEqual(args.control_mode,'adaptive')
    def test_responsive_profile_and_invalid_latency_bounds(self):
        args=parse_args(['--user','example-operator','--motion-mode','responsive','--tracking-pulse-ms','200',
                         '--preview-interval','.35','--max-control-age','.5'])
        self.assertEqual(args.motion_mode,'responsive');self.assertEqual(args.tracking_pulse_ms,200)
        for flags in (['--tracking-pulse-ms','99'],['--tracking-pulse-ms','501'],['--preview-interval','nan'],
                      ['--preview-interval','-1'],['--max-control-age','inf'],['--max-control-age','.01']):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):parse_args(['--user','example-operator']+flags)
    def test_unbounded_or_malformed_runtime_settings_are_rejected(self):
        for flags in (['--detector-threads','7'],['--opencv-threads','0'],['--detector-cpus','1,1'],
                      ['--detector-cpus','1;bad'],['--loop-interval','nan'],['--loop-interval','0']):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):parse_args(['--user','example-operator']+flags)
    def test_rtsp_affinity_cli_overrides_json_without_touching_detector_affinity(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'config.json'
            path.write_text(json.dumps({'user':'example-operator','rtsp_cpus':'0,3,4,5',
                                        'detector_cpus':'1,2'}))
            args=parse_args(['--config',str(path),'--rtsp-cpus','00,3'])
            self.assertEqual(args.rtsp_cpus,'0,3')
            self.assertEqual(args.detector_cpus,'1,2')
    def test_rtsp_affinity_rejects_flags_shell_syntax_and_invalid_json_types(self):
        for value in ('0;bad', '0-3', '0,0', '128', '0, 3', ['0','3'], True):
            with tempfile.TemporaryDirectory() as d:
                path=Path(d)/'config.json'
                path.write_text(json.dumps({'user':'example-operator','rtsp_cpus':value}))
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):parse_args(['--config',str(path)])

if __name__=='__main__':unittest.main()

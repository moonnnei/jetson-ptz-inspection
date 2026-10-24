"""Configuration mistakes must not expose credentials or arm motion."""
import sys
from pathlib import Path
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
from start_inspection import command


class StartupTests(unittest.TestCase):
    def config(self):
        return dict(camera_origin='http://camera.example',camera_user='admin',detector_root='models/private-runtime')

    def test_password_fields_and_credential_bearing_origins_are_rejected(self):
        for config in (dict(self.config(),password='forbidden'),
                       dict(self.config(),camera_origin='http://user:forbidden@camera.example'),
                       dict(self.config(),camera_origin='http://camera.example/path')):
            with self.assertRaises(ValueError):command(config)

    def test_preview_cannot_loop_into_console_and_invalid_ports_are_rejected(self):
        for patch in (dict(preview_port=8775),dict(port=True),dict(port=80),dict(preview_port=-1)):
            with self.assertRaises(ValueError):command(dict(self.config(),**patch))

    def test_startup_connects_backend_but_does_not_start_work(self):
        args=command(self.config())
        self.assertIn('--first-capture-password-stdin',args)
        self.assertIn('--scene-initialization',args)
        self.assertNotIn('--arm',args)
        self.assertNotIn('--start',args)
        self.assertEqual(args[args.index('--host')+1],'127.0.0.1')

    def test_image_store_must_match_adapter_console_root(self):
        with self.assertRaises(ValueError):command(dict(self.config(),data='other/registry'))
        args=command(dict(self.config(),data='other/var/inspection-console',console_root='other'))
        self.assertIn('--data',args)


if __name__=='__main__':unittest.main()

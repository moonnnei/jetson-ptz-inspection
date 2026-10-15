"""Exercise every JSON response branch without HTTP or hardware access."""
import base64
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/check_live.py'
SPEC = importlib.util.spec_from_file_location('checked_live_script', str(SCRIPT))
check_live = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check_live)
REAL_LOADS = json.loads


def strict_text_loads(value, *args, **kwargs):
    """Match Python 3.5, even when these tests run on a newer interpreter."""
    if not isinstance(value, str):
        raise TypeError('Python 3.5 requires str JSON input')
    return REAL_LOADS(value, *args, **kwargs)


class CheckLiveTests(unittest.TestCase):
    def check_action(self, action):
        state = dict(enabled=False, calibrated=True, calibrating=False,
                     message='测试画面', error=None,
                     jpeg=base64.b64encode(b'test-image').decode('ascii'))

        def fake_get(path):
            if path == '/':
                return b"const token='0123456789abcdef'"
            return json.dumps(state, ensure_ascii=False).encode('utf-8')

        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"ok":true}'
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(check_live, 'ROOT', Path(directory)), \
                    mock.patch.object(check_live, 'get', side_effect=fake_get), \
                    mock.patch.object(check_live, 'urlopen', return_value=response) as http, \
                    mock.patch.object(check_live.json, 'loads', side_effect=strict_text_loads) as parser, \
                    mock.patch.object(check_live.time, 'sleep'), \
                    mock.patch.object(check_live.time, 'monotonic', side_effect=[0, 0, 100]), \
                    mock.patch('sys.argv', ['check_live.py', action]), \
                    contextlib.redirect_stdout(io.StringIO()):
                check_live.main()
                self.assertGreater(parser.call_count, 0)
                if action in ('status', 'capture'):
                    self.assertFalse(http.called)
                if action == 'capture':
                    self.assertEqual((Path(directory) / 'live.jpg').read_bytes(), b'test-image')

    def test_status_uses_text_json(self):
        self.check_action('status')

    def test_capture_uses_text_json(self):
        self.check_action('capture')

    def test_lease_test_uses_text_json(self):
        self.check_action('lease-test')

    def test_calibrate_uses_text_json(self):
        self.check_action('calibrate')

    def test_track_uses_text_json(self):
        self.check_action('track')


if __name__ == '__main__':
    unittest.main()

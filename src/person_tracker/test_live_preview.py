"""Latest-frame preview fault/concurrency tests without hardware or GI."""
from collections import namedtuple
import io
import socket
import threading
import unittest
from unittest import mock

from live_preview import LivePreview
from rtsp_capture import CaptureError


Frame = namedtuple('Frame', 'jpeg sequence received_at')
JPEG_A = b'\xff\xd8first\xff\xd9'
JPEG_B = b'\xff\xd8second\xff\xd9'


class Handler:
    def __init__(self):
        self.connection = mock.Mock()
        self.connection.gettimeout.return_value = None
        self.wfile = io.BytesIO()
        self.headers = []
        self.status = None
        self.close_connection = False

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.headers.append((name, value))

    def end_headers(self):
        self.wfile.write(b'HTTP headers complete\r\n\r\n')


class PreviewTests(unittest.TestCase):
    def test_binary_parts_use_local_sequence_and_skip_superseded_frames(self):
        capture = mock.Mock()
        capture.source_sequence = 77
        capture.read.side_effect = [Frame(JPEG_A, 5, 0), Frame(JPEG_B, 17, 0),
                                    CaptureError('private-test-value')]
        handler = Handler()
        LivePreview(capture, threading.Event()).serve(handler)
        self.assertEqual(handler.status, 200)
        self.assertTrue(handler.close_connection)
        self.assertIn(('Content-Type', 'multipart/x-mixed-replace; boundary=jetson-live'),
                      handler.headers)
        self.assertNotIn('Content-Length', dict(handler.headers))
        self.assertEqual(capture.read.call_args_list,
                         [mock.call(after_sequence=0, timeout=1),
                          mock.call(after_sequence=5, timeout=1),
                          mock.call(after_sequence=17, timeout=1)])
        self.assertEqual(capture.source_sequence, 77)
        parts = [args[0] for args, kwargs in handler.connection.sendall.call_args_list]
        self.assertEqual(len(parts), 2)
        self.assertTrue(all(isinstance(part, bytes) for part in parts))
        self.assertIn(b'Content-Length: ' + str(len(JPEG_A)).encode() + b'\r\n', parts[0])
        self.assertIn(b'X-Source-Sequence: 17\r\n', parts[1])
        self.assertTrue(parts[1].endswith(JPEG_B + b'\r\n'))
        self.assertNotIn(b'private-test-value', b''.join(parts))
        self.assertEqual(handler.connection.settimeout.call_args_list,
                         [mock.call(1.0), mock.call(None)])

    def test_each_client_starts_with_its_own_sequence(self):
        capture = mock.Mock()
        capture.read.side_effect = [Frame(JPEG_A, 9, 0), CaptureError('ended'),
                                    Frame(JPEG_B, 12, 0), CaptureError('ended')]
        preview = LivePreview(capture, threading.Event())
        preview.serve(Handler())
        preview.serve(Handler())
        starts = [kwargs['after_sequence'] for args, kwargs in capture.read.call_args_list]
        self.assertEqual(starts, [0, 9, 0, 12])

    def test_write_timeout_releases_client_slot_and_sends_no_error_text(self):
        capture = mock.Mock()
        capture.read.side_effect = [Frame(JPEG_A, 1, 0), CaptureError('private-test-value')]
        preview = LivePreview(capture, threading.Event(), max_clients=1)
        slow = Handler()
        slow.connection.sendall.side_effect = socket.timeout('private-test-value')
        preview.serve(slow)
        next_client = Handler()
        preview.serve(next_client)
        self.assertEqual(slow.status, 200)
        self.assertEqual(next_client.status, 200)
        self.assertEqual(next_client.connection.sendall.call_count, 0)
        slow.connection.settimeout.assert_any_call(1.0)

    def test_header_failure_also_releases_client_slot(self):
        capture = mock.Mock()
        capture.read.side_effect = CaptureError('ended')
        preview = LivePreview(capture, threading.Event(), max_clients=1)
        broken = Handler()
        broken.end_headers = mock.Mock(side_effect=OSError('private-test-value'))
        preview.serve(broken)
        next_client = Handler()
        preview.serve(next_client)
        self.assertEqual(next_client.status, 200)

    def test_quit_before_stream_does_not_read_source(self):
        capture = mock.Mock()
        quit_event = threading.Event()
        quit_event.set()
        handler = Handler()
        LivePreview(capture, quit_event).serve(handler)
        self.assertEqual(handler.status, 503)
        self.assertTrue(handler.close_connection)
        capture.read.assert_not_called()

    def test_quit_during_read_does_not_write_frame_and_releases_slot(self):
        quit_event = threading.Event()
        capture = mock.Mock()
        def read(**kwargs):
            quit_event.set()
            return Frame(JPEG_A, 5, 0)
        capture.read.side_effect = read
        preview = LivePreview(capture, quit_event, max_clients=1)
        handler = Handler()
        preview.serve(handler)
        handler.connection.sendall.assert_not_called()
        quit_event.clear()
        capture.read.side_effect = CaptureError('ended')
        second = Handler()
        preview.serve(second)
        self.assertEqual(second.status, 200)

    def test_only_two_concurrent_clients_third_gets_429(self):
        entered = threading.Event()
        release = threading.Event()
        count = [0]
        lock = threading.Lock()
        capture = mock.Mock()
        def blocked_read(**kwargs):
            with lock:
                count[0] += 1
                if count[0] == 2:
                    entered.set()
            release.wait(1)
            raise CaptureError('ended')
        capture.read.side_effect = blocked_read
        preview = LivePreview(capture, threading.Event())
        handlers = [Handler(), Handler()]
        threads = [threading.Thread(target=preview.serve, args=(h,)) for h in handlers]
        for thread in threads:
            thread.start()
        try:
            self.assertTrue(entered.wait(1))
            third = Handler()
            preview.serve(third)
            self.assertEqual(third.status, 429)
            self.assertIn(('Retry-After', '1'), third.headers)
            self.assertEqual(capture.read.call_count, 2)
        finally:
            release.set()
            for thread in threads:
                thread.join(1)
        self.assertFalse(any(thread.is_alive() for thread in threads))

    def test_invalid_client_limits_and_missing_source_rejected(self):
        for limit in (0, 3, True, 1.5, '2'):
            with self.assertRaises(ValueError):
                LivePreview(mock.Mock(), threading.Event(), limit)
        with self.assertRaises(ValueError):
            LivePreview(None, threading.Event())


if __name__ == '__main__':
    unittest.main()

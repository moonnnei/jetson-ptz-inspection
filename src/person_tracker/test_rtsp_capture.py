"""RTSP pipe/concurrency fault injection; no camera or GStreamer required."""
import subprocess
import json
import io
import sys
import types
import threading
import time
import unittest
from unittest import mock
from rtsp_capture import (CaptureClosed, CaptureError, CaptureTimeout,
                          JPEGParser, RTSPCapture)
from rtsp_worker import main as worker_main


JPEG_A = b'\xff\xd8first\xff\xd9'
JPEG_B = b'\xff\xd8second\xff\xd9'


class ParserTests(unittest.TestCase):
    def test_prefix_markers_and_payload_split_across_chunks(self):
        parser = JPEGParser(64)
        data = b'launch-prefix\n' + JPEG_A + JPEG_B
        frames = []
        for value in data:
            frames.extend(parser.feed(bytes([value])))
        self.assertEqual(frames, [JPEG_A, JPEG_B])
        self.assertEqual(parser.buffer, bytearray())

    def test_one_chunk_can_contain_multiple_frames(self):
        self.assertEqual(JPEGParser().feed(JPEG_A + JPEG_B), [JPEG_A, JPEG_B])

    def test_oversized_unfinished_frame_is_bounded_then_recovers(self):
        parser = JPEGParser(16)
        self.assertEqual(parser.feed(b'\xff\xd8' + b'x' * 2000 + b'\xff'), [])
        self.assertLessEqual(len(parser.buffer), 16)
        self.assertEqual(parser.feed(b'\xd8ok\xff\xd9'), [b'\xff\xd8ok\xff\xd9'])

    def test_oversized_complete_frame_is_dropped(self):
        parser = JPEGParser(16)
        self.assertEqual(parser.feed(b'\xff\xd8' + b'x' * 20 + b'\xff\xd9' + JPEG_A), [JPEG_A])

    def test_nested_start_recovers_from_malformed_frame(self):
        self.assertEqual(JPEGParser().feed(b'\xff\xd8broken' + JPEG_A), [JPEG_A])

    def test_large_non_jpeg_prefix_does_not_accumulate(self):
        parser = JPEGParser(16)
        for unused in range(20):
            self.assertEqual(parser.feed(b'not-an-image' * 500), [])
            self.assertLessEqual(len(parser.buffer), 1)


class CaptureTests(unittest.TestCase):
    def capture(self, **settings):
        capture = RTSPCapture('rtsp://camera/Streaming/Channels/102',
                              'operator', 'private-test-value', **settings)
        self.addCleanup(capture.close)
        return capture

    def ready_capture(self, **settings):
        capture = self.capture(**settings)
        # Read-side tests isolate the condition/sequence logic from the worker.
        capture._thread = threading.Thread()
        capture._thread.start()
        capture._thread.join()
        return capture

    def test_credentials_are_rejected_in_url(self):
        with self.assertRaisesRegex(ValueError, 'must not contain credentials'):
            RTSPCapture('rtsp://operator:private-test-value@camera/live', 'a', 'b')

    def test_decoder_selection_and_secret_free_worker_command(self):
        software = self.capture()
        self.assertEqual(json.loads(software._configuration().decode())['decoder'], 'software')
        self.assertEqual(len(software._command()), 2)
        self.assertNotIn('private-test-value', ' '.join(software._command()))
        self.assertNotIn('rtsp://camera', ' '.join(software._command()))
        hardware = self.capture(decoder='hardware')
        self.assertEqual(json.loads(hardware._configuration().decode())['decoder'], 'hardware')

    def test_special_characters_roundtrip_in_private_configuration(self):
        capture = RTSPCapture('rtsp://camera/live', 'operator', 'p a"s\\s!')
        self.addCleanup(capture.close)
        self.assertEqual(json.loads(capture._configuration().decode())['password'], 'p a"s\\s!')

    def test_affinity_only_wraps_child_and_keeps_private_stdin(self):
        capture = self.capture(rtsp_cpus='00,3,4,5')
        self.assertEqual(capture._command()[:3], ['taskset', '-c', '0,3,4,5'])
        self.assertEqual(capture._command()[3:], [capture.python_executable, capture.worker_path])
        self.assertNotIn('private-test-value', ' '.join(capture._command()))
        self.assertNotIn('rtsp://camera', ' '.join(capture._command()))
        process = mock.Mock()
        process.stdin.write.side_effect = lambda data: len(data)
        capture._send_configuration(process)
        payload = json.loads(process.stdin.write.call_args[0][0].decode())
        self.assertEqual(payload['password'], 'private-test-value')
        self.assertNotIn('rtsp_cpus', payload)
        process.stdin.close.assert_called_once_with()

    def test_invalid_affinity_rejected_before_launch(self):
        for value in ('', '0,,3', '1,1', '0;touch-file', '--all-tasks', '0-3',
                      '0, 3', '-1', '128', '١', True, [0, 3]):
            with self.assertRaises(ValueError):
                self.capture(rtsp_cpus=value)

    def test_restart_preserves_child_affinity_and_no_shell(self):
        capture = self.capture(rtsp_cpus='0,3,4,5', max_restarts=1, retry_delay=0.01)
        with mock.patch('rtsp_capture.subprocess.Popen', side_effect=OSError('launch failed')) as launch:
            capture.start()
            capture._thread.join(timeout=1)
        self.assertEqual(launch.call_count, 2)
        for args, kwargs in launch.call_args_list:
            self.assertEqual(args[0][:3], ['taskset', '-c', '0,3,4,5'])
            self.assertFalse(kwargs.get('shell', False))
            self.assertEqual(kwargs['stdin'], subprocess.PIPE)
        self.assertEqual(capture.restarts, 1)

    def test_latest_frame_replaces_previous_frame(self):
        capture = self.ready_capture()
        capture._publish(JPEG_A, time.monotonic())
        capture._publish(JPEG_B, time.monotonic())
        frame = capture.read()
        self.assertEqual(frame.jpeg, JPEG_B)
        self.assertEqual(frame.sequence, 2)

    def test_status_exposes_counts_and_age_without_secrets(self):
        capture = self.ready_capture()
        capture._publish(JPEG_A, time.monotonic())
        status = capture.status()
        self.assertEqual(status['state'], 'streaming')
        self.assertEqual(status['sequence'], 1)
        self.assertGreaterEqual(status['last_frame_age'], 0)
        self.assertNotIn('private-test-value', json.dumps(status))
        self.assertNotIn('camera', json.dumps(status))
        capture.close()
        self.assertEqual(capture.status()['state'], 'closed')

    def test_read_does_not_repeat_sequence(self):
        capture = self.ready_capture()
        capture._publish(JPEG_A, time.monotonic())
        with self.assertRaises(CaptureTimeout):
            capture.read(after_sequence=1, timeout=0.02)

    def test_stale_frame_is_not_returned(self):
        capture = self.ready_capture(max_age=0.1)
        capture._publish(JPEG_A, time.monotonic() - 1)
        with self.assertRaises(CaptureTimeout):
            capture.read(timeout=0.02)

    def test_waiting_reader_wakes_on_new_frame(self):
        capture = self.ready_capture()
        capture._publish(JPEG_A, time.monotonic())
        def publish():
            time.sleep(0.02)
            capture._publish(JPEG_B, time.monotonic())
        thread = threading.Thread(target=publish)
        thread.start()
        try:
            frame = capture.read(after_sequence=1, timeout=1)
        finally:
            thread.join()
        self.assertEqual(frame.jpeg, JPEG_B)
        self.assertEqual(frame.sequence, 2)

    def test_close_wakes_reader_and_is_idempotent(self):
        capture = self.ready_capture()
        results = []
        def reader():
            try:
                capture.read(timeout=5)
            except CaptureClosed:
                results.append('closed')
        thread = threading.Thread(target=reader)
        thread.start()
        time.sleep(0.02)
        capture.close()
        capture.close()
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, ['closed'])
        with self.assertRaises(CaptureClosed):
            capture.start()

    def test_retry_limit_and_exception_text_does_not_leak_credentials(self):
        capture = self.capture(max_restarts=2, retry_delay=0.01)
        with mock.patch('rtsp_capture.subprocess.Popen',
                        side_effect=OSError('private-test-value secret argv')) as launch:
            capture.start()
            with self.assertRaises(CaptureError) as error:
                capture.read(timeout=1)
            capture._thread.join(timeout=1)
        self.assertEqual(launch.call_count, 3)
        self.assertEqual(capture.restarts, 2)
        self.assertIn('retry limit reached', str(error.exception))
        self.assertNotIn('private-test-value', str(error.exception))

    def test_complete_frame_deadline_ignores_non_jpeg_output(self):
        capture = self.capture(stream_timeout=0.1)
        process = mock.Mock()
        process.poll.return_value = None
        with mock.patch('rtsp_capture.time.monotonic', side_effect=[0, 0, 0.05, 0.11]), \
                mock.patch('rtsp_capture.select.select', return_value=([process.stdout], [], [])), \
                mock.patch('rtsp_capture.os.read', return_value=b'noise'):
            reason = capture._consume(process)
        self.assertIn('complete frame in time', reason)
        self.assertIsNone(capture._frame)

    def test_consumer_publishes_only_last_frame_from_pipe_chunk(self):
        capture = self.capture()
        process = mock.Mock()
        process.poll.side_effect = [None, 1]
        with mock.patch('rtsp_capture.select.select', return_value=([process.stdout], [], [])), \
                mock.patch('rtsp_capture.os.read', return_value=JPEG_A + JPEG_B):
            reason = capture._consume(process)
        self.assertIn('process exited', reason)
        self.assertEqual(capture._frame.jpeg, JPEG_B)
        self.assertEqual(capture._frame.sequence, 1)

    def test_failed_stream_clears_previous_frame_and_reaps_process(self):
        capture = self.capture(max_restarts=0)
        process = mock.Mock()
        process.poll.return_value = 1
        def consume(unused):
            capture._publish(JPEG_A, time.monotonic())
            return 'RTSP decoder process exited'
        with mock.patch('rtsp_capture.subprocess.Popen', return_value=process), \
                mock.patch.object(capture, '_send_configuration'), \
                mock.patch.object(capture, '_consume', side_effect=consume):
            capture.start()
            capture._thread.join(timeout=1)
        self.assertIsNone(capture._frame)
        process.stdout.close.assert_called_once_with()
        with self.assertRaisesRegex(CaptureError, 'process exited'):
            capture.read(timeout=1)

    def test_unresponsive_process_is_killed_and_waited(self):
        capture = self.capture()
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired('redacted', 1), 0]
        capture._stop_process(process)
        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual(process.wait.call_count, 2)
        process.stdout.close.assert_called_once_with()

    def test_launch_has_no_shell_and_discards_raw_stderr(self):
        capture = self.capture(max_restarts=0)
        process = mock.Mock()
        process.poll.return_value = 1
        with mock.patch('rtsp_capture.subprocess.Popen', return_value=process) as launch, \
                mock.patch.object(capture, '_send_configuration'):
            capture.start()
            capture._thread.join(timeout=1)
        kwargs = launch.call_args[1]
        self.assertFalse(kwargs.get('shell', False))
        self.assertEqual(kwargs['stderr'], subprocess.DEVNULL)
        self.assertEqual(kwargs['stdin'], subprocess.PIPE)

    def test_configuration_pipe_is_closed_after_bounded_write(self):
        capture = self.capture()
        process = mock.Mock()
        process.stdin.write.side_effect = lambda data: len(data)
        capture._send_configuration(process)
        payload = process.stdin.write.call_args[0][0]
        self.assertLessEqual(len(payload), 4096)
        self.assertEqual(json.loads(payload.decode())['password'], 'private-test-value')
        process.stdin.close.assert_called_once_with()

    def test_configuration_pipe_is_closed_on_write_failure(self):
        capture = self.capture()
        process = mock.Mock()
        process.stdin.write.side_effect = OSError('private-test-value')
        with self.assertRaises(OSError):
            capture._send_configuration(process)
        process.stdin.close.assert_called_once_with()

    def test_invalid_limits_and_partial_resize_are_rejected(self):
        for settings in ({'max_age': float('nan')}, {'fps': 0}, {'width': 640},
                         {'width': 641, 'height': 360}, {'max_restarts': -1}):
            with self.assertRaises(ValueError):
                self.capture(**settings)


class WorkerTests(unittest.TestCase):
    def worker(self, decoder, width=None, height=None):
        config = dict(url='rtsp://camera:1554/Streaming/Channels/102', user='operator',
                      password='private-test-value', decoder=decoder, width=width,
                      height=height, fps=10, latency=100, tcp_timeout=5000000)
        descriptions = []
        source = mock.Mock()
        pipeline = mock.Mock()
        pipeline.get_bus.return_value.timed_pop_filtered.return_value = object()
        pipeline.get_by_name.return_value = source
        def parse_launch(description):
            descriptions.append(description)
            return pipeline
        gst = types.SimpleNamespace(
            parse_launch=parse_launch,
            State=types.SimpleNamespace(PLAYING=1, NULL=0),
            StateChangeReturn=types.SimpleNamespace(FAILURE=-1),
            MessageType=types.SimpleNamespace(ERROR=1, EOS=2), MSECOND=1000000,
            init=lambda unused: None)
        gi = types.ModuleType('gi')
        gi.require_version = mock.Mock()
        repository = types.ModuleType('gi.repository')
        repository.Gst = gst
        fake_stdin = types.SimpleNamespace(buffer=io.BytesIO(json.dumps(config).encode()))
        with mock.patch.dict(sys.modules, {'gi': gi, 'gi.repository': repository}), \
                mock.patch('rtsp_worker.sys.stdin', fake_stdin), \
                mock.patch('rtsp_worker.signal.signal'), \
                mock.patch('rtsp_worker.sys.stdout', new_callable=io.StringIO) as output:
            code = worker_main()
        return code, descriptions[0], source, pipeline, output.getvalue()

    def test_software_pipeline_sets_private_properties_and_stdout_is_binary_only(self):
        code, description, source, pipeline, text = self.worker('software')
        self.assertEqual(code, 2)  # injected bus failure ends the worker
        self.assertIn('avdec_h264 ! videoconvert ! videoscale', description)
        self.assertNotIn('omxh264dec', description)
        self.assertNotIn('private-test-value', description)
        self.assertNotIn('rtsp://camera', description)
        self.assertNotIn('user-pw', description)
        source.set_property.assert_any_call('user-pw', 'private-test-value')
        source.set_property.assert_any_call('user-id', 'operator')
        self.assertIn('fdsink name=output fd=1 sync=false', description)
        self.assertEqual(text, '')
        pipeline.set_state.assert_any_call(0)

    def test_hardware_pipeline_leaves_nvmm_before_jpeg_encoder(self):
        code, description, source, pipeline, text = self.worker('hardware', 1280, 720)
        self.assertIn('omxh264dec ! nvvidconv ! video/x-raw,format=I420,width=1280,height=720', description)
        self.assertNotIn('videoscale', description)
        self.assertIn('application/x-rtp,media=video,encoding-name=H264 ! rtph264depay', description)
        self.assertIn('queue leaky=downstream max-size-buffers=1 max-size-bytes=0 max-size-time=0 ! videorate', description)
        self.assertIn('videorate ! video/x-raw,framerate=10/1 ! jpegenc quality=85', description)
        self.assertNotIn('drop-only', description)
        self.assertEqual(text, '')

    def test_both_decoders_keep_verified_rate_and_old_gstreamer_parameters(self):
        for decoder in ('hardware', 'software'):
            unused, description, source, pipeline, text = self.worker(decoder, 1280, 720)
            self.assertIn('videorate ! video/x-raw,framerate=10/1', description)
            self.assertNotIn('drop-only', description)
            self.assertIn('max-size-buffers=1', description)
            self.assertIn('protocols=tcp', description)
            self.assertIn('latency=100', description)
            self.assertNotIn('max-duplication-time', description)
            self.assertNotIn('drop-out-of-segment', description)
            self.assertNotIn('private-test-value', description)
            self.assertEqual(text, '')


if __name__ == '__main__':
    unittest.main()

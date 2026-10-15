"""Pipe fault injection; no model process or hardware is started."""
from pathlib import Path
import unittest
from unittest import mock
from server import Engine


class EngineTests(unittest.TestCase):
    def engine(self):
        engine=Engine.__new__(Engine)
        engine.p=mock.Mock()
        engine.p.stdin.fileno.return_value=123
        return engine

    def test_constructor_uses_nonblocking_input(self):
        # Windows Python3.11 lacks this Linux API; the test supplies its fake.
        process=mock.Mock()
        process.stdin.fileno.return_value=123
        with mock.patch('server.subprocess.Popen',return_value=process), \
                mock.patch('server.os.set_blocking',create=True) as nonblocking:
            Engine(Path('models'),detector=Path('detector'))
        nonblocking.assert_called_once_with(123,False)

    def test_nonblocking_setup_failure_closes_child(self):
        process=mock.Mock();process.poll.return_value=None
        with mock.patch('server.subprocess.Popen',return_value=process), \
                mock.patch('server.os.set_blocking',side_effect=OSError('unsupported'),create=True):
            with self.assertRaises(OSError):Engine(Path('models'),detector=Path('detector'))
        process.terminate.assert_called_once_with()

    def test_partial_writes_preserve_all_bytes(self):
        engine=self.engine();sent=[]
        def partial(fd,data):
            part=bytes(data[:2]);sent.append(part);return len(part)
        with mock.patch('server.select.select',return_value=([],[engine.p.stdin],[])), \
                mock.patch('server.time.monotonic',return_value=0), \
                mock.patch('server.os.write',side_effect=partial):
            engine.write(b'abcdefg')
        self.assertEqual(b''.join(sent),b'abcdefg')

    def test_stalled_input_times_out_without_blocking_write(self):
        engine=self.engine()
        with mock.patch('server.select.select',return_value=([],[],[])), \
                mock.patch('server.time.monotonic',return_value=0), \
                mock.patch('server.os.write') as write:
            with self.assertRaisesRegex(RuntimeError,'input timeout'):engine.write(b'image')
        self.assertFalse(write.called)

    def test_would_block_retries_share_deadline(self):
        engine=self.engine()
        with mock.patch('server.select.select',return_value=([],[engine.p.stdin],[])), \
                mock.patch('server.time.monotonic',side_effect=[0,0,1,3.1]), \
                mock.patch('server.os.write',side_effect=BlockingIOError()) as write:
            with self.assertRaisesRegex(RuntimeError,'input timeout'):engine.write(b'image')
        self.assertEqual(write.call_count,2)

    def test_closed_input_fails(self):
        engine=self.engine()
        with mock.patch('server.select.select',return_value=([],[engine.p.stdin],[])), \
                mock.patch('server.time.monotonic',return_value=0), \
                mock.patch('server.os.write',return_value=0):
            with self.assertRaisesRegex(RuntimeError,'pipe closed'):engine.write(b'image')


if __name__=='__main__':unittest.main()

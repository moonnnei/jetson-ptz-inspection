"""Bounded scanning checks with injected IO; zero physical motion."""
import copy
import json
from pathlib import Path
import struct
import tempfile
import threading
import unittest

from scan_capture import BoundedCapture, ISAPIScanIO, commissioning_points


class FakeIO:
    def __init__(self):
        self.current=dict(pan=202.9,tilt=5.5,zoom=1.4)
        self.moves=[]
        self.stops=0
        self.fail_snapshot=False
        self.fail_stop=False
        self.cancel_on_first=None
        self.block=False
    def position(self):
        return copy.deepcopy(self.current)
    def move(self,target,cancel):
        if cancel.is_set():raise RuntimeError('cancelled')
        self.moves.append(dict(target));self.current=dict(target)
        if self.block:cancel.wait(3)
        if self.cancel_on_first:self.cancel_on_first.set()
    def snapshot(self):
        if self.fail_snapshot:raise RuntimeError('snapshot_failure')
        # Synthetic JPEG header fixture, never used as a real capture.
        return b'\xff\xd8\xff\xc0'+struct.pack('>H',11)+b'\x08'+struct.pack('>HH',80,120)+b'\x01\x01\x11\x00'+b'\xff\xd9'
    def stop(self):
        self.stops+=1
        if self.fail_stop:raise RuntimeError('stop_failure')


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)/'new-scan'
        self.io=FakeIO()
    def tearDown(self):
        self.tmp.cleanup()
    def test_three_views_keep_tilt_zoom_and_restore(self):
        result=BoundedCapture(self.io,self.root).run()
        self.assertEqual(result['status'],'completed')
        self.assertEqual(len(result['frames']),3)
        self.assertEqual([p['pan'] for p in self.io.moves],[197.9,202.9,207.9,202.9])
        self.assertTrue(all(p['zoom']==1.4 and p['tilt']==5.5 for p in self.io.moves))
        self.assertEqual(json.loads((self.root/'manifest.json').read_text(encoding='utf-8'))['provenance'],'bounded_scan_capture')
        self.assertFalse(result['full_panorama'])
    def test_snapshot_failure_stops_without_more_moves_or_complete_manifest(self):
        self.io.fail_snapshot=True
        result=BoundedCapture(self.io,self.root).run()
        self.assertEqual(result['status'],'failed')
        self.assertEqual(len(self.io.moves),1)
        self.assertGreater(self.io.stops,0)
        self.assertFalse((self.root/'manifest.json').exists())
    def test_cancellation_does_not_send_return_or_next_move(self):
        cancel=threading.Event();self.io.cancel_on_first=cancel
        result=BoundedCapture(self.io,self.root,cancel).run()
        self.assertEqual(result['status'],'stopped')
        self.assertEqual(len(self.io.moves),1)
        self.assertEqual(result['frames'],[])
    def test_final_stop_failure_prevents_success_manifest(self):
        self.io.fail_stop=True
        result=BoundedCapture(self.io,self.root).run()
        self.assertEqual(result['status'],'failed')
        self.assertEqual(len(result['frames']),3)
        self.assertFalse((self.root/'manifest.json').exists())
    def test_watchdog_cancels_a_stalled_visit(self):
        self.io.block=True
        result=BoundedCapture(self.io,self.root,budget=1).run()
        self.assertEqual(result['status'],'stopped')
        self.assertEqual(len(self.io.moves),1)
        self.assertGreaterEqual(self.io.stops,1)
    def test_scan_does_not_wrap_near_mechanical_end(self):
        points=commissioning_points(dict(pan=1,tilt=0,zoom=1))
        self.assertEqual([p['pan'] for p in points],[0,1,6])
        self.assertTrue(all(0<=p['pan']<=350 for p in points))
    def test_adapter_rejects_wide_motion_before_sending_a_command(self):
        io=ISAPIScanIO(None);io.anchor=dict(pan=202.9,tilt=5.5,zoom=1.4)
        for target in (dict(pan=210,tilt=5.5,zoom=1.4),dict(pan=202.9,tilt=7,zoom=1.4),dict(pan=202.9,tilt=5.5,zoom=2)):
            with self.assertRaises(ValueError):
                io.move(target,threading.Event())


if __name__=='__main__':
    unittest.main()

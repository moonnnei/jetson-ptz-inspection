"""Image work starts during finite acquisition; failures cancel and join it."""
from contextlib import contextmanager
from pathlib import Path
import tempfile,threading,types,unittest
from unittest.mock import MagicMock,patch
from scene_initialization_io import SceneInitializationIO


class ScanPipelineTests(unittest.TestCase):
    def exercise(self,fail=False):
        started=threading.Event();cancel=threading.Event();closed=[]
        @contextmanager
        def lease():yield
        class Detector:
            @contextmanager
            def session(self):
                try:yield
                finally:closed.append(True)
        class Capture:
            def __init__(self,*args):self.state=dict(frames=[],status='running',started_at='fixture')
            def persist(self):pass
            def run(self):
                self.state['frames'].append(dict(path='first.jpg'));self.persist()
                if not started.wait(2):raise RuntimeError('processing did not overlap capture')
                if not cancel.is_set():self.state['frames'].append(dict(path='second.jpg'));self.persist()
                self.state['status']='stopped' if cancel.is_set() else 'completed'
                return self.state
            def stop(self):cancel.set()
        with tempfile.TemporaryDirectory() as tmp:
            backend=SceneInitializationIO(object(),tmp,Detector())
            def process(record,directory,event,progress,stream=None,total=None):
                frames=[]
                for item in stream:
                    frames.append(item);started.set()
                    if fail:raise ValueError('processing fixture failure')
                self.assertEqual(len(frames),2);return dict(frames=frames)
            backend.process_scan=process
            module=types.SimpleNamespace(motion_lease=lease,check_other_services=lambda:None)
            with patch.dict('sys.modules',{'capture_review_views':module}), \
                 patch('scene_initialization_io.ISAPIRingIO'),patch('scene_initialization_io.RingCapture',Capture):
                if fail:
                    with self.assertRaises(ValueError):backend.run(dict(scan=dict(kind='bounded_single_row_ring_scan',expected_frames=2)),Path(tmp)/'run',cancel,lambda *a:None)
                    self.assertTrue(cancel.is_set())
                else:
                    result=backend.run(dict(scan=dict(kind='bounded_single_row_ring_scan',expected_frames=2)),Path(tmp)/'run',cancel,lambda *a:None)
                    self.assertEqual(len(result['frames']),2)
            self.assertEqual(closed,[True]);self.assertIsNone(backend.active_scan)

    def test_image_processing_overlaps_acquisition_and_finishes_before_return(self):self.exercise()
    def test_processing_error_cancels_capture_without_orphan_worker(self):self.exercise(True)

if __name__=='__main__':unittest.main()

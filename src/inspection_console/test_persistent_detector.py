"""Process reuse and bounded partial-pipe framing, independent of hardware."""
import json,struct,unittest
from unittest.mock import MagicMock,patch
import test_region_detector as fixtures


class PersistentDetectorTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.RegionProtocolTests();self.fixture.setUp()
        self.detector=self.fixture.detector
        row=dict(model='YOLO11n-meter-Pointer10K',input_size=640,width=100,height=100,rotation=0,
                 confidence_threshold=.25,nms_iou_threshold=.45,detections=[dict(bbox=[.1,.1,.9,.9],confidence=.8)])
        body=json.dumps(row).encode();self.frame=struct.pack('<I',len(body))+body+struct.pack('<I',4)+b'jpeg'
        self.process=MagicMock();self.process.poll.return_value=0
    def tearDown(self):self.fixture.tearDown()

    def test_two_predictions_share_one_process_and_handle_partial_reads(self):
        buffer=bytearray(self.frame*2)
        def read(fd,size):
            part=bytes(buffer[:min(size,3)]);del buffer[:len(part)];return part
        def ready(readers,writers,errors,timeout):return (readers,writers,[])
        with patch('region_detector.subprocess.Popen',return_value=self.process) as start, \
             patch('region_detector.os.set_blocking'),patch('region_detector.select.select',side_effect=ready), \
             patch('region_detector.os.write',side_effect=lambda fd,data:len(data)),patch('region_detector.os.read',side_effect=read):
            with self.detector.session():
                a=self.detector.predict(self.fixture.roi);b=self.detector.predict(self.fixture.roi)
            self.assertEqual(start.call_count,1);self.assertEqual(a['detections'],b['detections'])
        self.assertIsNone(self.detector.worker);self.assertEqual(self.process.stdin.close.call_count,1)

    def test_backpressure_failure_kills_worker_and_is_not_retried(self):
        with patch('region_detector.subprocess.Popen',return_value=self.process) as start, \
             patch('region_detector.os.set_blocking'),patch('region_detector.select.select',return_value=([],[],[])):
            with self.detector.session():
                with self.assertRaises(RuntimeError):self.detector.predict(self.fixture.roi)
            self.assertEqual(start.call_count,1)
        self.assertEqual(self.process.kill.call_count,1);self.assertIsNone(self.detector.worker)

if __name__=='__main__':unittest.main()

"""Native pipe framing and ambiguity rejection. A mock is not model accuracy."""
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from region_detector import RegionDetector


class RegionProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        (self.root/'models/meter640').mkdir(parents=True);(self.root/'build').mkdir()
        (self.root/'build/meter_detector').write_bytes(b'not executed')
        (self.root/'models/meter640/model.ncnn.param').write_bytes(b'parameter fixture')
        (self.root/'models/meter640/model.ncnn.bin').write_bytes(b'weight fixture')
        self.roi=self.root/'roi.jpg';self.roi.write_bytes(b'bounded test image')
        self.detector=RegionDetector(self.root)

    def tearDown(self):self.tmp.cleanup()

    def process(self,detections):
        result=dict(model='YOLO11n-meter-Pointer10K',input_size=640,width=100,height=100,
            rotation=0,confidence_threshold=.25,nms_iou_threshold=.45,detections=detections)
        raw=json.dumps(result).encode();payload=struct.pack('<I',len(raw))+raw+struct.pack('<I',4)+b'jpeg'
        class Process:
            returncode=0
            def communicate(inner,input,timeout):
                self.assertEqual(struct.unpack('<I',input[:4])[0],len(self.roi.read_bytes()))
                self.assertLessEqual(timeout,10)
                return payload,b''
            def poll(inner):return 0
        return Process()

    def test_unique_overlapping_detection_is_accepted_without_rotation_or_threshold_changes(self):
        with patch('region_detector.subprocess.Popen',return_value=self.process([
                dict(bbox=[.1,.1,.9,.9],confidence=.8)])) as launch:
            result=self.detector.infer(self.roi,[0,0,100,100],[10,10,90,90])
        self.assertTrue(result['accepted'])
        self.assertEqual(launch.call_args[0][0][-1],'0')
        self.assertEqual(result['confidence_threshold'],.25)

    def test_empty_or_ambiguous_output_is_not_a_positive_identity(self):
        for rows in ([],[dict(bbox=[.1,.1,.8,.8],confidence=.9),dict(bbox=[.2,.2,.9,.9],confidence=.8)]):
            with patch('region_detector.subprocess.Popen',return_value=self.process(rows)):
                self.assertFalse(self.detector.infer(self.roi,[0,0,100,100],[10,10,90,90])['accepted'])

    def test_model_changed_after_creation_is_refused_before_native_execution(self):
        self.detector.weights.write_bytes(b'changed weights')
        with patch('region_detector.subprocess.Popen') as launch:
            with self.assertRaises(ValueError):self.detector.infer(self.roi,[0,0,100,100],[10,10,90,90])
        launch.assert_not_called()


if __name__=='__main__':unittest.main()

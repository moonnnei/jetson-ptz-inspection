"""Ring-map source preservation and fallback checks; no physical coverage claim."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import cv2
import numpy as np

from build_map import write_image
from build_ring_map import process,stitch_preview


class RingMapTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.image=np.random.RandomState(7).randint(0,256,(160,240,3),np.uint8)
        write_image(self.root/'source.png',self.image)
        self.sha=hashlib.sha256((self.root/'source.png').read_bytes()).hexdigest()
        self.manifest=dict(title='synthetic source index',provenance='bounded_scan_capture',
            frames=[dict(path='source.png',sha256=self.sha,orientation='raw_camera',
                pose=dict(pan=p,tilt=5.5,zoom=1),detections=[]) for p in (350,175,0)])
        self.path=self.root/'input.json';self.save()
    def save(self):self.path.write_text(json.dumps(self.manifest),encoding='utf-8')
    def tearDown(self):self.temp.cleanup()

    def test_index_fallback_preserves_raw_sources_without_inventing_transforms(self):
        result,report=process(self.path,self.root/'out',rotate180=True,stitch=False)
        self.assertEqual(report['kind'],'source_contact_sheet')
        self.assertFalse(report['full_panorama'])
        self.assertEqual(result['overview']['registered_views'],0)
        self.assertTrue(all(f['to_overview'] is None for f in result['frames']))
        for f in result['frames']:
            raw=(self.root/'out'/f['raw_source_path']).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(),self.sha)
            upright=cv2.imdecode(np.frombuffer((self.root/'out'/f['path']).read_bytes(),np.uint8),cv2.IMREAD_COLOR)
            np.testing.assert_array_equal(upright,cv2.flip(self.image,-1))

    def test_source_hash_corruption_is_rejected(self):
        self.manifest['frames'][0]['sha256']='0'*64;self.save()
        with self.assertRaises(ValueError):process(self.path,self.root/'out',stitch=False)

    def test_non_ring_pose_order_or_zoom_changes_are_rejected(self):
        for poses in ((0,100,30),(350,0,175),(0,20,40)):
            for f,p in zip(self.manifest['frames'],poses):f['pose']['pan']=p
            self.save()
            with self.assertRaises(ValueError):process(self.path,self.root/'out',stitch=False)
        for f,p in zip(self.manifest['frames'],(350,175,0)):f['pose']['pan']=p
        self.manifest['frames'][1]['pose']['zoom']=1.4;self.save()
        with self.assertRaises(ValueError):process(self.path,self.root/'out',stitch=False)

    def test_blank_images_fail_stitching_without_false_success(self):
        panorama,report=stitch_preview([np.zeros((200,300,3),np.uint8) for unused in range(3)])
        self.assertIsNone(panorama)
        self.assertFalse(report['accepted'])


if __name__=='__main__':unittest.main()

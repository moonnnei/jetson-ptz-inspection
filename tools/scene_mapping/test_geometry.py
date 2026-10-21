"""Optional geometry tests use synthetic images; no field precision claim."""
import json
from pathlib import Path
import tempfile
import unittest

import cv2
import numpy as np
from geometry import align, build_overview, project
from build_map import process, write_image


def textured():
    rng=np.random.RandomState(23)
    image=np.full((480,1000,3),225,np.uint8)
    for unused in range(400):
        x,y=rng.randint(10,990),rng.randint(10,470)
        color=tuple(int(v) for v in rng.randint(20,230,3))
        cv2.circle(image,(x,y),int(rng.randint(2,12)),color,-1)
    return image


class GeometryTests(unittest.TestCase):
    def test_translation_recovers_original_coordinate_mapping(self):
        scene=textured()
        first,second=scene[:,:700].copy(),scene[:,250:950].copy()
        out=align(second,first)
        self.assertTrue(out['accepted'],out)
        measured=project([[100,100],[300,250]],out['matrix'])
        np.testing.assert_allclose(measured,[[350,100],[550,250]],atol=1.2)

    def test_uniform_scene_is_not_false_panorama(self):
        blank=np.full((300,400,3),100,np.uint8)
        canvas,report=build_overview([blank,blank.copy()])
        self.assertEqual(report['registered_views'],1)
        self.assertEqual(report['disconnected'],[1])
        self.assertFalse(report['full_panorama'])
        self.assertIsNone(report['frames'][1]['to_overview'])

    def test_overview_roundtrip_and_budget(self):
        scene=textured()
        canvas,r=build_overview([scene[:,:700].copy(),scene[:,200:900].copy(),scene[:,300:].copy()],max_side=512)
        self.assertEqual(r['registered_views'],3)
        self.assertLessEqual(max(canvas.shape[:2]),512)
        point=np.array([[200.,200.]])
        h=np.array(r['frames'][2]['to_overview'])
        np.testing.assert_allclose(project(project(point,h),np.linalg.inv(h)),point,atol=1e-7)

    def test_projection_through_infinity_rejected(self):
        with self.assertRaises(ValueError):
            project([[0,0],[10,0]],[[1,0,0],[0,1,0],[1,0,-5]])

    def test_rotated_detection_and_raw_hash_retained(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);write_image(root/'scene.png',textured()[:,:700])
            source=dict(title='synthetic test',frames=[dict(path='scene.png',detections=[dict(bbox=[10,20,100,120],confidence=.5)])])
            (root/'input.json').write_text(json.dumps(source),encoding='utf-8')
            result,report=process(root/'input.json',root/'output',True)
            f=result['frames'][0]
            self.assertEqual(f['detections'][0]['bbox'],[600,360,690,460])
            self.assertTrue((root/'output'/f['raw_source_path']).is_file())
            self.assertNotEqual(f['sha256'],f['raw_source_sha256'])
            self.assertFalse(result['overview']['ptz_calibration'])


if __name__=='__main__':unittest.main()

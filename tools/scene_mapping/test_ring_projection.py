"""Cylindrical preview geometry checks; no intrinsics-calibration claim."""
import math
import unittest
import numpy as np

from ring_projection import estimate_focal,projected_preview,render


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.images=[np.full((180,320,3),30+i*20,np.uint8) for i in range(6)]
        self.poses=[dict(pan=i*20,tilt=5.5,zoom=1) for i in range(6)]
        self.edges=[dict(accepted=True,source=i,target=i+1,
            matrix=[[1,0,200*math.tan(math.radians(20))],[0,1,0],[0,0,1]]) for i in range(5)]

    def test_focal_estimate_and_direction_use_only_consistent_pairs(self):
        self.edges[0]['matrix'][0][2]=700*math.tan(math.radians(20))
        f,sign,report=estimate_focal(self.images,self.poses,self.edges)
        self.assertAlmostEqual(f,200)
        self.assertEqual(sign,1)
        self.assertEqual(report['usable_pairs'],4)
        self.assertFalse(report['calibration_validated'])

    def test_unreliable_intrinsics_fail_without_false_panorama(self):
        image,report=projected_preview(self.images,self.poses,self.edges[:2])
        self.assertIsNone(image)
        self.assertFalse(report['accepted'])

    def test_circular_image_coordinates_cover_endpoints_without_motor_commands(self):
        poses=[dict(pan=i*30,tilt=5.5,zoom=1) for i in range(12)]
        images=[np.full((180,320,3),120,np.uint8) for p in poses]
        image,report=render(images,poses,200,width=512)
        self.assertEqual(image.shape[1],512)
        self.assertEqual(report['centerline_pixel_coverage'],1)
        self.assertFalse(report['full_panorama'])
        self.assertFalse(report['source_pixel_mapping'])

    def test_missing_sector_remains_a_gap(self):
        image,report=render(self.images,self.poses,200,width=512)
        self.assertLess(report['centerline_pixel_coverage'],.75)
        self.assertTrue(np.any(np.all(image[image.shape[0]//2]==0,axis=1)))

    def test_invalid_render_parameters_rejected(self):
        for focal in (0,float('nan')):
            with self.assertRaises(ValueError):render(self.images,self.poses,focal,width=512)


if __name__=='__main__':unittest.main()

"""Panorama cyclic tiling and source lookup checks without a model download."""
import sys
from pathlib import Path
import unittest
from unittest.mock import patch
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'src/inspection_console'))
from detect_panorama import cyclic_iou,panorama_candidates,source_observations,tiles,patch_correlation
from test_ring_coordinates import geometry


class PanoramaDiscoveryTests(unittest.TestCase):
    def test_uniform_or_unrelated_patch_is_not_an_identity_match(self):
        pattern=np.random.RandomState(4).randint(0,256,(96,96,3)).astype(np.uint8)
        self.assertAlmostEqual(patch_correlation(pattern,pattern),1)
        self.assertIsNone(patch_correlation(pattern,np.zeros_like(pattern)))
        self.assertLess(patch_correlation(pattern,255-pattern),0)

    def test_high_detector_score_cannot_choose_different_source_appearance(self):
        frames=[dict(id=str(i),sha256=str(i)*64,width=2560,height=1440,
            orientation='rotate180_from_raw',pose=dict(pan=180+i,tilt=5.5,zoom=1)) for i in (1,2)]
        with patch('detect_panorama.source_appearance',side_effect=[.95,.1]):
            result,unused=source_observations([dict(bbox=[2020,270,2070,330],confidence=.7)],
                frames,geometry(),lambda f:np.zeros((1440,2560,3),np.uint8),
                lambda im:[dict(bbox_pixels=[0,0,im.shape[1],im.shape[0]],confidence=.99)],
                'b'*64,'c'*64,[],panorama=np.zeros((600,4096,3),np.uint8))
        self.assertEqual(result[0]['frame_sha256'],frames[0]['sha256'])
        self.assertEqual(result[0]['source_association'],'appearance_consistent')
        self.assertEqual(result[0]['source_similarity'],.95)

    def test_cyclic_iou_merges_one_object_across_the_zero_seam(self):
        self.assertGreater(cyclic_iou([4000,100,4150,200],[0,100,50,200],4096),.3)
        self.assertAlmostEqual(cyclic_iou([4000,100,4150,200],[-96,100,54,200],4096),1)

    def test_tiles_include_wrap_and_have_a_finite_inference_budget(self):
        regions=tiles(4096,600)
        self.assertLessEqual(len(regions),40)
        self.assertTrue(any(r[2]>4096 for r in regions))
        self.assertTrue(all(r[3]<=600 for r in regions))

    def test_global_coordinates_are_restored_from_a_wrapping_tile(self):
        image=np.zeros((600,4096,3),np.uint8)
        image[200:260,4050:4096]=255;image[200:260,0:34]=255
        def infer(patch):
            yy,xx=np.where(patch[:,:,0]>0)
            if len(xx)==0:return []
            return [dict(bbox_pixels=[float(xx.min()),float(yy.min()),float(xx.max()+1),float(yy.max()+1)],confidence=.9)]
        result,calls=panorama_candidates(image,infer)
        self.assertTrue(any(b['bbox'][2]>4096 and b['bbox'][2]-b['bbox'][0]>=79 for b in result))
        self.assertLessEqual(len(calls),41)

    def test_unconfirmed_source_is_retained_for_human_review(self):
        p=geometry()
        frames=[dict(id='frame',sha256='a'*64,width=2560,height=1440,orientation='rotate180_from_raw',pose=dict(pan=180,tilt=5.5,zoom=1))]
        candidate=dict(bbox=[2020,270,2070,330],confidence=.7)
        calls=[]
        observations,unmapped=source_observations([candidate],frames,p,
            lambda f:np.zeros((1440,2560,3),np.uint8),lambda i:[], 'b'*64,'c'*64,calls)
        self.assertEqual(len(observations),1)
        self.assertEqual(observations[0]['source_recheck'],'unverified')
        self.assertIsNone(observations[0]['source_confidence'])
        self.assertEqual(unmapped,[])
        self.assertNotIn('visit_pose',observations[0])


if __name__=='__main__':unittest.main()

import unittest
from unittest.mock import patch
import numpy as np
from detect_views import discover


class DiscoveryTests(unittest.TestCase):
    def test_crop_is_mapped_back_and_duplicate_removed(self):
        image=np.zeros((300,400,3),np.uint8)
        proposals=dict(proposals=[dict(id='1',crop=[100,50,220,180])])
        def infer(crop):
            box=[110,60,180,140] if crop.shape[1]==400 else [10,10,80,90]
            return [dict(bbox_pixels=box,confidence=.8)]
        with patch('detect_views.propose',return_value=proposals):
            detections,report=discover(image,infer)
        self.assertEqual(len(detections),1)
        self.assertEqual(detections[0]['bbox'],[110.,60.,180.,140.])
        self.assertEqual(report['model_calls'],2)

    def test_seam_fragment_without_recheck_not_accepted(self):
        image=np.zeros((300,400,3),np.uint8)
        def infer(crop):
            return [] if crop.shape[1]==400 else [dict(bbox_pixels=[0,5,100,90],confidence=.9)]
        with patch('detect_views.propose',return_value=dict(proposals=[dict(id='1',crop=[100,50,220,180])])):
            found,report=discover(image,infer,retry_limit=0)
        self.assertEqual(found,[])
        self.assertEqual(report['edge_rechecks'],0)

    def test_bad_budget_rejected_before_inference(self):
        called=[]
        with self.assertRaises(ValueError):
            discover(np.zeros((30,30,3),np.uint8),lambda x:called.append(x),retry_limit=5)
        self.assertEqual(called,[])


if __name__=='__main__':unittest.main()

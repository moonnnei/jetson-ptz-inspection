"""Offline coordinate, crop-boundary, budget and synthetic-discovery checks."""
import unittest
import cv2
import numpy as np
from shape_candidates import propose,padded_box,remap_detection,merge,iou,area,intersection


class CandidateTests(unittest.TestCase):
    def test_synthetic_unknown_location_and_translation(self):
        for x,y in ((60,50),(350,220)):
            image=np.full((480,640,3),180,np.uint8)
            cv2.rectangle(image,(x,y),(x+100,y+90),(15,15,15),-1)
            cv2.rectangle(image,(x+9,y+9),(x+91,y+81),(240,240,240),-1)
            result=propose(image)
            # The detected contour may be the readable inner face rather than
            # the outer frame. Its padded crop must still include the whole frame.
            outer=[x,y,x+101,y+91]
            inner=[x+9,y+9,x+92,y+82]
            self.assertTrue(any(iou(p['box'],inner)>.9 and
                                intersection(p['crop'],outer)/area(outer)>=.99
                                for p in result['proposals']))
            self.assertLessEqual(len(result['proposals']),12)

    def test_blank_and_budget(self):
        self.assertEqual(propose(np.zeros((400,600,3),np.uint8))['proposals'],[])
        rng=np.random.default_rng(1234)
        result=propose(rng.integers(0,256,(640,640,3),dtype=np.uint8),limit=4)
        self.assertLessEqual(len(result['proposals']),4)
        for p in result['proposals']:
            x1,y1,x2,y2=p['crop']
            self.assertTrue(0<=x1<x2<=640 and 0<=y1<y2<=640)

    def test_nonuniform_resize_and_edge_padding(self):
        image=np.full((1001,2501,3),180,np.uint8)
        cv2.rectangle(image,(1800,600),(2010,810),(0,0,0),-1)
        result=propose(image)
        self.assertTrue(any(iou(p['box'],[1800,600,2011,811])>.9 for p in result['proposals']))
        self.assertEqual(padded_box([0,0,20,20],100,100),[0,0,24,24])

    def test_mapping_and_cross_crop_dedup(self):
        a=remap_detection(dict(confidence=.8,bbox_pixels=[10,20,80,90]),[100,200,200,300],640,480,'a')
        b=remap_detection(dict(confidence=.9,bbox_pixels=[20,30,90,100]),[90,190,210,310],640,480,'b')
        self.assertEqual(a['bbox_pixels'],[110.,220.,180.,290.])
        self.assertFalse(a['touches_internal_crop_edge'])
        self.assertEqual(len(merge([a,b])),1)
        self.assertEqual(merge([a,b])[0]['source'],'b')

    def test_internal_seam_is_rejected_not_image_boundary(self):
        detection=dict(confidence=.8,bbox_pixels=[0,0,100,100])
        self.assertTrue(remap_detection(detection,[20,30,120,130],200,200,'x')['touches_internal_crop_edge'])
        self.assertFalse(remap_detection(detection,[0,0,100,100],100,100,'x')['touches_internal_crop_edge'])


if __name__=='__main__':
    unittest.main()

"""Synthetic cross-zoom rejection paths, not field accuracy measurements."""
import unittest
import cv2
import numpy as np
from duplicate_reference import locate_commissioned
from geometry import align


class DuplicateReferenceTests(unittest.TestCase):
    def setUp(self):
        rng=np.random.RandomState(73)
        gray=cv2.GaussianBlur(rng.randint(0,256,(480,640),np.uint8),(3,3),.5)
        self.source=cv2.cvtColor(gray,cv2.COLOR_GRAY2BGR)
        self.box=[280,205,360,275]
        self.matrix=np.array([[1.8,0,-256],[0,1.8,-192],[0,0,1.]])
        self.target=cv2.warpPerspective(self.source,self.matrix,(640,480))
        self.a=dict(pan=200.,tilt=0.,zoom=1.)
        self.b=dict(self.a,zoom=1.8)

    def test_expected_zoom_preserves_global_and_local_evidence(self):
        self.assertFalse(align(self.source,self.target)['accepted'])
        result=locate_commissioned(self.source,self.target,self.box,self.a,self.b)
        self.assertEqual(result['status'],'matched_reference',result)
        self.assertFalse(result['motion_authorized'])

    def test_inconsistent_camera_zoom_is_rejected_even_if_features_match(self):
        result=locate_commissioned(self.source,self.target,self.box,self.a,self.a)
        self.assertEqual(result['reason'],'optical_scale_inconsistent',result)

    def test_erased_inner_face_is_not_accepted_from_background_features(self):
        changed=self.source.copy();changed[205:275,280:360]=128
        target=cv2.warpPerspective(changed,self.matrix,(640,480))
        result=locate_commissioned(self.source,target,self.box,self.a,self.b)
        self.assertEqual(result['status'],'uncertain',result)

    def test_remote_poses_are_not_matched_from_similar_appearance(self):
        result=locate_commissioned(self.source,self.target,self.box,self.a,dict(self.b,pan=210.))
        self.assertEqual(result['reason'],'commissioned_poses_too_far',result)


if __name__=='__main__':unittest.main()

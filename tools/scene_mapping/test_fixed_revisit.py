"""Saved-pose positives and adversarial negatives; no camera or model needed."""
from __future__ import division
import unittest
import cv2
import numpy as np

from fixed_revisit import locate_saved_pose


class FixedRevisitTests(unittest.TestCase):
    def setUp(self):
        cv2.setNumThreads(2)
        self.source = np.random.RandomState(73).randint(0, 256, (800,1000,3), np.uint8)
        self.source = cv2.GaussianBlur(self.source, (3,3), .6)
        self.box = [400,280,600,500]
        cv2.rectangle(self.source, (402,282), (598,498), (15,15,15), 4)
        cv2.circle(self.source, (500,390), 80, (245,245,245), 4)
        cv2.line(self.source, (500,390), (465,340), (0,0,0), 4)
        cv2.putText(self.source, 'V 450', (440,455), cv2.FONT_HERSHEY_SIMPLEX, .7, (0,0,0), 2)
        self.pose = dict(pan=210.4, tilt=0., zoom=2.)

    def locate(self, image, observed=None, reference=None):
        return locate_saved_pose(self.source, image, self.box,
                                 self.pose if reference is None else reference,
                                 self.pose if observed is None else observed)

    def test_background_replaced_but_local_meter_survives(self):
        image = np.random.RandomState(32).randint(0,256,self.source.shape,np.uint8)
        image[225:555,345:655] = self.source[225:555,345:655]
        result = self.locate(image)
        self.assertEqual(result['status'], 'matched_reference', result)
        self.assertLess(np.max(np.abs(np.asarray(result['bbox'])-self.box)), 2)
        self.assertFalse(result['motion_authorized'])
        self.assertIsNone(result['reading'])
        self.assertTrue(result['competition']['compared_separate_location'])

    def test_small_translation_recovers_full_image_coordinates(self):
        h = np.array([[1.,0,8], [0,1.,-5], [0,0,1.]])
        target = cv2.warpPerspective(self.source,h,(1000,800))
        result = self.locate(target)
        self.assertEqual(result['status'], 'matched_reference', result)
        self.assertLess(np.max(np.abs(np.asarray(result['bbox'])-[408,275,608,495])), 2)

    def test_wrong_angle_and_zoom_rejected_before_matching(self):
        for axis, value in (('pan',210.8), ('tilt',.4), ('zoom',1.9)):
            observed = dict(self.pose, **{axis:value})
            result = self.locate(self.source, observed=observed)
            self.assertEqual(result['reason'], 'saved_capture_pose_not_restored')
            self.assertIsNone(result['bbox'])

    def test_invalid_or_missing_pose_does_not_enter_roi_mode(self):
        for reference in ({}, dict(self.pose,pan=float('nan')), dict(self.pose,zoom=True)):
            with self.assertRaises(ValueError):
                self.locate(self.source, reference=reference)

    def test_target_erased_but_surrounding_scene_survives(self):
        image = self.source.copy()
        image[275:505,395:605] = 128
        self.assertEqual(self.locate(image)['status'], 'uncertain')

    def test_frame_survives_but_inner_face_erased(self):
        image = self.source.copy()
        image[324:456,440:560] = 128
        self.assertEqual(self.locate(image)['status'], 'uncertain')

    def test_another_meter_patch_at_expected_location_is_rejected(self):
        image = self.source.copy()
        image[280:500,400:600] = self.source[20:240,40:240]
        self.assertEqual(self.locate(image)['status'], 'uncertain')

    def test_meter_displaced_beyond_local_budget_is_rejected(self):
        image = self.source.copy()
        patch = image[280:500,400:600].copy()
        image[280:500,400:600] = 128
        image[280:500,470:670] = patch
        self.assertEqual(self.locate(image)['status'], 'uncertain')

    def test_duplicate_nearby_meter_is_not_hidden_by_tiny_roi_peak_gap(self):
        image = self.source.copy()
        image[280:500,620:820] = self.source[280:500,400:600]
        result = self.locate(image)
        self.assertEqual(result['status'], 'uncertain', result)
        self.assertEqual(result['reason'], 'competing_meter_appearance', result)
        self.assertGreater(result['competition']['alternative_score'], .95)

    def test_unreported_optical_scale_change_does_not_get_accepted(self):
        h = cv2.getRotationMatrix2D((500,390), 0, 1.3)
        image = cv2.warpAffine(self.source,h,(1000,800))
        self.assertEqual(self.locate(image)['status'], 'uncertain')

    def test_dimension_changes_and_invalid_boxes_are_rejected(self):
        with self.assertRaises(ValueError):
            self.locate(self.source[:700])
        for box in ([0,0,2000,200], [400,280,float('inf'),500], [True,0,100,100]):
            with self.assertRaises(ValueError):
                locate_saved_pose(self.source,self.source,box,self.pose,self.pose)


if __name__ == '__main__':
    unittest.main()

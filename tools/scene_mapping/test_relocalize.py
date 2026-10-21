"""Synthetic error-path checks. These are not a field accuracy benchmark."""
import unittest
import cv2
import numpy as np

from relocalize import locate, reject_overlaps


class RelocalizeTests(unittest.TestCase):
    def setUp(self):
        rng = np.random.RandomState(21)
        gray = rng.randint(0, 256, (480, 640), np.uint8)
        gray = cv2.GaussianBlur(gray, (3, 3), .6)
        self.source = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        self.box = [210, 180, 290, 270]
        cv2.rectangle(self.source, (211, 181), (288, 268), (20, 20, 20), 3)
        cv2.circle(self.source, (250, 225), 27, (240, 240, 240), 3)
        cv2.line(self.source, (250, 225), (238, 204), (0, 0, 0), 3)
        self.h = np.array([[1., 0, 15], [0, 1., 7], [0, 0, 1.]])
        self.registration = dict(accepted=True, matrix=self.h.tolist())

    def shifted(self, image=None):
        return cv2.warpPerspective(self.source if image is None else image, self.h, (640, 480))

    def test_global_registration_and_local_appearance_recover_translation(self):
        r = locate(self.source, self.shifted(), self.box)
        self.assertEqual(r['status'], 'matched_reference', r)
        self.assertLess(np.max(np.abs(np.array(r['bbox'])-[225, 187, 305, 277])), 2)
        self.assertFalse(r['motion_authorized'])
        self.assertIsNone(r['reading'])

    def test_local_mask_rejected_even_when_background_registers(self):
        image = self.source.copy()
        image[174:276, 204:296] = 128
        r = locate(self.source, self.shifted(image), self.box)
        self.assertEqual(r['status'], 'uncertain', r)
        self.assertTrue(r['registration']['accepted'])

    def test_inner_face_erased_rejected_even_if_frame_survives(self):
        image = self.source.copy()
        image[198:252, 226:274] = 150
        r = locate(self.source, self.shifted(image), self.box, self.registration)
        self.assertEqual(r['status'], 'uncertain', r)
        self.assertEqual(r['reason'], 'local_appearance_rejected')

    def test_local_target_displacement_is_not_adopted_as_same_fixed_map(self):
        image = self.source.copy()
        patch = image[180:270, 210:290].copy()
        image[180:270, 210:290] = 128
        image[180:270, 240:320] = patch
        r = locate(self.source, self.shifted(image), self.box, self.registration)
        self.assertEqual(r['reason'], 'target_displaced_from_scene_reference', r)

    def test_blank_scene_rejected(self):
        r = locate(self.source, np.zeros_like(self.source), self.box)
        self.assertEqual(r['reason'], 'scene_registration_rejected')

    def test_textureless_reference_rejected(self):
        r = locate(np.zeros_like(self.source), self.source, self.box)
        self.assertEqual(r['reason'], 'reference_has_insufficient_texture')

    def test_crop_edge_is_not_filled_with_invented_pixels(self):
        h = np.array([[1., 0, -250], [0, 1., 0], [0, 0, 1.]])
        target = cv2.warpPerspective(self.source, h, (640, 480))
        r = locate(self.source, target, self.box, dict(accepted=True, matrix=h.tolist()))
        self.assertEqual(r['status'], 'uncertain', r)

    def test_invalid_matrix_rejected(self):
        for h in (np.zeros((3,3)), np.full((3,3), float('nan'))):
            r = locate(self.source, self.source, self.box, dict(accepted=True, matrix=h.tolist()))
            self.assertEqual(r['status'], 'uncertain')

    def test_one_visual_region_cannot_match_two_registry_ids(self):
        r = locate(self.source, self.shifted(), self.box, self.registration)
        out = reject_overlaps([dict(r, instrument_id='M0001'), dict(r, instrument_id='M0002')])
        self.assertTrue(all(x['status'] == 'uncertain' and x['bbox'] is None for x in out))

    def test_wrong_patch_rejected_inside_correct_scene(self):
        image = self.source.copy()
        image[180:270, 210:290] = self.source[70:160, 420:500]
        r = locate(self.source, self.shifted(image), self.box, self.registration)
        self.assertEqual(r['status'], 'uncertain', r)

    def test_repeated_texture_has_no_unique_peak(self):
        tile = np.random.RandomState(5).randint(0, 256, (16,16,3), np.uint8)
        repeated = np.tile(tile, (30,40,1))
        r = locate(repeated, self.shifted(repeated), self.box, self.registration)
        self.assertEqual(r['reason'], 'repeated_appearance_ambiguous', r)


if __name__ == '__main__':
    unittest.main()

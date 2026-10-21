"""Reference-plan integrity tests; no camera calls."""
import copy
import unittest

import test_console
from references import read_reference_image, reference_plan, validate_current
from store import Conflict


class ReferenceTests(test_console.RegistryFixture):
    def plan(self):
        s = self.state()
        return reference_plan(s, s['maps'][0]['id'], s['revision'])

    def test_confirmed_only_preserves_ids_and_source_pose_without_enabling_motion(self):
        self.review(0, 'confirm', name='left')
        p = self.plan()
        self.assertEqual([a['instrument_id'] for a in p['targets']], ['M0001'])
        self.assertEqual(p['targets'][0]['references'][0]['source_pose']['pan'], 200)
        self.assertFalse(p['execution_enabled'])
        self.assertFalse(p['motion_verified'])
        self.assertEqual(self.state()['instruments'][0]['pose_status'], 'unverified')
        self.assertIsNone(self.state()['instruments'][0]['visit_pose'])
        self.assertIn('source_pose_not_from_bounded_capture', p['targets'][0]['blockers'])

    def test_shared_source_group_and_human_merge(self):
        self.review(0, 'confirm')
        self.review(1, 'confirm')
        p = self.plan()
        self.assertEqual(p['source_groups'][0]['instruments'], ['M0001', 'M0002'])
        s = self.state()
        self.review(1, 'merge', target_id=s['candidates'][0]['id'])
        p = self.plan()
        self.assertEqual(len(p['targets']), 1)
        self.assertEqual(len(p['targets'][0]['references']), 2)
        self.assertEqual(p['targets'][0]['references'][1]['selection'], 'human_merged')

    def test_disabled_and_rejected_are_not_revisit_targets(self):
        self.review(0, 'confirm')
        s = self.state()
        self.store.update_instrument(s['revision'], 'M0001', 'left', False)
        self.assertEqual(self.plan()['targets'], [])
        self.review(1, 'reject')
        self.assertEqual(self.plan()['targets'], [])

    def test_revision_and_geometry_edits_invalidate_old_plan(self):
        self.review(0, 'confirm')
        old = self.plan()
        self.review(0, 'bbox', box=[21, 21, 50, 60])
        with self.assertRaises(Conflict):
            validate_current(old, self.state())
        tampered = copy.deepcopy(self.plan())
        tampered['targets'][0]['references'][0]['bbox'][0] = 3
        with self.assertRaises(Conflict):
            validate_current(tampered, self.state())

    def test_invalidated_map_cannot_supply_plan(self):
        s = self.state()
        self.store.invalidate(s['revision'], s['maps'][0]['id'])
        with self.assertRaises(ValueError):
            self.plan()

    def test_fingerprint_stable_and_result_detached(self):
        self.review(0, 'confirm')
        before = self.state()
        p = self.plan()
        self.assertEqual(p, validate_current(p, before))
        p['targets'][0]['references'][0]['bbox'][0] = 1
        self.assertEqual(self.state(), before)
        self.assertNotEqual(p, self.plan())

    def test_source_hash_dimensions_and_name_checked(self):
        self.review(0, 'confirm')
        ref = self.plan()['targets'][0]['references'][0]
        self.assertEqual(read_reference_image(ref, self.store.images), test_console.png())
        for changes in ({'image': '../source.png'}, {'width': 999}, {'sha256': '0'*64}):
            with self.assertRaises(ValueError):
                read_reference_image(dict(ref, **changes), self.store.images)
        (self.store.images / ref['image']).write_bytes(b'corrupt')
        with self.assertRaises(ValueError):
            read_reference_image(ref, self.store.images)


if __name__ == '__main__':
    unittest.main()

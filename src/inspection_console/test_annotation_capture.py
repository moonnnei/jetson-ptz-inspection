"""Acquisition/identity separation and bounded commissioning; no real camera."""
import copy
from pathlib import Path
import threading
import unittest

from annotation_capture import AnnotatedCapture, annotation_plan, validate_annotation
from store import Store
import test_first_capture as fixtures


class SeedIO(fixtures.PhotoIO):
    seed_rejected=False
    revisit_rejected=False

    def __init__(self,plan,raw):
        fixtures.PhotoIO.__init__(self,plan,raw)
        self.required_tilt=-3.
        self.tilt_sign=1.
        self.bootstrap_calls=0

    def bootstrap(self,ref,frame):
        self.bootstrap_calls+=1
        return dict(status='uncertain' if self.seed_rejected else 'matched_reference',
            reason='scene_unregistered' if self.seed_rejected else 'new_seed',
            bbox=frame['test_box'],basis='test_existing_annotation_transfer')

    def locate(self,ref,frame):
        if ref['image'].endswith('.png'):
            raise AssertionError('old-template appearance was used as an acquisition gate')
        if self.revisit_rejected and frame['purpose'].startswith('independent_revisit_'):
            return dict(status='uncertain',reason='revisit_identity_ambiguous')
        if not frame['purpose'].startswith('independent_revisit_'):
            if max(abs(frame['pose'][k]-ref['source_pose'][k]) for k in ('pan','tilt'))>4.11:
                return dict(status='uncertain',reason='large_unobserved_appearance_jump')
            if abs(frame['pose']['zoom']-ref['source_pose']['zoom'])>.31:
                return dict(status='uncertain',reason='large_unobserved_zoom_jump')
        return fixtures.PhotoIO.locate(self,ref,frame)

    def quality(self,frame):
        return dict(sharpness=120.,clipped_fraction=.01,crop_size=[100,120])


class AnnotationCaptureTests(unittest.TestCase):
    def setUp(self):
        fixtures.FirstCaptureTests.setUp(self)
        state=self.store.snapshot()
        def projection(s):
            Store.find(s,'maps',self.map_id)['overview']=dict(image=s['frames'][0]['image'],ring_projection=dict(
                version=1,projection='angle_assisted_cylindrical',width=4096,height=609,
                source_width=2560,source_height=1440,focal_pixels=1542.85,yaw_image_sign=1,
                source_orientation='rotate180_from_raw',calibration_validated=False))
        self.store.change(state['revision'],'test_projection',{},projection)
        target=self.store.snapshot()['instruments'][0]['id']
        self.plan=annotation_plan(self.store.snapshot(),self.map_id,self.anchor,target,'fresh-attempt')
        self.io=SeedIO(self.plan,self.raw)

    def tearDown(self):fixtures.FirstCaptureTests.tearDown(self)

    def run_capture(self,cancel=None):
        return AnnotatedCapture(self.io,self.root/'annotation-run',self.plan,cancel).run()

    def test_old_template_is_bypassed_but_two_independent_fresh_checks_are_required(self):
        result=self.run_capture()
        self.assertEqual(result['status'],'completed',result.get('error'))
        self.assertEqual(self.io.bootstrap_calls,1)
        self.assertTrue(result['preset_commissioned'])
        self.assertTrue(result['stop_confirmed'])
        self.assertEqual(result['matching_fresh_observations'],2)
        self.assertFalse(result['periodic_capture_enabled'])
        self.assertFalse(result['manual_review_required'])
        self.assertFalse(result['geometry_calibrated'])
        self.assertIsNone(result['reading'])
        self.assertEqual(self.io.position(),self.plan['anchor'])
        checks=[f for f in result['captures'] if f['purpose'].startswith('independent_revisit_')]
        self.assertEqual(len(checks),2)
        self.assertNotEqual(checks[0]['path'],checks[1]['path'])
        self.assertGreater(len([f for f in result['captures']
            if f['purpose']=='tracked_coarse_step_fresh_check']),1)

    def test_uncertain_scene_still_saves_a_new_diagnostic_image_without_commissioning(self):
        self.io.seed_rejected=True
        result=self.run_capture()
        self.assertEqual(result['status'],'failed')
        self.assertEqual(len(result['captures']),1)
        self.assertTrue((self.root/'annotation-run'/result['captures'][0]['path']).exists())
        self.assertFalse(result['preset_commissioned'])
        self.assertTrue(result['stop_confirmed'])
        self.assertNotIn('visit_pose',result)

    def test_independent_revisit_failure_never_commissions_a_preset(self):
        self.io.revisit_rejected=True
        result=self.run_capture()
        self.assertEqual(result['status'],'failed')
        self.assertFalse(result['preset_commissioned'])
        self.assertIn('revisit_identity_ambiguous',result['error'])

    def test_stop_failure_invalidates_otherwise_successful_capture(self):
        self.io.stop_failure=True
        result=self.run_capture()
        self.assertFalse(result['preset_commissioned'])
        self.assertFalse(result['stop_confirmed'])

    def test_benign_history_revision_does_not_replace_scene_or_invalidate_own_progress(self):
        state=self.store.snapshot()
        state['revision']+=1
        state['jobs'].append(dict(id='historical',status='completed'))
        self.assertEqual(validate_annotation(self.plan,state),self.plan)
        state['instruments'][0]['name']='a different binding'
        with self.assertRaises(ValueError):validate_annotation(self.plan,state)

    def test_changed_motion_budget_or_target_box_is_refused(self):
        plan=copy.deepcopy(self.plan)
        plan['budget_seconds']+=1
        with self.assertRaises(ValueError):validate_annotation(plan,self.store.snapshot())
        state=self.store.snapshot()
        state['candidates'][0]['bbox'][0]+=1
        with self.assertRaises(ValueError):validate_annotation(self.plan,state)

    def test_cancelled_start_stops_without_any_snapshot(self):
        cancel=threading.Event();cancel.set()
        result=self.run_capture(cancel)
        self.assertFalse(result['preset_commissioned'])
        self.assertEqual(result['captures'],[])
        self.assertTrue(result['stop_confirmed'])


if __name__=='__main__':unittest.main()

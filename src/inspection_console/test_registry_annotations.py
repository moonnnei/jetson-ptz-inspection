"""Missed boxes queue safely, and automatic proposals never become human reviews."""
import copy
import unittest

from annotation_capture import AnnotatedCapture,annotation_plan,scene_binding
from annotation_history import import_annotation_capture
from capture_round import photo_reference
from registry_annotations import queue_box,materialize,finish_queue,merge_commissioned_duplicate
from store import Conflict
import test_preset_patrol as fixtures


class SemanticIO(fixtures.SimulatedMeterIO):
    def locate(self,ref,frame):
        result=fixtures.SimulatedMeterIO.locate(self,ref,frame)
        if frame['purpose'].startswith('independent_revisit_'):
            result.update(category_verified=True,category_recheck=dict(accepted=True))
        return result


class AnnotationQueueTests(unittest.TestCase):
    setUp=fixtures.PresetPlanTests.setUp
    tearDown=fixtures.PresetPlanTests.tearDown
    commission_all=fixtures.PresetPlanTests.commission_all

    def queue_new(self,automatic=False):
        state=self.store.snapshot();frame=state['frames'][0]
        box=[1200.,300.,1400.,500.]
        candidate=None
        if automatic:
            candidate='candidate-model-proposal'
            def add(s):s['candidates'].append(dict(id=candidate,map_id=self.map_id,frame_id=frame['id'],
                bbox=box,confidence=.8,source='yolo_model',state='pending',instrument_id=None,merged_into=None))
            self.store.change(state['revision'],'test_model_proposal',{},add)
        result=queue_box(self.store,self.store.snapshot()['revision'],frame['id'],box,
                         'automatic' if automatic else 'human',candidate)
        return result['result']['queued_id']

    def test_a_box_during_patrol_is_queued_without_changing_its_running_scene_binding(self):
        state=self.commission_all();binding=scene_binding(state,self.map_id)
        self.store.change(state['revision'],'test_running_job',{},lambda s:s['jobs'].append(
            dict(id='current-run',map_id=self.map_id,status='running',results=[])))
        key=self.queue_new()
        queued=self.store.snapshot()
        self.assertEqual(scene_binding(queued,self.map_id),binding)
        with self.assertRaises(Conflict):materialize(self.store,queued['revision'],key)
        self.assertEqual(queued['annotation_queue'][-1]['state'],'queued')

    def test_new_human_box_needs_no_confirmation_and_preserves_other_verified_poses(self):
        before=self.commission_all();key=self.queue_new()
        imported=materialize(self.store,self.store.snapshot()['revision'],key)
        state=self.store.snapshot()
        self.assertEqual(len(imported['result']['rebound_unchanged_presets']),2)
        self.assertEqual(len(state['instruments']),3)
        new=next(a for a in state['instruments'] if a['id']==imported['result']['instrument_id'])
        candidate=next(c for c in state['candidates'] if c['id']==new['candidate_id'])
        self.assertEqual(candidate['state'],'human_labeled')
        self.assertEqual(new['pose_status'],'unverified')
        for asset in state['instruments'][:-1]:
            stop,reason=photo_reference(state,asset);self.assertIsNone(reason)
            old=next(a for a in before['instruments'] if a['id']==asset['id'])
            self.assertEqual(stop['pose'],old['visit_pose'])

    def test_duplicate_source_box_reuses_existing_id_without_new_motion_or_asset(self):
        state=self.commission_all();c=state['candidates'][0]
        q=queue_box(self.store,state['revision'],c['frame_id'],c['bbox'])['result']['queued_id']
        result=materialize(self.store,self.store.snapshot()['revision'],q)
        self.assertFalse(result['result']['camera_motion_required'])
        self.assertEqual(result['result']['duplicate_of'],c['instrument_id'])
        self.assertEqual(len(self.store.snapshot()['instruments']),2)

    def test_automatic_proposal_requires_fresh_semantic_checks_and_keeps_automatic_provenance(self):
        self.commission_all();key=self.queue_new(True)
        target=materialize(self.store,self.store.snapshot()['revision'],key)['result']['instrument_id']
        state=self.store.snapshot();plan=annotation_plan(state,self.map_id,self.anchor,target,'automatic-test')
        folder=self.root/'automatic-proposal'
        result=AnnotatedCapture(SemanticIO(plan,self.raw),folder,plan).run()
        self.assertEqual(result['status'],'completed',result.get('error'))
        import_annotation_capture(self.store,folder,state['revision'])
        state=self.store.snapshot();asset=next(a for a in state['instruments'] if a['id']==target)
        candidate=next(c for c in state['candidates'] if c['id']==asset['candidate_id'])
        self.assertEqual(candidate['state'],'auto_verified')
        self.assertEqual(candidate['acceptance_source'],'automatic')
        self.assertEqual(state['jobs'][-1]['results'][0]['categorical_verification_basis'],'two_fresh_NCNN_rechecks')
        self.assertTrue(state['maps'][0]['motion_verified'])

    def test_automatic_proposal_without_semantics_cannot_be_promoted(self):
        self.commission_all();key=self.queue_new(True)
        target=materialize(self.store,self.store.snapshot()['revision'],key)['result']['instrument_id']
        state=self.store.snapshot();plan=annotation_plan(state,self.map_id,self.anchor,target,'no-semantic-test')
        folder=self.root/'no-semantic'
        AnnotatedCapture(fixtures.SimulatedMeterIO(plan,self.raw),folder,plan).run()
        with self.assertRaises(ValueError):import_annotation_capture(self.store,folder,state['revision'])
        state=self.store.snapshot()
        asset=next(a for a in state['instruments'] if a['id']==target)
        self.assertEqual(asset['pose_status'],'unverified')

    def test_rejected_automatic_proposal_is_retained_but_does_not_poison_existing_patrol_positions(self):
        self.commission_all();key=self.queue_new(True)
        materialize(self.store,self.store.snapshot()['revision'],key)
        finish_queue(self.store,self.store.snapshot()['revision'],key,'uncertain')
        state=self.store.snapshot()
        self.assertEqual(state['annotation_queue'][-1]['state'],'uncertain')
        self.assertEqual(state['candidates'][-1]['state'],'auto_uncertain')
        self.assertFalse(state['instruments'][-1]['enabled'])
        self.assertTrue(state['maps'][0]['motion_verified'])

    def test_geometry_alone_cannot_merge_two_instrument_ids(self):
        state=self.commission_all()
        with self.assertRaises(ValueError):
            merge_commissioned_duplicate(self.store,state['revision'],state['instruments'][0]['id'],
                                        state['instruments'][1]['id'],dict(direction_difference_degrees=0))


if __name__=='__main__':unittest.main()

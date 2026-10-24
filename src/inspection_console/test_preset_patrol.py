"""Software-preset route binding and one-shot/periodic lifecycle, no hardware."""
import copy
import unittest

from annotation_capture import AnnotatedCapture,annotation_plan
from annotation_history import import_annotation_capture
from preset_patrol import preset_plan,validate_preset,result_for_registry
from scheduler import Scheduler
import test_annotation_capture as fixtures
import test_console as console_tests


class SimulatedMeterIO(fixtures.SeedIO):
    def capture(self,root,index,cancel):
        frame=fixtures.SeedIO.capture(self,root,index,cancel)
        offset=.012*frame['pose']['zoom']*(210.-self.plan['coarse_pose']['pan'])*frame['width']
        frame['test_box'][0]+=offset;frame['test_box'][2]+=offset
        return frame


class PresetPlanTests(unittest.TestCase):
    setUp=fixtures.AnnotationCaptureTests.setUp
    tearDown=fixtures.AnnotationCaptureTests.tearDown

    def commission_all(self):
        ids=[a['id'] for a in self.store.snapshot()['instruments']]
        for key in ids:
            state=self.store.snapshot()
            plan=annotation_plan(state,self.map_id,self.anchor,key,'test-'+key)
            io=SimulatedMeterIO(plan,self.raw)
            folder=self.root/key
            record=AnnotatedCapture(io,folder,plan).run()
            self.assertEqual(record['status'],'completed',record.get('error'))
            import_annotation_capture(self.store,folder,state['revision'])
        return self.store.snapshot()

    def test_patrol_uses_new_commissioned_references_instead_of_legacy_scan_positions(self):
        state=self.commission_all()
        plan=preset_plan(state,self.map_id,self.anchor,'patrol-test')
        self.assertTrue(plan['ready_for_trial'])
        self.assertEqual(len(plan['stops']),2)
        self.assertTrue(all(t['reference']['source_kind']=='annotation_commissioning' for t in plan['stops']))
        self.assertTrue(all(t['visit_pose_verified'] for t in plan['stops']))
        self.assertFalse(plan['periodic_capture_enabled'])
        self.assertEqual(plan['budget_seconds'],180)

    def test_incomplete_commissioning_or_changed_preset_blocks_patrol(self):
        with self.assertRaises(ValueError):preset_plan(self.store.snapshot(),self.map_id,self.anchor,'none')
        state=self.commission_all()
        state['instruments'][0]['visit_pose']['pan']+=1
        with self.assertRaises(ValueError):preset_plan(state,self.map_id,self.anchor,'changed')

    def test_history_progress_does_not_change_a_fixed_preset_route_but_annotation_changes_do(self):
        state=self.commission_all()
        plan=preset_plan(state,self.map_id,self.anchor,'patrol-test')
        state['revision']+=1
        state['jobs'].append(dict(id='current',status='running',results=[]))
        self.assertEqual(validate_preset(plan,state),plan)
        state['candidates'][0]['bbox'][0]+=2
        with self.assertRaises(ValueError):validate_preset(plan,state)

    def test_preset_stays_usable_after_its_job_leaves_the_recent_history_window(self):
        state=self.commission_all()
        original=preset_plan(state,self.map_id,self.anchor,'retention-test')
        self.store.change(state['revision'],'test_recent_window_trim',{},lambda s:s.update(jobs=[]))
        state=self.store.snapshot()
        retained=preset_plan(state,self.map_id,self.anchor,'retention-test')
        self.assertEqual(original['stops'],retained['stops'])
        for stop in retained['stops']:
            self.assertTrue(self.store.image_path(stop['reference']['image']).exists())


class OneShotSchedulerTests(console_tests.RegistryFixture):
    def make_once(self,round_adapter=False):
        for c in self.state()['candidates']:self.store.review(self.state()['revision'],c['id'],'confirm')
        def setup(s):
            s['maps'][0]['motion_verified']=True
            for i,a in enumerate(s['instruments']):a.update(pose_status='verified',visit_pose=dict(pan=200+i*5,tilt=5,zoom=1))
        self.store.change(None,'test_setup',{},setup)
        adapter=console_tests.FakeAdapter()
        if round_adapter:
            def run_round(key,targets,cancel):
                for asset in targets:
                    yield adapter.visit(asset,cancel)
            adapter.run_round=run_round
        self.engine=Scheduler(self.store,adapter)
        self.adapter=adapter
        return self.state()['maps'][0]['id']

    def test_one_shot_finishes_without_arming_or_scheduling_another_round(self):
        key=self.make_once(True)
        result=self.engine.run_once(self.state()['revision'],key)
        self.engine.worker.join(2)
        self.assertTrue(result['started'])
        state=self.state()
        self.assertEqual(state['jobs'][-1]['status'],'completed')
        self.assertFalse(state['schedule']['enabled'])
        self.assertIsNone(state['schedule']['next_due_at'])
        self.engine.tick()
        self.assertEqual(len(self.adapter.visits),2)

    def test_uncertain_periodic_round_pauses_instead_of_silently_retrying_forever(self):
        key=self.make_once(True)
        original=self.adapter.visit
        def uncertain(asset,cancel):
            item=original(asset,cancel);item['status']='uncertain';return item
        self.adapter.visit=uncertain
        self.store.configure_schedule(self.state()['revision'],key,60)
        self.engine.arm(self.state()['revision']);self.engine.tick();self.engine.worker.join(2)
        state=self.state()
        self.assertEqual(state['jobs'][-1]['status'],'completed_with_errors')
        self.assertFalse(state['schedule']['enabled'])
        self.assertIsNone(state['schedule']['next_due_at'])

    def test_omitted_target_does_not_count_as_complete(self):
        key=self.make_once(True)
        def missing(job,targets,cancel):
            yield self.adapter.visit(targets[0],cancel)
        self.adapter.run_round=missing
        self.engine.run_once(self.state()['revision'],key);self.engine.worker.join(2)
        self.assertEqual(self.state()['jobs'][-1]['status'],'failed')


if __name__=='__main__':unittest.main()

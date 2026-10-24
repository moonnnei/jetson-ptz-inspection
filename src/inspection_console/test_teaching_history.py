import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from references import reference_plan
from store import Store, Conflict
from teaching import pilot_plan
from teaching_history import import_failure,import_partial_capture,import_partial_batch,import_diagnostic_batch
from best_effort import capture_plan
from test_console import png


class TeachingHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.source=self.root/'source.png'
        self.source.write_bytes(png())
        self.store=Store(self.root/'private')
        self.store.import_manifest(dict(title='bounded test',provenance='bounded_scan_capture',frames=[
            dict(path=str(self.source),pose=dict(pan=233.3,tilt=5.5,zoom=1.),orientation='rotate180_from_raw',
                 detections=[dict(bbox=[20,20,50,60],confidence=.8),dict(bbox=[65,20,95,60],confidence=.8)])]))
        state=self.store.snapshot()
        for candidate in state['candidates']:
            self.store.review(self.store.snapshot()['revision'],candidate['id'],'confirm')
        self.before=self.store.snapshot()
        self.pilot=self.root/'pilot'
        self.pilot.mkdir()
        # Image format is detected from bytes, independently of the file name.
        (self.pilot/'frame-008.jpg').write_bytes(png(121,80))
        self.record=dict(status='failed',reading=None,periodic_capture_enabled=False,
            error='target_requires_motion_beyond_camera_range',stop_confirmed=True,
            started_at='2026-10-22T07:57:12+00:00',finished_at='2026-10-22T07:57:55+00:00',
            plan=pilot_plan(reference_plan(self.before,self.before['maps'][0]['id'],self.before['revision']),
                            dict(pan=202.9,tilt=5.5,zoom=1.4),'M0001'),
            captures=[dict(path='frame-008.jpg',sha256=hashlib.sha256(png(121,80)).hexdigest(),
                match=dict(status='matched_reference'),width=121,height=80,bbox=[20,20,50,60],
                pose=dict(pan=225.3,tilt=1.4,zoom=1.),center_error=[.186,.147],
                captured_at='2026-10-22T07:57:52+00:00')])
        self.save()

    def tearDown(self):self.tmp.cleanup()

    def save(self):
        (self.pilot/'record.json').write_text(json.dumps(self.record),encoding='utf-8')

    def test_failed_pilot_publishes_diagnostic_preserving_all_human_selections(self):
        receipt=import_failure(self.store,self.pilot,self.before['revision'])
        after=self.store.snapshot()
        self.assertEqual(receipt['revision'],self.before['revision']+1)
        self.assertEqual(after['instruments'],self.before['instruments'])
        self.assertEqual(after['candidates'],self.before['candidates'])
        self.assertFalse(after['schedule']['enabled'])
        self.assertFalse(after['maps'][0]['motion_verified'])
        result=after['jobs'][-1]['results'][0]
        self.assertEqual(result['image_status'],'failure_diagnostic_not_accepted_inspection_capture')
        self.assertEqual(self.store.image_path(result['image']).read_bytes(),png(121,80))

    def test_arbitrary_job_image_paths_are_not_served(self):
        self.store.change(self.before['revision'],'test_seed',{},lambda s:s['jobs'].append(
            dict(id='untrusted',status='failed',results=[dict(image='../registry.sqlite3')])))
        for key in ('../registry.sqlite3','unlisted.png','0'*64+'.jpg','0'*64+'.png\n'):
            with self.assertRaises(ValueError):self.store.image_path(key)

    def test_changed_review_revision_rejects_import_without_changing_registry(self):
        with self.assertRaises(Conflict):import_failure(self.store,self.pilot,self.before['revision']-1)
        self.assertEqual(self.store.snapshot(),self.before)

    def test_bad_image_hash_and_escaping_path_are_rejected(self):
        self.record['captures'][0]['sha256']='0'*64
        self.save()
        with self.assertRaises(ValueError):import_failure(self.store,self.pilot,self.before['revision'])
        self.record['captures'][0]['path']='../source.png'
        self.save()
        with self.assertRaises(ValueError):import_failure(self.store,self.pilot,self.before['revision'])
        self.assertEqual(self.store.snapshot(),self.before)

    def test_success_is_not_imported_using_a_failure_only_contract(self):
        self.record['status']='completed'
        self.save()
        with self.assertRaises(ValueError):import_failure(self.store,self.pilot,self.before['revision'])
        self.assertEqual(self.store.snapshot(),self.before)

    def test_partial_capture_can_be_reviewed_without_commissioning_a_pose(self):
        refs=reference_plan(self.before,self.before['maps'][0]['id'],self.before['revision'])
        self.record.update(status='partial_capture_completed',visit_pose_verified=False,
            revisit_verified=False,center_status='off_center_due_to_motion_limits',
            quality_status='appearance_checked_not_reading_validated',
            plan=capture_plan(refs,dict(pan=225.3,tilt=1.4,zoom=1.),'M0001'),
            evidence=dict(image_sha256=self.record['captures'][0]['sha256']))
        self.save()
        with self.assertRaises(ValueError):import_failure(self.store,self.pilot,self.before['revision'])
        import_partial_capture(self.store,self.pilot,self.before['revision'])
        after=self.store.snapshot()
        self.assertEqual(after['instruments'],self.before['instruments'])
        self.assertFalse(after['maps'][0]['motion_verified'])
        self.assertEqual(after['jobs'][-1]['results'][0]['status'],'captured_with_limits')
        self.assertIsNone(after['jobs'][-1]['error'])
        self.assertFalse(after['schedule']['enabled'])

    def test_partial_capture_refuses_a_fabricated_verification_flag(self):
        self.record.update(status='partial_capture_completed',visit_pose_verified=True,revisit_verified=True)
        self.save()
        with self.assertRaises(ValueError):import_partial_capture(self.store,self.pilot,self.before['revision'])
        self.assertEqual(self.store.snapshot(),self.before)

    def partial_record(self,target):
        record=copy.deepcopy(self.record)
        refs=reference_plan(self.before,self.before['maps'][0]['id'],self.before['revision'])
        record.update(status='partial_capture_completed',visit_pose_verified=False,revisit_verified=False,
            center_status='off_center_due_to_motion_limits',quality_status='not_reading_validated',
            plan=capture_plan(refs,dict(pan=225.3,tilt=1.4,zoom=1.),target,approach_source=True),
            evidence=dict(image_sha256=record['captures'][0]['sha256']))
        return record

    def test_two_partial_captures_are_imported_in_one_revision(self):
        roots=[]
        for key in ('M0001','M0002'):
            folder=self.root/key
            folder.mkdir()
            (folder/'record.json').write_text(json.dumps(self.partial_record(key)),encoding='utf-8')
            (folder/'frame-008.jpg').write_bytes(png(121,80))
            roots.append(folder)
        result=import_partial_batch(self.store,roots,self.before['revision'])
        after=self.store.snapshot()
        self.assertEqual(after['revision'],self.before['revision']+1)
        self.assertEqual(len(result['result']),2)
        self.assertEqual(after['instruments'],self.before['instruments'])
        self.assertFalse(after['schedule']['enabled'])

    def test_bad_second_capture_does_not_partially_change_registry(self):
        first=self.root/'first'
        second=self.root/'second'
        for folder,key in ((first,'M0001'),(second,'M0002')):
            folder.mkdir()
            record=self.partial_record(key)
            if folder==second:record['visit_pose_verified']=True
            (folder/'record.json').write_text(json.dumps(record),encoding='utf-8')
            (folder/'frame-008.jpg').write_bytes(png(121,80))
        with self.assertRaises(ValueError):import_partial_batch(self.store,[first,second],self.before['revision'])
        self.assertEqual(self.store.snapshot(),self.before)

    def test_mixed_batch_preserves_failure_and_retains_successful_capture(self):
        folders=[]
        for key in ('M0001','M0002'):
            folder=self.root/('mixed-'+key)
            folder.mkdir()
            record=self.partial_record(key)
            if key=='M0002':
                record.update(status='failed',error='target_uncertain:local_appearance_rejected')
            (folder/'record.json').write_text(json.dumps(record),encoding='utf-8')
            (folder/'frame-008.jpg').write_bytes(png(121,80))
            folders.append(folder)
        import_diagnostic_batch(self.store,folders,self.before['revision'])
        after=self.store.snapshot()
        self.assertEqual([j['status'] for j in after['jobs']],['partial_capture_completed','failed'])
        self.assertEqual(after['instruments'],self.before['instruments'])
        self.assertEqual(after['revision'],self.before['revision']+1)


if __name__=='__main__':unittest.main()

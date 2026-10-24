"""Initialization ownership, import handoff and interruption tests. No hardware."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request,build_opener,ProxyHandler

from annotation_service import AnnotationService
from scene_initialization import SceneInitialization
from scene_initialization_io import map_hits,merge
from server import Server
from store import Store,Conflict
from test_console import png


class Backend:
    def __init__(self,manifest,blocked=False):
        self.manifest=manifest;self.started=threading.Event();self.release=threading.Event()
        if not blocked:self.release.set()
        self.stop_calls=0
    def prepare(self):return dict(scan=dict(expected_frames=3),maximum_candidates=12)
    def run(self,plan,folder,cancel,progress):
        self.started.set()
        if not self.release.wait(3):raise RuntimeError('test worker timed out')
        progress('detecting',dict(views=3,total_views=3))
        return copy.deepcopy(self.manifest)
    def stop(self):self.stop_calls+=1;self.release.set()


class InitializationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.store=Store(self.root/'database')
        source=self.root/'source.png';source.write_bytes(png(2560,1440))
        overview=self.root/'overview.png';overview.write_bytes(png(4096,609))
        self.projection=dict(version=1,projection='angle_assisted_cylindrical',width=4096,height=609,
            source_width=2560,source_height=1440,focal_pixels=1542.85,yaw_image_sign=1,
            source_orientation='rotate180_from_raw',calibration_validated=False)
        self.manifest=dict(title='test new scene',provenance='bounded_scan_capture',
            frames=[dict(path=str(source),title='new image',width=2560,height=1440,
                pose=dict(pan=200,tilt=0,zoom=1),orientation='rotate180_from_raw',
                detections=[dict(bbox=[200,200,400,400],confidence=.8,source='ncnn_initialization/full_view')])],
            overview=dict(path=str(overview),sha256=hashlib.sha256(overview.read_bytes()).hexdigest(),
                kind='pose_projected_ring_preview',registered_views=0,total_views=1,stitched_views=0,
                projected_views=1,source_mapping='source_picker_only',full_panorama=False,ptz_calibration=False),
            projection=self.projection)
        old=copy.deepcopy(self.manifest);old.pop('overview');old['provenance']='historical_import'
        self.store.import_manifest(old)
        self.old=copy.deepcopy(self.store.snapshot()['maps'])
        self.annotations=AnnotationService(self.store,object(),self.root,self.root,object())

    def tearDown(self):self.tmp.cleanup()
    def service(self,backend=None):
        return SceneInitialization(self.store,self.annotations,backend,self.root)
    def start(self,backend=None):
        service=self.service(backend or Backend(self.manifest))
        service.start(self.store.snapshot()['revision']);service.thread.join(4)
        self.assertFalse(service.thread.is_alive())
        return service

    def test_default_never_starts_and_does_not_change_the_registry(self):
        service=self.service();before=self.store.snapshot()['revision']
        with self.assertRaises(ValueError):service.start(before)
        self.assertFalse(service.status()['available'])
        self.assertEqual(self.store.snapshot()['revision'],before)

    def test_import_preserves_old_scene_binds_geometry_and_queues_without_human_confirmation(self):
        service=self.start();state=self.store.snapshot()
        self.assertEqual(state['maps'][:-1],self.old)
        self.assertEqual(state['maps'][-1]['overview']['ring_projection'],self.projection)
        self.assertFalse(state['maps'][-1]['automatic_registration_requested'])
        self.assertEqual(len(state['annotation_queue']),1)
        self.assertEqual(state['annotation_queue'][0]['origin'],'automatic')
        self.assertEqual(state['initialization']['status'],'commissioning')
        self.assertFalse(state['schedule']['enabled'])
        self.assertTrue(service.status()['active'])

    def test_stop_during_processing_prevents_import_and_replay(self):
        backend=Backend(self.manifest,True);service=self.service(backend)
        service.start(self.store.snapshot()['revision']);self.assertTrue(backend.started.wait(2))
        service.stop();service.thread.join(4)
        self.assertEqual(len(self.store.snapshot()['maps']),1)
        self.assertEqual(self.store.snapshot()['initialization']['status'],'stopped')
        self.assertEqual(backend.stop_calls,1)
        self.assertTrue(self.annotations.status()['paused'])

    def test_no_detection_keeps_new_overview_for_supplement_without_success_claim(self):
        self.manifest['frames'][0]['detections']=[]
        service=self.start();job=service.status()['job']
        self.assertEqual(job['status'],'awaiting_supplement')
        self.assertFalse(job['ready_for_patrol'])
        self.assertEqual(len(self.store.snapshot()['maps']),2)
        self.assertFalse(service.status()['active'])

    def seed_previous_capture(self):
        state=self.store.snapshot();candidate=state['candidates'][0]
        self.store.review(state['revision'],candidate['id'],'confirm',name='原现场主电流表')
        state=self.store.snapshot();asset=state['instruments'][0];frame=state['frames'][0]
        row=dict(instrument_id=asset['id'],status='captured',image=frame['image'],
                 image_sha256=frame['sha256'],captured_at='2026-10-23T10:00:00+00:00',bbox=candidate['bbox'])
        def seed(s):
            s['jobs'].append(dict(id='previous-round',map_id=asset['map_id'],mode='software_preset_capture',
                status='completed',started_at=row['captured_at'],route=[asset['id']],results=[row]))
            s['instruments'][0].update(visit_pose=dict(pan=201,tilt=2,zoom=1.8),pose_status='verified')
        self.store.change(None,'fixture_previous_capture',{},seed)
        return self.store.snapshot(include_history=True)

    def assert_previous_capture_preserved(self,before):
        state=self.store.snapshot(include_history=True);old_id=before['maps'][0]['id']
        for kind in ('maps','frames','candidates','instruments'):
            self.assertEqual([v for v in state[kind] if (v['id'] if kind=='maps' else v['map_id'])==old_id],before[kind])
        self.assertEqual(state['job_history'],before['job_history'])
        old=next(j for j in state['jobs'] if j['id']=='previous-round')
        self.assertEqual(old,before['jobs'][0])
        raw=self.store.image_path(old['results'][0]['image']).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),old['results'][0]['image_sha256'])

    def test_new_scene_and_reopen_preserve_names_presets_photos_and_round_history(self):
        before=self.seed_previous_capture();service=self.service(Backend(self.manifest))
        service.start(self.store.snapshot()['revision'],review_first=True);service.thread.join(4)
        self.assertEqual(service.status()['job']['status'],'awaiting_review')
        self.assertEqual(len(self.store.snapshot()['maps']),2)
        self.assert_previous_capture_preserved(before)
        self.store=Store(self.root/'database')
        self.assert_previous_capture_preserved(before)
        self.assertFalse(self.store.snapshot()['schedule']['enabled'])

    def test_failed_new_scene_keeps_previous_capture_available(self):
        before=self.seed_previous_capture();backend=Backend(self.manifest)
        def fail(*unused):raise RuntimeError('fixture_scan_failed')
        backend.run=fail;service=self.start(backend)
        self.assertEqual(service.status()['job']['status'],'failed')
        self.assert_previous_capture_preserved(before)

    def test_archive_capacity_rejects_new_scene_without_eviction(self):
        before=self.seed_previous_capture()
        for i in range(19):self.store.import_manifest(dict(title='archive %d'%i,frames=self.manifest['frames']))
        full=self.store.snapshot(include_history=True);service=self.service(Backend(self.manifest))
        with self.assertRaises(ValueError):service.start(full['revision'])
        self.assertEqual(self.store.snapshot(include_history=True),full)
        self.assert_previous_capture_preserved(before)

    def test_stop_after_queue_resume_does_not_reenable_commissioning(self):
        service=self.service(Backend(self.manifest))
        resume=self.annotations.resume
        def stopped_resume(revision):
            result=resume(revision)
            service.stop()
            return result
        self.annotations.resume=stopped_resume
        service.start(self.store.snapshot()['revision']);service.thread.join(4)
        self.assertFalse(service.thread.is_alive())
        self.assertTrue(service.cancel.is_set())
        self.assertEqual(service.status()['job']['status'],'stopped')
        self.assertTrue(self.annotations.status()['paused'])
        self.assertEqual(self.annotations.status()['queued'],0)
        self.annotations.tick()
        self.assertIsNone(self.annotations.thread)

    def test_stop_after_proposals_are_queued_preserves_pause_and_never_starts_motion(self):
        service=self.service(Backend(self.manifest))
        discover=self.annotations.discover
        def stopped_discover(*args):
            result=discover(*args)
            service.stop()
            return result
        self.annotations.discover=stopped_discover
        service.start(self.store.snapshot()['revision']);service.thread.join(4)
        self.assertFalse(service.thread.is_alive())
        self.assertEqual(service.status()['job']['status'],'stopped')
        self.assertTrue(self.annotations.status()['paused'])
        self.assertEqual(self.annotations.status()['queued'],1)
        self.annotations.tick()
        self.assertIsNone(self.annotations.thread)

    def test_initialization_cannot_overlap_a_running_job_or_stale_revision(self):
        service=self.service(Backend(self.manifest));revision=self.store.snapshot()['revision']
        with self.assertRaises(Conflict):service.start(revision-1)
        self.store.change(revision,'fixture',{},lambda s:s['jobs'].append(dict(id='other',status='running')))
        with self.assertRaises(Conflict):service.start(self.store.snapshot()['revision'])

    def test_uncertain_candidate_never_marks_scene_ready(self):
        service=self.start()
        def uncertain(s):s['annotation_queue'][-1].update(state='uncertain')
        self.store.change(None,'fixture',{},uncertain);service.tick()
        job=service.status()['job']
        self.assertEqual(job['status'],'completed_with_uncertain')
        self.assertEqual(job['uncertain_candidates'],1)
        self.assertFalse(job['ready_for_patrol'])

    def test_restart_pauses_pending_commissioning_instead_of_replaying_it(self):
        self.start();service=self.service()
        service.recover()
        self.assertEqual(service.status()['job']['status'],'interrupted')
        self.assertTrue(self.annotations.status()['paused'])
        self.assertEqual(self.annotations.status()['queued'],1)

    def test_scan_phase_does_not_consume_annotation_queue_or_hide_status(self):
        self.start()
        self.store.change(None,'fixture',{},lambda s:s['initialization'].update(status='mapping'))
        self.annotations.tick()
        self.assertEqual(self.annotations.status()['queued'],1)
        self.assertIsNone(self.annotations.thread)

    def test_http_defaults_and_global_stop_own_the_initializer(self):
        backend=Backend(self.manifest,True);service=self.service(backend)
        server=Server(('127.0.0.1',0),self.store,annotations=self.annotations,initialization=service)
        worker=threading.Thread(target=server.serve_forever);worker.daemon=True;worker.start()
        base='http://127.0.0.1:'+str(server.server_address[1]);opener=build_opener(ProxyHandler({}))
        def post(path,revision=None):
            payload=dict(revision=self.store.snapshot()['revision'] if revision is None else revision)
            request=Request(base+path,json.dumps(payload).encode(),headers={'Content-Type':'application/json',
                'X-Control-Token':server.token,'Origin':base},method='POST')
            with opener.open(request,timeout=3) as r:return json.loads(r.read().decode())
        try:
            post('/api/initialization/start');self.assertTrue(backend.started.wait(2))
            with self.assertRaises(HTTPError) as error:post('/api/patrol/run-once')
            self.assertEqual(error.exception.code,409)
            post('/api/schedule/stop',0);service.thread.join(4)
            self.assertFalse(service.status()['active'])
        finally:
            service.close();server.shutdown();server.server_close();worker.join(2)


class DiscoveryGeometryTests(unittest.TestCase):
    def test_internal_seam_fragments_are_flagged_and_source_boxes_are_recovered(self):
        result=dict(width=100,height=100,detections=[dict(bbox=[0,.1,.8,.9],confidence=.8)])
        rows=map_hits(result,[100,50,200,150],400,300,'tile')
        self.assertTrue(rows[0]['internal_edge'])
        self.assertEqual(rows[0]['bbox'],[100,60,180,140])
        result['detections'][0]['bbox']=[.1,.1,.8,.9]
        row=map_hits(result,[100,50,200,150],400,300,'tile')[0]
        self.assertFalse(row['internal_edge'])
        self.assertEqual(len(merge([row,dict(row,confidence=.7)])),1)


if __name__=='__main__':unittest.main()

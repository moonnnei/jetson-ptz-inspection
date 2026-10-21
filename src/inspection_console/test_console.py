"""Registry/HTTP/planning/scheduling checks. No camera or network beyond loopback."""
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler
import zlib

from planner import pose, route, scan_grid
from scheduler import Scheduler
from server import Server
from store import Conflict, Store, image_size


def png(width=120, height=80):
    def chunk(name, data):
        return struct.pack('>I', len(data)) + name + data + struct.pack('>I', zlib.crc32(name + data) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)) +
            chunk(b'IDAT', zlib.compress((b'\x00' + b'\x85\x9f\xb8' * width) * height)) + chunk(b'IEND', b''))


class RegistryFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source.png'
        self.source.write_bytes(png())
        self.store = Store(self.root / 'private')
        self.store.import_manifest(dict(title='测试场景', frames=[dict(path=str(self.source),
            title='source', captured_at='2026-10-19T00:00:00+00:00', pose=dict(pan=200, tilt=5, zoom=1.4),
            detections=[dict(bbox=[20,20,50,60], confidence=.8), dict(bbox=[65,20,95,60], confidence=.7)])]))

    def tearDown(self):
        self.tmp.cleanup()

    def state(self):
        return self.store.snapshot()

    def review(self, index, action, **kwargs):
        state = self.state()
        return self.store.review(state['revision'], state['candidates'][index]['id'], action, **kwargs)


class RegistryTests(RegistryFixture):
    def overview_manifest(self):
        return dict(title='registered preview',overview=dict(path=str(self.source),
            registered_views=1,total_views=1,kind='view_index',full_panorama=False,ptz_calibration=False),
            frames=[dict(path=str(self.source),to_overview=[[1,0,0],[0,1,0],[0,0,1]],
                raw_source_path=str(self.source),detections=[dict(bbox=[10,10,30,30],confidence=.5)])])

    def test_overview_preserves_sources_without_creating_visit_pose(self):
        self.store.import_manifest(self.overview_manifest())
        s=self.state();m=s['maps'][-1];f=s['frames'][-1]
        self.assertEqual(self.store.image_path(m['overview']['image']).read_bytes(),png())
        self.assertEqual(f['raw_source']['sha256'],f['sha256'])
        self.assertEqual(f['to_overview'],[[1,0,0],[0,1,0],[0,0,1]])
        self.assertFalse(m['motion_verified'])
        self.assertEqual(s['candidates'][-1]['state'],'pending')
        self.assertEqual(s['instruments'],[])

    def test_overview_rejects_invalid_geometry_transactionally(self):
        before=self.state()
        for matrix in ([[1,0,200],[0,1,0],[0,0,1]], [[1,0,0],[0,0,0],[0,0,1]],
                       [[1,0,0],[0,1,0],[1,0,-60]], [[float('nan'),0,0],[0,1,0],[0,0,1]]):
            manifest=self.overview_manifest();manifest['frames'][0]['to_overview']=matrix
            with self.assertRaises(ValueError):self.store.import_manifest(manifest)
            self.assertEqual(self.state(),before)

    def test_overview_does_not_accept_false_coverage_or_count(self):
        before=self.state()
        for key,value in (('full_panorama',True),('ptz_calibration',True),('registered_views',2)):
            manifest=self.overview_manifest();manifest['overview'][key]=value
            with self.assertRaises(ValueError):self.store.import_manifest(manifest)
        manifest=self.overview_manifest();manifest['frames'][0]['to_overview']=None
        with self.assertRaises(ValueError):self.store.import_manifest(manifest)
        self.assertEqual(self.state(),before)

    def test_ring_preview_keeps_nonlinear_view_separate_from_source_geometry(self):
        manifest=self.overview_manifest()
        manifest['overview'].update(kind='stitched_ring_preview',registered_views=0,
            stitched_views=1,source_mapping='source_picker_only')
        manifest['frames'][0]['to_overview']=None
        self.store.import_manifest(manifest)
        s=self.state();m=s['maps'][-1]
        self.assertEqual(m['overview']['stitched_views'],1)
        self.assertEqual(m['overview']['registered_views'],0)
        self.assertIsNone(s['frames'][-1]['to_overview'])
        self.assertFalse(m['motion_verified'])
        self.assertEqual(self.store.image_path(m['overview']['image']).read_bytes(),png())

    def test_ring_preview_rejects_fake_planar_coordinates_or_stitched_counts(self):
        before=self.state()
        manifest=self.overview_manifest()
        manifest['overview'].update(kind='stitched_ring_preview',registered_views=0,
            stitched_views=1,source_mapping='source_picker_only')
        with self.assertRaises(ValueError):self.store.import_manifest(manifest)
        manifest['frames'][0]['to_overview']=None
        for updates in ({'stitched_views':2},{'registered_views':1},{'source_mapping':'linear'},
                        {'kind':'source_contact_sheet','stitched_views':1}):
            changed=json.loads(json.dumps(manifest));changed['overview'].update(updates)
            with self.assertRaises(ValueError):self.store.import_manifest(changed)
        self.assertEqual(self.state(),before)

    def test_angle_projection_does_not_claim_feature_stitching(self):
        manifest=self.overview_manifest()
        manifest['overview'].update(kind='pose_projected_ring_preview',registered_views=0,
            stitched_views=0,projected_views=1,source_mapping='source_picker_only')
        manifest['frames'][0]['to_overview']=None
        self.store.import_manifest(manifest)
        self.assertEqual(self.state()['maps'][-1]['overview']['projected_views'],1)
        before=self.state()
        manifest['overview']['stitched_views']=1
        with self.assertRaises(ValueError):self.store.import_manifest(manifest)
        self.assertEqual(self.state(),before)

    def test_import_keeps_original_hash_pose_and_pending_state(self):
        s = self.state()
        self.assertEqual(s['frames'][0]['sha256'], hashlib.sha256(png()).hexdigest())
        self.assertEqual(s['frames'][0]['pose']['pan'], 200)
        self.assertEqual(s['candidates'][0]['state'], 'pending')
        self.assertFalse(s['maps'][0]['motion_verified'])
        self.assertEqual(s['jobs'], [])
        self.assertEqual(s['instruments'], [])

    def test_confirm_restore_and_restart_preserve_stable_id(self):
        self.review(0, 'confirm', name='电流表')
        self.review(0, 'reject')
        self.review(0, 'restore')
        self.store = Store(self.root / 'private')
        self.review(0, 'confirm')
        s = self.state()
        self.assertEqual(len(s['instruments']), 1)
        self.assertEqual(s['instruments'][0]['id'], 'M0001')
        self.assertEqual(s['instruments'][0]['name'], '电流表')
        self.assertIsNone(s['instruments'][0]['visit_pose'])
        self.assertIsNone(s['instruments'][0]['reading'])
        self.assertEqual(s['instruments'][0]['pose_status'], 'unverified')

    def test_manual_add_coordinates_and_provenance(self):
        s = self.state()
        self.store.add_candidate(s['revision'], s['frames'][0]['id'], [1,2,22,25])
        item = self.state()['candidates'][-1]
        self.assertEqual(item['state'], 'pending')
        self.assertEqual(item['source'], 'human_added')
        self.assertIsNone(item['confidence'])

    def test_invalid_boxes_leave_database_unchanged(self):
        s = self.state()
        for box in ([0,0,121,80], [50,0,40,5], [0,0,float('nan'),5], [0,0,1,1], [True,0,10,10]):
            with self.assertRaises(ValueError):
                self.store.add_candidate(s['revision'], s['frames'][0]['id'], box)
            self.assertEqual(s, self.state())

    def test_stale_revision_is_rejected(self):
        s = self.state()
        self.review(0, 'confirm')
        with self.assertRaises(Conflict):
            self.store.review(s['revision'], s['candidates'][1]['id'], 'reject')
        self.assertEqual(self.state()['candidates'][1]['state'], 'pending')

    def test_two_database_connections_cannot_silently_overwrite(self):
        second = Store(self.root / 'private')
        old = second.snapshot()
        self.review(0, 'confirm')
        with self.assertRaises(Conflict):
            second.review(old['revision'], old['candidates'][1]['id'], 'confirm')

    def test_merge_preserves_evidence_disables_duplicate_and_blocks_cycle(self):
        self.review(0, 'confirm')
        self.review(1, 'confirm')
        s = self.state()
        self.review(1, 'merge', target_id=s['candidates'][0]['id'])
        s = self.state()
        self.assertFalse(s['instruments'][1]['enabled'])
        self.assertEqual(s['candidates'][1]['state'], 'merged')
        self.assertEqual(len(s['frames']), 1)
        with self.assertRaises(ValueError):
            self.review(0, 'merge', target_id=s['candidates'][1]['id'])
        self.review(1, 'restore')
        self.review(1, 'confirm')
        self.assertEqual(len(self.state()['instruments']), 2)

    def test_rejected_instrument_cannot_be_enabled(self):
        self.review(0, 'confirm')
        self.review(0, 'reject')
        s = self.state()
        with self.assertRaises(ValueError):
            self.store.update_instrument(s['revision'], 'M0001', 'test', True)

    def test_bbox_edit_invalidates_visit_pose(self):
        self.review(0, 'confirm')
        self.store.change(None, 'test_setup', {}, lambda s: s['instruments'][0].update(
            visit_pose=dict(pan=200,tilt=5,zoom=1.4), pose_status='verified'))
        self.review(0, 'bbox', box=[22,22,52,62])
        self.assertIsNone(self.state()['instruments'][0]['visit_pose'])
        self.assertFalse(self.state()['schedule']['enabled'])

    def test_map_invalidation_preserves_history_and_blocks_reuse(self):
        self.review(0, 'confirm')
        s = self.state()
        self.store.invalidate(s['revision'], s['maps'][0]['id'])
        s = self.state()
        self.assertEqual(s['maps'][0]['status'], 'invalidated')
        self.assertFalse(s['instruments'][0]['enabled'])
        self.assertEqual(len(s['frames']), 1)
        with self.assertRaises(ValueError):
            self.review(0, 'confirm')

    def test_source_hash_failure_has_no_database_side_effect(self):
        before = self.state()
        with self.assertRaises(ValueError):
            self.store.import_manifest(dict(title='bad', frames=[dict(path=str(self.source), sha256='0'*64)]))
        self.assertEqual(before, self.state())

    def test_images_are_allowlisted_and_not_arbitrary_paths(self):
        for name in ('../../registry.sqlite3', '/etc/passwd', 'unlisted.png'):
            with self.assertRaises(ValueError):
                self.store.image_path(name)
        self.assertEqual(self.store.image_path(self.state()['frames'][0]['image']).read_bytes(), png())

    def test_restart_marks_inflight_job_interrupted_without_replay(self):
        def seed(s):
            s['schedule'].update(enabled=True, state='running')
            s['jobs'].append(dict(id='job-test', status='running', results=[]))
        self.store.change(None, 'test_setup', {}, seed)
        Scheduler(Store(self.root / 'private'))
        s = self.state()
        self.assertFalse(s['schedule']['enabled'])
        self.assertEqual(s['jobs'][0]['status'], 'interrupted')

    def test_finished_jobs_remain_exportable_after_recent_list_is_trimmed(self):
        self.store.change(None, 'test_finished', {}, lambda s: s['jobs'].append(
            dict(id='job-archived', status='completed', results=[dict(image='evidence.png')])) )
        self.store.change(None, 'test_trim', {}, lambda s: s.update(jobs=[]))
        exported = self.store.snapshot(include_history=True)
        self.assertEqual(exported['jobs'], [])
        self.assertEqual(exported['job_history_count'], 1)
        self.assertEqual(exported['job_history'][0]['results'][0]['image'], 'evidence.png')

    def test_schedule_cannot_be_armed_without_hardware(self):
        self.review(0, 'confirm')
        s = self.state()
        self.store.configure_schedule(s['revision'], s['maps'][0]['id'], 300)
        engine = Scheduler(self.store)
        with self.assertRaises(ValueError):
            engine.arm(self.state()['revision'])
        self.assertFalse(self.state()['schedule']['enabled'])
        self.assertEqual(self.state()['jobs'], [])


class PlanningTests(unittest.TestCase):
    def test_snake_scan_includes_endpoints_and_overlap_budget(self):
        cfg = dict(pan_min=180,pan_max=220,tilt_min=0,tilt_max=20,hfov=40,vfov=25,overlap=.35,zoom=1)
        plan = scan_grid(cfg)
        self.assertEqual(plan['count'], 9)
        self.assertEqual([p['pan'] for p in plan['points'][3:6]], [220,200,180])
        self.assertLessEqual(plan['points'][1]['pan']-plan['points'][0]['pan'], 40*.65)
        self.assertFalse(plan['execution_enabled'])

    def test_invalid_scan_and_unsafe_pose_rejected(self):
        for value in (dict(pan=360,tilt=5,zoom=1),dict(pan=10,tilt=5,zoom=3),dict(pan=True,tilt=5,zoom=1)):
            with self.assertRaises(ValueError):
                pose(value)
        with self.assertRaises(ValueError):
            scan_grid(dict(pan_min=0,pan_max=350,tilt_min=0,tilt_max=90,hfov=1,vfov=1,overlap=.7,zoom=1))

    def test_route_does_not_wrap_mechanical_axis_or_visit_disabled_assets(self):
        assets = [dict(id='M1',name='a',enabled=True,pose_status='verified',visit_pose=dict(pan=1,tilt=5,zoom=1)),
                  dict(id='M2',name='b',enabled=True,pose_status='verified',visit_pose=dict(pan=300,tilt=5,zoom=1)),
                  dict(id='M3',name='c',enabled=False,pose_status='verified',visit_pose=dict(pan=349,tilt=5,zoom=1)),
                  dict(id='M4',name='d',enabled=True,pose_status='unverified',visit_pose=None)]
        result = route(assets, dict(pan=350,tilt=5,zoom=1))
        self.assertEqual([p['id'] for p in result['stops']], ['M2','M1'])
        self.assertEqual(result['blocked'][0]['id'], 'M4')


class FakeAdapter:
    mode = 'simulation_test_only'
    def __init__(self):
        self.visits = []
        self.stop_calls = 0
        self.block = False
        self.entered = threading.Event()
        self.stop_fails = False
    def ready(self):
        return True
    def position(self):
        return dict(pan=200,tilt=5,zoom=1)
    def visit(self, asset, cancel):
        self.visits.append(asset['id'])
        self.entered.set()
        if self.block:
            cancel.wait(3)
            return dict(instrument_id=asset['id'],status='failed')
        return dict(instrument_id=asset['id'],status='captured',capture_id='test-'+str(len(self.visits)),
                    captured_at='2026-10-19T00:00:00+00:00', image='test.png',image_sha256='0'*64)
    def stop(self):
        self.stop_calls += 1
        if self.stop_fails:
            raise RuntimeError('test stop failure')


class SchedulingTests(RegistryFixture):
    def make_engine(self):
        self.review(0, 'confirm')
        self.review(1, 'confirm')
        def setup(s):
            s['maps'][0]['motion_verified'] = True
            for index, asset in enumerate(s['instruments']):
                asset.update(pose_status='verified',visit_pose=dict(pan=200+index*5,tilt=5,zoom=1))
            s['schedule'].update(map_id=s['maps'][0]['id'], interval_seconds=60)
        self.store.change(None,'test_setup',{},setup)
        self.time = 1000
        self.elapsed = 0
        self.adapter = FakeAdapter()
        self.engine = Scheduler(self.store,self.adapter,clock=lambda:self.time,monotonic=lambda:self.elapsed)
        self.engine.arm(self.state()['revision'])

    def test_fixed_delay_runs_without_browser_and_only_once_when_due(self):
        self.make_engine()
        self.engine.tick()
        self.engine.worker.join(2)
        self.assertFalse(self.engine.worker.is_alive())
        s = self.state()
        self.assertEqual(len(self.adapter.visits), 2)
        self.assertEqual(s['schedule']['next_due_at'], 1060)
        self.assertEqual(s['jobs'][0]['status'], 'completed')
        self.time = 1059
        self.engine.tick()
        self.assertEqual(len(self.adapter.visits),2)
        self.time = 1060
        self.engine.tick()
        self.engine.worker.join(2)
        self.assertEqual(len(self.adapter.visits),4)

    def test_stop_cancels_remaining_visits_and_blocks_overlap(self):
        self.make_engine()
        self.adapter.block = True
        self.engine.tick()
        self.assertTrue(self.adapter.entered.wait(1))
        self.engine.tick()
        self.assertEqual(len(self.state()['jobs']),1)
        with self.assertRaises(Conflict):
            self.review(0,'reject')
        self.engine.stop()
        self.engine.worker.join(2)
        self.assertEqual(self.state()['jobs'][0]['status'],'stopped')
        self.assertEqual(len(self.adapter.visits),1)
        self.assertFalse(self.state()['schedule']['enabled'])

    def test_watchdog_and_stop_failure_prevent_new_round(self):
        self.make_engine()
        self.adapter.block = True
        self.engine.tick()
        self.assertTrue(self.adapter.entered.wait(1))
        self.adapter.stop_fails = True
        self.time = 1181
        self.elapsed = 181
        with self.assertRaises(RuntimeError):
            self.engine.tick()
        self.engine.worker.join(2)
        self.assertEqual(self.state()['jobs'][0]['status'],'failed')
        self.assertIsNotNone(self.engine.stop_error)
        with self.assertRaises(ValueError):
            self.engine.arm(self.state()['revision'])

    def test_wall_clock_rollback_does_not_disable_round_watchdog(self):
        self.make_engine()
        self.adapter.block = True
        self.engine.tick()
        self.assertTrue(self.adapter.entered.wait(1))
        self.time = 500
        self.elapsed = 181
        self.engine.tick()
        self.engine.worker.join(2)
        self.assertEqual(self.state()['jobs'][0]['status'],'stopped')
        self.assertFalse(self.state()['schedule']['enabled'])

    def test_pending_stop_cannot_be_rearmed(self):
        self.make_engine()
        entered, release = threading.Event(), threading.Event()
        def stopping():
            entered.set()
            release.wait(2)
        self.adapter.stop = stopping
        thread = threading.Thread(target=self.engine.stop)
        thread.start()
        self.assertTrue(entered.wait(1))
        try:
            with self.assertRaises(ValueError):
                self.engine.arm(self.state()['revision'])
        finally:
            release.set()
            thread.join(2)


class HTTPTests(RegistryFixture):
    def setUp(self):
        RegistryFixture.setUp(self)
        self.server = Server(('127.0.0.1',0), self.store)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()
        self.base = 'http://127.0.0.1:%d' % self.server.server_address[1]
        self.opener = build_opener(ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        RegistryFixture.tearDown(self)

    def post(self, path, body, token=True, origin=None, host=None):
        headers = {'Content-Type':'application/json'}
        if token:
            headers['X-Control-Token'] = self.server.token
        if origin:
            headers['Origin'] = origin
        if host:
            headers['Host'] = host
        return self.opener.open(Request(self.base+path,json.dumps(body).encode(),headers),timeout=3)

    def test_post_requires_token_same_origin_and_integer_revision(self):
        for kwargs,status in (({'token':False},403),({'origin':'http://elsewhere.invalid'},403),({'host':'elsewhere.invalid'},403)):
            with self.assertRaises(HTTPError) as error:
                self.post('/api/schedule/stop',dict(revision=self.state()['revision']),**kwargs)
            self.assertEqual(error.exception.code,status)
            error.exception.close()
        with self.assertRaises(HTTPError) as error:
            self.post('/api/schedule/stop',dict(revision=None))
        self.assertEqual(error.exception.code,400)
        error.exception.close()

    def test_full_image_and_export_are_complete_and_have_no_server_token(self):
        image = self.state()['frames'][0]['image']
        with self.opener.open(self.base+'/images/'+image) as response:
            data = response.read()
            self.assertEqual(len(data),int(response.headers['Content-Length']))
            self.assertEqual(data,png())
        with self.opener.open(self.base+'/api/export') as response:
            data = response.read()
            self.assertNotIn(self.server.token.encode(),data)
            self.assertFalse(json.loads(data.decode())['capabilities']['periodic_capture'])

    def test_malicious_name_is_data_and_stale_http_update_conflicts(self):
        s = self.state()
        body = dict(revision=s['revision'],id=s['candidates'][0]['id'],action='confirm',name='<script>alert(1)</script>')
        self.post('/api/review',body).close()
        with self.assertRaises(HTTPError) as error:
            self.post('/api/review',body)
        self.assertEqual(error.exception.code,409)
        error.exception.close()
        self.assertEqual(self.state()['instruments'][0]['name'],body['name'])

    def test_reference_plan_is_read_only_and_rejects_stale_revision(self):
        self.review(0, 'confirm')
        before = self.state()
        body = dict(map_id=before['maps'][0]['id'], revision=before['revision'])
        with self.post('/api/plan/references', body) as response:
            plan = json.loads(response.read().decode('utf-8'))
        self.assertEqual(plan['targets'][0]['instrument_id'], 'M0001')
        self.assertFalse(plan['execution_enabled'])
        self.assertEqual(self.state(), before)
        self.review(0, 'bbox', box=[22, 22, 50, 60])
        with self.assertRaises(HTTPError) as error:
            self.post('/api/plan/references', body)
        self.assertEqual(error.exception.code, 409)
        error.exception.close()


if __name__ == '__main__':
    unittest.main()

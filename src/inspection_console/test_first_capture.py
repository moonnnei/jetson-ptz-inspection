"""First-capture integration in a new registry with no pre-existing closeup photos."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

from capture_round import round_plan
from first_capture import FirstCaptureBatch, first_capture_plan, validate_first_capture
from first_capture_history import import_first_capture
from first_capture_io import FirstCaptureIO
from first_capture_service import FirstCaptureService
from server import Server
from store import Store
from teaching import Teaching
from test_console import png
from test_teaching import FakeIO


class PhotoIO(FakeIO):
    last_received = 0.
    def __init__(self, plan, raw):
        FakeIO.__init__(self, plan)
        self.raw = raw

    def capture(self, root, index, cancel):
        frame = FakeIO.capture(self, root, index, cancel)
        frame.update(path='frame-%03d.jpg' % index,
                     sha256=hashlib.sha256(self.raw).hexdigest(), orientation='rotate180_from_raw')
        (root / frame['path']).write_bytes(self.raw)
        # Windows Python can expose a coarse monotonic clock. Model separate
        # completed snapshot requests rather than give two frames one tick.
        received = time.monotonic()
        while received <= PhotoIO.last_received:
            time.sleep(.001)
            received = time.monotonic()
        PhotoIO.last_received = frame['received_monotonic'] = received
        return frame

    def evidence(self, frame, root, instrument_id):
        return dict(image=frame['path'], image_sha256=frame['sha256'], reading=None)


class FirstCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.raw = png(2560, 1440)
        source = self.root / 'scan.png'
        source.write_bytes(self.raw)
        self.store = Store(self.root / 'registry')
        self.store.import_manifest(dict(title='New scene without closeups', provenance='bounded_scan_capture',
            frames=[dict(path=str(source), orientation='rotate180_from_raw',
                         pose=dict(pan=233.3, tilt=5.5, zoom=1.),
                         detections=[dict(bbox=[1800,800,2100,1200], confidence=.8),
                                     dict(bbox=[800,800,1100,1200], confidence=.8)])]))
        state = self.store.snapshot()
        self.map_id = state['maps'][0]['id']
        self.store.review_many(state['revision'], [c['id'] for c in state['candidates']], 'confirm')
        self.anchor = dict(pan=202.9, tilt=5.5, zoom=1.)
        self.plan = first_capture_plan(self.store.snapshot(), self.map_id, self.anchor)
        self.ios = []

    def tearDown(self):
        self.tmp.cleanup()

    def factory(self, plan):
        io = PhotoIO(plan, self.raw)
        self.ios.append(io)
        return io

    def run_batch(self, factory=None, controller=Teaching, cancel=None):
        self.folder = self.root / 'new-batch'
        self.batch = FirstCaptureBatch(factory or self.factory, self.store.snapshot,
                                        self.folder, self.plan, cancel, controller)
        return self.batch.run()

    def test_new_scene_needs_no_prior_closeups_and_import_feeds_round_plan(self):
        before = self.store.snapshot()
        self.assertEqual(before['jobs'], [])
        self.assertFalse(round_plan(before, self.map_id, self.anchor)['ready_for_trial'])
        self.assertTrue(self.plan['ready_for_trial'])
        result = self.run_batch()
        self.assertEqual(result['status'], 'completed', result.get('error'))
        self.assertEqual(len(result['targets']), 2)
        imported = import_first_capture(self.store, self.folder, self.plan['revision'])
        self.assertEqual(imported['result']['ready_references'], 2)
        state = self.store.snapshot()
        for key in ('maps', 'frames', 'candidates', 'instruments'):
            self.assertEqual(before[key], state[key])
        prepared = round_plan(state, self.map_id, self.anchor)
        self.assertTrue(prepared['ready_for_trial'])
        self.assertTrue(all(t['reference']['source_kind'] == 'first_capture_batch' for t in prepared['stops']))
        self.assertFalse(state['schedule']['enabled'])
        self.assertTrue(all(a['visit_pose'] is None for a in state['instruments']))
        self.assertTrue(all(self.store.image_path(r['image']).is_file() for r in state['jobs'][-1]['results']))

    def test_tilt_transit_is_segmented_as_well_as_pan(self):
        self.anchor['tilt'] = 55.
        self.plan = first_capture_plan(self.store.snapshot(), self.map_id, self.anchor)
        result = self.run_batch()
        self.assertEqual(result['status'], 'completed', result.get('error'))
        for io in self.ios:
            previous = self.anchor
            for point in io.moves:
                self.assertLessEqual(max(abs(point[k]-previous[k]) for k in ('pan', 'tilt')), 20.11)
                previous = point

    def test_wrong_source_profile_blocks_the_batch_before_any_motion(self):
        state = self.store.snapshot()
        for field, value in (('orientation', 'as_imported'), ('pose', None), ('width', 5120),
                             ('pose', dict(pan=200., tilt=5., zoom=1.5))):
            altered = copy.deepcopy(state)
            altered['frames'][0][field] = value
            plan = first_capture_plan(altered, self.map_id, self.anchor)
            self.assertFalse(plan['ready_for_trial'])
            self.assertEqual(len(plan['blocked']), 2)

    def test_changed_review_or_tampered_motion_limits_rejects_old_plan(self):
        changed = copy.deepcopy(self.plan)
        changed['pan_limits'][0] = 0.
        with self.assertRaises(ValueError):
            validate_first_capture(changed, self.store.snapshot())
        asset = self.store.snapshot()['instruments'][0]
        self.store.update_instrument(self.plan['revision'], asset['id'], 'Changed name', True)
        with self.assertRaises(ValueError):
            self.run_batch()
        self.assertEqual(self.ios, [])

    def test_failed_first_target_stops_batch_and_retains_only_diagnostic(self):
        def factory(plan):
            io = self.factory(plan)
            io.match_failure = True
            return io
        result = self.run_batch(factory)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(len(result['targets']), 1)
        self.assertTrue(result['stop_confirmed'])
        imported = import_first_capture(self.store, self.folder, self.plan['revision'])
        self.assertEqual(imported['result']['ready_references'], 0)
        results = self.store.snapshot()['jobs'][-1]['results']
        self.assertEqual(results[0]['image_status'], 'first_capture_failed_diagnostic_not_accepted')
        self.assertEqual(results[1]['status'], 'first_capture_not_attempted')
        self.assertFalse(round_plan(self.store.snapshot(), self.map_id, self.anchor)['ready_for_trial'])

    def test_cancel_after_one_success_preserves_it_without_attempting_next(self):
        cancel = threading.Event()
        class CancelAfterSuccess(Teaching):
            def run(inner):
                result = Teaching.run(inner)
                cancel.set()
                return result
        result = self.run_batch(controller=CancelAfterSuccess, cancel=cancel)
        self.assertEqual(result['status'], 'stopped')
        self.assertEqual(len(result['targets']), 1)
        imported = import_first_capture(self.store, self.folder, self.plan['revision'])
        self.assertEqual(imported['result']['ready_references'], 1)
        self.assertFalse(round_plan(self.store.snapshot(), self.map_id, self.anchor)['ready_for_trial'])

    def test_unconfirmed_stop_is_never_a_successful_batch(self):
        def factory(plan):
            io = self.factory(plan)
            io.stop_failure = True
            return io
        result = self.run_batch(factory)
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(result['stop_confirmed'])
        imported = import_first_capture(self.store, self.folder, self.plan['revision'])
        self.assertEqual(imported['result']['ready_references'], 0)

    def test_changed_child_hash_and_false_completed_claim_are_rejected(self):
        self.run_batch()
        before = self.store.snapshot()
        child = self.folder / self.plan['stops'][0]['id'] / 'record.json'
        original = child.read_bytes()
        child.write_bytes(original + b' ')
        with self.assertRaises(ValueError):
            import_first_capture(self.store, self.folder, self.plan['revision'])
        self.assertEqual(before, self.store.snapshot())
        child.write_bytes(original)
        batch = json.loads((self.folder/'batch.json').read_text(encoding='utf-8'))
        batch['targets'].pop()
        (self.folder/'batch.json').write_text(json.dumps(batch))
        with self.assertRaises(ValueError):
            import_first_capture(self.store, self.folder, self.plan['revision'])
        self.assertEqual(before, self.store.snapshot())

    def test_stale_or_offcenter_reference_cannot_be_imported_as_ready(self):
        self.run_batch()
        before = self.store.snapshot()
        batch_path = self.folder/'batch.json'
        saved_batch = json.loads(batch_path.read_text(encoding='utf-8'))
        item = saved_batch['targets'][0]
        child = self.folder/item['directory']/'record.json'
        saved_child = json.loads(child.read_text(encoding='utf-8'))
        for mode in ('stale', 'offcenter', 'wrong_orientation'):
            data = copy.deepcopy(saved_child)
            fresh = [f for f in data['captures'] if f['purpose'].startswith('independent_revisit_')]
            if mode == 'stale':
                fresh[-1]['received_monotonic'] = fresh[0]['received_monotonic']
            elif mode == 'offcenter':
                fresh[-1]['bbox'] = [100,100,300,300]
            else:
                fresh[-1]['orientation'] = 'as_imported'
            child.write_text(json.dumps(data))
            altered = copy.deepcopy(saved_batch)
            altered['targets'][0]['record_sha256'] = hashlib.sha256(child.read_bytes()).hexdigest()
            batch_path.write_text(json.dumps(altered))
            with self.assertRaises(ValueError):
                import_first_capture(self.store, self.folder, self.plan['revision'])
            self.assertEqual(before, self.store.snapshot())

    def test_web_default_can_plan_but_cannot_start_hardware(self):
        server = Server(('127.0.0.1', 0), self.store)
        worker = threading.Thread(target=server.serve_forever)
        worker.daemon = True
        worker.start()
        origin = 'http://127.0.0.1:%d' % server.server_port
        op = build_opener(ProxyHandler({}))
        def post(path, body):
            request = Request(origin+path, json.dumps(body).encode(),
                              {'Content-Type':'application/json', 'X-Control-Token':server.token})
            with op.open(request, timeout=3) as response:
                return json.loads(response.read().decode())
        before = self.store.snapshot()
        try:
            prepared = post('/api/plan/first-capture', dict(map_id=self.map_id,
                revision=before['revision'], start=self.anchor,mode='strict'))
            self.assertEqual(prepared, self.plan)
            with self.assertRaises(HTTPError) as caught:
                post('/api/first-capture/start', dict(map_id=self.map_id, revision=before['revision'],
                    fingerprint=self.plan['fingerprint'], site_ready=True))
            self.assertEqual(caught.exception.code, 400)
            with op.open(origin+'/api/first-capture/status') as response:
                self.assertFalse(json.loads(response.read().decode())['available'])
            self.assertEqual(before, self.store.snapshot())
        finally:
            server.shutdown()
            worker.join(timeout=2)
            server.server_close()

    def test_execution_service_requires_matching_plan_and_current_site_readiness(self):
        service = FirstCaptureService(self.store, camera=object())
        service.prepared = copy.deepcopy(self.plan)
        for map_id, revision, fingerprint, ready in (
                (self.map_id, self.plan['revision'], self.plan['fingerprint'], False),
                ('wrong-map', self.plan['revision'], self.plan['fingerprint'], True),
                (self.map_id, self.plan['revision']+1, self.plan['fingerprint'], True),
                (self.map_id, self.plan['revision'], 'wrong-fingerprint', True)):
            with self.assertRaises(ValueError):
                service.start(map_id, revision, fingerprint, ready)
        self.assertIsNone(service.thread)
        service._run = lambda plan: None  # No camera IO in this guard test.
        service.start(self.map_id, self.plan['revision'], self.plan['fingerprint'], True)
        service.thread.join(timeout=2)
        service.prepared = copy.deepcopy(self.plan)
        with self.assertRaises(ValueError):
            service.start(self.map_id, self.plan['revision'], self.plan['fingerprint'], True)

    def test_new_attempt_has_a_distinct_binding_even_at_identical_camera_pose(self):
        one = first_capture_plan(self.store.snapshot(), self.map_id, self.anchor, 'attempt-'+'1'*16)
        two = first_capture_plan(self.store.snapshot(), self.map_id, self.anchor, 'attempt-'+'2'*16)
        self.assertNotEqual(one['fingerprint'], two['fingerprint'])
        validate_first_capture(one, self.store.snapshot())
        with self.assertRaises(ValueError):
            first_capture_plan(self.store.snapshot(), self.map_id, self.anchor, '../not-an-attempt')

    def test_independent_revisit_io_uses_pose_bound_matcher_and_checks_orientation(self):
        io = object.__new__(FirstCaptureIO)
        io.capture_root = Path('captures')
        io.source_path = lambda reference: Path('reference.jpg')
        io.vision = lambda request: request
        reference = dict(bbox=[20,20,100,100], orientation='rotate180_from_raw', source_pose=self.anchor)
        frame = dict(path='frame-001.jpg', purpose='independent_revisit_1',
                     orientation='rotate180_from_raw', pose=self.anchor)
        request = io.locate(reference, frame)
        self.assertEqual(request['action'], 'locate_saved_pose')
        self.assertEqual(request['source_pose'], self.anchor)
        frame['orientation'] = 'as_imported'
        with self.assertRaises(ValueError):
            io.locate(reference, frame)
        frame['purpose'] = 'root_reference_check'
        self.assertEqual(io.locate(reference, frame)['action'], 'locate')

    def test_review_change_during_blocked_image_check_requests_stop(self):
        entered, released = threading.Event(), threading.Event()
        class BlockedIO(PhotoIO):
            def locate(inner, ref, frame):
                entered.set()
                if not released.wait(3):
                    raise RuntimeError('test image worker timed out')
                return PhotoIO.locate(inner, ref, frame)
            def stop(inner):
                released.set()
                return PhotoIO.stop(inner)
        def factory(plan):
            return BlockedIO(plan, self.raw)
        holder = []
        worker = threading.Thread(target=lambda: holder.append(self.run_batch(factory)))
        worker.start()
        self.assertTrue(entered.wait(2))
        asset = self.store.snapshot()['instruments'][0]
        self.store.update_instrument(self.plan['revision'], asset['id'], 'Changed during motion', True)
        worker.join(timeout=4)
        self.assertFalse(worker.is_alive())
        self.assertTrue(released.is_set())
        self.assertNotEqual(holder[0]['status'], 'completed')
        self.assertEqual(len(holder[0]['targets']), 1)


if __name__ == '__main__':
    unittest.main()

"""Transactional panorama-review import; no new photos or physical poses."""
import copy
import json
import threading
import unittest
from urllib.request import build_opener,ProxyHandler

import test_console
from ring_coordinates import decorate_state,source_box
from store import Conflict
from server import Server


class PanoramaRegistryTests(test_console.RegistryFixture):
    def setUp(self):
        test_console.RegistryFixture.setUp(self)
        overview=self.root/'overview.png';overview.write_bytes(test_console.png(1024,256))
        self.store.import_manifest(dict(title='ring',provenance='bounded_scan_capture',
            overview=dict(path=str(overview),kind='pose_projected_ring_preview',registered_views=0,
                total_views=1,stitched_views=0,projected_views=1,source_mapping='source_picker_only',
                full_panorama=False,ptz_calibration=False),frames=[dict(path=str(self.source),
                orientation='rotate180_from_raw',pose=dict(pan=200,tilt=5.5,zoom=1),
                detections=[dict(bbox=[20,20,50,60],confidence=.8)])]))
        self.p=dict(version=1,projection='angle_assisted_cylindrical',width=1024,height=256,
            source_width=120,source_height=80,focal_pixels=72,yaw_image_sign=1,
            source_orientation='rotate180_from_raw',calibration_validated=False)
        state=self.state();frame=state['frames'][-1];scene=state['maps'][-1]
        self.candidate_id=state['candidates'][-1]['id']
        self.payload=dict(map_id=scene['id'],registry_revision=state['revision'],
            overview_sha256=scene['overview']['sha256'],projection=self.p,
            source_binding=[{k:frame[k] for k in ('sha256','width','height','orientation','pose')}],
            observations=[dict(frame_sha256=frame['sha256'],bbox=[20,20,50,60],
                panorama_bbox=source_box([20,20,50,60],200,self.p),confidence=.6,
                panorama_confidence=.6,source_confidence=None,source_recheck='unverified',observation_id='c'*24)],
            model_sha256='b'*64,operation_id='a'*24)

    def apply(self):return self.store.import_panorama_candidates(self.state()['revision'],self.payload)

    def test_link_preserves_confirmed_id_box_and_unverified_visit(self):
        self.store.review(self.state()['revision'],self.candidate_id,'confirm',name='manual name')
        before=self.state();result=self.apply();after=self.state()
        self.assertEqual(result['result']['linked'],[self.candidate_id])
        self.assertEqual(result['result']['added'],[])
        self.assertEqual(after['instruments'],before['instruments'])
        self.assertIsNone(after['instruments'][0]['visit_pose'])
        self.assertFalse(after['maps'][-1]['motion_verified'])
        self.assertEqual(after['candidates'][-1]['bbox'],before['candidates'][-1]['bbox'])

    def test_new_observation_is_pending_and_overlays_are_transient(self):
        observation=self.payload['observations'][0]
        observation['bbox']=[65,20,95,60]
        observation['panorama_bbox']=source_box(observation['bbox'],200,self.p)
        result=self.apply();raw=self.state();decorated=decorate_state(copy.deepcopy(raw))
        self.assertEqual(len(result['result']['added']),1)
        self.assertEqual(raw['candidates'][-1]['state'],'pending')
        self.assertIsNone(raw['candidates'][-1]['instrument_id'])
        self.assertEqual(decorated['candidates'][-1]['overview_geometry'],'panorama_detector_region')
        self.assertTrue(decorated['candidates'][-1]['overview_boxes'])
        self.assertNotIn('overview_boxes',self.state()['candidates'][-1])
        self.assertEqual(self.state(),raw)

    def test_import_does_not_restore_rejected_candidate(self):
        self.store.review(self.state()['revision'],self.candidate_id,'reject')
        self.apply()
        self.assertEqual(self.state()['candidates'][-1]['state'],'rejected')
        self.assertEqual(self.state()['instruments'],[])

    def test_stale_or_duplicate_import_does_not_duplicate_observations(self):
        revision=self.state()['revision'];self.apply();before=self.state()
        with self.assertRaises(Conflict):self.store.import_panorama_candidates(revision,self.payload)
        with self.assertRaises(ValueError):self.apply()
        self.assertEqual(self.state(),before)

    def test_changed_sources_or_bad_regions_roll_back_atomically(self):
        before=self.state()
        for field,value in (('overview_sha256','0'*64),('source_binding',[])):
            payload=copy.deepcopy(self.payload);payload[field]=value
            with self.assertRaises(ValueError):self.store.import_panorama_candidates(before['revision'],payload)
            self.assertEqual(self.state(),before)
        payload=copy.deepcopy(self.payload);payload['observations'][0]['panorama_bbox']=[0,0,9999,20]
        with self.assertRaises(ValueError):self.store.import_panorama_candidates(before['revision'],payload)
        self.assertEqual(self.state(),before)

    def test_human_bbox_edit_preserves_provenance_but_updates_overlay(self):
        self.apply()
        self.store.review(self.state()['revision'],self.candidate_id,'bbox',box=[24,24,54,64])
        raw=self.state();decorated=decorate_state(copy.deepcopy(raw))
        self.assertEqual(len(raw['candidates'][-1]['panorama_observations']),1)
        self.assertEqual(decorated['candidates'][-1]['overview_geometry'],'estimated_from_source_region')

    def test_failed_appearance_cannot_be_imported_as_a_pass(self):
        observation=self.payload['observations'][0]
        observation.update(source_association='appearance_consistent',source_similarity=.2)
        before=self.state()
        with self.assertRaises(ValueError):self.apply()
        self.assertEqual(self.state(),before)
        observation['source_similarity']=.95;self.apply()
        evidence=self.state()['candidates'][-1]['panorama_observations'][-1]
        self.assertEqual(evidence['source_association'],'appearance_consistent')

    def test_http_state_has_review_boxes_export_remains_raw(self):
        self.apply();before=self.state();server=Server(('127.0.0.1',0),self.store)
        thread=threading.Thread(target=server.serve_forever);thread.daemon=True;thread.start()
        opener=build_opener(ProxyHandler({}));base='http://127.0.0.1:%d'%server.server_address[1]
        try:
            with opener.open(base+'/api/state',timeout=3) as response:state=json.loads(response.read().decode())
            with opener.open(base+'/api/export',timeout=3) as response:export=json.loads(response.read().decode())
            self.assertTrue(state['candidates'][-1]['overview_boxes'])
            self.assertNotIn('overview_boxes',export['candidates'][-1])
            self.assertFalse(state['capabilities']['periodic_capture'])
            self.assertFalse(state['schedule']['enabled'])
            self.assertEqual(before,self.state())
        finally:
            server.shutdown();server.server_close();thread.join(2)


if __name__=='__main__':unittest.main()

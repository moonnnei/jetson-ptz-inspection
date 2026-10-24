"""Large-canvas review contracts; loopback HTTP only, no physical hardware."""
import copy
import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

from overview_review import source_proposal
from ring_coordinates import decorate_state
from server import Server
from store import Conflict, Store
from test_console import RegistryFixture
import test_panorama_registry


class OverviewFixture(RegistryFixture):
    def setUp(self):
        test_panorama_registry.PanoramaRegistryTests.setUp(self)
        self.store.import_panorama_candidates(self.state()['revision'], self.payload)
        self.scene_id=self.payload['map_id']
        self.sha=self.payload['overview_sha256']
        self.box=self.payload['observations'][0]['panorama_bbox']

    def proposal(self):return source_proposal(self.state(),self.scene_id,self.sha,self.box)

    def save(self, **changes):
        proposal=self.proposal();choice=proposal['choices'][0]
        args=dict(revision=self.state()['revision'],map_id=self.scene_id,
                  overview_sha256=self.sha,overview_box=self.box,
                  frame_id=choice['frame_id'],source_box=choice['bbox'],source_reviewed=True)
        args.update(changes)
        return self.store.add_overview_candidate(**args)


class OverviewReviewTests(OverviewFixture):
    def test_source_proposal_is_read_only_and_keeps_original_pixels(self):
        before=self.state();p=self.proposal()
        self.assertEqual(self.state(),before)
        self.assertEqual(p['choices'][0]['frame_id'],before['frames'][-1]['id'])
        self.assertLess(abs(p['choices'][0]['bbox'][0]-20),1)
        self.assertTrue(p['source_review_required'])
        self.assertFalse(p['motion_sent'])
        self.assertNotIn('visit_pose',p['choices'][0])

    def test_bad_or_changed_overview_never_creates_a_candidate(self):
        before=self.state()
        for sha,box in (('0'*64,self.box),(self.sha,[0,0,9999,10]),(self.sha,[0,0,1,1])):
            with self.assertRaises(ValueError):source_proposal(before,self.scene_id,sha,box)
        self.assertEqual(self.state(),before)

    def test_manual_overview_add_preserves_registry_and_remains_pending(self):
        before=self.state();result=self.save();after=self.state()
        for key in ('maps','frames','instruments','jobs'):
            self.assertEqual(after[key],before[key])
        self.assertEqual(len(after['candidates']),len(before['candidates'])+1)
        item=after['candidates'][-1]
        self.assertEqual(item['id'],result['result'])
        self.assertEqual(item['state'],'pending')
        self.assertIsNone(item['instrument_id'])
        self.assertIsNone(item['confidence'])
        self.assertEqual(item['source'],'human_panorama_added')
        self.assertFalse(after['schedule']['enabled'])
        self.assertEqual(Store(self.store.root).snapshot(),after)

    def test_source_review_is_explicit_and_foreign_source_is_rejected(self):
        before=self.state()
        for value in (False,None,'true',1):
            with self.assertRaises(ValueError):self.save(source_reviewed=value)
        with self.assertRaises(ValueError):self.save(frame_id=before['frames'][0]['id'])
        self.assertEqual(self.state(),before)

    def test_manual_source_refinement_is_stored_separately_from_estimate(self):
        self.save(source_box=[22,22,53,63])
        raw=self.state();item=raw['candidates'][-1]
        self.assertEqual(item['bbox'],[22.,22.,53.,63.])
        self.assertNotEqual(item['bbox'],item['manual_overview']['proposed_source_bbox'])
        decorated=decorate_state(copy.deepcopy(raw))
        self.assertEqual(decorated['candidates'][-1]['overview_geometry'],'human_overview_region')
        self.assertNotIn('overview_boxes',raw['candidates'][-1])
        self.store.review(raw['revision'],item['id'],'bbox',box=[25,25,55,65])
        self.assertEqual(decorate_state(self.state())['candidates'][-1]['overview_geometry'],'estimated_from_source_region')

    def test_stale_add_is_transactionally_rejected(self):
        revision=self.state()['revision'];self.save();before=self.state()
        with self.assertRaises(Conflict):self.save(revision=revision)
        self.assertEqual(self.state(),before)

    def test_batch_confirm_one_revision_and_stable_instrument_numbers(self):
        before=self.state();ids=[c['id'] for c in before['candidates'][:2]]
        self.store.review_many(before['revision'],ids,'confirm');after=self.state()
        self.assertEqual(after['revision'],before['revision']+1)
        self.assertEqual(len(after['instruments']),2)
        self.assertTrue(all(a['visit_pose'] is None and a['pose_status']=='unverified' for a in after['instruments']))
        self.store.review_many(after['revision'],ids,'confirm')
        self.assertEqual(self.state()['instruments'],after['instruments'])
        self.store.review_many(self.state()['revision'],ids,'reject')
        self.assertTrue(all(not a['enabled'] for a in self.state()['instruments']))

    def test_batch_failure_rolls_back_prior_confirmations(self):
        ids=[c['id'] for c in self.state()['candidates'][:2]]
        self.store.review(self.state()['revision'],ids[1],'merge',target_id=ids[0]);before=self.state()
        with self.assertRaises(ValueError):self.store.review_many(before['revision'],ids,'confirm')
        self.assertEqual(self.state(),before)

    def test_duplicate_cross_scene_or_excessive_batch_is_rejected(self):
        before=self.state();ids=[c['id'] for c in before['candidates']]
        for batch in ([],[ids[0],ids[0]],[ids[0],ids[-1]],['missing'],['a']*101):
            with self.assertRaises(ValueError):self.store.review_many(before['revision'],batch,'confirm')
        self.assertEqual(self.state(),before)


class SavedGeometryTests(RegistryFixture):
    def setUp(self):test_panorama_registry.PanoramaRegistryTests.setUp(self)

    def test_existing_preview_binding_preserves_all_review_choices(self):
        before=self.state()
        self.store.bind_overview_geometry(before['revision'],self.payload['map_id'],
            self.payload['overview_sha256'],self.p,self.payload['source_binding'])
        after=self.state()
        for key in ('frames','candidates','instruments','jobs'):
            self.assertEqual(after[key],before[key])
        self.assertEqual(after['maps'][-1]['overview']['ring_projection'],self.p)
        self.assertFalse(after['maps'][-1]['motion_verified'])
        self.assertFalse(after['schedule']['enabled'])

    def test_changed_sources_or_preview_hash_cannot_be_bound(self):
        before=self.state()
        for sha,binding in (('0'*64,self.payload['source_binding']),
                            (self.payload['overview_sha256'],[])):
            with self.assertRaises(ValueError):
                self.store.bind_overview_geometry(before['revision'],self.payload['map_id'],sha,self.p,binding)
        self.assertEqual(self.state(),before)


class OverviewHTTPTests(OverviewFixture):
    def setUp(self):
        OverviewFixture.setUp(self)
        self.server=Server(('127.0.0.1',0),self.store)
        self.thread=threading.Thread(target=self.server.serve_forever);self.thread.daemon=True;self.thread.start()
        self.base='http://127.0.0.1:%d'%self.server.server_address[1]
        self.opener=build_opener(ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2)
        OverviewFixture.tearDown(self)

    def post(self,path,body,token=True):
        headers={'Content-Type':'application/json'}
        if token:headers['X-Control-Token']=self.server.token
        return self.opener.open(Request(self.base+path,json.dumps(body).encode(),headers),timeout=3)

    def test_new_static_script_and_read_only_proposal(self):
        before=self.state();body=dict(revision=before['revision'],map_id=self.scene_id,overview_sha256=self.sha,bbox=self.box)
        with self.post('/api/overview/sources',body) as response:p=json.loads(response.read().decode())
        self.assertTrue(p['choices'])
        self.assertEqual(self.state(),before)
        with self.opener.open(self.base+'/overview.js') as response:
            self.assertIn(b'overviewView',response.read())
        with self.assertRaises(HTTPError) as error:self.post('/api/overview/sources',body,False)
        self.assertEqual(error.exception.code,403);error.exception.close()

    def test_http_save_and_stale_proposal_do_not_enable_hardware(self):
        p=self.proposal();choice=p['choices'][0]
        body=dict(revision=p['revision'],map_id=self.scene_id,overview_sha256=self.sha,bbox=self.box,
                  frame_id=choice['frame_id'],source_bbox=choice['bbox'],source_reviewed=True)
        with self.post('/api/candidates/from-overview',body) as response:self.assertIn('result',json.loads(response.read().decode()))
        old=dict(revision=p['revision'],map_id=self.scene_id,overview_sha256=self.sha,bbox=self.box)
        with self.assertRaises(HTTPError) as error:self.post('/api/overview/sources',old)
        self.assertEqual(error.exception.code,409);error.exception.close()
        with self.opener.open(self.base+'/api/state') as response:state=json.loads(response.read().decode())
        self.assertEqual(state['runtime']['mode'],'review_only')
        self.assertFalse(state['capabilities']['periodic_capture'])

    def test_http_batch_is_one_transaction(self):
        before=self.state();body=dict(revision=before['revision'],ids=[c['id'] for c in before['candidates'][:2]],action='confirm')
        with self.post('/api/review/many',body) as response:self.assertEqual(json.loads(response.read().decode())['revision'],before['revision']+1)


if __name__=='__main__':unittest.main()

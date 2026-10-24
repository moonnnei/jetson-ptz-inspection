"""Name-only updates, retained image access and non-authoritative progress."""
import copy
import hashlib
import json
from urllib.error import HTTPError

from preset_patrol import publish_progress,result_for_registry
from store import Conflict
from test_console import RegistryFixture,png
import test_console as console_fixtures
from test_capture_round import RoundFixture,FakeRoundIO
from capture_round import CaptureRound


class GalleryStoreTests(RegistryFixture):
    def asset(self):
        key=self.state()['candidates'][0]['id']
        self.store.review(self.state()['revision'],key,'confirm')
        return self.state()['instruments'][0]['id']

    def test_auto_verified_name_only_update_preserves_schedule_pose_and_evidence(self):
        key=self.asset()
        def automatic(s):
            s['candidates'][0]['state']='auto_verified'
            s['instruments'][0].update(pose_status='verified',visit_pose=dict(pan=30.,tilt=0.,zoom=2.))
        self.store.change(None,'test_automatic',{},automatic)
        before=self.state();self.store.rename_instrument(before['revision'],key,'主电流表')
        after=self.state();expected=copy.deepcopy(before['instruments']);expected[0]['name']='主电流表'
        self.assertEqual(after['instruments'],expected)
        for name in ('schedule','candidates','jobs','frames'):self.assertEqual(before[name],after[name])
        self.assertEqual(after['audit'][0]['action'],'instrument_rename')
        with self.assertRaises(Conflict):self.store.rename_instrument(before['revision'],key,'stale')

    def test_name_validation_and_active_round_do_not_mutate_registry(self):
        key=self.asset();before=self.state()
        for name in ('','a'*101,'bad\nname'):
            with self.assertRaises(ValueError):self.store.rename_instrument(before['revision'],key,name)
        self.assertEqual(self.state(),before)
        self.store.change(None,'test_active',{},lambda s:s['jobs'].append(dict(id='run',status='running',results=[])))
        with self.assertRaises(Conflict):self.store.rename_instrument(self.state()['revision'],key,'busy')
        self.store.change(None,'test_waiting',{},lambda s:(s['jobs'].clear(),s['schedule'].update(enabled=True)))
        with self.assertRaises(Conflict):self.store.rename_instrument(self.state()['revision'],key,'armed')

    def capture_result(self,key):
        folder=self.root/'fresh';folder.mkdir();raw=png(100,80);crop=png(20,25)
        (folder/'frame-001.jpg').write_bytes(raw);(folder/'meter-crop.jpg').write_bytes(crop)
        return folder,dict(instrument_id=key,status='captured_reference_checked',image='frame-001.jpg',
            image_sha256=hashlib.sha256(raw).hexdigest(),captured_at='2026-10-24T11:20:00Z',bbox=[10,20,30,45],
            evidence=dict(crop='meter-crop.jpg',crop_sha256=hashlib.sha256(crop).hexdigest()))

    def test_progress_has_its_own_list_and_is_not_accepted_history(self):
        key=self.asset();folder,result=self.capture_result(key)
        self.store.change(None,'test_round',{},lambda s:s['jobs'].append(dict(id='run',map_id=s['maps'][0]['id'],status='running',route=[key],results=[])))
        publish_progress(self.store,folder,'run',result)
        job=self.state()['jobs'][-1];self.assertEqual(job['results'],[])
        self.assertTrue(job['progress_results'][0]['pending_round_completion'])
        self.assertTrue(self.store.image_path(job['progress_results'][0]['crop_image']).is_file())
        self.assertTrue(self.store.image_path(job['progress_results'][0]['image']).is_file())
        self.assertEqual(result['image'],'frame-001.jpg')

    def test_persisted_crop_binding_and_older_round_image_remain_available(self):
        key=self.asset();folder,result=self.capture_result(key);row=result_for_registry(self.store,folder,result)
        self.assertEqual(row['crop_source_sha256'],row['image_sha256'])
        self.store.change(None,'test_completed',{},lambda s:s['jobs'].append(dict(id='old',map_id=s['maps'][0]['id'],status='completed',results=[row])))
        self.store.change(None,'test_trim_recent',{},lambda s:s.update(jobs=[]))
        self.assertTrue(self.store.image_path(row['crop_image']).exists())
        rogue=self.root/'rogue.png';rogue.write_bytes(png(9,8));saved=self.store.save_image(rogue)
        with self.assertRaises(ValueError):self.store.image_path(saved['image'])
        result['evidence']['crop']='../source.png'
        with self.assertRaises(ValueError):result_for_registry(self.store,folder,result)


class GalleryHTTPTests(RegistryFixture):
    setUp=console_fixtures.HTTPTests.setUp
    tearDown=console_fixtures.HTTPTests.tearDown
    post=console_fixtures.HTTPTests.post

    def test_name_endpoint_and_static_assets(self):
        self.store.review(self.state()['revision'],self.state()['candidates'][0]['id'],'confirm')
        key=self.state()['instruments'][0]['id'];body=dict(revision=self.state()['revision'],id=key,name='电压表')
        with self.post('/api/instruments/rename',body) as response:self.assertIn('result',json.loads(response.read().decode()))
        self.assertEqual(self.state()['instruments'][0]['name'],'电压表')
        for token,extra,code in ((False,{},403),(True,{'enabled':False},400)):
            with self.assertRaises(HTTPError) as error:self.post('/api/instruments/rename',dict(body,revision=self.state()['revision'],**extra),token)
            self.assertEqual(error.exception.code,code);error.exception.close()
        for name in ('results_model.js','results_ui.js','results.css'):
            with self.opener.open(self.base+'/'+name) as response:self.assertTrue(response.read())


class GalleryProgressTests(RoundFixture):
    def test_per_target_preview_happens_before_next_target_and_cannot_mutate_record(self):
        observed=[]
        def progress(row):
            observed.append(row['instrument_id']);row['status']='tampered'
        record=CaptureRound(FakeRoundIO(self.plan,self.raw),self.root/'progress-round',self.plan,on_result=progress).run()
        self.assertEqual(record['status'],'completed')
        self.assertEqual(observed,[s['id'] for s in self.plan['stops']])
        self.assertTrue(all(r['status']=='captured_reference_checked' for r in record['results']))

    def test_preview_failure_does_not_change_capture_or_stop_acceptance(self):
        def fail(row):raise OSError('display unavailable')
        record=CaptureRound(FakeRoundIO(self.plan,self.raw),self.root/'display-failure',self.plan,on_result=fail).run()
        self.assertEqual(record['status'],'completed');self.assertTrue(record['stop_confirmed'])
        self.assertTrue(any(e['event']=='result_preview_unavailable' for e in record['events']))

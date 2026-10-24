"""Finite-round planning, soft failures, fresh evidence and import isolation."""
import copy
import hashlib
import json
from pathlib import Path
import threading
import time
import unittest
from urllib.request import Request,build_opener,ProxyHandler

from capture_round import CaptureRound,round_plan,validate_round
from capture_round_history import import_round
from capture_round_io import CaptureRoundIO
from server import Server
from store import Conflict
from test_console import RegistryFixture,png


class RoundFixture(RegistryFixture):
    def setUp(self):
        RegistryFixture.setUp(self)
        state=self.state();self.map_id=state['maps'][0]['id']
        self.store.review_many(state['revision'],[c['id'] for c in state['candidates']],'confirm')
        self.raw=png(2560,1440);photo=self.root/'capture.png';photo.write_bytes(self.raw)
        image=self.store.save_image(photo)
        def prepare(s):
            for i,asset in enumerate(s['instruments']):
                region=[800+i*400,700,1000+i*400,950]
                s['jobs'].append(dict(id='prior-'+asset['id'],map_id=self.map_id,
                    status='partial_capture_completed',stop_confirmed=True,results=[dict(
                        instrument_id=asset['id'],image=image['image'],bbox=region,
                        pose=dict(pan=20.+60*i,tilt=5.,zoom=2.),reading=None,
                        image_status='constrained_capture_not_verified_visit')]))
        self.store.change(self.state()['revision'],'test_fixture',{},prepare)
        self.anchor=dict(pan=100.,tilt=5.,zoom=1.)
        self.plan=round_plan(self.state(),self.map_id,self.anchor)

    def run_round(self,io=None,cancel=None):
        self.io=io or FakeRoundIO(self.plan,self.raw)
        self.folder=self.root/'new-round'
        return CaptureRound(self.io,self.folder,self.plan,cancel).run()


class FakeRoundIO:
    def __init__(self,plan,raw):
        self.plan=plan;self.raw=raw;self.current=dict(plan['anchor'])
        self.moves=[];self.stops=0;self.uncertain=set();self.fail_capture=False
        self.stale=False;self.stop_failure=False;self.hard_stop_failed=False

    def preflight(self,plan):
        if self.current!=plan['anchor']:raise ValueError('camera moved')

    def position(self):return dict(self.current)

    def move(self,target,cancel):
        if cancel.is_set():raise RuntimeError('cancelled')
        for key in ('pan','tilt','zoom'):
            if not self.plan[key+'_limits'][0]<=target[key]<=self.plan[key+'_limits'][1]:raise RuntimeError('outside range')
        if max(abs(target[k]-self.current[k]) for k in ('pan','tilt'))>20.11:raise RuntimeError('step too large')
        self.current=dict(target);self.moves.append(dict(target))

    def capture(self,root,index,cancel):
        if self.fail_capture:raise OSError('snapshot transport failed')
        name='frame-%03d.jpg'%index;(root/name).write_bytes(self.raw)
        return dict(path=name,sha256=hashlib.sha256(self.raw).hexdigest(),width=2560,height=1440,
            pose=self.position(),received_monotonic=time.monotonic()-(100 if self.stale else 0),captured_at='new-request-%d'%index)

    def locate(self,reference,frame):
        if frame['instrument_id'] in self.uncertain:return dict(status='uncertain',reason='appearance_rejected')
        return dict(status='matched_reference',bbox=reference['bbox'])

    def evidence(self,frame,root,instrument_id):return dict(image=frame['path'],metrics={'sharpness_status':'recorded_only'})

    def stop(self):
        self.stops+=1
        if self.stop_failure:self.hard_stop_failed=True;raise OSError('STOP failed')


class CaptureRoundTests(RoundFixture):
    def test_automatic_order_is_separate_from_verified_pose_planner(self):
        self.assertEqual([t['id'] for t in self.plan['stops']],['M0002','M0001'])
        self.assertTrue(self.plan['ready_for_trial'])
        self.assertFalse(self.plan['execution_enabled'])
        self.assertTrue(all(a['visit_pose'] is None for a in self.state()['instruments']))
        self.assertTrue(all(t['pose_basis']=='unverified_previous_capture' for t in self.plan['stops']))

    def test_round_reuses_photos_and_returns_only_once(self):
        result=self.run_round()
        self.assertEqual(result['status'],'completed',result.get('error'))
        self.assertEqual(len(result['captures']),4)
        self.assertTrue(all(r['matching_fresh_observations']==2 for r in result['results']))
        self.assertEqual(self.io.position(),self.anchor)
        phases=[e.get('phase') for e in result['events'] if e['event']=='move']
        first_return=phases.index('return_once_after_round')
        self.assertTrue(all(p=='return_once_after_round' for p in phases[first_return:]))
        self.assertFalse(result['visit_pose_verified']);self.assertFalse(result['periodic_capture_enabled'])

    def test_uncertain_target_is_recorded_and_next_target_is_captured(self):
        io=FakeRoundIO(self.plan,self.raw);io.uncertain.add('M0002')
        result=self.run_round(io)
        self.assertEqual(result['status'],'completed_with_errors')
        self.assertEqual([r['status'] for r in result['results']],['uncertain','captured_reference_checked'])
        self.assertEqual(len(result['captures']),3)
        self.assertEqual(io.position(),self.anchor)

    def test_transport_failure_stops_without_attempting_next_meter_or_return(self):
        io=FakeRoundIO(self.plan,self.raw);io.fail_capture=True
        result=self.run_round(io)
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['results'],[])
        self.assertEqual(io.position()['pan'],80.)
        self.assertGreater(io.stops,0)

    def test_stale_capture_and_camera_change_are_hard_failures(self):
        io=FakeRoundIO(self.plan,self.raw);io.stale=True
        result=self.run_round(io)
        self.assertEqual(result['status'],'failed');self.assertIn('stale_capture',result['error'])
        self.assertEqual(io.position()['pan'],80.)

    def test_cancelled_round_does_not_move(self):
        cancel=threading.Event();cancel.set();result=self.run_round(cancel=cancel)
        self.assertEqual(result['status'],'stopped');self.assertEqual(self.io.moves,[])

    def test_review_watchdog_stops_while_image_processing_is_busy(self):
        io=FakeRoundIO(self.plan,self.raw)
        io.review_changed=lambda:True
        original=io.locate
        def busy(reference,frame):time.sleep(.35);return original(reference,frame)
        io.locate=busy
        result=self.run_round(io)
        self.assertEqual(result['status'],'stopped')
        self.assertEqual(result['results'],[])
        self.assertEqual(io.position()['pan'],80.)
        self.assertGreaterEqual(io.stops,2)

    def test_STOP_fault_is_sticky_even_if_angles_were_reached(self):
        io=FakeRoundIO(self.plan,self.raw);io.stop_failure=True
        result=self.run_round(io)
        self.assertEqual(result['status'],'failed');self.assertFalse(result['stop_confirmed'])

    def test_modified_limits_and_changed_review_invalidate_saved_plan(self):
        edited=copy.deepcopy(self.plan);edited['pan_limits']=[0,350]
        with self.assertRaises(ValueError):validate_round(edited,self.state())
        self.review(0,'reject')
        with self.assertRaises((Conflict,ValueError)):validate_round(self.plan,self.state())

    def test_actual_IO_guard_rejects_review_changes_before_hardware_access(self):
        io=object.__new__(CaptureRoundIO)
        io.round_plan=self.plan;io.state=self.state;io.hard_stop_failed=False;io.interlocks=None
        io.guard(threading.Event())
        self.review(0,'reject')
        with self.assertRaises((ValueError,Conflict)):io.guard(threading.Event())

    def test_fixed_pose_worker_receives_reference_and_observed_pose(self):
        io=object.__new__(CaptureRoundIO)
        io.round_plan=self.plan;io.capture_root=self.root
        io.source_path=lambda unused:self.root/'reference.jpg'
        requests=[]
        io.vision=lambda request:requests.append(request) or dict(status='uncertain')
        reference=self.plan['stops'][0]['reference']
        frame=dict(path='fresh.jpg',pose=dict(reference['source_pose']),
                   orientation='rotate180_from_raw')
        io.locate(reference,frame)
        self.assertEqual(requests[0]['action'],'locate_saved_pose')
        self.assertEqual(requests[0]['source_pose'],reference['source_pose'])
        self.assertEqual(requests[0]['target_pose'],frame['pose'])
        self.assertEqual(self.plan['appearance_strategy'],'saved_pose_roi_v1')
        frame['orientation']='uncommissioned'
        with self.assertRaises(ValueError):io.locate(reference,frame)
        self.assertEqual(len(requests),1)
        frame['orientation']='rotate180_from_raw'
        io.round_plan=dict(self.plan,appearance_strategy='old')
        with self.assertRaises(ValueError):io.locate(reference,frame)

    def test_matching_strategy_is_bound_into_the_plan(self):
        edited=copy.deepcopy(self.plan);edited.pop('appearance_strategy')
        with self.assertRaises(ValueError):validate_round(edited,self.state())

    def test_http_trial_plan_is_read_only_and_does_not_enable_verified_route(self):
        before=self.state();server=Server(('127.0.0.1',0),self.store)
        worker=threading.Thread(target=server.serve_forever);worker.daemon=True;worker.start()
        try:
            base='http://127.0.0.1:%d'%server.server_address[1]
            opener=build_opener(ProxyHandler({}))
            body=dict(map_id=self.map_id,revision=before['revision'],start=self.anchor)
            request=Request(base+'/api/plan/capture-round',json.dumps(body).encode(),
                            {'Content-Type':'application/json','X-Control-Token':server.token})
            with opener.open(request,timeout=3) as response:plan=json.loads(response.read().decode('utf-8'))
            self.assertEqual(plan['fingerprint'],self.plan['fingerprint'])
            self.assertTrue(plan['ready_for_trial']);self.assertFalse(plan['execution_enabled'])
            self.assertEqual(self.state(),before)
        finally:server.shutdown();server.server_close();worker.join(2)

    def test_manual_photo_needs_an_explicit_reference_region(self):
        def manual(s):
            job=s['jobs'][0];job['status']='reviewed_capture';r=job['results'][0]
            r.pop('bbox');r.update(image_status='fixed_angle_photo_accepted_by_human',manual_review={'status':'accepted'})
        self.store.change(self.state()['revision'],'test_manual_photo',{},manual)
        plan=round_plan(self.state(),self.map_id,self.anchor)
        self.assertFalse(plan['ready_for_trial']);self.assertEqual(plan['blocked'][0]['id'],'M0001')
        self.store.bind_accepted_photo_box(self.state()['revision'],'prior-M0001','M0001',[800,700,1000,950])
        plan=round_plan(self.state(),self.map_id,self.anchor)
        self.assertTrue(plan['ready_for_trial'])
        self.assertEqual(plan['stops'][-1]['reference']['selection'],'human_confirmed_photo_crop')
        self.assertIsNone(self.state()['instruments'][0]['visit_pose'])

    def test_import_preserves_selections_and_never_commissions_a_pose(self):
        self.run_round();before=self.state()
        imported=import_round(self.store,self.folder,before['revision']);after=self.state()
        for key in ('maps','frames','candidates','instruments'):self.assertEqual(after[key],before[key])
        job=next(j for j in after['jobs'] if j['id']==imported['result'])
        self.assertEqual(len(job['results']),2)
        self.assertTrue(all(r['image'].endswith('.png') for r in job['results']))
        self.assertFalse(after['schedule']['enabled'])
        with self.assertRaises((Conflict,ValueError)):import_round(self.store,self.folder,before['revision'])

    def test_import_rejects_faked_verification_or_incomplete_fresh_matches(self):
        self.run_round();path=self.folder/'record.json';record=json.loads(path.read_text(encoding='utf-8'))
        record['visit_pose_verified']=True;path.write_text(json.dumps(record),encoding='utf-8')
        with self.assertRaises(ValueError):import_round(self.store,self.folder,self.state()['revision'])
        record['visit_pose_verified']=False;record['results'][0]['matching_fresh_observations']=1
        path.write_text(json.dumps(record),encoding='utf-8')
        with self.assertRaises(ValueError):import_round(self.store,self.folder,self.state()['revision'])


if __name__=='__main__':unittest.main()

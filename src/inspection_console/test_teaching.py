"""No physical camera access: bounded-control and failure-propagation tests."""
import copy
import json
import math
from pathlib import Path
import tempfile
import threading
import time
import unittest

from teaching import Teaching, pilot_plan, validate_plan, centered, correction


def references():
    return dict(map_id='map-test',revision=42,fingerprint='reference-binding',targets=[
        dict(instrument_id='M0007',name='Meter V',references=[dict(
            image='original.png',sha256='1'*64,bbox=[1900,900,2050,1090],
            width=2560,height=1440,source_kind='bounded_scan_capture',
            orientation='rotate180_from_raw',source_pose=dict(pan=233.3,tilt=5.5,zoom=1.))])])


class FakeIO:
    def __init__(self,plan):
        self.current=dict(plan['anchor'])
        self.moves=[]
        self.stops=0
        self.match_failure=False
        self.bad_calibration=False
        self.stale=False
        self.stop_failure=False
        self.required_tilt=13.
        self.tilt_sign=-1.

    def preflight(self,plan):
        self.plan=plan

    def position(self):return dict(self.current)

    def move(self,target,cancel):
        if cancel.is_set():raise RuntimeError('cancelled')
        for key in ('pan','tilt','zoom'):
            low,high=self.plan[key+'_limits']
            if not low<=target[key]<=high:raise RuntimeError('outside_limits')
        if max(abs(target[k]-self.current[k]) for k in ('pan','tilt'))>20.11:
            raise RuntimeError('step_too_big')
        self.current=dict(target)
        self.moves.append(dict(target))

    def capture(self,root,index,cancel):
        z=self.current['zoom']
        x=.5+.012*z*(self.current['pan']-210.)
        y=.5+self.tilt_sign*.02*z*(self.current['tilt']-self.required_tilt)
        return dict(path='fresh-%03d.jpg'%index,sha256=str(index),width=2560,height=1440,
            pose=dict(self.current),received_monotonic=time.monotonic()-(100 if self.stale else 0),
            captured_at='fresh',test_box=[(x-.035*z)*2560,(y-.045*z)*1440,
                                        (x+.035*z)*2560,(y+.045*z)*1440])

    def locate(self,ref,frame):
        return dict(status='uncertain' if self.match_failure else 'matched_reference',
            reason='test_rejection' if self.match_failure else 'test_match',bbox=frame['test_box'])

    def measure(self,before,after):
        return dict(accepted=not self.bad_calibration,displacement=[
            .012*before['pose']['zoom']*(after['pose']['pan']-before['pose']['pan']),
            self.tilt_sign*.02*before['pose']['zoom']*(after['pose']['tilt']-before['pose']['tilt'])])

    def evidence(self,frame,root,instrument_id):return dict(image=frame['path'],reading=None)

    def stop(self):
        self.stops+=1
        if self.stop_failure:raise RuntimeError('transport_failed')


class TeachingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.plan=pilot_plan(references(),dict(pan=202.9,tilt=5.5,zoom=1.4),'M0007')
        self.io=FakeIO(self.plan)

    def tearDown(self):self.temp.cleanup()

    def run_pilot(self):
        return Teaching(self.io,Path(self.temp.name)/'run',self.plan).run()

    def test_teaches_returns_and_physically_revisits_without_enabling_periodic(self):
        result=self.run_pilot()
        self.assertEqual(result['status'],'completed',result.get('error'))
        self.assertTrue(result['revisit_verified'])
        self.assertTrue(result['stop_confirmed'])
        self.assertIsNone(result['reading'])
        self.assertFalse(result['periodic_capture_enabled'])
        self.assertEqual(self.io.position(),self.plan['anchor'])
        checks=[f for f in result['captures'] if f['purpose'].startswith('independent_revisit')]
        self.assertEqual(len(checks),2)
        self.assertTrue(all(f['centered'] for f in checks))
        self.assertNotEqual(checks[0]['path'],checks[1]['path'])
        saved=json.loads((Path(self.temp.name)/'run/record.json').read_text())
        self.assertEqual(saved['status'],'completed')

    def test_appearance_failure_never_attempts_centering_or_automatic_return(self):
        self.io.match_failure=True
        result=self.run_pilot()
        self.assertEqual(result['status'],'failed')
        self.assertIn('target_uncertain',result['error'])
        self.assertEqual(len(self.io.moves),3)  # wide zoom and two transit steps
        self.assertEqual(self.io.current['pan'],233.3)
        self.assertGreater(self.io.stops,0)
        self.assertNotIn('visit_pose',result)

    def test_stale_capture_stops_before_a_match_or_calibration(self):
        self.io.stale=True
        result=self.run_pilot()
        self.assertIn('stale_capture',result['error'])
        self.assertFalse(any(e['event']=='calibration_attempt' for e in result['events']))

    def test_calibration_failure_has_two_finite_attempts_and_no_return(self):
        self.io.bad_calibration=True
        result=self.run_pilot()
        self.assertEqual(result['status'],'failed')
        attempts=[e for e in result['events'] if e['event']=='calibration_attempt']
        self.assertEqual(len(attempts),2)
        self.assertEqual(result['error'],'RuntimeError: calibration_quality_rejected')
        self.assertNotEqual(self.io.position(),self.plan['anchor'])

    def test_stop_failure_cannot_be_reported_as_completed(self):
        self.io.stop_failure=True
        result=self.run_pilot()
        self.assertEqual(result['status'],'failed')
        self.assertFalse(result['stop_confirmed'])

    def test_unknown_selection_and_changed_plan_are_rejected(self):
        with self.assertRaises(ValueError):pilot_plan(references(),self.plan['anchor'],'M0099')
        altered=copy.deepcopy(self.plan)
        altered['budget_seconds']=100000
        with self.assertRaises(ValueError):validate_plan(altered,references())

    def test_unreachable_low_meter_is_reported_without_clamping_or_fake_success(self):
        self.io.required_tilt=-6.
        self.io.tilt_sign=1.
        result=self.run_pilot()
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['reachability']['axis'],'tilt')
        self.assertEqual(result['reachability']['reason'],'target_requires_motion_beyond_camera_range')
        self.assertLess(result['reachability']['estimated_center_pose']['tilt'],0)
        self.assertTrue(all(p['tilt']>=0 for p in self.io.moves))
        self.assertNotIn('visit_pose',result)

    def test_both_axis_signs_and_singular_models_are_handled(self):
        delta=correction([[.02,0],[0,-.03]],[.2,.15],.65,4.)
        self.assertLess(delta[0],0)
        self.assertGreater(delta[1],0)
        self.assertLessEqual(max(abs(v) for v in delta),4.)
        for matrix in ([[.01,.02],[.02,.04]],[[math.nan,0],[0,.03]]):
            with self.assertRaises(ValueError):correction(matrix,[.1,.1],.65,4.)


if __name__=='__main__':unittest.main()

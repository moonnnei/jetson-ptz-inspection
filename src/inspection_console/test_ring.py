"""Ring planning/limits/cancellation checks. No physical camera commands."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest

from ring_capture import ISAPIRingIO, RingCapture, ring_plan, transit, validate_plan
from scan_capture import BoundedCapture
from test_scan import FakeIO


class RingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)/'ring'
        self.io=FakeIO()
        self.plan=ring_plan(self.io.current)
    def tearDown(self):self.tmp.cleanup()

    def test_default_19_views_uses_nearest_endpoint_and_no_wrap(self):
        self.assertEqual(self.plan['expected_frames'],19)
        pans=[p['pan'] for p in self.plan['points']]
        self.assertEqual((pans[0],pans[-1]),(350,0))
        self.assertTrue(all(0<=a-b<=20.1 for a,b in zip(pans,pans[1:])))
        self.assertFalse(self.plan['full_panorama'])
        self.assertFalse(self.plan['execution_enabled'])
        self.assertEqual(ring_plan(dict(pan=50,tilt=0,zoom=1))['points'][0]['pan'],0)

    def test_modified_or_excessive_plan_is_rejected(self):
        changed=copy.deepcopy(self.plan);changed['points'][0]['pan']=360
        with self.assertRaises(ValueError):validate_plan(changed)
        for step in (0,19,31,True,float('nan')):
            with self.assertRaises(ValueError):ring_plan(self.io.current,step)

    def test_success_saves_19_original_views_and_restores_original_zoom(self):
        anchor=dict(self.io.current)
        result=RingCapture(self.io,self.root,self.plan).run()
        self.assertEqual(result['status'],'completed')
        self.assertEqual(len(result['frames']),19)
        self.assertTrue(all(f['pose']['zoom']==1 and f['pose']['tilt']==5.5 for f in result['frames']))
        self.assertEqual(self.io.current,anchor)
        previous=anchor
        for target in self.io.moves:
            self.assertLessEqual(abs(target['pan']-previous['pan']),20.11)
            previous=target
        manifest=json.loads((self.root/'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(len(manifest['frames']),19)
        self.assertFalse(manifest['full_panorama'])
        self.assertFalse(result['panorama_closure_verified'])

    def test_drift_since_preparation_blocks_all_nonzero_actions(self):
        self.io.current['pan']+=1
        r=RingCapture(self.io,self.root,self.plan).run()
        self.assertEqual(r['status'],'failed')
        self.assertEqual(self.io.moves,[])
        self.assertEqual(r['frames'],[])

    def test_capture_failure_does_not_continue_or_return(self):
        self.io.fail_snapshot=True
        r=RingCapture(self.io,self.root,self.plan).run()
        self.assertEqual(r['status'],'failed')
        self.assertEqual(self.io.current['pan'],350)
        self.assertFalse((self.root/'manifest.json').exists())
        self.assertTrue(all(a['phase'] not in ('return_pan','restore_original_zoom') for a in r['actions']))

    def test_cancel_during_approach_does_not_capture_or_return(self):
        cancel=threading.Event();self.io.cancel_on_first=cancel
        r=RingCapture(self.io,self.root,self.plan,cancel).run()
        self.assertEqual(r['status'],'stopped')
        self.assertEqual(len(self.io.moves),1)
        self.assertEqual(r['frames'],[])
        self.assertFalse((self.root/'manifest.json').exists())

    def test_watchdog_stops_stalled_operation(self):
        self.io.block=True
        r=RingCapture(self.io,self.root,self.plan,budget=1).run()
        self.assertEqual(r['status'],'stopped')
        self.assertEqual(len(self.io.moves),1)
        self.assertGreaterEqual(self.io.stops,1)

    def test_final_stop_failure_keeps_evidence_but_blocks_success(self):
        self.io.fail_stop=True
        r=RingCapture(self.io,self.root,self.plan).run()
        self.assertEqual(r['status'],'failed')
        self.assertEqual(len(r['frames']),19)
        self.assertFalse((self.root/'manifest.json').exists())

    def test_old_local_capture_budget_stays_limited(self):
        with self.assertRaises(ValueError):BoundedCapture(self.io,self.root,budget=91)
        with self.assertRaises(ValueError):RingCapture(self.io,self.root,self.plan,budget=361)

    def test_transit_subdivides_long_travel_without_crossing_zero(self):
        a=dict(pan=350,tilt=5.5,zoom=1)
        b=dict(a,pan=0)
        values=transit(a,b,20)
        self.assertEqual(len(values),18)
        self.assertEqual(values[-1],b)
        self.assertTrue(all(0<=p['pan']<=350 for p in values))

    def test_adapter_rejects_unprepared_wrong_tilt_zoom_large_step_and_wrap(self):
        io=ISAPIRingIO(None,self.plan)
        with self.assertRaises(ValueError):io.move(self.plan['anchor'],threading.Event())
        io.accepted=True
        io.position=lambda:dict(self.plan['anchor'])
        for target in (dict(pan=360,tilt=5.5,zoom=1),dict(pan=202.9,tilt=7,zoom=1),
                       dict(pan=202.9,tilt=5.5,zoom=2),dict(pan=0,tilt=5.5,zoom=1)):
            with self.assertRaises(ValueError):io.move(target,threading.Event())

    def test_adapter_cancel_and_unconfirmed_stop_block_next_command(self):
        io=ISAPIRingIO(None,self.plan);io.accepted=True
        event=threading.Event();event.set()
        with self.assertRaises(RuntimeError):io.move(self.plan['anchor'],event)
        event.clear();io.stop_error=True
        with self.assertRaises(RuntimeError):io.move(self.plan['anchor'],event)

    def test_indeterminate_absolute_request_still_sends_stop(self):
        class BrokenCamera:
            def xml(self,*unused):raise RuntimeError('transport_failed_after_send')
        io=ISAPIRingIO(BrokenCamera(),self.plan);io.accepted=True
        io.position=lambda:dict(self.plan['anchor'])
        stops=[];io.stop=lambda:stops.append(True)
        with self.assertRaises(RuntimeError):
            io.move(dict(self.plan['anchor'],zoom=1),threading.Event())
        self.assertEqual(stops,[True])


if __name__=='__main__':unittest.main()

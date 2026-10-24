"""Adaptive framing under hardware limits; synthetic IO is not image acceptance."""
import copy
import json
import threading
import unittest

from adaptive_capture import AdaptiveCapture,adaptive_plan,fit_zoom,inside_frame,validate_adaptive
from first_capture import FirstCaptureBatch,first_capture_plan
from first_capture_history import import_first_capture
from capture_round import round_plan
import test_first_capture as first_capture_tests
from test_teaching import references


PhotoIO = first_capture_tests.PhotoIO


class AdaptiveCaptureTests(unittest.TestCase):
    setUp = first_capture_tests.FirstCaptureTests.setUp
    tearDown = first_capture_tests.FirstCaptureTests.tearDown

    def test_limit_residual_is_recorded_and_variable_zoom_reference_feeds_revisit(self):
        plan = first_capture_plan(self.store.snapshot(),self.map_id,self.anchor,mode='adaptive')
        ios=[]
        def factory(child):
            io=PhotoIO(child,self.raw);io.required_tilt=-16.;io.tilt_sign=1.
            ios.append(io);return io
        folder=self.root/'adaptive-batch'
        batch=FirstCaptureBatch(factory,self.store.snapshot,folder,plan)
        result=batch.run()
        self.assertEqual(result['status'],'completed',result.get('error'))
        imported=import_first_capture(self.store,folder,plan['revision'])
        self.assertEqual(imported['result']['ready_references'],2)
        prepared=round_plan(self.store.snapshot(),self.map_id,self.anchor)
        self.assertTrue(prepared['ready_for_trial'])
        self.assertTrue(all(1.<=s['pose']['zoom']<2. for s in prepared['stops']))
        results=self.store.snapshot()['jobs'][-1]['results']
        self.assertTrue(all(r['center_status']=='reachable_pose_with_residual' for r in results))
        self.assertTrue(all(r['frame_bounds_checked'] and r['visit_pose_verified'] is False for r in results))
        self.assertTrue(all(p['tilt']>=0 for io in ios for p in io.moves))

    def test_fit_zoom_reduces_magnification_for_offcenter_target(self):
        frame=dict(bbox=[400,700,600,950],width=1000,height=1000,pose=dict(zoom=1.))
        self.assertTrue(inside_frame(frame))
        self.assertLess(fit_zoom(frame),2.)
        self.assertGreaterEqual(fit_zoom(frame),1.)

    def test_appearance_loss_at_zoom_rolls_back_once_and_checks_fresh_image(self):
        plan=adaptive_plan(references(),self.anchor,'M0007')
        class RejectZoomIO(PhotoIO):
            def locate(inner,reference,frame):
                if frame['purpose']=='adaptive_post_zoom_check' and frame['pose']['zoom']>=1.5:
                    return dict(status='uncertain',reason='test_zoom_loss')
                return PhotoIO.locate(inner,reference,frame)
        io=RejectZoomIO(plan,self.raw)
        result=AdaptiveCapture(io,self.root/'adaptive-rollback',plan).run()
        self.assertEqual(result['status'],'completed',result.get('error'))
        self.assertEqual(result['visit_pose']['zoom'],1.2)
        limited=[e for e in result['events'] if e['event']=='zoom_limited' and e['reason']=='appearance_rejected_at_larger_zoom']
        self.assertEqual(len(limited),1)
        self.assertTrue(any(f['purpose']=='adaptive_zoom_recovery' for f in result['captures']))

    def test_transport_fault_is_not_treated_as_zoom_fallback(self):
        plan=adaptive_plan(references(),self.anchor,'M0007')
        class FaultIO(PhotoIO):
            def locate(inner,reference,frame):
                if frame['purpose']=='adaptive_post_zoom_check':
                    raise OSError('test transport fault')
                return PhotoIO.locate(inner,reference,frame)
        result=AdaptiveCapture(FaultIO(plan,self.raw),self.root/'adaptive-fault',plan).run()
        self.assertEqual(result['status'],'failed')
        self.assertTrue(result['stop_confirmed'])
        self.assertFalse(any(e.get('phase')=='restore_last_checked_zoom' for e in result['events']))

    def test_frame_edge_risk_is_not_imported_as_a_usable_reference(self):
        plan=adaptive_plan(references(),self.anchor,'M0007')
        io=PhotoIO(plan,self.raw);io.required_tilt=-25.;io.tilt_sign=1.
        result=AdaptiveCapture(io,self.root/'edge-risk',plan).run()
        self.assertEqual(result['status'],'failed')
        self.assertNotIn('visit_pose',result)
        self.assertTrue(result['stop_confirmed'])

    def test_adaptive_limits_and_mode_remain_plan_bound(self):
        plan=adaptive_plan(references(),self.anchor,'M0007')
        changed=copy.deepcopy(plan);changed['frame_margin']=0.
        with self.assertRaises(ValueError):validate_adaptive(changed,references())

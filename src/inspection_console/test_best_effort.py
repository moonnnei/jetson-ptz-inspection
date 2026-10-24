import copy
from pathlib import Path
import tempfile
import unittest

from best_effort import ConstrainedCapture,capture_plan,validate_capture_plan
from test_teaching import FakeIO,references


class ConstrainedTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.plan=capture_plan(references(),dict(pan=225.3,tilt=1.4,zoom=1.),'M0007')
        self.io=FakeIO(self.plan)
        self.io.required_tilt=-6.
        self.io.tilt_sign=1.

    def tearDown(self):self.tmp.cleanup()

    def run_capture(self):return ConstrainedCapture(self.io,Path(self.tmp.name)/'run',self.plan).run()

    def test_saves_off_center_image_without_claiming_pose_verification(self):
        result=self.run_capture()
        self.assertEqual(result['status'],'partial_capture_completed',result.get('error'))
        self.assertEqual(result['center_status'],'off_center_due_to_motion_limits')
        self.assertFalse(result['visit_pose_verified'])
        self.assertFalse(result['revisit_verified'])
        self.assertNotIn('visit_pose',result)
        self.assertFalse(result['periodic_capture_enabled'])
        self.assertTrue(all(m['tilt']>=0 for m in self.io.moves))
        self.assertEqual(self.io.position(),self.plan['anchor'])
        self.assertEqual(result['capture_pose']['zoom'],2.)

    def test_mismatched_current_view_never_uses_old_source_angle_to_move(self):
        self.io.match_failure=True
        result=self.run_capture()
        self.assertEqual(result['status'],'failed')
        self.assertEqual(self.io.moves,[])
        self.assertGreater(self.io.stops,0)

    def test_cancelled_capture_does_not_report_completed(self):
        self.io.stop_failure=True
        self.assertEqual(self.run_capture()['status'],'failed')

    def test_relaxed_quality_still_requires_the_exact_bounded_plan(self):
        bad=copy.deepcopy(self.plan)
        bad['pan_limits']=[0,350]
        with self.assertRaises(ValueError):validate_capture_plan(bad,references())

    def test_explicit_source_approach_checks_identity_before_centering(self):
        self.plan=capture_plan(references(),self.plan['anchor'],'M0007',approach_source=True)
        self.io=FakeIO(self.plan)
        self.io.match_failure=True
        result=self.run_capture()
        self.assertEqual(result['status'],'failed')
        self.assertEqual(self.io.current['pan'],233.3)
        self.assertFalse(any(e['event']=='calibration' for e in result['events']))

    def test_default_capture_plan_remains_distinct_from_source_approach(self):
        self.assertNotIn('approach_source_view',self.plan)
        changed=copy.deepcopy(self.plan)
        changed['approach_source_view']=True
        with self.assertRaises(ValueError):validate_capture_plan(changed,references())


if __name__=='__main__':unittest.main()

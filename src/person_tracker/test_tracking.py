import unittest
from tracking import Target,correction,validate_matrix

def person(x,y=.5):return {'confidence':.8,'bbox':[x-.05,y-.15,x+.05,y+.15]}
class Tests(unittest.TestCase):
    def test_requires_two_frames(self):
        t=Target();self.assertIsNone(t.update([person(.7)],10));self.assertIsNotNone(t.update([person(.71)],10.2))
    def test_no_immediate_switch(self):
        t=Target();t.update([person(.2)],10);t.update([person(.2)],10.2)
        self.assertIsNone(t.update([person(.8)],10.4))
        self.assertIsNone(t.update([person(.8)],13));self.assertIsNotNone(t.update([person(.8)],13.2))
    def test_lost_person_stops(self):
        t=Target();t.update([person(.5)],10);t.update([person(.5)],10.2)
        self.assertIsNone(t.update([],10.4))
    def test_low_score_cannot_acquire_but_can_keep_target(self):
        t=Target();p=person(.6);p['confidence']=.67
        self.assertIsNone(t.update([p],10));self.assertIsNone(t.update([p],10.2))
        t.update([person(.6)],10.4);self.assertIsNotNone(t.update([person(.6)],10.6))
        self.assertIsNotNone(t.update([p],10.8))
        self.assertIsNone(t.update([p],13))
    def test_acquisition_requires_consecutive_frames(self):
        t=Target();t.update([person(.6)],10);t.update([],10.2)
        self.assertIsNone(t.update([person(.6)],10.4))
        self.assertIsNotNone(t.update([person(.6)],10.6))
    def test_center_deadband(self):
        self.assertIsNone(correction(person(.53)['bbox'],[[-.03,0],[0,.03]]))
    def test_direction_and_rotated_mount(self):
        self.assertEqual(correction(person(.8)['bbox'],[[-.03,0],[0,.03]])['speed'],20)
        cmd=correction(person(.8)['bbox'],[[0,.03],[-.03,0]])
        self.assertEqual((cmd['axis'],cmd['speed']),('tilt',-20))
    def test_bad_calibration(self):
        for m in ([[0,0],[0,0]],[[.02,.02],[.02,.02]],[[float('nan'),0],[0,.03]]):
            with self.assertRaises(ValueError):validate_matrix(m)
    def test_default_fixed_step_is_unchanged(self):
        box=person(.8)['bbox'];m=[[-.03,0],[0,.03]]
        command=correction(box,m)
        self.assertEqual(command,correction(box,m,mode='fixed'))
        self.assertEqual(set(command),{'axis','speed','duration','error'})
        self.assertEqual((command['axis'],command['speed'],command['duration']),('pan',20,500))
        self.assertAlmostEqual(command['error'][0],.3)
    def test_adaptive_reduces_steps_near_existing_deadband(self):
        m=[[-.03,0],[0,.03]]
        small=correction(person(.59)['bbox'],m,mode='adaptive')
        middle=correction(person(.60)['bbox'],m,mode='adaptive')
        large=correction(person(.8)['bbox'],m,mode='adaptive')
        self.assertEqual((small['duration'],middle['duration'],large['duration']),(133,267,500))
        self.assertLess(small['duration'],middle['duration'])
        self.assertAlmostEqual(small['step_magnitude'],1/3)
        self.assertAlmostEqual(small['error'][0],.09)
    def test_adaptive_duration_and_speed_are_bounded(self):
        m=[[-.03,0],[0,.03]]
        for x in (.581,.59,.65,.9,.419,.41,.35,.1):
            command=correction(person(x)['bbox'],m,mode='adaptive')
            self.assertGreaterEqual(command['duration'],100)
            self.assertLessEqual(command['duration'],500)
            self.assertEqual(abs(command['speed']),20)
        self.assertEqual(correction(person(.581)['bbox'],m,mode='adaptive')['duration'],100)
    def test_adaptive_respects_both_deadbands(self):
        m=[[-.03,0],[0,.03]]
        for x,y in ((.5,.5),(.579,.599),(.421,.401)):
            self.assertIsNone(correction(person(x,y)['bbox'],m,mode='adaptive'))
        command=correction(person(.5,.601)['bbox'],m,mode='adaptive')
        self.assertEqual((command['axis'],command['speed'],command['duration']),('tilt',-20,100))
    def test_adaptive_direction_after_rotated_calibration(self):
        m=[[0,.03],[-.03,0]]
        right=correction(person(.59)['bbox'],m,mode='adaptive')
        left=correction(person(.41)['bbox'],m,mode='adaptive')
        lower=correction(person(.5,.62)['bbox'],m,mode='adaptive')
        self.assertEqual((right['axis'],right['speed']),('tilt',-20))
        self.assertEqual((left['axis'],left['speed']),('tilt',20))
        self.assertEqual((lower['axis'],lower['speed']),('pan',20))
        self.assertEqual((right['duration'],left['duration'],lower['duration']),(133,133,267))
    def test_adaptive_bad_calibration_cannot_move(self):
        for m in ([[0,0],[0,0]],[[.02,.02],[.02,.02]],[[float('inf'),0],[0,.03]]):
            with self.assertRaises(ValueError):correction(person(.8)['bbox'],m,mode='adaptive')
    def test_invalid_control_mode_is_rejected(self):
        with self.assertRaises(ValueError):correction(person(.8)['bbox'],[[-.03,0],[0,.03]],mode='pid')
if __name__=='__main__':unittest.main()

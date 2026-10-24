"""Basic quality checks and finite retries are separate from semantic identity."""
import unittest
from image_quality import assess
from test_capture_round import RoundFixture,FakeRoundIO


class QualityRulesTests(unittest.TestCase):
    def result(self,**changes):
        q=dict(sharpness=100.,clipped_fraction=.01)
        return assess(q,dict(q,**changes),[500,400,800,700],2560,1440,[0,.2])
    def test_off_center_is_reported_without_claiming_occlusion_or_readability(self):
        result=self.result()
        self.assertTrue(result['accepted'])
        self.assertIn('reachable_off_center',result['warnings'])
        self.assertEqual(result['occlusion_status'],'not_assessed')
        self.assertEqual(result['numeric_readability'],'not_assessed')
    def test_blur_and_saturation_are_rejected_independently(self):
        self.assertIn('blur_worse_than_reference',self.result(sharpness=10.)['reasons'])
        self.assertIn('exposure_clipping',self.result(clipped_fraction=.8)['reasons'])
    def test_tiny_or_edge_crop_is_not_an_accepted_full_target(self):
        q=dict(sharpness=100.,clipped_fraction=.01)
        result=assess(q,q,[0,0,30,30],2560,1440,[0,0])
        self.assertIn('target_too_small',result['reasons'])
        self.assertIn('target_near_image_boundary',result['reasons'])
    def test_invalid_or_detail_free_reference_cannot_produce_quality_pass(self):
        self.assertFalse(self.result(sharpness=0.)['accepted'])
        with self.assertRaises(ValueError):self.result(sharpness=float('nan'))


class QualityRoundTests(RoundFixture):
    def quality_io(self,bad):
        self.plan.update(quality_policy='relative_picture_checks_v1',maximum_quality_retries=1)
        io=FakeRoundIO(self.plan,self.raw)
        count=[0]
        def assess_frame(reference,frame):
            count[0]+=1
            return dict(accepted=count[0] not in bad,reasons=['simulated_blur'] if count[0] in bad else [],
                        current_metrics=dict(sharpness=float(count[0])))
        io.assess=assess_frame
        return io
    def test_one_quality_retry_requires_two_new_passing_frames_and_selects_best(self):
        result=self.run_round(self.quality_io({1}))
        self.assertEqual(result['status'],'completed')
        self.assertEqual(len(result['captures']),5)
        first=result['results'][0]
        self.assertEqual(first['matching_fresh_observations'],2)
        self.assertEqual(first['image'],'frame-003.jpg')
    def test_repeated_blur_is_diagnostic_and_does_not_loop_forever(self):
        result=self.run_round(self.quality_io(set(range(1,10))))
        self.assertEqual(result['status'],'completed_with_errors')
        self.assertEqual(len(result['captures']),6)
        self.assertTrue(all(r['status']=='uncertain' for r in result['results']))
        self.assertTrue(result['stop_confirmed'])
    def test_quality_worker_failure_stops_the_round_instead_of_retrying_other_targets(self):
        io=self.quality_io(set())
        def failed(*unused):raise OSError('image worker unavailable')
        io.assess=failed
        result=self.run_round(io)
        self.assertEqual(result['status'],'failed')
        self.assertEqual(len(result['captures']),1)
        self.assertTrue(result['stop_confirmed'])


if __name__=='__main__':unittest.main()

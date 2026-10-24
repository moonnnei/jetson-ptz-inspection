"""Cross-view hypotheses keep observations and never create formal identities."""
import copy
import unittest
from initialization_proposals import groups
from ring_coordinates import source_box,panorama_box


class ProposalTests(unittest.TestCase):
    def state(self):
        p=dict(version=1,projection='angle_assisted_cylindrical',width=4096,height=609,
            source_width=2560,source_height=1440,focal_pixels=1542.85,yaw_image_sign=1,
            source_orientation='rotate180_from_raw',calibration_validated=False)
        a=[1100,500,1400,800]
        b=panorama_box(source_box(a,200,p),205,p)
        frames=[dict(id='a',map_id='m',pose=dict(pan=200),width=2560,height=1440),
                dict(id='b',map_id='m',pose=dict(pan=205),width=2560,height=1440)]
        candidates=[dict(id='one',map_id='m',frame_id='a',state='pending',bbox=a,confidence=.7),
                    dict(id='two',map_id='m',frame_id='b',state='pending',bbox=b,confidence=.9)]
        return dict(maps=[dict(id='m',overview=dict(ring_projection=p))],frames=frames,candidates=candidates)
    def test_same_region_schedules_one_central_source_and_retains_other_observations(self):
        state=self.state();before=copy.deepcopy(state)
        result=groups(state,'m')
        self.assertEqual(result['observation_count'],2)
        self.assertEqual(result['hypothesis_count'],1)
        self.assertEqual(result['selected'][0]['candidate_id'],'one')
        self.assertEqual(result['selected'][0]['alternatives'],['two'])
        self.assertEqual(state,before)
        self.assertFalse(result['formal_identity_merged'])
    def test_distinct_instruments_stay_separate_and_overflow_is_explicit(self):
        state=self.state()
        state['candidates'].append(dict(id='different',map_id='m',frame_id='a',state='pending',bbox=[500,500,700,800],confidence=.99))
        result=groups(state,'m',limit=1)
        self.assertEqual(result['hypothesis_count'],2)
        self.assertEqual(len(result['deferred']),1)
        self.assertEqual(result['selected'][0]['distinct_views'],2)


if __name__=='__main__':unittest.main()

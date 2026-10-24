import copy,unittest
from review_groups import review_groups,expand_groups
from initialization_proposals import groups
import test_initialization_proposals as fixtures


class ReviewGroupTests(unittest.TestCase):
    def test_decision_does_not_make_duplicate_observation_reappear(self):
        s=fixtures.ProposalTests().state();s['instruments']=[]
        before=review_groups(s,'m');self.assertEqual(len(before),1)
        ids=expand_groups(s,['one']);self.assertEqual(set(ids),{'one','two'})
        s['candidates'][0]['state']='rejected'
        after=review_groups(s,'m');self.assertEqual(len(after),1);self.assertTrue(after[0]['excluded'])
        self.assertEqual(groups(s,'m')['selected'],[])

    def test_uncertain_is_still_visible_and_excludable(self):
        s=fixtures.ProposalTests().state();s['instruments']=[]
        for c in s['candidates']:c['state']='auto_uncertain'
        g=review_groups(s,'m');self.assertEqual(len(g),1);self.assertFalse(g[0]['excluded'])
        self.assertEqual(set(expand_groups(s,['one'])),{'one','two'})

    def test_neighboring_meter_is_not_swallowed_by_repeated_views(self):
        s=fixtures.ProposalTests().state();s['instruments']=[]
        s['candidates'].append(dict(id='third',map_id='m',frame_id='a',state='pending',bbox=[1500,500,1700,800],confidence=.9))
        self.assertEqual(len(review_groups(s,'m')),2)

if __name__=='__main__':unittest.main()

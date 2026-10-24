"""Independent evidence, append-only migration and software-preset provenance."""
import copy
import json
import unittest

from annotation_capture import AnnotatedCapture
from annotation_history import import_annotation_capture
import test_annotation_capture as fixtures


class AnnotationHistoryTests(unittest.TestCase):
    setUp=fixtures.AnnotationCaptureTests.setUp
    tearDown=fixtures.AnnotationCaptureTests.tearDown

    def capture(self):
        folder=self.root/'history-capture'
        result=AnnotatedCapture(self.io,folder,self.plan).run()
        self.assertEqual(result['status'],'completed',result.get('error'))
        return folder,result

    def test_import_commissions_only_the_checked_target_and_preserves_scan_and_history(self):
        before=self.store.snapshot()
        folder,record=self.capture()
        imported=import_annotation_capture(self.store,folder,before['revision'])
        self.assertTrue(imported['result']['preset_commissioned'])
        after=self.store.snapshot()
        for key in ('frames','candidates'):
            self.assertEqual(before[key],after[key])
        asset=next(a for a in after['instruments'] if a['id']==self.plan['target']['instrument_id'])
        self.assertEqual(asset['pose_status'],'verified')
        self.assertEqual(asset['visit_pose'],record['visit_pose'])
        self.assertEqual(asset['commissioning']['method'],'automatic_annotation_then_independent_revisit')
        self.assertFalse(asset['commissioning']['geometry_calibrated'])
        self.assertFalse(after['schedule']['enabled'])
        self.assertFalse(imported['result']['scene_presets_ready']) # second target not yet commissioned
        self.assertEqual(len(after['jobs']),len(before['jobs'])+1)
        self.assertEqual(after['candidates'][0]['state'],'confirmed') # original provenance retained

    def test_record_cannot_be_imported_twice(self):
        folder,unused=self.capture()
        import_annotation_capture(self.store,folder,self.store.snapshot()['revision'])
        with self.assertRaises(ValueError):
            import_annotation_capture(self.store,folder,self.store.snapshot()['revision'])

    def test_missing_independent_departure_never_promotes_preset(self):
        folder,record=self.capture()
        record['independent_departure_pose']=record['visit_pose']
        (folder/'record.json').write_text(json.dumps(record),encoding='utf8')
        with self.assertRaises(ValueError):
            import_annotation_capture(self.store,folder,self.store.snapshot()['revision'])
        self.assertTrue(all(a['pose_status']=='unverified' for a in self.store.snapshot()['instruments']))

    def test_failed_acquisition_is_kept_as_diagnostic_and_does_not_block_other_targets(self):
        self.io.seed_rejected=True
        folder=self.root/'failed-seed'
        result=AnnotatedCapture(self.io,folder,self.plan).run()
        self.assertEqual(result['status'],'failed')
        imported=import_annotation_capture(self.store,folder,self.store.snapshot()['revision'])
        self.assertFalse(imported['result']['preset_commissioned'])
        state=self.store.snapshot()
        self.assertEqual(state['jobs'][-1]['results'][0]['image_status'],'diagnostic_not_commissioned')
        self.assertTrue(all(a['pose_status']=='unverified' for a in state['instruments']))

    def test_changed_fresh_image_prevents_commit(self):
        folder,record=self.capture()
        frame=next(f for f in record['captures'] if f['purpose']=='independent_revisit_2')
        (folder/frame['path']).write_bytes(b'not an image')
        with self.assertRaises(ValueError):
            import_annotation_capture(self.store,folder,self.store.snapshot()['revision'])
        self.assertEqual(self.store.snapshot()['jobs'],[])


if __name__=='__main__':unittest.main()

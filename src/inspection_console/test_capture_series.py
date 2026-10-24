"""Finite repeatability trials, non-replay evidence and atomic history import."""
import copy
import hashlib
import json
import threading
import unittest

from capture_series import CaptureSeries,read_records,series_plan,summarize,validate_series
from capture_series_history import import_series
from test_capture_round import FakeRoundIO,RoundFixture


class SeriesTests(RoundFixture):
    def setUp(self):
        RoundFixture.setUp(self)
        self.series_plan=series_plan(self.state(),self.map_id,self.anchor,2)
        self.created=[];self.cancel=threading.Event()

    def factory(self,plan):
        io=FakeRoundIO(plan,self.raw);self.created.append(io);return io

    def run_series(self,factory=None,state=None):
        self.directory=self.root/'series'
        # No sleep in tests; cancellation.wait is still exercised explicitly.
        class FastPause:
            def is_set(inner):return self.cancel.is_set()
            def set(inner):self.cancel.set()
            def wait(inner,seconds):return self.cancel.is_set()
        return CaptureSeries(factory or self.factory,state or self.state,
            self.directory,self.series_plan,FastPause()).run()

    def test_two_rounds_get_distinct_new_requests_and_timing(self):
        result=self.run_series();self.assertEqual(result['status'],'completed',result.get('error'))
        self.assertEqual(len(self.created),2);self.assertEqual(result['summary']['fresh_capture_requests'],8)
        series,records=read_records(self.directory)
        self.assertEqual(len(records),2)
        self.assertTrue(records[1]['captures'][0]['received_monotonic']>records[0]['captures'][-1]['received_monotonic'])
        self.assertTrue(all(r['elapsed_seconds']>=0 for rec in records for r in rec['results']))
        self.assertTrue(all(io.position()==self.anchor and io.stops>0 for io in self.created))

    def test_incomplete_round_stops_series_even_after_successful_return(self):
        def bad(plan):
            io=self.factory(plan);io.uncertain.add(plan['stops'][0]['id']);return io
        result=self.run_series(bad)
        self.assertEqual(result['status'],'failed');self.assertEqual(len(self.created),1)
        self.assertEqual(result['rounds'][0]['status'],'completed_with_errors')
        self.assertEqual(result['summary']['completed_rounds'],0)
        self.assertEqual(self.created[0].position(),self.anchor)

    def test_transport_and_STOP_failure_prevent_any_later_round(self):
        def broken(plan):
            io=self.factory(plan);io.fail_capture=True;io.stop_failure=True;return io
        result=self.run_series(broken)
        self.assertEqual(result['status'],'failed');self.assertEqual(len(self.created),1)
        self.assertFalse(json.loads((self.directory/'round-01/record.json').read_text(encoding='utf-8'))['stop_confirmed'])

    def test_cancel_before_start_never_constructs_hardware_IO(self):
        self.cancel.set();result=self.run_series()
        self.assertEqual(result['status'],'stopped');self.assertEqual(self.created,[])

    def test_changed_registry_between_rounds_prevents_next_round(self):
        def state():
            snapshot=self.state()
            if self.created:snapshot['revision']+=1
            return snapshot
        result=self.run_series(state=state)
        self.assertEqual(result['status'],'failed');self.assertEqual(len(self.created),1)

    def test_fixed_bias_and_jitter_are_different_statistics(self):
        self.run_series();series,records=read_records(self.directory);summary=summarize(series,records)
        target=summary['targets'][0]
        self.assertEqual(target['successful_visits'],2)
        self.assertNotEqual(target['mean_center_error'],[0,0])
        self.assertEqual(target['center_error_population_std'],[0,0])
        self.assertEqual(target['precisely_centered_visits'],0)
        self.assertFalse(summary['visit_poses_commissioned'])

    def test_round_count_or_limits_cannot_be_expanded_after_preparation(self):
        for count in (0,1,4,True):
            with self.assertRaises(ValueError):series_plan(self.state(),self.map_id,self.anchor,count)
        changed=copy.deepcopy(self.series_plan);changed['round_plan']['pan_limits']=[0,350]
        with self.assertRaises(ValueError):validate_series(changed,self.state())

    def test_import_adds_both_rounds_in_one_revision_and_preserves_registry(self):
        self.run_series();before=self.state()
        imported=import_series(self.store,self.directory,before['revision']);after=self.state()
        self.assertEqual(after['revision'],before['revision']+1)
        for key in ('maps','frames','candidates','instruments'):self.assertEqual(after[key],before[key])
        self.assertEqual(after['jobs'][:-2],before['jobs'])
        self.assertEqual(len(imported['result']['round_ids']),2)
        self.assertTrue(all(j['series_round_index']==i for i,j in enumerate(after['jobs'][-2:],1)))
        self.assertFalse(after['schedule']['enabled'])
        self.assertTrue(all(a['visit_pose'] is None for a in after['instruments']))

    def test_replayed_second_round_is_rejected_without_database_change(self):
        self.run_series();before=self.state()
        first=json.loads((self.directory/'round-01/record.json').read_text(encoding='utf-8'))
        path=self.directory/'round-02/record.json';second=json.loads(path.read_text(encoding='utf-8'))
        for a,b in zip(second['captures'],first['captures']):a['received_monotonic']=b['received_monotonic']
        path.write_text(json.dumps(second),encoding='utf-8')
        manifest=json.loads((self.directory/'series.json').read_text(encoding='utf-8'))
        manifest['rounds'][1]['record_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
        (self.directory/'series.json').write_text(json.dumps(manifest),encoding='utf-8')
        with self.assertRaises(ValueError):import_series(self.store,self.directory,before['revision'])
        self.assertEqual(self.state(),before)

    def test_tampered_round_file_rejected_before_any_registry_write(self):
        self.run_series();before=self.state();path=self.directory/'round-02/record.json'
        path.write_text(path.read_text(encoding='utf-8')+' ',encoding='utf-8')
        with self.assertRaises(ValueError):import_series(self.store,self.directory,before['revision'])
        self.assertEqual(self.state(),before)


if __name__=='__main__':unittest.main()

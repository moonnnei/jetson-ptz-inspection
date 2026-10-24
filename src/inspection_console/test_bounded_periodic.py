"""Finite batch acceptance with deterministic clocks and no camera commands."""
import unittest

from scheduler import Scheduler
from store import Conflict,Store
import test_console as fixtures


class BoundedPeriodicTests(fixtures.RegistryFixture):
    def bounded(self,limit=2):
        fixtures.SchedulingTests.make_engine(self)
        self.engine.stop()
        s=self.state();self.map_id=s['maps'][0]['id']
        self.store.configure_schedule(s['revision'],self.map_id,60,limit)
        self.engine.arm(self.state()['revision'])

    def run_due(self):
        self.engine.tick();self.engine.worker.join(2)
        self.assertFalse(self.engine.worker.is_alive())

    def test_two_round_limit_is_owned_by_scheduler_without_external_stop(self):
        self.bounded(2);self.run_due()
        first=self.state()
        self.assertTrue(first['schedule']['enabled'])
        self.assertEqual(first['schedule']['completed_rounds'],1)
        self.assertEqual(first['schedule']['next_due_at'],1060)
        self.time=1060;self.run_due()
        s=self.state()
        self.assertEqual(s['schedule']['state'],'batch_completed')
        self.assertFalse(s['schedule']['enabled'])
        self.assertIsNone(s['schedule']['next_due_at'])
        self.assertEqual(s['schedule']['completed_rounds'],2)
        self.assertEqual([j['schedule_round_index'] for j in s['jobs']],[1,2])
        self.assertEqual(len({j['schedule_batch_id'] for j in s['jobs']}),1)
        self.time=100000;self.engine.tick()
        self.assertEqual(len(self.adapter.visits),4)

    def test_limit_validation_is_transactional_and_legacy_omission_is_unlimited(self):
        self.bounded();self.engine.stop();before=self.state()
        for limit in (True,0,-1,1.5,'2',1001):
            with self.assertRaises(ValueError):self.store.configure_schedule(before['revision'],self.map_id,60,limit)
            self.assertEqual(self.state(),before)
        self.store.configure_schedule(before['revision'],self.map_id,60)
        self.assertIsNone(self.state()['schedule']['round_limit'])

    def test_rearming_an_enabled_waiting_batch_cannot_reset_its_counter(self):
        self.bounded();self.run_due();before=self.state()
        with self.assertRaises(Conflict):self.engine.arm(before['revision'])
        self.assertEqual(self.state(),before)

    def test_uncertain_round_pauses_and_is_not_counted_as_success(self):
        self.bounded(3);self.run_due();original=self.adapter.visit
        def uncertain(asset,cancel):
            item=original(asset,cancel);item['status']='uncertain';return item
        self.adapter.visit=uncertain;self.time=1060;self.run_due()
        s=self.state()
        self.assertEqual(s['schedule']['completed_rounds'],1)
        self.assertEqual(s['schedule']['attempted_rounds'],2)
        self.assertFalse(s['schedule']['enabled'])
        self.assertEqual(s['jobs'][-1]['status'],'completed_with_errors')
        self.assertEqual(s['jobs'][-1]['metrics']['unsuccessful'],2)

    def test_cancelled_batch_does_not_count_or_replay_an_incomplete_round(self):
        self.bounded();self.adapter.block=True;self.engine.tick()
        self.assertTrue(self.adapter.entered.wait(1))
        self.engine.stop();self.engine.worker.join(2)
        s=self.state()
        self.assertEqual(s['schedule']['completed_rounds'],0)
        self.assertEqual(s['jobs'][-1]['status'],'stopped')
        self.time=100000;self.engine.tick()
        self.assertEqual(len(self.adapter.visits),1)

    def test_restart_preserves_count_and_pauses_without_replay(self):
        self.bounded();self.run_due()
        recreated=Scheduler(Store(self.root/'private'),self.adapter,clock=lambda:100000)
        recreated.tick();s=self.state()
        self.assertFalse(s['schedule']['enabled'])
        self.assertEqual(s['schedule']['completed_rounds'],1)
        self.assertEqual(len(self.adapter.visits),2)

    def test_single_round_after_batch_does_not_increment_or_rearm_old_batch(self):
        self.bounded(1);self.run_due()
        self.engine.run_once(self.state()['revision'],self.map_id);self.engine.worker.join(2)
        s=self.state()
        self.assertFalse(s['schedule']['enabled'])
        self.assertEqual(s['schedule']['completed_rounds'],1)
        self.assertNotIn('schedule_batch_id',s['jobs'][-1])

    def test_metrics_failure_cannot_leave_job_running_or_schedule_stuck(self):
        self.bounded(1)
        def broken_summary(key):raise RuntimeError('summary-only-fault')
        self.adapter.summary=broken_summary;self.run_due();s=self.state()
        self.assertEqual(s['jobs'][-1]['status'],'completed')
        self.assertEqual(s['jobs'][-1]['metrics']['summary_error'],'RuntimeError')
        self.assertEqual(s['schedule']['state'],'batch_completed')
        self.assertIsNone(self.engine.active_id)

    def test_cancelled_real_round_adapter_exception_is_stopped_not_failed(self):
        self.bounded()
        def interrupted(key,targets,cancel):
            self.adapter.entered.set();cancel.wait(2)
            raise RuntimeError('finite_preset_patrol_stopped')
        self.adapter.run_round=interrupted;self.engine.tick()
        self.assertTrue(self.adapter.entered.wait(1))
        self.engine.stop();self.engine.worker.join(2)
        job=self.state()['jobs'][-1]
        self.assertEqual(job['status'],'stopped')
        self.assertIsNone(job['error'])
        self.assertFalse(self.state()['schedule']['enabled'])


if __name__=='__main__':unittest.main()

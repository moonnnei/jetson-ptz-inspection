"""Server-owned fixed-delay scheduler. No camera adapter is enabled by default.

An adapter must provide ready(), position(), visit(asset, cancel), stop(). It
must enforce motion limits, fresh-frame barriers, timeouts, identity checks and
persist new captures itself. A separate hardware acceptance step is required
before a map/asset can be marked motion_verified/pose_status=verified.
"""
import threading
import time

from planner import route
from store import Conflict, identifier, now


class NoHardware:
    mode = 'review_only'

    def ready(self):
        return False

    def stop(self):
        pass


class Scheduler:
    def __init__(self, store, adapter=None, clock=time.time, monotonic=time.monotonic):
        self.store = store
        self.adapter = adapter or NoHardware()
        self.clock = clock
        self.monotonic = monotonic
        self.lock = threading.RLock()
        self.cancel = threading.Event()
        self.worker = None
        self.stop_error = None
        self.stopping = 0
        self.active_id = None
        self.deadline = None
        self.store.recover()

    def status(self):
        with self.lock:
            return dict(mode=self.adapter.mode, hardware_ready=bool(self.adapter.ready()),
                        active_job=self.active_id, stop_error=self.stop_error,
                        stop_pending=bool(self.stopping),
                        control_enabled=bool(self.adapter.ready()) and not self.stop_error and not self.stopping)

    def _targets(self, state, map_id):
        item = self.store.find(state, 'maps', map_id)
        if item['status'] == 'invalidated' or not item.get('motion_verified'):
            raise ValueError('map has not passed hardware validation')
        targets = [a for a in state['instruments'] if a['map_id'] == map_id and a['enabled']]
        if not targets:
            raise ValueError('there are no enabled confirmed instruments')
        for asset in targets:
            candidate = self.store.find(state, 'candidates', asset['candidate_id'])
            if candidate['state'] not in ('confirmed','human_labeled','auto_verified') or asset['pose_status'] != 'verified' or not asset['visit_pose']:
                raise ValueError('all enabled instruments need verified visit poses')
        return targets

    def arm(self, revision):
        with self.lock:
            if not self.adapter.ready() or self.stop_error or self.stopping:
                raise ValueError('hardware adapter is not commissioned; schedule remains paused')
            def apply(state):
                if state['schedule']['enabled']:raise Conflict('pause the current batch before arming another')
                self._targets(state, state['schedule']['map_id'])
                if any(j['status'] == 'running' for j in state['jobs']):
                    raise Conflict('inspection is already active')
                limit=state['schedule'].get('round_limit')
                if limit is not None and (type(limit) is not int or not 1<=limit<=1000):
                    raise ValueError('invalid saved round limit')
                state['schedule'].update(enabled=True, state='armed', next_due_at=self.clock(),
                    batch_id=identifier('batch'),completed_rounds=0,attempted_rounds=0,armed_at=now())
            return self.store.change(revision, 'schedule_arm', {}, apply)

    def run_once(self,revision,map_id):
        """One bounded patrol, without implicitly enabling a recurring timer."""
        with self.lock:
            if (not self.adapter.ready() or self.stop_error or self.stopping
                    or self.worker is not None and self.worker.is_alive()):
                raise ValueError('hardware unavailable or an inspection is active')
            state=self.store.snapshot()
            if state['revision']!=revision:raise Conflict('registry revision changed')
            if state['schedule']['enabled']:raise ValueError('pause periodic capture before a one-shot run')
            targets=self._targets(state,map_id)
            plan=route(targets,self.adapter.position())
            if plan['blocked']:raise ValueError('route contains unverified poses')
            by_id={a['id']:a for a in targets}
            targets=[by_id[item['id']] for item in plan['stops']]
            key=identifier('job')
            def start(s):
                self._targets(s,map_id)
                if any(j['status']=='running' for j in s['jobs']):raise Conflict('inspection is active')
                s['jobs'].append(dict(id=key,map_id=map_id,status='running',mode=self.adapter.mode,
                    started_at=now(),results=[],route=[a['id'] for a in targets],error=None,
                    execution='single_round_no_periodic_timer'))
                s['jobs']=s['jobs'][-80:]
                s['schedule'].update(enabled=False,state='running_once',next_due_at=None)
            self.store.change(revision,'inspection_start_once',dict(id=key,map_id=map_id),start)
            self.cancel=threading.Event();self.active_id=key
            self.deadline=self.monotonic()+(self.adapter.round_budget(len(targets)) if hasattr(self.adapter,'round_budget') else 180)
            self.worker=threading.Thread(target=self._run,args=(key,targets,self.cancel),name='registry-inspection-once')
            self.worker.daemon=True;self.worker.start()
            return dict(started=True,job_id=key,periodic_enabled=False)

    def stop(self, revision=None):
        with self.lock:
            # Check optimistic revision before changing runtime cancellation state.
            result = self.store.change(revision, 'schedule_stop', {},
                                       lambda s: self.store.pause(s, 'user_paused'))
            self.cancel.set()
            self.stopping += 1
        self._finish_stop()
        return result

    def _finish_stop(self):
        try:
            self.adapter.stop()
            with self.lock:
                self.stop_error = None
        except Exception:
            with self.lock:
                self.stop_error = 'STOP unconfirmed'
            raise RuntimeError('STOP unconfirmed; new runs are blocked')
        finally:
            with self.lock:
                self.stopping -= 1

    def tick(self):
        # Do not hold the lifecycle lock while issuing a watchdog STOP.
        with self.lock:
            running = self.worker is not None and self.worker.is_alive()
            expired = running and self.deadline is not None and self.monotonic() > self.deadline and not self.cancel.is_set()
        if running:
            if expired:
                self.stop()
            return
        with self.lock:
            if self.worker is not None and self.worker.is_alive():
                return
            state = self.store.snapshot()
            schedule = state['schedule']
            if (state.get('initialization') or {}).get('status') in ('scanning','detecting','mapping','importing','commissioning'):
                return
            if not schedule['enabled'] or schedule['next_due_at'] is None or self.clock() < schedule['next_due_at']:
                return
            if schedule.get('round_limit') is not None and schedule.get('completed_rounds',0)>=schedule['round_limit']:
                self.store.change(None,'schedule_batch_complete',{},lambda s:self.store.pause(s,'batch_completed'))
                return
            if not self.adapter.ready() or self.stop_error or self.stopping:
                self.store.change(None, 'schedule_blocked', {},
                                  lambda s: self.store.pause(s, 'hardware_unavailable'))
                return
            try:
                targets = self._targets(state, schedule['map_id'])
                plan = route(targets, self.adapter.position())
                if plan['blocked']:
                    raise ValueError('route contains unverified poses')
                by_id = {a['id']: a for a in targets}
                targets = [by_id[item['id']] for item in plan['stops']]
                key = identifier('job')
                def start(s):
                    self._targets(s, schedule['map_id'])
                    if any(j['status'] == 'running' for j in s['jobs']):
                        raise Conflict('inspection already running')
                    s['jobs'].append(dict(id=key, map_id=schedule['map_id'], status='running',
                        mode=self.adapter.mode, started_at=now(), results=[],
                        route=[a['id'] for a in targets], error=None,
                        schedule_batch_id=schedule.get('batch_id'),
                        schedule_round_index=schedule.get('attempted_rounds',0)+1,
                        schedule_round_limit=schedule.get('round_limit')))
                    s['jobs'] = s['jobs'][-80:]
                    s['schedule'].update(state='running', next_due_at=None,
                        attempted_rounds=schedule.get('attempted_rounds',0)+1)
                self.store.change(state['revision'], 'inspection_start', dict(id=key), start)
            except (ValueError, Conflict):
                self.store.change(None, 'schedule_blocked', {},
                                  lambda s: self.store.pause(s, 'validation_failed'))
                return
            self.cancel = threading.Event()
            self.active_id = key
            # Hard round budget; an adapter must also bound each operation.
            self.deadline = self.monotonic() + (self.adapter.round_budget(len(targets)) if hasattr(self.adapter,'round_budget') else 180)
            self.worker = threading.Thread(target=self._run, args=(key, targets, self.cancel),
                                           name='registry-inspection')
            self.worker.daemon = True
            self.worker.start()

    def _run(self, key, targets, cancel):
        result_state, error = 'completed', None
        started=self.monotonic()
        try:
            by_id={a['id']:a for a in targets}
            seen=set()
            if hasattr(self.adapter,'run_round'):
                results=self.adapter.run_round(key,targets,cancel)
            else:
                def legacy_results():
                    for asset in targets:
                        if cancel.is_set():break
                        yield self.adapter.visit(asset,cancel)
                results=legacy_results()
            for result in results:
                if cancel.is_set():
                    result_state = 'stopped'
                    break
                # A new adapter cannot silently present cached photos as current.
                if (not isinstance(result,dict) or result.get('instrument_id') not in by_id
                        or result['instrument_id'] in seen):
                    raise ValueError('invalid capture result')
                asset=by_id[result['instrument_id']]
                seen.add(asset['id'])
                if result.get('status') not in ('captured', 'not_found', 'uncertain', 'failed'):
                    raise ValueError('capture status is missing')
                if result['status'] == 'captured' and not all(result.get(k) for k in ('capture_id', 'captured_at', 'image_sha256', 'image')):
                    raise ValueError('new image evidence is required')
                result.update(reading=None, reading_status='not_integrated')
                def save(s):
                    self.store.find(s, 'jobs', key)['results'].append(result)
                self.store.change(None, 'inspection_result', dict(id=key, instrument_id=asset['id'],
                                   status=result['status']), save)
                if result['status'] != 'captured':
                    result_state = 'completed_with_errors'
            if cancel.is_set():
                result_state = 'stopped'
            elif seen!=set(by_id):
                raise ValueError('inspection omitted targets')
        except Exception as exc:
            # A real round adapter reports a cancelled finite capture by
            # raising after STOP. Keep that distinct from an uncancelled fault;
            # any unconfirmed STOP below still overrides this as failed.
            result_state,error=('stopped',None) if cancel.is_set() else ('failed',type(exc).__name__)
        finally:
            with self.lock:
                self.stopping += 1
            try:
                self._finish_stop()
            except Exception:
                result_state, error = 'failed', 'STOP_unconfirmed'
                with self.lock:
                    self.stop_error = 'STOP unconfirmed'
            detail={}
            try:
                if hasattr(self.adapter,'summary'):detail=self.adapter.summary(key) or {}
                if not isinstance(detail,dict):raise ValueError('invalid capture summary')
            except Exception as exc:
                detail=dict(summary_error=type(exc).__name__)
            with self.lock:
                def finish(s):
                    job = self.store.find(s, 'jobs', key)
                    metrics=dict(targets=len(targets),captured=sum(r['status']=='captured' for r in job['results']),
                        unsuccessful=sum(r['status']!='captured' for r in job['results']))
                    metrics.update(detail)
                    job.update(status=result_state, finished_at=now(), error=error,
                        elapsed_seconds=max(0.,self.monotonic()-started),metrics=metrics)
                    schedule = s['schedule']
                    if (result_state=='completed' and job.get('schedule_batch_id') is not None
                            and job['schedule_batch_id']==schedule.get('batch_id')):
                        schedule['completed_rounds']=schedule.get('completed_rounds',0)+1
                    limit=schedule.get('round_limit')
                    if schedule['enabled'] and result_state=='completed':
                        if limit is not None and schedule.get('completed_rounds',0)>=limit:
                            self.store.pause(s,'batch_completed')
                        else:schedule.update(state='waiting', next_due_at=self.clock() + schedule['interval_seconds'])
                    else:
                        self.store.pause(s, 'inspection_' + result_state)
                self.store.change(None, 'inspection_finish', dict(id=key, status=result_state), finish)
                self.active_id = None
                self.deadline = None

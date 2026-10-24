"""Finite first-capture workflow over reviewed scan references, without readings."""
import copy
import hashlib
import json
from pathlib import Path
import re
import threading
import time

from planner import nearest_stops, pose
from references import digest, reference_plan
from ring_capture import close_pose
from store import now
from teaching import Teaching, pilot_plan


def save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    __import__('os').replace(str(temporary), str(path))


def first_capture_plan(state, map_id, anchor, operation_id=None, mode='strict'):
    anchor = pose(anchor)
    if mode not in ('strict', 'adaptive'):
        raise ValueError('first-capture mode must be strict or adaptive')
    if operation_id is not None and (not isinstance(operation_id, str)
            or not re.fullmatch(r'attempt-[a-f0-9]{16}', operation_id)):
        raise ValueError('invalid first-capture attempt identifier')
    references = reference_plan(state, map_id, state['revision'])
    if len(references['targets']) > 12:
        raise ValueError('first capture supports at most 12 reviewed instruments per finite batch')
    pending, blocked = [], []
    for target in references['targets']:
        ref = target['references'][0]
        reason = None
        if (ref['source_kind'] != 'bounded_scan_capture' or ref['source_pose'] is None
                or ref['orientation'] != 'rotate180_from_raw'):
            reason = 'oriented_scan_source_pose_required'
        elif (ref['width'], ref['height']) != (2560, 1440):
            reason = 'scan_resolution_profile_mismatch'
        elif pose(ref['source_pose'])['zoom'] != 1.:
            reason = 'one_times_scan_reference_required'
        if reason:
            blocked.append(dict(id=target['instrument_id'], reason=reason))
        else:
            pending.append(dict(id=target['instrument_id'], name=target['name'], pose=ref['source_pose']))
    ordered, cost = nearest_stops(pending, anchor)
    from adaptive_capture import adaptive_plan
    builder = adaptive_plan if mode == 'adaptive' else pilot_plan
    stops = [dict(id=stop['id'], name=stop['name'],
                  teaching_plan=builder(references, anchor, stop['id'])) for stop in ordered]
    plan = dict(schema_version=1, kind='bounded_first_capture_batch', map_id=map_id,
                revision=state['revision'], anchor=anchor, references=references, stops=stops,
                operation_id=operation_id, mode=mode,
                blocked=blocked, ordering='nearest_source_view_with_return_each_target',
                source_order_cost=round(cost, 4), optimality_claim=False,
                strict_centering=mode=='strict', target_zoom=2.,
                return_each_successful_target=True, stop_on_any_failed_target=True,
                requires_prior_closeup=False, reading=None, periodic_capture_enabled=False,
                visit_pose_verified=False, quality_review_required=True,
                budget_seconds=sum(s['teaching_plan']['budget_seconds'] for s in stops) + 10,
                ready_for_trial=bool(stops) and not blocked, execution_enabled=False)
    for axis, hardware in (('pan', (0., 350.)), ('tilt', (0., 90.)), ('zoom', (1., 2.))):
        values = [anchor[axis]]
        for stop in stops:
            values.extend(stop['teaching_plan'][axis + '_limits'])
        plan[axis + '_limits'] = [max(hardware[0], min(values)), min(hardware[1], max(values))]
    plan['fingerprint'] = digest(plan)
    return plan


def validate_first_capture(plan, state):
    if plan != first_capture_plan(state, plan['map_id'], plan['anchor'], plan.get('operation_id'),plan['mode']):
        raise ValueError('first-capture plan or human review changed')
    return copy.deepcopy(plan)


class FirstCaptureBatch:
    def __init__(self, io_factory, state_provider, directory, plan, cancel=None, controller=None):
        self.factory, self.provider = io_factory, state_provider
        self.root, self.plan = Path(directory), copy.deepcopy(plan)
        self.cancel = cancel or threading.Event()
        from adaptive_capture import AdaptiveCapture
        self.done = threading.Event()
        self.controller = controller or (AdaptiveCapture if plan['mode']=='adaptive' else Teaching)
        self.record = dict(kind=plan['kind'], plan=copy.deepcopy(plan), status='prepared',
                           targets=[], reading=None, periodic_capture_enabled=False,
                           visit_pose_verified=False, quality_review_required=True)
        self.active_io = None
        self.watch_fault = False

    def persist(self):
        save(self.root / 'batch.json', self.record)

    def watch(self):
        while not self.done.wait(.25):
            try:
                validate_first_capture(self.plan, self.provider())
                expired = time.monotonic() >= self.deadline
            except Exception:
                expired = True
            if expired or self.cancel.is_set():
                self.cancel.set()
                try:
                    if self.active_io is not None:
                        self.active_io.stop()
                except Exception:
                    self.watch_fault = True
                return

    def run(self):
        validate_first_capture(self.plan, self.provider())
        if not self.plan['ready_for_trial']:
            raise ValueError('first capture has missing or unsupported source references')
        self.root.mkdir(parents=True, exist_ok=False)
        started = time.monotonic()
        self.deadline = started + self.plan['budget_seconds']
        self.record.update(status='running', started_at=now())
        self.persist()
        watcher = threading.Thread(target=self.watch, name='first-capture-watchdog')
        watcher.daemon = True
        watcher.start()
        try:
            for stop in self.plan['stops']:
                if self.cancel.is_set() or time.monotonic() >= self.deadline:
                    raise RuntimeError('first_capture_cancelled_or_timed_out')
                validate_first_capture(self.plan, self.provider())
                self.record['active_target'] = stop['id']
                self.persist()
                self.active_io = self.factory(stop['teaching_plan'])
                folder = self.root / stop['id']
                result = self.controller(self.active_io, folder, stop['teaching_plan'], self.cancel).run()
                item = dict(instrument_id=stop['id'], directory=stop['id'], status=result['status'],
                            record_sha256=hashlib.sha256((folder / 'record.json').read_bytes()).hexdigest(),
                            stop_confirmed=result.get('stop_confirmed') is True,
                            elapsed_seconds=result['elapsed_seconds'], error=result.get('error'))
                self.record['targets'].append(item)
                self.persist()
                if result['status'] != 'completed' or not item['stop_confirmed']:
                    raise RuntimeError('first_capture_target_failed')
                if not close_pose(self.active_io.position(), self.plan['anchor']):
                    raise RuntimeError('first_capture_anchor_not_restored')
                self.active_io = None
            self.record.update(status='completed', active_target=None)
        except Exception as exc:
            self.record.update(status='stopped' if self.cancel.is_set() else 'failed',
                               error=type(exc).__name__ + ': ' + str(exc), active_target=None)
        finally:
            self.done.set()
            watcher.join(timeout=4)
            if self.active_io is not None:
                try:
                    self.active_io.stop()
                except Exception:
                    self.watch_fault = True
            if self.watch_fault or watcher.is_alive():
                self.record.update(status='failed', error='first_capture_STOP_or_watchdog_unconfirmed')
            self.record.update(finished_at=now(), elapsed_seconds=time.monotonic() - started,
                               stop_confirmed=not self.watch_fault and not watcher.is_alive()
                               and all(t['stop_confirmed'] for t in self.record['targets']))
            self.persist()
        return copy.deepcopy(self.record)

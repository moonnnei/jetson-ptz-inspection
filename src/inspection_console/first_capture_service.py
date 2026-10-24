"""Optional finite first-capture execution. Camera credentials stay in memory."""
import copy
import json
from pathlib import Path
import threading

from capture_round import round_plan
from first_capture import FirstCaptureBatch, first_capture_plan, validate_first_capture
from first_capture_history import import_first_capture
from store import identifier


class FirstCaptureService:
    def __init__(self, store, project_root=None, console_root=None, camera=None):
        self.store, self.root, self.console, self.camera = store, project_root, console_root, camera
        self.enabled = camera is not None
        self.lock = threading.RLock()
        self.thread = None
        self.cancel = threading.Event()
        self.prepared = None
        self.used = set()
        self.batch = None
        self.outcome = None

    def status(self):
        with self.lock:
            active = self.thread is not None and self.thread.is_alive()
            report = dict(available=self.enabled, active=active,
                          status='running' if active else 'idle', periodic_enabled=False,
                          reading_enabled=False, outcome=copy.deepcopy(self.outcome))
            if self.batch is not None:
                try:
                    record = json.loads((self.batch.root / 'batch.json').read_text(encoding='utf-8'))
                    report.update(status=record['status'], active_target=record.get('active_target'),
                                  completed_targets=sum(t['status'] == 'completed' for t in record['targets']),
                                  total_targets=len(record['plan']['stops']),
                                  stop_confirmed=record.get('stop_confirmed', False))
                except (OSError, ValueError):
                    pass
            return report

    def prepare(self, map_id, revision, mode='adaptive'):
        if not self.enabled:
            raise ValueError('first-capture hardware is not connected')
        from capture_review_views import motion_lease
        from capture_ring_views import check_additional_services
        from capture_review_views import check_other_services
        from scan_capture import ISAPIScanIO
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                raise ValueError('a first-capture batch is already running')
            state = self.store.snapshot()
            if state['revision'] != revision:
                raise ValueError('human review changed; refresh before preparing')
            with motion_lease():
                check_other_services()
                check_additional_services()
                anchor = ISAPIScanIO(self.camera).preflight()['anchor']
            self.prepared = first_capture_plan(state, map_id, anchor, identifier('attempt'),mode)
            return dict(motion_sent=False, plan=copy.deepcopy(self.prepared))

    def start(self, map_id, revision, fingerprint, site_ready):
        if not self.enabled or site_ready is not True:
            raise ValueError('connected hardware and explicit site readiness are required')
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                raise ValueError('a first-capture batch is already running')
            plan = self.prepared
            if (plan is None or plan['fingerprint'] != fingerprint or plan['map_id'] != map_id
                    or plan['revision'] != revision or fingerprint in self.used):
                raise ValueError('prepare a fresh first-capture plan before starting')
            validate_first_capture(plan, self.store.snapshot())
            if not plan['ready_for_trial']:
                raise ValueError('first-capture plan has blocked targets')
            self.used.add(fingerprint)
            self.prepared = None
            self.cancel = threading.Event()
            self.outcome = None
            self.batch = None
            self.thread = threading.Thread(target=self._run, args=(copy.deepcopy(plan),),
                                           name='first-capture-execution')
            self.thread.daemon = True
            self.thread.start()
            return dict(started=True, fingerprint=fingerprint, periodic_enabled=False)

    def _run(self, plan):
        try:
            from capture_review_views import motion_lease
            from first_capture_runtime import io_factory, interlocks
            output = Path(self.root) / 'var/first-capture' / identifier('batch')
            with motion_lease():
                interlocks()
                factory = io_factory(self.camera, self.root, self.console, plan['references'])
                self.batch = FirstCaptureBatch(factory, self.store.snapshot, output, plan, self.cancel)
                result = self.batch.run()
                imported = import_first_capture(self.store, output, plan['revision'])
                next_plan = round_plan(self.store.snapshot(), plan['map_id'], plan['anchor'])
            with self.lock:
                self.outcome = dict(status=result['status'], imported=imported,
                                    elapsed_seconds=result['elapsed_seconds'],
                                    stop_confirmed=result['stop_confirmed'],
                                    next_round_ready=result['status']=='completed' and next_plan['ready_for_trial'],
                                    next_round_fingerprint=next_plan['fingerprint'])
        except Exception as exc:
            with self.lock:
                # Do not expose camera credentials, URLs or adapter tracebacks.
                self.outcome = dict(status='failed', error_type=type(exc).__name__,
                                    saved_evidence_available=self.batch is not None)

    def stop(self):
        self.cancel.set()
        return dict(cancel_requested=True, state=self.status())

    def close(self):
        self.cancel.set()
        thread = self.thread
        if thread is not None:
            thread.join(timeout=20)
            if thread.is_alive():
                raise RuntimeError('first-capture worker shutdown is unconfirmed')

"""Reachable first captures: bounded centering, zoom fitting and explicit residuals."""
import copy
import math
import threading
import time

from best_effort import ConstrainedCapture
from references import digest
from store import bbox, now
from teaching import pilot_plan


def adaptive_plan(references, anchor, instrument_id):
    plan = pilot_plan(references, anchor, instrument_id)
    plan.pop('fingerprint')
    plan.update(kind='bounded_meter_adaptive_capture', allow_center_residual=True,
                strict_centering=False, frame_margin=.02, target_zoom=2.,
                zoom_fit_model='linear_image_center_estimate_then_fresh_appearance_check',
                zoom_mismatch_policy='one_return_to_last_checked_pose_then_stop_zooming',
                quality_review_required=True)
    plan['fingerprint'] = digest(plan)
    return plan


def validate_adaptive(plan, references):
    if plan != adaptive_plan(references, plan['anchor'], plan['target']['instrument_id']):
        raise ValueError('adaptive capture plan changed')
    return copy.deepcopy(plan)


def inside_frame(frame, margin=.02):
    box = bbox(frame['bbox'], frame['width'], frame['height'])
    return (box[0] >= margin*frame['width'] and box[1] >= margin*frame['height']
            and box[2] <= (1-margin)*frame['width'] and box[3] <= (1-margin)*frame['height'])


def fit_zoom(frame, maximum=2., margin=.02):
    box = bbox(frame['bbox'], frame['width'], frame['height'])
    extent = max(abs(box[0]/frame['width']-.5), abs(box[2]/frame['width']-.5),
                 abs(box[1]/frame['height']-.5), abs(box[3]/frame['height']-.5))
    estimate = min(maximum, frame['pose']['zoom']*(.5-margin)/max(extent,1e-8))
    return max(1., math.floor((estimate+1e-8)*10)/10.)


class AdaptiveCapture(ConstrainedCapture):
    def run(self):
        self.root.mkdir(parents=True, exist_ok=False)
        self.started = time.monotonic()
        self.deadline = self.started + self.plan['budget_seconds']
        self.state.update(status='running', started_at=now(), visit_pose_verified=False,
                          quality_review_required=True, strict_centering=False)
        watcher = threading.Thread(target=self.watch, name='adaptive-capture-watchdog')
        watcher.daemon = True
        watcher.start()
        last = None
        try:
            self.io.preflight(self.plan)
            self.persist()
            current = self.io.position()
            self.move(dict(current, zoom=1.), 'source_approach_wide_zoom')
            self.travel(self.plan['target']['references'][0]['source_pose'], 'approach_source_view')
            last = self.observe('adaptive_source_reference_check')
            last = self.center_constrained(self.calibration(1.))
            for requested in self.plan['zoom_steps']:
                fitted = fit_zoom(last, self.plan['target_zoom'], self.plan['frame_margin'])
                zoom = min(requested, fitted)
                if zoom <= last['pose']['zoom']+.05:
                    self.event('zoom_limited', reason='predicted_frame_edge', fitted_zoom=fitted,
                               current_zoom=last['pose']['zoom'])
                    break
                previous = copy.deepcopy(last)
                reference = copy.deepcopy(self.reference)
                self.move(dict(last['pose'], zoom=zoom), 'adaptive_optical_zoom')
                try:
                    observed = self.observe('adaptive_post_zoom_check')
                except RuntimeError as exc:
                    if not str(exc).startswith('target_uncertain:'):
                        raise
                    # Only appearance rejection permits this one bounded
                    # rollback. Transport, cancel and STOP errors propagate.
                    self.reference = reference
                    self.move(previous['pose'], 'restore_last_checked_zoom')
                    last = self.observe('adaptive_zoom_recovery')
                    self.event('zoom_limited', reason='appearance_rejected_at_larger_zoom',
                               retained_zoom=last['pose']['zoom'])
                    break
                if not inside_frame(observed, self.plan['frame_margin']):
                    self.reference = reference
                    self.move(previous['pose'], 'restore_last_full_frame_zoom')
                    last = self.observe('adaptive_zoom_recovery')
                    self.event('zoom_limited', reason='observed_frame_edge', retained_zoom=last['pose']['zoom'])
                    break
                last = observed
                if zoom < requested-.05:
                    break
            if not inside_frame(last, self.plan['frame_margin']):
                self.state['capture_status'] = 'frame_edge_risk_not_accepted_reference'
                raise RuntimeError('target_at_frame_edge_even_at_reachable_wide_view')
            taught = copy.deepcopy(last)
            self.state.update(taught_pose=taught['pose'], teaching_frame=taught['path'])
            self.return_home()
            self.event('independent_revisit_started', target=taught['pose'])
            self.travel(dict(taught['pose'], zoom=1.), 'revisit_pan_tilt')
            self.move(taught['pose'], 'restore_adaptive_capture_zoom')
            checked = [self.observe('independent_revisit_1'), self.observe('independent_revisit_2')]
            if not all(inside_frame(f, self.plan['frame_margin']) for f in checked):
                raise RuntimeError('revisit_frame_edge_risk')
            evidence = self.io.evidence(checked[-1], self.root, self.plan['target']['instrument_id'])
            is_centered = all(f['centered'] for f in checked)
            self.state.update(revisit_verified=True, evidence=evidence, visit_pose=checked[-1]['pose'],
                              capture_status='centered' if is_centered else 'off_center_inside_frame',
                              center_status='centered' if is_centered else 'reachable_pose_with_residual',
                              center_error=checked[-1]['center_error'],
                              frame_bounds_checked=True, reading=None)
            self.return_home()
            self.state.update(status='completed', restored_pose=self.io.position())
        except Exception as exc:
            self.state.update(status='stopped' if self.cancel.is_set() else 'failed',
                              error=type(exc).__name__+': '+str(exc))
        finally:
            try:
                self.stop()
            except Exception:
                self.state.update(status='failed', error='STOP_unconfirmed')
            self.done.set()
            watcher.join(timeout=4)
            if self.stop_failed or watcher.is_alive():
                self.state.update(status='failed', error='STOP_or_watchdog_unconfirmed')
            self.state.update(finished_at=now(), elapsed_seconds=time.monotonic()-self.started,
                              stop_confirmed=not self.stop_failed)
            self.persist()
        return copy.deepcopy(self.state)

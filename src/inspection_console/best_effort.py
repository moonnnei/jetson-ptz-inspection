"""Constrained image capture when precise centering is explicitly not required.

This separate mode never verifies visit poses or enables periodic inspection.
It can retain an off-center image, but must still check the reference's identity.
"""
import copy
import math
import threading
import time

from references import digest
from store import now
from teaching import Teaching, pilot_plan, correction


def capture_plan(references,anchor,instrument_id,approach_source=False):
    if type(approach_source) is not bool:raise ValueError('source approach flag must be boolean')
    result=pilot_plan(references,anchor,instrument_id)
    result.pop('fingerprint')
    result.update(kind='bounded_meter_constrained_capture',budget_seconds=240,
        max_center_iterations=12,independent_revisit=False,
        start_requires_current_reference_match=True,
        allow_center_residual=True,allow_verified_visit_pose=False,
        quality_goal='reference_checked_image_capture_not_commissioning')
    if approach_source:
        result.update(approach_source_view=True,start_requires_current_reference_match=False)
    result['fingerprint']=digest(result)
    return result


def validate_capture_plan(plan,references):
    expected=capture_plan(references,plan['anchor'],plan['target']['instrument_id'],
                          plan.get('approach_source_view',False))
    if expected!=plan:raise ValueError('constrained capture plan changed')
    return copy.deepcopy(expected)


class ConstrainedCapture(Teaching):
    def center_constrained(self,matrix):
        for unused in range(self.plan['max_center_iterations']):
            frame=self.observe('constrained_centering')
            if frame['centered']:return frame
            delta=correction(matrix,frame['center_error'],self.plan['proportional_gain'],
                             self.plan['max_servo_step_degrees'])
            proposed=dict(frame['pose'],pan=frame['pose']['pan']+delta[0],
                          tilt=frame['pose']['tilt']+delta[1])
            target=dict(proposed)
            limited=[]
            for axis in ('pan','tilt'):
                low,high=self.plan[axis+'_limits']
                target[axis]=round(min(high,max(low,target[axis])),1)
                if abs(target[axis]-proposed[axis])>.051:limited.append(axis)
            if limited:
                self.state['center_residual_allowed']=True
                self.event('constrained_action',limited_axes=limited,proposed=proposed,
                           bounded=target,center_error=frame['center_error'])
            # Once an axis is blocked, finish the other axis instead of asking
            # the blocked axis to compensate for residual error indefinitely.
            pan_ok=abs(frame['center_error'][0])<=self.plan['center_tolerance'][0]
            tilt_blocked='tilt' in limited
            if (pan_ok and tilt_blocked) or max(abs(target[k]-frame['pose'][k]) for k in ('pan','tilt'))<.11:
                return frame
            self.move(target,'constrained_proportional_centering')
        return self.observe('constrained_iteration_limit')

    def run(self):
        self.root.mkdir(parents=True,exist_ok=False)
        self.started=time.monotonic()
        self.deadline=self.started+self.plan['budget_seconds']
        self.state.update(started_at=now(),visit_pose_verified=False,revisit_verified=False,
                          image_status='constrained_capture_not_verified_visit')
        watcher=threading.Thread(target=self.watch,name='constrained-capture-watchdog')
        watcher.daemon=True
        watcher.start()
        last=None
        try:
            self.io.preflight(self.plan)
            self.state['status']='running'
            self.persist()
            if self.plan.get('approach_source_view'):
                # Separate prepared profile: coarse source-angle search is
                # permitted only when its larger range was explicitly reviewed.
                current=self.io.position()
                self.move(dict(current,zoom=1.),'source_approach_wide_zoom')
                self.travel(self.plan['target']['references'][0]['source_pose'],'approach_source_view')
                last=self.observe('source_view_reference_check')
            else:
                # Default does not trust a pre-adjustment source angle.
                last=self.observe('current_reference_check_before_any_movement')
            if last['pose']['zoom']!=1.:
                self.move(dict(last['pose'],zoom=1.),'wide_zoom')
                last=self.observe('wide_reference_check')
            matrix=self.calibration(1.)
            last=self.center_constrained(matrix)
            for zoom in self.plan['zoom_steps']:
                self.move(dict(last['pose'],zoom=zoom),'optical_zoom')
                last=self.observe('reference_check_after_zoom')
            self.state['evidence']=self.io.evidence(last,self.root,self.plan['target']['instrument_id'])
            self.state.update(capture_pose=last['pose'],center_error=last['center_error'],
                center_status='centered' if last['centered'] else 'off_center_due_to_motion_limits',
                quality_status='appearance_checked_sharpness_recorded_not_reading_validated')
            self.return_home()
            self.state.update(status='partial_capture_completed',restored_pose=self.io.position())
        except Exception as exc:
            self.state.update(status='stopped' if self.cancel.is_set() else 'failed',
                              error=type(exc).__name__+': '+str(exc))
            # An earlier passing frame may be retained diagnostically. It is
            # never called the current view after a later mismatch or failure.
            if last is not None:
                self.state['last_passing_frame']=last['path']
        finally:
            try:self.stop()
            except Exception:self.state.update(status='failed',error='STOP_unconfirmed')
            self.done.set()
            watcher.join(timeout=4)
            if self.stop_failed or watcher.is_alive():
                self.state.update(status='failed',error='STOP_or_watchdog_unconfirmed')
            self.state.update(finished_at=now(),elapsed_seconds=time.monotonic()-self.started,
                              stop_confirmed=not self.stop_failed)
            self.persist()
        return copy.deepcopy(self.state)

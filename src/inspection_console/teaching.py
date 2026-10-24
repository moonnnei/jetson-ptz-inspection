"""Single-meter teaching and independent revisit, Python 3.5 standard library.

No hardware default and no registry writes. IO owns fresh snapshots, image
matching, commands and interlocks. A returned encoder angle is not evidence of
target identity: every operational image must pass the appearance check.
"""
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import threading
import time

from planner import pose
from references import digest
from store import now


def pilot_plan(references, anchor, instrument_id):
    anchor = pose(anchor)
    selected = [x for x in references['targets'] if x['instrument_id'] == instrument_id]
    if len(selected) != 1:
        raise ValueError('one confirmed meter is required')
    target = copy.deepcopy(selected[0])
    reference = target['references'][0]
    source = pose(reference['source_pose'])
    if reference['source_kind'] != 'bounded_scan_capture' or reference['orientation'] != 'rotate180_from_raw':
        raise ValueError('pilot requires an oriented bounded-scan reference')
    # Local centering can require tens of degrees when a meter is at an edge.
    # These are explicit position limits, not estimates of angular velocity.
    pans = [max(0., min(anchor['pan'], source['pan']-50.)),
            min(350., max(anchor['pan'], source['pan']+12.))]
    tilts = [max(0., min(anchor['tilt'], source['tilt'])-8.),
             min(90., max(anchor['tilt'], source['tilt'])+20.)]
    result = dict(schema_version=1, kind='bounded_meter_teaching_pilot',
        map_id=references['map_id'], revision=references['revision'],
        reference_fingerprint=references['fingerprint'], target=target, anchor=anchor,
        pan_limits=pans, tilt_limits=tilts, zoom_limits=[1.,2.],
        max_transit_step_degrees=20., max_servo_step_degrees=4.,
        zoom_steps=[1.2,1.5,1.8,2.], proportional_gain=.65,
        center_tolerance=[.035,.04], max_center_iterations=14,
        calibration_preload_degrees=3., calibration_steps_degrees=[1.5,3.],
        budget_seconds=360, return_on_success=True, return_after_failure=False,
        independent_revisit=True, periodic_capture_enabled=False,
        reading=None, execution_enabled=False)
    result['fingerprint'] = digest(result)
    return result


def validate_plan(plan, references):
    expected = pilot_plan(references, plan['anchor'], plan['target']['instrument_id'])
    if plan != expected:
        raise ValueError('teaching plan changed')
    return copy.deepcopy(expected)


def centered(box, width, height, tolerance):
    error = [(box[0]+box[2])/(2.*width)-.5,
             (box[1]+box[3])/(2.*height)-.5]
    if not all(math.isfinite(x) for x in error):
        raise ValueError('invalid center error')
    return all(abs(x)<=limit for x,limit in zip(error,tolerance)), error


def correction(matrix, error, gain, limit):
    a,b = matrix[0]
    c,d = matrix[1]
    if not all(math.isfinite(x) for x in (a,b,c,d)):
        raise ValueError('non-finite motion model')
    lengths = [math.hypot(a,c),math.hypot(b,d)]
    determinant = a*d-b*c
    if (min(lengths)<.001 or max(lengths)>.1 or
            abs(determinant)<.25*lengths[0]*lengths[1]):
        raise ValueError('weak or ambiguous motion model')
    delta = [-gain*(d*error[0]-b*error[1])/determinant,
             -gain*(-c*error[0]+a*error[1])/determinant]
    scale = min(1., limit/max(max(abs(v) for v in delta),1e-12))
    return [v*scale for v in delta]


class Teaching:
    def __init__(self, io, directory, plan, cancel=None):
        self.io, self.root, self.plan = io, Path(directory), copy.deepcopy(plan)
        self.cancel = cancel or threading.Event()
        self.done = threading.Event()
        self.lock = threading.RLock()
        self.stop_failed = False
        self.state = dict(kind=plan['kind'], plan=copy.deepcopy(plan), status='prepared',
            events=[], captures=[], reading=None, periodic_capture_enabled=False,
            identity_method='human_reference_scene_and_local_appearance_chain',
            identity_guarantee=False, image_time_basis='host_receive_not_exposure')
        self.reference = copy.deepcopy(plan['target']['references'][0])
        self.matrices = {}

    def persist(self):
        with self.lock:
            temp = self.root/'record.tmp'
            temp.write_text(json.dumps(self.state,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
            os.replace(str(temp),str(self.root/'record.json'))

    def event(self, kind, **data):
        with self.lock:
            data.update(event=kind, at=now())
            self.state['events'].append(data)
            self.persist()

    def check(self):
        if self.cancel.is_set() or time.monotonic()>self.deadline:
            self.cancel.set()
            raise RuntimeError('teaching_cancelled_or_timed_out')
        if self.stop_failed:
            raise RuntimeError('STOP_unconfirmed')

    def stop(self):
        try:
            self.io.stop()
        except Exception:
            self.stop_failed = True
            self.cancel.set()
            raise RuntimeError('STOP_unconfirmed')

    def watch(self):
        if not self.done.wait(self.plan['budget_seconds']):
            self.cancel.set()
            try:self.stop()
            except Exception:pass

    def move(self, target, phase):
        self.check()
        self.io.move(pose(target), self.cancel)
        self.check()
        self.event('move',phase=phase, actual=self.io.position())

    def travel(self, target, phase):
        target = pose(target)
        start = self.io.position()
        count = max(1,int(math.ceil(max(abs(target[k]-start[k]) for k in ('pan','tilt'))/self.plan['max_transit_step_degrees'])))
        for i in range(1,count+1):
            point = dict(target,**{k:round(start[k]+(target[k]-start[k])*i/count,1) for k in ('pan','tilt')})
            self.move(point,phase)

    def capture(self, purpose):
        self.check()
        frame = self.io.capture(self.root, len(self.state['captures'])+1, self.cancel)
        self.check()
        if frame['received_monotonic'] < self.started or time.monotonic()-frame['received_monotonic']>12:
            raise RuntimeError('stale_capture')
        frame['purpose'] = purpose
        self.state['captures'].append(frame)
        self.persist()
        return frame

    def observe(self, purpose):
        frame = self.capture(purpose)
        match = self.io.locate(self.reference, frame)
        self.check()
        self.event('appearance_check', frame=frame['path'], parent=self.reference['image'], result=match)
        if match['status'] != 'matched_reference':
            raise RuntimeError('target_uncertain:' + match['reason'])
        box = match['bbox']
        ok,error = centered(box,frame['width'],frame['height'],self.plan['center_tolerance'])
        frame.update(bbox=box,center_error=error,centered=ok,match=match)
        # Only a passing check extends the chain. The human primary is immutable.
        self.reference = dict(image=frame['path'], bbox=box, width=frame['width'],
                              height=frame['height'], sha256=frame['sha256'], local_capture=True,
                              source_pose=frame['pose'],orientation=frame.get('orientation'))
        self.event('observation',frame=frame['path'],center_error=error,centered=ok)
        return frame

    def calibration(self, zoom):
        self.check()
        if zoom in self.matrices:return self.matrices[zoom]
        home = self.io.position()
        directions = {}
        for axis,limits in (('pan',self.plan['pan_limits']),('tilt',self.plan['tilt_limits'])):
            reserve = self.plan['calibration_preload_degrees']+max(self.plan['calibration_steps_degrees'])+.3
            if limits[1]-home[axis]>=reserve:directions[axis]=1.
            elif home[axis]-limits[0]>=reserve:directions[axis]=-1.
            else:raise RuntimeError('insufficient_calibration_margin')
        columns = []
        for axis in ('pan','tilt'):
            accepted = False
            for step in self.plan['calibration_steps_degrees']:
                self.move(home,'calibration_home')
                preload = dict(home)
                preload[axis] += directions[axis]*self.plan['calibration_preload_degrees']
                self.move(preload,'calibration_preload')
                before = self.capture('calibration_reference_'+axis)
                probe = dict(preload)
                probe[axis] += directions[axis]*step
                self.move(probe,'calibration_probe')
                after = self.capture('calibration_probe_'+axis)
                measured = self.io.measure(before,after)
                self.check()
                delta = after['pose'][axis]-before['pose'][axis]
                column = [v/delta for v in measured['displacement']] if measured.get('accepted') and abs(delta)>=1. else None
                accepted = bool(column and .001<=math.hypot(*column)<=.1)
                self.event('calibration_attempt',axis=axis, zoom=zoom, home=home,
                           step=step, delta=delta, column=column, accepted=accepted,measurement=measured)
                if accepted:
                    columns.append(column)
                    break
                # Only a completed, quality-rejected measurement permits the
                # second finite probe. Transport/cancel/STOP failures propagate.
            if not accepted:raise RuntimeError('calibration_quality_rejected')
        matrix = [[columns[0][0],columns[1][0]],[columns[0][1],columns[1][1]]]
        correction(matrix,[0.,0.],1.,1.)
        self.move(home,'calibration_return')
        self.matrices[zoom] = matrix
        self.event('calibration',zoom=zoom,matrix=matrix,scope='fixed_base_local_scene_zoom_specific')
        return matrix

    def center(self, matrix=None):
        stable = 0
        for iteration in range(self.plan['max_center_iterations']):
            frame = self.observe('visual_centering')
            if frame['centered']:
                stable += 1
                if stable>=2:return frame
                continue
            stable = 0
            zoom = frame['pose']['zoom']
            if matrix is None:
                matrix = self.calibration(zoom)
                # Calibration moved the camera. Observe again after its return
                # instead of applying an error from a pre-calibration frame.
                continue
            delta = correction(matrix,frame['center_error'],self.plan['proportional_gain'],self.plan['max_servo_step_degrees'])
            target = dict(frame['pose'])
            target['pan'] += delta[0]
            target['tilt'] += delta[1]
            for axis,hardware in (('pan',(0.,350.)),('tilt',(0.,90.)),('zoom',(1.,2.))):
                limits=self.plan[axis+'_limits']
                if not limits[0]<=target[axis]<=limits[1]:
                    full_delta=correction(matrix,frame['center_error'],1.,100000.)
                    estimated=dict(frame['pose'],pan=frame['pose']['pan']+full_delta[0],
                                   tilt=frame['pose']['tilt']+full_delta[1])
                    reason=('target_requires_motion_beyond_camera_range' if
                        not hardware[0]<=target[axis]<=hardware[1] else
                        'target_outside_prepared_envelope')
                    diagnostic=dict(reason=reason,axis=axis,current_pose=frame['pose'],
                        proposed_bounded_step=target,estimated_center_pose=estimated,
                        prepared_limits=limits,hardware_limits=list(hardware),
                        center_error=frame['center_error'],frame=frame['path'],
                        estimate_scope='local_linear_model_not_global_reachability_proof')
                    self.state['reachability']=diagnostic
                    self.event('motion_refused',**diagnostic)
                    raise RuntimeError(reason)
            self.move(target,'proportional_centering')
        raise RuntimeError('centering_iteration_budget_exhausted')

    def return_home(self):
        current = self.io.position()
        self.move(dict(current,zoom=1.),'return_wide_zoom')
        self.travel(dict(self.plan['anchor'],zoom=1.),'return_pan_tilt')
        self.move(self.plan['anchor'],'restore_anchor_zoom')

    def run(self):
        self.root.mkdir(parents=True,exist_ok=False)
        self.started=time.monotonic()
        self.deadline=self.started+self.plan['budget_seconds']
        watch=threading.Thread(target=self.watch,name='meter-teaching-watchdog')
        watch.daemon=True
        watch.start()
        try:
            self.io.preflight(self.plan)
            self.state.update(status='running',started_at=now())
            self.persist()
            current=self.io.position()
            self.move(dict(current,zoom=1.),'wide_zoom')
            source=self.plan['target']['references'][0]['source_pose']
            self.travel(source,'approach_source_view')
            self.observe('root_reference_check')
            matrix=self.calibration(1.)
            frame=self.center(matrix)
            for zoom in self.plan['zoom_steps']:
                self.move(dict(frame['pose'],zoom=zoom),'optical_zoom')
                frame=self.observe('post_zoom_reference_check')
                if not frame['centered']:
                    frame=self.center(self.calibration(zoom))
            taught=self.center()
            self.state['taught_pose']=dict(taught['pose'])
            self.state['teaching_frame']=taught['path']
            self.persist()
            self.return_home()
            self.event('independent_revisit_started', target=taught['pose'])
            self.travel(dict(taught['pose'],zoom=1.),'revisit_pan_tilt')
            for zoom in self.plan['zoom_steps']:
                self.move(dict(taught['pose'],zoom=zoom),'revisit_zoom')
            # Reference is the prior taught image. Two fresh frames after a
            # physical return-and-revisit are required; no archive replay.
            checked=[self.observe('independent_revisit_1'),self.observe('independent_revisit_2')]
            if not all(f['centered'] for f in checked):
                raise RuntimeError('revisit_center_outside_tolerance')
            evidence=self.io.evidence(checked[-1],self.root,self.plan['target']['instrument_id'])
            self.state.update(revisit_verified=True,evidence=evidence,
                              visit_pose=checked[-1]['pose'], reading=None)
            self.return_home()
            self.state.update(status='completed',restored_pose=self.io.position())
        except Exception as exc:
            self.state.update(status='stopped' if self.cancel.is_set() else 'failed',
                              error=type(exc).__name__+': '+str(exc))
        finally:
            try:self.stop()
            except Exception:self.state.update(status='failed',error='STOP_unconfirmed')
            self.done.set()
            watch.join(timeout=4)
            if self.stop_failed or watch.is_alive():
                self.state.update(status='failed',error='STOP_or_watchdog_unconfirmed')
            self.state.update(finished_at=now(), elapsed_seconds=time.monotonic()-self.started,
                              stop_confirmed=not self.stop_failed)
            self.persist()
        return copy.deepcopy(self.state)

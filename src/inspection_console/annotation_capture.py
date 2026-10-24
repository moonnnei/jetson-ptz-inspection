"""Annotation-seeded commissioning; old appearance is never an acquisition gate.

Python 3.5, no image dependencies or hardware defaults. A scene registration
transfers an existing annotation to a NEW image. Only subsequent fresh-image
checks may extend that seed. Encoder position alone never commissions a preset.
"""
import copy
import math
import threading
import time

from adaptive_capture import fit_zoom, inside_frame
from planner import pose
from references import digest, reference_plan
from ring_coordinates import parameters
from store import Store, now
from teaching import Teaching, centered


def scene_binding(state, map_id):
    """Bind scene/targets, not job progress or unrelated history revisions."""
    refs = reference_plan(state, map_id, state['revision'])
    refs.pop('revision')
    refs.pop('fingerprint')
    scene = Store.find(state, 'maps', map_id)
    return digest(dict(references=refs, overview=scene.get('overview'),
                       scene_status=scene['status']))


def coarse_pose(reference, projection):
    p = parameters(projection)
    if (reference['width'], reference['height'], reference['orientation']) != (
            p['source_width'], p['source_height'], p['source_orientation']):
        raise ValueError('annotation geometry and source image differ')
    source = pose(reference['source_pose'])
    if source['zoom'] != 1.:
        raise ValueError('coarse geometry requires a wide source view')
    box = reference['bbox']
    dx = (box[0]+box[2])/2.-reference['width']/2.
    dy = (box[1]+box[3])/2.-reference['height']/2.
    # Explicitly an estimated pinhole direction, refined by fresh observations.
    pan = source['pan']-p['yaw_image_sign']*math.degrees(math.atan(dx/p['focal_pixels']))
    tilt = source['tilt']-math.degrees(math.atan(dy/math.hypot(p['focal_pixels'], dx)))
    return dict(pan=round(max(0.,min(350.,pan)),1),
                tilt=round(max(0.,min(90.,tilt)),1), zoom=1.)


def annotation_plan(state, map_id, anchor, instrument_id, attempt_id, profile='tracked_v3'):
    if profile not in ('endpoint_v1','tracked_v2','tracked_v3'):
        raise ValueError('unknown bounded commissioning profile')
    refs = reference_plan(state, map_id, state['revision'])
    selected = [t for t in refs['targets'] if t['instrument_id']==instrument_id]
    if len(selected)!=1:raise ValueError('one enabled annotated target is required')
    scene = Store.find(state, 'maps', map_id)
    projection = parameters(scene['overview']['ring_projection'])
    target = copy.deepcopy(selected[0])
    ref = target['references'][0]
    if ref['source_kind']!='bounded_scan_capture' or ref['orientation']!='rotate180_from_raw':
        raise ValueError('a posed, oriented source annotation is required')
    anchor, source = pose(anchor), pose(ref['source_pose'])
    coarse = coarse_pose(ref, projection)
    neighbors = [coarse]
    for axis, offset in (('pan',-3.),('pan',3.),('tilt',-2.),('tilt',2.)):
        point = dict(coarse)
        limit = 350. if axis=='pan' else 90.
        point[axis] = round(max(0.,min(limit,point[axis]+offset)),1)
        if point not in neighbors:neighbors.append(point)
    result = dict(schema_version=1,kind='bounded_annotation_capture',map_id=map_id,
        revision=state['revision'],attempt_id=attempt_id,scene_binding=scene_binding(state,map_id),
        target=target,anchor=anchor,projection=projection,coarse_pose=coarse,search_views=neighbors,
        pan_limits=[max(0.,min(anchor['pan'],source['pan'],coarse['pan']-12.)),
                    min(350.,max(anchor['pan'],source['pan'],coarse['pan']+12.))],
        tilt_limits=[max(0.,min(anchor['tilt'],source['tilt'],coarse['tilt']-6.)),
                     min(90.,max(anchor['tilt'],source['tilt'],coarse['tilt']+6.))],
        zoom_limits=[1.,2.],max_transit_step_degrees=20.,max_servo_step_degrees=4.,
        proportional_gain=.7,center_tolerance=[.035,.04],max_center_iterations=3,
        max_appearance_step_degrees=4.,maximum_zoom_step=.3,
        frame_margin=.02,budget_seconds=180 if profile=='tracked_v3' else 150,maximum_captures=40,
        independent_revisit=True,old_template_is_acquisition_gate=False,
        manual_operation_policy='missed_target_boxes_only',
        return_on_success=True,return_after_failure=False,periodic_capture_enabled=False,
        execution_enabled=False,reading=None)
    if profile=='tracked_v3':
        result['validation_departure_strategy']='nearby_pose_then_saved_preset'
    elif profile=='endpoint_v1':
        result.pop('max_appearance_step_degrees')
        result.pop('maximum_zoom_step')
        result.update(budget_seconds=120,maximum_captures=22)
    result['fingerprint']=digest(result)
    return result


def validate_annotation(plan,state):
    if plan.get('scene_binding')!=scene_binding(state,plan['map_id']):
        raise ValueError('scene or target annotations changed')
    # Rebuild using the original revision: benign history progress is not a
    # reason to silently replace a prepared target or increase its envelope.
    snapshot=copy.deepcopy(state)
    snapshot['revision']=plan['revision']
    profile=('tracked_v3' if plan.get('validation_departure_strategy') else
             'tracked_v2' if 'max_appearance_step_degrees' in plan else 'endpoint_v1')
    expected=annotation_plan(snapshot,plan['map_id'],plan['anchor'],
                             plan['target']['instrument_id'],plan['attempt_id'],profile)
    if expected!=plan:raise ValueError('annotation capture plan changed')
    return copy.deepcopy(plan)


def quality_rank(frame):
    """Select among identity-checked images, never turn sharpness into identity."""
    if not inside_frame(frame):return float('-inf')
    q=frame['quality']
    if not all(math.isfinite(q[k]) for k in ('sharpness','clipped_fraction')):
        raise ValueError('non-finite image quality')
    box=frame['bbox']
    pixels=(box[2]-box[0])*(box[3]-box[1])
    return (math.log(max(1.,pixels))+.15*math.log(1.+max(0.,q['sharpness']))
            -2.*q['clipped_fraction']-.15*sum(abs(x) for x in frame['center_error']))


class AnnotatedCapture(Teaching):
    def __init__(self,io,directory,plan,cancel=None):
        Teaching.__init__(self,io,directory,plan,cancel)
        self.seeded=False
        self.good=[]
        self.state.update(identity_method='annotation_scene_transfer_then_fresh_appearance_chain',
            old_template_is_acquisition_gate=False,manual_review_required=False,
            preset_commissioned=False,geometry_calibrated=False)

    def capture(self,purpose):
        if len(self.state['captures'])>=self.plan['maximum_captures']:
            raise RuntimeError('capture_budget_exhausted')
        return Teaching.capture(self,purpose)

    def accept(self,frame,match):
        self.check()
        ok,error=centered(match['bbox'],frame['width'],frame['height'],self.plan['center_tolerance'])
        frame.update(bbox=match['bbox'],centered=ok,center_error=error,match=match)
        frame['quality']=self.io.quality(frame)
        self.check()
        self.reference=dict(image=frame['path'],sha256=frame['sha256'],bbox=frame['bbox'],
            width=frame['width'],height=frame['height'],source_pose=frame['pose'],
            orientation=frame['orientation'],local_capture=True)
        self.good.append(copy.deepcopy(frame))
        self.event('new_reference',frame=frame['path'],bbox=frame['bbox'],
                   basis=match.get('basis',match['status']),quality=frame['quality'])
        return frame

    def observe(self,purpose):
        frame=self.capture(purpose)
        match=self.io.locate(self.reference,frame)
        self.event('fresh_identity_check',frame=frame['path'],parent=self.reference['image'],result=match)
        if match['status']!='matched_reference':
            raise RuntimeError('target_uncertain:'+match['reason'])
        return self.accept(frame,match)

    def bootstrap(self):
        # Always acquire and persist first. Old-template appearance may be
        # diagnostic, but cannot prevent the seed photograph from being taken.
        frame=self.capture('annotation_seed_new_image')
        match=self.io.bootstrap(self.reference,frame)
        self.event('annotation_transfer',frame=frame['path'],result=match)
        if match['status']!='matched_reference':
            raise RuntimeError('annotation_seed_uncertain:'+match['reason'])
        self.accept(frame,match)
        self.seeded=True
        self.state['seed_basis']=match.get('basis')
        return self.observe('annotation_seed_second_fresh_check')

    def tracked_travel(self,target,phase):
        """Maintain a fresh appearance chain across a large coarse correction.

        A 35-degree endpoint jump can break a perfectly valid meter match due
        to parallax. Motion is still finite, but identity is checked every at
        most four degrees rather than after the entire transit.
        """
        start=self.io.position()
        count=max(1,int(math.ceil(max(abs(target[k]-start[k]) for k in ('pan','tilt'))/
                                 self.plan['max_appearance_step_degrees'])))
        frame=None
        for i in range(1,count+1):
            point=dict(target,**{k:round(start[k]+(target[k]-start[k])*i/count,1) for k in ('pan','tilt')})
            self.move(point,phase)
            frame=self.observe('tracked_coarse_step_fresh_check')
        return frame

    def refine(self,frame):
        for unused in range(self.plan['max_center_iterations']):
            if frame['centered']:break
            f=self.plan['projection']['focal_pixels']*frame['pose']['zoom']
            box=frame['bbox']
            errors=[(box[0]+box[2])/2.-frame['width']/2.,
                    (box[1]+box[3])/2.-frame['height']/2.]
            point=dict(frame['pose'])
            for axis,error,sign in (('pan',errors[0],self.plan['projection']['yaw_image_sign']),
                                    ('tilt',errors[1],1.)):
                step=-sign*self.plan['proportional_gain']*math.degrees(math.atan(error/f))
                step=max(-self.plan['max_servo_step_degrees'],min(self.plan['max_servo_step_degrees'],step))
                low,high=self.plan[axis+'_limits']
                point[axis]=round(max(low,min(high,point[axis]+step)),1)
            if all(abs(point[k]-frame['pose'][k])<.1 for k in ('pan','tilt')):
                self.event('reachable_residual',pose=frame['pose'],center_error=frame['center_error'])
                break
            self.move(point,'bounded_estimated_geometry_refinement')
            frame=self.observe('geometry_refinement_fresh_check')
        return frame

    def run(self):
        self.root.mkdir(parents=True,exist_ok=False)
        self.started=time.monotonic()
        self.deadline=self.started+self.plan['budget_seconds']
        watcher=threading.Thread(target=self.watch,name='annotation-capture-watchdog')
        watcher.daemon=True
        watcher.start()
        try:
            self.io.preflight(self.plan)
            self.state.update(status='running',started_at=now())
            self.persist()
            self.move(dict(self.io.position(),zoom=1.),'source_approach_wide')
            self.travel(self.plan['target']['references'][0]['source_pose'],'annotation_source_view')
            self.bootstrap()
            last=None
            for index,point in enumerate(self.plan['search_views']):
                # Only successfully checked NEW images extend the chain.
                try:last=self.tracked_travel(point,'bounded_annotation_neighbor_%d'%index)
                except RuntimeError as exc:
                    if not str(exc).startswith('target_uncertain:'):raise
                    self.event('neighbor_uncertain',index=index,reason=str(exc))
                    continue
                if inside_frame(last,self.plan['frame_margin']):break
            if last is None:raise RuntimeError('finite_neighbor_search_exhausted')
            last=self.refine(last)
            for unused in range(4):
                fitted=fit_zoom(last,2.,self.plan['frame_margin'])
                zoom=min(fitted,round(last['pose']['zoom']+self.plan['maximum_zoom_step'],1))
                if zoom<=last['pose']['zoom']+.05:break
                before=copy.deepcopy(last)
                ref=copy.deepcopy(self.reference)
                self.move(dict(last['pose'],zoom=zoom),'fitted_optical_zoom')
                try:
                    enlarged=self.observe('zoom_fresh_check')
                    if not inside_frame(enlarged,self.plan['frame_margin']):
                        raise RuntimeError('target_uncertain:frame_edge_after_zoom')
                    last=enlarged
                except RuntimeError as exc:
                    if not str(exc).startswith('target_uncertain:'):raise
                    self.reference=ref
                    self.move(before['pose'],'one_zoom_rollback')
                    last=self.observe('zoom_rollback_fresh_check')
                    break
            usable=[f for f in self.good if inside_frame(f,self.plan['frame_margin'])]
            if not usable:raise RuntimeError('no_identity_checked_in_frame_capture')
            best=max(usable,key=quality_rank)
            self.state.update(selected_frame=best['path'],selection_score=quality_rank(best),
                selection_method='identity_then_bounds_then_pixels_sharpness_exposure',
                taught_pose=best['pose'],teaching_frame=best['path'])
            self.reference=dict(image=best['path'],sha256=best['sha256'],bbox=best['bbox'],
                width=best['width'],height=best['height'],source_pose=best['pose'],
                orientation=best['orientation'],local_capture=True)
            if self.plan.get('validation_departure_strategy')=='nearby_pose_then_saved_preset':
                departure=dict(best['pose'],zoom=1.)
                low,high=self.plan['pan_limits']
                departure['pan']=round(best['pose']['pan']+(3. if high-best['pose']['pan']>=3. else -3.),1)
                if not low<=departure['pan']<=high:raise RuntimeError('no_independent_departure_margin')
                self.move(dict(self.io.position(),zoom=1.),'validation_departure_wide')
                self.travel(departure,'nearby_independent_validation_departure')
                departure=self.io.position()
            else:
                self.return_home()
                departure=self.io.position()
            if max(abs(departure[k]-best['pose'][k]) for k in ('pan','tilt'))<2.:
                # A target already at the anchor still needs a real departure;
                # two pictures without leaving are not a revisit experiment.
                point=dict(departure,zoom=1.)
                low,high=self.plan['pan_limits']
                point['pan']=round(departure['pan']+(3. if high-departure['pan']>=3. else -3.),1)
                if not low<=point['pan']<=high:raise RuntimeError('no_independent_departure_margin')
                self.travel(point,'independent_validation_departure')
                departure=self.io.position()
            self.state['independent_departure_pose']=departure
            self.event('independent_revisit_started',target=best['pose'])
            self.travel(dict(best['pose'],zoom=1.),'independent_revisit_pan_tilt')
            self.move(best['pose'],'independent_revisit_zoom')
            checks=[self.observe('independent_revisit_1'),self.observe('independent_revisit_2')]
            if not all(inside_frame(f,self.plan['frame_margin']) for f in checks):
                raise RuntimeError('independent_revisit_frame_edge')
            evidence=self.io.evidence(checks[-1],self.root,self.plan['target']['instrument_id'])
            self.state.update(revisit_verified=True,preset_commissioned=True,
                visit_pose=checks[-1]['pose'],evidence=evidence,matching_fresh_observations=2,
                center_status='centered' if all(f['centered'] for f in checks) else 'reachable_with_residual',
                frame_bounds_checked=True,physical_face_visibility='not_proven_by_bbox',reading=None)
            self.return_home()
            self.state.update(status='completed',restored_pose=self.io.position())
        except Exception as exc:
            self.state.update(status='stopped' if self.cancel.is_set() else 'failed',
                              error=type(exc).__name__+': '+str(exc),preset_commissioned=False)
        finally:
            try:self.stop()
            except Exception:self.state.update(status='failed',error='STOP_unconfirmed',preset_commissioned=False)
            self.done.set()
            watcher.join(timeout=4)
            if self.stop_failed or watcher.is_alive():
                self.state.update(status='failed',error='STOP_or_watchdog_unconfirmed',preset_commissioned=False)
            self.state.update(finished_at=now(),elapsed_seconds=time.monotonic()-self.started,
                              stop_confirmed=not self.stop_failed)
            self.persist()
        return copy.deepcopy(self.state)

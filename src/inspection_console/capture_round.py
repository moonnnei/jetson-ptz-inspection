"""Finite automatic revisit trial using prior photos, not verified visit poses.

No training, calibration, registry mutation or periodic execution occurs here.
The caller owns the shared hardware lease and explicit saved-plan execution.
"""
import copy
import math
import os
from pathlib import Path
import threading
import time

from planner import nearest_stops, pose
from references import digest, reference_plan
from store import bbox, now
from teaching import centered


def photo_reference(state, asset):
    commissioned=asset.get('commissioning')
    if commissioned:
        from annotation_capture import scene_binding
        if (asset['pose_status']!='verified' or not asset['visit_pose']
                or commissioned['scene_binding']!=scene_binding(state,asset['map_id'])):
            return None,'commissioned_scene_binding_changed'
        job=next((j for j in state['jobs'] if j['id']==commissioned['job_id']),None)
        if job is None and commissioned.get('capture_result'):
            pinned=commissioned['capture_result']
            if digest(pinned)!=commissioned.get('capture_result_fingerprint'):
                return None,'pinned_preset_evidence_changed'
            job=dict(id=commissioned['job_id'],mode=commissioned.get('source_job_mode'),
                status=commissioned.get('source_job_status'),
                stop_confirmed=commissioned.get('source_job_stop_confirmed'),results=[pinned])
        result=next((r for r in job['results'] if r['instrument_id']==asset['id']),None) if job else None
        if (not job or job['mode']!='annotation_commissioning' or job['status']!='completed'
                or not job.get('stop_confirmed') or not result or result['status']!='preset_commissioned'
                or result.get('matching_fresh_observations')!=2
                or result['image_sha256']!=commissioned['image_sha256']
                or commissioned.get('capture_result_fingerprint',digest(result))!=digest(result)
                or pose(result['pose'])!=pose(asset['visit_pose'])):
            return None,'commissioning_evidence_missing_or_changed'
        ref=dict(image=result['image'],sha256=result['image_sha256'],width=2560,height=1440,
            bbox=bbox(result['bbox'],2560,1440),orientation=result['orientation'],
            source_pose=pose(result['pose']),source_kind='annotation_commissioning',
            selection='automatic_independent_revisit')
        return dict(id=asset['id'],name=asset['name'],pose=pose(asset['visit_pose']),reference=ref,
            capture_job_id=job['id'],capture_result_fingerprint=digest(result),
            pose_basis='software_preset_after_independent_revisit',visit_pose_verified=True),None
    for job in reversed(state['jobs']):
        if job['map_id'] != asset['map_id'] or not job.get('stop_confirmed'):
            continue
        for result in job['results']:
            if result.get('instrument_id') != asset['id']:
                continue
            if (job.get('mode')=='first_capture_batch' and
                    result.get('status')=='first_capture_reference_ready' and
                    result.get('independent_revisit_checked') is True and
                    result.get('reference_observations')==2 and
                    result.get('orientation')=='rotate180_from_raw'):
                source_pose=pose(result['pose'])
                image=result['image'];sha=result['image_sha256']
                if image!=sha+'.jpg' and image!=sha+'.png':raise ValueError('invalid first capture reference name')
                ref=dict(image=image,sha256=sha,width=2560,height=1440,
                    bbox=bbox(result['bbox'],2560,1440),orientation='rotate180_from_raw',
                    source_pose=source_pose,source_kind='first_capture_batch',
                    selection='first_capture_appearance_and_center_checked')
                return dict(id=asset['id'],name=asset['name'],pose=source_pose,reference=ref,
                    capture_job_id=job['id'],capture_result_fingerprint=digest(result),
                    pose_basis='first_capture_quality_review_pending',visit_pose_verified=False),None
            automatic=(job['status']=='partial_capture_completed' and
                       result.get('image_status')=='constrained_capture_not_verified_visit')
            manual=(job['status']=='reviewed_capture' and
                    result.get('image_status')=='fixed_angle_photo_accepted_by_human' and
                    result.get('manual_review',{}).get('status')=='accepted')
            if not (automatic or manual):continue
            region=result.get('bbox') if automatic else result.get('reference_bbox',{}).get('bbox')
            if region is None:
                return None,'accepted_photo_reference_box_required'
            source_pose=pose(result['pose'])
            if source_pose['zoom']!=2.:return None,'two_times_capture_reference_required'
            image=result['image'];sha=image.rsplit('.',1)[0]
            # Exact S6 pilot format; IO verifies actual content and dimensions.
            ref=dict(image=image,sha256=sha,width=2560,height=1440,
                bbox=bbox(region,2560,1440),orientation='rotate180_from_raw',
                source_pose=source_pose,source_kind='prior_limited_capture',
                selection='human_confirmed_photo_crop' if manual else 'previous_appearance_checked_capture')
            return dict(id=asset['id'],name=asset['name'],pose=source_pose,reference=ref,
                capture_job_id=job['id'],capture_result_fingerprint=digest(result),
                pose_basis='unverified_previous_capture',visit_pose_verified=False),None
    return None,'first_capture_required'


def round_plan(state,map_id,anchor):
    anchor=pose(anchor)
    references=reference_plan(state,map_id,state['revision'])
    assets={a['id']:a for a in state['instruments'] if a['map_id']==map_id and a['enabled']}
    stops=[];blocked=[]
    for target in references['targets']:
        stop,reason=photo_reference(state,assets[target['instrument_id']])
        if reason:blocked.append(dict(id=target['instrument_id'],reason=reason))
        else:stops.append(stop)
    if len(stops)>12:raise ValueError('a finite trial supports at most 12 instruments')
    ordered,cost=nearest_stops(stops,anchor)
    points=[anchor]+[s['pose'] for s in ordered]
    plan=dict(schema_version=1,kind='bounded_uncommissioned_capture_round',
        map_id=map_id,revision=state['revision'],anchor=anchor,
        confirmed_references=references,stops=ordered,blocked=blocked,
        algorithm='nearest_neighbor_bounded_axes',angular_cost=round(cost,4),
        cost_includes_zoom=False,optimality_claim=False,
        pan_limits=[max(0.,min(p['pan'] for p in points)-1.),min(350.,max(p['pan'] for p in points)+1.)],
        tilt_limits=[max(0.,min(p['tilt'] for p in points)-1.),min(90.,max(p['tilt'] for p in points)+1.)],
        zoom_limits=[1.,2.],max_transit_step_degrees=20.,
        appearance_strategy='saved_pose_roi_v1',
        frames_per_target=2,budget_seconds=min(600,60+45*len(ordered)),
        return_between_targets=False,return_once_on_normal_finish=True,
        return_after_hardware_failure=False,allow_center_residual=True,
        uncertainty_policy='record_and_continue',hardware_failure_policy='stop_round',
        ready_for_trial=bool(ordered) and not blocked,execution_enabled=False,
        visit_pose_verified=False,periodic_capture_enabled=False,reading=None)
    plan['fingerprint']=digest(plan)
    return plan


def validate_round(plan,state):
    if plan!=round_plan(state,plan['map_id'],plan['anchor']):
        raise ValueError('capture round or review binding changed')
    return copy.deepcopy(plan)


class CaptureRound:
    def __init__(self,io,directory,plan,cancel=None,on_result=None):
        self.io,self.root,self.plan=io,Path(directory),copy.deepcopy(plan)
        self.on_result=on_result
        self.cancel=cancel or threading.Event();self.done=threading.Event()
        self.lock=threading.RLock();self.stop_failed=False
        self.state=dict(kind=plan['kind'],plan=copy.deepcopy(plan),status='prepared',
            events=[],captures=[],results=[],reading=None,visit_pose_verified=False,
            periodic_capture_enabled=False,image_time_basis='host_receive_not_exposure')

    def persist(self):
        import json
        with self.lock:
            temporary=self.root/'record.tmp'
            temporary.write_text(json.dumps(self.state,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
            os.replace(str(temporary),str(self.root/'record.json'))

    def event(self,kind,**fields):
        with self.lock:
            fields.update(event=kind,at=now());self.state['events'].append(fields);self.persist()

    def check(self):
        if self.cancel.is_set() or time.monotonic()>self.deadline or self.stop_failed:
            self.cancel.set();raise RuntimeError('capture_round_cancelled_timed_out_or_STOP_failed')

    def stop(self):
        try:self.io.stop()
        except Exception:
            self.stop_failed=True;self.cancel.set();raise RuntimeError('STOP_unconfirmed')

    def watch(self):
        while not self.done.wait(.25):
            try:
                invalidated=(hasattr(self.io,'review_changed') and self.io.review_changed())
                must_stop=invalidated or time.monotonic()>self.deadline or self.cancel.is_set()
            except Exception:must_stop=True
            if must_stop:
                self.cancel.set()
                try:self.stop()
                except Exception:pass
                return

    def measured(self,phase,action,*args):
        started=time.monotonic()
        try:return action(*args)
        finally:
            value=self.state.setdefault('phase_timings',{}).setdefault(phase,dict(seconds=0.,calls=0))
            value['seconds']+=time.monotonic()-started;value['calls']+=1

    def move(self,target,phase):
        self.check();self.measured('move_and_settle',self.io.move,pose(target),self.cancel);self.check()
        self.event('move',phase=phase,actual=self.io.position())

    def travel(self,target,phase):
        start=self.io.position();target=pose(target)
        count=max(1,int(math.ceil(max(abs(target[k]-start[k]) for k in ('pan','tilt'))/self.plan['max_transit_step_degrees'])))
        for i in range(1,count+1):
            point=dict(target,**{k:round(start[k]+(target[k]-start[k])*i/count,1) for k in ('pan','tilt')})
            if any(abs(point[k]-self.io.position()[k])>.051 for k in ('pan','tilt','zoom')):
                self.move(point,phase)

    def visit(self,target):
        self.state['active_target']=target['id'];self.persist()
        current=self.io.position()
        same_position=all(abs(current[k]-target['pose'][k])<=.15 for k in ('pan','tilt'))
        if not same_position:
            if current['zoom']!=1.:self.move(dict(current,zoom=1.),'wide_before_transit')
            self.travel(dict(target['pose'],zoom=1.),'direct_target_transit')
        if any(abs(self.io.position()[k]-target['pose'][k])>t for k,t in (('pan',.15),('tilt',.15),('zoom',.04))):
            self.move(target['pose'],'restore_capture_zoom')
        else:self.event('same_pose_move_skipped',instrument_id=target['id'])
        passed=[];last=None
        attempts=0;quality_retries=self.plan.get('maximum_quality_retries',0)
        while len(passed)<self.plan['frames_per_target'] and attempts<self.plan['frames_per_target']+quality_retries:
            attempts+=1
            self.check()
            frame=self.measured('capture_and_normalize',self.io.capture,self.root,len(self.state['captures'])+1,self.cancel)
            self.check()
            if frame['received_monotonic']<self.started or time.monotonic()-frame['received_monotonic']>15:
                raise RuntimeError('stale_capture')
            if (any(f['path']==frame['path'] for f in self.state['captures']) or
                    passed and frame['received_monotonic']<passed[-1]['received_monotonic']):
                raise RuntimeError('capture_order_not_fresh')
            if any(abs(frame['pose'][k]-target['pose'][k])>v for k,v in (('pan',.3),('tilt',.3),('zoom',.05))):
                raise RuntimeError('capture_pose_not_reached')
            frame.update(instrument_id=target['id'],purpose='independent_photo_reference_check')
            self.state['captures'].append(frame);self.persist();last=frame
            match=self.measured('identity_match',self.io.locate,target['reference'],frame);self.check()
            frame['match']=match
            self.event('appearance_check',instrument_id=target['id'],frame=frame['path'],result=match)
            if match['status']!='matched_reference':
                return dict(instrument_id=target['id'],status='uncertain',
                    reason=match.get('reason','appearance_rejected'),image=frame['path'],
                    image_sha256=frame['sha256'],captured_at=frame['captured_at'],pose=frame['pose'],
                    image_status='round_unmatched_diagnostic_not_accepted',reading=None,visit_pose_verified=False)
            region=bbox(match['bbox'],frame['width'],frame['height'])
            ok,error=centered(region,frame['width'],frame['height'],[.035,.04])
            frame.update(bbox=region,centered=ok,center_error=error)
            if self.plan.get('quality_policy'):
                if not hasattr(self.io,'assess'):raise ValueError('image quality adapter missing')
                assessment=self.measured('quality_check',self.io.assess,target['reference'],frame);self.check()
                frame['quality_assessment']=assessment
                self.event('image_quality_check',instrument_id=target['id'],frame=frame['path'],result=assessment)
                if not assessment['accepted']:
                    if attempts<self.plan['frames_per_target']+quality_retries:
                        if hasattr(self.io,'quality_retry'):self.io.quality_retry(self.cancel)
                        continue
                    break
            passed.append(frame)
            self.persist()
        if len(passed)<self.plan['frames_per_target']:
            return dict(instrument_id=target['id'],status='uncertain',reason='image_quality_not_sufficient',
                image=last['path'],image_sha256=last['sha256'],captured_at=last['captured_at'],pose=last['pose'],
                quality=last.get('quality_assessment'),image_status='quality_diagnostic_not_accepted',reading=None,visit_pose_verified=False)
        if self.plan.get('quality_policy'):
            last=max(passed,key=lambda f:f['quality_assessment']['current_metrics']['sharpness'])
        evidence=self.measured('evidence_and_crop',self.io.evidence,last,self.root,target['id']);self.check()
        return dict(instrument_id=target['id'],status='captured_reference_checked',
            image=last['path'],image_sha256=last['sha256'],pose=last['pose'],bbox=last['bbox'],
            captured_at=last['captured_at'],center_error=last['center_error'],optical_zoom=last['pose']['zoom'],
            image_status='one_shot_reference_checked_unverified_visit',
            evidence=evidence,matching_fresh_observations=len(passed),quality=last.get('quality_assessment'),
            reading=None,visit_pose_verified=False)

    def run(self):
        if not self.plan['ready_for_trial']:raise ValueError('round has blocked targets; prepare references first')
        self.root.mkdir(parents=True,exist_ok=False)
        self.started=time.monotonic();self.deadline=self.started+self.plan['budget_seconds']
        self.state.update(status='running',started_at=now())
        watcher=threading.Thread(target=self.watch,name='capture-round-watchdog');watcher.daemon=True;watcher.start()
        try:
            self.check();self.measured('preflight',self.io.preflight,self.plan);self.persist()
            for target in self.plan['stops']:
                visit_started=time.monotonic()
                self.event('target_started',instrument_id=target['id'])
                result=self.visit(target)
                result['elapsed_seconds']=time.monotonic()-visit_started
                self.state['results'].append(result)
                self.event('target_finished',instrument_id=target['id'],status=result['status'],
                           elapsed_seconds=result['elapsed_seconds'])
                if self.on_result:
                    try:self.on_result(copy.deepcopy(result))
                    except Exception:
                        # Display progress cannot change the capture/STOP policy.
                        self.event('result_preview_unavailable',instrument_id=target['id'])
            current=self.io.position()
            anchor=self.plan['anchor']
            if any(abs(current[k]-anchor[k])>.15 for k in ('pan','tilt')):
                if current['zoom']!=1.:self.move(dict(current,zoom=1.),'final_return_wide')
                self.travel(dict(anchor,zoom=1.),'return_once_after_round')
            if abs(self.io.position()['zoom']-anchor['zoom'])>.04:self.move(anchor,'restore_anchor_zoom')
            self.state.update(status='completed' if all(r['status']=='captured_reference_checked' for r in self.state['results']) else 'completed_with_errors',
                              active_target=None,restored_pose=self.io.position())
        except Exception as exc:
            self.state.update(status='stopped' if self.cancel.is_set() else 'failed',error=type(exc).__name__+': '+str(exc))
        finally:
            try:self.stop()
            except Exception:self.state.update(status='failed',error='STOP_unconfirmed')
            self.done.set();watcher.join(timeout=4)
            if self.stop_failed or getattr(self.io,'hard_stop_failed',False) or watcher.is_alive():
                self.stop_failed=True;self.state.update(status='failed',error='STOP_or_watchdog_unconfirmed')
            self.state.update(finished_at=now(),elapsed_seconds=time.monotonic()-self.started,
                              stop_confirmed=not self.stop_failed)
            self.persist()
        return copy.deepcopy(self.state)

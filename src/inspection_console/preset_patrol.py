"""Finite patrol using automatically commissioned software presets.

Does not infer positions from old panoramas during regular visits. New scene
annotations invalidate the binding and require a new commissioning attempt.
"""
import copy
from pathlib import Path
import threading

from annotation_capture import scene_binding
from capture_round import CaptureRound,round_plan
from references import digest,read_reference_image
from ring_capture import close_pose
from scan_capture import ISAPIScanIO
from teaching_io import TeachingIO
from store import Store


def preset_plan(state,map_id,anchor,attempt_id):
    plan=round_plan(state,map_id,anchor)
    plan.pop('fingerprint')
    plan.update(kind='bounded_software_preset_patrol',attempt_id=attempt_id,
        scene_binding=scene_binding(state,map_id),budget_seconds=max(180,min(600,60+45*len(plan['stops']))),
        maximum_targets=12,source_presets_commissioned=True,
        quality_policy='relative_picture_checks_v1',maximum_quality_retries=1,
        readiness_scope='repeatable_image_capture_not_numeric_reading')
    if (plan['blocked'] or not plan['stops'] or
            not all(s.get('visit_pose_verified') is True for s in plan['stops'])):
        raise ValueError('all enabled targets need independent commissioned software presets')
    if not next(m for m in state['maps'] if m['id']==map_id)['motion_verified']:
        raise ValueError('scene commissioning is incomplete')
    plan['fingerprint']=digest(plan)
    return plan


def validate_preset(plan,state):
    snapshot=copy.deepcopy(state)
    snapshot['revision']=plan['revision']
    if plan!=preset_plan(snapshot,plan['map_id'],plan['anchor'],plan['attempt_id']):
        raise ValueError('software preset or scene binding changed')
    return copy.deepcopy(plan)


class PresetIO(TeachingIO):
    def __init__(self,camera,project_root,console_root,plan,job_id=None,interlocks=None):
        TeachingIO.__init__(self,camera,project_root,console_root,None)
        self.plan=plan
        self.job_id=job_id
        self.interlocks=interlocks

    def guard(self,cancel):
        if cancel.is_set() or self.hard_stop_failed:raise RuntimeError('cancelled_or_STOP_unconfirmed')
        state=self.state()
        validate_preset(self.plan,state)
        running=[j['id'] for j in state['jobs'] if j['status']=='running']
        if (self.job_id is None and (running or state['schedule']['enabled'])
                or self.job_id is not None and running!=[self.job_id]):
            raise RuntimeError('patrol_job_ownership_changed')
        if self.interlocks:self.interlocks()

    def preflight(self,plan):
        self.plan=validate_preset(plan,self.state())
        self.guard(threading.Event())
        report=ISAPIScanIO.preflight(self)
        if not close_pose(report['anchor'],plan['anchor']):raise ValueError('patrol_anchor_changed')
        for target in plan['stops']:
            read_reference_image(target['reference'],self.console/'var/inspection-console/images')
        return report

    def locate(self,reference,frame):
        return self.vision(dict(action='locate_saved_pose',source=str(self.source_path(reference)),
            target=str(self.capture_root/frame['path']),bbox=reference['bbox'],
            source_pose=reference['source_pose'],target_pose=frame['pose']))

    def assess(self,reference,frame):
        from image_quality import assess
        metrics=self.vision(dict(action='capture_quality',source=str(self.source_path(reference)),
            target=str(self.capture_root/frame['path']),reference_bbox=reference['bbox'],bbox=frame['bbox']))
        return assess(metrics['reference'],metrics['current'],frame['bbox'],frame['width'],frame['height'],frame['center_error'])

    def quality_retry(self,cancel):
        self.guard(cancel)
        if cancel.wait(.8):raise RuntimeError('quality_retry_cancelled')

    def review_changed(self):
        try:self.guard(threading.Event());return False
        except Exception:return True

    def move(self,target,cancel):
        self.guard(cancel)
        if close_pose(self.position(),target):self.stop();return
        return TeachingIO.move(self,target,cancel)


def result_for_registry(store,root,result):
    """Save a new photograph; the scheduler owns job mutation and completion."""
    item=copy.deepcopy(result)
    saved=store.save_image(Path(root)/item['image'],item['image_sha256'])
    item['image']=saved['image']
    evidence=item.get('evidence') or {}
    if evidence.get('crop') and evidence.get('crop_sha256'):
        source=Path(root)/evidence['crop']
        if source.is_symlink() or source.resolve().parent!=Path(root).resolve():
            raise ValueError('crop leaves capture directory')
        cropped=store.save_image(source,evidence['crop_sha256'])
        item.update(crop_image=cropped['image'],crop_sha256=cropped['sha256'],
                    crop_source_sha256=item['image_sha256'],crop_bbox=copy.deepcopy(item.get('bbox')))
    passed=item['status']=='captured_reference_checked'
    item.update(status='captured' if passed else 'uncertain',
        source_preset_verified=True,visit_pose_verified=passed,
        image_status='software_preset_fresh_reference_checked' if passed else 'software_preset_uncertain_diagnostic',reading=None)
    if item['status']=='captured':
        item['capture_id']=item['image_sha256'][:24]+'-'+item['captured_at']
    return item


def publish_progress(store,root,key,result):
    """Expose a per-target preview; it is never an accepted scheduler result."""
    item=result_for_registry(store,root,result)
    item['pending_round_completion']=True
    def apply(state):
        job=Store.find(state,'jobs',key)
        if job['status']!='running':return
        if item['instrument_id'] not in job['route']:raise ValueError('unexpected progress target')
        rows=job.setdefault('progress_results',[])
        if any(r['instrument_id']==item['instrument_id'] for r in rows):raise ValueError('duplicate progress target')
        rows.append(item)
    store.change(None,'inspection_image_preview',dict(id=key,instrument_id=item['instrument_id']),apply)


class SoftwarePresetAdapter:
    mode='software_preset_capture'

    def __init__(self,store,camera,project_root,console_root):
        self.store,self.camera=store,camera
        self.root,self.console=Path(project_root),Path(console_root)
        self.lock=threading.RLock()
        self.active=None
        self.stop_failed=False
        self.last_summary=None

    def ready(self):
        return self.camera is not None and not self.stop_failed

    def round_budget(self,target_count):
        return max(180,min(600,60+45*target_count))

    def summary(self,job_id):
        with self.lock:
            return copy.deepcopy(self.last_summary['metrics']) if self.last_summary and self.last_summary['job_id']==job_id else None

    def position(self):
        from capture_review_views import motion_lease
        with motion_lease():
            return ISAPIScanIO(self.camera).position()

    def stop(self):
        with self.lock:active=self.active
        if active is None:
            if self.stop_failed:raise RuntimeError('STOP_unconfirmed')
            return
        active.cancel.set()
        try:active.stop()
        except Exception:self.stop_failed=True;raise

    def run_round(self,key,targets,cancel):
        from capture_review_views import motion_lease,check_other_services
        from store import identifier
        state=self.store.snapshot()
        map_id=targets[0]['map_id']
        root=self.root/'var/software-patrol'/key
        with self.lock:self.last_summary=None
        with motion_lease():
            check_other_services()
            anchor=ISAPIScanIO(self.camera).preflight()['anchor']
            plan=preset_plan(state,map_id,anchor,identifier('patrol-attempt'))
            if set(t['id'] for t in plan['stops'])!=set(t['id'] for t in targets):
                raise ValueError('scheduler and preset target lists differ')
            io=PresetIO(self.camera,self.root,self.console,plan,key,check_other_services)
            run=CaptureRound(io,root,plan,cancel,on_result=lambda r:publish_progress(self.store,root,key,r))
            with self.lock:self.active=run
            try:record=run.run()
            finally:
                with self.lock:self.active=None
            checks=[e for e in record['events'] if e['event']=='image_quality_check']
            metrics=dict(fresh_capture_requests=len(record['captures']),quality_checks=len(checks),
                quality_rejections=sum(not e['result']['accepted'] for e in checks),
                quality_passes=sum(e['result']['accepted'] for e in checks),
                capture_elapsed_seconds=record['elapsed_seconds'],stop_confirmed=record['stop_confirmed'],
                returned_pose=record.get('restored_pose'),phase_timings=record.get('phase_timings',{}))
            with self.lock:self.last_summary=dict(job_id=key,metrics=metrics)
            if not record['stop_confirmed']:
                self.stop_failed=True;raise RuntimeError('STOP_unconfirmed')
            if record['status'] not in ('completed','completed_with_errors'):
                raise RuntimeError('finite_preset_patrol_'+record['status'])
            rows=[result_for_registry(self.store,root,r) for r in record['results']]
        # Release the motion lease only after the round returned and stopped.
        for row in rows:yield row

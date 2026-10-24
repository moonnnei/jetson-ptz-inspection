"""Optional finite automatic registration, including queued human missed boxes.

No camera by default. The server explicitly enables this for a fixed site.
One queued item produces at most one bounded attempt; uncertainty is retained,
never converted into a request for a person to approve an image or type a pose.
"""
import copy
from pathlib import Path
import threading

from annotation_capture import AnnotatedCapture,annotation_plan
from annotation_history import import_annotation_capture
from classified_annotation_io import ClassifiedAnnotationIO
from references import read_reference_image
from registry_annotations import (queue_box,materialize,finish_queue,
    direction_difference,merge_commissioned_duplicate)
from region_detector import iou
from scan_capture import ISAPIScanIO
from store import Store,identifier


class AnnotationService:
    def __init__(self,store,camera=None,project_root=None,console_root=None,detector=None):
        self.store,self.camera=store,camera
        self.root=Path(project_root) if project_root else None
        self.console=Path(console_root) if console_root else None
        self.detector=detector
        self.enabled=camera is not None
        self.lock=threading.RLock();self.thread=None;self.cancel=threading.Event()
        self.capture=None;self.outcome=None;self.stop_error=None

    def status(self):
        with self.lock:
            state=self.store.snapshot()
            queue=state.get('annotation_queue',[])
            return dict(available=self.enabled,automatic_model_recheck=self.detector is not None,
                active=self.thread is not None and self.thread.is_alive(),
                paused=state.get('annotation_paused',False),
                queued=sum(q['state']=='queued' for q in queue),
                uncertain=sum(q['state'] in ('uncertain','interrupted') for q in queue),
                duplicates=sum(q['state']=='duplicate' for q in queue),
                outcome=copy.deepcopy(self.outcome),stop_error=self.stop_error,
                manual_operation_policy='missed_target_boxes_only')

    def queue(self,revision,frame_id,box):
        if not self.enabled:raise ValueError('automatic registration is not connected')
        state=self.store.snapshot();frame=Store.find(state,'frames',frame_id)
        scene=Store.find(state,'maps',frame['map_id'])
        if scene.get('review_before_commissioning'):
            return self.store.add_candidate(revision,frame_id,box)
        return queue_box(self.store,revision,frame_id,box)

    def discover(self,revision,map_id,candidate_ids=None):
        if not self.enabled or self.detector is None:raise ValueError('meter detector is not connected')
        state=self.store.snapshot()
        if state['revision']!=revision:raise ValueError('scene revision changed')
        selected=sorted([c for c in state['candidates'] if c['map_id']==map_id
            and c['state']=='pending' and c.get('confidence') is not None
            and c['confidence']>=.25 and not c['source'].startswith('human_')],
            key=lambda c:(-c['confidence'],c['id']))
        if candidate_ids is not None:
            if (not isinstance(candidate_ids,list) or not 1<=len(candidate_ids)<=40
                    or len(set(candidate_ids))!=len(candidate_ids)):
                raise ValueError('select 1-40 distinct model proposals')
            if not set(candidate_ids)<={c['id'] for c in selected}:
                raise ValueError('selected proposal is not pending in this scene')
            selected=[c for c in selected if c['id'] in candidate_ids]
        already={q.get('candidate_id') for q in state.get('annotation_queue',[]) if q['state'] not in ('cancelled','excluded')}
        selected=[c for c in selected if c['id'] not in already]
        deferred=max(0,len(selected)-40)
        selected=selected[:40]
        queued=[]
        for candidate in selected:
            result=queue_box(self.store,self.store.snapshot()['revision'],candidate['frame_id'],
                candidate['bbox'],'automatic',candidate['id'])
            queued.append(result['result']['queued_id'])
        return dict(queued=queued,manual_confirmation_required=False,
                    maximum_attempts=len(queued),per_attempt_seconds=180,deferred_proposals=deferred)

    def tick(self):
        if not self.enabled or self.stop_error:return
        with self.lock:
            if self.thread is not None and self.thread.is_alive():return
            state=self.store.snapshot()
            initialization=state.get('initialization') or {}
            if initialization.get('status') in ('scanning','detecting','mapping','importing'):return
            if state.get('annotation_paused'):return
            if state['schedule']['enabled'] or any(j['status']=='running' for j in state['jobs']):return
            queued=next((q for q in state.get('annotation_queue',[]) if q['state']=='queued'),None)
            if queued is None:
                scene=next((m for m in state['maps'] if m.get('automatic_registration_requested')
                            and m['status']!='invalidated'),None) if self.detector else None
                if scene is None:return
                def consume(s):
                    Store.find(s,'maps',scene['id'])['automatic_registration_requested']=False
                self.store.change(state['revision'],'automatic_discovery_requested',dict(map_id=scene['id']),consume)
                try:self.outcome=self.discover(self.store.snapshot()['revision'],scene['id'])
                except Exception as exc:self.outcome=dict(status='uncertain',error_type=type(exc).__name__)
                return
            self.cancel=threading.Event();self.capture=None
            self.thread=threading.Thread(target=self._run,args=(queued['id'],),name='automatic-annotation-registration')
            self.thread.daemon=True;self.thread.start()

    def _deduplicate(self,asset,io):
        state=self.store.snapshot()
        projection=Store.find(state,'maps',asset['map_id'])['overview']['ring_projection']
        duplicates=[]
        for other in state['instruments']:
            if (other['id']==asset['id'] or other['map_id']!=asset['map_id'] or
                    not other['enabled'] or other['pose_status']!='verified' or not other.get('commissioning')):continue
            difference=direction_difference(asset,other,projection)
            if difference>3.:continue
            a,b=other['commissioning'],asset['commissioning']
            directory=self.console/'var/inspection-console/images'
            for c in (a,b):
                read_reference_image(dict(image=c['image'],sha256=c['image_sha256'],width=2560,height=1440),directory)
            match=io.vision(dict(action='duplicate_reference',source=str(directory/a['image']),target=str(directory/b['image']),
                bbox=a['bbox'],source_pose=other['visit_pose'],target_pose=asset['visit_pose']))
            if match['status']=='matched_reference' and iou(match['bbox'],b['bbox'])>=.5:
                match.update(overlap_iou=iou(match['bbox'],b['bbox']),direction_difference_degrees=difference,
                    source_sha256=a['image_sha256'],target_sha256=b['image_sha256'])
                duplicates.append((other['id'],match))
        if len(duplicates)>1:raise ValueError('duplicate identity is ambiguous')
        if duplicates:
            keeper,evidence=duplicates[0]
            merge_commissioned_duplicate(self.store,self.store.snapshot()['revision'],asset['id'],keeper,evidence)
            return keeper
        return None

    def _run(self,queue_id):
        from capture_review_views import motion_lease,check_other_services
        materialized=False
        try:
            with motion_lease():
                check_other_services()
                state=self.store.snapshot()
                queued=next(q for q in state['annotation_queue'] if q['id']==queue_id)
                result=materialize(self.store,state['revision'],queue_id)['result']
                if result.get('duplicate_of'):
                    self.outcome=dict(status='duplicate',queue_id=queue_id,duplicate_of=result['duplicate_of'])
                    return
                materialized=True
                target=result['instrument_id']
                state=self.store.snapshot()
                anchor=ISAPIScanIO(self.camera).preflight()['anchor']
                plan=annotation_plan(state,queued['map_id'],anchor,target,identifier('annotation-attempt'))
                output=self.root/'var/auto-annotations'/queue_id
                used=self.root/'var/annotation-plans-used';used.mkdir(parents=True,exist_ok=True)
                with (used/plan['fingerprint']).open('x',encoding='utf8') as f:f.write(plan['attempt_id'])
                io=ClassifiedAnnotationIO(self.camera,self.root,self.console,self.detector,check_other_services)
                self.capture=AnnotatedCapture(io,output,plan,self.cancel)
                record=self.capture.run()
                imported=import_annotation_capture(self.store,output,self.store.snapshot()['revision'])
                status='completed' if record['status']=='completed' else 'uncertain'
                keeper=None
                if status=='completed':
                    asset=Store.find(self.store.snapshot(),'instruments',target)
                    keeper=self._deduplicate(asset,io)
                    if keeper:status='duplicate'
                finish_queue(self.store,self.store.snapshot()['revision'],queue_id,status,imported['result']['job_id'])
                self.outcome=dict(status=status,queue_id=queue_id,instrument_id=target,duplicate_of=keeper,
                    elapsed_seconds=record['elapsed_seconds'],stop_confirmed=record['stop_confirmed'])
                if not record['stop_confirmed']:self.stop_error='STOP_unconfirmed'
        except Exception as exc:
            if materialized:
                try:finish_queue(self.store,self.store.snapshot()['revision'],queue_id,'uncertain')
                except Exception:pass
            else:
                # Do not spin on a failing queued item on every scheduler tick.
                def failed(s):
                    entry=next(q for q in s['annotation_queue'] if q['id']==queue_id)
                    if entry['state']=='queued':entry.update(state='uncertain',error_type=type(exc).__name__)
                try:self.store.change(None,'annotation_prepare_failed',dict(queue_id=queue_id),failed)
                except Exception:pass
            self.outcome=dict(status='uncertain',queue_id=queue_id,error_type=type(exc).__name__)

    def stop(self):
        self.store.change(None,'automatic_annotation_pause',{},lambda s:s.update(annotation_paused=True))
        self.cancel.set()
        if self.thread is not None and self.thread.is_alive() and self.capture:
            try:self.capture.stop()
            except Exception:self.stop_error='STOP_unconfirmed';raise
        return dict(cancel_requested=True)

    def resume(self,revision):
        if not self.enabled or self.stop_error:raise ValueError('automatic registration is unavailable')
        return self.store.change(revision,'automatic_annotation_resume',{},lambda s:s.update(annotation_paused=False))

    def recover(self):
        """Pause every pending motion source before the scheduler can tick."""
        if not self.enabled:return
        state=self.store.snapshot()
        pending=[q for q in state.get('annotation_queue',[]) if q['state'] in ('queued','commissioning')]
        discovery=any(m.get('automatic_registration_requested') and m['status']!='invalidated'
                      for m in state['maps'])
        if not pending and not discovery:return
        # Persist first: even a failing camera STOP must not expose another
        # queued box or an unconsumed discovery request to automatic replay.
        if not state.get('annotation_paused'):
            self.store.change(None,'automatic_annotation_restart_pause',{},
                              lambda s:s.update(annotation_paused=True))
        entries=[q for q in pending if q['state']=='commissioning']
        if not entries:return
        from capture_review_views import motion_lease
        try:
            with motion_lease():
                self.camera.stop()
                for entry in entries:
                    finish_queue(self.store,self.store.snapshot()['revision'],entry['id'],'uncertain')
        except Exception:
            self.stop_error='restart_STOP_or_recovery_unconfirmed'

    def close(self):
        self.cancel.set()
        if self.thread:
            self.thread.join(timeout=20)
            if self.thread.is_alive():raise RuntimeError('annotation_worker_shutdown_unconfirmed')

"""Persistent one-button initialization. Default has no camera/backend.

Scanning and processing are finite; commissioning reuses the existing bounded
annotation queue. No successful result is fabricated when candidates fail.
"""
import copy
from pathlib import Path
import threading
import time

from store import Conflict,Store,identifier,now

ACTIVE=('scanning','detecting','mapping','importing','commissioning')


class SceneInitialization:
    def __init__(self,store,annotations,backend=None,root=None):
        self.store,self.annotations,self.backend=store,annotations,backend
        self.root=Path(root) if root else None
        self.thread=None;self.cancel=threading.Event();self.lock=threading.RLock()
        self.deadline=None;self.stop_error=None;self.prepared=None

    def status(self):
        job=copy.deepcopy(self.store.snapshot().get('initialization'))
        return dict(available=self.backend is not None,active=bool(job and job['status'] in ACTIVE
                    or self.thread is not None and self.thread.is_alive()),
                    job=job,stop_error=self.stop_error,backend='TX2_local_NCNN_OpenCV' if self.backend else 'unconfigured',
                    preserves_existing_scenes=True,super_resolution=False,maximum_candidates=12)

    def update(self,key,status,**fields):
        def apply(s):
            job=s.get('initialization')
            if not job or job['id']!=key:raise Conflict('initialization ownership changed')
            job.update(status=status,updated_at=now(),**fields)
        self.store.change(None,'scene_initialization_progress',dict(id=key,stage=status),apply)

    def prepare(self,revision,bounds):
        with self.lock:
            state=self.store.snapshot()
            if state['revision']!=revision:raise Conflict('scene revision changed')
            if self.backend is None or self.status()['active']:raise Conflict('scan backend busy')
            self.prepared=self.backend.prepare(bounds)
            return copy.deepcopy(self.prepared)

    def start(self,revision,review_first=False,fingerprint=None):
        with self.lock:
            if self.backend is None or self.stop_error:raise ValueError('initialization backend unavailable')
            state=self.store.snapshot();registration=self.annotations.status()
            if revision!=state['revision']:raise Conflict('scene revision changed')
            if (self.status()['active'] or self.thread is not None and self.thread.is_alive()
                    or state['schedule']['enabled'] or any(j['status']=='running' for j in state['jobs'])
                    or registration['active'] or (registration['queued'] and not registration['paused']) or registration['stop_error']):
                raise Conflict('pause active work before initializing a new scene')
            if len(state['maps'])>=20:raise ValueError('scene archive capacity reached')
            if type(review_first) is not bool:raise ValueError('review_first must be boolean')
            if fingerprint is not None:
                if not self.prepared or self.prepared['scan']['fingerprint']!=fingerprint:
                    raise Conflict('scan plan expired; prepare the range again')
                prepared=copy.deepcopy(self.prepared);self.prepared=None
            else:prepared=self.backend.prepare()
            prepared['review_first']=review_first
            key=identifier('initialization')
            def begin(s):
                # A new scan replaces paused unfinished work, without deleting
                # any observations, images or audit history.
                for q in s.get('annotation_queue',[]):
                    if q['state']=='queued':q.update(state='cancelled',finished_at=now(),reason='new_scan_replaced_paused_work')
                if s.get('initialization'):s.setdefault('initialization_history',[]).append(s['initialization'])
                s['initialization_history']=s.get('initialization_history',[])[-20:]
                s['initialization']=dict(id=key,status='scanning',started_at=now(),updated_at=now(),
                    map_id=None,plan=prepared,views=0,total_views=prepared['scan']['expected_frames'],
                    candidate_count=0,existing_scenes_preserved=True,periodic_enabled=False)
                Store.pause(s,'initialization_running')
            self.store.change(revision,'scene_initialization_start',dict(id=key),begin)
            self.cancel=threading.Event()
            self.deadline=time.monotonic()+(1260 if review_first else 3480)
            self.thread=threading.Thread(target=self._run,args=(key,prepared),name='scene-initialization')
            self.thread.daemon=True;self.thread.start()
            return dict(started=True,id=key,plan=prepared,preserves_existing_scenes=True)

    def _run(self,key,prepared):
        try:
            def progress(stage,details):
                if self.cancel.is_set():raise RuntimeError('initialization_cancelled')
                self.update(key,stage,**details)
            manifest=self.backend.run(prepared,self.root/'var/scene-initializations'/key,self.cancel,progress)
            if self.cancel.is_set():raise RuntimeError('initialization_cancelled')
            # An import must not independently trigger automatic movement before
            # its projection is bound or after initialization is cancelled.
            manifest['automatic_registration_requested']=False
            self.update(key,'importing')
            result=self.store.import_manifest(manifest)
            map_id=result['result']
            state=self.store.snapshot()
            binding=[{k:f[k] for k in ('sha256','width','height','orientation','pose')}
                     for f in state['frames'] if f['map_id']==map_id]
            self.store.bind_overview_geometry(state['revision'],map_id,manifest['overview']['sha256'],
                                              manifest['projection'],binding)
            self.update(key,'importing',map_id=map_id,processing=manifest.get('processing'))
            if self.cancel.is_set():raise RuntimeError('initialization_cancelled')
            state=self.store.snapshot()
            from initialization_proposals import groups
            proposals=groups(state,map_id)
            chosen=[g['candidate_id'] for g in proposals['selected']]
            if prepared.get('review_first'):
                with self.lock:
                    if self.cancel.is_set():raise RuntimeError('initialization_cancelled')
                    self.annotations.stop()
                    self.store.change(None,'scene_review_ready',dict(map_id=map_id),
                        lambda s:Store.find(s,'maps',map_id).update(review_before_commissioning=True))
                    self.update(key,'awaiting_review' ,candidate_count=proposals['observation_count'],
                        proposal_groups=proposals,scheduled_candidates=0,deferred_candidates=len(proposals['deferred']),
                        completed_candidates=0,ready_for_patrol=False,finished_at=now())
                return
            # STOP and this ownership handoff are serialized. Without this lock,
            # a STOP after resume could be overwritten by commissioning and leave
            # a runnable queue even though this worker was already cancelled.
            with self.lock:
                if self.cancel.is_set():raise RuntimeError('initialization_cancelled')
                # Starting a new initialization explicitly resumes its finite
                # work; an earlier user's STOP remains audited in history.
                self.annotations.resume(state['revision'])
                if self.cancel.is_set():raise RuntimeError('initialization_cancelled')
                if chosen:
                    self.annotations.discover(self.store.snapshot()['revision'],map_id,chosen)
                    if self.cancel.is_set():raise RuntimeError('initialization_cancelled')
                    self.update(key,'commissioning',candidate_count=proposals['observation_count'],
                                proposal_groups=proposals,scheduled_candidates=len(chosen),
                                deferred_candidates=len(proposals['deferred']),completed_candidates=0)
                else:
                    self.update(key,'awaiting_supplement',candidate_count=0,scheduled_candidates=0,
                                deferred_candidates=0,ready_for_patrol=False,finished_at=now())
        except Exception as exc:
            try:self.annotations.stop()
            except Exception:self.stop_error='initialization_STOP_unconfirmed'
            job=self.store.snapshot().get('initialization') or {}
            stage=job.get('status')
            # No unbound imported scene may accidentally start a second queue.
            self.update(key,'stopped' if self.cancel.is_set() else 'failed',failure_stage=stage,
                        error_type=type(exc).__name__,finished_at=now(),ready_for_patrol=False)

    def commission(self,revision,map_id):
        with self.lock:
            if self.status()['active']:raise Conflict('initialization is active')
            state=self.store.snapshot()
            if state['revision']!=revision:raise Conflict('review changed')
            from initialization_proposals import groups
            proposals=groups(state,map_id)
            chosen=[g['candidate_id'] for g in proposals['selected']]
            result=self.annotations.discover(revision,map_id,chosen) if chosen else dict(queued=[])
            from registry_annotations import queue_box
            from review_groups import review_groups
            representatives={g['candidate_id'] for g in review_groups(state,map_id) if not g['excluded']}
            existing={q.get('candidate_id') for q in state.get('annotation_queue',[]) if q['state'] not in ('cancelled','excluded')}
            humans=[c for c in state['candidates'] if c['map_id']==map_id and c['state']=='pending'
                    and c['source'].startswith('human_') and not c.get('instrument_id')
                    and c['id'] in representatives and c['id'] not in existing]
            for c in humans[:max(0,40-len(result['queued']))]:
                q=queue_box(self.store,self.store.snapshot()['revision'],c['frame_id'],c['bbox'],'human',c['id'])
                result['queued'].append(q['result']['queued_id'])
            self.annotations.resume(self.store.snapshot()['revision'])
            job=state.get('initialization')
            if job and job.get('map_id')==map_id:
                self.deadline=time.monotonic()+2400
                self.update(job['id'],'commissioning',scheduled_candidates=len(result['queued']),
                    proposal_groups=proposals,deferred_candidates=len(proposals['deferred']))
            return result

    def tick(self):
        info=self.status();job=info['job']
        if not info['active']:return
        if self.deadline and time.monotonic()>self.deadline:
            self.stop()
            self.update(job['id'],'timed_out',ready_for_patrol=False,finished_at=now())
            return
        if job['status']!='commissioning':return
        state=self.store.snapshot()
        queue=[q for q in state.get('annotation_queue',[]) if q['map_id']==job['map_id']]
        pending=[q for q in queue if q['state'] in ('queued','commissioning')]
        complete=len(queue)-len(pending)
        if pending:
            if job.get('completed_candidates')!=complete:self.update(job['id'],'commissioning',completed_candidates=complete)
            return
        assets=[a for a in state['instruments'] if a['map_id']==job['map_id'] and a['enabled']]
        uncertain=sum(q['state'] in ('uncertain','interrupted') for q in queue)
        verified=sum(a['pose_status']=='verified' for a in assets)
        from capture_round import photo_reference
        ready=(bool(assets) and verified==len(assets) and
               Store.find(state,'maps',job['map_id']).get('motion_verified') and
               all(photo_reference(state,a)[1] is None for a in assets) and
               not self.annotations.status()['stop_error'])
        status='completed' if ready and not uncertain and not job.get('deferred_candidates') else 'completed_with_uncertain'
        self.update(job['id'],status,completed_candidates=complete,verified_instruments=verified,
                    uncertain_candidates=uncertain,duplicate_candidates=sum(q['state']=='duplicate' for q in queue),
                    ready_for_patrol=ready,finished_at=now())

    def stop(self):
        with self.lock:
            self.cancel.set()
            info=self.status()
            if not info['active']:return dict(cancel_requested=False)
            # Pause the queue even before commissioning: import handoff may
            # already have queued work while its public stage is still importing.
            actions=[self.annotations.stop]
            if info['job']['status']!='commissioning' and self.backend:
                actions.append(self.backend.stop)
            error=None
            for action in actions:
                try:action()
                except Exception as exc:
                    self.stop_error='initialization_STOP_unconfirmed'
                    error=exc
            # Active workers retain ownership until they leave _run; the server
            # also blocks new starts while that thread is still alive.
            self.update(info['job']['id'],'stopped',ready_for_patrol=False,finished_at=now())
            if error is not None:raise error
            return dict(cancel_requested=True)

    def recover(self):
        info=self.status()
        if not info['active']:return
        if self.backend:
            from capture_review_views import motion_lease
            try:
                with motion_lease():self.backend.camera.stop()
            except Exception:self.stop_error='restart_initialization_STOP_unconfirmed'
        self.annotations.stop()
        self.update(info['job']['id'],'interrupted',ready_for_patrol=False,finished_at=now())

    def close(self):
        if self.status()['active']:self.stop()
        if self.thread:
            self.thread.join(timeout=20)
            if self.thread.is_alive():raise RuntimeError('initialization_shutdown_unconfirmed')

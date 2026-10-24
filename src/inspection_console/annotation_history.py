"""Audit/import automatic commissioning, preserving all earlier jobs and photos."""
import copy
import hashlib
import json
from pathlib import Path
import re

from adaptive_capture import inside_frame
from annotation_capture import validate_annotation
from references import digest
from planner import pose
from ring_capture import close_pose
from store import Store


def checked_frame(root,frame,store):
    name=frame['path']
    if not re.fullmatch(r'frame-[0-9]{3}\.jpg',name):raise ValueError('invalid capture frame name')
    path=root/name
    if path.is_symlink() or path.resolve().parent!=root:raise ValueError('capture image leaves run')
    saved=store.save_image(path,frame['sha256'])
    if (saved['width'],saved['height'])!=(frame['width'],frame['height']):
        raise ValueError('capture dimensions differ from record')
    return saved


def import_annotation_capture(store,directory,revision):
    root=Path(directory).resolve()
    path=root/'record.json'
    if path.is_symlink() or path.stat().st_size>5000000:raise ValueError('invalid annotation record')
    raw=path.read_bytes();record=json.loads(raw.decode('utf8'))
    state=store.snapshot()
    if revision!=state['revision']:raise ValueError('import needs current revision')
    plan=validate_annotation(record['plan'],state)
    if (record['status'] not in ('completed','failed','stopped') or record.get('reading') is not None
            or record.get('periodic_capture_enabled') is not False):
        raise ValueError('annotation import cannot enable reading or periodic capture')
    key='annotation-'+hashlib.sha256(raw).hexdigest()[:16]
    instrument=plan['target']['instrument_id']
    result=dict(instrument_id=instrument,status='commissioning_failed',reading=None,
        error=record.get('error'),visit_pose_verified=False,elapsed_seconds=record['elapsed_seconds'],
        manual_review_required=False)
    if record['status']=='completed':
        if (not record.get('preset_commissioned') or not record.get('stop_confirmed')
                or not record.get('revisit_verified') or not record.get('seed_basis')
                or not close_pose(record['restored_pose'],plan['anchor'])):
            raise ValueError('commissioning lacks seed, revisit, return or STOP')
        departure=record.get('independent_departure_pose')
        if departure is None:
            # First staged revision recorded the anchor return as an IO event.
            # Accept that original evidence, never edit a historical record.
            before=[e for e in record['events'] if e.get('phase')=='restore_anchor_zoom']
            departure=before[0]['actual'] if before else None
        visit=pose(record['visit_pose']);departure=pose(departure)
        if max(abs(departure[k]-visit[k]) for k in ('pan','tilt'))<2.:
            raise ValueError('independent revisit needs physical departure')
        checks=[f for f in record['captures'] if f['purpose'].startswith('independent_revisit_')]
        if (len(checks)!=2 or checks[0]['path']==checks[1]['path']
                or not checks[0]['received_monotonic']<checks[1]['received_monotonic']):
            raise ValueError('commissioning needs two distinct, chronological fresh frames')
        for f in checks:
            if (f.get('match',{}).get('status')!='matched_reference'
                    or not inside_frame(f,plan['frame_margin']) or not close_pose(f['pose'],visit)
                    or f['orientation']!='rotate180_from_raw'):
                raise ValueError('revisit identity, bounds, orientation or pose rejected')
            checked_frame(root,f,store)
        automatic=plan['target']['references'][0]['selection']=='automatic_unverified_proposal'
        if automatic and not all(f['match'].get('category_verified') is True and
                f['match'].get('category_recheck',{}).get('accepted') is True for f in checks):
            raise ValueError('automatic proposal lacks two fresh semantic meter rechecks')
        frame=checks[-1]
        if record['evidence']['image_sha256']!=frame['sha256'] or record['evidence']['image']!=frame['path']:
            raise ValueError('evidence must be the last fresh revisit observation')
        saved=checked_frame(root,frame,store)
        result.update(status='preset_commissioned',image=saved['image'],image_sha256=frame['sha256'],
            bbox=frame['bbox'],pose=visit,orientation=frame['orientation'],
            captured_at=frame['captured_at'],matching_fresh_observations=2,visit_pose_verified=True,
            center_status=record['center_status'],center_error=frame['center_error'],
            frame_bounds_checked=True,physical_face_visibility='not_proven_by_bbox',
            image_status='automatic_reference_after_independent_revisit',
            source_kind='annotation_commissioning',seed_basis=record['seed_basis'],
            categorical_verification_basis='two_fresh_NCNN_rechecks' if automatic else 'existing_human_annotation')
    elif record['captures']:
        frame=record['captures'][-1]
        saved=checked_frame(root,frame,store)
        result.update(image=saved['image'],image_sha256=frame['sha256'],
            captured_at=frame['captured_at'],image_status='diagnostic_not_commissioned')
    job=dict(id=key,map_id=plan['map_id'],mode='annotation_commissioning',status=record['status'],
        route=[instrument],started_at=record.get('started_at',record['finished_at']),
        finished_at=record['finished_at'],results=[result],error=record.get('error'),
        elapsed_seconds=record['elapsed_seconds'],stop_confirmed=record['stop_confirmed'],
        record_sha256=hashlib.sha256(raw).hexdigest(),plan_fingerprint=plan['fingerprint'])

    def apply(current):
        Store.editable(current,plan['map_id'])
        validate_annotation(plan,dict(current,revision=revision))
        if any(j['id']==key for j in current['jobs']):raise ValueError('annotation record already imported')
        before=copy.deepcopy(dict(current,revision=revision))
        current['jobs'].append(job)
        if result['status']=='preset_commissioned':
            asset=Store.find(current,'instruments',instrument)
            candidate=Store.find(current,'candidates',asset['candidate_id'])
            if candidate['state']=='automatic_proposal':
                candidate.update(state='auto_verified',verification_source='fresh_NCNN_and_independent_revisit')
            asset.update(pose_status='verified',visit_pose=copy.deepcopy(result['pose']),
                commissioning=dict(method='automatic_annotation_then_independent_revisit',
                    job_id=key,scene_binding=plan['scene_binding'],image=result['image'],
                    image_sha256=result['image_sha256'],bbox=result['bbox'],
                    orientation=result['orientation'],fresh_observations=2,
                    center_status=result['center_status'],reading_verified=False,
                    geometry_calibrated=False,record_sha256=job['record_sha256'],
                    capture_result=copy.deepcopy(result),capture_result_fingerprint=digest(result),
                    source_job_stop_confirmed=True,source_job_status='completed',
                    source_job_mode='annotation_commissioning'))
        from registry_annotations import rebind_unchanged_presets
        rebind_unchanged_presets(current,plan['map_id'],before)
        scene=Store.find(current,'maps',plan['map_id'])
        Store.pause(current,'automatic_commissioning_imported_periodic_not_enabled')
        return dict(job_id=key,preset_commissioned=result['status']=='preset_commissioned',
                    scene_presets_ready=scene['motion_verified'])
    return store.change(revision,'annotation_commissioning_import',dict(job_id=key,instrument_id=instrument),apply)


def migrate_preset_proofs(store,revision):
    """Pin immutable preset evidence before the bounded recent-job list ages out."""
    snapshot=store.snapshot(include_history=True)
    if snapshot['revision']!=revision:raise ValueError('migration needs current revision')
    jobs={j['id']:j for j in snapshot.get('job_history',[])+snapshot['jobs']}
    def apply(state):
        if any(j['status']=='running' for j in state['jobs']) or state['schedule']['enabled']:
            raise ValueError('pause capture before migrating preset proofs')
        changed=[]
        for asset in state['instruments']:
            c=asset.get('commissioning')
            if not c or c.get('capture_result'):continue
            job=jobs.get(c['job_id'])
            r=next((r for r in job['results'] if r['instrument_id']==asset['id']),None) if job else None
            if (not job or job['status']!='completed' or job['mode']!='annotation_commissioning'
                    or not job.get('stop_confirmed') or not r or r['status']!='preset_commissioned'
                    or r['image_sha256']!=c['image_sha256'] or r.get('matching_fresh_observations')!=2
                    or pose(r['pose'])!=pose(asset['visit_pose'])):
                raise ValueError('original commissioned evidence is unavailable')
            c.update(capture_result=copy.deepcopy(r),capture_result_fingerprint=digest(r),
                source_job_stop_confirmed=True,source_job_status='completed',source_job_mode=job['mode'])
            changed.append(asset['id'])
        return dict(preset_proofs_pinned=changed,camera_motion_sent=False)
    return store.change(revision,'preset_proofs_pinned',{},apply)

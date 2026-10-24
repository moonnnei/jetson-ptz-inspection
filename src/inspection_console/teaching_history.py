"""Trusted CLI import of failed/limited captures for diagnostic web review.

These images never verify a visit pose, change human selection, or enable
periodic capture. Successful commissioning has a separate acceptance contract.
"""
import hashlib
import json
from pathlib import Path
import re

from references import reference_plan
from store import Conflict, Store, now, text
from teaching import validate_plan


def import_failure(store, directory, revision):
    return _import_record(store,directory,revision,allow_partial=False)


def import_partial_capture(store,directory,revision):
    return _import_record(store,directory,revision,allow_partial=True)


def _import_record(store, directory, revision, allow_partial, prepare_only=False):
    root=Path(directory).resolve()
    record_path=root/'record.json'
    if record_path.is_symlink() or record_path.stat().st_size>2000000:
        raise ValueError('invalid pilot record')
    raw=record_path.read_bytes()
    record=json.loads(raw.decode('utf-8'))
    if allow_partial:
        if (record['status']!='partial_capture_completed' or
                record.get('visit_pose_verified') is not False or
                record.get('revisit_verified') is not False or
                record.get('stop_confirmed') is not True):
            raise ValueError('partial capture cannot commission a visit pose')
    elif record['status'] not in ('failed','stopped'):
        raise ValueError('only failed/stopped diagnostics use this importer')
    state=store.snapshot()
    plan=record['plan']
    if revision!=state['revision'] or revision!=plan['revision']:
        raise Conflict('review changed since this pilot; do not overwrite current selection')
    refs=reference_plan(state,plan['map_id'],revision)
    if plan['kind']=='bounded_meter_constrained_capture':
        from best_effort import validate_capture_plan
        validate=validate_capture_plan
    else:validate=validate_plan
    validate(plan,refs)
    if record.get('reading') is not None or record.get('periodic_capture_enabled') is not False:
        raise ValueError('pilot must not invent readings or enable periodic capture')
    key='teaching-'+hashlib.sha256(raw).hexdigest()[:16]
    if any(j['id']==key for j in state['jobs']):raise Conflict('pilot already imported')
    target=plan['target']['instrument_id']
    result=dict(instrument_id=target,status='captured_with_limits' if allow_partial else 'teaching_failed',reading=None,
        captured_at=None,error=None if allow_partial else text(record.get('error','pilot_failed'),500),
        stop_confirmed=record.get('stop_confirmed') is True)
    frames=[f for f in record['captures'] if f.get('match',{}).get('status')=='matched_reference']
    if frames:
        frame=frames[-1]
        name=frame['path']
        if not re.match(r'^frame-[0-9]{3}\.jpg$',name):raise ValueError('invalid diagnostic image name')
        image=root/name
        if image.is_symlink() or image.resolve().parent!=root:raise ValueError('image leaves pilot directory')
        saved=store.save_image(image,frame['sha256'])
        if (saved['width'],saved['height'])!=(frame['width'],frame['height']):
            raise ValueError('diagnostic image dimensions changed')
        result.update(image=saved['image'],bbox=frame['bbox'],pose=frame['pose'],
            center_error=frame['center_error'],captured_at=frame['captured_at'],
            image_status='constrained_capture_not_verified_visit' if allow_partial else
                'failure_diagnostic_not_accepted_inspection_capture')
        if allow_partial:
            if record.get('evidence',{}).get('image_sha256')!=frame['sha256']:
                raise ValueError('partial capture evidence does not match the final observation')
            result.update(center_status=record['center_status'],
                optical_zoom=frame['pose']['zoom'],quality_status=record['quality_status'])
    diagnostic=record.get('reachability')
    if diagnostic:result['reachability']=diagnostic
    job=dict(id=key,map_id=plan['map_id'],status=record['status'],
        mode='constrained_meter_capture' if allow_partial else 'single_meter_teaching_pilot',
        started_at=record['started_at'],finished_at=record['finished_at'],results=[result],route=[target],
        error=result['error'],record_sha256=hashlib.sha256(raw).hexdigest(),
        stop_confirmed=result['stop_confirmed'],plan_fingerprint=plan['fingerprint'])
    if prepare_only:return job,plan
    def apply(current):
        Store.editable(current,plan['map_id'])
        validate(plan,reference_plan(dict(current,revision=revision),plan['map_id'],revision))
        if any(j['id']==key for j in current['jobs']):raise Conflict('pilot already imported')
        # Preserve every instrument/candidate and all prior captures exactly.
        current['jobs'].append(job)
        Store.pause(current,'limited_capture_requires_revisit_acceptance' if allow_partial else
                    'teaching_failed_requires_site_adjustment')
        return key
    return store.change(revision,'limited_capture_import' if allow_partial else 'teaching_failure_import',
        dict(id=key,instrument_id=target,record_sha256=job['record_sha256']),apply)


def import_partial_batch(store,directories,revision):
    """All accepted capture records enter one version-checked transaction."""
    return _import_batch(store,directories,revision,allow_failed=False)


def import_diagnostic_batch(store,directories,revision):
    """Publish successful limited captures and stopped diagnostics atomically."""
    return _import_batch(store,directories,revision,allow_failed=True)


def _import_batch(store,directories,revision,allow_failed):
    if not isinstance(directories,list) or not 1<=len(directories)<=4:
        raise ValueError('batch requires one to four new capture directories')
    prepared=[]
    for directory in directories:
        partial=True
        if allow_failed:
            path=Path(directory)/'record.json'
            if path.stat().st_size>2000000:raise ValueError('record exceeds import budget')
            partial=json.loads(path.read_text(encoding='utf-8'))['status']=='partial_capture_completed'
        prepared.append(_import_record(store,directory,revision,partial,prepare_only=True))
    maps={job['map_id'] for job,plan in prepared}
    targets=[job['results'][0]['instrument_id'] for job,plan in prepared]
    if len(maps)!=1 or len(set(targets))!=len(targets):
        raise ValueError('batch must contain distinct instruments from one map')
    def apply(state):
        map_id=next(iter(maps))
        Store.editable(state,map_id)
        refs=reference_plan(dict(state,revision=revision),map_id,revision)
        from best_effort import validate_capture_plan
        for job,plan in prepared:
            validate_capture_plan(plan,refs)
            if any(j['id']==job['id'] for j in state['jobs']):raise Conflict('capture already imported')
        state['jobs'].extend(job for job,plan in prepared)
        Store.pause(state,'limited_capture_requires_revisit_acceptance')
        return [job['id'] for job,plan in prepared]
    return store.change(revision,'limited_capture_batch_import',dict(instrument_ids=targets),apply)

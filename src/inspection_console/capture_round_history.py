"""Import new single-round evidence without commissioning positions or schedules."""
import hashlib
import json
from pathlib import Path
import re

from capture_round import validate_round
from store import Store


def prepare_round_job(store,directory,revision,state):
    root=Path(directory).resolve();path=root/'record.json'
    if path.is_symlink() or path.stat().st_size>5000000:raise ValueError('invalid round record')
    raw=path.read_bytes();record=json.loads(raw.decode('utf-8'))
    plan=record['plan'];validate_round(plan,state)
    if revision!=plan['revision']:raise ValueError('round import needs its original review revision')
    if (record['status'] not in ('completed','completed_with_errors','failed','stopped')
            or record.get('visit_pose_verified') is not False
            or record.get('periodic_capture_enabled') is not False or record.get('reading') is not None):
        raise ValueError('round cannot commission poses or fabricate readings')
    if record['status'] in ('completed','completed_with_errors') and not record.get('stop_confirmed'):
        raise ValueError('a normally finished round needs confirmed STOP')
    if record['status']=='completed' and (not record.get('stop_confirmed') or
            len(record['results'])!=len(plan['stops']) or
            any(r.get('status')!='captured_reference_checked' for r in record['results'])):
        raise ValueError('completed round has incomplete stop or target evidence')
    results=[];seen=set();target_ids={t['id'] for t in plan['stops']}
    for item in record['results']:
        item=dict(item);key=item['instrument_id']
        if key not in target_ids or key in seen:raise ValueError('unknown or duplicated round result')
        seen.add(key)
        if item['status'] not in ('captured_reference_checked','uncertain'):
            raise ValueError('unsupported round result')
        frame=next((f for f in record['captures'] if f['path']==item['image'] and f['instrument_id']==key),None)
        if frame is None or frame['sha256']!=item['image_sha256']:raise ValueError('result is not a new round capture')
        if item['status']=='captured_reference_checked':
            checked=[f for f in record['captures'] if f['instrument_id']==key and f.get('match',{}).get('status')=='matched_reference']
            if len(checked)!=2 or item.get('matching_fresh_observations')!=2:
                raise ValueError('accepted result requires two fresh matching observations')
        name=item['image']
        if not re.match(r'^frame-[0-9]{3}\.jpg$',name):raise ValueError('invalid round image path')
        image=root/name
        if image.is_symlink() or image.resolve().parent!=root:raise ValueError('image leaves round directory')
        saved=store.save_image(image,item['image_sha256'])
        if (saved['width'],saved['height'])!=(frame['width'],frame['height']):
            raise ValueError('round capture dimensions changed')
        item.update(image=saved['image'],reading=None,visit_pose_verified=False)
        results.append(item)
    for key in target_ids-seen:results.append(dict(instrument_id=key,status='not_visited_round_terminated',reading=None))
    key='round-'+hashlib.sha256(raw).hexdigest()[:16]
    job=dict(id=key,map_id=plan['map_id'],mode=plan['kind'],status=record['status'],
        started_at=record['started_at'],finished_at=record['finished_at'],
        route=[t['id'] for t in plan['stops']],results=results,error=record.get('error'),
        elapsed_seconds=record['elapsed_seconds'],stop_confirmed=record.get('stop_confirmed') is True,
        record_sha256=hashlib.sha256(raw).hexdigest(),plan_fingerprint=plan['fingerprint'])
    return job,plan


def import_round(store,directory,revision):
    job,plan=prepare_round_job(store,directory,revision,store.snapshot())
    key=job['id']
    def apply(current):
        Store.editable(current,plan['map_id'])
        validate_round(plan,dict(current,revision=revision))
        if any(j['id']==key for j in current['jobs']):raise ValueError('round already imported')
        current['jobs'].append(job);Store.pause(current,'single_round_trial_completed_periodic_not_enabled')
        return key
    return store.change(revision,'capture_round_import',dict(id=key,route=job['route']),apply)

"""Queued missed boxes and automatic proposals, without fabricated human review."""
import copy
import math

from annotation_capture import scene_binding
from references import reference_plan
from region_detector import iou
from store import Store,bbox,identifier,now


def image_binding(target):
    value=copy.deepcopy(target)
    value.pop('name',None);value.pop('blockers',None)
    for ref in value['references']:ref.pop('selection',None)
    return value


def rebind_unchanged_presets(state,map_id,before):
    """Rebind a collection index only; never invent another physical visit."""
    old=scene_binding(before,map_id)
    after=dict(state,revision=before['revision'])
    fresh=scene_binding(after,map_id)
    previous={t['instrument_id']:image_binding(t) for t in reference_plan(before,map_id,before['revision'])['targets']}
    current={t['instrument_id']:image_binding(t) for t in reference_plan(after,map_id,after['revision'])['targets']}
    rebound=[]
    for asset in state['instruments']:
        c=asset.get('commissioning');key=asset['id']
        if (asset['map_id']==map_id and asset['enabled'] and c and c['scene_binding']==old
                and key in previous and previous[key]==current.get(key)):
            c['scene_binding']=fresh
            if fresh!=old:
                c.setdefault('prior_collection_bindings',[]).append(old)
                rebound.append(key)
    active=[a for a in state['instruments'] if a['map_id']==map_id and a['enabled']]
    Store.find(state,'maps',map_id)['motion_verified']=bool(active) and all(
        a['pose_status']=='verified' and a.get('commissioning',{}).get('scene_binding')==fresh for a in active)
    return rebound


def queue_box(store,revision,frame_id,box,origin='human',candidate_id=None):
    if origin not in ('human','automatic'):raise ValueError('unknown annotation origin')
    key=identifier('annotation')
    def apply(state):
        frame=Store.find(state,'frames',frame_id)
        scene=Store.find(state,'maps',frame['map_id'])
        if scene['status']=='invalidated':raise ValueError('scene invalidated')
        queue=state.setdefault('annotation_queue',[])
        if sum(q['state']=='queued' for q in queue)>=40:raise ValueError('finite annotation queue is full')
        checked=bbox(box,frame['width'],frame['height'])
        if origin=='automatic':
            c=Store.find(state,'candidates',candidate_id)
            if c['state']!='pending' or c['frame_id']!=frame_id or c['bbox']!=checked:
                raise ValueError('automatic proposal changed')
            if c.get('confidence') is None or c['confidence']<.25 or c['source'].startswith('human_'):
                raise ValueError('automatic queue requires an actual model proposal')
        queue.append(dict(id=key,map_id=frame['map_id'],frame_id=frame_id,
            frame_sha256=frame['sha256'],bbox=checked,origin=origin,candidate_id=candidate_id,
            state='queued',created_at=now()))
        # Existing running capture keeps its immutable source binding. The new
        # box is materialized only at the next idle phase boundary.
        Store.pause(state,'annotation_queued_for_safe_boundary')
        return dict(queued_id=key,manual_confirmation_required=False)
    return store.change(revision,'annotation_queued',dict(frame_id=frame_id,bbox=box,origin=origin),apply)


def materialize(store,revision,queue_id):
    def apply(state):
        entry=next(q for q in state['annotation_queue'] if q['id']==queue_id)
        if entry['state']!='queued':raise ValueError('annotation already consumed')
        Store.editable(state,entry['map_id'])
        if state['schedule']['enabled']:raise ValueError('pause scheduling before commissioning')
        frame=Store.find(state,'frames',entry['frame_id'])
        if frame['sha256']!=entry['frame_sha256']:raise ValueError('queued source changed')
        previous=copy.deepcopy(dict(state,revision=revision))
        # Same-source overlap can safely reuse an already verified identity.
        for asset in state['instruments']:
            c=Store.find(state,'candidates',asset['candidate_id'])
            if (asset['map_id']==entry['map_id'] and asset['enabled'] and asset['pose_status']=='verified'
                    and c['frame_id']==entry['frame_id'] and iou(c['bbox'],entry['bbox'])>=.55):
                entry.update(state='duplicate',duplicate_of=asset['id'],finished_at=now())
                return dict(duplicate_of=asset['id'],camera_motion_required=False)
        if sum(a['enabled'] and a['map_id']==entry['map_id'] for a in state['instruments'])>=12:
            raise ValueError('finite prototype patrol capacity reached')
        candidate=Store.find(state,'candidates',entry['candidate_id']) if entry['candidate_id'] else None
        if candidate is None:
            candidate=dict(id=identifier('candidate'),map_id=entry['map_id'],frame_id=entry['frame_id'],
                bbox=entry['bbox'],confidence=None,source='human_missed_target_box',created_at=now(),
                instrument_id=None,merged_into=None)
            state['candidates'].append(candidate)
        if candidate and candidate.get('state')=='rejected':raise ValueError('proposal excluded during review')
        if candidate.get('instrument_id'):raise ValueError('proposal already bound to an instrument')
        key='M%04d'%state['next_instrument'];state['next_instrument']+=1
        candidate.update(state='human_labeled' if entry['origin']=='human' else 'automatic_proposal',
                         instrument_id=key,acceptance_source=entry['origin'])
        state['instruments'].append(dict(id=key,map_id=entry['map_id'],candidate_id=candidate['id'],
            name='仪表 '+key,enabled=True,pose_status='unverified',visit_pose=None,created_at=now(),
            reading=None,reading_status='not_integrated',acceptance_source=entry['origin']))
        entry.update(state='commissioning',instrument_id=key,candidate_id=candidate['id'])
        rebound=rebind_unchanged_presets(state,entry['map_id'],previous)
        return dict(instrument_id=key,rebound_unchanged_presets=rebound,camera_motion_required=True)
    return store.change(revision,'annotation_materialized',dict(queue_id=queue_id),apply)


def finish_queue(store,revision,queue_id,status,job_id=None):
    if status not in ('completed','uncertain','duplicate'):raise ValueError('invalid finite annotation outcome')
    def apply(state):
        entry=next(q for q in state['annotation_queue'] if q['id']==queue_id)
        if entry['state']!='commissioning':raise ValueError('annotation is not being commissioned')
        previous=copy.deepcopy(dict(state,revision=revision))
        entry.update(state=status,finished_at=now(),job_id=job_id)
        if status=='uncertain':
            asset=Store.find(state,'instruments',entry['instrument_id'])
            asset.update(pose_status='uncertain',visit_pose=None)
            candidate=Store.find(state,'candidates',asset['candidate_id'])
            if entry['origin']=='automatic':
                asset['enabled']=False
                candidate['state']='auto_uncertain'
            rebind_unchanged_presets(state,entry['map_id'],previous)
        return dict(queue_id=queue_id,status=status)
    return store.change(revision,'annotation_finished',dict(queue_id=queue_id,status=status),apply)


def merge_commissioned_duplicate(store,revision,duplicate_id,keeper_id,evidence):
    """Called only with a positive image match and overlapping fresh target ROI."""
    if (evidence.get('status')!='matched_reference' or evidence.get('overlap_iou',0)<.5
            or evidence.get('direction_difference_degrees',100)>3.):
        raise ValueError('duplicate identity evidence is insufficient')
    def apply(state):
        duplicate=Store.find(state,'instruments',duplicate_id);keeper=Store.find(state,'instruments',keeper_id)
        Store.editable(state,duplicate['map_id'])
        if duplicate_id==keeper_id or duplicate['map_id']!=keeper['map_id']:
            raise ValueError('duplicate must be another instrument in the same scene')
        if not all(a['enabled'] and a['pose_status']=='verified' and a.get('commissioning') for a in (duplicate,keeper)):
            raise ValueError('both images need independently commissioned capture positions')
        projection=Store.find(state,'maps',duplicate['map_id'])['overview']['ring_projection']
        measured=direction_difference(duplicate,keeper,projection)
        if measured>3. or abs(measured-evidence['direction_difference_degrees'])>1e-5:
            raise ValueError('duplicate directions differ from the stored capture geometry')
        if (evidence.get('source_sha256')!=keeper['commissioning']['image_sha256']
                or evidence.get('target_sha256')!=duplicate['commissioning']['image_sha256']):
            raise ValueError('duplicate check does not bind the commissioned photos')
        previous=copy.deepcopy(dict(state,revision=revision))
        duplicate.update(enabled=False,pose_status='automatic_duplicate',duplicate_of=keeper_id)
        candidate=Store.find(state,'candidates',duplicate['candidate_id'])
        candidate.update(state='auto_duplicate',duplicate_of=keeper['candidate_id'],
                         duplicate_evidence=copy.deepcopy(evidence))
        rebind_unchanged_presets(state,duplicate['map_id'],previous)
        return dict(duplicate_id=duplicate_id,duplicate_of=keeper_id,source_images_preserved=True)
    return store.change(revision,'automatic_duplicate_merged',dict(duplicate_id=duplicate_id,keeper_id=keeper_id),apply)


def direction(asset,projection):
    p=asset['visit_pose'];box=asset['commissioning']['bbox']
    dx=(box[0]+box[2])/2.-projection['source_width']/2.
    dy=(box[1]+box[3])/2.-projection['source_height']/2.
    f=projection['focal_pixels']*p['zoom']
    return [p['pan']-projection['yaw_image_sign']*math.degrees(math.atan(dx/f)),
            p['tilt']-math.degrees(math.atan(dy/math.hypot(f,dx)))]


def direction_difference(a,b,projection):
    aa,bb=direction(a,projection),direction(b,projection)
    return max(abs((aa[0]-bb[0]+180)%360-180),abs(aa[1]-bb[1]))

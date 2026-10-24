"""Cross-view review units, separate from verified instrument identities.

All observations, including exclusions, participate so a decision cannot make
the same target reappear under another observation ID. Complete-link overlap
avoids chaining adjacent instruments into a single giant proposal.
"""
import math
from region_detector import iou


def angular_box(candidate,frame,projection):
    f=projection['focal_pixels']*frame['pose'].get('zoom',1.)
    w,h=frame['width'],frame['height'];x,y,r,b=candidate['bbox'];p=frame['pose']
    sign=projection.get('yaw_image_sign',1)
    xs=[p['pan']-sign*math.degrees(math.atan((a-w/2.)/f)) for a in (x,r)]
    ys=[p.get('tilt',0)-math.degrees(math.atan((a-h/2.)/f)) for a in (y,b)]
    return [min(xs),min(ys),max(xs),max(ys)]


def compatible(a,b):
    # Same-frame distinct boxes are never merged merely for being nearby.
    if a['frame_id']==b['frame_id']:return iou(a['bbox'],b['bbox'])>=.65
    x,y=a['_angle'],b['_angle']
    overlap=max(iou(x,[y[0]+s,y[1],y[2]+s,y[3]]) for s in (-360,0,360))
    return overlap>=.45


def review_groups(state,map_id):
    scene=next(m for m in state['maps'] if m['id']==map_id)
    projection=(scene.get('overview') or {}).get('ring_projection')
    frames={f['id']:f for f in state['frames'] if f['map_id']==map_id}
    observations=[]
    for raw in state['candidates']:
        if raw['map_id']!=map_id:continue
        c=dict(raw);f=frames.get(c['frame_id'])
        try:c['_angle']=angular_box(c,f,projection)
        except (KeyError,TypeError,ValueError):c['_angle']=None
        observations.append(c)
    observations.sort(key=lambda c:c['id'])
    clusters=[]
    for c in observations:
        cluster=next((g for g in clusters if c['_angle'] is not None and all(
            a['_angle'] is not None and compatible(c,a) for a in g)),None)
        if cluster is None:cluster=[];clusters.append(cluster)
        cluster.append(c)
    assets={a['id']:a for a in state.get('instruments',[])}
    result=[]
    for cluster in clusters:
        def cost(c):
            f=frames[c['frame_id']];b=c['bbox'];a=assets.get(c.get('instrument_id')) or {}
            return (0 if a.get('pose_status')=='verified' else 1,
                abs((b[0]+b[2])/2.-f['width']/2.)/f['width'],-(b[2]-b[0])*(b[3]-b[1]),c['id'])
        ordered=sorted(cluster,key=cost);members=[c['id'] for c in ordered]
        rejected=any(c['state']=='rejected' for c in ordered)
        result.append(dict(id=min(members),candidate_id=members[0],members=members,
            alternatives=members[1:],observation_count=len(members),
            distinct_views=len({c['frame_id'] for c in ordered}),excluded=rejected,
            confidence=max(c.get('confidence') or 0 for c in ordered),
            state='rejected' if rejected else ordered[0]['state']))
    return sorted(result,key=lambda g:(-min(3,g['distinct_views']),-g['confidence'],g['id']))


def expand_groups(state,ids):
    selected=set(ids);maps={c['map_id'] for c in state['candidates'] if c['id'] in selected}
    for map_id in maps:
        for g in review_groups(state,map_id):
            if selected.intersection(g['members']):selected.update(g['members'])
    return sorted(selected)


def decorate_review(state):
    for scene in state['maps']:
        if (scene.get('overview') or {}).get('ring_projection'):
            scene['review_groups']=review_groups(state,scene['id'])
    return state

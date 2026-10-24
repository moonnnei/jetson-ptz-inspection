"""Schedule representatives of overlapping panorama hypotheses, not final IDs.

Every source observation is preserved. A hypothesis needs fresh category and
appearance checks before it becomes an instrument; geometry alone never merges
formal assets. The most central source reduces unnecessary first-capture travel.
"""
import math
from region_detector import iou
from ring_coordinates import source_box


def overlap(a,b,width):
    return max(iou(a,[b[0]+shift,b[1],b[2]+shift,b[3]]) for shift in (-width,0,width))


def groups(state,map_id,limit=12):
    from review_groups import review_groups
    scene=next(m for m in state['maps'] if m['id']==map_id)
    projection=scene['overview']['ring_projection']
    frames={f['id']:f for f in state['frames'] if f['map_id']==map_id}
    review=review_groups(state,map_id)
    candidates=sorted([c for c in state['candidates'] if c['map_id']==map_id and c['state']=='pending'
                       and c.get('confidence') is not None and c['confidence']>=.25],
                      key=lambda c:(-c['confidence'],c['id']))
    # The same grouping is used by the browser, exclusion transaction and
    # commissioning. It includes observations whose states have since changed.
    byid={c['id']:c for c in candidates}
    result=[]
    for g in review:
        if g['excluded'] or g['candidate_id'] not in byid:continue
        c=byid[g['candidate_id']];f=frames[c['frame_id']]
        try:box=source_box(c['bbox'],f['pose']['pan'],projection,f['pose'].get('tilt'))
        except ValueError:box=None
        result.append(dict(g,panorama_box=box))
    return dict(selected=result[:limit],deferred=result[limit:],observation_count=len(candidates),
                hypothesis_count=len(result),formal_identity_merged=False,method='cross_view_angular_complete_link_review_groups')

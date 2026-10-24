"""Read-only source proposals for manual boxes drawn on a ring preview.

Estimated projection selects possible source images, never a verified PTZ pose.
The user must inspect the high-resolution source before saving a candidate.
"""
import copy

from ring_coordinates import parameters, ring_box, source_choices


def source_proposal(state, map_id, overview_sha256, box):
    scenes = [m for m in state['maps'] if m['id'] == map_id]
    if not scenes or scenes[0]['status'] == 'invalidated':
        raise ValueError('scene is missing or invalidated')
    overview = scenes[0].get('overview') or {}
    if overview.get('sha256') != overview_sha256:
        raise ValueError('overview changed; refresh before drawing')
    if not overview.get('ring_projection'):
        raise ValueError('this overview has no source mapping; use the original image')
    projection = parameters(overview['ring_projection'])
    box = ring_box(box, projection)
    if box[2] - box[0] < 2 or box[3] - box[1] < 2:
        raise ValueError('draw a larger box')
    frames = [f for f in state['frames'] if f['map_id'] == map_id]
    by_id = {f['id']: f for f in frames}
    choices = []
    for choice in source_choices(box, frames, projection)[:8]:
        frame = by_id[choice['frame_id']]
        item = copy.deepcopy(choice)
        item.update(image=frame['image'], title=frame['title'], width=frame['width'],
                    height=frame['height'], pose=frame['pose'])
        choices.append(item)
    if not choices:
        raise ValueError('no full high-resolution source covers this box; draw a smaller box or use the original image')
    return dict(map_id=map_id, revision=state['revision'], overview_sha256=overview_sha256,
                bbox=box, choices=choices, source_review_required=True,
                mapping='estimated_cylindrical_review', motion_sent=False)

"""Read-only reference plans for confirmed meters; no hardware or image library.

A source pose is where the *overview* was acquired, not a centered visit pose.
These plans never mark an instrument verified or enable the scheduler.
"""
import copy
import hashlib
import json
from pathlib import Path
import re

from store import Conflict, Store, bbox, image_size
from planner import pose


def digest(value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def reference_plan(state, map_id, revision):
    if type(revision) is not int or revision != state['revision']:
        raise Conflict('reference plan needs the current registry revision')
    scene = Store.find(state, 'maps', map_id)
    if scene['status'] == 'invalidated':
        raise ValueError('map is invalidated; references require a new map')
    targets = []
    for asset in sorted(state['instruments'], key=lambda a: a['id']):
        if asset['map_id'] != map_id or not asset['enabled']:
            continue
        candidate = Store.find(state, 'candidates', asset['candidate_id'])
        if (candidate['map_id'] != map_id or candidate['state'] != 'confirmed'
                or candidate['instrument_id'] != asset['id']):
            raise ValueError('enabled instrument is not bound to a confirmed candidate')
        observations = [candidate] + sorted(
            [c for c in state['candidates'] if c['map_id'] == map_id
             and c['state'] == 'merged' and c['merged_into'] == candidate['id']],
            key=lambda c: c['id'])
        references = []
        for observed in observations:
            frame = Store.find(state, 'frames', observed['frame_id'])
            if frame['map_id'] != map_id:
                raise ValueError('reference frame belongs to another map')
            source_pose = pose(frame['pose']) if frame.get('pose') else None
            references.append(dict(
                candidate_id=observed['id'], frame_id=frame['id'],
                image=frame['image'], sha256=frame['sha256'],
                width=frame['width'], height=frame['height'],
                bbox=bbox(observed['bbox'], frame['width'], frame['height']),
                orientation=frame['orientation'], source_kind=frame['source_kind'],
                captured_at=frame.get('captured_at'), source_pose=source_pose,
                selection='confirmed' if observed is candidate else 'human_merged'))
        primary = references[0]
        blockers = ['image_motion_calibration_required', 'fresh_target_check_required',
                    'bounded_hardware_acceptance_required']
        if primary['source_pose'] is None or primary['source_kind'] != 'bounded_scan_capture':
            blockers.append('source_pose_not_from_bounded_capture')
        targets.append(dict(instrument_id=asset['id'], name=asset['name'],
                            references=references, blockers=blockers))
    groups = {}
    for item in targets:
        ref = item['references'][0]
        group = groups.setdefault(ref['frame_id'], dict(
            frame_id=ref['frame_id'], source_pose=ref['source_pose'], instruments=[]))
        group['instruments'].append(item['instrument_id'])
    result = dict(schema_version=1, kind='revisit_reference_plan', map_id=map_id,
                  revision=revision, targets=targets,
                  source_groups=[groups[key] for key in sorted(groups)],
                  execution_enabled=False, motion_verified=False,
                  reading=None, reading_status='not_integrated')
    result['fingerprint'] = digest(result)
    return copy.deepcopy(result)


def validate_current(plan, state):
    if plan.get('kind') != 'revisit_reference_plan':
        raise ValueError('not a reference plan')
    current = reference_plan(state, plan['map_id'], plan['revision'])
    if current != plan:
        raise Conflict('reference selection or source geometry changed')
    return current


def read_reference_image(reference, directory):
    """Accept only a hash-named file immediately inside the given image folder."""
    name, sha = reference['image'], reference['sha256']
    if (not isinstance(name, str) or not isinstance(sha, str)
            or not re.match(r'^[a-f0-9]{64}$', sha)
            or name not in (sha + '.png', sha + '.jpg')):
        raise ValueError('reference image must use its content-addressed name')
    root = Path(directory).resolve()
    path = root / name
    if path.is_symlink() or path.resolve().parent != root:
        raise ValueError('reference image leaves the image directory')
    if path.stat().st_size > 25000000:
        raise ValueError('reference image exceeds size budget')
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != sha:
        raise ValueError('reference image hash changed')
    size, unused = image_size(data)
    if size != (reference['width'], reference['height']):
        raise ValueError('reference image dimensions changed')
    return data

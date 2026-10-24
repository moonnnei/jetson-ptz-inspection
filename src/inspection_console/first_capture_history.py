"""Trusted first-capture import; creates reference photos, never commissions poses."""
import hashlib
import json
from pathlib import Path
import re

from first_capture import validate_first_capture
from adaptive_capture import inside_frame
from planner import pose
from ring_capture import close_pose
from store import Store, bbox
from teaching import centered


def import_first_capture(store, directory, revision):
    root = Path(directory).resolve()
    path = root / 'batch.json'
    if path.is_symlink() or path.stat().st_size > 2000000:
        raise ValueError('invalid first-capture record')
    raw = path.read_bytes()
    batch = json.loads(raw.decode('utf-8'))
    state = store.snapshot()
    plan = batch['plan']
    if revision != state['revision'] or revision != plan['revision']:
        raise ValueError('first-capture review revision changed')
    validate_first_capture(plan, state)
    if (batch['status'] not in ('completed', 'failed', 'stopped')
            or batch.get('reading') is not None or batch.get('visit_pose_verified') is not False
            or batch.get('periodic_capture_enabled') is not False):
        raise ValueError('first capture cannot enable periodic capture or fabricate acceptance')
    stops = plan['stops']
    if len(batch['targets']) > len(stops):
        raise ValueError('too many first-capture records')
    if batch['status'] == 'completed' and (len(batch['targets']) != len(stops) or not batch.get('stop_confirmed')):
        raise ValueError('completed first capture lacks complete stopped records')
    results = []
    last_received = None
    for expected, item in zip(stops, batch['targets']):
        key = expected['id']
        if item['instrument_id'] != key or item['directory'] != key:
            raise ValueError('first-capture target order or directory differs')
        folder = root / key
        record_path = folder / 'record.json'
        if (folder.is_symlink() or record_path.is_symlink() or folder.resolve().parent != root
                or record_path.stat().st_size > 5000000):
            raise ValueError('invalid first-capture child record')
        child_raw = record_path.read_bytes()
        if hashlib.sha256(child_raw).hexdigest() != item['record_sha256']:
            raise ValueError('first-capture record changed')
        child = json.loads(child_raw.decode('utf-8'))
        if child['plan'] != expected['teaching_plan'] or child['status'] != item['status']:
            raise ValueError('first-capture child differs from the prepared plan')
        if child.get('reading') is not None or child.get('periodic_capture_enabled') is not False:
            raise ValueError('first-capture child changed scope')
        if child.get('stop_confirmed') is not item['stop_confirmed']:
            raise ValueError('first-capture child STOP differs from its batch record')
        result = dict(instrument_id=key, status='first_capture_failed', reading=None,
                      visit_pose_verified=False, quality_review_required=True,
                      error=child.get('error'), elapsed_seconds=child['elapsed_seconds'])
        if child['status'] == 'completed':
            if (not child.get('stop_confirmed') or not child.get('revisit_verified')
                    or not close_pose(child['restored_pose'], plan['anchor'])):
                raise ValueError('first capture lacks independent revisit, return or STOP')
            fresh = [f for f in child['captures'] if f['purpose'].startswith('independent_revisit_')]
            if (len(fresh) != 2 or fresh[0]['path'] == fresh[1]['path']
                    or not fresh[0]['received_monotonic'] < fresh[1]['received_monotonic']
                    or last_received is not None and fresh[0]['received_monotonic'] <= last_received):
                raise ValueError('first-capture revisit chronology is invalid')
            for frame in fresh:
                if frame.get('match', {}).get('status') != 'matched_reference':
                    raise ValueError('first-capture appearance not checked')
                region = bbox(frame['bbox'], frame['width'], frame['height'])
                ok, unused = centered(region, frame['width'], frame['height'], expected['teaching_plan']['center_tolerance'])
                adaptive = plan['mode']=='adaptive'
                if ((not adaptive and (not ok or not frame.get('centered') or pose(frame['pose'])['zoom']!=2.))
                        or adaptive and not inside_frame(frame, expected['teaching_plan']['frame_margin'])
                        or frame.get('orientation') != 'rotate180_from_raw'
                        or not close_pose(frame['pose'], child['visit_pose'])):
                    raise ValueError('first-capture reference not centered at the taught pose')
            last_received = fresh[-1]['received_monotonic']
            frame = fresh[-1]
            if (child.get('evidence', {}).get('image_sha256') != frame['sha256']
                    or child['evidence'].get('image') != frame['path']):
                raise ValueError('first-capture evidence is not the final fresh observation')
            if not re.fullmatch(r'frame-[0-9]{3}\.jpg', frame['path']):
                raise ValueError('invalid first-capture image name')
            image = folder / frame['path']
            if image.is_symlink() or image.resolve().parent != folder.resolve():
                raise ValueError('first-capture image leaves its directory')
            saved = store.save_image(image, frame['sha256'])
            if (saved['width'], saved['height']) != (2560, 1440):
                raise ValueError('first-capture image resolution changed')
            result.update(status='first_capture_reference_ready', image=saved['image'],
                          image_sha256=frame['sha256'], bbox=region, pose=frame['pose'],
                          center_error=frame['center_error'], captured_at=frame['captured_at'],
                          optical_zoom=frame['pose']['zoom'], independent_revisit_checked=True,
                          capture_mode=plan['mode'],
                          center_status='centered' if all(f['centered'] for f in fresh) else 'reachable_pose_with_residual',
                          frame_bounds_checked=True,
                          orientation=frame['orientation'], reference_observations=2,
                          image_status='first_capture_reference_quality_review_required')
        else:
            if (child['status'] not in ('failed', 'stopped') or batch['status'] == 'completed'
                    or len(results) != len(batch['targets']) - 1):
                raise ValueError('failed target must end the first-capture batch')
            if child['captures']:
                frame = child['captures'][-1]
                if not re.fullmatch(r'frame-[0-9]{3}\.jpg', frame['path']):
                    raise ValueError('invalid first-capture diagnostic image name')
                image = folder / frame['path']
                if image.is_symlink() or image.resolve().parent != folder.resolve():
                    raise ValueError('diagnostic image leaves its capture directory')
                saved = store.save_image(image, frame['sha256'])
                result.update(image=saved['image'], captured_at=frame['captured_at'],
                              image_status='first_capture_failed_diagnostic_not_accepted')
        results.append(result)
    for stop in stops[len(batch['targets']):]:
        results.append(dict(instrument_id=stop['id'], status='first_capture_not_attempted',
                            reading=None, visit_pose_verified=False))
    key = 'first-capture-' + hashlib.sha256(raw).hexdigest()[:16]
    job = dict(id=key, map_id=plan['map_id'], mode='first_capture_batch', status=batch['status'],
               started_at=batch['started_at'], finished_at=batch['finished_at'],
               route=[s['id'] for s in stops], results=results, error=batch.get('error'),
               elapsed_seconds=batch['elapsed_seconds'], stop_confirmed=batch.get('stop_confirmed') is True,
               plan_fingerprint=plan['fingerprint'], record_sha256=hashlib.sha256(raw).hexdigest())

    def apply(current):
        Store.editable(current, plan['map_id'])
        validate_first_capture(plan, dict(current, revision=revision))
        if any(j['id'] == key for j in current['jobs']):
            raise ValueError('first-capture batch already imported')
        current['jobs'].append(job)
        Store.pause(current, 'first_capture_imported_periodic_not_enabled')
        return dict(job_id=key, ready_references=sum(r['status'] == 'first_capture_reference_ready' for r in results))
    return store.change(revision, 'first_capture_import', dict(id=key, map_id=plan['map_id']), apply)

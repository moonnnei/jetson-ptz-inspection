"""Persistent human-reviewed instrument registry; Python 3.5 standard library.

SQLite stores a small versioned aggregate plus an append-only audit table. Image
bytes live in content-addressed files outside the database. All writes use
BEGIN IMMEDIATE and compare the browser's revision before applying changes.
"""
import copy
from contextlib import contextmanager
import datetime
import hashlib
import json
import math
import os
import re
from pathlib import Path
import sqlite3
import struct
import threading
import uuid

from planner import pose
from ring_coordinates import parameters as ring_parameters,ring_box


class Conflict(ValueError):
    pass


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def identifier(prefix):
    return prefix + '-' + uuid.uuid4().hex[:16]


def text(value, limit=100):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError('text must contain 1-%d characters' % limit)
    if any(ord(c) < 32 for c in value):
        raise ValueError('control characters are not allowed')
    return value.strip()


def boolean(value):
    if type(value) is not bool:
        raise ValueError('expected a boolean')
    return value


def bbox(value, width, height):
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError('bbox requires four source-image pixel coordinates')
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or
           not math.isfinite(v) for v in value):
        raise ValueError('bbox contains invalid coordinates')
    x1, y1, x2, y2 = value
    if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
        raise ValueError('bbox must lie inside the original image')
    if x2 - x1 < 2 or y2 - y1 < 2:
        raise ValueError('bbox is smaller than two pixels')
    return [round(float(v), 4) for v in value]


def image_size(data):
    """Read dimensions without installing an image library on the old TX2."""
    if data[:8] == b'\x89PNG\r\n\x1a\n' and data[12:16] == b'IHDR' and len(data) >= 24:
        return struct.unpack('>II', data[16:24]), '.png'
    if data[:2] == b'\xff\xd8':
        offset = 2
        while offset + 4 < len(data):
            if data[offset] != 255:
                break
            while offset < len(data) and data[offset] == 255:
                offset += 1
            marker = data[offset]
            offset += 1
            if marker in (0xd8, 0xd9) or 0xd0 <= marker <= 0xd7:
                continue
            if marker == 0xda:
                break
            length = struct.unpack('>H', data[offset:offset + 2])[0]
            if length < 2 or offset + length > len(data):
                break
            if marker in (0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf):
                height, width = struct.unpack('>HH', data[offset + 3:offset + 7])
                return (width, height), '.jpg'
            offset += length
    raise ValueError('only PNG or JPEG source images are accepted')


def overview_transform(value, source_size, overview_size):
    if (not isinstance(value, list) or len(value)!=3 or
            any(not isinstance(row,list) or len(row)!=3 for row in value)):
        raise ValueError('overview transform must be a 3 by 3 matrix')
    if any(type(v) not in (int,float) or not math.isfinite(v) or abs(v)>1e7 for row in value for v in row):
        raise ValueError('invalid overview transform values')
    a,b,c=value
    determinant=a[0]*(b[1]*c[2]-b[2]*c[1])-a[1]*(b[0]*c[2]-b[2]*c[0])+a[2]*(b[0]*c[1]-b[1]*c[0])
    if abs(determinant)<1e-12:
        raise ValueError('singular overview transform')
    width,height=source_size
    signs=[]
    for x,y in ((0,0),(width,0),(width,height),(0,height)):
        divisor=c[0]*x+c[1]*y+c[2]
        if abs(divisor)<1e-8:
            raise ValueError('overview projection at infinity')
        signs.append(divisor)
        px,py=(a[0]*x+a[1]*y+a[2])/divisor,(b[0]*x+b[1]*y+b[2])/divisor
        if not (-1<=px<=overview_size[0]+1 and -1<=py<=overview_size[1]+1):
            raise ValueError('source view lies outside overview')
    if min(signs)*max(signs)<=0:
        raise ValueError('overview projection crosses infinity')
    return value


def initial_state():
    return dict(schema_version=1, maps=[], frames=[], candidates=[], instruments=[],
                next_instrument=1, jobs=[],
                schedule=dict(map_id=None, interval_seconds=300, enabled=False,
                              state='not_configured', next_due_at=None,
                              semantics='fixed_delay_after_completion'))


class Store:
    def __init__(self, directory):
        # Python 3.5 Path.resolve() requires the path to exist.
        self.root = Path(os.path.abspath(str(directory)))
        self.root.mkdir(parents=True, exist_ok=True)
        self.root = self.root.resolve()
        self.images = self.root / 'images'
        self.images.mkdir(exist_ok=True)
        self.path = self.root / 'registry.sqlite3'
        self.lock = threading.RLock()
        with self.connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS registry (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, payload TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, revision INTEGER NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS job_history (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL UNIQUE, payload TEXT NOT NULL)')
            db.execute('INSERT OR IGNORE INTO registry VALUES (1,0,?)', (json.dumps(initial_state()),))
        if os.name != 'nt':
            os.chmod(str(self.root), 0o700)
            os.chmod(str(self.path), 0o600)
        if self.snapshot()['schema_version'] != 1:
            raise ValueError('unsupported registry schema; do not overwrite database')

    @contextmanager
    def connection(self):
        db = sqlite3.connect(str(self.path), timeout=8)
        try:
            db.execute('PRAGMA busy_timeout=8000')
            with db:
                yield db
        finally:
            db.close()

    def snapshot(self, include_history=False):
        with self.lock, self.connection() as db:
            rev, payload = db.execute('SELECT revision,payload FROM registry WHERE id=1').fetchone()
            data = json.loads(payload)
            data['revision'] = rev
            data['audit'] = [dict(id=row[0], timestamp=row[1], revision=row[2],
                                  action=row[3], detail=json.loads(row[4]))
                             for row in db.execute('SELECT * FROM audit ORDER BY id DESC LIMIT 80')]
            data['job_history_count'] = db.execute('SELECT COUNT(*) FROM job_history').fetchone()[0]
            if include_history:
                data['job_history'] = [json.loads(row[0]) for row in db.execute('SELECT payload FROM job_history ORDER BY id')]
            return data

    def change(self, expected, action, detail, function):
        if expected is not None and (type(expected) is not int or expected < 0):
            raise ValueError('revision must be a nonnegative integer')
        with self.lock, self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            revision, payload = db.execute('SELECT revision,payload FROM registry WHERE id=1').fetchone()
            if expected is not None and revision != expected:
                raise Conflict('data changed in another request; refresh and retry')
            state = json.loads(payload)
            result = function(state)
            for job in state['jobs']:
                if job['status'] != 'running':
                    db.execute('INSERT OR IGNORE INTO job_history(job_id,payload) VALUES (?,?)',
                               (job['id'], json.dumps(job, ensure_ascii=False, allow_nan=False)))
            revision += 1
            db.execute('UPDATE registry SET revision=?,payload=? WHERE id=1',
                       (revision, json.dumps(state, ensure_ascii=False, allow_nan=False)))
            db.execute('INSERT INTO audit(timestamp,revision,action,detail) VALUES (?,?,?,?)',
                       (now(), revision, action, json.dumps(detail, ensure_ascii=False, allow_nan=False)))
            return dict(revision=revision, result=result)

    @staticmethod
    def find(state, kind, key):
        for item in state[kind]:
            if item['id'] == key:
                return item
        raise ValueError('unknown ' + kind + ' id')

    @staticmethod
    def editable(state, map_id):
        item = Store.find(state, 'maps', map_id)
        if item['status'] == 'invalidated':
            raise ValueError('map is invalidated; create a new map before reviewing')
        if any(j['status'] == 'running' for j in state['jobs']):
            raise Conflict('an inspection owns this registry; stop it before editing')
        return item

    @staticmethod
    def pause(state, reason):
        state['schedule'].update(enabled=False, state=reason, next_due_at=None)

    def import_manifest(self, manifest):
        """CLI-only trusted import. Historical observations never create visit poses."""
        title = text(manifest['title'])
        provenance = manifest.get('provenance', 'historical_import')
        if provenance not in ('historical_import', 'bounded_scan_capture'):
            raise ValueError('unsupported map provenance')
        frames = manifest.get('frames', [])
        if not 1 <= len(frames) <= 240:
            raise ValueError('import requires 1-240 frames')
        map_id = identifier('map')
        overview = None
        if manifest.get('overview'):
            entry=manifest['overview']
            if entry.get('full_panorama') is not False or entry.get('ptz_calibration') is not False:
                raise ValueError('overview import is only an uncalibrated sector preview')
            nonlinear = entry.get('kind') in ('stitched_ring_preview','source_contact_sheet','pose_projected_ring_preview')
            if entry.get('kind') not in ('registered_sector_preview','view_index','stitched_ring_preview','source_contact_sheet','pose_projected_ring_preview'):
                raise ValueError('unsupported overview kind')
            overview=self.save_image(entry['path'],entry.get('sha256'))
            count=entry.get('registered_views')
            if type(count) is not int or not (0 if nonlinear else 1)<=count<=len(frames) or entry.get('total_views')!=len(frames):
                raise ValueError('overview view counts do not match source frames')
            overview.update(registered_views=count,total_views=len(frames),kind=entry['kind'],
                            full_panorama=False,ptz_calibration=False)
            if nonlinear:
                stitched=entry.get('stitched_views')
                if count!=0 or entry.get('source_mapping')!='source_picker_only' or type(stitched) is not int or not 0<=stitched<=len(frames):
                    raise ValueError('ring preview requires a separate source picker and valid stitched count')
                if entry['kind']=='source_contact_sheet' and stitched!=0:
                    raise ValueError('contact sheet is not a stitched panorama')
                overview.update(stitched_views=stitched,source_mapping='source_picker_only')
                if entry['kind']=='pose_projected_ring_preview':
                    projected=entry.get('projected_views')
                    if stitched!=0 or type(projected) is not int or projected!=len(frames):
                        raise ValueError('angle projection must preserve all source views without claiming feature stitching')
                    overview['projected_views']=projected
        new_frames, candidates = [], []
        for entry in frames:
            path = Path(entry['path'])
            saved=self.save_image(path,entry.get('sha256'))
            size=(saved['width'],saved['height'])
            frame_id = identifier('frame')
            frame = dict(id=frame_id, map_id=map_id, image=saved['image'], sha256=saved['sha256'],
                         width=size[0], height=size[1], title=text(entry.get('title', path.stem)),
                         captured_at=entry.get('captured_at'), imported_at=now(),
                         source_kind='bounded_scan_capture' if provenance=='bounded_scan_capture' else 'historical_image',
                         orientation=entry.get('orientation', 'as_imported'),
                         pose=pose(entry['pose']) if entry.get('pose') else None)
            frame['orientation'] = text(frame['orientation'], 50)
            if frame['captured_at'] is not None:
                frame['captured_at'] = text(frame['captured_at'], 50)
            frame['to_overview']=None
            if entry.get('to_overview') is not None:
                if overview is None:
                    raise ValueError('transform requires an overview image')
                if overview.get('source_mapping')=='source_picker_only':
                    raise ValueError('nonlinear preview cannot use a planar source homography')
                frame['to_overview']=overview_transform(entry['to_overview'],size,(overview['width'],overview['height']))
            if entry.get('raw_source_path'):
                frame['raw_source']=self.save_image(entry['raw_source_path'],entry.get('raw_source_sha256'))
            new_frames.append(frame)
            for detection in entry.get('detections', []):
                confidence = detection.get('confidence')
                if confidence is not None and (isinstance(confidence, bool) or not isinstance(confidence, (float, int)) or not math.isfinite(confidence) or not 0 <= confidence <= 1):
                    raise ValueError('invalid confidence')
                candidates.append(dict(id=identifier('candidate'), map_id=map_id,
                    frame_id=frame_id, bbox=bbox(detection['bbox'], *size),
                    confidence=confidence, state='pending', source=text(detection.get('source', 'imported_detector')),
                    created_at=now(), instrument_id=None, merged_into=None))
        if len(candidates) > 500:
            raise ValueError('map exceeds the 500-candidate review budget')
        if overview and sum(f['to_overview'] is not None for f in new_frames)!=overview['registered_views']:
            raise ValueError('registered view count differs from transforms')
        def apply(state):
            if len(state['maps']) >= 20:
                raise ValueError('this prototype supports at most 20 maps')
            state['maps'].append(dict(id=map_id, title=title, created_at=now(),
                status='review', kind='view_index', provenance=provenance,
                geometry_status='not_calibrated', motion_verified=False,
                overview=overview,
                note='局部配准总览仅用于查看；云台回访尚未标定。' if overview else '原始视图索引；未生成全景拼接或验证云台回访坐标。'))
            state['frames'].extend(new_frames)
            state['candidates'].extend(candidates)
            return map_id
        return self.change(None, 'import_map', dict(title=title, frames=len(new_frames),
                           candidates=len(candidates)), apply)

    def save_image(self, path, expected=None):
        path=Path(path)
        if path.stat().st_size>25000000:
            raise ValueError('source image exceeds 25 MB')
        data=path.read_bytes()
        size,suffix=image_size(data)
        if min(size)<1 or max(size)>24000 or size[0]*size[1]>80000000:
            raise ValueError('invalid or excessive image dimensions')
        sha=hashlib.sha256(data).hexdigest()
        if expected and expected!=sha:
            raise ValueError('source image hash does not match manifest')
        filename=sha+suffix
        target=self.images/filename
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest()!=sha:
                raise ValueError('stored image hash mismatch')
        else:
            with target.open('xb') as stream:stream.write(data)
        return dict(image=filename,sha256=sha,width=size[0],height=size[1])

    def review(self, revision, candidate_id, action, name=None, target_id=None, box=None):
        def apply(state):
            candidate = self.find(state, 'candidates', candidate_id)
            self.editable(state, candidate['map_id'])
            asset = self.find(state, 'instruments', candidate['instrument_id']) if candidate['instrument_id'] else None
            if action == 'confirm':
                if candidate['state'] == 'merged':
                    raise ValueError('restore a merged candidate before confirming it')
                if asset is None:
                    key = 'M%04d' % state['next_instrument']
                    state['next_instrument'] += 1
                    asset = dict(id=key, map_id=candidate['map_id'], candidate_id=candidate_id,
                        name=text(name) if name else '仪表 ' + key, enabled=True,
                        pose_status='unverified', visit_pose=None, created_at=now(),
                        reading=None, reading_status='not_integrated')
                    state['instruments'].append(asset)
                    candidate['instrument_id'] = key
                else:
                    if name is not None:
                        asset['name'] = text(name)
                    asset['enabled'] = True
                candidate.update(state='confirmed', merged_into=None)
                result = asset['id']
            elif action in ('reject', 'restore'):
                candidate.update(state='rejected' if action == 'reject' else 'pending', merged_into=None)
                if asset:
                    asset['enabled'] = False
                result = candidate_id
            elif action == 'merge':
                target = self.find(state, 'candidates', target_id)
                if target['map_id'] != candidate['map_id'] or target_id == candidate_id or target['state'] in ('merged', 'rejected'):
                    raise ValueError('merge requires a different active candidate in the same map')
                # No alias chains: callers must merge leaf observations into one keeper.
                if any(c.get('merged_into') == candidate_id for c in state['candidates']):
                    raise ValueError('candidate already has merged observations; choose it as keeper')
                candidate.update(state='merged', merged_into=target_id)
                if asset:
                    asset['enabled'] = False
                result = target_id
            elif action == 'bbox':
                if candidate['state'] == 'merged':
                    raise ValueError('restore merged candidate before editing')
                frame = self.find(state, 'frames', candidate['frame_id'])
                candidate['bbox'] = bbox(box, frame['width'], frame['height'])
                candidate['source'] = 'human_corrected'
                candidate['panorama_box_edited'] = True
                if asset:
                    asset.update(pose_status='unverified', visit_pose=None)
                result = candidate_id
            else:
                raise ValueError('unknown review action')
            self.pause(state, 'review_changed_requires_rearm')
            return result
        return self.change(revision, 'review_' + action,
                           dict(candidate_id=candidate_id, target_id=target_id, name=name, bbox=box), apply)

    def add_candidate(self, revision, frame_id, box):
        key = identifier('candidate')
        def apply(state):
            frame = self.find(state, 'frames', frame_id)
            self.editable(state, frame['map_id'])
            if sum(c['map_id'] == frame['map_id'] for c in state['candidates']) >= 500:
                raise ValueError('map candidate budget reached')
            state['candidates'].append(dict(id=key, map_id=frame['map_id'], frame_id=frame_id,
                bbox=bbox(box, frame['width'], frame['height']), confidence=None,
                state='pending', source='human_added', created_at=now(), instrument_id=None, merged_into=None))
            self.pause(state, 'review_changed_requires_rearm')
            return key
        return self.change(revision, 'candidate_add', dict(frame_id=frame_id, bbox=box), apply)

    def update_instrument(self, revision, key, name, enabled):
        def apply(state):
            asset = self.find(state, 'instruments', key)
            self.editable(state, asset['map_id'])
            candidate = self.find(state, 'candidates', asset['candidate_id'])
            if boolean(enabled) and candidate['state'] != 'confirmed':
                raise ValueError('only confirmed instruments can be enabled')
            asset.update(name=text(name), enabled=enabled)
            self.pause(state, 'registry_changed_requires_rearm')
            return key
        return self.change(revision, 'instrument_update', dict(id=key, name=name, enabled=enabled), apply)

    def import_panorama_candidates(self,revision,payload):
        """Trusted CLI import; attach image-space proposals without confirming IDs."""
        required={'map_id','registry_revision','overview_sha256','projection','source_binding',
                  'observations','model_sha256','operation_id'}
        if not isinstance(payload,dict) or set(payload)!=required:
            raise ValueError('panorama import fields do not match')
        projection=ring_parameters(payload['projection'])
        for field,length in (('overview_sha256',64),('model_sha256',64),('operation_id',24)):
            if not isinstance(payload[field],str) or not re.match('^[a-f0-9]{%d}$'%length,payload[field]):
                raise ValueError('invalid panorama '+field)
        observations=payload['observations']
        if not isinstance(observations,list) or len(observations)>40:raise ValueError('panorama observation budget exceeded')
        def score(value):
            if type(value) not in (int,float) or not math.isfinite(value) or not 0<=value<=1:
                raise ValueError('invalid panorama score')
            return float(value)
        def overlap(a,b):
            area=max(0,min(a[2],b[2])-max(a[0],b[0]))*max(0,min(a[3],b[3])-max(a[1],b[1]))
            return area/max(1e-9,(a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-area)
        def apply(state):
            scene=self.editable(state,payload['map_id'])
            overview=scene.get('overview')
            if not overview or overview['kind']!='pose_projected_ring_preview' or overview['sha256']!=payload['overview_sha256']:
                raise ValueError('panorama source changed')
            if (projection['width'],projection['height'])!=(overview['width'],overview['height']):
                raise ValueError('projection dimensions differ from stored overview')
            operations=scene.setdefault('panorama_import_operations',[])
            if payload['operation_id'] in operations:raise ValueError('panorama result already imported')
            frames=[f for f in state['frames'] if f['map_id']==scene['id']]
            binding=[{k:f[k] for k in ('sha256','width','height','orientation','pose')} for f in frames]
            canonical=lambda v:json.dumps(v,sort_keys=True,separators=(',',':'),allow_nan=False)
            if sorted(canonical(v) for v in payload['source_binding'])!=sorted(canonical(v) for v in binding):
                raise ValueError('source images, poses or orientation changed')
            if any((f['width'],f['height'],f['orientation'])!=(projection['source_width'],projection['source_height'],projection['source_orientation']) for f in frames):
                raise ValueError('source dimensions or orientation differ from projection')
            by_sha={f['sha256']:f for f in frames}
            added=[];linked=[];seen=set()
            for observation in observations:
                fields={'frame_sha256','bbox','panorama_bbox','panorama_confidence','source_confidence',
                        'source_recheck','confidence','observation_id'}
                optional={'source_association','source_similarity'}
                if not isinstance(observation,dict) or not fields<=set(observation) or set(observation)-fields-optional:
                    raise ValueError('invalid panorama observation fields')
                oid=observation['observation_id']
                if not isinstance(oid,str) or not re.match('^[a-f0-9]{24}$',oid) or oid in seen:
                    raise ValueError('invalid or duplicate panorama observation ID')
                seen.add(oid)
                frame=by_sha.get(observation['frame_sha256'])
                if frame is None:raise ValueError('panorama observation source is unknown')
                box=bbox(observation['bbox'],frame['width'],frame['height'])
                pano=ring_box(observation['panorama_bbox'],projection)
                confidence=score(observation['confidence']);pano_score=score(observation['panorama_confidence'])
                source_score=observation['source_confidence']
                if observation['source_recheck']=='matched_detector':source_score=score(source_score)
                elif observation['source_recheck']!='unverified' or source_score is not None:
                    raise ValueError('invalid source recheck state')
                association=observation.get('source_association','geometric_only')
                similarity=observation.get('source_similarity')
                if association not in ('geometric_only','appearance_consistent'):
                    raise ValueError('invalid source association state')
                if similarity is not None and (type(similarity) not in (float,int) or not math.isfinite(similarity) or not -1<=similarity<=1):
                    raise ValueError('invalid source appearance score')
                if association=='appearance_consistent' and (similarity is None or similarity<.65):
                    raise ValueError('source appearance association has not passed')
                matches=[c for c in state['candidates'] if c['map_id']==scene['id'] and c['frame_id']==frame['id'] and overlap(c['bbox'],box)>=.45]
                if matches:
                    candidate=max(matches,key=lambda c:overlap(c['bbox'],box));linked.append(candidate['id'])
                else:
                    if sum(c['map_id']==scene['id'] for c in state['candidates'])>=500:
                        raise ValueError('map candidate budget reached')
                    candidate=dict(id=identifier('candidate'),map_id=scene['id'],frame_id=frame['id'],bbox=box,
                        confidence=confidence,state='pending',source='panorama_tiled',created_at=now(),
                        instrument_id=None,merged_into=None)
                    state['candidates'].append(candidate);added.append(candidate['id'])
                evidence=dict(observation_id=oid,bbox=pano,overview_sha256=overview['sha256'],
                    panorama_confidence=pano_score,source_confidence=source_score,
                    source_recheck=observation['source_recheck'],model_sha256=payload['model_sha256'],
                    source_association=association,source_similarity=similarity)
                existing=candidate.setdefault('panorama_observations',[])
                if not any(o['observation_id']==oid for o in existing):existing.append(evidence)
                candidate['panorama_observations']=existing[-8:]
            overview.update(ring_projection=projection,source_mapping='estimated_cylindrical_review')
            scene['panorama_detection']=dict(operation_id=payload['operation_id'],imported_at=now(),
                observations=len(observations),added=len(added),linked=len(linked),model_sha256=payload['model_sha256'],
                human_review_required=True)
            operations.append(payload['operation_id']);scene['panorama_import_operations']=operations[-20:]
            self.pause(state,'panorama_review_requires_rearm')
            return dict(added=added,linked=linked,observations=len(observations),motion_sent=False)
        return self.change(revision,'panorama_candidates_import',dict(map_id=payload['map_id'],
            operation_id=payload['operation_id'],observations=len(observations)),apply)

    def configure_schedule(self, revision, map_id, seconds):
        if type(seconds) is not int or not 60 <= seconds <= 86400:
            raise ValueError('interval must be an integer from 60 to 86400 seconds')
        def apply(state):
            self.editable(state, map_id)
            state['schedule'].update(map_id=map_id, interval_seconds=seconds, enabled=False,
                state='awaiting_hardware_validation', next_due_at=None)
            return copy.deepcopy(state['schedule'])
        return self.change(revision, 'schedule_configure', dict(map_id=map_id, seconds=seconds), apply)

    def invalidate(self, revision, map_id):
        def apply(state):
            item = self.editable(state, map_id)
            item.update(status='invalidated', motion_verified=False, invalidated_at=now())
            for asset in state['instruments']:
                if asset['map_id'] == map_id:
                    asset.update(enabled=False, pose_status='invalidated', visit_pose=None)
            self.pause(state, 'map_invalidated')
            return map_id
        return self.change(revision, 'map_invalidated', dict(map_id=map_id), apply)

    def recover(self):
        state = self.snapshot()
        if not state['schedule']['enabled'] and not any(j['status'] == 'running' for j in state['jobs']):
            return
        def apply(state):
            self.pause(state, 'restart_requires_revalidation')
            for job in state['jobs']:
                if job['status'] == 'running':
                    job.update(status='interrupted', finished_at=now(), error='server_restart')
        self.change(None, 'restart_recovery', {}, apply)

    def image_path(self, key):
        # Only content-addressed images registered in the database are reachable.
        state=self.snapshot()
        names={f['image'] for f in state['frames']}
        names.update(f['raw_source']['image'] for f in state['frames'] if f.get('raw_source'))
        names.update(m['overview']['image'] for m in state['maps'] if m.get('overview'))
        if key not in names:
            raise ValueError('unknown image')
        path = (self.images / key).resolve()
        if path.parent != self.images.resolve() or (self.images / key).is_symlink():
            raise ValueError('invalid image path')
        return path

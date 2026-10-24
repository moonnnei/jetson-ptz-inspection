"""TX2-only IO for a finite meter pilot, not a periodic hardware adapter."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

from planner import pose
from references import read_reference_image, reference_plan, validate_current
from ring_capture import close_pose
from scan_capture import ISAPIScanIO
from store import image_size, now


class TeachingIO(ISAPIScanIO):
    def __init__(self, camera, project_root, console_root, references):
        ISAPIScanIO.__init__(self,camera)
        self.root, self.console = Path(project_root), Path(console_root)
        self.references = references
        self.plan = None
        self.capture_root = None
        self.hard_stop_failed = False
        from store import Store
        self.store = Store(self.console/'var/inspection-console')

    def state(self):
        return self.store.snapshot()

    def guard(self, cancel):
        if cancel.is_set() or self.hard_stop_failed:
            raise RuntimeError('cancelled_or_STOP_unconfirmed')
        state = self.state()
        validate_current(self.references,state)
        if state['schedule']['enabled'] or any(j['status']=='running' for j in state['jobs']):
            raise RuntimeError('registry_became_active')

    def preflight(self, plan):
        if plan['kind']=='bounded_meter_adaptive_capture':
            from adaptive_capture import validate_adaptive
            self.plan=validate_adaptive(plan,self.references)
        elif plan['kind']=='bounded_meter_constrained_capture':
            from best_effort import validate_capture_plan
            self.plan=validate_capture_plan(plan,self.references)
        else:
            from teaching import validate_plan
            self.plan = validate_plan(plan,self.references)
        self.guard(__import__('threading').Event())
        report=ISAPIScanIO.preflight(self)
        if not close_pose(report['anchor'],plan['anchor']):
            raise ValueError('camera_pose_changed_since_plan')
        for target in self.references['targets']:
            read_reference_image(target['references'][0],self.console/'var/inspection-console/images')
        if subprocess.check_output(['python2','-c','import cv2;print(cv2.__version__)']).decode().strip()!='3.3.1':
            raise ValueError('review image-worker runtime before using this profile')
        return report

    def stop(self):
        try:
            ISAPIScanIO.stop(self)
        except Exception:
            self.hard_stop_failed=True
            raise

    def move(self,target,cancel):
        target={k:round(v,1) for k,v in pose(target).items()}
        if not self.plan:raise ValueError('read-only preflight is required')
        for axis in ('pan','tilt','zoom'):
            lower,upper=self.plan[axis+'_limits']
            if not lower-.001<=target[axis]<=upper+.001:
                raise ValueError('target_outside_prepared_'+axis+'_limits')
        try:
            with self.command_lock:
                self.guard(cancel)
                current=self.position()
                if any(abs(target[k]-current[k])>20.11 for k in ('pan','tilt')):
                    raise ValueError('individual_absolute_step_exceeds_20_degrees')
                raw={k:int(round(v*10)) for k,v in target.items()}
                body=('<PTZData version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">'
                    '<AbsoluteHigh><elevation>{tilt}</elevation><azimuth>{pan}</azimuth>'
                    '<absoluteZoom>{zoom}</absoluteZoom></AbsoluteHigh></PTZData>').format(**raw).encode()
                self.camera.xml('/ISAPI/PTZCtrl/channels/1/absolute',body)
            deadline,stable=time.monotonic()+7,0
            while time.monotonic()<deadline:
                self.guard(cancel)
                stable=stable+1 if close_pose(self.position(),target) else 0
                if stable>=3:
                    self.stop()
                    if cancel.wait(.8):raise RuntimeError('teaching_cancelled')
                    return
                cancel.wait(.15)
            raise RuntimeError('absolute_position_timeout')
        finally:
            self.stop()

    def vision(self, request):
        worker=self.root/'tools/scene_mapping/revisit_worker.py'
        raw=subprocess.check_output(['python2',str(worker)],
            input=json.dumps(request).encode(), timeout=12)
        return json.loads(raw.decode())

    def capture(self,directory,index,cancel):
        self.guard(cancel)
        before=self.position()
        started=time.monotonic()
        raw=self.snapshot()
        received=time.monotonic()
        captured=now()
        self.guard(cancel)
        after=self.position()
        if not close_pose(before,after):raise RuntimeError('camera_moved_during_capture')
        size,suffix=image_size(raw)
        if size!=(2560,1440) or suffix!='.jpg' or not raw.endswith(b'\xff\xd9'):
            raise ValueError('unexpected_or_incomplete_camera_snapshot')
        directory=Path(directory)
        self.capture_root=directory
        raw_name='frame-%03d-raw.jpg'%index
        name='frame-%03d.jpg'%index
        (directory/raw_name).write_bytes(raw)
        normalized=self.vision(dict(action='normalize',raw=str(directory/raw_name),
            output=str(directory/name),orientation='rotate180_from_raw'))
        return dict(path=name,raw=raw_name,sha256=hashlib.sha256((directory/name).read_bytes()).hexdigest(),
            raw_sha256=hashlib.sha256(raw).hexdigest(),width=normalized['width'],height=normalized['height'],
            pose=after,captured_at=captured,received_monotonic=received,
            request_started_monotonic=started,request_seconds=received-started,
            orientation='rotate180_from_raw')

    def source_path(self, reference):
        if reference.get('local_capture'):
            path=self.capture_root/reference['image']
            if path.resolve().parent!=self.capture_root.resolve():raise ValueError('invalid local frame')
            if hashlib.sha256(path.read_bytes()).hexdigest()!=reference['sha256']:
                raise ValueError('local reference hash changed')
            return path
        # The human reference stays in the existing database's image directory.
        read_reference_image(reference,self.console/'var/inspection-console/images')
        return self.console/'var/inspection-console/images'/reference['image']

    def locate(self, reference, frame):
        return self.vision(dict(action='locate',source=str(self.source_path(reference)),
            target=str(self.capture_root/frame['path']),bbox=reference['bbox']))

    def measure(self,before,after):
        return self.vision(dict(action='measure',source=str(self.capture_root/before['path']),
            target=str(self.capture_root/after['path'])))

    def evidence(self,frame,directory,instrument_id):
        directory=Path(directory)
        crop,overlay=instrument_id+'-crop.jpg',instrument_id+'-overlay.jpg'
        metrics=self.vision(dict(action='evidence',source=str(directory/frame['path']),
            target=str(directory/frame['path']),bbox=frame['bbox'],
            crop=str(directory/crop),overlay=str(directory/overlay),instrument_id=instrument_id))
        return dict(instrument_id=instrument_id, image=frame['path'], image_sha256=frame['sha256'],
            crop=crop,crop_sha256=hashlib.sha256((directory/crop).read_bytes()).hexdigest(),
            overlay=overlay,overlay_sha256=hashlib.sha256((directory/overlay).read_bytes()).hexdigest(),
            bbox=frame['bbox'],pose=frame['pose'],center_error=frame['center_error'],
            captured_at=frame['captured_at'],metrics=metrics,reading=None,
            focus=self.camera.xml('/ISAPI/Image/channels/1/focusConfiguration'))

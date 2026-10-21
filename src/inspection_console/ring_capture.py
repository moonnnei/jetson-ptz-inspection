"""Explicit, bounded single-row 0..350 degree acquisition; no automatic startup.

Wide scans are a separate profile. The original five-degree adapter stays
restricted. Coverage and panorama closure are unknown until image validation.
"""
import copy
import hashlib
import json
import math
import threading
import time

from planner import number, pose
from scan_capture import BoundedCapture, ISAPIScanIO
from store import image_size, now


def ring_plan(anchor, step=20):
    anchor = pose(anchor)
    step = number(step, 20, 30, 'maximum step')
    count = int(math.ceil(350. / step))
    pans = [round(350. * i / count, 1) for i in range(count + 1)]
    if anchor['pan'] >= 175:
        pans.reverse()
    points = [dict(pan=p, tilt=anchor['tilt'], zoom=1.) for p in pans]
    result = dict(schema_version=1, kind='bounded_single_row_ring_scan',
        anchor=anchor, points=points, maximum_step_degrees=step,
        optical_zoom=1., tilt=anchor['tilt'], pan_limits=[0.,350.],
        expected_frames=len(points), budget_seconds=360,
        return_to_anchor_on_success=True, return_after_failure=False,
        physical_axis_wrap=False, full_panorama=False,
        coverage_status='unverified_until_adjacent_and_end_view_checks',
        execution_enabled=False)
    result['fingerprint'] = hashlib.sha256(json.dumps(result,sort_keys=True,
        separators=(',',':'),allow_nan=False).encode('utf-8')).hexdigest()
    return result


def validate_plan(plan):
    if plan != ring_plan(plan['anchor'],plan['maximum_step_degrees']):
        raise ValueError('ring plan content changed or profile is unsupported')
    return copy.deepcopy(plan)


def close_pose(a, b):
    return all(abs(a[k]-b[k])<=t for k,t in (('pan',.3),('tilt',.3),('zoom',.05)))


def transit(start, target, step):
    """Subdivide without wrapping across the mechanical endpoints."""
    start, target = pose(start), pose(target)
    if start['tilt'] != target['tilt'] or start['zoom'] != target['zoom']:
        raise ValueError('transit only changes pan')
    count=max(1,int(math.ceil(abs(target['pan']-start['pan'])/step)))
    return [dict(target,pan=round(start['pan']+(target['pan']-start['pan'])*i/count,1))
            for i in range(1,count+1)]


class RingCapture(BoundedCapture):
    MAX_BUDGET = 360

    def __init__(self, io, directory, plan, cancel=None, budget=360):
        self.plan=validate_plan(plan)
        BoundedCapture.__init__(self,io,directory,cancel,budget)
        self.state.update(kind='bounded_single_row_ring_scan', plan=self.plan,
                          actions=[], panorama_closure_verified=False)

    def move(self, target, phase):
        self.check()
        action=dict(phase=phase,target=target,started_at=now(),completed=False)
        self.state['actions'].append(action);self.persist()
        self.io.move(target,self.cancel)
        self.check()
        actual=pose(self.io.position())
        if not close_pose(actual,target):
            raise RuntimeError('ring_position_not_reached')
        action.update(completed=True,actual=actual,finished_at=now());self.persist()

    def travel(self, target, phase):
        for point in transit(pose(self.io.position()),target,self.plan['maximum_step_degrees']):
            self.move(point,phase)

    def run(self):
        self.root.mkdir(parents=True,exist_ok=False)
        self.deadline=time.monotonic()+self.budget
        worker=threading.Thread(target=self.watchdog,name='ring-scan-watchdog')
        worker.daemon=True;worker.start()
        try:
            self.check()
            anchor=self.plan['anchor']
            if not close_pose(pose(self.io.position()),anchor):
                raise RuntimeError('camera_pose_changed_since_plan')
            self.state.update(status='running',anchor=anchor,started_at=now());self.persist()
            self.move(dict(anchor,zoom=1.),'wide_zoom')
            self.travel(self.plan['points'][0],'approach_scan_start')
            for index,target in enumerate(self.plan['points']):
                self.move(target,'scan_view')
                before=pose(self.io.position());self.check()
                raw=self.io.snapshot();received=now();self.check()
                after=pose(self.io.position())
                if not close_pose(before,target) or not close_pose(after,target) or not close_pose(before,after):
                    raise RuntimeError('ring_camera_moved_during_capture')
                size,suffix=image_size(raw)
                if suffix!='.jpg' or not raw.endswith(b'\xff\xd9') or len(raw)>12000000 or min(size)<1 or max(size)>8192:
                    raise ValueError('incomplete_or_excessive_snapshot')
                name='view-%02d.jpg'%(index+1)
                (self.root/name).write_bytes(raw)
                self.state['frames'].append(dict(path=name,title='环视视图 %02d'%(index+1),
                    width=size[0],height=size[1],sha256=hashlib.sha256(raw).hexdigest(),
                    captured_at=received,pose=after,source_kind='bounded_scan_capture',
                    orientation='raw_camera',detections=[]))
                self.persist()
            self.travel(dict(anchor,zoom=1.),'return_pan')
            self.move(anchor,'restore_original_zoom')
            self.state.update(status='completed',restored_pose=pose(self.io.position()))
        except Exception as exc:
            self.state.update(status='stopped' if self.cancel.is_set() else 'failed',error=type(exc).__name__)
        finally:
            try:self.stop()
            except Exception:self.state.update(status='failed',error='STOP_unconfirmed')
            self.done.set();worker.join(timeout=4)
            if worker.is_alive() or self.stop_error:
                self.state.update(status='failed',error=self.stop_error or 'watchdog_still_active')
            self.state['finished_at']=now();self.persist()
        if self.state['status']=='completed':
            manifest=dict(title='单行环视扫描 / '+self.state['started_at'],
                provenance='bounded_scan_capture',frames=self.state['frames'],
                scan_plan=self.plan,full_panorama=False)
            (self.root/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
        return self.state


class ISAPIRingIO(ISAPIScanIO):
    """Finite S6 profile: fixed tilt, zoom 1x, <=30 degree individual moves."""
    def __init__(self,camera,plan):
        ISAPIScanIO.__init__(self,camera)
        self.plan=validate_plan(plan)
        self.accepted=False

    def preflight(self):
        result=ISAPIScanIO.preflight(self)
        if not close_pose(result['anchor'],self.plan['anchor']):
            raise ValueError('camera pose changed; rebuild and review the scan plan')
        self.accepted=True
        return dict(anchor=result['anchor'],plan=self.plan,motion_sent=False)

    def move(self,target,cancel):
        target=pose(target)
        if not self.accepted:
            raise ValueError('ring preflight required')
        anchor=self.plan['anchor']
        if abs(target['tilt']-anchor['tilt'])>.001:
            raise ValueError('ring tilt must remain fixed')
        if abs(target['zoom']-1.)>.001 and not close_pose(target,anchor):
            raise ValueError('non-wide zoom is allowed only at the original anchor')
        with self.command_lock:
            if cancel.is_set() or self.stop_error:
                raise RuntimeError('cancelled_or_stop_unconfirmed')
            current=self.position()
            if abs(current['tilt']-anchor['tilt'])>.3:
                raise RuntimeError('camera tilt changed during ring scan')
            if abs(target['pan']-current['pan'])>self.plan['maximum_step_degrees']+.11:
                raise ValueError('ring command exceeds the finite pan step')
            raw={k:int(round(v*10)) for k,v in target.items()}
            body=('<PTZData version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">'
                '<AbsoluteHigh><elevation>{tilt}</elevation><azimuth>{pan}</azimuth>'
                '<absoluteZoom>{zoom}</absoluteZoom></AbsoluteHigh></PTZData>').format(**raw).encode()
        try:
            with self.command_lock:
                if cancel.is_set() or self.stop_error:
                    raise RuntimeError('cancelled_or_stop_unconfirmed')
                self.camera.xml('/ISAPI/PTZCtrl/channels/1/absolute',body)
            deadline,stable=time.monotonic()+7,0
            while time.monotonic()<deadline:
                if cancel.is_set():raise RuntimeError('ring_scan_cancelled')
                reached=close_pose(self.position(),target)
                stable=stable+1 if reached else 0
                if stable>=3:
                    self.stop()
                    if cancel.wait(.7):raise RuntimeError('ring_scan_cancelled')
                    return
                cancel.wait(.15)
            raise RuntimeError('ring_position_timeout')
        finally:self.stop()

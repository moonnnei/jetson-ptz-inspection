"""Finite user-selected rectangular PTZ scan. Encoder axes never wrap."""
import copy
import hashlib
import json
import math
import threading
import time

from planner import number, pose, scan_grid
from ring_capture import RingCapture, ISAPIRingIO, close_pose


def area_plan(anchor, bounds, focal=1542.84959310078):
    anchor=pose(anchor)
    if not isinstance(bounds,dict) or set(bounds)!={'pan_min','pan_max','tilt_min','tilt_max'}:
        raise ValueError('select both scan corners')
    focal=number(focal,256,25600,'wide focal pixels')
    hfov=math.degrees(2*math.atan(1280./focal));vfov=math.degrees(2*math.atan(720./focal))
    # Corners delimit the desired scene, not compulsory camera stops. One
    # wide frame already covers a small range; visiting both corners repeats it.
    checked=scan_grid(dict(bounds,hfov=hfov,vfov=vfov,overlap=.35,zoom=1.))
    def centers(low,high,fov):
        span=high-low
        if span<=fov*.85:return [(low+high)/2.]
        left,right=low+fov*.425,high-fov*.425
        count=max(2,int(math.ceil((right-left)/(fov*.65)))+1)
        return [left+(right-left)*i/(count-1) for i in range(count)]
    xs=centers(bounds['pan_min'],bounds['pan_max'],hfov);ys=centers(bounds['tilt_min'],bounds['tilt_max'],vfov)
    grid=dict(rows=len(ys),columns=len(xs),count=len(xs)*len(ys),points=[])
    for i,y in enumerate(ys):
        grid['points'].extend(dict(pan=x,tilt=y,zoom=1.) for x in (xs if i%2==0 else list(reversed(xs))))
    if grid['count']>24 or grid['rows']>6:
        raise ValueError('range exceeds 24 views / 6 rows; select a smaller area')
    points=[{k:round(p[k],1) for k in ('pan','tilt','zoom')} for p in grid['points']]
    if len({(p['pan'],p['tilt']) for p in points})!=len(points):
        raise ValueError('scan corners are too close for encoder resolution')
    # Enter the nearest end of the serpentine path; do not cross axis endpoints.
    if math.hypot(points[-1]['pan']-anchor['pan'],points[-1]['tilt']-anchor['tilt']) < math.hypot(points[0]['pan']-anchor['pan'],points[0]['tilt']-anchor['tilt']):
        points.reverse()
    result=dict(schema_version=1,kind='bounded_area_scan',anchor=anchor,bounds=copy.deepcopy(bounds),
        points=points,rows=grid['rows'],columns=grid['columns'],expected_frames=len(points),
        maximum_step_degrees=20.,budget_seconds=360,optical_zoom=1.,overlap=.35,
        focal_pixels=focal,return_to_anchor_on_success=True,return_after_failure=False,
        physical_axis_wrap=False,full_panorama=False,execution_enabled=False,
        coverage_status='approximate_fov_requires_scene_validation')
    result['fingerprint']=hashlib.sha256(json.dumps(result,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    return result


def validate_area(plan):
    if plan!=area_plan(plan['anchor'],plan['bounds'],plan['focal_pixels']):
        raise ValueError('area plan changed')
    return copy.deepcopy(plan)


class AreaScanIO(ISAPIRingIO):
    def __init__(self,camera,plan):
        # Reuse verified encoder, STOP and snapshot implementations, with a
        # distinct profile rather than broadening the old single-row adapter.
        from scan_capture import ISAPIScanIO
        ISAPIScanIO.__init__(self,camera)
        self.plan=validate_area(plan);self.accepted=False;self.settle_seconds=.7

    def move(self,target,cancel):
        target=pose(target)
        if not self.accepted:raise ValueError('area preflight required')
        if abs(target['zoom']-1.)>.001 and not close_pose(target,self.plan['anchor']):
            raise ValueError('zoom restoration only at the anchor')
        current=self.position()
        if max(abs(target[k]-current[k]) for k in ('pan','tilt'))>20.11:
            raise ValueError('area command exceeds finite step')
        if cancel.is_set() or self.stop_error:raise RuntimeError('area cancelled')
        raw={k:int(round(v*10)) for k,v in target.items()}
        body=('<PTZData version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">'
            '<AbsoluteHigh><elevation>{tilt}</elevation><azimuth>{pan}</azimuth>'
            '<absoluteZoom>{zoom}</absoluteZoom></AbsoluteHigh></PTZData>').format(**raw).encode()
        try:
            with self.command_lock:
                if cancel.is_set() or self.stop_error:raise RuntimeError('area cancelled')
                self.camera.xml('/ISAPI/PTZCtrl/channels/1/absolute',body)
            deadline=time.monotonic()+7;stable=0
            while time.monotonic()<deadline:
                if cancel.is_set():raise RuntimeError('area cancelled')
                stable=stable+1 if close_pose(self.position(),target) else 0
                if stable>=3:
                    self.stop()
                    if cancel.wait(self.settle_seconds):raise RuntimeError('area cancelled')
                    return
                cancel.wait(.15)
            raise RuntimeError('area position timeout')
        finally:self.stop()


class AreaCapture(RingCapture):
    def __init__(self,io,directory,plan,cancel=None,budget=360):
        from scan_capture import BoundedCapture
        self.plan=validate_area(plan)
        BoundedCapture.__init__(self,io,directory,cancel,budget)
        self.state.update(kind='bounded_area_scan',plan=self.plan,actions=[],panorama_closure_verified=False)

    def move(self,target,phase):
        # Acquisition spacing follows the lens FOV. Intermediate motor steps
        # retain the original limit without needlessly capturing every step.
        start=self.io.position();target=pose(target)
        count=max(1,int(math.ceil(max(abs(target[k]-start[k]) for k in ('pan','tilt'))/20.)))
        for i in range(1,count+1):
            p=dict(target,**{k:round(start[k]+(target[k]-start[k])*i/count,1) for k in ('pan','tilt')})
            RingCapture.move(self,p,phase)

    def travel(self,target,phase):
        start=self.io.position();target=pose(target)
        count=max(1,int(math.ceil(max(abs(target[k]-start[k]) for k in ('pan','tilt'))/20.)))
        for i in range(1,count+1):
            self.move(dict(target,**{k:round(start[k]+(target[k]-start[k])*i/count,1) for k in ('pan','tilt')}),phase)

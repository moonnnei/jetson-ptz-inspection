"""Bounded web PTZ with a single replaceable intent, never a command FIFO."""
import copy
import math
import threading
import time
from planner import number,pose
from store import Conflict,identifier,now


def click_target(current,x,y,focal=1542.84959310078):
    current=pose(current);x=number(x,0,1,'x');y=number(y,0,1,'y')
    focal=number(focal,256,25600,'focal')*current['zoom']
    wanted=dict(current,pan=current['pan']-math.degrees(math.atan((x-.5)*2560/focal)),
                tilt=current['tilt']-math.degrees(math.atan((y-.5)*1440/focal)))
    target=dict(wanted,pan=round(max(0.,min(350.,wanted['pan'])),1),tilt=round(max(0.,min(90.,wanted['tilt'])),1))
    return target,any(abs(target[k]-wanted[k])>.06 for k in ('pan','tilt'))


class ManualControl:
    def __init__(self,camera=None,focal=1542.84959310078,lease=None,check=None):
        self.camera=camera;self.focal=focal;self.lease=lease;self.check=check or (lambda:None)
        self.lock=threading.RLock();self.io_lock=threading.Lock();self.thread=None
        self.views={};self.epoch=0;self.result=None;self.stop_error=None
        self.cached=None;self.cached_at=0;self.owns_motion=False
        self.pending=None;self.executing=False;self.cancel=threading.Event()
        self.wake=threading.Event();self.closed=threading.Event()

    def active(self):
        with self.lock:return self.executing or self.pending is not None

    def position(self):
        v=self.camera.status()
        value=pose(dict(pan=float(v['azimuth'])/10.,tilt=float(v['elevation'])/10.,zoom=float(v['absoluteZoom'])/10.))
        with self.lock:self.cached=value;self.cached_at=time.monotonic()
        return value

    def status(self):
        if self.camera is not None and not self.active() and time.monotonic()-self.cached_at>.4 and self.io_lock.acquire(False):
            try:
                if not self.active():self.position()
            except Exception:pass
            finally:self.io_lock.release()
        with self.lock:
            result=dict(available=self.camera is not None and self.cached is not None,active=self.active(),
                stop_error=self.stop_error,result=copy.deepcopy(self.result),calibration='estimated_fov',epoch=self.epoch,queue_policy='latest_wins')
            if self.cached is not None:
                result.update(pose=copy.deepcopy(self.cached),pose_age_ms=round((time.monotonic()-self.cached_at)*1000,1))
                stamp=time.monotonic();self.views={k:v for k,v in self.views.items() if stamp-v[0]<2.}
                if not self.stop_error and stamp-self.cached_at<1.5:
                    key=identifier('view');self.views[key]=(stamp,self.epoch,copy.deepcopy(self.cached))
                    while len(self.views)>24:self.views.pop(next(iter(self.views)))
                    result['view_token']=key
            return result

    def start(self,command,view_token=None,x=None,y=None,step=2.):
        if self.camera is None or self.closed.is_set() or self.stop_error:raise ValueError('manual camera unavailable')
        self.check();current=None
        if command=='center' and not self.active():
            with self.io_lock:current=self.position()
        with self.lock:
            current=current or copy.deepcopy(self.cached);limited=False;target=None
            if command=='center':
                view=self.views.get(view_token);moving=self.active()
                if (view is None or time.monotonic()-view[0]>2. or view[1]!=self.epoch or current is None or
                    any(abs(current[k]-view[2][k])>t for k,t in (('pan',2. if moving else .3),('tilt',2. if moving else .3),('zoom',.1)))):
                    raise Conflict('画面位置已变化，请在当前画面重新点击')
                target,limited=click_target(view[2],x,y,self.focal)
            elif command not in ('left','right','up','down','zoom_in','zoom_out'):raise ValueError('unknown manual action')
            step=number(step,.2,5.,'step');old=self.result.get('id') if self.active() and self.result else None
            self.cancel.set();self.epoch+=1;self.views.clear()
            item=dict(id=identifier('manual'),command=command,status='accepted',target=target,step=step,
                limited=limited,started_at=now(),estimated_center=command=='center',supersedes=old)
            self.pending=item;self.result=copy.deepcopy(item);self.wake.set()
            if self.thread is None:
                self.thread=threading.Thread(target=self._dispatch,name='web-manual-latest');self.thread.daemon=True;self.thread.start()
            return copy.deepcopy(item)

    def _dispatch(self):
        while not self.closed.is_set():
            self.wake.wait(.2)
            with self.lock:
                item=self.pending;self.pending=None;self.wake.clear()
                if item is None:continue
                self.executing=True;cancel=threading.Event();self.cancel=cancel
            self._execute(item,cancel)
            with self.lock:self.executing=False

    def _execute(self,item,cancel):
        started=time.monotonic();watch_done=threading.Event();watcher=None
        def report(**fields):
            item.update(**fields)
            with self.lock:
                if self.result and self.result['id']==item['id']:self.result=copy.deepcopy(item)
        def stop():
            try:self.camera.stop();self.stop_error=None
            except Exception:self.stop_error='manual_STOP_unconfirmed';raise
        def watch():
            if not watch_done.wait(25):
                cancel.set()
                try:stop()
                except Exception:pass
        try:
            with self.lease():
                self.owns_motion=True
                try:
                    self.check()
                    if cancel.is_set():raise RuntimeError('superseded')
                    with self.io_lock:current=self.position()
                    target=item['target']
                    if target is None:
                        target=dict(current);step=item['step']
                        axis,delta={'left':('pan',step),'right':('pan',-step),'up':('tilt',step),'down':('tilt',-step),
                                    'zoom_in':('zoom',.1),'zoom_out':('zoom',-.1)}[item['command']]
                        lo,hi=(1.,2.) if axis=='zoom' else (0.,350. if axis=='pan' else 90.)
                        wanted=target[axis]+delta;target[axis]=round(max(lo,min(hi,wanted)),1);item['limited']=abs(target[axis]-wanted)>.06
                    report(status='moving',target=target)
                    watcher=threading.Thread(target=watch,name='manual-deadline');watcher.daemon=True;watcher.start()
                    count=max(1,int(math.ceil(max(abs(target[k]-current[k]) for k in ('pan','tilt'))/20.)))
                    for i in range(1,count+1):
                        if cancel.is_set():raise RuntimeError('superseded')
                        p=dict(target,**{k:round(current[k]+(target[k]-current[k])*i/count,1) for k in ('pan','tilt')})
                        raw={k:int(round(v*10)) for k,v in p.items()}
                        body=('<PTZData version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">'
                            '<AbsoluteHigh><elevation>{tilt}</elevation><azimuth>{pan}</azimuth>'
                            '<absoluteZoom>{zoom}</absoluteZoom></AbsoluteHigh></PTZData>').format(**raw).encode()
                        self.camera.xml('/ISAPI/PTZCtrl/channels/1/absolute',body)
                        deadline=time.monotonic()+7;stable=0
                        while time.monotonic()<deadline:
                            if cancel.is_set():raise RuntimeError('superseded')
                            actual=self.position()
                            reached=all(abs(actual[k]-p[k])<=t for k,t in (('pan',.3),('tilt',.3),('zoom',.05)))
                            stable=stable+1 if reached else 0
                            if stable>=2:break
                            cancel.wait(.08)
                        else:raise RuntimeError('manual_position_timeout')
                    if cancel.is_set():raise RuntimeError('superseded')
                    report(status='completed',actual=self.position())
                finally:
                    try:stop()
                    finally:
                        watch_done.set()
                        if watcher:watcher.join(timeout=1)
                        self.owns_motion=False
        except Exception as exc:report(status='superseded' if cancel.is_set() else 'failed',error_type=type(exc).__name__)
        finally:
            watch_done.set()
            if self.stop_error:
                report(status='failed')
                with self.lock:self.pending=None;self.wake.clear()
            report(elapsed_seconds=time.monotonic()-started,finished_at=now())

    def stop(self):
        with self.lock:self.pending=None;self.cancel.set();self.epoch+=1;self.views.clear();self.wake.clear()
        if self.camera is not None and (self.owns_motion or self.stop_error):
            try:self.camera.stop();self.stop_error=None
            except Exception:self.stop_error='manual_STOP_unconfirmed';raise
        return dict(stop_requested=True)

    def close(self):
        self.stop();self.closed.set();self.wake.set()
        if self.thread:self.thread.join(timeout=12)
        if self.thread and self.thread.is_alive():raise RuntimeError('manual_shutdown_unconfirmed')

"""Pure target association and bounded visual step control; Python 3.5+."""
import math
import time

def center(b):
    return ((b[0]+b[2])/2, (b[1]+b[3])/2)

def area(b):
    return max(0,b[2]-b[0])*max(0,b[3]-b[1])

def iou(a,b):
    inter=max(0,min(a[2],b[2])-max(a[0],b[0]))*max(0,min(a[3],b[3])-max(a[1],b[1]))
    return inter/max(1e-9,area(a)+area(b)-inter)

class Target:
    def __init__(self): self.reset()
    def reset(self):
        self.box=None; self.last_seen=0; self.hits=0
    def update(self, persons, now):
        if self.box is not None and now-self.last_seen>=2:self.reset()
        threshold=0.55 if self.hits>=2 else 0.75
        candidates=[p for p in persons if p['confidence']>=threshold and 0.002<=area(p['bbox'])<=0.9]
        if self.box is not None and now-self.last_seen<2:
            x,y=center(self.box)
            candidates=[p for p in candidates if math.hypot(center(p['bbox'])[0]-x,center(p['bbox'])[1]-y)<0.25
                        and 0.3<area(p['bbox'])/max(area(self.box),1e-6)<3]
            if not candidates:
                if self.hits<2:self.hits=0
                return None
            p=max(candidates,key=lambda p:iou(self.box,p['bbox'])-math.hypot(center(p['bbox'])[0]-x,center(p['bbox'])[1]-y))
        else:
            self.reset()
            if not candidates:return None
            p=min(candidates,key=lambda p:math.hypot(center(p['bbox'])[0]-0.5,center(p['bbox'])[1]-0.5))
        self.box=p['bbox']; self.last_seen=now; self.hits+=1
        return p if self.hits>=2 else None

def validate_matrix(m):
    if len(m)!=2 or any(len(r)!=2 for r in m) or not all(math.isfinite(v) for r in m for v in r):
        raise ValueError('Invalid calibration')
    a,b=m[0];c,d=m[1];det=a*d-b*c
    if abs(det)<0.000025 or min(math.hypot(a,c),math.hypot(b,d))<0.005:
        raise ValueError('Camera movement too small or calibration axes ambiguous')
    if max(abs(v) for r in m for v in r)>0.2:raise ValueError('Calibration displacement too large')
    return det

def correction(box,m,mode='fixed',speed=20):
    """Choose one calibrated axis; adaptive mode changes duration, not speed.

    The calibration columns describe image displacement for one 500-ms pulse
    at the selected speed. Fixed mode preserves the original behavior. Adaptive mode
    aims for the existing deadband boundary with 80% of the estimated step,
    bounded to 100..500 ms. The duration/displacement relation is only a local
    approximation and must be checked again after a zoom or mounting change.
    """
    if mode not in ('fixed','adaptive'):
        raise ValueError('Invalid control mode')
    if type(speed) is not int or not 20<=speed<=40:raise ValueError('Invalid calibrated speed')
    x,y=center(box);ex=x-0.5;ey=y-0.5
    ex=0 if abs(ex)<=0.08 else ex
    ey=0 if abs(ey)<=0.10 else ey
    if ex==0 and ey==0:return None
    det=validate_matrix(m);a,b=m[0];c,d=m[1]
    # Preserve the reported center error while scaling only the part outside
    # the deadband. Using the full center error would still command a maximum
    # pulse immediately outside the deadband for small calibrated movements.
    dx,dy=ex,ey
    if mode=='adaptive':
        dx=math.copysign(max(0,abs(ex)-0.08),ex)
        dy=math.copysign(max(0,abs(ey)-0.10),ey)
    pan=(-d*dx+b*dy)/det;tilt=(c*dx-a*dy)/det
    axis='pan' if abs(pan)>=abs(tilt) else 'tilt'
    v=pan if axis=='pan' else tilt
    action=dict(axis=axis,speed=speed if v>0 else -speed,duration=500,error=[ex,ey])
    if mode=='adaptive':
        action['duration']=max(100,min(500,int(round(500*0.8*abs(v)))))
        action['step_magnitude']=abs(v)
    return action

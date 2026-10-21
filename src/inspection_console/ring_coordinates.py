"""Estimated cylindrical image coordinates for review; never PTZ calibration.

The formulas invert the preview renderer. They do not correct physical axis
tilt, parallax or lens distortion. A source image remains the review authority.
"""
import math


def parameters(value):
    required={'version','projection','width','height','source_width','source_height',
              'focal_pixels','yaw_image_sign','source_orientation','calibration_validated'}
    if not isinstance(value,dict) or set(value)!=required:
        raise ValueError('cylindrical review geometry fields do not match')
    if value['version']!=1 or value['projection']!='angle_assisted_cylindrical' or value['calibration_validated'] is not False:
        raise ValueError('only explicitly uncalibrated cylindrical review coordinates are supported')
    for key,low,high in (('width',512,8192),('height',32,2048),('source_width',32,8192),('source_height',32,8192)):
        if type(value[key]) is not int or not low<=value[key]<=high:raise ValueError('invalid '+key)
    f=value['focal_pixels']
    if type(f) not in (int,float) or not math.isfinite(f) or not value['source_width']*.1<=f<=value['source_width']*10:
        raise ValueError('invalid preview focal estimate')
    if type(value['yaw_image_sign']) is not int or value['yaw_image_sign'] not in (-1,1):raise ValueError('invalid yaw sign')
    if value['source_orientation'] not in ('rotate180_from_raw','as_imported','raw_camera'):
        raise ValueError('unsupported source orientation')
    return dict(value)


def ring_box(box,projection):
    p=parameters(projection);w,h=p['width'],p['height']
    if not isinstance(box,list) or len(box)!=4 or any(type(x) not in (int,float) or not math.isfinite(x) for x in box):
        raise ValueError('invalid panorama box')
    x1,y1,x2,y2=box
    if not (0<=x1<w and x1<x2<=x1+w/2 and 0<=y1<y2<=h):
        raise ValueError('panorama box exceeds the bounded cyclic image')
    return [float(x) for x in box]


def segments(box,projection):
    x1,y1,x2,y2=ring_box(box,projection);width=projection['width']
    if x2<=width:return [[x1,y1,x2,y2]]
    return [[x1,y1,float(width),y2],[0.,y1,x2-width,y2]]


def source_point(x,y,pan,projection):
    p=parameters(projection);f=p['focal_pixels'];scale=p['width']/(2*math.pi)
    dx=x-p['source_width']/2.
    theta=math.radians(pan)-p['yaw_image_sign']*math.atan(dx/f)
    return theta*scale,p['height']/2.+scale*(y-p['source_height']/2.)/math.sqrt(f*f+dx*dx)


def panorama_point(u,v,pan,projection):
    p=parameters(projection);f=p['focal_pixels'];scale=p['width']/(2*math.pi)
    delta=(math.radians(pan)-u/scale+math.pi)%(2*math.pi)-math.pi
    if abs(delta)>=math.atan(p['source_width']/(2*f)):raise ValueError('panorama point outside source view')
    dx=p['yaw_image_sign']*f*math.tan(delta)
    x=p['source_width']/2.+dx
    y=p['source_height']/2.+(v-p['height']/2.)/scale*math.sqrt(f*f+dx*dx)
    if not 0<=y<=p['source_height']:raise ValueError('panorama point outside source height')
    return x,y


def source_box(box,pan,projection):
    p=parameters(projection)
    x1,y1,x2,y2=box
    if not (0<=x1<x2<=p['source_width'] and 0<=y1<y2<=p['source_height']):raise ValueError('invalid source box')
    xs=[x1,x2,(x1+x2)/2.]
    if x1<p['source_width']/2.<x2:xs.append(p['source_width']/2.)
    points=[source_point(x,y,pan,p) for x in xs for y in (y1,y2,(y1+y2)/2.)]
    lowx=min(x for x,y in points);highx=max(x for x,y in points)
    lowy=max(0.,min(y for x,y in points));highy=min(p['height'],max(y for x,y in points))
    if highy<=lowy:raise ValueError('source box lies outside preview height')
    start=lowx%p['width']
    return ring_box([start,lowy,start+highx-lowx,highy],p)


def panorama_box(box,pan,projection):
    p=parameters(projection);x1,y1,x2,y2=ring_box(box,p)
    xs=[x1,x2,(x1+x2)/2.]
    center=(pan%360)/360.*p['width']
    for candidate in (center,center+p['width']):
        if x1<candidate<x2:xs.append(candidate)
    points=[panorama_point(x,y,pan,p) for x in xs for y in (y1,y2,(y1+y2)/2.)]
    return [min(x for x,y in points),min(y for x,y in points),
            max(x for x,y in points),max(y for x,y in points)]


def source_choices(box,frames,projection):
    p=parameters(projection);box=ring_box(box,p)
    azimuth=((box[0]+box[2])/2.%p['width'])/p['width']*360.
    choices=[]
    for frame in frames:
        if (frame['width'],frame['height'],frame['orientation'])!=(p['source_width'],p['source_height'],p['source_orientation']) or not frame.get('pose'):
            continue
        try:mapped=panorama_box(box,frame['pose']['pan'],p)
        except ValueError:continue
        cost=abs((frame['pose']['pan']-azimuth+180)%360-180)
        choices.append(dict(frame_id=frame['id'],sha256=frame['sha256'],bbox=mapped,angular_distance=cost))
    return sorted(choices,key=lambda c:(c['angular_distance'],c['frame_id']))


def decorate_state(state):
    """Transient browser overlays; the raw export/database stay authoritative."""
    maps={m['id']:m for m in state['maps']}
    frames={f['id']:f for f in state['frames']}
    for candidate in state['candidates']:
        scene=maps[candidate['map_id']]
        overview=scene.get('overview') or {}
        projection=overview.get('ring_projection')
        if not projection:continue
        frame=frames[candidate['frame_id']]
        try:
            evidence=[o for o in candidate.get('panorama_observations',[]) if o['overview_sha256']==overview['sha256']]
            if evidence and not candidate.get('panorama_box_edited'):
                box=evidence[-1]['bbox'];kind='panorama_detector_region'
            else:
                box=source_box(candidate['bbox'],frame['pose']['pan'],projection)
                kind='estimated_from_source_region'
            candidate['overview_boxes']=segments(box,projection)
            candidate['overview_geometry']=kind
        except (ValueError,KeyError,TypeError):
            candidate['overview_boxes']=[]
            candidate['overview_geometry']='unavailable'
    return state

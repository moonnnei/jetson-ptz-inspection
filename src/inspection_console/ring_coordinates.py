"""Estimated cylindrical image coordinates for review; never PTZ calibration.

The formulas invert the preview renderer. They do not correct physical axis
tilt, parallax or lens distortion. A source image remains the review authority.
"""
import math


def parameters(value):
    required={'version','projection','width','height','source_width','source_height',
              'focal_pixels','yaw_image_sign','source_orientation','calibration_validated'}
    if not isinstance(value,dict) or set(value) not in (required,required|{'row_tilts','row_height'}):
        raise ValueError('cylindrical review geometry fields do not match')
    if value['version'] not in (1,2) or value['projection']!='angle_assisted_cylindrical' or value['calibration_validated'] is not False:
        raise ValueError('only explicitly uncalibrated cylindrical review coordinates are supported')
    for key,low,high in (('width',512,8192),('height',32,2048),('source_width',32,8192),('source_height',32,8192)):
        if type(value[key]) is not int or not low<=value[key]<=high:raise ValueError('invalid '+key)
    f=value['focal_pixels']
    if type(f) not in (int,float) or not math.isfinite(f) or not value['source_width']*.1<=f<=value['source_width']*10:
        raise ValueError('invalid preview focal estimate')
    if type(value['yaw_image_sign']) is not int or value['yaw_image_sign'] not in (-1,1):raise ValueError('invalid yaw sign')
    if value['source_orientation'] not in ('rotate180_from_raw','as_imported','raw_camera'):
        raise ValueError('unsupported source orientation')
    if value['version']==2:
        rows=value.get('row_tilts');h=value.get('row_height')
        if (not isinstance(rows,list) or not 1<=len(rows)<=6 or
                any(type(t) not in (int,float) or not math.isfinite(t) or not 0<=t<=90 for t in rows) or
                rows!=sorted(set(rows)) or type(h) is not int or h<32 or h*len(rows)!=value['height']):
            raise ValueError('invalid cylindrical row index')
    elif set(value)!=required:raise ValueError('extra projection fields')
    return dict(value)


def row_geometry(p,tilt):
    if p['version']==1:return p['height'],0
    rows=p['row_tilts']
    if tilt is None:raise ValueError('multirow source requires tilt')
    i=min(range(len(rows)),key=lambda i:abs(rows[i]-tilt))
    if abs(rows[i]-tilt)>.11:raise ValueError('source tilt differs from row binding')
    return p['row_height'],i*p['row_height']


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


def source_point(x,y,pan,projection,tilt=None):
    p=parameters(projection);f=p['focal_pixels'];scale=p['width']/(2*math.pi)
    height,offset=row_geometry(p,tilt)
    dx=x-p['source_width']/2.
    theta=math.radians(pan)-p['yaw_image_sign']*math.atan(dx/f)
    return theta*scale,offset+height/2.+scale*(y-p['source_height']/2.)/math.sqrt(f*f+dx*dx)


def panorama_point(u,v,pan,projection,tilt=None):
    p=parameters(projection);f=p['focal_pixels'];scale=p['width']/(2*math.pi)
    height,offset=row_geometry(p,tilt)
    if not offset<=v<=offset+height:raise ValueError('box crosses source rows')
    delta=(math.radians(pan)-u/scale+math.pi)%(2*math.pi)-math.pi
    if abs(delta)>=math.atan(p['source_width']/(2*f)):raise ValueError('panorama point outside source view')
    dx=p['yaw_image_sign']*f*math.tan(delta)
    x=p['source_width']/2.+dx
    y=p['source_height']/2.+(v-offset-height/2.)/scale*math.sqrt(f*f+dx*dx)
    if not 0<=y<=p['source_height']:raise ValueError('panorama point outside source height')
    return x,y


def source_box(box,pan,projection,tilt=None):
    p=parameters(projection)
    x1,y1,x2,y2=box
    if not (0<=x1<x2<=p['source_width'] and 0<=y1<y2<=p['source_height']):raise ValueError('invalid source box')
    xs=[x1,x2,(x1+x2)/2.]
    if x1<p['source_width']/2.<x2:xs.append(p['source_width']/2.)
    height,offset=row_geometry(p,tilt)
    points=[source_point(x,y,pan,p,tilt) for x in xs for y in (y1,y2,(y1+y2)/2.)]
    lowx=min(x for x,y in points);highx=max(x for x,y in points)
    lowy=max(float(offset),min(y for x,y in points));highy=min(offset+height,max(y for x,y in points))
    if highy<=lowy:raise ValueError('source box lies outside preview height')
    start=lowx%p['width']
    return ring_box([start,lowy,start+highx-lowx,highy],p)


def panorama_box(box,pan,projection,tilt=None):
    p=parameters(projection);x1,y1,x2,y2=ring_box(box,p)
    xs=[x1,x2,(x1+x2)/2.]
    center=(pan%360)/360.*p['width']
    for candidate in (center,center+p['width']):
        if x1<candidate<x2:xs.append(candidate)
    points=[panorama_point(x,y,pan,p,tilt) for x in xs for y in (y1,y2,(y1+y2)/2.)]
    return [min(x for x,y in points),min(y for x,y in points),
            max(x for x,y in points),max(y for x,y in points)]


def source_choices(box,frames,projection):
    p=parameters(projection);box=ring_box(box,p)
    azimuth=((box[0]+box[2])/2.%p['width'])/p['width']*360.
    choices=[]
    for frame in frames:
        if (frame['width'],frame['height'],frame['orientation'])!=(p['source_width'],p['source_height'],p['source_orientation']) or not frame.get('pose'):
            continue
        try:mapped=panorama_box(box,frame['pose']['pan'],p,frame['pose']['tilt'])
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
            annotation=candidate.get('manual_overview') or {}
            if annotation.get('overview_sha256')==overview['sha256'] and not candidate.get('panorama_box_edited'):
                box=annotation['bbox'];kind='human_overview_region'
            elif evidence and not candidate.get('panorama_box_edited'):
                box=evidence[-1]['bbox'];kind='panorama_detector_region'
            else:
                box=source_box(candidate['bbox'],frame['pose']['pan'],projection,frame['pose']['tilt'])
                kind='estimated_from_source_region'
            candidate['overview_boxes']=segments(box,projection)
            candidate['overview_geometry']=kind
        except (ValueError,KeyError,TypeError):
            candidate['overview_boxes']=[]
            candidate['overview_geometry']='unavailable'
    return state

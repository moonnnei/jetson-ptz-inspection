"""Image-only initialization IPC, compatible with Python 2.7/OpenCV 3.3.

Reuses the existing shape proposals, ORB registration and cylindrical renderer.
No network, registry mutation, training or camera commands.
"""
from __future__ import division
import hashlib
import json
import math
import os
import sys

import cv2
import numpy as np
from geometry import align, prepare
from ring_projection import estimate_focal, render
from shape_candidates import propose


def read(path):
    with open(path, 'rb') as stream:
        raw = stream.read(12000001)
    if not 4 <= len(raw) <= 12000000:
        raise ValueError('image size outside worker budget')
    image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if image is None or min(image.shape[:2]) < 16 or max(image.shape[:2]) > 8192:
        raise ValueError('image dimensions outside worker budget')
    return image


def write(path, image, png=False):
    ok, raw = cv2.imencode('.png' if png else '.jpg', image,
                           [] if png else [cv2.IMWRITE_JPEG_QUALITY, 96])
    if not ok:
        raise ValueError('image encoding failed')
    with open(path, 'wb') as stream:
        stream.write(raw.tostring())
    return hashlib.sha256(raw.tostring()).hexdigest()


def crops(image):
    h,w = image.shape[:2]
    boxes = [dict(box=[0,0,w,h],source='full_view')]
    tw,th = int(math.ceil(w*.65)),int(math.ceil(h*.65))
    for y in (0,h-th):
        for x in (0,w-tw):
            boxes.append(dict(box=[x,y,x+tw,y+th],source='overlap_tile'))
    for p in propose(image,6)['proposals']:
        boxes.append(dict(box=p['crop'],source='shape_'+p['id']))
    unique = []
    for item in boxes:
        if not any(item['box']==b['box'] for b in unique):unique.append(item)
    return unique


def main(request):
    cv2.setNumThreads(2)
    action=request['action']
    if action=='prepare':
        image=read(request['source'])
        if request['orientation']!='rotate180_from_raw':
            raise ValueError('unsupported initialization orientation')
        image=cv2.flip(image,-1)
        normalized=os.path.join(request['output'],'source.jpg')
        digest=write(normalized,image)
        boxes=crops(image)
        for index,item in enumerate(boxes):
            x,y,r,b=item['box']
            item['path']='crop-%02d.jpg'%index
            write(os.path.join(request['output'],item['path']),image[y:b,x:r])
        return dict(width=image.shape[1],height=image.shape[0],path='source.jpg',
                    sha256=digest,crops=boxes,orientation='rotate180_from_raw')
    if action=='crop':
        image=read(request['source']);x,y,r,b=request['bbox']
        if not (0<=x<r<=image.shape[1] and 0<=y<b<=image.shape[0]):
            raise ValueError('invalid crop')
        write(request['output'],image[y:b,x:r])
        return dict(width=r-x,height=b-y)
    if action=='preview':
        frames=request['frames']
        if not 1<=len(frames)<=24:raise ValueError('preview requires 1-24 views')
        images=[]
        for f in frames:
            image=read(f['path'])
            if sum(i.shape[0]*i.shape[1] for i in images)+image.shape[0]*image.shape[1]>100000000:
                raise ValueError('aggregate preview pixel budget exceeded')
            images.append(image)
        poses=[f['pose'] for f in frames]
        features=[prepare(image) for image in images]
        edges=[]
        for i in range(1,len(images)):
            result=align(images[i-1],images[i],features[i-1],features[i])
            result.update(source=i-1,target=i);edges.append(result)
        try:
            focal,sign,estimate=estimate_focal(images,poses,edges)
        except ValueError:
            # Installer-provided, versioned camera profile, never a calibrated claim.
            focal=float(request['fallback_focal_pixels']);sign=request['yaw_image_sign']
            if not .1*images[0].shape[1]<=focal<=10*images[0].shape[1] or sign not in (-1,1):
                raise ValueError('invalid fallback camera projection')
            estimate=dict(method='configured_camera_profile_fallback',calibration_validated=False)
        tilts=sorted(set(p['tilt'] for p in poses))
        width=min(4096,int(2048*2*math.pi*focal/(images[0].shape[0]*len(tilts))))
        width=max(512,width//16*16)
        parts=[];reports=[]
        for tilt in tilts:
            indices=[i for i,p in enumerate(poses) if p['tilt']==tilt]
            part,report=render([images[i] for i in indices],[poses[i] for i in indices],focal,sign,width)
            parts.append(part);reports.append(report)
        canvas=np.concatenate(parts,axis=0)
        report=dict(rows=len(tilts),row_tilts=tilts,row_reports=reports,
                    projection='row_indexed_cylindrical',vertical_stitching=False)
        if canvas.shape[0]>2048:raise ValueError('multirow preview height budget exceeded')
        digest=write(request['output'],canvas,True)
        projection=dict(version=1,projection='angle_assisted_cylindrical',
            width=canvas.shape[1],height=canvas.shape[0],source_width=images[0].shape[1],
            source_height=images[0].shape[0],focal_pixels=focal,yaw_image_sign=sign,
            source_orientation='rotate180_from_raw',calibration_validated=False)
        if len(tilts)>1:projection.update(version=2,row_tilts=tilts,row_height=parts[0].shape[0])
        return dict(sha256=digest,projection=projection,report=report,
                    focal_estimation=estimate,adjacent_edges=edges)
    raise ValueError('unsupported initialization action')


if __name__=='__main__':
    result=main(json.loads(sys.stdin.read(200000)))
    result['opencv']=cv2.__version__
    print(json.dumps(result,allow_nan=False))

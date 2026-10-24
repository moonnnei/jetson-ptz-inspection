"""Bounded JSON image worker, compatible with TX2 Python 2.7/OpenCV 3.3.

Runs as a subprocess of the Python 3 controller. It never contacts a camera,
changes the registry, or issues motion. Paths are supplied by that controller.
"""
from __future__ import division
import json
import sys
import time

import cv2
import numpy as np
from geometry import align, project
from relocalize import checked_image, locate
from fixed_revisit import locate_saved_pose


def read(path):
    with open(path, 'rb') as stream:
        raw = stream.read(12000001)
    if len(raw) > 12000000:
        raise ValueError('image exceeds worker budget')
    image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    checked_image(image)
    return image


def write(path, image):
    ok, raw = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 96])
    if not ok:
        raise ValueError('cannot encode image')
    with open(path, 'wb') as stream:
        stream.write(raw.tostring())


def main(request):
    cv2.setNumThreads(2)
    action = request['action']
    if action == 'normalize':
        image = read(request['raw'])
        if request['orientation'] != 'rotate180_from_raw':
            raise ValueError('uncommissioned image orientation')
        write(request['output'], cv2.flip(image, -1))
        return dict(width=image.shape[1], height=image.shape[0])
    source, target = read(request['source']), read(request['target'])
    if source.shape != target.shape:
        raise ValueError('image dimensions changed')
    if action == 'locate':
        return locate(source, target, request['bbox'])
    if action == 'duplicate_reference':
        from duplicate_reference import locate_commissioned
        return locate_commissioned(source,target,request['bbox'],request['source_pose'],request['target_pose'])
    if action == 'locate_saved_pose':
        return locate_saved_pose(source, target, request['bbox'],
                                 request['source_pose'], request['target_pose'])
    if action == 'annotation_transfer':
        from annotation_vision import transfer
        return transfer(source,target,request['bbox'])
    if action == 'quality':
        from annotation_vision import quality
        return quality(target,request['bbox'])
    if action == 'capture_quality':
        from annotation_vision import quality
        return dict(reference=quality(source,request['reference_bbox']),current=quality(target,request['bbox']))
    if action == 'crop_region':
        import math
        x,y,r,b=request['bbox'];dx=(r-x)*.25;dy=(b-y)*.25
        box=[max(0,int(math.floor(x-dx))),max(0,int(math.floor(y-dy))),
             min(target.shape[1],int(math.ceil(r+dx))),min(target.shape[0],int(math.ceil(b+dy)))]
        if not 16<=min(box[2]-box[0],box[3]-box[1])<=2048:raise ValueError('ROI crop outside detector budget')
        write(request['output'],target[box[1]:box[3],box[0]:box[2]])
        return dict(crop=box)
    if action == 'measure':
        registration = align(source, target)
        if not registration['accepted']:
            return dict(accepted=False, registration=registration)
        h, w = source.shape[:2]
        point = project([[w / 2., h / 2.]], registration['matrix'])[0]
        return dict(accepted=True, displacement=[float(point[0]/w-.5),
                    float(point[1]/h-.5)], registration=registration)
    if action == 'evidence':
        box = request['bbox']
        x1, y1, x2, y2 = [int(round(v)) for v in box]
        if not (0 <= x1 < x2 <= target.shape[1] and 0 <= y1 < y2 <= target.shape[0]):
            raise ValueError('invalid evidence region')
        crop = target[y1:y2, x1:x2]
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        write(request['crop'], crop)
        overlay = target.copy()
        cv2.rectangle(overlay, (x1,y1), (x2,y2), (0,220,80), 3)
        cv2.line(overlay, (target.shape[1]//2-30,target.shape[0]//2),
                 (target.shape[1]//2+30,target.shape[0]//2), (0,180,255), 2)
        cv2.line(overlay, (target.shape[1]//2,target.shape[0]//2-30),
                 (target.shape[1]//2,target.shape[0]//2+30), (0,180,255), 2)
        cv2.putText(overlay, request['instrument_id'], (x1,max(30,y1-10)),
                    cv2.FONT_HERSHEY_SIMPLEX, 1, (0,220,80), 2)
        write(request['overlay'], overlay)
        return dict(sharpness=sharpness, sharpness_status='recorded_threshold_not_validated',
                    crop_size=[crop.shape[1],crop.shape[0]])
    raise ValueError('unsupported image action')


if __name__ == '__main__':
    started = time.time()
    result = main(json.loads(sys.stdin.read(200000)))
    result['image_worker_seconds'] = time.time()-started
    result['opencv'] = cv2.__version__
    print(json.dumps(result, allow_nan=False))

"""Bounded, image-only shape proposals. No labels, camera API, or model weights.

Designed as a proposal stage, not as a semantic meter detector. All coordinates
are exclusive-right/bottom pixel boxes in the original input image.
"""
from __future__ import division
import math

import cv2
import numpy as np


CONFIG = dict(max_work_side=1536, min_side=16, min_area_fraction=0.0002,
              max_area_fraction=0.25, max_aspect=2.2, padding_fraction=0.20,
              proposal_nms_iou=0.65, max_candidates=12,
              candidate_dedup_iou=0.82, crop_edge_fraction=0.015,
              global_nms_iou=0.45)


def area(box):
    return max(0., box[2]-box[0]) * max(0., box[3]-box[1])


def intersection(a, b):
    return max(0., min(a[2], b[2])-max(a[0], b[0])) * max(0., min(a[3], b[3])-max(a[1], b[1]))


def iou(a, b):
    overlap = intersection(a, b)
    return overlap / max(1e-9, area(a)+area(b)-overlap)


def padded_box(box, width, height, padding=0.2):
    x1, y1, x2, y2 = box
    dx, dy = (x2-x1)*padding, (y2-y1)*padding
    return [max(0, int(math.floor(x1-dx))), max(0, int(math.floor(y1-dy))),
            min(width, int(math.ceil(x2+dx))), min(height, int(math.ceil(y2+dy)))]


def _contrast(gray, box):
    x, y, x2, y2 = [int(v) for v in box]
    patch = gray[y:y2, x:x2]
    if not patch.size or min(patch.shape) < 4:
        return 0.
    h, w = patch.shape
    inner = patch[max(1,h//5):max(2,h-h//5), max(1,w//5):max(2,w-w//5)]
    ring = np.concatenate((patch[0,:], patch[-1,:], patch[:,0], patch[:,-1]))
    return float(min(1., abs(float(np.median(inner))-float(np.median(ring)))/64.))


def propose(image, limit=12):
    if not isinstance(image, np.ndarray) or image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
        raise ValueError('Expected uint8 BGR image')
    if min(image.shape[:2]) < 16 or max(image.shape[:2]) > 8192 or not 1 <= limit <= CONFIG['max_candidates']:
        raise ValueError('Unsupported input dimensions or proposal budget')
    height, width = image.shape[:2]
    scale = min(1., CONFIG['max_work_side']/max(height,width))
    work = cv2.resize(image, (int(round(width*scale)), int(round(height*scale))), interpolation=cv2.INTER_AREA)
    wh, ww = work.shape[:2]
    sx, sy = width/ww, height/wh
    gray = cv2.cvtColor(work, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (3,3), 0)
    enhanced = cv2.createCLAHE(clipLimit=2., tileGridSize=(8,8)).apply(blur)
    kernel = np.ones((3,3), dtype=np.uint8)
    otsu = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY+cv2.THRESH_OTSU)[1]
    masks = [
        ('canny-low', cv2.morphologyEx(cv2.Canny(enhanced,40,120), cv2.MORPH_CLOSE, kernel)),
        ('canny-high', cv2.morphologyEx(cv2.Canny(enhanced,80,200), cv2.MORPH_CLOSE, kernel)),
        ('otsu', otsu),
        ('adaptive', cv2.adaptiveThreshold(blur,255,cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                         cv2.THRESH_BINARY,41,7))]
    raw = []
    contour_counts = {}
    for source, mask in masks:
        # OpenCV 3 and 4 have different findContours tuple lengths.
        contours = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)[-2]
        contour_counts[source] = len(contours)
        for contour in contours:
            a = float(cv2.contourArea(contour))
            if not CONFIG['min_area_fraction']*ww*wh <= a <= CONFIG['max_area_fraction']*ww*wh:
                continue
            x,y,w,h = cv2.boundingRect(contour)
            if min(w,h) < CONFIG['min_side'] or max(w/h,h/w) > CONFIG['max_aspect']:
                continue
            (_, _), (rw,rh), _ = cv2.minAreaRect(contour)
            if min(rw,rh) < CONFIG['min_side'] or max(rw,rh)/max(1e-9,min(rw,rh)) > CONFIG['max_aspect']:
                continue
            fill = min(1., a/max(1.,rw*rh))
            perimeter = float(cv2.arcLength(contour,True))
            poly = cv2.approxPolyDP(contour,0.025*perimeter,True)
            contrast = _contrast(gray,[x,y,x+w,y+h])
            kind = None
            if 4 <= len(poly) <= 8 and cv2.isContourConvex(poly) and fill >= .70:
                kind = 'quadrilateral' if len(poly)==4 else 'rounded-shape'
                score = .55*fill+.25*(1. if len(poly)==4 else .75)+.20*contrast
            elif len(contour) >= 12:
                (_, _), (ew,eh), _ = cv2.fitEllipse(contour)
                ellipse_fill = a/max(1.,math.pi*ew*eh/4.)
                if min(ew,eh)>0 and max(ew,eh)/min(ew,eh)<=CONFIG['max_aspect'] and .80<=ellipse_fill<=1.20:
                    kind='ellipse'
                    circularity=min(1.,4*math.pi*a/max(1.,perimeter*perimeter))
                    score=.5*(1.-abs(ellipse_fill-1.))+.3*circularity+.2*contrast
            if kind is None:
                continue
            box=[max(0,int(math.floor(x*sx))),max(0,int(math.floor(y*sy))),
                 min(width,int(math.ceil((x+w)*sx))),min(height,int(math.ceil((y+h)*sy)))]
            raw.append(dict(box=box, score=float(score), kind=kind, sources=[source],
                            fill=fill, contrast=contrast))
    # Deterministic geometry ranking, not target positions or known labels.
    raw.sort(key=lambda r:(-r['score'],r['box']))
    clusters=[]
    for row in raw:
        existing=next((r for r in clusters if iou(r['box'],row['box'])>=CONFIG['candidate_dedup_iou']),None)
        if existing is None:
            clusters.append(dict(row))
        else:
            existing['sources']=sorted(set(existing['sources']+row['sources']))
    for row in clusters:
        row['rank_score']=row['score']+.02*(len(row['sources'])-1)
    clusters.sort(key=lambda r:(-r['rank_score'],r['box']))
    selected=[]
    for row in clusters:
        if any(iou(row['box'],p['box'])>=CONFIG['proposal_nms_iou'] for p in selected):
            continue
        record=dict(row)
        record['id']='shape-%02d'%(len(selected)+1)
        record['rank']=len(selected)+1
        record['crop']=padded_box(row['box'],width,height,CONFIG['padding_fraction'])
        selected.append(record)
        if len(selected)==limit:
            break
    return dict(proposals=selected, contour_counts=contour_counts,
                shape_candidates_before_dedup=len(raw), unique_shape_candidates=len(clusters),
                work_size=[ww,wh], coordinate_scale=[sx,sy])


def remap_detection(detection, crop, image_width, image_height, source):
    x1,y1,x2,y2=crop
    w,h=x2-x1,y2-y1
    local=np.asarray(detection['bbox_pixels'], dtype=float)
    global_box=(local+np.array([x1,y1,x1,y1],dtype=float)).tolist()
    global_box[0]=max(0.,min(float(image_width),global_box[0]))
    global_box[2]=max(0.,min(float(image_width),global_box[2]))
    global_box[1]=max(0.,min(float(image_height),global_box[1]))
    global_box[3]=max(0.,min(float(image_height),global_box[3]))
    # Suppress only artificial crop seams; an actual outer image boundary is not a seam.
    e=CONFIG['crop_edge_fraction']
    touches=((x1>0 and local[0]<=w*e) or (y1>0 and local[1]<=h*e) or
             (x2<image_width and local[2]>=w*(1-e)) or (y2<image_height and local[3]>=h*(1-e)))
    return dict(class_id=0, class_name='meter', confidence=detection['confidence'],
                bbox_pixels=global_box, bbox=(np.array(global_box)/[image_width,image_height,image_width,image_height]).tolist(),
                source=source, local_bbox_pixels=local.tolist(), touches_internal_crop_edge=bool(touches))


def merge(detections, threshold=.45):
    chosen=[]
    for row in sorted(detections,key=lambda r:-r['confidence']):
        if not any(iou(row['bbox_pixels'],other['bbox_pixels'])>=threshold for other in chosen):
            chosen.append(row)
    return chosen

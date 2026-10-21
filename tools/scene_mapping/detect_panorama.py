#!/usr/bin/env python3
"""Panorama-first proposals, then estimated source lookup and optional recheck.

Originals remain authoritative. No result is a human confirmation or a visit
pose. Cyclic tiles cover the panorama seam without commanding motor wrapping.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time

import cv2
import numpy as np

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'src/inspection_console'))
from references import read_reference_image
from ring_coordinates import parameters,ring_box,source_choices,segments
from shape_candidates import iou,padded_box


def tiles(width,height):
    result=[]
    for size in (640,960):
        tw,th=min(size,width),min(size,height)
        stride=max(1,int(tw*.65))
        ys=list(range(0,max(1,height-th+1),max(1,int(th*.65))))
        if ys[-1]!=height-th:ys.append(height-th)
        for y in ys:
            for x in range(0,width,stride):result.append([x,y,x+tw,y+th])
    if len(result)>40:raise ValueError('panorama exceeds tile-call budget')
    return result


def cyclic_iou(a,b,width):
    return max(iou(a,[b[0]+shift,b[1],b[2]+shift,b[3]]) for shift in (-width,0,width))


def panorama_candidates(image,infer,max_candidates=40):
    height,width=image.shape[:2]
    found=[];calls=[]
    regions=[[0,0,width,height]]+tiles(width,height)
    for index,region in enumerate(regions):
        x,y,right,bottom=region
        columns=np.arange(x,right)%width
        patch=image[y:bottom,columns].copy()
        started=time.monotonic();detections=infer(patch)
        calls.append(dict(kind='panorama_full' if index==0 else 'panorama_tile',region=region,
                          seconds=time.monotonic()-started))
        for detection in detections:
            local=detection['bbox_pixels'];score=detection['confidence']
            if len(local)!=4 or any(type(v) not in (int,float) or not math.isfinite(v) for v in local):continue
            a,b,c,d=local
            if not (0<=a<c<=patch.shape[1] and 0<=b<d<=patch.shape[0]) or c-a<4 or d-b<4:continue
            if c-a>width/2:continue
            left=(x+a)%width
            box=[left,y+b,left+c-a,y+d]
            edge=(a<2 or c>patch.shape[1]-2) and index!=0
            found.append(dict(bbox=box,confidence=float(score),tile=region,internal_tile_edge=edge))
    selected=[]
    for item in sorted(found,key=lambda d:(d['internal_tile_edge'],-d['confidence'],d['bbox'])):
        if not any(cyclic_iou(item['bbox'],other['bbox'],width)>=.45 for other in selected):
            selected.append(item)
            if len(selected)>=max_candidates:break
    return selected,calls


def patch_correlation(reference,sample):
    a=cv2.cvtColor(reference,cv2.COLOR_BGR2GRAY).astype(np.float64).ravel()
    b=cv2.cvtColor(sample,cv2.COLOR_BGR2GRAY).astype(np.float64).ravel()
    if min(a.std(),b.std())<5:return None
    a-=a.mean();b-=b.mean()
    return float(np.clip(a.dot(b)/(np.linalg.norm(a)*np.linalg.norm(b)),-1,1))


def source_appearance(panorama,box,image,frame,projection):
    """Compare the same projected region before trusting a detector recheck."""
    p=parameters(projection);x1,y1,x2,y2=ring_box(box,p)
    u,v=np.meshgrid(np.linspace(x1,x2,96,endpoint=False)+(x2-x1)/192.,
                   np.linspace(y1,y2,96,endpoint=False)+(y2-y1)/192.)
    reference=cv2.remap(panorama,(u%p['width']).astype(np.float32),v.astype(np.float32),cv2.INTER_LINEAR)
    delta=(math.radians(frame['pose']['pan'])-u/p['width']*2*math.pi+math.pi)%(2*math.pi)-math.pi
    dx=p['yaw_image_sign']*p['focal_pixels']*np.tan(delta)
    sx=p['source_width']/2.+dx
    sy=p['source_height']/2.+(v-p['height']/2.)*np.sqrt(p['focal_pixels']**2+dx**2)/(p['width']/(2*math.pi))
    if sx.min()<0 or sx.max()>image.shape[1]-1 or sy.min()<0 or sy.max()>image.shape[0]-1:return None
    sample=cv2.remap(image,sx.astype(np.float32),sy.astype(np.float32),cv2.INTER_LINEAR)
    return patch_correlation(reference,sample)


def source_observations(candidates,frames,projection,load,infer,overview_sha256,model_sha256,calls,panorama=None):
    observations=[];unmapped=[]
    for candidate in candidates:
        choices=source_choices(candidate['bbox'],frames,projection)
        if not choices:
            unmapped.append(dict(candidate,reason='no_complete_source_roi'));continue
        ranked=[]
        for choice in choices[:2]:
            frame=next(f for f in frames if f['id']==choice['frame_id'])
            image=load(frame)
            similarity=source_appearance(panorama,candidate['bbox'],image,frame,projection) if panorama is not None else None
            if similarity is not None and similarity>=.65:ranked.append((similarity,choice,frame,image))
        best=dict(choices[0],source_confidence=None,source_similarity=None,source_association='geometric_only')
        if ranked:
            similarity,choice,frame,image=max(ranked,key=lambda item:item[0])
            best=dict(choice,source_confidence=None,source_similarity=similarity,source_association='appearance_consistent')
        if ranked and len(calls)<120:
            crop=padded_box(best['bbox'],frame['width'],frame['height'],.25)
            x,y,right,bottom=crop
            started=time.monotonic();found=infer(image[y:bottom,x:right])
            calls.append(dict(kind='source_roi_recheck',source_sha256=frame['sha256'],region=crop,
                              seconds=time.monotonic()-started))
            for item in found:
                box=[float(v)+offset for v,offset in zip(item['bbox_pixels'],(x,y,x,y))]
                if not (0<=box[0]<box[2]<=frame['width'] and 0<=box[1]<box[3]<=frame['height']):continue
                if iou(box,choice['bbox'])<.35:continue
                score=float(item['confidence'])
                if best['source_confidence'] is None or score>best['source_confidence']:
                    best.update(bbox=box,source_confidence=score)
        observation=dict(frame_sha256=best['sha256'],bbox=best['bbox'],panorama_bbox=candidate['bbox'],
            panorama_confidence=candidate['confidence'],source_confidence=best['source_confidence'],
            source_recheck='matched_detector' if best['source_confidence'] is not None else 'unverified',
            source_association=best['source_association'],source_similarity=best['source_similarity'],
            confidence=best['source_confidence'] if best['source_confidence'] is not None else candidate['confidence'])
        stable=json.dumps(dict(overview=overview_sha256,model=model_sha256,observation=observation),sort_keys=True,separators=(',',':'))
        observation['observation_id']=hashlib.sha256(stable.encode('utf-8')).hexdigest()[:24]
        observations.append(observation)
    return observations,unmapped


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registry',required=True);parser.add_argument('--map-id',required=True)
    parser.add_argument('--images',required=True);parser.add_argument('--projection',required=True)
    parser.add_argument('--weights',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args()
    state=json.loads(Path(args.registry).read_text(encoding='utf-8'))
    scene=next(m for m in state['maps'] if m['id']==args.map_id)
    if scene['status']=='invalidated' or scene['overview']['kind']!='pose_projected_ring_preview':
        raise ValueError('requires an active angle-projected ring map')
    frames=[f for f in state['frames'] if f['map_id']==scene['id']]
    report=json.loads(Path(args.projection).read_text(encoding='utf-8'))
    descriptor=parameters(dict(version=1,projection='angle_assisted_cylindrical',
        width=scene['overview']['width'],height=scene['overview']['height'],
        source_width=frames[0]['width'],source_height=frames[0]['height'],
        focal_pixels=report['focal_estimation']['focal_pixels'],
        yaw_image_sign=report['focal_estimation']['yaw_image_sign'],
        source_orientation=frames[0]['orientation'],calibration_validated=False))
    images=Path(args.images);cache={}
    def load(frame):
        if frame['sha256'] not in cache:
            raw=read_reference_image(frame,images)
            image=cv2.imdecode(np.frombuffer(raw,np.uint8),cv2.IMREAD_COLOR)
            if image is None:raise ValueError('source cannot be decoded')
            cache[frame['sha256']]=image
        return cache[frame['sha256']]
    panorama=load(scene['overview'])
    output=Path(args.output);output.mkdir(parents=True,exist_ok=False)
    config=output/'ultralytics-config';config.mkdir()
    os.environ['YOLO_CONFIG_DIR']=str(config);os.environ['YOLO_OFFLINE']='true'
    weights=Path(args.weights).resolve()
    if not weights.is_file() or weights.suffix!='.pt':raise ValueError('trusted local weights required')
    from ultralytics import YOLO
    model=YOLO(str(weights),task='detect')
    if len(model.names)!=1 or str(model.names[0]).lower() not in ('meter','gauge','dial'):
        raise ValueError('expected a single-class meter detector')
    model_sha=hashlib.sha256(weights.read_bytes()).hexdigest()
    def infer(image):
        prediction=model.predict(image,imgsz=640,conf=.25,iou=.45,max_det=50,device='cpu',verbose=False)[0]
        return [dict(bbox_pixels=b.xyxy[0].cpu().numpy().astype(float).tolist(),confidence=float(b.conf[0])) for b in prediction.boxes]
    candidates,calls=panorama_candidates(panorama,infer)
    observations,unmapped=source_observations(candidates,frames,descriptor,load,infer,
        scene['overview']['sha256'],model_sha,calls,panorama=panorama)
    bindings=[{k:f[k] for k in ('sha256','width','height','orientation','pose')} for f in frames]
    payload=dict(map_id=scene['id'],registry_revision=state['revision'],overview_sha256=scene['overview']['sha256'],
        projection=descriptor,source_binding=bindings,observations=observations,model_sha256=model_sha)
    payload['operation_id']=hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode('utf-8')).hexdigest()[:24]
    summary=dict(panorama_candidates=len(candidates),mapped_observations=len(observations),unmapped=unmapped,
        source_recheck_matches=sum(o['source_recheck']=='matched_detector' for o in observations),
        model_calls=len(calls),calls=calls,projection=descriptor,model_sha256=model_sha,
        physical_motion=False,human_review_required=True,pose_calibrated=False)
    for name,value in (('payload.json',payload),('discovery.json',summary)):
        (output/name).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    debug=panorama.copy()
    for index,item in enumerate(candidates):
        for box in segments(item['bbox'],descriptor):
            a,b,c,d=[int(round(v)) for v in box]
            cv2.rectangle(debug,(a,b),(c,d),(30,210,250),2)
            cv2.putText(debug,'P%02d'%(index+1),(a,max(15,b-4)),cv2.FONT_HERSHEY_SIMPLEX,.5,(30,210,250),1)
    ok,encoded=cv2.imencode('.png',debug)
    if ok:(output/'panorama-proposals.png').write_bytes(encoded.tobytes())
    print(json.dumps({k:summary[k] for k in ('panorama_candidates','mapped_observations','source_recheck_matches','model_calls','physical_motion')}))


if __name__=='__main__':main()

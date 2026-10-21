#!/usr/bin/env python3
"""Offline candidate discovery on original views, never on a shrunk panorama.

Use a locally trained single-class meter model. Runs on the PC, not the TX2.
Proposals and YOLO scores are not human confirmations or meter readings.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import time

import numpy as np
from build_map import read_image
from shape_candidates import propose, padded_box, remap_detection, merge


def discover(image, infer, proposal_limit=12, retry_limit=4):
    if type(proposal_limit) is not int or not 1<=proposal_limit<=12 or type(retry_limit) is not int or not 0<=retry_limit<=4:
        raise ValueError('invalid proposal or retry budget')
    height,width=image.shape[:2]
    proposals=propose(image,proposal_limit)['proposals']
    kept,edge,calls=[],[],[]
    def run(crop,source):
        x,y,r,b=crop
        started=time.monotonic()
        found=infer(image[y:b,x:r])
        calls.append(dict(source=source,crop=crop,seconds=time.monotonic()-started))
        return [remap_detection(d,crop,width,height,source) for d in found]
    kept.extend(run([0,0,width,height],'full_view'))
    for proposal in proposals:
        for d in run(proposal['crop'],'shape_'+proposal['id']):
            if d['touches_internal_crop_edge']:edge.append(d)
            else:kept.append(d)
    # One expansion per high-scoring seam candidate, bounded and deduplicated.
    expanded=set()
    for d in merge(edge):
        if len(expanded)>=retry_limit:break
        crop=padded_box(d['bbox_pixels'],width,height,.65)
        if tuple(crop) in expanded:continue
        expanded.add(tuple(crop))
        kept.extend(x for x in run(crop,'edge_recheck') if not x['touches_internal_crop_edge'])
    detections=merge(kept)
    return [dict(bbox=d['bbox_pixels'],confidence=d['confidence'],source='shape_yolo/'+d['source'])
            for d in detections],dict(calls=calls,model_calls=len(calls),proposals=len(proposals),
                                    edge_rechecks=len(expanded),candidate_count=len(detections))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest')
    parser.add_argument('--weights',required=True,help='Trusted locally trained .pt file; never auto-download')
    parser.add_argument('--output',required=True,help='New private output directory')
    parser.add_argument('--proposal-limit',type=int,default=12,choices=range(1,13))
    args=parser.parse_args()
    weights=Path(args.weights).resolve()
    if not weights.is_file() or weights.suffix!='.pt':
        parser.error('a trusted local .pt checkpoint is required')
    source=Path(args.manifest).resolve()
    manifest=json.loads(source.read_text(encoding='utf-8'))
    if not 1<=len(manifest['frames'])<=24:raise ValueError('supports 1-24 views')
    output=Path(args.output).resolve()
    output.mkdir(parents=True,exist_ok=False)
    config=output/'ultralytics-config'
    config.mkdir()
    os.environ['YOLO_CONFIG_DIR']=str(config)
    os.environ['YOLO_OFFLINE']='true'
    from ultralytics import YOLO
    import ultralytics
    model=YOLO(str(weights),task='detect')
    if len(model.names)!=1 or str(model.names[0]).lower() not in ('meter','gauge','dial'):
        raise ValueError('expected a single-class meter detector')
    def infer(image):
        result=model.predict(image,imgsz=640,conf=.25,iou=.45,max_det=50,device='cpu',verbose=False)[0]
        return [dict(bbox_pixels=b.xyxy[0].cpu().numpy().astype(float).tolist(),confidence=float(b.conf[0]))
                for b in result.boxes]
    def copy_path(value):
        path=Path(value);path=path if path.is_absolute() else source.parent/path
        data=path.read_bytes();name=hashlib.sha256(data).hexdigest()+path.suffix.lower()
        target=output/name
        if not target.exists():shutil.copyfile(str(path),str(target))
        return name
    report=dict(model_sha256=hashlib.sha256(weights.read_bytes()).hexdigest(),
                ultralytics_version=ultralytics.__version__,backend='PC_PyTorch_CPU',input_size=640,
                confidence_threshold=.25,proposal_limit=args.proposal_limit,retry_limit=4,
                detection_coordinates='source_image_pixels',human_review_required=True,frames=[])
    count=0
    for frame in manifest['frames']:
        frame['path']=copy_path(frame['path'])
        if frame.get('raw_source_path'):frame['raw_source_path']=copy_path(frame['raw_source_path'])
        raw,image=read_image(output/frame['path'])
        if frame.get('sha256') and hashlib.sha256(raw).hexdigest()!=frame['sha256']:
            raise ValueError('source hash mismatch')
        detections,details=discover(image,infer,args.proposal_limit)
        frame['detections']=detections;count+=len(detections)
        details['source_sha256']=hashlib.sha256(raw).hexdigest()
        report['frames'].append(details)
        (output/'discovery.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    if count>500:raise ValueError('more than 500 candidates: split the map before import')
    if manifest.get('overview'):manifest['overview']['path']=copy_path(manifest['overview']['path'])
    (output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(frames=len(manifest['frames']),candidates=count,
                         model_calls=sum(f['model_calls'] for f in report['frames']),human_review_required=True)))


if __name__=='__main__':main()

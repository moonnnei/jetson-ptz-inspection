#!/usr/bin/env python3
"""PC spherical panorama preview plus authoritative source-view index.

The OpenCV Stitcher estimates image rotations. Preview images do not carry
linear source homographies; candidates stay in original source coordinates.
All originals survive failed or partial stitching. No PTZ angles are inferred.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import time

import cv2
import numpy as np

from build_map import read_image,write_image
from geometry import align
from ring_projection import projected_preview


def contact_sheet(images,poses):
    columns=4;rows=(len(images)+columns-1)//columns
    canvas=np.full((rows*170,columns*300,3),235,np.uint8)
    for index,(image,pose) in enumerate(zip(images,poses)):
        h,w=image.shape[:2];scale=min(294./w,132./h)
        small=cv2.resize(image,None,fx=scale,fy=scale)
        y=(index//columns)*170+3;x=(index%columns)*300+3
        canvas[y:y+small.shape[0],x:x+small.shape[1]]=small
        cv2.putText(canvas,'%02d  pan %.1f deg'%(index+1,pose['pan']),(x,y+153),
                    cv2.FONT_HERSHEY_SIMPLEX,.55,(25,35,45),1,cv2.LINE_AA)
    return canvas


def stitch_preview(images):
    # Downscale before OpenCV's camera estimation/composition to cap memory.
    work=[]
    for image in images:
        scale=min(1.,1280./max(image.shape[:2]))
        work.append(cv2.resize(image,None,fx=scale,fy=scale))
    stitcher=cv2.Stitcher_create(cv2.Stitcher_PANORAMA)
    stitcher.setRegistrationResol(.4)
    stitcher.setSeamEstimationResol(.1)
    stitcher.setCompositingResol(.8)
    cv2.setRNGSeed(17)
    started=time.monotonic()
    try:
        status,panorama=stitcher.stitch(work)
    except cv2.error:
        return None,dict(status_code=None,stitched_indices=[],omitted_indices=list(range(len(images))),
            accepted=False,reason='opencv_stitching_error',seconds=time.monotonic()-started,
            algorithm='OpenCV_Stitcher_PANORAMA',projection='spherical',source_pixel_mapping=False)
    used=[int(v) for v in stitcher.component()]
    report=dict(status_code=int(status),stitched_indices=used,
                omitted_indices=[i for i in range(len(images)) if i not in used],
                seconds=time.monotonic()-started,algorithm='OpenCV_Stitcher_PANORAMA',
                projection='spherical',source_pixel_mapping=False)
    if status!=cv2.Stitcher_OK or panorama is None:
        report['accepted']=False
        return None,report
    height,width=panorama.shape[:2]
    if min(height,width)<1 or max(height,width)>24000 or height*width>40000000:
        report.update(accepted=False,reason='panorama_size_budget_exceeded')
        return None,report
    scale=min(1.,8192./max(height,width),(12000000./(height*width))**.5)
    panorama=cv2.resize(panorama,None,fx=scale,fy=scale)
    report.update(accepted=True,preview_size=[panorama.shape[1],panorama.shape[0]])
    return panorama,report


def process(source,output,rotate180=False,stitch=True):
    source=Path(source).resolve();output=Path(output)
    manifest=json.loads(source.read_text(encoding='utf-8'))
    original=manifest['frames']
    if not 3<=len(original)<=24:
        raise ValueError('ring preview supports 3-24 original views')
    poses=[f['pose'] for f in original]
    pans=[p['pan'] for p in poses]
    if any(not 0<=p<=350 for p in pans) or max(pans)-min(pans)<300:
        raise ValueError('ring input must cover at least a 300-degree encoder span')
    if any(abs(p['tilt']-poses[0]['tilt'])>.3 or abs(p['zoom']-poses[0]['zoom'])>.05 for p in poses):
        raise ValueError('ring source row must keep tilt and zoom fixed')
    if not (all(b>a for a,b in zip(pans,pans[1:])) or all(b<a for a,b in zip(pans,pans[1:]))):
        raise ValueError('source poses must be monotonic without wrapping')
    output.mkdir(parents=True,exist_ok=False)
    images=[];frames=[]
    for index,entry in enumerate(original):
        path=Path(entry['path']);path=path if path.is_absolute() else source.parent/path
        raw,image=read_image(path)
        sha=hashlib.sha256(raw).hexdigest()
        if entry.get('sha256') and entry['sha256']!=sha:
            raise ValueError('ring source hash mismatch')
        raw_name='source-%02d%s'%(index+1,path.suffix.lower())
        shutil.copyfile(str(path),str(output/raw_name))
        frame=dict(entry,path=raw_name,sha256=sha,to_overview=None,
                   view_key='view-%02d'%(index+1))
        if rotate180:
            height,width=image.shape[:2]
            image=cv2.flip(image,-1)
            name='upright-%02d.png'%(index+1);write_image(output/name,image)
            frame.update(path=name,sha256=hashlib.sha256((output/name).read_bytes()).hexdigest(),
                         orientation='rotate180_from_raw',raw_source_path=raw_name,raw_source_sha256=sha)
            frame['detections']=[dict(d,bbox=[width-d['bbox'][2],height-d['bbox'][3],
                width-d['bbox'][0],height-d['bbox'][1]]) for d in entry.get('detections',[])]
        if sum(i.shape[0]*i.shape[1] for i in images)+image.shape[0]*image.shape[1]>100000000:
            raise ValueError('ring input exceeds aggregate pixel budget')
        images.append(image);frames.append(frame)
    write_image(output/'source-index.png',contact_sheet(images,poses))
    edges=[]
    # Adjacent overlap and the physical endpoints are separate evidence from
    # Stitcher success: neither is by itself a certificate of 360-degree coverage.
    for index in range(1,len(images)):
        result=align(images[index-1],images[index])
        result.update(source=index-1,target=index);edges.append(result)
    closure=align(images[-1],images[0])
    closure.update(source=len(images)-1,target=0,encoder_gap_degrees=360-(max(pans)-min(pans)))
    pano,stitching=stitch_preview(images) if stitch else (None,dict(accepted=False,reason='stitching_not_requested',stitched_indices=[]))
    projection_report=None
    stitched_success=pano is not None
    if pano is None and stitch:
        pano,projection_report=projected_preview(images,poses,edges)
    if pano is not None:
        name='ring-preview.png';write_image(output/name,pano)
        kind='stitched_ring_preview' if stitched_success else 'pose_projected_ring_preview'
    else:name='source-index.png';kind='source_contact_sheet'
    report=dict(schema_version=1,kind=kind,source_views=len(frames),
        encoder_span_degrees=max(pans)-min(pans),adjacent_edges=edges,end_overlap=closure,
        all_overlap_checks_passed=all(e['accepted'] for e in edges) and closure['accepted'],
        stitching=stitching,angle_projection=projection_report,full_panorama=False,ptz_calibration=False,
        source_pixel_mapping=False,visual_acceptance_required=True,opencv=cv2.__version__)
    result=dict(title=manifest['title'],provenance=manifest.get('provenance','historical_import'),
        frames=frames,scan_plan=manifest.get('scan_plan'),overview=dict(
            path=name,sha256=hashlib.sha256((output/name).read_bytes()).hexdigest(),
            kind=kind,registered_views=0,total_views=len(frames),
            stitched_views=len(stitching['stitched_indices']) if stitched_success else 0,
            source_mapping='source_picker_only',full_panorama=False,ptz_calibration=False))
    if kind=='pose_projected_ring_preview':result['overview']['projected_views']=len(images)
    (output/'manifest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    (output/'ring-registration.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return result,report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest');parser.add_argument('--output',required=True)
    parser.add_argument('--rotate180',action='store_true')
    parser.add_argument('--index-only',action='store_true')
    args=parser.parse_args()
    result,report=process(args.manifest,args.output,args.rotate180,not args.index_only)
    print(json.dumps(dict(kind=report['kind'],views=report['source_views'],
        adjacent_passed=sum(e['accepted'] for e in report['adjacent_edges']),
        end_overlap=report['end_overlap']['accepted'],stitching=report['stitching'],full_panorama=False)))


if __name__=='__main__':main()

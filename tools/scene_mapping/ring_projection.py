"""Angle-assisted cylindrical preview with estimated, uncalibrated intrinsics.

Angle wrapping here is an image-coordinate operation only. This module has no
camera access and never sends a motor across its mechanical limit.
"""
from __future__ import division
import argparse
import hashlib
import json
import math

import cv2
import numpy as np


def estimate_focal(images, poses, edges):
    height,width=images[0].shape[:2]
    if any(i.shape[:2]!=(height,width) for i in images):
        raise ValueError('angle projection requires equal source dimensions')
    estimates=[]
    for edge in edges:
        if not edge.get('accepted'):continue
        a,b=edge['source'],edge['target']
        delta=(poses[b]['pan']-poses[a]['pan']+180)%360-180
        if not 3<=abs(delta)<=35:continue
        h=np.asarray(edge['matrix'],np.float64)
        p=h.dot([width/2.,height/2.,1.])
        if not np.isfinite(p).all() or abs(p[2])<1e-8:continue
        focal=(p[0]/p[2]-width/2.)/math.tan(math.radians(delta))
        if .3*width<=abs(focal)<=3*width:
            estimates.append(dict(source=a,target=b,focal_signed_pixels=float(focal)))
    if len(estimates)<4:raise ValueError('too few usable image/angle pairs for preview focal estimate')
    raw=np.asarray([e['focal_signed_pixels'] for e in estimates])
    median=float(np.median(raw));sign=1 if median>0 else -1
    keep=(raw*sign>0)&(np.abs(raw-median)<=max(abs(median)*.15,3*float(np.median(np.abs(raw-median)))))
    if int(keep.sum())<4 or float(keep.mean())<.6:
        raise ValueError('inconsistent image/angle focal estimates')
    focal=abs(float(np.median(raw[keep])))
    spread=float(np.median(np.abs(np.abs(raw[keep])-focal))/focal)
    if spread>.15:raise ValueError('preview focal dispersion is excessive')
    return focal,sign,dict(method='median_homography_center_shift_over_tan_encoder_delta',
        focal_pixels=focal,yaw_image_sign=sign,usable_pairs=int(keep.sum()),
        relative_median_deviation=spread,horizontal_fov_estimate_degrees=math.degrees(2*math.atan(width/(2*focal))),
        estimates=estimates,calibration_validated=False,
        assumptions='centered_principal_point_fixed_lens_and_approximate_yaw_model')


def render(images,poses,focal,sign=1,width=4096):
    if type(width) is not int or not 512<=width<=8192 or not np.isfinite(focal) or focal<=0 or sign not in (-1,1):
        raise ValueError('invalid cylindrical preview parameters')
    height0,width0=images[0].shape[:2]
    scale=width/(2*math.pi)
    height=min(2048,int(math.ceil(scale*height0/focal)))
    if height*width>12000000:raise ValueError('cylindrical preview exceeds memory budget')
    canvas=np.zeros((height,width,3),np.uint8)
    priority=np.zeros((height,width),np.float32)
    theta=(np.arange(width,dtype=np.float32)+.5)*(2*math.pi/width)
    vertical=(np.arange(height,dtype=np.float32)[:,None]+.5-height/2.)/scale
    for image,pose in zip(images,poses):
        if image.shape[:2]!=(height0,width0):raise ValueError('source dimensions differ')
        delta=(math.radians(pose['pan'])-theta+math.pi)%(2*math.pi)-math.pi
        valid_columns=np.abs(delta)<math.atan(width0/(2*focal))
        dx=(sign*focal*np.tan(delta)).astype(np.float32)
        mapx=np.broadcast_to(dx[None,:]+width0/2.,(height,width)).copy()
        mapy=(height0/2.+vertical*np.sqrt(focal*focal+dx[None,:]*dx[None,:])).astype(np.float32)
        valid=(valid_columns[None,:]&(mapx>=1)&(mapx<width0-2)&(mapy>=1)&(mapy<height0-2))
        weight=np.broadcast_to(np.cos(delta)[None,:]**4,(height,width)).copy().astype(np.float32)
        weight[~valid]=0
        warped=cv2.remap(image,mapx,mapy,cv2.INTER_LINEAR,borderMode=cv2.BORDER_CONSTANT)
        take=weight>priority
        canvas[take]=warped[take];priority[take]=weight[take]
    return canvas,dict(projection='angle_assisted_cylindrical',width=width,height=height,
        preview_views=len(images),centerline_pixel_coverage=float(np.mean(priority[height//2]>0)),
        pixel_coverage=float(np.mean(priority>0)),principal_point_assumed_center=True,
        source_pixel_mapping=False,full_panorama=False,visual_acceptance_required=True,
        note='Estimated projection; parallax, axis tilt, exposure seams and angle errors remain.')


def projected_preview(images,poses,edges):
    try:
        focal,sign,estimate=estimate_focal(images,poses,edges)
        panorama,report=render(images,poses,focal,sign)
        report.update(accepted=True,focal_estimation=estimate)
        return panorama,report
    except ValueError as exc:
        return None,dict(accepted=False,reason=str(exc),full_panorama=False)


def main():
    from pathlib import Path
    from build_map import read_image,write_image
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mapped_manifest');parser.add_argument('--registration',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    source=Path(args.mapped_manifest).resolve()
    manifest=json.loads(source.read_text(encoding='utf-8'))
    registration=json.loads(Path(args.registration).read_text(encoding='utf-8'))
    images=[];poses=[]
    for f in manifest['frames']:
        path=Path(f['path']);path=path if path.is_absolute() else source.parent/path
        raw,image=read_image(path)
        if hashlib.sha256(raw).hexdigest()!=f['sha256']:raise ValueError('source hash mismatch')
        images.append(image);poses.append(f['pose']);f['path']=str(path)
        if f.get('raw_source_path'):
            p=Path(f['raw_source_path']);f['raw_source_path']=str(p if p.is_absolute() else source.parent/p)
    image,report=projected_preview(images,poses,registration['adjacent_edges'])
    if image is None:raise RuntimeError(report['reason'])
    output=Path(args.output);output.mkdir(parents=True,exist_ok=False)
    write_image(output/'ring-preview.png',image)
    sha=hashlib.sha256((output/'ring-preview.png').read_bytes()).hexdigest()
    manifest['overview']=dict(path='ring-preview.png',sha256=sha,kind='pose_projected_ring_preview',
        registered_views=0,total_views=len(images),stitched_views=0,projected_views=len(images),
        source_mapping='source_picker_only',full_panorama=False,ptz_calibration=False)
    (output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    (output/'projection.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report))


if __name__=='__main__':main()

#!/usr/bin/env python3
"""Create a PC-side scan preview and an importable source-view manifest.

No camera connection. Existing detections are retained, never invented. A
180-degree upright copy can be created while retaining the raw original hash.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

import cv2
import numpy as np
from geometry import build_overview


def read_image(path):
    data = Path(path).read_bytes()
    if len(data)>25000000:
        raise ValueError('source exceeds 25 MB')
    image = cv2.imdecode(np.frombuffer(data,np.uint8),cv2.IMREAD_COLOR)
    if image is None or image.shape[0]*image.shape[1]>40000000:
        raise ValueError('invalid or excessive image')
    return data,image


def write_image(path, image, suffix='.png'):
    ok, data=cv2.imencode(suffix,image)
    if not ok:
        raise ValueError('image encoding failed')
    Path(path).write_bytes(data.tobytes())


def process(source, output, rotate180=False):
    source, output=Path(source).resolve(), Path(output)
    manifest=json.loads(source.read_text(encoding='utf-8'))
    if not 1<=len(manifest['frames'])<=24:
        raise ValueError('supports 1-24 original views')
    output.mkdir(parents=True,exist_ok=False)
    images,frames=[],[]
    for index,entry in enumerate(manifest['frames']):
        path=Path(entry['path'])
        path=path if path.is_absolute() else source.parent/path
        data,image=read_image(path)
        digest=hashlib.sha256(data).hexdigest()
        if entry.get('sha256') and entry['sha256']!=digest:
            raise ValueError('source hash mismatch')
        name='source-%02d%s'%(index+1,path.suffix.lower())
        shutil.copyfile(str(path),str(output/name))
        frame=dict(entry)
        frame.update(path=name,sha256=digest,view_key='view-%02d'%(index+1))
        if rotate180:
            height,width=image.shape[:2]
            image=cv2.flip(image,-1)
            upright='upright-%02d.png'%(index+1)
            write_image(output/upright,image)
            frame.update(path=upright,sha256=hashlib.sha256((output/upright).read_bytes()).hexdigest(),
                         orientation='rotate180_from_raw',raw_source_path=name,raw_source_sha256=digest)
            frame['detections']=[dict(d,bbox=[width-d['bbox'][2],height-d['bbox'][3],
                                            width-d['bbox'][0],height-d['bbox'][1]])
                                 for d in entry.get('detections',[])]
        images.append(image);frames.append(frame)
    canvas,report=build_overview(images)
    write_image(output/'overview.png',canvas)
    for frame,registration in zip(frames,report['frames']):
        frame['to_overview']=registration['to_overview']
    result=dict(title=manifest['title'],provenance=manifest.get('provenance','historical_import'),frames=frames,
                overview=dict(path='overview.png',sha256=hashlib.sha256((output/'overview.png').read_bytes()).hexdigest(),
                    registered_views=report['registered_views'],total_views=len(frames),
                    kind=report['kind'],full_panorama=False,ptz_calibration=False))
    (output/'manifest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    (output/'registration.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return result,report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest')
    parser.add_argument('--output',required=True,help='New private directory')
    parser.add_argument('--rotate180',action='store_true',help='Rotate image AND boxes; raw copy retained')
    args=parser.parse_args()
    result,report=process(args.manifest,args.output,args.rotate180)
    print(json.dumps(dict(registered=report['registered_views'],total=report['total_views'],
                         full_panorama=False,disconnected=report['disconnected'],
                         manifest=str(Path(args.output)/'manifest.json'))))


if __name__=='__main__':main()

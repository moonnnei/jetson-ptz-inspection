#!/usr/bin/env python3
"""PC-side YOLO11n meter -> fixed 640 NCNN FP32, preserving a reference tensor."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--checkpoint',type=Path,required=True)
    p.add_argument('--trusted-sha256',required=True)
    p.add_argument('--output',type=Path,required=True,help='New experiment directory')
    args=p.parse_args()
    if args.output.exists():p.error('output must be new; preserve earlier exports')
    args.output.mkdir(parents=True)
    os.environ['YOLO_CONFIG_DIR']=str(args.output/'config')
    os.environ['YOLO_AUTOINSTALL']='false'
    import numpy as np
    import torch
    import ultralytics
    from ultralytics import YOLO
    from meter_checkpoint import load_meter
    torch.set_num_threads(4)
    model=load_meter(args.checkpoint,args.trusted_sha256)
    wrapper=YOLO('yolo11n.yaml',task='detect')
    wrapper.model=copy.deepcopy(model)
    wrapper.model.task='detect'
    wrapper.model.pt_path=str(args.output/'meter640.pt')
    wrapper.model.args=dict(task='detect',imgsz=640,data=None,single_cls=True)
    wrapper.ckpt_path=wrapper.model.pt_path
    wrapper.overrides=dict(task='detect',model=wrapper.ckpt_path)
    folder=Path(wrapper.export(format='ncnn',imgsz=640,batch=1,half=False,device='cpu'))
    sample=np.random.default_rng(20261019).random((1,3,640,640),dtype=np.float32)
    reference=copy.deepcopy(model).fuse(verbose=False).eval()
    head=reference.model[-1];head.export,head.format,head.dynamic=True,'ncnn',False
    with torch.inference_mode():raw=reference(torch.from_numpy(sample))
    if tuple(raw.shape)!=(1,5,8400):raise ValueError('export head layout differs from the native detector')
    sample[0].astype('<f4').tofile(args.output/'reference-input-640.raw')
    np.save(args.output/'reference-output-640.npy',raw.numpy())
    files={f.name:dict(bytes=f.stat().st_size,sha256=hashlib.sha256(f.read_bytes()).hexdigest())
           for f in folder.iterdir() if f.suffix in ('.param','.bin')}
    metadata=dict(checkpoint_sha256=args.trusted_sha256,model='YOLO11n-meter',classes=model.names,
        torch=torch.__version__,ultralytics=ultralytics.__version__,precision='FP32',
        output_shape=list(raw.shape),export_directory=str(folder),files=files)
    (args.output/'export-info.json').write_text(json.dumps(metadata,indent=2),encoding='utf8')
    print(json.dumps(metadata,indent=2))


if __name__=='__main__':main()

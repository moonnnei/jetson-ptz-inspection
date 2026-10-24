"""Reviewed single-class YOLO11n checkpoint loader for the PC export environment."""
from collections import OrderedDict
import hashlib
from pathlib import Path
import re


def load_meter(path,expected_sha256):
    if not re.fullmatch('[a-f0-9]{64}',expected_sha256):raise ValueError('a trusted checkpoint SHA256 is required')
    if hashlib.sha256(Path(path).read_bytes()).hexdigest()!=expected_sha256:
        raise ValueError('checkpoint differs from the trusted training artifact')
    import torch
    from torch import nn
    from ultralytics.nn.tasks import DetectionModel
    from ultralytics.nn.modules.conv import Conv,Concat,DWConv
    from ultralytics.nn.modules.block import C3k2,Bottleneck,C3k,SPPF,C2PSA,PSABlock,Attention,DFL
    from ultralytics.nn.modules.head import Detect
    allowed=[DetectionModel,set,OrderedDict,nn.Sequential,Conv,nn.Conv2d,nn.BatchNorm2d,nn.SiLU,
        C3k2,nn.ModuleList,Bottleneck,C3k,SPPF,nn.MaxPool2d,C2PSA,PSABlock,Attention,nn.Identity,
        nn.Upsample,Concat,Detect,DWConv,DFL,torch.Size]
    with torch.serialization.safe_globals(allowed):
        checkpoint=torch.load(str(path),map_location='cpu',weights_only=True)
    model=checkpoint['model'].float().eval()
    if model.names!={0:'meter'} or type(model.model[-1]) is not Detect or model.model[-1].nc!=1:
        raise ValueError('this runtime requires a single-class Detect model named meter')
    return model

"""Export official YOLO26n to a fixed 320x320, FP32 NCNN model on the PC."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
os.environ['YOLO_CONFIG_DIR'] = str(ROOT / 'ultralytics-config')

from ultralytics import YOLO
import torch
import ultralytics
from export_adapter import use_host_topk

torch.set_num_threads(4)
model = YOLO('yolo26n.pt')
use_host_topk(model)
(ROOT / 'classes.txt').write_text('\n'.join(model.names[i] for i in range(len(model.names))) + '\n', encoding='utf-8')
result = model.export(format='ncnn', imgsz=320, batch=1, half=False, device='cpu')
(ROOT / 'export-info.json').write_text(json.dumps({
    'model': 'yolo26n.pt', 'ultralytics': ultralytics.__version__,
    'torch': torch.__version__, 'imgsz': 320, 'batch': 1,
    'half': False, 'output': str(result),
    'output_layout': 'batch,anchors,84: xyxy + 80 class scores',
    'postprocess': 'host global top300 scores, conf>=0.25, no NMS',
    'reason': 'PNNX could not convert default end-to-end TopK/Gather; one-to-one network and weights unchanged'
}, indent=2), encoding='utf-8')
print('EXPORTED', result)

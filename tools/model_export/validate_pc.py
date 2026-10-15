"""Compare the exported NCNN graph to its PyTorch source on identical pixels."""
import json
import os
import shutil
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
os.environ['YOLO_CONFIG_DIR'] = str(ROOT / 'ultralytics-config')
import cv2
import ncnn
import numpy as np
import torch
from ultralytics import YOLO
from ultralytics.utils import ASSETS
from export_adapter import use_host_topk

torch.set_num_threads(4)
outdir = ROOT / 'validation'
outdir.mkdir(exist_ok=True)
shutil.copyfile(ASSETS / 'bus.jpg', ROOT / 'bus.jpg')
model = YOLO('yolo26n.pt')
results = model.predict('bus.jpg', imgsz=320, rect=False, conf=0.25, device='cpu', verbose=False)
result = results[0]
result.save(filename=str(outdir / 'pytorch-bus.jpg'))
(outdir / 'pytorch-bus.json').write_text(result.to_json(), encoding='utf-8')
use_host_topk(model)

# Match infer.cpp: fixed square letterbox, RGB, float32 / 255.
bgr = cv2.imread('bus.jpg')
h, w = bgr.shape[:2]
r = min(320 / w, 320 / h)
rw, rh = round(w*r), round(h*r)
left, top = (320-rw)//2, (320-rh)//2
resized = cv2.resize(bgr, (rw, rh), interpolation=cv2.INTER_LINEAR)
padded = cv2.copyMakeBorder(resized, top, 320-rh-top, left, 320-rw-left, cv2.BORDER_CONSTANT, value=(114,114,114))
rgb = cv2.cvtColor(padded, cv2.COLOR_BGR2RGB)
array = np.ascontiguousarray(rgb.transpose(2,0,1), dtype=np.float32) / 255.
model.model.eval()
model.model.fuse()
# Configure the head exactly as the NCNN exporter does.
head = model.model.model[-1]
head.export = True
head.format = 'ncnn'
head.dynamic = False
head.max_det = 300
with torch.no_grad():
    reference = model.model(torch.from_numpy(array[None]))
    if isinstance(reference, (list, tuple)):
        reference = reference[0]
    reference = reference.detach().cpu().numpy().squeeze(0)

with ncnn.Net() as net:
    net.opt.use_vulkan_compute = False
    net.opt.num_threads = 4
    net.opt.use_fp16_storage = False
    net.opt.use_fp16_arithmetic = False
    assert net.load_param('yolo26n_ncnn_model/model.ncnn.param') == 0
    assert net.load_model('yolo26n_ncnn_model/model.ncnn.bin') == 0
    with net.create_extractor() as ex:
        ex.input('in0', ncnn.Mat(array).clone())
        status, output = ex.extract('out0')
        assert status == 0
        actual = np.array(output).copy()
print('PyTorch shape', reference.shape, 'NCNN shape', actual.shape)
np.save(outdir / 'pytorch-raw.npy', reference)
np.save(outdir / 'ncnn-raw.npy', actual)
report = {'reference_shape': list(reference.shape), 'ncnn_shape': list(actual.shape)}
if actual.shape == reference.shape:
    report['max_absolute_error'] = float(np.max(np.abs(actual-reference)))
    report['mean_absolute_error'] = float(np.mean(np.abs(actual-reference)))
    assert report['max_absolute_error'] < 0.01, 'NCNN raw output mismatch'
else:
    raise AssertionError('NCNN output shape mismatch')
# Same host-side top300 selection as infer.cpp, checked against official predict.
flat_scores = actual[:, 4:].reshape(-1)
indices = np.argsort(-flat_scores)[:300]
indices = indices[flat_scores[indices] >= 0.25]
detections = []
annotated = bgr.copy()
for index in indices:
    anchor, label = int(index//80), int(index%80)
    box = actual[anchor,:4].copy()
    box[[0,2]] = np.clip((box[[0,2]]-left)/r, 0, w)
    box[[1,3]] = np.clip((box[[1,3]]-top)/r, 0, h)
    score = float(flat_scores[index])
    detections.append({'class_id': label, 'name': model.names[label], 'confidence': score, 'xyxy': box.tolist()})
    cv2.rectangle(annotated, tuple(box[:2].astype(int)), tuple(box[2:].astype(int)), (0,220,0), 2)
    cv2.putText(annotated, '{} {:.2f}'.format(model.names[label],score), (int(box[0]),max(18,int(box[1])-6)), cv2.FONT_HERSHEY_SIMPLEX,0.6,(0,220,0),2)
expected = result.boxes
assert len(detections) == len(expected), 'Detection count mismatch'
box_errors, score_errors = [], []
for i,d in enumerate(detections):
    assert d['class_id'] == int(expected.cls[i]), 'Detection class mismatch'
    box_errors.append(float(np.max(np.abs(np.array(d['xyxy'])-expected.xyxy[i].cpu().numpy()))))
    score_errors.append(abs(d['confidence']-float(expected.conf[i])))
report.update(detection_count=len(detections), classes_match=True,
              max_box_error_pixels=max(box_errors, default=0),
              max_confidence_error=max(score_errors, default=0))
assert report['max_box_error_pixels'] < 0.05, 'Decoded boxes mismatch'
assert report['max_confidence_error'] < 0.0001, 'Confidence mismatch'
(outdir / 'ncnn-bus.json').write_text(json.dumps(detections,indent=2),encoding='utf-8')
cv2.imwrite(str(outdir / 'ncnn-bus.jpg'), annotated)
print(json.dumps(report, indent=2))
(outdir / 'conversion-check.json').write_text(json.dumps(report, indent=2), encoding='utf-8')

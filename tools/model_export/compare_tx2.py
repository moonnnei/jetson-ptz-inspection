"""Check actual TX2 C++ outputs against the unmodified official PyTorch model."""
import json
import os
from pathlib import Path
ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
os.environ['YOLO_CONFIG_DIR'] = str(ROOT / 'ultralytics-config')
import numpy as np
import cv2
import torch
from ultralytics import YOLO
torch.set_num_threads(4)
model = YOLO('yolo26n.pt')
reports = []
for sample, image in [('bus','bus.jpg'),('dog','tx2-results/dog-input.jpg')]:
    expected = model.predict(image, imgsz=320, rect=False, conf=0.25, device='cpu', verbose=False)[0]
    if sample == 'dog':
        # Compare identical pixels to separate runtime errors from old/new JPEG decoders.
        pc_decoded = cv2.imread(image)
        tx2_decoded = cv2.imread('tx2-results/dog-decoded.png')
        pixel_diff = np.abs(pc_decoded.astype(np.int16)-tx2_decoded.astype(np.int16))
        print('JPEG decoder differences: max',pixel_diff.max(),'mean',pixel_diff.mean(),flush=True)
        tx2_input = cv2.imread('tx2-results/dog-input320.png')
        pc_input = cv2.copyMakeBorder(cv2.resize(pc_decoded,(320,240)),40,40,0,0,cv2.BORDER_CONSTANT,value=(114,114,114))
        preprocess_diff = np.abs(pc_input.astype(np.int16)-tx2_input.astype(np.int16))
        print('Preprocess differences: max',preprocess_diff.max(),'mean',preprocess_diff.mean(),flush=True)
        tensor = torch.from_numpy(np.ascontiguousarray(tx2_input[:,:,::-1].transpose(2,0,1),dtype=np.float32)/255.)[None]
        expected = model.predict(tensor, imgsz=320, conf=0.25, device='cpu', verbose=False)[0]
        expected.boxes.data = expected.boxes.data.clone()
        expected.boxes.data[:,[0,2]] *= 768/320
        expected.boxes.data[:,[1,3]] = (expected.boxes.data[:,[1,3]]-40)*768/320
    actual = json.loads((ROOT/'tx2-results'/(sample+'.json')).read_text())['detections']
    assert len(actual) == len(expected.boxes), sample + ': count mismatch'
    box_err, conf_err = 0., 0.
    for i, d in enumerate(actual):
        assert d['class_id'] == int(expected.boxes.cls[i]), sample + ': class mismatch'
        box_err = max(box_err, float(np.max(np.abs(np.array(d['xyxy'])-expected.boxes.xyxy[i].numpy()))))
        conf_err = max(conf_err, abs(d['confidence']-float(expected.boxes.conf[i])))
    print(sample, 'box error', box_err, 'confidence error', conf_err, flush=True)
    (ROOT/'validation'/('pytorch-'+sample+'.json')).write_text(expected.to_json(),encoding='utf-8')
    assert box_err < 0.05 and conf_err < 0.0001, sample + ': numeric mismatch'
    reports.append(dict(sample=sample,passed=True,detection_count=len(actual),
                        max_box_error_pixels=box_err,max_confidence_error=conf_err))
    (ROOT/'validation'/('pytorch-'+sample+'.json')).write_text(expected.to_json(),encoding='utf-8')
print(json.dumps(reports, indent=2))
(ROOT/'tx2-results'/'comparison.json').write_text(json.dumps(reports,indent=2),encoding='utf-8')

#!/usr/bin/env python3
"""PC-only paired raw/bicubic/Real-ESRGAN experiment on already annotated ROIs.

No camera access, downloads or automatic installation. One meter (or none) per
ROI; do not count derived crops as independent field scenes. See README.md.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import statistics
import time

SR_SOURCE_SHA = '7419a75e789ca09295266563d74a6d7c08d6c24502af6b0a919b1811d0a0be59'
SR_WEIGHT_SHA = '8dc7edb9ac80ccdc30c3a5dca6616509367f05fbc184ad95b731f05bece96292'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def iou(a, b):
    hit = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
    area = (a[2]-a[0]) * (a[3]-a[1]) + (b[2]-b[0]) * (b[3]-b[1]) - hit
    return hit / max(1e-9, area)


def detect(model, image, cv2, np, torch, nms):
    h, w = image.shape[:2]
    ratio = min(640. / w, 640. / h)
    rw, rh = round(w * ratio), round(h * ratio)
    left, top = (640-rw)//2, (640-rh)//2
    image = cv2.resize(image, (rw, rh), interpolation=cv2.INTER_LINEAR)
    image = cv2.copyMakeBorder(image, top, 640-rh-top, left, 640-rw-left,
                              cv2.BORDER_CONSTANT, value=(114, 114, 114))
    tensor = np.ascontiguousarray(image[:, :, ::-1].transpose(2, 0, 1)[None], dtype=np.float32) / 255
    with torch.inference_mode():
        raw = model(torch.from_numpy(tensor))
        if tuple(raw.shape) != (1, 5, 8400):
            raise ValueError('requires single-class 640 YOLO11n export-layout output')
        boxes = nms(raw.clone(), conf_thres=.25, iou_thres=.45, nc=1, max_det=100, max_nms=1000)[0].numpy()
    output = []
    for box in boxes:
        b = box[:4].copy()
        b[[0, 2]] = np.clip((b[[0, 2]]-left)/ratio, 0, w)
        b[[1, 3]] = np.clip((b[[1, 3]]-top)/ratio, 0, h)
        if b[2] > b[0] and b[3] > b[1]:
            output.append(dict(bbox=b.tolist(), confidence=float(box[4])))
    return output


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest', type=Path, required=True, help='JSON {cases:[{id,group,image,bbox}]}')
    p.add_argument('--checkpoint', type=Path, required=True)
    p.add_argument('--trusted-sha256', required=True)
    p.add_argument('--sr-source', type=Path, required=True, help='Pinned official srvgg_arch.py, unchanged')
    p.add_argument('--sr-weights', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True, help='New output directory')
    p.add_argument('--repeats', type=int, default=3, choices=range(1, 11))
    args = p.parse_args()
    if args.output.exists():
        p.error('preserve old evidence: output must be new')
    if sha(args.sr_source) != SR_SOURCE_SHA or sha(args.sr_weights) != SR_WEIGHT_SHA:
        p.error('SR artifact differs from pinned upstream version; review a new experiment before changing hashes')
    cases = json.loads(args.manifest.read_text(encoding='utf8'))['cases']
    if not 1 <= len(cases) <= 100 or len({row['id'] for row in cases}) != len(cases):
        p.error('one to 100 cases with distinct IDs required')
    args.output.mkdir(parents=True)
    os.environ['YOLO_CONFIG_DIR'] = str(args.output / 'config')
    os.environ['YOLO_AUTOINSTALL'] = 'false'
    import cv2
    import numpy as np
    import torch
    import ultralytics
    from ultralytics.utils.nms import non_max_suppression
    from meter_checkpoint import load_meter

    torch.set_num_threads(4)
    torch.manual_seed(0)
    model = load_meter(args.checkpoint, args.trusted_sha256).fuse(verbose=False).eval()
    head = model.model[-1]
    head.export, head.format, head.dynamic = True, 'ncnn', False
    upstream = args.sr_source.read_text(encoding='utf8')
    standalone = upstream.replace('from basicsr.utils.registry import ARCH_REGISTRY\n', '').replace('@ARCH_REGISTRY.register()\n', '')
    # Only the pinned, reviewed upstream file is executed. Keep its license with local artifacts.
    local_source = args.output / 'srvgg_arch.standalone.py'
    local_source.write_text(standalone, encoding='utf8')
    spec = importlib.util.spec_from_file_location('paired_sr_arch', local_source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sr = module.SRVGGNetCompact(num_conv=32).eval()
    weights = torch.load(args.sr_weights, map_location='cpu', weights_only=True)
    sr.load_state_dict(weights['params_ema'] if 'params_ema' in weights else weights['params'], strict=True)
    for unused in range(2):
        detect(model, np.zeros((128, 128, 3), np.uint8), cv2, np, torch, non_max_suppression)
    with torch.inference_mode():
        sr(torch.zeros(1, 3, 64, 64))
    report = dict(scope='PC paired ROI experiment; not whole-scene discovery or TX2 latency',
                  checkpoint_sha256=args.trusted_sha256, sr_source_sha256=SR_SOURCE_SHA,
                  sr_weights_sha256=SR_WEIGHT_SHA, torch=torch.__version__, ultralytics=ultralytics.__version__,
                  opencv=cv2.__version__, threads=4, detector_input=640, confidence=.25, nms_iou=.45,
                  match_iou=.5, repeats=args.repeats, cases=[], summary={})
    for row in cases:
        source = (args.manifest.parent / row['image']).resolve()
        image = cv2.imdecode(np.frombuffer(source.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
        if image is None or image.shape[0] * image.shape[1] > 250000:
            raise ValueError('provide valid cropped ROIs no larger than 250000 pixels')
        box = row.get('bbox')
        if box is not None and not (len(box) == 4 and 0 <= box[0] < box[2] <= image.shape[1]
                                  and 0 <= box[1] < box[3] <= image.shape[0]):
            raise ValueError('ROI-local positive bbox is outside the image')
        entry = dict(id=row['id'], group=row['group'], image_sha256=sha(source),
                     roi_size=[image.shape[1], image.shape[0]], bbox=box, variants={})
        for variant in ('raw_same_roi', 'bicubic_4x_same_roi', 'real_esrgan_4x_same_roi'):
            timings = []
            for unused in range(args.repeats):
                before = time.perf_counter()
                if variant == 'raw_same_roi':
                    processed = image
                elif variant == 'bicubic_4x_same_roi':
                    processed = cv2.resize(image, (image.shape[1]*4, image.shape[0]*4), interpolation=cv2.INTER_CUBIC)
                else:
                    tensor = torch.from_numpy(np.ascontiguousarray(image[:, :, ::-1].transpose(2, 0, 1))).float()[None]/255.
                    with torch.inference_mode():
                        output = sr(tensor).clamp(0, 1)[0].numpy()
                    processed = np.ascontiguousarray(np.round(output.transpose(1, 2, 0)[:, :, ::-1]*255).astype(np.uint8))
                enhanced = time.perf_counter()
                predictions = detect(model, processed, cv2, np, torch, non_max_suppression)
                end = time.perf_counter()
                timings.append(dict(enhancement_ms=(enhanced-before)*1000,
                                    detection_ms=(end-enhanced)*1000, total_ms=(end-before)*1000))
            scale = 1 if variant == 'raw_same_roi' else 4
            predictions = [dict(confidence=d['confidence'], bbox=[v/scale for v in d['bbox']]) for d in predictions]
            best = max([iou(d['bbox'], box) for d in predictions] or [0]) if box else 0
            tp = int(box is not None and best >= .5)
            entry['variants'][variant] = dict(tp=tp, fn=int(box is not None)-tp, fp=len(predictions)-tp,
                best_iou=best, predictions=predictions, repetitions=timings,
                median_total_ms=statistics.median(t['total_ms'] for t in timings))
        report['cases'].append(entry)
        print(json.dumps(dict(id=entry['id'], counts={k:[v['tp'], v['fn'], v['fp']]
                                                   for k, v in entry['variants'].items()})), flush=True)
    for group in sorted({c['group'] for c in report['cases']}):
        subset = [c for c in report['cases'] if c['group'] == group]
        report['summary'][group] = {v:dict(tp=sum(c['variants'][v]['tp'] for c in subset),
            fn=sum(c['variants'][v]['fn'] for c in subset), fp=sum(c['variants'][v]['fp'] for c in subset),
            median_total_ms=statistics.median(c['variants'][v]['median_total_ms'] for c in subset))
            for v in subset[0]['variants']}
    (args.output / 'results.json').write_text(json.dumps(report, indent=2), encoding='utf8')
    print('Saved results; integration decisions require review of independent field cases and cost.')


if __name__ == '__main__':
    main()

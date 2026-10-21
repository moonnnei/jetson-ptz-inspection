#!/usr/bin/env python3
"""Offline reference-plan replay against saved views or one read-only probe.

Requires PC OpenCV/NumPy. Does not contact a camera, edit the registry, or infer
PTZ angles. Output directories must be new to preserve previous evidence.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src/inspection_console'))
from references import read_reference_image, reference_plan
from geometry import align
from relocalize import locate, reject_overlaps, MIN_SCORE, MIN_INNER, MIN_GAP


def decode(reference, folder):
    image = cv2.imdecode(np.frombuffer(read_reference_image(reference, folder), np.uint8),
                         cv2.IMREAD_COLOR)
    if image is None or image.shape[:2] != (reference['height'], reference['width']):
        raise ValueError('source cannot be decoded at declared dimensions')
    return image


def save_image(path, image):
    ok, encoded = cv2.imencode('.png', image)
    if not ok:
        raise ValueError('image encoding failed')
    path.write_bytes(encoded.tobytes())


def check(state, map_id, images, output, probe=None, probe_directory=None, negative_controls=False):
    plan = reference_plan(state, map_id, state['revision'])
    if not plan['targets']:
        raise ValueError('no enabled, confirmed meters')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    (output / 'reference-plan.json').write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
    sources = {}
    for item in plan['targets']:
        ref = item['references'][0]
        if ref['sha256'] not in sources:
            sources[ref['sha256']] = decode(ref, images)
    frames = [probe] if probe else [f for f in state['frames'] if f['map_id'] == map_id]
    records = []
    controls = []
    for index, frame in enumerate(frames):
        target = decode(frame, probe_directory if probe else images)
        registrations = {}
        results = []
        for item in plan['targets']:
            ref = item['references'][0]
            if ref['orientation'] != frame['orientation']:
                raise ValueError('source and probe orientation conventions differ')
            if ref['sha256'] == frame['sha256']:
                continue  # The reference image itself is not a successful revisit.
            source = sources[ref['sha256']]
            started = time.perf_counter()
            if ref['sha256'] not in registrations:
                registrations[ref['sha256']] = align(source, target)
            result = locate(source, target, ref['bbox'], registrations[ref['sha256']])
            result.update(instrument_id=item['instrument_id'], source_sha256=ref['sha256'],
                          elapsed_pc_seconds=time.perf_counter()-started)
            results.append(result)
        reject_overlaps(results)
        if not results:
            continue
        overlay = target.copy()
        for item in results:
            if item['bbox']:
                x1, y1, x2, y2 = [int(round(v)) for v in item['bbox']]
                cv2.rectangle(overlay, (x1,y1), (x2,y2), (30,220,60), 3)
                cv2.putText(overlay, item['instrument_id'], (x1,max(25,y1-8)),
                            cv2.FONT_HERSHEY_SIMPLEX, .8, (30,220,60), 2)
        scale = min(1., 1280./overlay.shape[1])
        overlay = cv2.resize(overlay, None, fx=scale, fy=scale)
        name = 'view-%02d-result.png' % (index+1)
        save_image(output/name, overlay)
        records.append(dict(target_sha256=frame['sha256'], frame_id=frame.get('id'),
                            source_kind=frame.get('source_kind'), overlay=name, results=results))
        if negative_controls:
            for result in results:
                if result['status'] != 'matched_reference':
                    continue
                item = next(x for x in plan['targets'] if x['instrument_id'] == result['instrument_id'])
                ref = item['references'][0]
                source = sources[ref['sha256']]
                erased = target.copy()
                x1,y1,x2,y2 = [int(round(v)) for v in result['bbox']]
                erased[max(0,y1-5):y2+5, max(0,x1-5):x2+5] = 128
                # Recompute scene alignment after masking; do not feed the true
                # transformation as if the runtime had a perfect oracle.
                rejected = locate(source, erased, ref['bbox'])
                controls.append(dict(instrument_id=result['instrument_id'],
                    target_sha256=frame['sha256'], change='synthetic_target_erased',
                    expected='uncertain', actual=rejected['status'],
                    passed=rejected['status']=='uncertain', result=rejected))
    count = sum(len(r['results']) for r in records)
    matched = sum(r['status']=='matched_reference' for frame in records for r in frame['results'])
    report = dict(created_at=datetime.now(timezone.utc).isoformat(),
        evidence_kind='read_only_probe_comparison' if probe else 'historical_views_replay',
        map_id=map_id, registry_revision=state['revision'], plan_fingerprint=plan['fingerprint'],
        checks=count, matched=matched, uncertain=count-matched, records=records,
        controls=controls, physical_motion=False, new_captures_by_this_tool=0,
        identity_guarantee=False, meter_reading=False, independent_test_set=False,
        thresholds=dict(correlation=MIN_SCORE, inner_face=MIN_INNER, distinct_peak_gap=MIN_GAP),
        runtime=dict(python=sys.version.split()[0], opencv=cv2.__version__, numpy=np.__version__))
    (output/'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registry', required=True)
    parser.add_argument('--map-id', required=True)
    parser.add_argument('--images', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--probe', help='JSON metadata; image must be beside this file')
    parser.add_argument('--negative-controls', action='store_true')
    args = parser.parse_args()
    state = json.loads(Path(args.registry).read_text(encoding='utf-8'))
    probe_path = Path(args.probe) if args.probe else None
    probe = json.loads(probe_path.read_text(encoding='utf-8')) if probe_path else None
    result = check(state, args.map_id, args.images, args.output, probe,
                   probe_path.parent if probe_path else None, args.negative_controls)
    print(json.dumps({k:result[k] for k in ('evidence_kind','checks','matched','uncertain','physical_motion')},
                     ensure_ascii=True))
    if not all(c['passed'] for c in result['controls']):
        raise SystemExit(1)


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bounded live calibration and target sampling; may move the PTZ camera."""
import argparse
import json
import re
import time
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['calibrate', 'track', 'lease'])
    parser.add_argument('--url', default='http://192.168.1.4:8768')
    parser.add_argument('--seconds', type=float, default=20)
    parser.add_argument('--out', default='runs/reverify-20261008/control')
    args = parser.parse_args()
    assert 1 <= args.seconds <= 40
    root = Path(args.out)
    root.mkdir(parents=True, exist_ok=True)
    base = args.url.rstrip('/')
    opener = build_opener(ProxyHandler({}))

    def get(path):
        with opener.open(base + path, timeout=5) as response:
            return response.read()

    def status():
        return json.loads(get('/api/status').decode('utf-8'))

    token = re.search(r"const token='([a-f0-9]+)'", get('/').decode('utf-8')).group(1)

    def post(action):
        request = Request(base + '/api/' + action, data=b'', headers={'X-Control-Token': token})
        with opener.open(request, timeout=5) as response:
            return json.loads(response.read().decode('utf-8'))

    samples = []
    summary = dict(action=args.action, started=time.strftime('%Y-%m-%dT%H:%M:%S%z'))
    try:
        if args.action == 'lease':
            post('start')
            time.sleep(4.5)
            sample = status()
            samples.append(sample)
            assert not sample['enabled'], 'Other pages may still be sending heartbeats'
            assert not sample.get('error'), sample.get('error')
            summary['software_lease_stop_passed'] = True
        else:
            post('calibrate' if args.action == 'calibrate' else 'start')
            deadline = time.monotonic() + (45 if args.action == 'calibrate' else args.seconds)
            while time.monotonic() < deadline:
                post('heartbeat')
                sample = status()
                samples.append(sample)
                print(json.dumps({k: sample.get(k) for k in ['frames', 'mode', 'target', 'error', 'matrix']}, ensure_ascii=False), flush=True)
                if args.action == 'calibrate' and not sample['calibrating']:
                    assert sample['calibrated'], sample.get('error')
                    break
                if args.action == 'track':
                    assert not sample.get('error'), sample.get('error')
                    assert sample['enabled'], 'Tracking was disabled unexpectedly'
                time.sleep(.5)
            if args.action == 'calibrate':
                sample = status()
                assert sample['calibrated'] and not sample['calibrating']
                summary['calibration_passed'] = True
            else:
                unique = {}
                for sample in samples:
                    unique[sample.get('frames')] = sample
                target_frames = [s for s in unique.values() if s.get('target') and s.get('mode') == 'tracking']
                assert target_frames, 'No person locked: not a successful visual tracking test'
                errors = []
                for sample in target_frames:
                    x1, y1, x2, y2 = sample['target']['bbox']
                    ex, ey = (x1 + x2) / 2 - .5, (y1 + y2) / 2 - .5
                    errors.append(dict(frames=sample['frames'], ex=ex, ey=ey, centered=abs(ex)<=.08 and abs(ey)<=.10))
                summary.update(unique_frames=len(unique), target_frames=len(target_frames), centered_frames=sum(e['centered'] for e in errors), errors=errors)
                summary['target_acquisition_passed'] = True
                summary['center_observed'] = any(e['centered'] for e in errors)
                summary['motion_and_convergence_need_event_log_review'] = True
    finally:
        try:
            post('stop')
            final = status()
            summary['stop_confirmed'] = not final['enabled'] and not final.get('error')
            summary['final'] = final
        finally:
            summary['samples'] = samples
            summary['finished'] = time.strftime('%Y-%m-%dT%H:%M:%S%z')
            (root / (args.action + '.json')).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    assert summary.get('stop_confirmed'), 'Final STOP was not confirmed'
    print(json.dumps({k:v for k,v in summary.items() if k not in ('samples','final','errors')}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

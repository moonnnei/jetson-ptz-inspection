#!/usr/bin/env python3
"""Observe-only API checks of the revised tracker; Python 3.5+."""
import argparse
import base64
import json
import re
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://192.168.1.4:8768')
    parser.add_argument('--out', default='runs/reverify-20261008/api')
    args = parser.parse_args()
    root = Path(args.out)
    root.mkdir(parents=True, exist_ok=True)
    opener = build_opener(ProxyHandler({}))
    base = args.url.rstrip('/')

    def request(path, post=False, token=None):
        headers = {'X-Control-Token': token} if token else {}
        req = Request(base + path, data=b'' if post else None, headers=headers)
        try:
            with opener.open(req, timeout=5) as response:
                return response.status, response.read()
        except HTTPError as exc:
            return exc.code, exc.read()

    def status():
        code, data = request('/api/status')
        assert code == 200
        return json.loads(data.decode('utf-8'))

    code, html = request('/')
    assert code == 200
    token = re.search(r"const token='([a-f0-9]+)'", html.decode('utf-8')).group(1)
    results = {}
    code, unused = request('/api/stop', True)
    assert code == 403
    results['token_required'] = True
    code, unused = request('/missing')
    assert code == 404
    results['unknown_route_404'] = True
    samples = []
    for i in range(8):
        sample = status()
        assert sample['enabled'] is False
        assert sample['calibrating'] is False
        assert not sample.get('error'), sample.get('error')
        samples.append(sample)
        time.sleep(.7)
    assert samples[-1]['frames'] > samples[0]['frames']
    results['observe_frames_advance'] = True
    results['frames_first'] = samples[0]['frames']
    results['frames_last'] = samples[-1]['frames']
    code, data = request('/api/frame')
    frame = json.loads(data.decode('utf-8'))
    assert code == 200 and frame.get('jpeg')
    jpeg = base64.b64decode(frame.pop('jpeg'))
    assert jpeg.startswith(b'\xff\xd8')
    (root / 'preview.jpg').write_bytes(jpeg)
    results['preview_jpeg'] = True
    code, unused = request('/api/start', True, token)
    assert code == 409, 'Start without calibration must fail'
    assert status()['enabled'] is False
    results['uncalibrated_start_rejected'] = True
    code, unused = request('/api/stop', True, token)
    assert code == 200
    stopped = status()
    assert not stopped['enabled'] and not stopped.get('error')
    results['stop_api_confirmed'] = True
    results['passed'] = True
    results['samples'] = samples
    results['frame'] = frame
    results['time'] = time.strftime('%Y-%m-%dT%H:%M:%S%z')
    (root / 'api-smoke.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in results.items() if k not in ('samples', 'frame')}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

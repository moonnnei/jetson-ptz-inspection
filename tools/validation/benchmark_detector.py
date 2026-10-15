#!/usr/bin/env python3
"""Benchmark a compiled detector with local JPEGs only; no camera/PTZ access.

Python 3.5+ standard library. Timing units are milliseconds. Legacy inference_ms
is preprocessing + network; total_process_ms includes decode, optical flow and
optional preview encoding, but excludes IPC waiting/output. control_result_ms
ends after detection postprocessing; wall_ms is the full request.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import queue
import statistics
import struct
import subprocess
import threading
import time

NO_FLOW = 0x80000000
NO_PREVIEW = 0x40000000
HOLD_FLOW_REFERENCE = 0x20000000
LENGTH_MASK = 0x1fffffff
MAX_JPEG = 12000000
STAGES = ('decode_ms', 'preprocess_ms', 'network_ms', 'flow_ms',
          'postprocess_ms', 'preview_ms')
METRICS = STAGES + ('inference_ms', 'control_result_ms', 'total_process_ms', 'wall_ms')


def request_bytes(jpeg, flow_enabled, preview_enabled=True, hold_reference=False):
    if not 4 <= len(jpeg) <= MAX_JPEG:
        raise ValueError('JPEG length is outside the detector protocol limit')
    if hold_reference and not flow_enabled:
        raise ValueError('Holding the flow reference requires optical flow')
    header = len(jpeg) | (0 if flow_enabled else NO_FLOW)
    header |= 0 if preview_enabled else NO_PREVIEW
    header |= HOLD_FLOW_REFERENCE if hold_reference else 0
    return struct.pack('<I', header) + jpeg


def read_exact(stream, size):
    parts = []
    while size:
        data = stream.read(size)
        if not data:
            raise RuntimeError('Detector exited or truncated its response')
        parts.append(data)
        size -= len(data)
    return b''.join(parts)


def read_preview(stream, size, preview_enabled):
    if not preview_enabled:
        if size != 0:
            raise RuntimeError('Detector generated an unrequested preview')
        return b''
    if size < 4 or size > MAX_JPEG:
        raise RuntimeError('Invalid detector preview length')
    preview = read_exact(stream, size)
    if not preview.startswith(b'\xff\xd8') or not preview.endswith(b'\xff\xd9'):
        raise RuntimeError('Detector did not return a JPEG preview')
    return preview


class Pipe:
    def __init__(self, args):
        model = args.models
        command = [str(args.detector), str(model / 'model.ncnn.param'),
                   str(model / 'model.ncnn.bin'), str(args.rotation),
                   str(args.threads)]
        if args.cv_threads is not None:
            command.append(str(args.cv_threads))
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, bufsize=0)
        self.timeout = args.timeout

    def infer(self, jpeg, flow_enabled, preview_enabled=True, hold_reference=False):
        payload = request_bytes(jpeg, flow_enabled, preview_enabled, hold_reference)
        result = queue.Queue()

        def exchange():
            try:
                start = time.monotonic()
                # Handle short writes as well as pipe backpressure. The owning
                # thread kills a stalled child on timeout, unblocking this I/O.
                remaining = memoryview(payload)
                while remaining:
                    written = self.process.stdin.write(remaining)
                    if not written:
                        raise RuntimeError('Detector input pipe closed')
                    remaining = remaining[written:]
                self.process.stdin.flush()
                size = struct.unpack('<I', read_exact(self.process.stdout, 4))[0]
                if size < 2 or size > 1000000:
                    raise RuntimeError('Invalid detector JSON length')
                decoded = json.loads(read_exact(self.process.stdout, size).decode('utf-8'))
                size = struct.unpack('<I', read_exact(self.process.stdout, 4))[0]
                preview = read_preview(self.process.stdout, size, preview_enabled)
                decoded['wall_ms'] = (time.monotonic() - start) * 1000
                decoded['preview_bytes'] = len(preview)
                result.put((decoded, None))
            except Exception as error:
                result.put((None, error))

        worker = threading.Thread(target=exchange)
        worker.daemon = True
        worker.start()
        try:
            decoded, error = result.get(timeout=self.timeout)
        except queue.Empty:
            self.close()
            raise RuntimeError('Detector request timeout')
        if error:
            raise error
        validate_result(decoded, flow_enabled, preview_enabled, hold_reference)
        return decoded

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        for stream in (self.process.stdin, self.process.stdout):
            if stream:
                stream.close()


def validate_result(result, flow_enabled, preview_enabled=True, hold_reference=False):
    if result.get('flow_enabled') is not flow_enabled:
        raise ValueError('Detector does not support the requested flow flag')
    if result.get('preview_generated') is not preview_enabled:
        raise ValueError('Detector does not support the requested preview flag')
    if hold_reference and not flow_enabled:
        raise ValueError('Holding the flow reference requires optical flow')
    if result.get('flow_reference_held') is not hold_reference:
        raise ValueError('Detector does not support the requested held reference flag')
    for name in METRICS:
        value = result.get(name)
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError('Invalid or absent timing: ' + name)
    tolerance = max(0.02, result['total_process_ms'] * 0.00001)
    if abs(result['inference_ms'] - result['preprocess_ms'] - result['network_ms']) > tolerance:
        raise ValueError('inference_ms changed its preprocessing + network meaning')
    if abs(result['total_process_ms'] - sum(result[name] for name in STAGES)) > tolerance:
        raise ValueError('Processing stages do not add up to total_process_ms')
    if abs(result['control_result_ms'] - sum(result[name] for name in STAGES[:-1])) > tolerance:
        raise ValueError('Control timing includes preview or excludes a detection stage')
    if not preview_enabled and result['preview_ms'] != 0:
        raise ValueError('Skipped preview has nonzero encoding time')
    if result['wall_ms'] + 2 < result['total_process_ms']:
        raise ValueError('Processing time exceeds the full IPC request time')
    if not flow_enabled and (result['shift'] != [0, 0] or result['shift_response'] != 0):
        raise ValueError('Light request returned stale optical flow')
    for person in result['persons']:
        box, score = person['bbox'], person['confidence']
        if len(box) != 4 or not all(math.isfinite(x) and 0 <= x <= 1 for x in box):
            raise ValueError('Invalid normalized person coordinates')
        if box[2] <= box[0] or box[3] <= box[1] or not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError('Invalid person box or confidence')


def same_persons(left, right, tolerance=0.00001):
    if len(left) != len(right):
        return False
    for a, b in zip(left, right):
        if abs(a['confidence'] - b['confidence']) > tolerance:
            return False
        if any(abs(x-y) > tolerance for x, y in zip(a['bbox'], b['bbox'])):
            return False
    return True


def percentile(values, fraction):
    values = sorted(values)
    if len(values) == 1:
        return values[0]
    position = (len(values)-1) * fraction
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    return values[lower] + (values[upper]-values[lower]) * (position-lower)


def summarize(records):
    result = {}
    for mode in sorted(set(row['mode'] for row in records)):
        subset = [row for row in records if row['mode'] == mode and not row['warmup']]
        result[mode] = {'count': len(subset), 'metrics': {}}
        for name in METRICS:
            values = [row[name] for row in subset]
            result[mode]['metrics'][name] = {'median': statistics.median(values),
                                           'p95': percentile(values, 0.95)}
    return result


def run(args):
    images = []
    for path in args.image:
        jpeg = path.read_bytes()
        if not jpeg.startswith(b'\xff\xd8'):
            raise ValueError('Use original JPEG samples, not annotated previews or PNG: ' + str(path))
        images.append((path.name, hashlib.sha256(jpeg).hexdigest(), jpeg))
    records, golden = [], {}
    modes = ('legacy', 'light') if args.mode == 'both' else (args.mode,)
    preview_mode = getattr(args, 'preview_mode', 'all')
    previews = (True, False) if preview_mode == 'both' else (preview_mode == 'all',)
    for mode in modes:
        for preview_enabled in previews:
            pipe = Pipe(args)
            try:
                for index in range(args.warmup + args.runs):
                    name, digest, jpeg = images[index % len(images)]
                    row = pipe.infer(jpeg, mode == 'legacy', preview_enabled)
                    if digest in golden and not same_persons(golden[digest], row['persons']):
                        raise ValueError('Person outputs changed for identical JPEG: ' + name)
                    golden[digest] = row['persons']
                    label = mode if preview_enabled else mode + '-no-preview'
                    row.update(mode=label, index=index, warmup=index < args.warmup,
                               image=name, image_sha256=digest)
                    records.append(row)
            finally:
                pipe.close()
    # A mode change must discard the old reference, including when returning
    # to the legacy calibration path. This contract check is not in benchmarks.
    pipe = Pipe(args)
    try:
        first = images[0][2]
        other = images[-1][2]
        pipe.infer(first, True)
        pipe.infer(other, True)
        pipe.infer(first, False)
        reset = pipe.infer(other, True)
        if reset['shift'] != [0, 0] or reset['shift_response'] != 0:
            raise ValueError('Legacy flow history survived a light request')
        # Exercise all flag combinations even when timing only one mode. A
        # zero-length preview must not shift the following response boundary.
        reference = pipe.infer(first, True, True)['persons']
        for flow_enabled, preview_enabled in ((True, False), (False, True),
                                               (False, False), (True, True)):
            current = pipe.infer(first, flow_enabled, preview_enabled)
            if not same_persons(reference, current['persons']):
                raise ValueError('Preview flag changed person output')
        # Hold every post-motion sample against one immutable before-motion
        # image, then verify a normal request advances and no-flow resets it.
        pipe.infer(first, False, False)
        pipe.infer(first, True, False)
        held_a = pipe.infer(other, True, False, True)
        held_b = pipe.infer(other, True, False, True)
        if (any(abs(a-b) > 0.00001 for a, b in zip(held_a['shift'], held_b['shift']))
                or abs(held_a['shift_response'] - held_b['shift_response']) > 0.00001):
            raise ValueError('Held optical-flow reference advanced unexpectedly')
        if not same_persons(held_a['persons'], held_b['persons']):
            raise ValueError('Held optical-flow reference changed person output')
        pipe.infer(other, True, False)
        advanced = pipe.infer(other, True, False, True)
        if any(abs(x) > 0.00001 for x in advanced['shift']):
            raise ValueError('Normal optical-flow request did not advance reference')
        pipe.infer(first, False, False)
        reset_held = pipe.infer(other, True, False, True)
        if reset_held['shift'] != [0, 0] or reset_held['shift_response'] != 0:
            raise ValueError('Held optical-flow reference survived a light request')
    finally:
        pipe.close()
    detected = any(row['persons'] for row in records)
    if args.require_person and not detected:
        raise ValueError('No person detected: this sample cannot validate person outputs')
    summary = {'config': {'threads': args.threads, 'cv_threads': args.cv_threads,
                          'rotation': args.rotation, 'warmup': args.warmup,
                          'runs_per_mode': args.runs, 'mode': args.mode,
                          'preview_mode': preview_mode},
               'metrics': summarize(records),
               'contracts': {'flow_history_reset': True,
                             'held_reference_fixed_pair_repeatable': True,
                             'held_reference_advance_and_reset': True,
                             'preview_protocol_all_flags': True,
                             'same_jpeg_person_outputs_equal': True,
                             'person_detected': detected},
               'timing_scope': 'control: decode through postprocess; process: decode through optional preview encode; wall: full IPC request',
               'accuracy_scope': 'Same-input consistency only; no dataset accuracy evaluation.'}
    if args.output_dir:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        with (args.output_dir / 'frames.jsonl').open('w', encoding='utf-8') as stream:
            for row in records:
                stream.write(json.dumps(row, sort_keys=True) + '\n')
        (args.output_dir / 'summary.json').write_text(json.dumps(summary, indent=2, sort_keys=True), encoding='utf-8')
    print(json.dumps(summary, indent=2, sort_keys=True))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--detector', type=Path, required=True)
    parser.add_argument('--models', type=Path, required=True,
                        help='Directory containing model.ncnn.param and model.ncnn.bin')
    parser.add_argument('--image', type=Path, action='append', required=True)
    parser.add_argument('--mode', choices=('legacy', 'light', 'both'), default='both')
    parser.add_argument('--preview-mode', choices=('all', 'none', 'both'), default='all',
                        help='Generate every preview, skip previews, or compare both')
    parser.add_argument('--threads', type=int, choices=range(1, 7), default=4)
    parser.add_argument('--cv-threads', type=int, choices=range(1, 7), default=None)
    parser.add_argument('--rotation', type=int, choices=(0, 90, 180, 270), default=0)
    parser.add_argument('--warmup', type=int, default=3)
    parser.add_argument('--runs', type=int, default=20)
    parser.add_argument('--timeout', type=float, default=10)
    parser.add_argument('--require-person', action='store_true')
    parser.add_argument('--output-dir', type=Path)
    args = parser.parse_args()
    if args.warmup < 0 or args.runs < 1 or args.timeout <= 0:
        parser.error('warmup >= 0, runs >= 1 and timeout > 0 are required')
    run(args)


if __name__ == '__main__':
    main()

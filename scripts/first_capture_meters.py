#!/usr/bin/env python3
"""Prepare or execute first captures for an entire human-confirmed meter list."""
import argparse
import getpass
import json
from pathlib import Path
import signal
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src/inspection_console'))
from capture_review_views import SnapshotCamera, motion_lease
from first_capture import FirstCaptureBatch, first_capture_plan, validate_first_capture
from first_capture_history import import_first_capture
from first_capture_runtime import interlocks, io_factory
from capture_round import round_plan
from scan_capture import ISAPIScanIO
from store import Store, identifier


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--console-root', required=True)
    parser.add_argument('--camera', required=True)
    parser.add_argument('--user', required=True)
    parser.add_argument('--map', required=True)
    parser.add_argument('--mode', choices=('strict','adaptive'), default='adaptive')
    parser.add_argument('--password-stdin', action='store_true')
    parser.add_argument('--plan-out')
    parser.add_argument('--plan')
    parser.add_argument('--output')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--acknowledge-plan')
    args = parser.parse_args()
    if args.execute and (not sys.platform.startswith('linux') or not args.plan or not args.output or args.plan_out):
        parser.error('execute needs Linux, --plan, --output and --acknowledge-plan')
    if not args.execute and (not args.plan_out or args.plan or args.output):
        parser.error('read-only preparation needs a new --plan-out')
    auth = json.load(sys.stdin) if args.password_stdin else {'password': getpass.getpass('Camera password: ')}
    camera = SnapshotCamera(args.camera, args.user, auth.pop('password'))
    console = Path(args.console_root)
    store = Store(console / 'var/inspection-console')
    if not args.execute:
        with motion_lease():
            interlocks()
            anchor = ISAPIScanIO(camera).preflight()['anchor']
            plan = first_capture_plan(store.snapshot(), args.map, anchor, identifier('attempt'),args.mode)
        with Path(args.plan_out).open('x', encoding='utf-8') as stream:
            json.dump(plan, stream, ensure_ascii=False, indent=2)
        print(json.dumps(dict(motion_sent=False, plan=plan)))
        return 0
    plan = json.loads(Path(args.plan).read_text(encoding='utf-8'))
    validate_first_capture(plan, store.snapshot())
    if plan['map_id'] != args.map or plan['mode']!=args.mode or args.acknowledge_plan != plan['fingerprint']:
        parser.error('map and acknowledgment must match the prepared plan')
    cancel = threading.Event()
    def stop(*unused):
        cancel.set()
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    with motion_lease():
        interlocks()
        batch = FirstCaptureBatch(io_factory(camera, ROOT, console, plan['references']),
                                  store.snapshot, args.output, plan, cancel)
        result = batch.run()
        imported = import_first_capture(store, args.output, plan['revision'])
        next_plan = round_plan(store.snapshot(), args.map, plan['anchor'])
    print(json.dumps(dict(status=result['status'], stop_confirmed=result['stop_confirmed'],
                          imported=imported, next_round_ready=next_plan['ready_for_trial'],
                          next_round_plan=next_plan, periodic_enabled=False)))
    return 0 if result['status'] == 'completed' else 1


if __name__ == '__main__':
    sys.exit(main())

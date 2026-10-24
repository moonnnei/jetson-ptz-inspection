#!/usr/bin/env python3
"""Prepare or explicitly execute one finite automatic meter revisit trial.

The periodic scheduler remains disabled. Passwords arrive on stdin or getpass.
"""
import argparse
import getpass
import json
from pathlib import Path
import signal
import sys
import threading

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src/inspection_console'))
from capture_review_views import SnapshotCamera,motion_lease,check_other_services
from capture_ring_views import check_additional_services
from capture_round import round_plan,validate_round,CaptureRound
from capture_round_io import CaptureRoundIO
from references import read_reference_image
from scan_capture import ISAPIScanIO
from store import Store


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--console-root',required=True)
    parser.add_argument('--camera',required=True)
    parser.add_argument('--user',required=True)
    parser.add_argument('--map',required=True)
    parser.add_argument('--password-stdin',action='store_true')
    parser.add_argument('--plan-out')
    parser.add_argument('--plan')
    parser.add_argument('--output')
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--acknowledge-plan')
    args=parser.parse_args()
    if args.execute and (not sys.platform.startswith('linux') or not args.plan or not args.output or args.plan_out):
        parser.error('execution needs Linux, --plan, --output and a saved-plan acknowledgment')
    if not args.execute and (not args.plan_out or args.plan or args.output):
        parser.error('preparation needs a new --plan-out')
    auth=json.load(sys.stdin) if args.password_stdin else {'password':getpass.getpass('Camera password: ')}
    camera=SnapshotCamera(args.camera,args.user,auth.pop('password'))
    console=Path(args.console_root);store=Store(console/'var/inspection-console')
    def interlocks():check_other_services();check_additional_services()
    interlocks()
    if not args.execute:
        anchor=ISAPIScanIO(camera).preflight()['anchor']
        plan=round_plan(store.snapshot(),args.map,anchor)
        for target in plan['stops']:
            read_reference_image(target['reference'],console/'var/inspection-console/images')
        with Path(args.plan_out).open('x',encoding='utf-8') as stream:json.dump(plan,stream,ensure_ascii=False,indent=2)
        print(json.dumps(dict(motion_sent=False,plan=plan)));return 0
    plan=json.loads(Path(args.plan).read_text(encoding='utf-8'))
    validate_round(plan,store.snapshot())
    if plan['map_id']!=args.map or args.acknowledge_plan!=plan['fingerprint']:
        parser.error('map and explicit acknowledgment must match this saved plan')
    cancel=threading.Event()
    def stopping(*unused):cancel.set()
    signal.signal(signal.SIGINT,stopping);signal.signal(signal.SIGTERM,stopping)
    with motion_lease():
        interlocks()
        io=CaptureRoundIO(camera,ROOT,console,plan,interlocks)
        result=CaptureRound(io,args.output,plan,cancel).run()
    print(json.dumps(dict(status=result['status'],results=result['results'],
        elapsed_seconds=result['elapsed_seconds'],stop_confirmed=result['stop_confirmed'],
        visit_pose_verified=False,periodic_capture_enabled=False)))
    return 0 if result['status']=='completed' else 1


if __name__=='__main__':sys.exit(main())

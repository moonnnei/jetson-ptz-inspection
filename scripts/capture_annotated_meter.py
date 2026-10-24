#!/usr/bin/env python3
"""Prepare or execute ONE finite annotation-driven commissioning attempt.

Preparation reads camera state only. --execute consumes a fresh saved plan.
This does not enable periodic execution, upload images, or modify old records.
"""
import argparse
import getpass
import json
import os
from pathlib import Path
import signal
import sys
import threading
from urllib.parse import urlsplit

from capture_review_views import ROOT, SnapshotCamera, motion_lease, check_other_services
from capture_ring_views import check_additional_services
from annotation_capture import AnnotatedCapture, annotation_plan, validate_annotation
from annotation_io import AnnotationIO
from scan_capture import ISAPIScanIO
from store import Store, identifier


def interlocks():
    check_other_services()
    check_additional_services()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--camera',required=True)
    p.add_argument('--user',required=True)
    p.add_argument('--console-root',required=True)
    p.add_argument('--map')
    p.add_argument('--instrument')
    p.add_argument('--password-stdin',action='store_true')
    p.add_argument('--plan-out')
    p.add_argument('--plan')
    p.add_argument('--execute',action='store_true')
    p.add_argument('--output')
    args=p.parse_args()
    if args.execute:
        if not sys.platform.startswith('linux') or not args.plan or not args.output or args.plan_out:
            p.error('execution needs Linux, --plan and a new --output')
    elif not args.map or not args.instrument or not args.plan_out or args.plan or args.output:
        p.error('preparation needs --map, --instrument and a new --plan-out')
    url=urlsplit(args.camera)
    if (url.scheme not in ('http','https') or not url.hostname or url.username or url.password
            or url.path not in ('','/') or url.query or url.fragment):
        p.error('camera must be an origin without credentials')
    secret=json.loads(sys.stdin.read())['password'] if args.password_stdin else getpass.getpass('Camera password: ')
    camera=SnapshotCamera(args.camera,args.user,secret)
    del secret
    console=Path(args.console_root)
    store=Store(console/'var/inspection-console')
    with motion_lease():
        interlocks()
        if not args.execute:
            anchor=ISAPIScanIO(camera).preflight()['anchor']
            plan=annotation_plan(store.snapshot(),args.map,anchor,args.instrument,identifier('annotation-attempt'))
            with Path(args.plan_out).open('x',encoding='utf8') as stream:
                json.dump(plan,stream,indent=2,sort_keys=True)
            print(json.dumps(dict(motion_sent=False,fingerprint=plan['fingerprint'],
                source_pose=plan['target']['references'][0]['source_pose'],coarse_pose=plan['coarse_pose'],
                pan_limits=plan['pan_limits'],tilt_limits=plan['tilt_limits'],zoom_limits=plan['zoom_limits'],
                budget_seconds=plan['budget_seconds'],plan_path=args.plan_out)))
            return 0
        plan=validate_annotation(json.loads(Path(args.plan).read_text(encoding='utf8')),store.snapshot())
        used=ROOT/'var/annotation-plans-used'
        used.mkdir(parents=True,exist_ok=True)
        # O_EXCL means a finished or interrupted attempt cannot be replayed.
        with (used/plan['fingerprint']).open('x',encoding='utf8') as receipt:
            receipt.write(plan['attempt_id'])
        cancel=threading.Event()
        signal.signal(signal.SIGTERM,lambda *unused:cancel.set())
        signal.signal(signal.SIGINT,lambda *unused:cancel.set())
        io=AnnotationIO(camera,ROOT,console,interlocks)
        result=AnnotatedCapture(io,args.output,plan,cancel).run()
        print(json.dumps(dict(status=result['status'],error=result.get('error'),
            elapsed_seconds=result['elapsed_seconds'],captures=len(result['captures']),
            preset_commissioned=result['preset_commissioned'],stop_confirmed=result['stop_confirmed'],
            visit_pose=result.get('visit_pose'),directory=args.output)))
        return 0 if result['status']=='completed' else 1


if __name__=='__main__':sys.exit(main())
